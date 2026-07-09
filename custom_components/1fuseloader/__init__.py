from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CHARGER_CURRENT_ENTITY,
    CONF_DEVICE_ID,
    CONF_L1_ENTITY,
    CONF_L2_ENTITY,
    CONF_L3_ENTITY,
    CONF_STATUS_ENTITY,
    CONF_CONTROLLED_PHASE,
    DEFAULT_OPTIONS,
    DOMAIN,
    PLATFORMS,
)
from .logic import OneFuseRegulator, RegulatorInput, RegulatorSettings

_LOGGER = logging.getLogger(__name__)


def _get_state_float(hass: HomeAssistant, entity_id: str | None, default: Any = None) -> Any:
    if not entity_id:
        return default
    state = hass.states.get(entity_id)
    if state is None:
        return default
    return state.state


def _settings_from_entry(entry: ConfigEntry) -> RegulatorSettings:
    opts = dict(DEFAULT_OPTIONS)
    opts.update(entry.options or {})
    data = entry.data or {}
    return RegulatorSettings(
        main_fuse_amps=float(opts["main_fuse_amps"]),
        buffer_amps=float(opts["buffer_amps"]),
        min_charge_amps=int(opts["min_charge_amps"]),
        max_charge_amps=int(opts["max_charge_amps"]),
        safe_l1_amps=float(opts["safe_l1_amps"]),
        soft_limit_amps=float(opts["soft_limit_amps"]),
        soft_for_seconds=int(opts["soft_for_seconds"]),
        soft_current_amps=int(opts["soft_current_amps"]),
        pause_limit_amps=float(opts["pause_limit_amps"]),
        pause_for_seconds=int(opts["pause_for_seconds"]),
        resume_below_amps=float(opts["resume_below_amps"]),
        resume_for_seconds=int(opts["resume_for_seconds"]),
        normal_ttl_seconds=int(opts["normal_ttl_seconds"]),
        soft_ttl_seconds=int(opts["soft_ttl_seconds"]),
        resend_limit_every_seconds=int(opts["resend_limit_every_seconds"]),
        min_seconds_between_actions=int(opts["min_seconds_between_actions"]),
        active_control=bool(opts["active_control"]),
        dry_run=bool(opts["dry_run"]),
        enable_dynamic_limit=bool(opts["enable_dynamic_limit"]),
        enable_soft_guard=bool(opts["enable_soft_guard"]),
        enable_pause_resume=bool(opts["enable_pause_resume"]),
        force_min_when_available_too_low=bool(opts["force_min_when_available_too_low"]),
        controlled_phase=str(data.get(CONF_CONTROLLED_PHASE, "L1")),
        unused_phase_current=int(opts["unused_phase_current"]),
    )


class OneFuseLoaderController:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.regulator = OneFuseRegulator(_settings_from_entry(entry))
        self.last_decision = None
        self.unsub_timer = None
        self.listeners: list[callable] = []

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def settings(self) -> RegulatorSettings:
        return self.regulator.settings

    def add_listener(self, listener: callable) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: callable) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    @callback
    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()

    def reload_settings(self) -> None:
        self.regulator.update_settings(_settings_from_entry(self.entry))

    async def async_set_option(self, key: str, value: Any) -> None:
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self.entry.options or {})
        opts[key] = value
        self.hass.config_entries.async_update_entry(self.entry, options=opts)
        self.reload_settings()
        await self.async_tick()

    async def async_set_switch(self, key: str, value: bool) -> None:
        await self.async_set_option(key, bool(value))

    async def async_reset_state(self) -> None:
        self.regulator.reset()
        await self.async_tick()

    async def async_start(self) -> None:
        self.reload_settings()
        await self.async_tick()
        interval = int((self.entry.options or DEFAULT_OPTIONS).get("poll_interval_seconds", 5))
        # poll_interval_seconds is intentionally not a Number entity yet to keep v0.2 small.
        self.unsub_timer = async_track_time_interval(
            self.hass,
            lambda now: self.hass.async_create_task(self.async_tick()),
            timedelta(seconds=max(5, interval)),
        )

    async def async_stop(self) -> None:
        if self.unsub_timer:
            self.unsub_timer()
            self.unsub_timer = None

    async def async_tick(self) -> None:
        data = self.entry.data
        status = _get_state_float(self.hass, data.get(CONF_STATUS_ENTITY), "unknown")
        inp = RegulatorInput(
            now_ts=self.hass.loop.time(),
            status=str(status),
            grid_l1=_get_state_float(self.hass, data.get(CONF_L1_ENTITY)),
            grid_l2=_get_state_float(self.hass, data.get(CONF_L2_ENTITY)),
            grid_l3=_get_state_float(self.hass, data.get(CONF_L3_ENTITY)),
            charger_current=_get_state_float(self.hass, data.get(CONF_CHARGER_CURRENT_ENTITY), 0),
        )
        decision = self.regulator.evaluate(inp)
        self.last_decision = decision
        self.notify()

        if not decision.should_call_service:
            _LOGGER.debug("1FuseLoader decision: %s", asdict(decision))
            return
        if decision.dry_run:
            _LOGGER.info("1FuseLoader dry-run decision: %s", asdict(decision))
            return

        device_id = data.get(CONF_DEVICE_ID)
        if decision.command == "set_limit":
            await self.hass.services.async_call(
                "easee",
                "set_circuit_dynamic_limit",
                {
                    "device_id": device_id,
                    "current_p1": decision.current_p1,
                    "current_p2": decision.current_p2,
                    "current_p3": decision.current_p3,
                    "time_to_live": decision.ttl_seconds,
                },
                blocking=False,
            )
        elif decision.command in {"pause", "resume"}:
            command = "Pause" if decision.command == "pause" else "Resume"
            await self.hass.services.async_call(
                "easee",
                "action_command",
                {"device_id": device_id, "command": command},
                blocking=False,
            )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller = OneFuseLoaderController(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    await controller.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, [Platform(p) for p in PLATFORMS])
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    controller: OneFuseLoaderController = hass.data[DOMAIN][entry.entry_id]
    controller.reload_settings()
    await controller.async_tick()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, [Platform(p) for p in PLATFORMS])
    if unload_ok:
        controller: OneFuseLoaderController = hass.data[DOMAIN].pop(entry.entry_id)
        await controller.async_stop()
    return unload_ok
