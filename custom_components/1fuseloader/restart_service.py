"""Register the administrator action for a managed Home Assistant restart."""

from __future__ import annotations

import asyncio

import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .restart_bridge import BridgeRefused

RESTART_WITH_CHARGING = "restart_with_charging"
ENTRY_ID = "entry_id"


@callback
def async_setup_restart_service(hass: HomeAssistant) -> None:
    """Register a restart request that runs its charging checks before shutdown."""
    restart_lock = asyncio.Lock()

    def controller_for(entry_id: str | None):
        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id is None:
            if len(entries) != 1:
                raise ServiceValidationError("Select one 1FuseLoader configuration entry.")
            entry = entries[0]
        else:
            entry = next((item for item in entries if item.entry_id == entry_id), None)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("The selected 1FuseLoader configuration entry is not loaded.")
        controller = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if controller is None:
            raise ServiceValidationError("The selected 1FuseLoader controller is not ready.")
        return entry.entry_id, controller

    async def async_handle_restart(call: ServiceCall) -> None:
        # The Core admin wrapper also allows trusted system calls without a
        # user. This action requires an explicit administrator user context.
        if not call.context.user_id:
            raise Unauthorized(context=call.context)
        if restart_lock.locked():
            raise HomeAssistantError("A 1FuseLoader restart is already being prepared.")
        async with restart_lock:
            if hass.state is not CoreState.running or not hass.services.has_service(
                "homeassistant", "restart"
            ):
                raise HomeAssistantError("Home Assistant is not ready for a managed restart.")
            entry_id, controller = controller_for(call.data.get(ENTRY_ID))
            await hass.services.async_call(
                "homeassistant", "check_config", blocking=True, context=call.context
            )

            def verify_controller() -> None:
                if hass.state is not CoreState.running or controller_for(entry_id)[1] is not controller:
                    raise HomeAssistantError("The Home Assistant restart context has changed.")

            async def async_request_restart() -> None:
                verify_controller()
                # The built-in restart retains the Core/Supervisor check
                # and stop handler, including revalidating the configuration.
                await hass.services.async_call(
                    "homeassistant", "restart", blocking=True, context=call.context
                )

            verify_controller()
            try:
                restarted = await controller.async_restart_with_charging(async_request_restart)
            except BridgeRefused as err:
                raise HomeAssistantError(f"Managed restart postponed ({err}).") from err
            if restarted is False:
                raise HomeAssistantError("Managed restart postponed. Charging was not verified.")

    async_register_admin_service(
        hass,
        DOMAIN,
        RESTART_WITH_CHARGING,
        async_handle_restart,
        probatio.Schema({probatio.Optional(ENTRY_ID): str}),
    )
