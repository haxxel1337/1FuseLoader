from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CHARGER_CURRENT_ENTITY, CONF_DEVICE_ID, CONF_BMW_DEVICE_ID, CONF_L1_ENTITY, CONF_L2_ENTITY,
    CONF_L3_ENTITY, CONF_STATUS_ENTITY, CONF_CONTROLLED_PHASE, DEFAULT_OPTIONS,
    DOMAIN, PLATFORMS, validate_options,
)
from .easee_adapter import EaseeCommandAdapter
from .bmw_context import BMWContext
from .logic import OneFuseRegulator, RegulatorInput, RegulatorSettings

_LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT_SECONDS = 20


def _get_state_value(hass: HomeAssistant, entity_id: str | None) -> Any:
    state = hass.states.get(entity_id) if entity_id else None
    return state.state if state is not None else None


def _settings_from_entry(entry: ConfigEntry) -> RegulatorSettings:
    options = validate_options(entry.options or {})
    phase = str((entry.data or {}).get(CONF_CONTROLLED_PHASE, "L1"))
    if phase != "L1":
        raise ValueError("Only controlled phase L1 is supported")
    return RegulatorSettings(**options, controlled_phase=phase)


class OneFuseLoaderController:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, command_adapter: Any = None) -> None:
        self.hass = hass
        self.entry = entry
        self.regulator = OneFuseRegulator(_settings_from_entry(entry))
        self.last_decision = None
        self.last_service_result = "not_sent"
        self.last_service_error: str | None = None
        self.control_block_reason: str | None = "Easee adapter has not been initialized."
        self.unsub_timer = None
        self.unsub_states = None
        self.listeners: list[Any] = []
        self._lock = asyncio.Lock()
        self._stopped = False
        self._generation = 0
        self._pending_refresh = False
        self._refresh_task: asyncio.Task | None = None
        self._bmw_context = BMWContext(hass, entry.data.get(CONF_BMW_DEVICE_ID))
        self.bmw_context: dict[str, Any] = {}
        self._adapter = command_adapter
        if command_adapter is not None:
            self.control_block_reason = command_adapter.control_block_reason

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def settings(self) -> RegulatorSettings:
        return self.regulator.settings

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    @callback
    def notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception as err:
                # A display listener must not prevent a protective control action.
                _LOGGER.warning("1FuseLoader listener failed (%s)", type(err).__name__)

    def _reload_settings_locked(self) -> bool:
        settings = _settings_from_entry(self.entry)
        if settings == self.settings:
            return False
        self.regulator.update_settings(settings)
        return True

    async def async_set_option(self, key: str, value: Any) -> None:
        self._generation += 1
        async with self._lock:
            options = dict(self.entry.options or {})
            options[key] = value
            options = validate_options(options)
            self.hass.config_entries.async_update_entry(self.entry, options=options)
            self._reload_settings_locked()
            if not self._stopped:
                await self._async_tick_locked()

    async def async_set_switch(self, key: str, value: bool) -> None:
        await self.async_set_option(key, bool(value))

    async def async_reconfigure(self) -> None:
        self._generation += 1
        async with self._lock:
            changed = self._reload_settings_locked()
            if changed and not self._stopped:
                await self._async_tick_locked()

    async def async_reset_state(self) -> None:
        self._generation += 1
        async with self._lock:
            self.regulator.reset()
            if not self._stopped:
                await self._async_tick_locked()

    async def async_start(self) -> None:
        async with self._lock:
            self._stopped = False
            self._reload_settings_locked()
            source_entities = [self.entry.data.get(key) for key in (
                CONF_L1_ENTITY, CONF_L2_ENTITY, CONF_L3_ENTITY,
                CONF_STATUS_ENTITY, CONF_CHARGER_CURRENT_ENTITY,
            ) if self.entry.data.get(key)]
            self.unsub_states = async_track_state_change_event(
                self.hass, source_entities, self._handle_source_change,
            )
            # Preserve the user's active/dry-run preferences across HA restarts.
            # Pause ownership is deliberately memory-only, so a restart cannot
            # resume a car which a person may have paused in the meantime.
            if self._adapter is None:
                self._adapter = EaseeCommandAdapter(self.hass, self.entry.data.get(CONF_DEVICE_ID))
            try:
                await self._adapter.async_initialize()
                self.control_block_reason = self._adapter.control_block_reason
            except Exception as err:
                self.control_block_reason = f"Easee adapter initialization failed ({type(err).__name__})."
                _LOGGER.warning("1FuseLoader adapter initialization failed (%s)", type(err).__name__)
            await self._async_tick_locked()
            interval = int((self.entry.options or DEFAULT_OPTIONS).get("poll_interval_seconds", 5))

            async def interval_tick(_now: datetime) -> None:
                # HA classifies the registered callable before dispatch. A
                # plain lambda would run in its executor, outside this loop.
                await self.async_tick()

            self.unsub_timer = async_track_time_interval(
                self.hass,
                interval_tick,
                timedelta(seconds=max(5, interval)),
            )

    @callback
    def _handle_source_change(self, _event: Any) -> None:
        """One coalesced refresh follows the newest local/Easee telemetry."""
        if self._stopped:
            return
        self._pending_refresh = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.hass.async_create_task(self._async_process_refreshes())

    async def _async_process_refreshes(self) -> None:
        try:
            while self._pending_refresh and not self._stopped:
                self._pending_refresh = False
                generation = self._generation
                async with self._lock:
                    if self._stopped or generation != self._generation:
                        continue
                    await self._async_tick_locked()
        finally:
            self._refresh_task = None

    async def async_stop(self) -> None:
        # Mark stopped before waiting for a currently running request. Poll ticks
        # coalesce instead of queuing; none can submit after this method returns.
        self._stopped = True
        self._generation += 1
        self._pending_refresh = False
        if self.unsub_states:
            self.unsub_states()
            self.unsub_states = None
        if self.unsub_timer:
            self.unsub_timer()
            self.unsub_timer = None
        async with self._lock:
            pass
        pending = self._refresh_task
        if pending is not None and pending is not asyncio.current_task():
            await pending

    def _read_input(self) -> RegulatorInput:
        data = self.entry.data
        errors: list[str] = []
        grid_state = self.hass.states.get(data.get(CONF_L1_ENTITY))
        status_state = self.hass.states.get(data.get(CONF_STATUS_ENTITY))
        current_state = self.hass.states.get(data.get(CONF_CHARGER_CURRENT_ENTITY))
        for label, state, requires_amps in (
            ("L1", grid_state, True), ("Charger status", status_state, False),
            ("Charger current", current_state, True),
        ):
            if state is None:
                errors.append(f"{label} telemetry is missing.")
                continue
            attributes = getattr(state, "attributes", {})
            if attributes.get("restored"):
                errors.append(f"{label} telemetry is restored and has not been verified live.")
            if requires_amps and attributes.get("unit_of_measurement") != "A":
                errors.append(f"{label} telemetry must have unit A.")
        if grid_state is not None:
            reported = getattr(grid_state, "last_reported", None) or getattr(grid_state, "last_updated", None)
            if not isinstance(reported, datetime) or reported.tzinfo is None:
                errors.append("L1 telemetry has no trustworthy report timestamp.")
            else:
                age = (datetime.now(timezone.utc) - reported).total_seconds()
                if age < -5 or age > self.settings.max_sensor_age_seconds:
                    errors.append("L1 telemetry is stale or has an invalid timestamp.")
        # Easee states are event-driven: a stable value may be hours old. Its
        # adapter verifies source heartbeat/online health instead of entity age.
        if self._adapter is None:
            errors.append("Easee source health has not been verified.")
        else:
            health_error = self._adapter.telemetry_block_reason
            if health_error:
                errors.append(str(health_error))
        return RegulatorInput(
            now_ts=self.hass.loop.time(),
            status=str(status_state.state if status_state is not None else "unknown"),
            grid_l1=grid_state.state if grid_state is not None else None,
            grid_l2=_get_state_value(self.hass, data.get(CONF_L2_ENTITY)),
            grid_l3=_get_state_value(self.hass, data.get(CONF_L3_ENTITY)),
            charger_current=current_state.state if current_state is not None else None,
            data_valid=not errors,
            data_error=" ".join(errors) or None,
            grid_report_ts=self._report_timestamp(grid_state),
            charger_report_ts=self._report_timestamp(current_state),
        )

    @staticmethod
    def _report_timestamp(state: Any) -> float | None:
        reported = getattr(state, "last_reported", None)
        return reported.timestamp() if isinstance(reported, datetime) and reported.tzinfo else None

    async def async_tick(self) -> None:
        # Drop redundant timer ticks while an action or reconfiguration is in
        # progress; always evaluate the newest snapshot at the next interval.
        if self._stopped or self._lock.locked():
            return
        generation = self._generation
        async with self._lock:
            if self._stopped or generation != self._generation:
                return
            await self._async_tick_locked()

    async def _async_tick_locked(self) -> None:
        # BMW is optional advisory context. Failure cannot stop or delay the
        # local meter/Easee regulator and never causes a BMW API request.
        try:
            self.bmw_context = self._bmw_context.snapshot()
        except Exception:
            self.bmw_context = {}
        inp = self._read_input()
        decision = self.regulator.evaluate(inp)
        self.last_decision = decision
        self.last_service_error = None
        if not decision.should_call_service:
            self.last_service_result = "not_sent"
            self.notify()
            _LOGGER.debug("1FuseLoader decision: %s", asdict(decision))
            return
        if decision.dry_run:
            self.last_service_result = "dry_run"
            self.notify()
            _LOGGER.info("1FuseLoader dry-run decision: %s", asdict(decision))
            return
        if self._adapter is not None:
            self.control_block_reason = self._adapter.control_block_reason
        if self.control_block_reason or self._adapter is None:
            self.last_service_result = "blocked"
            self.last_decision = replace(
                decision, mode="control_blocked", reason=self.control_block_reason or "No verified command adapter.",
                should_call_service=False,
            )
            self.notify()
            return
        try:
            await asyncio.wait_for(self._adapter.async_execute(decision), ACTION_TIMEOUT_SECONDS)
        except Exception as err:
            self.last_service_result = "error"
            self.last_service_error = type(err).__name__
            self.last_decision = replace(
                decision, reason=f"Command submission failed ({type(err).__name__}); charger state is unverified.",
            )
            _LOGGER.warning("1FuseLoader command submission failed (%s)", type(err).__name__)
        else:
            self.regulator.acknowledge(decision, self.hass.loop.time())
            self.last_service_result = "submitted_unverified"
        self.notify()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller = OneFuseLoaderController(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    try:
        await controller.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, [Platform(p) for p in PLATFORMS])
    except BaseException:
        await controller.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.data[DOMAIN][entry.entry_id].async_reconfigure()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller: OneFuseLoaderController = hass.data[DOMAIN][entry.entry_id]
    await controller.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, [Platform(p) for p in PLATFORMS])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    else:
        await controller.async_start()
    return unload_ok
