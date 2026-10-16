from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CHARGER_CURRENT_ENTITY, CONF_DEVICE_ID, CONF_BMW_DEVICE_ID, CONF_L1_ENTITY, CONF_L2_ENTITY,
    CONF_L3_ENTITY, CONF_STATUS_ENTITY, CONF_CONTROLLED_PHASE, DEFAULT_OPTIONS,
    DOMAIN, PLATFORMS, validate_options,
)
from .easee_adapter import EaseeCommandAdapter
from .bmw_context import BMWContext
from .logic import ALLOWED_ACTIVE_STATUSES, IDLE_STATUSES, OneFuseRegulator, RegulatorInput, RegulatorSettings
from .overload_persistence import OverloadBudgetStore

_LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT_SECONDS = 20
EXPOSURE_STORE_TIMEOUT_SECONDS = 2
# One minute lease plus storage, request and scheduling allowances.
OVERLOAD_LEASE_RESERVE_SECONDS = 90


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
        # Keep actual command history separate from each watchdog evaluation.
        # These are memory-only diagnostic receipts, never physical proof or
        # pause ownership restored across controller restarts.
        self.last_command_attempt: dict[str, Any] | None = None
        self.last_accepted_command: dict[str, Any] | None = None
        self.last_accepted_limit: dict[str, Any] | None = None
        self.source_diagnostics: dict[str, Any] = {}
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
        self._budget_store = OverloadBudgetStore(Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.overload"))
        self._budget_loaded = False
        self._budget_error: str | None = None
        self._overload_restore_receipt_floor: float | None = None
        self._unsub_deadline = None
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
            if not self._budget_loaded:
                try:
                    saved = await asyncio.wait_for(self._budget_store.async_load(), EXPOSURE_STORE_TIMEOUT_SECONDS)
                    if saved is not None:
                        if not self.regulator.import_overload_state(saved):
                            raise ValueError("Invalid restored exposure")
                        if saved.get("minimum_held"):
                            self._overload_restore_receipt_floor = datetime.now(timezone.utc).timestamp()
                except Exception:
                    self._budget_error = "overload_budget_restore_failed"
                self._budget_loaded = True
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
        if self._unsub_deadline:
            self._unsub_deadline()
            self._unsub_deadline = None
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
        health_error = "easee_source_unverified"
        if self._adapter is None:
            errors.append("Easee source health has not been verified.")
        else:
            health_error = self._adapter.telemetry_block_reason
            if health_error:
                errors.append(str(health_error))
        self._read_source_diagnostics(grid_state, current_state, status_state, health_error, errors)
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
            wall_now_ts=datetime.now(timezone.utc).timestamp(),
        )

    @staticmethod
    def _report_timestamp(state: Any) -> float | None:
        reported = getattr(state, "last_reported", None)
        return reported.timestamp() if isinstance(reported, datetime) and reported.tzinfo else None

    @staticmethod
    def _utc_time(value: Any) -> datetime | None:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)

    def _read_source_diagnostics(self, grid: Any, current: Any, status: Any,
                                 health_error: str | None, input_errors: list[str]) -> None:
        """Read only whitelisted cached HA attributes from the selected device.

        A state report is HA receipt/publication time, not the measurement's
        physical acquisition time. Easee entities can contain private site
        details, so never copy their complete attribute dictionaries here.
        Diagnostic failure must not inhibit the protective control path.
        """
        source_errors = list(input_errors)
        for label, state in (("L1", grid), ("Charger current", current)):
            if state is None:
                continue  # Missing-state detail already comes from _read_input.
            value = getattr(state, "state", None)
            try:
                valid = not isinstance(value, bool) and isfinite(float(value)) and float(value) >= 0
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                source_errors.append(f"{label} telemetry must be a finite, non-negative current.")
        if status is not None and str(getattr(status, "state", "")).strip().lower() not in ALLOWED_ACTIVE_STATUSES | IDLE_STATUSES:
            source_errors.append("Charger status telemetry is unavailable, unsupported or a fault.")
        diagnostics: dict[str, Any] = {
            "source_health": "unhealthy" if source_errors else "healthy",
            "source_errors": source_errors,
            "source_health_reason": " ".join(source_errors) or "Required source checks passed.",
            "easee_source_health": health_error or "healthy",
            "source_health_scope": "required_input_values_and_source_heartbeat; see regulator reason for policy and alignment",
            "report_time_scope": "ha_receipt_not_physical_acquisition",
            "easee_stream_pulse_utc": None,
            "reported_circuit_p1": None, "reported_circuit_p2": None, "reported_circuit_p3": None,
            "reported_circuit_reported_at_utc": None,
            "reported_circuit_source": None,
            "reported_circuit_available": False,
        }
        for name, state in (("grid", grid), ("charger_current", current), ("charger_status", status)):
            reported = self._utc_time(getattr(state, "last_reported", None))
            diagnostics[f"{name}_reported_at_utc"] = reported.isoformat() if reported else None
        try:
            registry = er.async_get(self.hass)
            entries = er.async_entries_for_device(registry, self.entry.data.get(CONF_DEVICE_ID))
            candidates = []
            pulses = []
            for entity in entries:
                if entity.platform != "easee" or entity.disabled_by:
                    continue
                state = self.hass.states.get(entity.entity_id)
                if state is None or state.state in {"unavailable", "unknown"}:
                    continue
                attributes = getattr(state, "attributes", {})
                if attributes.get("restored"):
                    continue
                pulse = self._utc_time(attributes.get("state_latestPulse"))
                if pulse is not None:
                    pulses.append(pulse)
                values = [attributes.get(f"state_dynamicCircuitCurrentP{phase}") for phase in (1, 2, 3)]
                if any(isinstance(value, bool) for value in values):
                    continue
                try:
                    currents = tuple(float(value) for value in values)
                except (TypeError, ValueError, OverflowError):
                    continue
                if attributes.get("unit_of_measurement") != "A" or not all(isfinite(value) and 0 <= value <= 1000 for value in currents):
                    continue
                candidates.append((entity.entity_id, currents, self._utc_time(getattr(state, "last_reported", None))))
            if pulses:
                pulse = max(pulses)
                diagnostics["easee_stream_pulse_utc"] = pulse.isoformat()
            if len(candidates) == 1:
                entity_id, currents, reported = candidates[0]
                diagnostics.update({
                    **{f"reported_circuit_p{phase}": currents[phase - 1] for phase in (1, 2, 3)},
                    "reported_circuit_reported_at_utc": reported.isoformat() if reported else None,
                    "reported_circuit_source": entity_id,
                    "reported_circuit_available": True,
                })
            elif candidates:
                diagnostics["reported_circuit_source"] = "ambiguous_selected_device_sources"
        except Exception:
            # This is diagnostic only. Do not expose exception text/site data.
            diagnostics["reported_circuit_source"] = "cached_readback_unavailable"
        self.source_diagnostics = diagnostics

    def diagnostic_attributes(self) -> dict[str, Any]:
        accepted = self.last_accepted_limit
        reported = tuple(self.source_diagnostics.get(f"reported_circuit_p{phase}") for phase in (1, 2, 3))
        comparison = None
        post_acceptance = None
        if accepted is not None and self.source_diagnostics.get("reported_circuit_available"):
            comparison = reported == tuple(accepted[f"target_p{phase}"] for phase in (1, 2, 3))
            report_time = self._utc_time(self.source_diagnostics.get("reported_circuit_reported_at_utc"))
            accepted_time = self._utc_time(accepted.get("completed_at_utc"))
            if report_time is not None and accepted_time is not None:
                post_acceptance = report_time >= accepted_time
        return {
            **self.source_diagnostics,
            "last_command_attempt": dict(self.last_command_attempt) if self.last_command_attempt else None,
            "last_accepted_command": dict(self.last_accepted_command) if self.last_accepted_command else None,
            "last_accepted_limit": dict(accepted) if accepted else None,
            "reported_cap_matches_last_accepted_limit": comparison,
            "reported_cap_report_after_acceptance": post_acceptance,
            "readback_scope": "cached_circuit_cap_only",
            "physical_current_verified": False,
            "device_ttl_verified": False,
        }

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
        if self.settings.overload_enabled:
            offline_problem = getattr(self._adapter, "overload_block_reason", "offline_fallback_unverified")
            if offline_problem or self._budget_error:
                inp = replace(inp, data_valid=False, data_error=self._budget_error or offline_problem)
        decision = self.regulator.evaluate(inp)
        exposure = self.regulator.export_overload_state()
        if self.settings.overload_enabled or exposure["used_seconds"] or exposure["failure_reason"]:
            try:
                await asyncio.wait_for(self._budget_store.async_save(exposure), EXPOSURE_STORE_TIMEOUT_SECONDS)
            except Exception:
                self._budget_error = "overload_budget_save_failed"
        if self.settings.overload_enabled and decision.current_p1 > 0 and (
            self._budget_error or (
                decision.mode.removeprefix("dry_run_").startswith("overload")
                and (decision.overload_remaining_seconds < OVERLOAD_LEASE_RESERVE_SECONDS
                     or (self._overload_restore_receipt_floor is not None and (
                         inp.grid_report_ts is None or inp.charger_report_ts is None
                         or inp.grid_report_ts < self._overload_restore_receipt_floor
                         or inp.charger_report_ts < self._overload_restore_receipt_floor
                     )))
            )
        ):
            decision = replace(
                decision, mode="overload_control_blocked",
                reason=self._budget_error or "Overload requires a complete lease reserve and fresh post-restart reports.",
                command="set_limit", current_p1=0, current_p2=0, current_p3=0,
                should_call_service=self.settings.active_control,
            )
        self._schedule_overload_deadline(decision, max(0.0, self.hass.loop.time() - inp.now_ts))
        self.last_decision = decision
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
        attempt = {
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "completed_at_utc": None, "command": decision.command,
            "target_p1": decision.current_p1, "target_p2": decision.current_p2,
            "target_p3": decision.current_p3, "ttl_seconds": decision.ttl_seconds,
            "result": "pending", "error": None,
        }
        self.last_command_attempt = attempt
        self.last_service_error = None
        try:
            await asyncio.wait_for(self._adapter.async_execute(decision), ACTION_TIMEOUT_SECONDS)
        except Exception as err:
            self.last_service_result = "error"
            self.last_service_error = type(err).__name__
            attempt.update(result="error", error=self.last_service_error)
            self.last_decision = replace(
                decision, reason=f"Command submission failed ({type(err).__name__}); charger state is unverified.",
            )
            _LOGGER.warning("1FuseLoader command submission failed (%s)", type(err).__name__)
        else:
            self.regulator.acknowledge(decision, self.hass.loop.time())
            self.last_service_result = "submitted_unverified"
            attempt["result"] = "submitted_unverified"
        attempt["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        if attempt["result"] == "submitted_unverified":
            self.last_accepted_command = dict(attempt)
            if decision.command in {"set_limit", "resume"}:
                self.last_accepted_limit = dict(attempt)
        self.notify()

    def _schedule_overload_deadline(self, decision: Any, elapsed: float = 0.0) -> None:
        if self._unsub_deadline:
            self._unsub_deadline()
            self._unsub_deadline = None
        if not self.settings.overload_enabled or self._stopped:
            return
        delays = []
        if decision.overload_transition_remaining_seconds is not None:
            delays.append(decision.overload_transition_remaining_seconds)
        if decision.mode.removeprefix("dry_run_").startswith("overload") and decision.current_p1 > 0:
            delays.append(decision.overload_remaining_seconds - OVERLOAD_LEASE_RESERVE_SECONDS)
        if not delays:
            return

        async def deadline_tick(_now: datetime) -> None:
            # The event loop and remote command still have delivery latency;
            # this removes the avoidable five-second watchdog rounding only.
            self._handle_source_change(None)

        self._unsub_deadline = async_call_later(self.hass, max(0.05, min(delays) - elapsed), deadline_tick)


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
