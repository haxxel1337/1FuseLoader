from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_CHARGER_CURRENT_ENTITY,
    CONF_CONTROLLED_PHASE,
    CONF_DEVICE_ID,
    CONF_L1_ENTITY,
    CONF_L2_ENTITY,
    CONF_L3_ENTITY,
    CONF_NAME,
    CONF_STATUS_ENTITY,
    DEFAULT_CHARGER_CURRENT_ENTITY,
    DEFAULT_CONTROLLED_PHASE,
    DEFAULT_DEVICE_ID,
    DEFAULT_L1_ENTITY,
    DEFAULT_L2_ENTITY,
    DEFAULT_L3_ENTITY,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    DEFAULT_STATUS_ENTITY,
    DOMAIN,
)


def _config_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID)): str,
            vol.Required(CONF_STATUS_ENTITY, default=d.get(CONF_STATUS_ENTITY, DEFAULT_STATUS_ENTITY)): str,
            vol.Required(CONF_L1_ENTITY, default=d.get(CONF_L1_ENTITY, DEFAULT_L1_ENTITY)): str,
            vol.Required(CONF_L2_ENTITY, default=d.get(CONF_L2_ENTITY, DEFAULT_L2_ENTITY)): str,
            vol.Required(CONF_L3_ENTITY, default=d.get(CONF_L3_ENTITY, DEFAULT_L3_ENTITY)): str,
            vol.Required(CONF_CHARGER_CURRENT_ENTITY, default=d.get(CONF_CHARGER_CURRENT_ENTITY, DEFAULT_CHARGER_CURRENT_ENTITY)): str,
            vol.Required(CONF_CONTROLLED_PHASE, default=d.get(CONF_CONTROLLED_PHASE, DEFAULT_CONTROLLED_PHASE)): vol.In(["L1", "L2", "L3"]),
        }
    )


class OneFuseLoaderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_DEVICE_ID] = str(user_input.get(CONF_DEVICE_ID, "")).strip()

            if not user_input[CONF_DEVICE_ID]:
                errors[CONF_DEVICE_ID] = "device_id_required"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                title = str(user_input.get(CONF_NAME, DEFAULT_NAME)).strip() or DEFAULT_NAME
                data = dict(user_input)
                data.pop(CONF_NAME, None)
                return self.async_create_entry(title=title, data=data, options=dict(DEFAULT_OPTIONS))

        return self.async_show_form(step_id="user", data_schema=_config_schema(user_input), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the options flow.

        HA 2026.7 exposes the current config entry as self.config_entry
        inside OptionsFlow; do not pass/store it manually.
        """
        return OneFuseLoaderOptionsFlow()


class OneFuseLoaderOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self.config_entry.options or {})

        schema = vol.Schema(
            {
                vol.Required("main_fuse_amps", default=float(opts["main_fuse_amps"])): vol.Coerce(float),
                vol.Required("buffer_amps", default=float(opts["buffer_amps"])): vol.Coerce(float),
                vol.Required("min_charge_amps", default=int(opts["min_charge_amps"])): vol.Coerce(int),
                vol.Required("max_charge_amps", default=int(opts["max_charge_amps"])): vol.Coerce(int),
                vol.Required("safe_l1_amps", default=float(opts["safe_l1_amps"])): vol.Coerce(float),
                vol.Required("soft_limit_amps", default=float(opts["soft_limit_amps"])): vol.Coerce(float),
                vol.Required("soft_for_seconds", default=int(opts["soft_for_seconds"])): vol.Coerce(int),
                vol.Required("soft_current_amps", default=int(opts["soft_current_amps"])): vol.Coerce(int),
                vol.Required("pause_limit_amps", default=float(opts["pause_limit_amps"])): vol.Coerce(float),
                vol.Required("pause_for_seconds", default=int(opts["pause_for_seconds"])): vol.Coerce(int),
                vol.Required("resume_below_amps", default=float(opts["resume_below_amps"])): vol.Coerce(float),
                vol.Required("resume_for_seconds", default=int(opts["resume_for_seconds"])): vol.Coerce(int),
                vol.Required("normal_ttl_seconds", default=int(opts["normal_ttl_seconds"])): vol.Coerce(int),
                vol.Required("soft_ttl_seconds", default=int(opts["soft_ttl_seconds"])): vol.Coerce(int),
                vol.Required("resend_limit_every_seconds", default=int(opts["resend_limit_every_seconds"])): vol.Coerce(int),
                vol.Required("min_seconds_between_actions", default=int(opts["min_seconds_between_actions"])): vol.Coerce(int),
                vol.Required("unused_phase_current", default=int(opts["unused_phase_current"])): vol.Coerce(int),
                vol.Required("active_control", default=bool(opts["active_control"])): bool,
                vol.Required("dry_run", default=bool(opts["dry_run"])): bool,
                vol.Required("enable_dynamic_limit", default=bool(opts["enable_dynamic_limit"])): bool,
                vol.Required("enable_soft_guard", default=bool(opts["enable_soft_guard"])): bool,
                vol.Required("enable_pause_resume", default=bool(opts["enable_pause_resume"])): bool,
                vol.Required("force_min_when_available_too_low", default=bool(opts["force_min_when_available_too_low"])): bool,
            }
        )
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
