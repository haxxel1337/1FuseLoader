from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from collections import deque
from copy import deepcopy
from pathlib import Path
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from math import floor, isfinite, log10
from typing import Any
from uuid import uuid4

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
from .easee_adapter import CommandNotSent, EaseeCommandAdapter, NativeTelemetryObservation, PRIVATE_TELEMETRY_OBSERVATION_IDS
from .command_delivery import CommandDeliveryTracker
from .bmw_context import BMWContext
from .logic import (
    ALLOWED_ACTIVE_STATUSES, IDLE_STATUSES, START_ALLOWED_STATUSES,
    PEAK_RESPONSE_TRIGGER_AMPS, ElectricalStopCause, OneFuseRegulator,
    RegulatorInput, RegulatorSettings, SourceHealth, ControlledL1Evidence,
)
from .overload_persistence import OverloadBudgetStore
from .mqtt_freshness import MqttL1Config, MqttL1Source

_LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT_SECONDS = 20
INITIAL_EXPOSURE_LOAD_TIMEOUT_SECONDS = 10
EXPOSURE_STORE_TIMEOUT_SECONDS = 2
EXPOSURE_STORE_RETRY_SECONDS = 15
CURRENT_SNAPSHOT_INTERVAL_SECONDS = 30
CURRENT_SNAPSHOT_MAX_AGE_SECONDS = 30
CURRENT_SNAPSHOT_MIN_INTERVAL_SECONDS = 5
CURRENT_SNAPSHOT_REQUEST_TIMEOUT_SECONDS = 10
CURRENT_SNAPSHOT_MAX_BACKOFF_SECONDS = 300
CURRENT_TUPLE_QUIET_SECONDS = 0.25
CURRENT_PUBLICATION_WATCHDOG_SECONDS = 1.0
_NO_NATIVE_CURRENT_CLOCK = object()
# Match Entity._stringify_state in supported HA Core exactly. This is a
# representation identity, not an electrical tolerance or a sampling clock.
_HA_FLOAT_PRECISION = abs(floor(log10(abs(sys.float_info.epsilon)))) - 1
SNAPSHOT_ERROR_CODES = frozenset({
    "easee_snapshot_api_unavailable", "easee_snapshot_missing_or_invalid",
    "easee_snapshot_invalid_current", "easee_snapshot_invalid_operation_mode",
    "easee_snapshot_failed", "easee_runtime_changed_during_request",
})
SNAPSHOT_FAILURE_TYPES = frozenset({
    "KeyError", "TypeError", "ValueError", "RuntimeError", "TimeoutError", "OSError",
    "ClientError", "ClientResponseError", "ClientConnectionError", "ClientConnectorError",
    "ForbiddenServiceException", "NotFoundException", "TooManyRequestsException",
    "ServerFailureException", "AuthorizationFailedException", "BadRequestException",
    "JSONDecodeError", "ClientPayloadError", "ServerDisconnectedError",
})
_PENDING_BUDGET_WRITES_KEY = f"{DOMAIN}_pending_budget_writes"
# One minute lease plus storage, request and scheduling allowances.
OVERLOAD_LEASE_RESERVE_SECONDS = 90
# Leave room for HA's own entity attributes below Recorder's 16 KiB limit.
DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES = 11 * 1024
# The source clocks are operational evidence, not expendable incident detail.
# Keep their bounded scalar representations intact through both publication passes.
_SOURCE_METADATA_FIELDS = frozenset({
    "grid_source_kind", "grid_reported_at_utc", "grid_mqtt_received_at_utc",
    "charger_current_reported_at_utc", "effective_charger_reported_at_utc",
    "charger_status_reported_at_utc", "easee_stream_pulse_utc",
    "reported_circuit_reported_at_utc", "easee_source_health",
    "meter_envelope_control", "meter_control_health", "meter_estimate_quality",
    "meter_grid_valid", "meter_actuator_valid", "meter_ev_valid",
    "easee_product_data_quiet",
    "meter_state", "meter_zero_cause",
    "meter_control_state", "meter_episode_id", "meter_operational_fault",
    "meter_recovery_stage",
    "meter_episode_phase", "meter_response_deadline_ts", "meter_grid_deadline_ts",
    "meter_recovery_required", "meter_cycle_id", "meter_cycle_used_seconds",
    "meter_cycle_remaining_seconds", "meter_historical_fault", "meter_incident_count",
    "controlled_l1_current", "controlled_l1_observed_current", "controlled_l1_used",
    "controlled_l1_received_at_utc", "controlled_l1_source_at_utc",
    "controlled_l1_output_phase", "controlled_l1_mapping_epoch",
    "controlled_l1_mapping_adopted_at_utc", "controlled_l1_sequence",
    "controlled_l1_error", "controlled_l1_current_error", "controlled_l1_committed",
})


def _finite_current_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if not isinstance(value, bool) and isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _native_current_for_ha_state(state: Any, native_value: Any) -> float | None:
    """Recover only the exact source number represented by HA's state text.

    HA Core limits float states to its machine-epsilon precision. Parsing that
    display string need not round-trip to the source float. Compare its exact
    canonical representation instead of allowing any amp-sized difference.
    Full-precision input remains valid for injected legacy state publishers.
    """
    number, native = _finite_current_number(state), _finite_current_number(native_value)
    if number is None or native is None:
        return None
    if number == native or number == float(f"{native:.{_HA_FLOAT_PRECISION}}"):
        return native
    return None


def _limit_receipt_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Detached local receipt evidence, never remote delivery or physical proof."""
    summary = {key: record[key] for key in (
        "started_at_utc", "completed_at_utc", "command", "target_p1", "target_p2",
        "target_p3", "ttl_seconds", "result", "error",
    ) if key in record}
    delivery = record.get("delivery")
    if isinstance(delivery, dict):
        summary["delivery"] = {key: delivery[key] for key in (
            "sequence", "state", "http_status", "correlation_available", "vendor_accepted",
            "delivery_received", "readback_matches", "readback_after_submission", "superseded",
            "physical_response_verified", "device_ttl_verified",
        ) if key in delivery}
    return summary


def _compact_state_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Bound only HA state views; never mutate regulator or private journal data."""
    truncated = bool(attributes.get("diagnostics_truncated"))
    previous_omitted = attributes.get("diagnostics_omitted_fields_count", 0)
    omitted = (min(previous_omitted, 2**63 - 1)
               if isinstance(previous_omitted, int) and not isinstance(previous_omitted, bool)
               and previous_omitted >= 0 else 0)
    prior_metadata_omissions = attributes.get("source_metadata_omitted_fields", [])
    metadata_omissions = ({key for key in prior_metadata_omissions
                          if isinstance(key, str) and key in _SOURCE_METADATA_FIELDS}
                         if isinstance(prior_metadata_omissions, (list, tuple)) else set())
    source_metadata = {}
    for key in _SOURCE_METADATA_FIELDS & attributes.keys():
        value = attributes[key]
        scalar_value = value is None or isinstance(value, (str, int, float, bool))
        try:
            bounded = (scalar_value and len(json.dumps(value, ensure_ascii=True,
                       allow_nan=False).encode("utf-8")) <= 256)
        except (TypeError, ValueError, OverflowError):
            bounded = False
        # Never shorten a timestamp into a plausible but different clock, and
        # never publish a nested payload in place of primary source evidence.
        source_metadata[key] = value if bounded else None
        if not bounded:
            metadata_omissions.add(key)
            truncated = True

    def publication_flags() -> None:
        view.update(source_metadata)
        if metadata_omissions:
            view["source_metadata_omitted_fields"] = sorted(metadata_omissions)
        view["diagnostics_scope"] = "compact_state_view_full_incident_journal_in_storage"
        view["diagnostics_truncated"] = truncated
        if omitted or "diagnostics_omitted_fields_count" in attributes:
            view["diagnostics_omitted_fields_count"] = omitted

    def compact(value: Any, *, text_bytes: int = 1024, depth: int = 0) -> Any:
        nonlocal truncated
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) > text_bytes:
                truncated = True
                return encoded[:text_bytes].decode("utf-8", errors="ignore") + " [truncated]"
            return value
        if isinstance(value, dict):
            if depth >= 6:
                truncated = True
                return "[truncated nested diagnostic]"
            return {key: compact(item, text_bytes=text_bytes, depth=depth + 1)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            if len(value) > 4:
                truncated = True
            return [compact(item, text_bytes=text_bytes, depth=depth + 1) for item in value[:4]]
        return value

    view = compact({key: value for key, value in attributes.items()
                    if key not in _SOURCE_METADATA_FIELDS
                    and key not in {"source_metadata_omitted_fields", "diagnostics_omitted_fields_count"}})
    policy = view.get("overload_trial_policy")
    if isinstance(policy, dict) and "settings" in policy:
        # The immutable complete settings snapshot belongs to the ledger, not
        # three trial sensors and Source Health on every watchdog evaluation.
        policy.pop("settings")
        truncated = True
    publication_flags()

    def size() -> int:
        # Escaped ASCII is a conservative upper bound for UTF-8 JSON as well.
        return len(json.dumps(view, ensure_ascii=True, default=str).encode("utf-8"))

    if size() > DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES:
        truncated = True
        view = compact(view, text_bytes=384)
        publication_flags()
        # Histories are optional state summaries. The full rings remain in the
        # private incident journal and must never be shortened for Recorder.
        for key in ("recent_command_journal", "recent_stop_contexts", "recent_command_deliveries"):
            if key in view:
                view[key] = []
    if size() > DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES:
        essential = {
            "mode", "reason", "current_p1", "current_p2", "current_p3", "target_p1",
            "source_health", "source_health_reason", "source_errors", "last_stop_context",
            "last_stop_nominal_diagnostics", "command_result", "command_error", "control_block_reason",
            "overload_failure_reason", "overload_historical_failure_reason",
            "overload_lifetime_used_seconds", "overload_trial_state", "overload_trial_used_seconds",
            "overload_trial_remaining_seconds", "overload_trial_failure_reason",
            "overload_trial_policy",
            "grid_confirmation_deadline_ts", "grid_confirmation_remaining_seconds",
            "ordinary_response_deadline_ts",
            "normal_continuity_deadline_ts",
            "readiness_deadline_ts",
            "peak_only_response", "response_policy", "peak_response_trigger_amps",
            "peak_response_active", "peak_response_deadline_ts", "ordinary_lower_offer_pending",
            "automatic_overload_cycles", "automatic_cycle_state", "automatic_cycle_used_seconds",
            "disconnected_readiness",
            "automatic_cycle_remaining_seconds", "automatic_cycle_failure_reason",
            "automatic_qualification_seconds", "automatic_cooling_seconds",
            "budget_storage_error", "budget_storage_error_type", "budget_verification_pending",
            "budget_latest_state_durable", "budget_checkpoint_verified_at_utc",
            "recent_command_journal_count", "recent_stop_contexts_count", "recent_command_deliveries_count",
            "last_command_attempt", "last_accepted_command", "last_accepted_limit",
            "diagnostics_scope", "diagnostics_truncated", "diagnostics_omitted_fields_count",
            "source_metadata_omitted_fields",
        } | _SOURCE_METADATA_FIELDS
        # A future unexpectedly verbose source field must not silently discard
        # the latest cause or accounting counters by crossing Recorder's limit.
        for key in sorted((key for key in view if key not in essential),
                          key=lambda key: len(json.dumps(view[key], default=str)), reverse=True):
            if size() <= DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES:
                break
            del view[key]
            omitted += 1
        publication_flags()
    if size() > DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES:
        # Fixed final projection keeps the limit hard even if a future nested
        # source payload is accidentally attached to an essential field.
        def scalar(value: Any) -> Any:
            value = (value if value is None or isinstance(value, (str, int, float, bool))
                     else "[omitted diagnostic detail]")
            if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) <= 96:
                return value
            text = str(value)
            low, high = 0, min(len(text), 96)
            # Control characters can require six JSON bytes each. Raw UTF-8
            # length alone cannot enforce Recorder's serialized byte limit.
            while low < high:
                middle = (low + high + 1) // 2
                candidate = text[:middle] + " [truncated]"
                if len(json.dumps(candidate, ensure_ascii=True).encode("utf-8")) <= 96:
                    low = middle
                else:
                    high = middle - 1
            return text[:low] + " [truncated]"
        last_stop = view.get("last_stop_context")
        view = {key: (view[key] if key in _SOURCE_METADATA_FIELDS
                      or key == "source_metadata_omitted_fields" else scalar(view[key]))
                for key in essential if key in view}
        if isinstance(last_stop, dict):
            view["last_stop_context"] = {key: scalar(last_stop[key]) for key in (
                "plan_utc", "reason", "mode", "grid_l1", "charger_current", "status",
                "command_result", "command_error", "overload_failure_reason",
            ) if key in last_stop}
        publication_flags()
        if size() > DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES:
            view = {key: view[key] for key in (
                "mode", "reason", "current_p1", "current_p2", "current_p3", "source_health",
                "source_health_reason", "last_stop_context", "overload_lifetime_used_seconds",
                "overload_historical_failure_reason", "overload_trial_state", "overload_trial_used_seconds",
                "overload_trial_remaining_seconds", "recent_command_journal_count", "recent_stop_contexts_count",
                "diagnostics_truncated", "diagnostics_scope", "diagnostics_omitted_fields_count",
                "source_metadata_omitted_fields",
            ) + tuple(_SOURCE_METADATA_FIELDS) if key in view}
    return view


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
        self.last_stop_context: dict[str, Any] | None = None
        self.last_stop_nominal_diagnostics: dict[str, Any] | None = None
        self._recent_commands: deque[dict[str, Any]] = deque(maxlen=32)
        self._delivery = CommandDeliveryTracker()
        self._delivery_unsub = None
        self._current_observation_unsub = None
        self._telemetry_unsub = None
        self._native_telemetry_events: deque[dict[str, Any]] = deque(maxlen=128)
        self._native_diagnostic_generation: int | None = None
        self._native_diagnostics_by_id: dict[int, dict[str, Any]] = {}
        self._transport_events: deque[dict[str, Any]] = deque(maxlen=32)
        self._pilot_zero_observations: deque[dict[str, Any]] = deque(maxlen=8)
        self._last_pilot_output: tuple[int, float] | None = None
        self._passive_journal_unsub = None
        self._passive_journal_token = None
        self._current_quiet_unsub = None
        self._current_quiet_token: object | None = None
        self._current_publication_unsub = None
        self._current_publication_token: object | None = None
        self._current_publication_deadline: float | None = None
        self._current_publication_key: tuple | None = None
        self._control_identity = None
        self._delivery_binding = None
        self._applied_control_binding = None
        self._applied_control_selection = None
        self._positive_retry_after = 0.0
        self._local_retry_after = 0.0
        self._local_retry_unsub = None
        self._local_retry_token = None
        self._dispatch_task: asyncio.Task | None = None
        self._dispatch_input: RegulatorInput | None = None
        self._dispatch_decision: Any = None
        self._dispatch_positive = False
        self._dispatch_started = False
        self._dispatch_superseded = False
        self._protective_zero_pending = False
        self._recent_stop_contexts: deque[dict[str, Any]] = deque(maxlen=8)
        self._incident_journal_pending: dict[str, Any] | None = None
        self._incident_journal_task: asyncio.Task | None = None
        self._incident_journal_error: str | None = None
        self._stop_context_armed = True
        self.source_diagnostics: dict[str, Any] = {}
        self._charger_snapshot: Any = None
        self._charger_snapshot_selection: tuple | None = None
        self._charger_snapshot_task: asyncio.Task | None = None
        self._charger_snapshot_next_request = 0.0
        self._charger_snapshot_last_receipt_monotonic: float | None = None
        self._charger_snapshot_receipt_has_old_event = False
        self._charger_snapshot_last_request_need: tuple | None = None
        self._charger_vendor_evidence: dict[str, Any] | None = None
        self._charger_snapshot_error: str | None = None
        self._charger_snapshot_error_reason: str | None = None
        self._charger_snapshot_failure_type: str | None = None
        self._charger_snapshot_refresh_unsub = None
        self._charger_snapshot_refresh_due: float | None = None
        self._charger_snapshot_refresh_token: object | None = None
        self._charger_snapshot_failures = 0
        self.control_block_reason: str | None = "Easee adapter has not been initialized."
        self.unsub_timer = None
        self.unsub_states = None
        self._source_entities: tuple[str, ...] = ()
        self._l1_source: MqttL1Source | None = None
        self._l1_source_config: MqttL1Config | None = None
        self._l1_source_error: str | None = None
        self.listeners: list[Any] = []
        self._lock = asyncio.Lock()
        self._stopped = False
        self._generation = 0
        self._pending_refresh = False
        self._refresh_task: asyncio.Task | None = None
        self._bmw_context = BMWContext(hass, entry.data.get(CONF_BMW_DEVICE_ID))
        self.bmw_context: dict[str, Any] = {}
        self._adapter = command_adapter
        storage_key = f"{DOMAIN}.{entry.entry_id}.overload"
        writer = Store(hass, 1, storage_key, atomic_writes=True)
        runtime_data = getattr(hass, "data", None)
        journal_states = (runtime_data.setdefault(f"{DOMAIN}_incident_journal_writers", {})
                          if runtime_data is not None else {})
        self._incident_journal_writer = journal_states.setdefault(
            entry.entry_id, {"lock": asyncio.Lock(), "revision": 0})
        rate_states = (runtime_data.setdefault(f"{DOMAIN}_snapshot_rate_limits", {})
                       if runtime_data is not None else {})
        self._charger_snapshot_rate_state = rate_states.setdefault(entry.entry_id, {})
        self._charger_snapshot_next_request = self._charger_snapshot_rate_state.get("next_request", 0.0)
        self._charger_snapshot_failures = self._charger_snapshot_rate_state.get("failures", 0)
        pending_writes = (runtime_data.setdefault(_PENDING_BUDGET_WRITES_KEY, {})
                          if runtime_data is not None else {})

        async def read_budget_checkpoint() -> dict | None:
            # A new read-only public Store cannot return this writer's pending
            # in-memory _data. Completed Store writes invalidate HA's preload
            # cache; exact unique-revision matching rejects deferred writes.
            return await Store(hass, 1, storage_key, read_only=True).async_load()

        async def budget_ledger_exists() -> bool | None:
            path = getattr(writer, "path", None)
            if path is None:
                return None  # Pure test stores need not provide a filesystem.
            def known_ledger() -> bool:
                ledger = Path(path)
                return (os.path.isfile(path)
                        or next(ledger.parent.glob(f"{ledger.name}.corrupt.*"), None) is not None)
            return await hass.async_add_executor_job(known_ledger)

        self._budget_store = OverloadBudgetStore(
            writer, read_back=read_budget_checkpoint, pending_writes=pending_writes,
            pending_key=storage_key, create_task=hass.async_create_task,
            write_allowed=lambda: (not self._stopped and
                getattr(getattr(hass, "state", None), "name", None) != "stopping"),
            ledger_exists=budget_ledger_exists,
        )
        self._budget_loaded = False
        self._normal_control_checkpoint_pending = False
        self._budget_error: str | None = None
        self._budget_error_type: str | None = None
        self._budget_retry_at = 0.0
        self._budget_pending_token: object | None = None
        self._meter_checkpoint_proposal: dict[str, Any] | None = None
        self._overload_restore_receipt_floor: float | None = None
        self._trial_action_result = "not_requested"
        self._trial_action_error: str | None = None
        self._unsub_deadline = None
        self._deadline_token: object | None = None
        if command_adapter is not None:
            self.control_block_reason = command_adapter.control_block_reason

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

    def _electrical_selection(self) -> tuple:
        """Semantic selection remains comparable when registry proof is None."""
        data = self.entry.data or {}
        return (data.get(CONF_DEVICE_ID), data.get(CONF_CONTROLLED_PHASE, "L1"),
            data.get(CONF_L1_ENTITY), data.get(CONF_CHARGER_CURRENT_ENTITY),
            data.get(CONF_STATUS_ENTITY), repr(self._l1_source_config))

    def _reload_settings_locked(self) -> bool:
        settings = _settings_from_entry(self.entry)
        if settings == self.settings:
            return False
        self.regulator.update_settings(settings)
        self._charger_snapshot = None
        self._charger_snapshot_selection = None
        return True

    async def async_set_option(self, key: str, value: Any) -> None:
        # Invalid dependency changes must have no transport side effects.
        # Revalidate under the lock as another valid update may precede us.
        candidate = dict(self.entry.options or {})
        candidate[key] = value
        candidate = validate_options(candidate)
        if (candidate == validate_options(self.entry.options or {})
                and _settings_from_entry(self.entry) == self.settings
                and self._electrical_selection() == self._applied_control_selection
                and self._configured_control_identity() == self._applied_control_binding):
            # No await separates this comparison from return. A repeated UI
            # value grants no reason to cancel a live request or phase.
            return
        self._generation += 1
        self._cancel_positive_dispatch()
        self._cancel_local_retry()
        self._cancel_snapshot_refresh(cancel_request=True)
        async with self._lock:
            options = dict(self.entry.options or {})
            options[key] = value
            options = validate_options(options)
            self.hass.config_entries.async_update_entry(self.entry, options=options)
            self._reload_settings_locked()
            if not self._stopped:
                await self._async_tick_locked()

    async def async_set_switch(self, key: str, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("Switch values must be booleans")
        await self.async_set_option(key, value)

    async def async_reconfigure(self) -> None:
        if (_settings_from_entry(self.entry) == self.settings
                and self._electrical_selection() == self._applied_control_selection
                and self._configured_control_identity() == self._applied_control_binding):
            async with self._lock:
                if (_settings_from_entry(self.entry) == self.settings
                        and self._electrical_selection() == self._applied_control_selection
                        and self._configured_control_identity() == self._applied_control_binding):
                    # Advisory selection/subscriptions can refresh without
                    # interrupting the unchanged electrical control contract.
                    self._bmw_context = BMWContext(self.hass, self.entry.data.get(CONF_BMW_DEVICE_ID))
                    if self.unsub_states is not None:
                        self._subscribe_source_entities()
                    return
        electrical_changed = (self._electrical_selection() != self._applied_control_selection
            or self._configured_control_identity() != self._applied_control_binding)
        self._generation += 1
        self._cancel_positive_dispatch()
        self._cancel_local_retry()
        self._cancel_snapshot_refresh(cancel_request=True)
        async with self._lock:
            changed = self._reload_settings_locked()
            source_changed = await self._async_configure_l1_source_locked()
            if self.unsub_states is not None:
                self._subscribe_source_entities()
            self._applied_control_binding = self._configured_control_identity()
            self._applied_control_selection = self._electrical_selection()
            if (changed or source_changed or electrical_changed) and not self._stopped:
                await self._async_tick_locked()

    async def async_reset_state(self) -> None:
        self._generation += 1
        self._cancel_positive_dispatch()
        self._cancel_local_retry()
        self._cancel_snapshot_refresh(cancel_request=True)
        async with self._lock:
            self.regulator.reset()
            if not self._stopped:
                await self._async_tick_locked()

    def _trial_start_input(self, generation: int) -> RegulatorInput:
        """Read current start prerequisites without requesting any EV action."""
        if self.settings.meter_envelope_control:
            raise ValueError("Manual trials are unavailable in standalone meter control")
        if self._stopped or generation != self._generation:
            raise ValueError("Trial start interrupted by controller reconfiguration")
        if not self._budget_loaded or self._budget_error:
            raise ValueError("Trial start requires a successfully loaded, verified exposure ledger")
        if (self._dispatch_task is not None or self._protective_zero_pending
                or self._delivery.unresolved_correlated_zero
                or self.hass.loop.time() < self._positive_retry_after):
            raise ValueError("Trial start cannot bypass an unresolved command or retry barrier")
        if self._adapter is None or self._adapter.control_block_reason:
            raise ValueError("Trial start requires a verified command adapter")
        if (self._adapter.telemetry_block_reason
                or getattr(self._adapter, "overload_block_reason", "unverified")
                or not getattr(self._adapter, "offline_limits_verified_zero", False)):
            raise ValueError("Trial start requires healthy telemetry and verified zero offline limits")
        return self._read_input()

    async def _save_trial_checkpoint(self) -> None:
        exposure = self.regulator.export_overload_state()
        await asyncio.wait_for(self._budget_store.async_save(exposure), EXPOSURE_STORE_TIMEOUT_SECONDS)
        if not self._budget_store.state_is_durable(self.regulator.export_overload_state()):
            raise RuntimeError("Trial checkpoint is not verified")

    async def async_start_overload_trial(self) -> None:
        """Explicitly arm one period; this method never sends a charger command.

        Both pending and active states are independently verified before the
        in-memory permission is committed. A failed/late transaction cannot
        grant another period after restart: restored periods are interrupted.
        """
        generation = self._generation
        async with self._lock:
            inp = self._trial_start_input(generation)
            trial_id = str(uuid4())
            self.regulator.prepare_overload_trial(
                inp, trial_id=trial_id, started_at_utc=datetime.now(timezone.utc).isoformat())
            self._trial_action_result = "pending_checkpoint"
            self._trial_action_error = None
            try:
                await self._save_trial_checkpoint()
                self.regulator.stage_overload_trial_activation(
                    trial_id, self._trial_start_input(generation))
                self._trial_action_result = "active_checkpoint"
                await self._save_trial_checkpoint()
                self.regulator.commit_overload_trial_activation(
                    trial_id, self._trial_start_input(generation))
                if not self._budget_store.state_is_durable(self.regulator.export_overload_state()):
                    raise RuntimeError("Latest trial activation state is not verified")
            except (Exception, asyncio.CancelledError) as err:
                self.regulator.reject_overload_trial_activation(trial_id, "activation_not_verified")
                self._trial_action_result = "blocked"
                self._trial_action_error = type(err).__name__
                # Keep the terminal period in the next serialized checkpoint,
                # even if an earlier shielded write completes after timeout.
                self._budget_error = "overload_budget_save_failed"
                self._budget_error_type = type(err).__name__
                self._budget_retry_at = 0.0
                self._handle_source_change(None)
                self.notify()
                if isinstance(err, asyncio.CancelledError):
                    raise
                raise ValueError("Trial activation was not verified; no new overload control is permitted") from err
            self._trial_action_result = "active"
            self.notify()

    async def async_end_overload_trial(self) -> None:
        """Withdraw the period and let ordinary protection evaluate next."""
        if self.settings.meter_envelope_control:
            raise ValueError("Manual trials are unavailable in standalone meter control")
        self._generation += 1
        self._cancel_positive_dispatch()
        async with self._lock:
            if self.settings.meter_envelope_control:
                raise ValueError("Manual trials are unavailable in standalone meter control")
            if self._stopped or not self._budget_loaded:
                raise ValueError("Controller is not ready to end a trial")
            self.regulator.end_overload_trial(self._read_input())
            try:
                await self._save_trial_checkpoint()
            except (Exception, asyncio.CancelledError) as err:
                self._trial_action_result = "end_checkpoint_unverified"
                self._trial_action_error = type(err).__name__
                self._budget_error = "overload_budget_save_failed"
                self._budget_error_type = type(err).__name__
                self._budget_retry_at = 0.0
                self._handle_source_change(None)
                self.notify()
                if isinstance(err, asyncio.CancelledError):
                    raise
                raise ValueError("Trial ended locally; its checkpoint is unverified and control is blocked") from err
            self._trial_action_result = "ended"
            self._trial_action_error = None
            self._handle_source_change(None)
            self.notify()

    async def async_start(self) -> None:
        async with self._lock:
            self._stopped = False
            self._reload_settings_locked()
            if not self._budget_loaded:
                restore_rejected = False
                try:
                    saved = await asyncio.wait_for(self._budget_store.async_load(), INITIAL_EXPOSURE_LOAD_TIMEOUT_SECONDS)
                    if saved is not None:
                        self._normal_control_checkpoint_pending = bool(
                            not self.settings.meter_envelope_control and saved.get("normal_control"))
                        if not self.regulator.import_overload_state(saved):
                            restore_rejected = True
                            raise ValueError("Invalid restored exposure")
                        if not self.settings.meter_envelope_control and saved.get("minimum_held"):
                            self._overload_restore_receipt_floor = datetime.now(timezone.utc).timestamp()
                except Exception as err:
                    self._budget_error = "overload_budget_restore_failed"
                    self._budget_error_type = type(err).__name__
                    # An unknown older ledger must never be overwritten by a
                    # newly initialized zero allowance after a failed load.
                    # A rejected additive recovery checkpoint may already
                    # have preserved valid older exposure and failure fields.
                    # Do not replace those with a new synthetic allowance.
                    if not restore_rejected:
                        self.regulator.import_overload_state(None)
                    _LOGGER.warning("1FuseLoader exposure restore failed (%s)",
                                    self._budget_error_type, exc_info=True)
                self._budget_loaded = True
            if self._stopped:
                return
            self._subscribe_source_entities()
            await self._async_configure_l1_source_locked()
            if self._stopped:
                return
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
            if self._stopped:
                return
            subscribe = getattr(self._adapter, "set_delivery_callback", None)
            if callable(subscribe):
                if self._delivery_unsub is not None:
                    self._delivery_unsub()
                self._delivery_unsub = subscribe(self._handle_delivery)
            subscribe_current = getattr(self._adapter, "set_current_observation_callback", None)
            if callable(subscribe_current):
                if self._current_observation_unsub is not None:
                    self._current_observation_unsub()
                self._current_observation_unsub = subscribe_current(lambda: self._handle_source_change(None))
            subscribe_telemetry = getattr(self._adapter, "set_telemetry_callback", None)
            if callable(subscribe_telemetry):
                if self._telemetry_unsub is not None:
                    self._telemetry_unsub()
                self._telemetry_unsub = subscribe_telemetry(self._handle_passive_telemetry)
            self._applied_control_binding = self._configured_control_identity()
            self._applied_control_selection = self._electrical_selection()
            await self._async_tick_locked()
            # async_stop marks stopped before waiting for this lock. It may
            # already have removed listeners while any startup await yielded;
            # never install a new watchdog after that cleanup has run.
            if self._stopped:
                return
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

    def _subscribe_source_entities(self) -> None:
        source_entities = tuple(self.entry.data[key] for key in (
            CONF_L1_ENTITY, CONF_L2_ENTITY, CONF_L3_ENTITY,
            CONF_STATUS_ENTITY, CONF_CHARGER_CURRENT_ENTITY,
        ) if self.entry.data.get(key))
        if self.unsub_states is not None and source_entities == self._source_entities:
            return
        if self.unsub_states is not None:
            self.unsub_states()
        self._source_entities = source_entities
        self.unsub_states = async_track_state_change_event(
            self.hass, source_entities, self._handle_source_change,
        )

    async def _async_configure_l1_source_locked(self) -> bool:
        """Resolve only the selected source using public entity/device metadata."""
        if self._stopped:
            return False
        config = None
        try:
            registry = er.async_get(self.hass)
            entity_id = self.entry.data.get(CONF_L1_ENTITY)
            entity = registry.async_get(entity_id) if entity_id else None
            if entity is not None and entity.platform == "mqtt":
                from homeassistant.helpers import device_registry as dr
                device = dr.async_get(self.hass).async_get(entity.device_id)
                config = MqttL1Config.from_registry(entity_id, entity, device)
        except Exception:
            # Never keep an already enabled receipt source after its binding
            # becomes unverifiable. Unknown sources retain the original policy.
            if self._l1_source is not None:
                self._l1_source.stop()
                self._l1_source = None
                self._l1_source_config = None
                self._l1_source_error = "L1 MQTT source identity could not be verified."
                return True
            return False
        if config == self._l1_source_config and self._l1_source_error is None:
            return False
        if self._l1_source is not None:
            self._l1_source.stop()
        self._l1_source = None
        self._l1_source_config = config
        self._l1_source_error = None
        if config is None:
            return True
        source = MqttL1Source(
            config, monotonic=self.hass.loop.time,
            utc_now=lambda: datetime.now(timezone.utc),
            age_limit=lambda: self.settings.max_sensor_age_seconds,
            on_change=lambda: self._handle_source_change(None),
        )
        self._l1_source = source
        try:
            await source.async_start(self.hass)
        except Exception as err:
            self._l1_source = None
            self._l1_source_error = f"L1 MQTT receipt subscription failed ({type(err).__name__})."
        if self._stopped:
            source.stop()
            self._l1_source = None
        return True

    @callback
    def _handle_delivery(self, event: Any) -> None:
        if self._stopped:
            return
        if self._delivery.delivery(event, self.hass.loop.time()):
            if self._delivery.latest is not None and self._delivery.latest.vendor_accepted is False:
                self._protective_zero_pending = True
            self._handle_source_change(None)
            self.notify()

    @callback
    def _handle_passive_telemetry(self, observations: Any) -> None:
        """Journal selected events without waking/evaluating the regulator."""
        if self._stopped or not isinstance(observations, tuple):
            return
        urgent = False
        recorded = []
        for event in observations[:64]:
            if (not isinstance(event, NativeTelemetryObservation)
                    or type(event.observation_id) is not int
                    or event.observation_id not in PRIVATE_TELEMETRY_OBSERVATION_IDS
                    or type(event.receiver_generation) is not int or event.receiver_generation < 1):
                continue
            item = event.public()
            self._native_telemetry_events.append(item)
            relation = self._retain_passive_diagnostic(event, item)
            recorded.append((event, item, relation))
        # Freeze end-of-batch context so a rare reason/limit occurring after
        # output114 in the same payload is retained too. This is not a claim
        # that those values were measured simultaneously or caused the zero.
        context = self._passive_diagnostic_context()
        for event, item, relation in recorded:
            if event.receiver_generation != self._native_diagnostic_generation:
                continue
            if event.observation_id != 114 or event.error is not None or type(event.value) not in (int, float):
                continue
            previous = (self._last_pilot_output[1] if self._last_pilot_output is not None
                and self._last_pilot_output[0] == event.receiver_generation else None)
            self._last_pilot_output = (event.receiver_generation, float(event.value))
            if event.value == 0 and previous != 0:
                self._pilot_zero_observations.append({**item,
                    "previous_output_amps": previous,
                    "last_accepted_limit": _limit_receipt_summary(self.last_accepted_limit)
                        if self.last_accepted_limit else None,
                    "last_attempt": _limit_receipt_summary(self.last_command_attempt)
                        if self.last_command_attempt else None,
                    "last_mode": self.last_decision.mode if self.last_decision else None,
                    "source_relation": relation,
                    "diagnostic_context": deepcopy(context),
                    "scope": "pilot_zero_observed_not_measured_EV_current_or_causal_command_proof"})
                urgent = True
        if not recorded:
            return
        if urgent and self._passive_journal_unsub is None:
            self._queue_incident_journal()
            # Normal telemetry stays in memory. One finite post-incident
            # flush captures its immediate aftermath (including more pilot
            # toggles) without extending the window or periodic I/O.
            token = self._passive_journal_token = object()
            generation = self._generation

            async def flush(_now: datetime) -> None:
                if token is not self._passive_journal_token:
                    return
                self._passive_journal_unsub = self._passive_journal_token = None
                if self._stopped or generation != self._generation:
                    return
                self._queue_incident_journal()

            self._passive_journal_unsub = async_call_later(self.hass, 5., flush)

    def _retain_passive_diagnostic(self, event: NativeTelemetryObservation, item: dict[str, Any]) -> str:
        """Bounded private provenance only; never supply controller evidence."""
        generation = event.receiver_generation
        if self._native_diagnostic_generation is None or generation > self._native_diagnostic_generation:
            self._native_diagnostics_by_id.clear()
            self._native_diagnostic_generation = generation
        elif generation < self._native_diagnostic_generation:
            return "retired_receiver_generation"
        previous = self._native_diagnostics_by_id.get(event.observation_id, {})
        highwater = previous.get("provider_highwater")
        source = self._utc_time(event.source_at_utc)
        received = self._utc_time(event.received_at_utc)
        previous_source = self._utc_time(highwater.get("source_at_utc")) if highwater else None
        advanced = False
        if item.get("origin") != "signalr_product_update_callback":
            relation = "unknown_event_origin"
        elif event.error is not None:
            relation = "invalid_observation"
        elif source is None:
            relation = "missing_provider_timestamp"
        elif previous_source is None or source > previous_source:
            relation = "first_provider_timestamp" if previous_source is None else "advancing_provider_timestamp"
            highwater = dict(item)
            advanced = True
        elif source < previous_source:
            relation = "regressed_provider_timestamp"
        else:
            relation = ("duplicate_provider_timestamp" if item["value"] == highwater["value"]
                else "same_provider_timestamp_changed_value")
        self._native_diagnostics_by_id[event.observation_id] = {
            "last_received": dict(item), "provider_highwater": highwater,
            "source_relation": relation, "highwater_receipt_advanced": advanced,
            "provider_age_at_receipt_seconds": (received-source).total_seconds()
                if received is not None and source is not None else None,
            "control_evidence": False,
        }
        return relation

    def _passive_diagnostic_context(self) -> dict[str, Any]:
        return {"receiver_generation": self._native_diagnostic_generation,
            "observations": {str(key): deepcopy(value) for key, value in sorted(self._native_diagnostics_by_id.items())},
            "scope": "latest_per_id_at_received_batch_end_not_synchronized_or_control_proof"}

    def _cancel_passive_journal_flush(self) -> None:
        if self._passive_journal_unsub is not None:
            self._passive_journal_unsub()
        self._passive_journal_unsub = self._passive_journal_token = None

    def _cancel_positive_dispatch(self) -> None:
        self._meter_checkpoint_proposal = None
        if self._dispatch_positive and self._dispatch_task is not None and not self._dispatch_task.done():
            self._dispatch_superseded = True
            # Cancellation before the adapter's actual SDK boundary cannot
            # leave a remote command outstanding. After entry it is unknown.
            if self._dispatch_started:
                self._protective_zero_pending = True
            self._dispatch_task.cancel()

    def _defer_local_submission(self, seconds: float) -> None:
        """One bounded retry for a proven local refusal; zero remains urgent."""
        delay = min(3600., max(1., float(seconds)))
        due = self.hass.loop.time() + delay
        if self._local_retry_unsub is not None:
            self._local_retry_unsub()
        self._local_retry_after = due
        token = self._local_retry_token = object()
        generation = self._generation

        async def retry(_now: datetime) -> None:
            if (self._stopped or generation != self._generation
                    or token is not self._local_retry_token):
                return
            self._local_retry_unsub = self._local_retry_token = None
            self._local_retry_after = 0.
            self._handle_source_change(None)

        self._local_retry_unsub = async_call_later(self.hass, delay, retry)

    def _cancel_local_retry(self) -> None:
        if self._local_retry_unsub is not None:
            self._local_retry_unsub()
        self._local_retry_unsub = self._local_retry_token = None
        self._local_retry_after = 0.

    @callback
    def _handle_source_change(self, _event: Any) -> None:
        """One coalesced refresh follows the newest local/Easee telemetry."""
        if self._stopped:
            return
        if self._dispatch_positive and self._dispatch_input is not None:
            latest = self._read_input()
            try:
                grid = float(latest.grid_l1)
                planned_grid = float(self._dispatch_input.grid_l1)
                ceiling = (self.settings.overload_transition_limit_amps if self.settings.overload_enabled
                           else self.settings.main_fuse_amps)
                rising_load = grid > ceiling and grid > planned_grid
            except (TypeError, ValueError, OverflowError):
                rising_load = True
            readiness_dispatch = (self._dispatch_decision is not None
                and self._dispatch_decision.mode.removeprefix("dry_run_") == "disconnected_ready")
            if self.settings.meter_envelope_control and self._dispatch_decision is not None:
                rejected = not self.regulator.can_continue_meter_dispatch(
                    self._dispatch_input, latest, self._dispatch_decision,
                    dispatched=self._dispatch_started)
            elif readiness_dispatch:
                # Only the exact normal-budget readiness proposal may finish
                # across a benign unplugged/connected status transition. The
                # regulator validates its source, fixed age and protective
                # barriers; a disconnected status alone grants no exception.
                rejected = not self.regulator.can_continue_readiness_dispatch(
                    self._dispatch_input, latest, self._dispatch_decision)
            else:
                rejected = (not latest.data_valid or latest.status.strip().lower() not in START_ALLOWED_STATUSES
                    or latest.control_source_id != self._dispatch_input.control_source_id or rising_load)
            if rejected:
                self._cancel_positive_dispatch()
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
        self._budget_pending_token = None
        self._generation += 1
        self._cancel_positive_dispatch()
        if self._delivery_unsub is not None:
            self._delivery_unsub()
            self._delivery_unsub = None
        if self._current_observation_unsub is not None:
            self._current_observation_unsub()
            self._current_observation_unsub = None
        if self._telemetry_unsub is not None:
            self._telemetry_unsub()
            self._telemetry_unsub = None
        self._cancel_passive_journal_flush()
        self._cancel_local_retry()
        self._current_quiet_token = None
        if self._current_quiet_unsub is not None:
            self._current_quiet_unsub()
            self._current_quiet_unsub = None
        self._cancel_current_publication_watchdog()
        self._pending_refresh = False
        snapshot_task = self._charger_snapshot_task
        self._cancel_snapshot_refresh(cancel_request=True)
        self._charger_snapshot = None
        self._charger_snapshot_selection = None
        if self._l1_source is not None:
            self._l1_source.stop()
            self._l1_source = None
        self._l1_source_config = None
        self._l1_source_error = None
        if self.unsub_states:
            self.unsub_states()
            self.unsub_states = None
        self._source_entities = ()
        if self.unsub_timer:
            self.unsub_timer()
            self.unsub_timer = None
        if self._unsub_deadline:
            self._unsub_deadline()
            self._unsub_deadline = None
        async with self._lock:
            pass
        close = getattr(self._adapter, "async_close", None)
        if callable(close):
            await close()
        try:
            await asyncio.wait_for(self._budget_store.async_wait_pending(), EXPOSURE_STORE_TIMEOUT_SECONDS)
        except Exception:
            # The per-entry hass.data gate retains the shielded transaction.
            # A new controller must await it before reading or writing ledger.
            _LOGGER.warning("1FuseLoader exposure transaction remains pending or failed during unload")
        pending = self._refresh_task
        if pending is not None and pending is not asyncio.current_task():
            await pending
        if snapshot_task is not None and snapshot_task is not asyncio.current_task():
            try:
                await snapshot_task
            except asyncio.CancelledError:
                pass

    def _charger_source_revision(self) -> tuple:
        """Identify actual selected value/observation changes, not HA fanout."""
        data = self.entry.data
        result: list[Any] = [data.get(CONF_DEVICE_ID)]
        native = self._native_current_clock()
        if native is not _NO_NATIVE_CURRENT_CLOCK:
            status_id = data.get(CONF_STATUS_ENTITY)
            status = self.hass.states.get(status_id)
            changed = self._utc_time(getattr(status, "last_changed", None))
            current_id = data.get(CONF_CHARGER_CURRENT_ENTITY)
            current = self.hass.states.get(current_id)
            receipt = self._utc_time(getattr(native, "received_at", None))
            current_value = getattr(current, "state", None)
            try:
                clock_owner = getattr(self._adapter, "observation_clock_identity", None)
            except Exception:
                clock_owner = None
            if clock_owner is not None and getattr(native, "source_identity", None) == clock_owner:
                source_number = (_finite_current_number(getattr(native, "current", None))
                    if getattr(native, "committed", False) is True and _finite_current_number(current_value) is not None
                    else _native_current_for_ha_state(current_value, getattr(native, "current", None)))
                if source_number is not None:
                    current_value = source_number
            return (data.get(CONF_DEVICE_ID), status_id, getattr(status, "state", None),
                    changed.timestamp() if changed else None, current_id, current_value,
                    receipt.timestamp() if receipt else None,
                    ("native_terminal_receipt", getattr(native, "source_identity", None),
                     getattr(native, "observation_ids", ()), getattr(native, "error", None),
                     getattr(native, "observation_sequences", ()),
                     getattr(native, "observation_evidence_credit", ()),
                     getattr(native, "native_publication_pending", False),
                     getattr(native, "publication_generation", 0),
                     getattr(native, "divergence_started_monotonic", None)))
        for key in (CONF_STATUS_ENTITY, CONF_CHARGER_CURRENT_ENTITY):
            entity_id = data.get(key)
            state = self.hass.states.get(entity_id)
            result.extend((entity_id, getattr(state, "state", None), self._report_timestamp(state)))
        return tuple(result)

    def _native_current_clock(self) -> Any:
        try:
            return getattr(self._adapter, "native_current_observation", _NO_NATIVE_CURRENT_CLOCK)
        except Exception:
            # Real adapters with unavailable evidence must never silently fall
            # back to the aggregate HA receipt clock.
            return None

    def _schedule_current_quiet_tick(self, changed_at: float | None) -> bool:
        """Defer only new positive authority; do not alter source/deadline time."""
        now = self.hass.loop.time()
        pending = (type(changed_at) in (int, float) and isfinite(changed_at)
                   and now - changed_at < CURRENT_TUPLE_QUIET_SECONDS)
        if not pending:
            return True
        self._current_quiet_token = token = object()
        if self._current_quiet_unsub is not None:
            self._current_quiet_unsub()
        generation = self._generation

        async def ready(_now: datetime) -> None:
            if self._stopped or generation != self._generation or token is not self._current_quiet_token:
                return
            self._current_quiet_unsub = None
            self._current_quiet_token = None
            self._handle_source_change(None)

        self._current_quiet_unsub = async_call_later(
            self.hass, max(0.001, CURRENT_TUPLE_QUIET_SECONDS - (now - changed_at)), ready)
        return False

    def _cancel_current_publication_watchdog(self) -> None:
        if self._current_publication_unsub is not None:
            self._current_publication_unsub()
        self._current_publication_unsub = None
        self._current_publication_token = None
        self._current_publication_deadline = None
        self._current_publication_key = None

    def _current_publication_state(self, native: Any) -> tuple[bool, str | None]:
        """One finite coherence watchdog, independent of electrical deadlines."""
        if getattr(native, "native_publication_pending", False) is not True:
            self._cancel_current_publication_watchdog()
            return False, None
        now = self.hass.loop.time()
        started = getattr(native, "divergence_started_monotonic", None)
        episode = getattr(native, "publication_generation", 0)
        window = _finite_current_number(self.settings.measurement_settle_seconds)
        if (type(started) not in (int, float) or not isfinite(started) or started > now
                or type(episode) is not int or episode < 0 or window is None or window <= 0):
            return True, "native_publication_clock_invalid"
        key = (getattr(native, "source_identity", None), episode)
        if key != self._current_publication_key:
            # A generation changes only after an adapter-proven complete
            # coherent raw commit ends the previous publication episode.
            self._cancel_current_publication_watchdog()
            self._current_publication_key = key
        deadline = started + min(CURRENT_PUBLICATION_WATCHDOG_SECONDS, window)
        if self._current_publication_deadline is not None:
            deadline = min(deadline, self._current_publication_deadline)
        if now >= deadline:
            if self._current_publication_unsub is not None:
                self._current_publication_unsub()
            self._current_publication_unsub = None
            self._current_publication_token = None
            self._current_publication_deadline = deadline
            return True, "native_publication_diverged"
        if self._current_publication_unsub is None or self._current_publication_deadline != deadline:
            if self._current_publication_unsub is not None:
                self._current_publication_unsub()
            self._current_publication_deadline = deadline
            self._current_publication_token = token = object()
            generation = self._generation

            async def expired(_now: datetime) -> None:
                if self._stopped or generation != self._generation or token is not self._current_publication_token:
                    return
                self._current_publication_unsub = None
                self._current_publication_token = None
                self._handle_source_change(None)

            self._current_publication_unsub = async_call_later(self.hass, deadline-now, expired)
        return True, None

    def _cancel_snapshot_refresh(self, *, cancel_request: bool = False) -> None:
        if self._charger_snapshot_refresh_unsub is not None:
            self._charger_snapshot_refresh_unsub()
        self._charger_snapshot_refresh_unsub = None
        self._charger_snapshot_refresh_due = None
        self._charger_snapshot_refresh_token = None
        if cancel_request and self._charger_snapshot_task is not None:
            self._charger_snapshot_task.cancel()

    def _snapshot_freshness_window(self) -> float:
        return max(0.0, min(float(self.settings.measurement_settle_seconds),
                            float(self.settings.max_sensor_age_seconds), CURRENT_SNAPSHOT_MAX_AGE_SECONDS))

    def _snapshot_prefetch_allowed(self, inp: RegulatorInput) -> bool:
        if inp.charger_publication_pending:
            return False  # A GET cannot complete an in-flight raw publication.
        try:
            charging = inp.status == "charging" and float(inp.charger_current) >= 1
        except (TypeError, ValueError):
            charging = False
        return bool(not self._stopped and self.settings.active_control and not self.settings.dry_run
                    and self.settings.enable_dynamic_limit and inp.data_valid and charging
                    and self._snapshot_freshness_window() > 0 and self._adapter is not None
                    and not self._adapter.telemetry_block_reason
                    and callable(getattr(self._adapter, "async_read_current_snapshot", None)))

    def _schedule_current_snapshot(self, inp: RegulatorInput) -> None:
        """Prefetch before numeric expiry, independent of the watchdog period.

        The GET receipt is real input evidence, never a physical sample. A
        five-second start floor shares quota with demand reads; very short
        alignment windows or a slow API can still fail closed normally.
        """
        if not self._snapshot_prefetch_allowed(inp):
            self._cancel_snapshot_refresh(cancel_request=not inp.data_valid or bool(
                self._adapter is not None and self._adapter.telemetry_block_reason))
            return
        in_flight = self._charger_snapshot_task is not None and not self._charger_snapshot_task.done()
        if in_flight and not self._charger_snapshot_task.cancelling():
            self._cancel_snapshot_refresh()
            return
        now = self.hass.loop.time()
        wall = inp.wall_now_ts
        report = inp.charger_report_ts
        age = (wall - report if wall is not None and report is not None
               and isfinite(wall) and isfinite(report) else float("inf"))
        target_age = self._snapshot_freshness_window() / 2
        wait = max(0.0, target_age - age) if age >= -5 else 0.0
        due = max(now + wait, self._charger_snapshot_next_request,
                  (self._charger_snapshot_last_receipt_monotonic + target_age
                   if self._charger_snapshot_last_receipt_monotonic is not None else now),
                  now + CURRENT_SNAPSHOT_MIN_INTERVAL_SECONDS if in_flight else now)
        if (self._charger_snapshot_refresh_due is not None
                and abs(self._charger_snapshot_refresh_due - due) < .05):
            return
        self._cancel_snapshot_refresh()
        token = object()
        generation = self._generation
        self._charger_snapshot_refresh_token = token
        self._charger_snapshot_refresh_due = due

        async def refresh_due(_now: datetime) -> None:
            if (self._stopped or generation != self._generation
                    or token is not self._charger_snapshot_refresh_token):
                return
            self._charger_snapshot_refresh_unsub = None
            self._charger_snapshot_refresh_due = None
            self._charger_snapshot_refresh_token = None
            latest = self._read_input()
            if not self._snapshot_prefetch_allowed(latest):
                self._schedule_current_snapshot(latest)
                return
            age_now = (latest.wall_now_ts - latest.charger_report_ts
                       if latest.wall_now_ts is not None and latest.charger_report_ts is not None
                       else float("inf"))
            if 0 <= age_now < self._snapshot_freshness_window() / 2:
                self._schedule_current_snapshot(latest)
                return
            self._request_current_snapshot()
            self._schedule_current_snapshot(latest)

        self._charger_snapshot_refresh_unsub = async_call_later(self.hass, max(.001, due - now), refresh_due)

    def _snapshot_backoff(self) -> None:
        self._charger_snapshot_failures = min(self._charger_snapshot_failures + 1, 5)
        delay = min(CURRENT_SNAPSHOT_MAX_BACKOFF_SECONDS,
                    CURRENT_SNAPSHOT_INTERVAL_SECONDS * 2 ** (self._charger_snapshot_failures - 1))
        self._charger_snapshot_next_request = max(self._charger_snapshot_next_request,
                                                   self.hass.loop.time() + delay)
        self._charger_snapshot_rate_state.update(next_request=self._charger_snapshot_next_request,
                                                  failures=self._charger_snapshot_failures)

    def _request_current_snapshot(self) -> None:
        """One bounded read shared by demand and deadline-driven prefetch."""
        read_snapshot = getattr(self._adapter, "async_read_current_snapshot", None)
        snapshot_is_current = getattr(self._adapter, "snapshot_is_current", None)
        if (self._stopped or not callable(read_snapshot) or not callable(snapshot_is_current)
                or self.hass.loop.time() < self._charger_snapshot_next_request
                or (self._charger_snapshot_task is not None and not self._charger_snapshot_task.done())):
            return
        if self._adapter.telemetry_block_reason:
            self._charger_snapshot = None
            return
        need = (self._generation, id(self._adapter), self.entry.data.get(CONF_CHARGER_CURRENT_ENTITY),
                _get_state_value(self.hass, self.entry.data.get(CONF_CHARGER_CURRENT_ENTITY)),
                _get_state_value(self.hass, self.entry.data.get(CONF_STATUS_ENTITY)), self.regulator.last_target)
        if (need == self._charger_snapshot_last_request_need
                and self._charger_snapshot_receipt_has_old_event
                and self._charger_snapshot_last_receipt_monotonic is not None
                and self.hass.loop.time() < self._charger_snapshot_last_receipt_monotonic
                    + self._snapshot_freshness_window() / 2):
            return
        generation = self._generation
        revision = self._charger_source_revision()
        adapter = self._adapter
        self._cancel_snapshot_refresh()
        self._charger_snapshot_next_request = self.hass.loop.time() + CURRENT_SNAPSHOT_MIN_INTERVAL_SECONDS
        self._charger_snapshot_last_request_need = need
        self._charger_snapshot_rate_state.update(next_request=self._charger_snapshot_next_request)

        async def fetch() -> None:
            try:
                # Adapter uses a bounded GET. A slow network cannot hold the
                # regulator lock and delay a meter-triggered reduction.
                async with asyncio.timeout(CURRENT_SNAPSHOT_REQUEST_TIMEOUT_SECONDS):
                    snapshot = await read_snapshot()
                async with self._lock:
                    if (self._stopped or generation != self._generation or adapter is not self._adapter
                            or revision != self._charger_source_revision()
                            or self._adapter.telemetry_block_reason or not snapshot_is_current(snapshot)):
                        return
                    received = self._utc_time(getattr(snapshot, "received_at", None))
                    age = ((datetime.now(timezone.utc) - received).total_seconds()
                           if received is not None else float("inf"))
                    if not 0 <= age <= CURRENT_SNAPSHOT_MAX_AGE_SECONDS:
                        self._charger_snapshot_error = "snapshot_receipt_invalid"
                        self._charger_snapshot_error_reason = "snapshot_receipt_invalid"
                        self._charger_snapshot_failure_type = None
                        self._charger_snapshot = None
                        self._snapshot_backoff()
                        return
                    self._charger_snapshot_last_receipt_monotonic = self.hass.loop.time() - age
                    observed = self._utc_time(getattr(snapshot, "current_source_timestamp", None))
                    self._charger_snapshot_receipt_has_old_event = bool(
                        observed is None or getattr(snapshot, "source_timestamp_error", None)
                        or (received - observed).total_seconds() >= self._snapshot_freshness_window() / 2
                        or observed > received + timedelta(seconds=2))
                    report_times = [revision[3], revision[6]]
                    if any((stamp is None and len(revision) == 7) or
                           (stamp is not None and stamp >= received.timestamp()) for stamp in report_times):
                        self._charger_snapshot_error = "snapshot_not_newer_than_entity_reports"
                        self._charger_snapshot_error_reason = "snapshot_not_newer_than_entity_reports"
                        self._charger_snapshot_failure_type = None
                        self._charger_snapshot = None
                        self._snapshot_backoff()
                        return
                    self._charger_snapshot = snapshot
                    self._charger_snapshot_selection = revision
                    self._charger_snapshot_error = None
                    self._charger_snapshot_error_reason = None
                    self._charger_snapshot_failure_type = None
                    self._charger_snapshot_failures = 0
                    self._charger_snapshot_rate_state["failures"] = 0
                # Always evaluate the newest grid after completion; the GET
                # result is never a precomputed command or direct submission.
                self._handle_source_change(None)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if (self._stopped or generation != self._generation or adapter is not self._adapter
                        or revision != self._charger_source_revision()):
                    return
                self._charger_snapshot = None
                self._charger_snapshot_error = type(err).__name__
                # Only our fixed vocabulary may enter diagnostics/logs. Never
                # expose arbitrary exception text, response bodies or URLs.
                parts = str(err).split(":")
                candidate = parts[0]
                self._charger_snapshot_error_reason = candidate if candidate in SNAPSHOT_ERROR_CODES else None
                self._charger_snapshot_failure_type = (
                    parts[1] if candidate == "easee_snapshot_failed" and len(parts) == 2
                    and parts[1] in SNAPSHOT_FAILURE_TYPES else None
                )
                self._snapshot_backoff()
                _LOGGER.warning("1FuseLoader current snapshot failed (%s; %s; %s)", type(err).__name__,
                                self._charger_snapshot_error_reason or "unclassified",
                                self._charger_snapshot_failure_type or "unspecified")
            finally:
                if self._charger_snapshot_task is asyncio.current_task():
                    self._charger_snapshot_task = None
                if (not self._stopped and generation == self._generation and adapter is self._adapter):
                    self._schedule_current_snapshot(self._read_input())

        self._charger_snapshot_task = self.hass.async_create_task(fetch())

    def _read_input(self) -> RegulatorInput:
        data = self.entry.data
        errors: list[str] = []
        # Keep the legacy error contract unchanged. The separate opt-in policy
        # receives source classes from the checks which generated each error;
        # it never guesses electrical authority by matching diagnostic text.
        grid_errors: list[str] = []
        actuator_errors: list[str] = []
        ev_errors: list[str] = []
        grid_state = self.hass.states.get(data.get(CONF_L1_ENTITY))
        status_state = self.hass.states.get(data.get(CONF_STATUS_ENTITY))
        current_state = self.hass.states.get(data.get(CONF_CHARGER_CURRENT_ENTITY))
        grid_value = grid_state.state if grid_state is not None else None
        grid_report_ts = self._report_timestamp(grid_state)
        mqtt_snapshot = None
        if self._l1_source is not None:
            mqtt_snapshot = self._l1_source.snapshot()
            grid_value = mqtt_snapshot.current
            grid_report_ts = mqtt_snapshot.report_ts
            if mqtt_snapshot.error:
                errors.append(mqtt_snapshot.error)
                grid_errors.append(mqtt_snapshot.error)
        elif self._l1_source_error:
            grid_value = None
            grid_report_ts = None
            errors.append(self._l1_source_error)
            grid_errors.append(self._l1_source_error)
        for label, state, requires_amps in (
            ("L1", grid_state, True), ("Charger status", status_state, False),
            ("Charger current", current_state, True),
        ):
            classified = grid_errors if label == "L1" else actuator_errors if label == "Charger status" else ev_errors
            if state is None:
                errors.append(f"{label} telemetry is missing.")
                classified.append(f"{label} telemetry is missing.")
                continue
            attributes = getattr(state, "attributes", {})
            if attributes.get("restored"):
                errors.append(f"{label} telemetry is restored and has not been verified live.")
                classified.append(f"{label} telemetry is restored and has not been verified live.")
            if requires_amps and attributes.get("unit_of_measurement") != "A":
                errors.append(f"{label} telemetry must have unit A.")
                classified.append(f"{label} telemetry must have unit A.")
        if grid_state is not None and self._l1_source is None and not self._l1_source_error:
            reported = getattr(grid_state, "last_reported", None) or getattr(grid_state, "last_updated", None)
            if not isinstance(reported, datetime) or reported.tzinfo is None:
                errors.append("L1 telemetry has no trustworthy report timestamp.")
                grid_errors.append("L1 telemetry has no trustworthy report timestamp.")
            else:
                age = (datetime.now(timezone.utc) - reported).total_seconds()
                if age < -5 or age > self.settings.max_sensor_age_seconds:
                    errors.append("L1 telemetry is stale or has an invalid timestamp.")
                    grid_errors.append("L1 telemetry is stale or has an invalid timestamp.")
        # Easee states are event-driven: a stable value may be hours old. Its
        # adapter verifies source heartbeat/online health instead of entity age.
        health_error = "easee_source_unverified"
        product_data_quiet = False
        if self._adapter is None:
            errors.append("Easee source health has not been verified.")
            actuator_errors.append("Easee source health has not been verified.")
        else:
            product_data_quiet = getattr(self._adapter,"product_data_quiet",False) is True
            health_error = (getattr(self._adapter,"transport_block_reason","easee_source_unverified")
                if self.settings.meter_envelope_control and product_data_quiet else self._adapter.telemetry_block_reason)
            if health_error:
                errors.append(str(health_error))
                actuator_errors.append(str(health_error))
        self._read_source_diagnostics(
            grid_state, current_state, status_state, health_error, errors,
            grid_value=grid_value, grid_report_ts=grid_report_ts,
        )
        if self.settings.meter_envelope_control and product_data_quiet:
            quiet_reason="ProductUpdate data is quiet; only independently whole-meter-bounded normal retention is eligible. Cached EV data is not fresh."
            prior_reason=(self.source_diagnostics.get("source_health_reason","")+" "
                if self.source_diagnostics.get("source_health")!="healthy" else "")
            self.source_diagnostics.update(
                source_health="degraded" if self.source_diagnostics.get("source_health")=="healthy" else "unhealthy",
                easee_source_health="easee_product_data_quiet",
                source_health_reason=prior_reason+quiet_reason)
        self.source_diagnostics.update({
            "grid_source_kind": "currently_one_validated_mqtt" if self._l1_source_config or self._l1_source_error else "ha_entity_report",
            "grid_state_reported_at_utc": (self._utc_time(getattr(grid_state, "last_reported", None)).isoformat()
                                           if self._utc_time(getattr(grid_state, "last_reported", None)) else None),
            "grid_mqtt_received_at_utc": (datetime.fromtimestamp(mqtt_snapshot.receipt_ts, timezone.utc).isoformat()
                                          if mqtt_snapshot and mqtt_snapshot.receipt_ts is not None else None),
            "grid_mqtt_sample_at_utc": (datetime.fromtimestamp(mqtt_snapshot.sample_ts, timezone.utc).isoformat()
                                        if mqtt_snapshot and mqtt_snapshot.sample_ts is not None else None),
        })
        current_value = current_state.state if current_state is not None else None
        status_value = str(status_state.state if status_state is not None else "unknown")
        current_report_ts = self._report_timestamp(current_state)
        snapshot = self._charger_snapshot
        received = self._utc_time(getattr(snapshot, "received_at", None))
        snapshot_age = ((datetime.now(timezone.utc) - received).total_seconds()
                        if received is not None else float("inf"))
        snapshot_is_current = getattr(self._adapter, "snapshot_is_current", None)
        revision = self._charger_source_revision()
        native_clock = self._native_current_clock()
        explicit_native_clock = native_clock is not _NO_NATIVE_CURRENT_CLOCK
        entity_report_times = ((revision[6], revision[3]) if explicit_native_clock else
                              (self._report_timestamp(current_state), self._report_timestamp(status_state)))
        source_stamp = self._utc_time(getattr(snapshot, "current_source_timestamp", None))
        status_source_stamp = self._utc_time(getattr(snapshot, "status_source_timestamp", None))
        source_error = getattr(snapshot, "source_timestamp_error", None)
        current_trusted = True
        current_clock_scope = "native_HA_receipt_not_guaranteed_acquisition"
        # Keep age/observation metadata intact, but identify clocks explicitly.
        # A later HTTP read can reveal an earlier vendor event for the SAME
        # native value. That is a provenance change, not a replay of either
        # clock, and it must never be treated as new response evidence.
        try:
            clock_owner = getattr(self._adapter, "observation_clock_identity", None)
        except Exception:
            clock_owner = None
        if (not isinstance(clock_owner, tuple) or not clock_owner
                or any(type(part) is not int for part in clock_owner)):
            clock_owner = (id(self._adapter),)
        current_report_family = "native_ha_receipt"
        current_report_provenance = ("entity", data.get(CONF_CHARGER_CURRENT_ENTITY) or "", *clock_owner)
        current_observation_ids: tuple[int, ...] = ()

        def maximum_ids(maxima: Any) -> tuple[int, ...]:
            if (not isinstance(maxima, tuple) or not maxima
                    or any(type(key) is not int or key not in (182, 183, 184, 185) for key in maxima)
                    or tuple(sorted(set(maxima))) != maxima):
                return ()
            return maxima

        def vendor_clock_provenance(item: Any) -> tuple[str | int, ...]:
            owner = getattr(item, "source_identity", None)
            if (not isinstance(owner, tuple) or not owner
                    or any(type(part) is not int for part in owner)):
                owner = clock_owner
            maxima = maximum_ids(getattr(item, "current_source_observation_ids", ()))
            return (*owner, "max_terminals", *maxima) if maxima else (*owner, "unknown_maximum")
        native_number = _finite_current_number(current_value)
        increase_ready = True
        native_receipt = None
        native_clock_error = None
        native_maxima = ()
        native_source_times: tuple = ()
        publication_pending = False
        publication_error = None
        event_source_known = False
        event_source_ts = None
        observation_report_times = None
        observation_sequences = None
        observation_evidence_credit = None
        if explicit_native_clock:
            native_maxima = maximum_ids(getattr(native_clock, "observation_ids", ()))
            native_clock_error = getattr(native_clock, "error", None)
            native_receipt = self._utc_time(getattr(native_clock, "received_at", None))
            publication_pending, publication_error = self._current_publication_state(native_clock)
            native_clock_error = native_clock_error or publication_error
            # Numeric state and this terminal receipt must describe the same
            # winning value. A missing receipt is explicit unknown provenance,
            # never an excuse to adopt a fresh HA attribute-fanout timestamp.
            source_number = (_finite_current_number(getattr(native_clock, "current", None))
                if getattr(native_clock, "committed", False) is True and native_number is not None and native_number >= 0
                else _native_current_for_ha_state(current_value, getattr(native_clock, "current", None)))
            binding_matches = bool(native_clock is not None
                and getattr(native_clock, "source_identity", None) == clock_owner)
            if not binding_matches or not native_maxima or source_number is None:
                native_receipt = None
            else:
                # The adapter owns the validated conductor number and event.
                # HA's rounded display supplies no substitute measurement.
                current_value = native_number = source_number
            if native_clock is not None and not binding_matches:
                native_clock_error = "native_terminal_source_mismatch"
                actuator_errors.append("The selected native current source binding changed.")
            current_report_ts = native_receipt.timestamp() if native_receipt else None
            current_report_family = "native_terminal_receipt"
            current_report_provenance = (*clock_owner, "max_terminals", *native_maxima)
            current_observation_ids = native_maxima
            current_clock_scope = "native_winning_terminal_local_receipt_not_physical_acquisition"
            native_source_times = getattr(native_clock, "source_timestamps", ())
            source_times = dict(native_source_times) if isinstance(native_source_times, tuple) else {}
            known_sources = [self._utc_time(source_times.get(key)) for key in native_maxima]
            event_source_known = any(stamp is not None for stamp in known_sources)
            if native_maxima and all(stamp is not None for stamp in known_sources):
                # Same raw frame/terminal observations bind these provider
                # times to our native events. Keep this bound separate from
                # actual local receipt ordering; neither clock is fabricated.
                event_source_ts = min(known_sources).timestamp()
            if native_clock_error and native_clock_error != "native_terminal_values_unavailable":
                current_trusted = False
            tuple_changed_at = getattr(native_clock, "last_tuple_change_monotonic", None)
            tuple_clock_valid = (tuple_changed_at is None or (
                type(tuple_changed_at) in (int, float) and isfinite(tuple_changed_at)
                and 0 <= tuple_changed_at <= self.hass.loop.time()))
            if not tuple_clock_valid:
                # False readiness must mean a genuine recent publication,
                # never a future/nonfinite clock that could open a hold fence.
                native_clock_error = native_clock_error or "native_current_tuple_clock_invalid"
                current_trusted = False
                increase_ready = False
            else:
                increase_ready = self._schedule_current_quiet_tick(tuple_changed_at)
            observation_report_times = tuple((key, stamp.timestamp())
                for key, raw_stamp in getattr(native_clock, "observation_report_times", ())
                if type(key) is int and key in (182, 183, 184, 185)
                and (stamp := self._utc_time(raw_stamp)) is not None)
            observation_sequences = getattr(native_clock, "observation_sequences", ())
            raw_credit = getattr(native_clock, "observation_evidence_credit", ())
            # An absent map belongs to older audited offline fixtures. A live
            # explicit map is validated centrally and never made true by HA.
            observation_evidence_credit = raw_credit or None
            # Atomic publication readiness and measurement credit are separate.
            # The core can prove a whole-RAW normal set_limit without using
            # an ambiguous current event as measurement/response evidence.
            if native_clock_error and native_clock_error != "native_terminal_values_unavailable":
                error = ("Easee native current publication is inconsistent: " + publication_error + "."
                    if publication_error else "Easee native current observation evidence is invalid.")
                errors.append(error)
                ev_errors.append(error)
                source_errors = [*self.source_diagnostics.get("source_errors", ()), error]
                self.source_diagnostics.update(source_health="unhealthy", source_errors=source_errors,
                    source_health_reason=" ".join(source_errors))
        evidence = self._charger_vendor_evidence
        if evidence is not None and (
                self._stopped or errors or health_error or evidence["generation"] != self._generation
                or evidence["adapter"] is not self._adapter
                or evidence["entity_id"] != data.get(CONF_CHARGER_CURRENT_ENTITY)
                or native_number is None or native_number != evidence["value"]
                or not callable(snapshot_is_current) or not snapshot_is_current(evidence["snapshot"])):
            evidence = self._charger_vendor_evidence = None
        if (snapshot is not None and not errors and not health_error and current_trusted and not publication_pending
                and self._charger_snapshot_selection == self._charger_source_revision()
                and 0 <= snapshot_age <= CURRENT_SNAPSHOT_MAX_AGE_SECONDS
                and callable(snapshot_is_current) and snapshot_is_current(snapshot)
                and native_number is not None and native_number == _finite_current_number(snapshot.current)):
            missing = source_stamp is None and (source_error is None
                or source_error == "easee_observation_timestamp_missing")
            malformed = (bool(source_error) and source_error != "easee_observation_timestamp_missing"
                         or (getattr(snapshot, "current_source_timestamp", None) is not None and source_stamp is None)
                         or (source_stamp is not None and (source_stamp - datetime.now(timezone.utc)).total_seconds() > 2))
            if malformed or not missing:
                evidence = self._charger_vendor_evidence = {
                    "generation": self._generation, "adapter": self._adapter, "snapshot": snapshot,
                    "entity_id": data.get(CONF_CHARGER_CURRENT_ENTITY), "value": native_number,
                    "timestamp": None if malformed else source_stamp, "invalid": bool(malformed),
                }
        snapshot_source_rejection = None
        # Import-only raw budgeting can use a bound atomic cache value without
        # using its current as credit. Preserve the native zero-sequence clock
        # domain at bootstrap: a matching old GET must not replace that baseline
        # or turn the first actual native response into an unrelated new domain.
        committed_native_tuple = bool(explicit_native_clock and binding_matches
            and native_maxima and source_number is not None
            and getattr(native_clock, "committed", False) is True
            and native_clock_error is None)
        canonical_native = bool(explicit_native_clock and native_maxima
            and (native_receipt is not None
                 or (self.settings.nominal_import_only_fallback is True and committed_native_tuple)))
        if snapshot is not None:
            if publication_pending:
                snapshot_source_rejection = "native_current_publication_pending"
            elif source_error:
                snapshot_source_rejection = "observation_timestamp_invalid"
            elif source_stamp is None:
                snapshot_source_rejection = "observation_timestamp_missing"
            else:
                source_age = (datetime.now(timezone.utc) - source_stamp).total_seconds()
                if source_age < -2:
                    snapshot_source_rejection = "observation_timestamp_in_future"
                elif source_age > self.settings.max_sensor_age_seconds:
                    snapshot_source_rejection = "observation_timestamp_stale"
                elif (current_report_ts is not None and source_stamp.timestamp() < current_report_ts):
                    # A GET of a previous observation must not replace a later
                    # native event or renew that event's report clock.
                    snapshot_source_rejection = "observation_precedes_native_report"
            if snapshot_source_rejection is None:
                if status_source_stamp is None:
                    snapshot_source_rejection = "status_observation_timestamp_missing_or_invalid"
                elif (status_value.strip().lower() not in START_ALLOWED_STATUSES
                      and snapshot.status != status_value):
                    snapshot_source_rejection = "native_pause_schedule_or_idle_wins"
                elif (snapshot.status != status_value and entity_report_times[1] is not None
                      and status_source_stamp.timestamp() <= entity_report_times[1]):
                    snapshot_source_rejection = "status_observation_precedes_native_report"
            if canonical_native and native_number == _finite_current_number(getattr(snapshot, "current", None)):
                # Equality of float/winner/device does not establish event
                # identity: an older A value can predate an intervening B.
                # Keep the actual native event canonical and the GET metadata
                # unassociated, instead of relabelling A(new) as A(old).
                snapshot_source_rejection = "matching_get_not_native_event_identity"
        # A new HA status/current event supersedes the whole REST pair. Never
        # combine a new current with an old authorization/schedule status.
        snapshot_used = bool(snapshot is not None and not errors and not health_error and current_trusted
            and native_number is not None and native_number >= 0
            and snapshot_source_rejection is None
            and self._charger_snapshot_selection == self._charger_source_revision()
            and 0 <= snapshot_age <= CURRENT_SNAPSHOT_MAX_AGE_SECONDS
            and all((stamp is None and explicit_native_clock) or
                    (stamp is not None and stamp < received.timestamp()) for stamp in entity_report_times)
            and callable(snapshot_is_current) and snapshot_is_current(snapshot))
        if snapshot_used:
            current_value = snapshot.current
            status_value = snapshot.status
            current_trusted = True
            # Vendor observation time is still not guaranteed acquisition
            # time. It is nevertheless distinct from a new HTTP receipt.
            current_report_ts = source_stamp.timestamp()
            current_clock_scope = "vendor_observation_timestamp_not_guaranteed_acquisition"
            current_report_family = "vendor_event"
            current_report_provenance = vendor_clock_provenance(snapshot)
            current_observation_ids = maximum_ids(getattr(snapshot, "current_source_observation_ids", ()))
            event_source_known = True
            event_source_ts = source_stamp.timestamp()
            raw_times = getattr(snapshot, "current_source_timestamps", None)
            observation_report_times = (tuple((key, stamp.timestamp()) for key, raw_stamp in raw_times
                if type(key) is int and key in (182, 183, 184, 185)
                and (stamp := self._utc_time(raw_stamp)) is not None) if raw_times is not None else None)
            observation_sequences = None  # GET receipts are never native push events.
            observation_evidence_credit = None
        elif snapshot is not None:
            self._charger_snapshot = None
            self._charger_snapshot_selection = None
        if not snapshot_used and evidence is not None and not canonical_native and not publication_pending:
            current_trusted = current_trusted and not evidence["invalid"]
            if evidence["timestamp"] is not None:
                current_report_ts = evidence["timestamp"].timestamp()
                current_clock_scope = "retained_vendor_event_timestamp_not_guaranteed_acquisition"
                current_report_family = "vendor_event"
                current_report_provenance = vendor_clock_provenance(evidence["snapshot"])
                current_observation_ids = maximum_ids(getattr(evidence["snapshot"], "current_source_observation_ids", ()))
                event_source_known = True
                event_source_ts = evidence["timestamp"].timestamp()
                raw_times = getattr(evidence["snapshot"], "current_source_timestamps", None)
                observation_report_times = (tuple((key, stamp.timestamp()) for key, raw_stamp in raw_times
                    if type(key) is int and key in (182, 183, 184, 185)
                    and (stamp := self._utc_time(raw_stamp)) is not None) if raw_times is not None else None)
                observation_sequences = None
                observation_evidence_credit = None
            else:
                current_clock_scope = "native_HA_receipt_with_invalid_matching_vendor_metadata"
        if not explicit_native_clock:
            # Preserve the established input contract for injected legacy
            # adapters without the new evidence API. The real Easee adapter
            # always exposes that API, including an explicit unknown result;
            # unavailable real evidence never enters this compatibility path.
            current_report_family = "legacy"
            current_report_provenance = None
            event_source_known = False
            event_source_ts = None
            observation_report_times = None
            observation_sequences = None
            observation_evidence_credit = None
        self.source_diagnostics.update({
            "charger_current_entity_reported_at_utc": (self._utc_time(getattr(current_state, "last_reported", None)).isoformat()
                if self._utc_time(getattr(current_state, "last_reported", None)) else None),
            "charger_status_entity_reported_at_utc": (self._utc_time(getattr(status_state, "last_reported", None)).isoformat()
                if self._utc_time(getattr(status_state, "last_reported", None)) else None),
            "charger_current_source": ("selected_charger_rest_snapshot" if snapshot_used else
                "selected_native_raw_observation" if getattr(native_clock, "committed", False) is True else "ha_entity"),
            "charger_snapshot_received_at_utc": received.isoformat() if received else None,
            "charger_snapshot_used": snapshot_used,
            "charger_snapshot_error_type": self._charger_snapshot_error,
            "charger_snapshot_error_reason": self._charger_snapshot_error_reason,
            "charger_snapshot_failure_type": self._charger_snapshot_failure_type,
            "charger_snapshot_scope": "cloud_state_read_receipt_not_physical_acquisition",
            "charger_snapshot_current_source_timestamp_utc": source_stamp.isoformat() if source_stamp else None,
            "charger_snapshot_status_source_timestamp_utc": (
                self._utc_time(getattr(snapshot, "status_source_timestamp", None)).isoformat()
                if self._utc_time(getattr(snapshot, "status_source_timestamp", None)) else None),
            "charger_snapshot_source_rejection": snapshot_source_rejection,
            "effective_current_clock_scope": current_clock_scope,
            "effective_current_report_family": current_report_family,
            "effective_current_max_observation_ids": current_observation_ids,
            "native_max_observation_ids": native_maxima,
            "native_max_received_at_utc": native_receipt.isoformat() if native_receipt else None,
            "native_max_clock_error": native_clock_error,
            "native_current_committed": getattr(native_clock, "committed", False) is True,
            "native_sdk_current": getattr(native_clock, "sdk_current", None),
            "native_publication_pending": publication_pending,
            "native_publication_watchdog_remaining_seconds": (max(0., self._current_publication_deadline-self.hass.loop.time())
                if self._current_publication_deadline is not None else None),
            "native_max_source_timestamps_utc": tuple((key, stamp.isoformat() if isinstance(stamp, datetime) else None)
                for key, stamp in native_source_times),
            "current_tuple_increase_ready": increase_ready,
            "current_event_source_known": event_source_known,
            "current_event_source_at_utc": (datetime.fromtimestamp(event_source_ts, timezone.utc).isoformat()
                if event_source_ts is not None else None),
            "current_observation_report_times": observation_report_times,
            "current_observation_sequences": observation_sequences,
            "current_observation_evidence_credit": observation_evidence_credit,
            "matching_get_metadata_association": "unassociated_with_native_event" if canonical_native and evidence else None,
            "charger_current_trusted": current_trusted,
            "retained_current_source_timestamp_utc": (
                evidence["timestamp"].isoformat() if evidence is not None and evidence["timestamp"] is not None else None),
            "charger_snapshot_prefetch_target_seconds": self._snapshot_freshness_window() / 2,
            "charger_snapshot_request_floor_seconds": CURRENT_SNAPSHOT_MIN_INTERVAL_SECONDS,
            "charger_snapshot_failure_backoff_count": self._charger_snapshot_failures,
            "charger_snapshot_next_request_in_seconds": max(0.0, self._charger_snapshot_next_request - self.hass.loop.time()),
            "charger_snapshot_prefetch_due_in_seconds": (
                max(0.0, self._charger_snapshot_refresh_due - self.hass.loop.time())
                if self._charger_snapshot_refresh_due is not None else None),
            "effective_charger_current": current_value,
            "effective_charger_status": status_value,
            "effective_charger_reported_at_utc": (datetime.fromtimestamp(current_report_ts, timezone.utc).isoformat()
                if current_report_ts is not None else None),
        })
        if self._l1_source_config or self._l1_source_error:
            self.source_diagnostics["report_time_scope"] = "grid_validated_mqtt_sample_and_receipt;easee_ha_receipt_not_physical_acquisition"
        source_id = self._configured_control_identity()
        grid_health = actuator_health = ev_health = None
        if self.settings.meter_envelope_control:
            wall_now = datetime.now(timezone.utc).timestamp()
            grid_number = _finite_current_number(grid_value)
            if grid_number is None or grid_number < 0:
                grid_errors.append("L1 telemetry must be a finite, non-negative current.")
            if (grid_report_ts is None or not -5 <= wall_now - grid_report_ts <= self.settings.max_sensor_age_seconds):
                grid_errors.append("L1 telemetry has no fresh validated report clock.")
            if status_value.strip().lower() not in ALLOWED_ACTIVE_STATUSES | IDLE_STATUSES:
                actuator_errors.append("Charger status is unavailable, unsupported or a fault.")
            if source_id is None:
                actuator_errors.append("The configured meter/charger control binding is unverified.")
            ev_number = _finite_current_number(current_value)
            if ev_number is None or ev_number < 0:
                ev_errors.append("Charger current must be a finite, non-negative estimate.")
            if not current_trusted:
                ev_errors.append("Charger current provenance is untrusted.")
            if publication_pending or not increase_ready:
                ev_errors.append("Charger current publication is incomplete.")
            if (current_report_ts is None or not -5 <= wall_now - current_report_ts <= self.settings.max_sensor_age_seconds):
                ev_errors.append("Charger current has no fresh observation clock.")
            if event_source_known and (event_source_ts is None
                    or not -2 <= wall_now - event_source_ts <= self.settings.max_sensor_age_seconds):
                ev_errors.append("Charger current provider event is old or invalid.")
            if product_data_quiet:
                ev_errors.append("ProductUpdate data is quiet; cached EV readings grant no response or increase credit.")
            grid_health = SourceHealth(not grid_errors, " ".join(dict.fromkeys(grid_errors)) or None)
            actuator_health = SourceHealth(not actuator_errors, " ".join(dict.fromkeys(actuator_errors)) or None)
            ev_health = SourceHealth(not ev_errors, " ".join(dict.fromkeys(ev_errors)) or None)
            self.source_diagnostics.update({
                "meter_grid_health": asdict(grid_health),
                "meter_actuator_health": asdict(actuator_health),
                "meter_ev_health": asdict(ev_health),
                "meter_envelope_control": True,
                "meter_grid_valid": grid_health.valid,
                "meter_actuator_valid": actuator_health.valid,
                "meter_ev_valid": ev_health.valid,
                "meter_control_health": "healthy" if grid_health.valid and actuator_health.valid else "unhealthy",
                "meter_estimate_quality": "usable" if ev_health.valid else "unavailable_for_increase",
                "easee_product_data_quiet": product_data_quiet,
                "meter_source_scope": "validated_RAW_and_actuator_faults_are_critical;EV_estimate_quality_controls_increase_credit",
            })
        controlled_l1 = None
        # Observational evidence may be captured at a legacy SDK boundary for
        # a future explicit policy handoff. Only the selected policy consumes
        # it; legacy numeric inputs, decisions and health remain unchanged.
        if self._adapter is not None:
            try:
                native_l1 = getattr(self._adapter, "native_controlled_l1_observation", None)
            except Exception:
                native_l1 = None
            def l1_stamp(name: str) -> float | None:
                value = self._utc_time(getattr(native_l1, name, None))
                return value.timestamp() if value is not None else None
            if native_l1 is not None:
                controlled_l1 = ControlledL1Evidence(
                    current=getattr(native_l1, "current", None),
                    received_at_ts=l1_stamp("received_at"),
                    sequence=getattr(native_l1, "sequence", 0),
                    evidence_credit=getattr(native_l1, "evidence_credit", False),
                    source_id=source_id,
                    receiver_identity=getattr(native_l1, "source_identity", ()),
                    mapping_epoch=getattr(native_l1, "mapping_epoch", 0),
                    output_phase=getattr(native_l1, "output_phase", None),
                    mapping_adopted_at_ts=l1_stamp("mapping_adopted_at"),
                    mapping_sequence=getattr(native_l1, "mapping_sequence", 0),
                    source_ts=l1_stamp("source_at"),
                    source_highwater_ts=l1_stamp("source_highwater_at"),
                    mapping_source_ts=l1_stamp("mapping_source_at"),
                    mapping_source_highwater_ts=l1_stamp("mapping_source_highwater_at"),
                    error=getattr(native_l1, "error", None),
                    observation_id=getattr(native_l1, "observation_id", None),
                    committed=getattr(native_l1, "committed", False))
            def l1_utc(name: str) -> str | None:
                value = self._utc_time(getattr(native_l1, name, None))
                return value.isoformat() if value is not None else None
            controlled_diagnostics = {
                "controlled_l1_observed_current": getattr(native_l1, "current", None),
                "controlled_l1_received_at_utc": l1_utc("received_at"),
                "controlled_l1_source_at_utc": l1_utc("source_at"),
                "controlled_l1_output_phase": getattr(native_l1, "output_phase", None),
                "controlled_l1_mapping_epoch": getattr(native_l1, "mapping_epoch", None),
                "controlled_l1_mapping_adopted_at_utc": l1_utc("mapping_adopted_at"),
                "controlled_l1_mapping_sequence": getattr(native_l1, "mapping_sequence", None),
                "controlled_l1_sequence": getattr(native_l1, "sequence", None),
                "controlled_l1_error": getattr(native_l1, "error", "controlled_l1_unavailable"),
                "controlled_l1_current_error": getattr(native_l1, "current_error", None),
                "controlled_l1_committed": getattr(native_l1, "committed", False),
                "controlled_l1_scope": "native183_input_L1_under_verified_output110mode10;MAX_and_pilot_separate",
            }
            # MAX is retained as an aggregate diagnostic/upper, never an L1
            # subtraction certificate. Describe the new estimate channel
            # without promoting its availability into response/rise proof.
            if self.settings.meter_envelope_control:
                self.source_diagnostics.update(controlled_diagnostics)
                mapped_current = _finite_current_number(getattr(controlled_l1, "current", None))
                mapped_quality = bool(controlled_l1 is not None and controlled_l1.committed is True
                    and controlled_l1.error is None and controlled_l1.output_phase == 10
                    and mapped_current is not None
                    and mapped_current >= 0 and controlled_l1.received_at_ts is not None
                    and 0 <= wall_now-controlled_l1.received_at_ts <= self.settings.max_sensor_age_seconds
                    and (controlled_l1.source_ts is None or 0 <= wall_now-controlled_l1.source_ts <= self.settings.max_sensor_age_seconds))
                ev_health = SourceHealth(mapped_quality, None if mapped_quality else
                    getattr(native_l1, "error", None) or getattr(native_l1, "current_error", None)
                    or "Mapped L1 native evidence is unavailable.")
                self.source_diagnostics.update({"meter_ev_health": asdict(ev_health),
                    "meter_ev_valid": ev_health.valid,
                    "meter_estimate_quality": "mapped_l1_available_for_qualification" if mapped_quality else "unavailable_for_increase"})
        return RegulatorInput(
            now_ts=self.hass.loop.time(),
            status=status_value,
            grid_l1=grid_value,
            grid_l2=_get_state_value(self.hass, data.get(CONF_L2_ENTITY)),
            grid_l3=_get_state_value(self.hass, data.get(CONF_L3_ENTITY)),
            charger_current=current_value,
            data_valid=not errors,
            data_error=" ".join(errors) or None,
            grid_report_ts=grid_report_ts,
            charger_report_ts=current_report_ts,
            wall_now_ts=datetime.now(timezone.utc).timestamp(),
            charger_snapshot_verified=snapshot_used,
            charger_current_trusted=current_trusted,
            # Only the selected atomic native tuple can authorize the narrow
            # raw-grid recovery path. A matching cloud GET is not that tuple.
            charger_native_committed=(canonical_native and not snapshot_used
                and committed_native_tuple and not publication_pending),
            control_source_id=source_id,
            charger_report_family=current_report_family,
            charger_report_provenance=current_report_provenance,
            charger_increase_ready=increase_ready,
            charger_publication_pending=publication_pending,
            charger_event_source_known=event_source_known,
            charger_event_source_ts=event_source_ts,
            charger_observation_report_times=observation_report_times,
            charger_observation_sequences=observation_sequences,
            charger_observation_evidence_credit=observation_evidence_credit,
            meter_grid_health=grid_health,
            meter_actuator_health=actuator_health,
            meter_ev_health=ev_health,
            meter_product_data_quiet=product_data_quiet,
            controlled_l1=controlled_l1,
        )

    def _configured_control_identity(self) -> str | None:
        """Stable verified source binding; never include secrets or SDK objects."""
        try:
            registry = er.async_get(self.hass)
            data = self.entry.data
            selected_device = data.get(CONF_DEVICE_ID)
            if not selected_device:
                return None
            values = [selected_device, data.get(CONF_CONTROLLED_PHASE, "L1")]
            for key in (CONF_L1_ENTITY, CONF_CHARGER_CURRENT_ENTITY, CONF_STATUS_ENTITY):
                entity_id = data.get(key)
                entity = registry.async_get(entity_id)
                if entity is None or not entity.unique_id or entity.disabled_by:
                    return None
                if key != CONF_L1_ENTITY and (entity.platform != "easee" or entity.device_id != selected_device):
                    return None
                values.extend((entity_id, entity.platform, entity.unique_id, entity.device_id))
            values.append(repr(self._l1_source_config))
            return hashlib.sha256(repr(tuple(values)).encode()).hexdigest()
        except Exception:
            return None

    def _sync_command_identity(self) -> None:
        identity = getattr(self._adapter, "control_identity", None)
        if identity != self._control_identity:
            binding = self._configured_control_identity()
            latest = self._delivery.latest
            unresolved_zero = self._delivery.unresolved_correlated_zero or (
                    latest is not None and latest.phases == (0, 0, 0))
            same_or_unknown_binding = binding is None or self._delivery_binding is None or binding == self._delivery_binding
            if self._control_identity is not None and unresolved_zero and same_or_unknown_binding:
                self._protective_zero_pending = True
                # A new stream is not proof that the same charger's cloud
                # queue drained. Keep exact unresolved IDs for later replies.
            elif not same_or_unknown_binding or not unresolved_zero:
                self._delivery.reset()
            self._control_identity = identity
            self._delivery_binding = binding

    def _zero_readback_evidence(self, inp: RegulatorInput, *, require_new_current: bool = True) -> dict[str, Any]:
        record = self._delivery.latest
        reported = tuple(self.source_diagnostics.get(f"reported_circuit_p{phase}") for phase in (1, 2, 3))
        receipts = getattr(self._adapter, "reported_limit_receipts", (None, None, None))
        transition = receipts[0] if isinstance(receipts, tuple) and len(receipts) == 3 else None
        if type(transition) not in (int, float) or not isfinite(transition):
            transition = None
        after = bool(record and transition is not None and transition >= record.started)
        age = self.hass.loop.time() - transition if transition is not None else None
        if self.settings.meter_envelope_control:
            # The standalone episode owns the original zero-command floor and
            # any newer possibly delivered positive command. Only qualified
            # mapped L1 evidence can discharge that delivery barrier. A GET or
            # aggregate MAX report cannot create such physical response proof.
            current_evidence = self.regulator.meter_zero_current_evidence(inp)
            healthy = bool(inp.meter_grid_health and inp.meter_grid_health.valid
                           and inp.meter_actuator_health and inp.meter_actuator_health.valid)
        else:
            # A GET receipt cannot turn an old 0 A current event into fresh proof.
            accepted = self.last_accepted_limit
            started = self._utc_time(accepted.get("started_at_utc")) if accepted else None
            fresh_ev = bool(inp.charger_current_trusted and (not require_new_current or (
                            started is not None and inp.charger_report_ts is not None
                            and inp.charger_report_ts > started.timestamp()
                            and (not inp.charger_event_source_known or (
                                inp.charger_event_source_ts is not None
                                and inp.charger_event_source_ts > started.timestamp())))))
            current_evidence = inp.charger_current if fresh_ev else None
            healthy = inp.data_valid
        return dict(phases=reported if self.source_diagnostics.get("reported_circuit_available") else None,
                    after_submission=after, report_age=age,
                    ev_current=current_evidence,
                    healthy=healthy, offline_zero=bool(getattr(self._adapter, "offline_limits_verified_zero", False)),
                    max_age=self.settings.max_sensor_age_seconds)

    def _zero_ordering_confirmed(self, inp: RegulatorInput) -> bool:
        """Separate expired prior authority from a measured physical OFF."""
        if self._delivery.can_skip_zero(**self._zero_readback_evidence(inp, require_new_current=False)):
            return True
        return bool(self.settings.meter_envelope_control
                    and self.regulator.meter_positive_authority_ended(inp))

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
                                 health_error: str | None, input_errors: list[str], *,
                                 grid_value: Any = None, grid_report_ts: float | None = None) -> None:
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
            value = grid_value if label == "L1" else getattr(state, "state", None)
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
            "source_health_scope": "required_input_values_and_latest_product_update; not_transport_ping_or_physical_sample; see regulator reason for policy and alignment",
            "report_time_scope": "ha_receipt_not_physical_acquisition",
            "easee_stream_pulse_utc": None,
            "reported_circuit_p1": None, "reported_circuit_p2": None, "reported_circuit_p3": None,
            "reported_circuit_reported_at_utc": None,
            "reported_circuit_source": None,
            "reported_circuit_available": False,
        }
        for name, state in (("grid", grid), ("charger_current", current), ("charger_status", status)):
            reported = (datetime.fromtimestamp(grid_report_ts, timezone.utc)
                        if name == "grid" and grid_report_ts is not None
                        else self._utc_time(getattr(state, "last_reported", None)) if name != "grid" else None)
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
        diagnostics["easee_product_update_received_at_utc"] = diagnostics["easee_stream_pulse_utc"]
        diagnostics["easee_product_update_clock_scope"] = "native_local_ProductUpdate_receipt_not_transport_ping_or_physical_acquisition"
        self.source_diagnostics = diagnostics

    @staticmethod
    def compact_state_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
        return _compact_state_attributes(attributes)

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
        delivery = self._delivery.diagnostics()
        delivery["recent_command_deliveries"] = delivery.get("recent_command_deliveries", [])[-2:]
        stop_summaries = [{key: item[key] for key in (
            "plan_utc", "mode", "reason", "grid_l1", "charger_current", "status", "command_result",
        ) if key in item} for item in list(self._recent_stop_contexts)[-2:]]
        for item in stop_summaries:
            reason = item.get("reason")
            if isinstance(reason, str) and len(reason.encode("utf-8")) > 160:
                item["reason"] = reason.encode("utf-8")[:160].decode("utf-8", errors="ignore") + " [truncated]"
        stop = dict(self.last_stop_context) if self.last_stop_context else None
        if stop is not None and isinstance(stop.get("preceding_positive_limit_receipts"), list):
            stop["preceding_positive_limit_receipts"] = [
                _limit_receipt_summary(item) for item in stop["preceding_positive_limit_receipts"][-4:]]
        meter_control = {}
        if self.settings.meter_envelope_control:
            meter_control["meter_control_health"] = (
                "blocked" if self.last_decision is not None
                and getattr(self.last_decision, "meter_zero_cause", None) is not None
                else self.source_diagnostics.get("meter_control_health", "unverified"))
            standalone = self.regulator.meter_control_diagnostics()
            meter_control.update({public: standalone.get(internal) for public, internal in (
                ("meter_control_state", "state"), ("meter_episode_id", "episode_id"),
                ("meter_episode_phase", "episode_phase"),
                ("meter_response_deadline_ts", "response_deadline"),
                ("meter_grid_deadline_ts", "grid_deadline"),
                ("meter_operational_fault", "operational_fault"),
                ("meter_recovery_stage", "recovery_stage"),
                ("meter_recovery_required", "recovery_required"),
                ("meter_cycle_id", "cycle_id"), ("meter_cycle_used_seconds", "used_seconds"),
                ("meter_cycle_remaining_seconds", "remaining_seconds"),
                ("meter_historical_fault", "historical_failure"),
                ("meter_incident_count", "incident_count"),
            )})
        return self.compact_state_attributes({
            **self.source_diagnostics,
            **meter_control,
            **delivery,
            "peak_only_response": (self.settings.meter_envelope_control or self.regulator.settings.peak_only_response),
            "peak_only_response_setting": self.regulator.settings.peak_only_response,
            "response_policy": ("standalone_meter_episode" if self.settings.meter_envelope_control
                                else "raw_peak_over_28_only" if self.regulator.settings.peak_only_response else "legacy"),
            "peak_response_trigger_amps": PEAK_RESPONSE_TRIGGER_AMPS,
            "peak_response_active": self.regulator.peak_response_active,
            "peak_response_deadline_ts": self.regulator.peak_response_deadline(),
            "ordinary_lower_offer_pending": (not self.settings.meter_envelope_control
                                              and self.regulator._ordinary_lower is not None),
            "automatic_overload_cycles": (self.settings.meter_envelope_control
                                           or self.regulator.settings.automatic_overload_cycles),
            "automatic_overload_cycles_setting": self.regulator.settings.automatic_overload_cycles,
            "disconnected_readiness": self.regulator.settings.disconnected_readiness,
            **self.regulator.automatic_cycle_diagnostics(),
            **({"mode": self.last_decision.mode, "reason": self.last_decision.reason,
                "current_p1": self.last_decision.current_p1, "current_p2": self.last_decision.current_p2,
                "current_p3": self.last_decision.current_p3,
                "meter_state": getattr(self.last_decision, "meter_state", None),
                "meter_zero_cause": getattr(self.last_decision, "meter_zero_cause", None),
                "controlled_l1_current": getattr(self.last_decision, "controlled_l1_current", None),
                "controlled_l1_used": getattr(self.last_decision, "controlled_l1_used", False),
                "readiness_deadline_ts": getattr(self.last_decision, "readiness_deadline_ts", None)}
                if self.last_decision is not None else {}),
            "zero_confirmation_scope": ("qualified_mapped_L1_after_actual_zero_SDK_and_circuit_delivery_not_GET_or_MAX"
                if self.settings.meter_envelope_control
                else "native_ProductUpdate_receipt_and_correlated_delivery_not_physical_response"),
            "last_command_attempt": dict(self.last_command_attempt) if self.last_command_attempt else None,
            "last_accepted_command": dict(self.last_accepted_command) if self.last_accepted_command else None,
            "last_accepted_limit": dict(accepted) if accepted else None,
            "last_stop_context": stop,
            "recent_command_journal": [_limit_receipt_summary(item) for item in list(self._recent_commands)[-2:]],
            "recent_stop_contexts": stop_summaries,
            "recent_command_journal_count": len(self._recent_commands),
            "recent_stop_contexts_count": len(self._recent_stop_contexts),
            "recent_command_deliveries_count": len(self._delivery.records),
            "diagnostics_truncated": bool(self._recent_commands or self._recent_stop_contexts
                                           or len(self._delivery.records) > 2),
            "incident_journal_error_type": self._incident_journal_error,
            "incident_journal_scope": "bounded_local_receipts_and_vendor_observations_not_physical_proof",
            "last_stop_nominal_diagnostics": (dict(self.last_stop_nominal_diagnostics)
                                               if self.last_stop_nominal_diagnostics else None),
            "reported_cap_matches_last_accepted_limit": comparison,
            "reported_cap_report_after_acceptance": post_acceptance,
            "readback_scope": "cached_circuit_cap_only",
            "physical_current_verified": False,
            "device_ttl_verified": False,
            "budget_storage_error": self._budget_error,
            "budget_verification_pending": self._budget_store.verification_pending,
            "overload_failure_reason": self.regulator.effective_overload_failure_reason,
            "overload_historical_failure_reason": self.regulator.export_overload_state()["failure_reason"],
            **self.regulator.trial_diagnostics(),
            "overload_trial_action_result": self._trial_action_result,
            "overload_trial_action_error_type": self._trial_action_error,
            "budget_storage_error_type": self._budget_error_type,
            "budget_checkpoint_verified_at_utc": (
                datetime.fromtimestamp(self._budget_store.last_verified_at, timezone.utc).isoformat()
                if self._budget_store.last_verified_at is not None else None),
            "budget_latest_state_durable": self._budget_store.state_is_durable(
                self.regulator.export_overload_state()),
        })

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

    def _watch_pending_checkpoint(self) -> None:
        if self._budget_pending_token is not None:
            return
        token = self._budget_pending_token = object()

        def finished(_task: asyncio.Task) -> None:
            if self._budget_pending_token is not token:
                return
            self._budget_pending_token = None
            if not self._stopped:
                # Completion alone grants nothing. Re-evaluate fresh inputs
                # and verify the LATEST conservative state before permission.
                self._budget_retry_at = 0.0
                self._handle_source_change(None)

        if not self._budget_store.add_pending_callback(finished):
            self._budget_pending_token = None

    def _pending_checkpoint_allows_normal(self, decision: Any, inp: RegulatorInput) -> bool:
        """No new durability-dependent authority, EV credit, rise or restart."""
        if (self._budget_error != "overload_budget_save_pending"
                or not self._budget_store.verification_pending):
            return False
        r, s = self.regulator, self.settings
        inp = self._read_input()
        if s.meter_envelope_control:
            # Only the supervisor may retain a real old normal lease. EV
            # publication quality is not a persistence or RAW fault. A genuine
            # overload PRE still needs its exact durable checkpoint.
            if not self._budget_loaded or decision.meter_overload_authority:
                return False
            retained = self._committed_normal_retention(inp, decision)
            return bool(retained is not None and retained.current_p1 == decision.current_p1)
        if not inp.data_valid or inp.data_error:
            return False
        if decision.mode.removeprefix("dry_run_") == "normal_continuity":
            retained = self._committed_normal_retention(inp, decision)
            return bool(retained is not None and retained.current_p1 == decision.current_p1)
        no_overload_authority = (not r.automatic_cycle_requires_checkpoint
            and not r._overload_hold_active and r._overload_transition_started is None)
        if (decision.mode.removeprefix("dry_run_") == "ordinary_response_hold"
                and self._budget_loaded and no_overload_authority):
            # This accepted decrease keeps only its frozen birth bound and
            # original response time; a pending write cannot renew either.
            return r.can_retain_ordinary_response(inp, decision)
        if (decision.mode.removeprefix("dry_run_") == "startup_grid_wait"
                and self._budget_loaded and no_overload_authority):
            # Only the already accepted minimum and its original observation
            # deadline survive an unfinished metadata write. The core checks
            # the latest sources again; no command or lease is created here.
            return r.can_retain_startup_grid_confirmation(inp, decision)
        if (decision.mode.removeprefix("dry_run_") == "readiness_wait"
                and self._budget_loaded and no_overload_authority):
            # This connected waiting phase can retain only the old accepted
            # Ready lease. It grants neither a write nor additional lifetime.
            return r.can_retain_readiness_wait(inp, decision)
        if (decision.mode.removeprefix("dry_run_") == "disconnected_ready"
                and self._budget_loaded and no_overload_authority
                and not self._protective_zero_pending
                and self.hass.loop.time() >= self._positive_retry_after
                and not self._delivery.unresolved_correlated_zero):
            receipt = self._delivery.latest
            ordered_zero = self._delivery.can_skip_zero(**self._zero_readback_evidence(
                inp, require_new_current=False))
            zero_barrier = bool(receipt and receipt.phases == (0, 0, 0)
                and (receipt.http_status is not None or receipt.state in {
                    "submission_failed_unverified", "superseded_unverified", "vendor_rejected"})
                and not ordered_zero)
            if not zero_barrier and r.can_dispatch_pending_readiness(inp, decision):
                # A loaded ledger with an unfinished verification is not a new
                # overload allowance. Only an independent whole-raw normal
                # offer while genuinely disconnected may use this path.
                return True
        if (decision.mode.removeprefix("dry_run_") == "positive_ramp_hold"
                and no_overload_authority):
            # A centrally qualified existing owner grants NO action or lease.
            # Check fresh sources and the original deadline after the await;
            # a forged mode string cannot create this proof or another owner.
            return r.can_retain_positive_ramp(inp, decision)
        if no_overload_authority and r.can_retain_normal_offer(inp, decision):
            # A genuine coherent normal pair can retain only the old live
            # upper bound. The caller strips its proposed TTL renewal below.
            return True
        cap, grid = (_finite_current_number(value) for value in (decision.current_p1, inp.grid_l1))
        now, wall, gt = (_finite_current_number(value) for value in
                         (self.hass.loop.time(), inp.wall_now_ts, inp.grid_report_ts))
        normal = r.export_overload_state()["normal_control"]
        return bool(
            inp.data_valid and not inp.data_error and inp.charger_current_trusted
            and decision.command in {None, "set_limit"} and not decision.dry_run
            and s.active_control and not s.dry_run and s.enable_dynamic_limit
            and cap is not None and s.min_charge_amps <= cap <= s.max_charge_amps
            and grid is not None and grid >= 0 and grid + cap <= s.main_fuse_amps-s.buffer_amps
            and now is not None and wall is not None and gt is not None
            and 0 <= wall-gt <= s.max_sensor_age_seconds
            and inp.status.strip().lower() in START_ALLOWED_STATUSES
            and r.last_target is not None and 0 < cap <= r.last_target[0]
            and r.last_set_limit_ts is not None and 0 <= now-r.last_set_limit_ts < r.last_target[3]
            and inp.control_source_id is not None and inp.control_source_id == r._accepted_control_source_id
            and not r._restart_required and not r.paused_by_us and not r.peak_response_active
            and not r.automatic_cycle_requires_checkpoint and not r._overload_hold_active
            and r._overload_transition_started is None
            and not decision.mode.removeprefix("dry_run_").startswith("overload")
            and normal["cap"] is None and not normal["response_pending"]
            and not normal.get("alignment_pending") and not normal["stops"]
            and not normal["restart"] and not normal["retry_submission_pending"]
            and normal["response_failure"] is None
        )

    def _committed_normal_retention(self, inp: RegulatorInput, proposed: Any = None) -> Any:
        """Retain old actual normal authority, never an unfinished proposal."""
        if (not self._budget_loaded or self._stopped
                or self._budget_error not in {None, "overload_budget_save_pending"}
                or self._protective_zero_pending
                or self.hass.loop.time() < self._positive_retry_after
                or self._delivery.unresolved_correlated_zero):
            return None
        receipt = self._delivery.latest
        if (receipt and receipt.phases == (0, 0, 0)
                and (receipt.http_status is not None or receipt.state in {
                    "submission_failed_unverified", "superseded_unverified", "vendor_rejected"})
                and not self._zero_ordering_confirmed(inp)):
            return None
        if self.settings.meter_envelope_control:
            return self.regulator.meter_envelope_retention(inp, proposed)
        return self.regulator.committed_normal_retention(inp, proposed)

    def _critical_runtime_input(self, inp: RegulatorInput, reason: str) -> RegulatorInput:
        """Propagate actual runtime faults without reclassifying EV estimates."""
        if self.settings.meter_envelope_control:
            return replace(inp, data_valid=False, data_error=reason,
                           meter_actuator_health=SourceHealth(False, reason))
        return replace(inp, data_valid=False, data_error=reason)

    def _runtime_offline_problem(self, inp: RegulatorInput) -> str | None:
        if self.settings.meter_envelope_control and inp.meter_product_data_quiet:
            return getattr(self._adapter,"offline_fallback_block_reason","offline_fallback_unverified")
        return getattr(self._adapter,"overload_block_reason","offline_fallback_unverified")

    async def _checkpoint_unsent_cleanup(self, decision: Any) -> None:
        """Remove only a proven unsent intent, preserving all old exposure."""
        if not self.regulator.reject_unsubmitted(decision, self.hass.loop.time()):
            return
        self._normal_control_checkpoint_pending = True
        try:
            await asyncio.wait_for(self._budget_store.async_save(
                self.regulator.export_overload_state()), EXPOSURE_STORE_TIMEOUT_SECONDS)
            if not self._budget_store.state_is_durable(self.regulator.export_overload_state()):
                raise RuntimeError("Unsent intent cleanup is not yet verified")
        except Exception as err:
            pending = isinstance(err, TimeoutError) and self._budget_store.verification_pending
            if self._budget_error != "overload_budget_restore_failed":
                self._budget_error = ("overload_budget_save_pending" if pending
                    else "overload_budget_save_failed")
            self._budget_error_type = type(err).__name__
            self._budget_retry_at = self.hass.loop.time() + EXPOSURE_STORE_RETRY_SECONDS
            if pending:
                self._watch_pending_checkpoint()
            else:
                # The protection is for a real durability failure, never the
                # local refusal itself. Re-evaluate the independent fault.
                self._handle_source_change(None)
        else:
            if self._budget_error == "overload_budget_save_pending":
                self._budget_error = self._budget_error_type = None
            self._budget_retry_at = 0.

    def _input_revision(self, inp: RegulatorInput) -> tuple:
        def age_class(report: float | None) -> tuple | None:
            if report is None or inp.wall_now_ts is None:
                return None
            age = inp.wall_now_ts - report
            return (age < 0, age < -5,
                    age <= self.settings.measurement_settle_seconds,
                    age <= self.settings.max_sensor_age_seconds)
        return (inp.status, inp.grid_l1, inp.charger_current, inp.data_valid, inp.data_error,
                inp.grid_report_ts, inp.charger_report_ts, inp.charger_snapshot_verified,
                inp.charger_current_trusted, inp.charger_native_committed, inp.control_source_id,
                inp.charger_report_family, inp.charger_report_provenance,
                inp.charger_increase_ready,
                inp.charger_publication_pending,
                inp.charger_event_source_known, inp.charger_event_source_ts,
                inp.charger_observation_report_times,
                inp.charger_observation_sequences, inp.charger_observation_evidence_credit,
                inp.meter_grid_health, inp.meter_actuator_health, inp.meter_ev_health,
                inp.meter_product_data_quiet,
                inp.controlled_l1 if self.settings.meter_envelope_control else None,
                age_class(inp.grid_report_ts), age_class(inp.charger_report_ts))

    async def _async_tick_locked(self) -> None:
        generation = self._generation
        # Recheck public identity metadata as well as entry updates. A renamed,
        # rebound or removed source cannot leave an old MQTT subscription live.
        await self._async_configure_l1_source_locked()
        if self._stopped or generation != self._generation:
            return
        # BMW is optional advisory context. Failure cannot stop or delay the
        # local meter/Easee regulator and never causes a BMW API request.
        try:
            self.bmw_context = self._bmw_context.snapshot()
        except Exception:
            self.bmw_context = {}
        self._sync_command_identity()
        inp = self._read_input()
        try:
            same_settings = _settings_from_entry(self.entry) == self.settings
        except (TypeError, ValueError):
            same_settings = False
        if inp.data_valid and same_settings:
            # Registry/MQTT metadata can become verified after initial setup.
            # Remember the binding this locked tick actually processes, so
            # a later identical entry notification is not a policy change.
            self._applied_control_binding = inp.control_source_id
            self._applied_control_selection = self._electrical_selection()
        self._schedule_current_snapshot(inp)
        plan_source_diagnostics = dict(self.source_diagnostics)
        if (self._budget_error == "overload_budget_save_pending"
                and not self._budget_store.verification_pending
                and self._budget_store.last_error_type):
            self._budget_error = "overload_budget_save_failed"
            self._budget_error_type = self._budget_store.last_error_type
            self._budget_retry_at = self.hass.loop.time() + EXPOSURE_STORE_RETRY_SECONDS
        if self.settings.overload_enabled or self.settings.meter_envelope_control:
            offline_problem = self._runtime_offline_problem(inp)
            budget_fault = self._budget_error if self._budget_error != "overload_budget_save_pending" else None
            if offline_problem or budget_fault:
                inp = self._critical_runtime_input(inp, budget_fault or offline_problem)
        # A protective evaluation may clear the owner that explains its cause.
        # Keep only bounded scalar diagnostics for the private incident journal;
        # these snapshots have no control authority or public entity attribute.
        r = self.regulator
        if self.settings.meter_envelope_control:
            private_plan_context = {"meter_control_before": r.meter_control_diagnostics(),
                                    "owner_clock_scope": "standalone_episode_monotonic_seconds_not_utc"}
        else:
            ordinary = r._ordinary_lower
            confirmation = r._minimum_confirmation
            private_plan_context = {
                "established_current_before": _finite_current_number(r._ev_established_value),
                "established_value_match_before": r._ev_established_value is not None
                    and inp.charger_current == r._ev_established_value,
                "established_binding_match_before": r._ev_established_binding is not None
                    and inp.control_source_id == r._ev_established_binding,
                "active_owner_caps_before": {key: value for key, value in {
                    "positive": r._positive_cap,
                    "ordinary": ordinary.cap if ordinary is not None else None,
                    "minimum_confirmation": confirmation.cap if confirmation is not None else None,
                }.items() if value is not None},
                "active_owner_deadlines_before": {key: value for key, value in {
                    "positive": r._positive_ramp_deadline,
                    "ordinary": ordinary.observation_deadline if ordinary is not None else None,
                    "minimum_confirmation": confirmation.deadline if confirmation is not None else None,
                    "nominal": r._normal_shed_deadline,
                    "low_grid": r._low_grid_deadline,
                    "readiness": r._readiness_wait_deadline,
                    "peak": getattr(r._peak_response, "deadline", None),
                }.items() if value is not None},
                "shared_confirmation_owner_before": confirmation.owner if confirmation is not None else None,
                "owner_clock_scope": "monotonic_seconds_not_utc",
            }
        decision = None
        pending_proposal = self._meter_checkpoint_proposal
        if pending_proposal is not None:
            # A verified write can take longer than the caller's bounded I/O
            # wait. Re-evaluating and saving a moving exposure counter on every
            # completion would make that successful checkpoint obsolete before
            # its original reduction could ever enter SDK. Keep this one
            # unsent intent and exact snapshot, without pausing source checks.
            compatible = bool(self.settings.meter_envelope_control and same_settings
                and pending_proposal["generation"] == generation
                and self._budget_error in {None, "overload_budget_save_pending"}
                and self._budget_store.fingerprint(r.export_overload_state())
                    == pending_proposal["fingerprint"]
                and r.can_continue_meter_dispatch(pending_proposal["input"], inp,
                                                 pending_proposal["decision"]))
            if not compatible:
                self._meter_checkpoint_proposal = None
            elif self._budget_store.verification_pending:
                retained = self._committed_normal_retention(inp, pending_proposal["decision"])
                self.last_decision = retained if retained is not None else replace(
                    pending_proposal["decision"], mode="meter_wait_storage", meter_state="meter_wait_storage",
                    current_p1=0, current_p2=0, current_p3=0, command=None, service=None,
                    should_call_service=False, meter_normal_retention=False,
                    reason="Waiting for this exact normal proposal's checkpoint; no command or lease has been issued.")
                self.last_service_result = "not_sent_storage_pending"
                self._schedule_overload_deadline(self.last_decision)
                self.notify()
                return
            elif self._budget_store.state_is_durable(pending_proposal["exposure"]):
                decision = pending_proposal["decision"]
                self._meter_checkpoint_proposal = None
                if self._budget_error == "overload_budget_save_pending":
                    self._budget_error = self._budget_error_type = None
                    self._budget_retry_at = 0.
            else:
                self._meter_checkpoint_proposal = None
        if decision is None:
            decision = self.regulator.evaluate(inp)
        if self._protective_zero_pending and self.settings.active_control and not self.settings.dry_run:
            self.regulator.reject_unsubmitted(decision, self.hass.loop.time())
            if self.settings.meter_envelope_control:
                decision = self.regulator.meter_envelope_fault(inp, ElectricalStopCause.UNKNOWN_DELIVERY)
            else:
                self.regulator.interrupt_positive_control("protective_zero_preempts_positive")
                decision = replace(decision, command="set_limit", current_p1=0, current_p2=0, current_p3=0,
                                   should_call_service=True, mode="command_protection",
                                   reason="A previous SDK request was superseded or has unverified delivery; confirming 0 A before a new offer.")
        if decision.current_refresh_required:
            self._request_current_snapshot()
        exposure = self.regulator.export_overload_state()
        normal_control = exposure.get("normal_control", {}) if not self.settings.meter_envelope_control else {}
        normal_active = bool(not self.settings.meter_envelope_control and (
                             normal_control.get("cap") is not None
                             or normal_control.get("response_pending")
                             or normal_control.get("alignment_pending")
                             or normal_control.get("stops")
                             or normal_control.get("response_failure")))
        if normal_active:
            self._normal_control_checkpoint_pending = True
        if (self._budget_error != "overload_budget_restore_failed"
                and (self.settings.overload_enabled or exposure["used_seconds"] or exposure["failure_reason"]
                     or exposure.get("trial_epoch") is not None
                     or exposure.get("automatic_cycle") is not None
                     or exposure.get("meter_control") is not None
                     or self._normal_control_checkpoint_pending)
                and self.hass.loop.time() >= self._budget_retry_at):
            try:
                await asyncio.wait_for(self._budget_store.async_save(exposure), EXPOSURE_STORE_TIMEOUT_SECONDS)
                if not self._budget_store.state_is_durable(self.regulator.export_overload_state()):
                    raise RuntimeError("Latest conservative exposure checkpoint is not verified")
            except Exception as err:
                pending = isinstance(err, TimeoutError) and self._budget_store.verification_pending
                if pending and self._budget_error in {None, "overload_budget_save_pending"}:
                    self._budget_error = "overload_budget_save_pending"
                elif self._budget_error != "overload_budget_restore_failed":
                    self._budget_error = "overload_budget_save_failed"
                self._budget_error_type = type(err).__name__
                self._budget_retry_at = self.hass.loop.time() + EXPOSURE_STORE_RETRY_SECONDS
                if pending:
                    if (self.settings.meter_envelope_control and decision.current_p1 > 0
                            and decision.command == "set_limit" and decision.should_call_service
                            and not decision.dry_run):
                        self._meter_checkpoint_proposal = {
                            "generation": generation, "input": inp, "decision": decision,
                            "exposure": deepcopy(exposure),
                            "fingerprint": self._budget_store.fingerprint(exposure),
                        }
                    self._watch_pending_checkpoint()
                    _LOGGER.warning("1FuseLoader exposure verification is still pending; timed overload remains blocked")
                else:
                    _LOGGER.warning("1FuseLoader exposure save failed (%s); timed overload remains blocked",
                                    self._budget_error_type, exc_info=True)
            else:
                self._normal_control_checkpoint_pending = normal_active
                if self._budget_error in {"overload_budget_save_failed", "overload_budget_save_pending"}:
                    # This tick was already evaluated conservatively with the
                    # storage fault. Only a subsequent fresh evaluation can
                    # offer current; no timers, exposure or safety latch reset.
                    self._budget_error = None
                    self._budget_error_type = None
                    _LOGGER.info("1FuseLoader latest exposure checkpoint verified; storage fault recovered")
                self._budget_retry_at = 0.0
        if self._stopped or generation != self._generation:
            return
        if (self._budget_error == "overload_budget_save_pending"
                and self._budget_store.verification_pending and decision.current_p1 > 0):
            retained = self._committed_normal_retention(self._read_input(), decision)
            if retained is not None:
                # Evaluation may have proposed a first lower cap. It is not
                # accepted authority until an actual dispatch and ACK exist.
                if self.regulator.reject_unsubmitted(decision, self.hass.loop.time()):
                    self._normal_control_checkpoint_pending = True
                    self._budget_retry_at = 0.
                decision = retained
            elif (self.settings.meter_envelope_control and not decision.meter_overload_authority
                    and (self.regulator.last_target is None or self.regulator.last_target[0] <= 0)):
                # A cold normal proposal has not acquired a lease. Waiting for
                # ordinary metadata I/O grants no positive permission and does
                # not justify sending an otherwise unnecessary zero either.
                latest = self._read_input()
                latest_plan = self.regulator.evaluate(latest)
                if latest_plan.meter_zero_cause is None and not latest_plan.meter_overload_authority:
                    self.regulator.reject_unsubmitted(decision, self.hass.loop.time())
                    self.last_decision = replace(latest_plan, mode="meter_wait_storage",
                        meter_state="meter_wait_storage", current_p1=0, current_p2=0, current_p3=0,
                        command=None, service=None, should_call_service=False,
                        meter_normal_retention=False,
                        reason="Waiting for normal metadata verification before the first positive offer; no command or lease has been issued.")
                    self.last_service_result = "not_sent_storage_pending"
                    self._schedule_overload_deadline(self.last_decision)
                    self.notify()
                    return
                decision, inp = latest_plan, latest
        previous_offer = self.regulator.last_target
        if (decision.mode == "normal" and decision.command == "set_limit"
                and previous_offer is not None and 0 < previous_offer[0] < decision.current_p1):
            retained = replace(decision, current_p1=previous_offer[0], current_p2=0, current_p3=0,
                command=None, service=None, should_call_service=False,
                reason="Exposure verification is pending; retaining the existing normal offer without the proposed increase.")
            if self._pending_checkpoint_allows_normal(retained, inp):
                decision = retained
        pending_normal = self._pending_checkpoint_allows_normal(decision, inp)
        checkpoint_unverified = bool(self._budget_error and not pending_normal or (
            (self.settings.meter_envelope_control or self.settings.automatic_overload_cycles)
            and self.regulator.automatic_cycle_requires_checkpoint
            and not self._budget_store.state_is_durable(self.regulator.export_overload_state())
            and not (self.settings.meter_envelope_control and pending_normal
                     and not decision.meter_overload_authority)
        ))
        if decision.current_p1 > 0 and (
            checkpoint_unverified or (
                self.settings.overload_enabled and (
                    decision.meter_overload_authority if self.settings.meter_envelope_control
                    else decision.mode.removeprefix("dry_run_").startswith("overload"))
                and (decision.overload_remaining_seconds < OVERLOAD_LEASE_RESERVE_SECONDS
                     or (not self.settings.meter_envelope_control
                         and self._overload_restore_receipt_floor is not None and (
                         inp.grid_report_ts is None or inp.charger_report_ts is None
                         or inp.grid_report_ts < self._overload_restore_receipt_floor
                         or inp.charger_report_ts < self._overload_restore_receipt_floor
                         or (inp.charger_event_source_known and (
                             inp.charger_event_source_ts is None
                             or inp.charger_event_source_ts < self._overload_restore_receipt_floor
                         ))
                     )))
            )
        ):
            if self.regulator.reject_unsubmitted(decision, self.hass.loop.time()):
                self._normal_control_checkpoint_pending = True
            if checkpoint_unverified:
                if not self.settings.meter_envelope_control:
                    self.regulator.interrupt_positive_control("checkpoint_unverified_before_positive")
            if self.settings.meter_envelope_control:
                cause = (ElectricalStopCause.PERSISTENCE if checkpoint_unverified
                    else ElectricalStopCause.OVERLOAD_LEASE_RESERVE
                    if decision.overload_remaining_seconds < OVERLOAD_LEASE_RESERVE_SECONDS
                    else ElectricalStopCause.ACTUATOR_INVALID)
                decision = self.regulator.meter_envelope_fault(inp, cause)
            else:
                decision = replace(
                    decision, mode="overload_control_blocked",
                    reason=self._budget_error or "Overload requires a complete lease reserve and fresh post-restart reports.",
                    command="set_limit", current_p1=0, current_p2=0, current_p3=0,
                    should_call_service=self.settings.active_control,
                )
        elif pending_normal:
            decision = replace(decision, reason=decision.reason +
                " Exposure verification is pending; only this independently validated normal offer is permitted.")
            retaining_ready = (decision.mode.removeprefix("dry_run_") == "disconnected_ready")
            retain_without_renewal = (
                self.regulator.can_retain_readiness_offer(self._read_input(), decision) if retaining_ready
                else self.regulator.last_target is not None
                    and decision.current_p1 == self.regulator.last_target[0])
            if retain_without_renewal:
                # Retention may not extend an old lease while verification waits.
                decision = replace(decision, command=None, service=None, should_call_service=False)
        if (decision.mode.removeprefix("dry_run_") in {
                "normal_shedding_sync", "normal_shedding_grid_wait", "overload_grid_wait", "low_grid_grid_wait",
                "ordinary_grid_wait", "ordinary_response_hold", "startup_grid_wait", "overload_peak_hold", "positive_ramp_hold", "readiness_wait", "normal_continuity", "waiting_for_current_cohort"}
                and decision.current_p1 > 0):
            # Synchronization retains only an existing live offer. It cannot
            # write, renew a lease, or resume, even if a stale plan says so.
            decision = replace(decision, command=None, service=None, should_call_service=False)
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
            await self._checkpoint_unsent_cleanup(decision)
            self.last_service_result = "blocked"
            self.last_decision = replace(
                decision, mode="control_blocked", reason=self.control_block_reason or "No verified command adapter.",
                should_call_service=False,
            )
            self.notify()
            return
        positive_submission = decision.command == "resume" or (
            decision.command == "set_limit" and decision.current_p1 > 0)
        if (positive_submission and self.hass.loop.time() < self._local_retry_after
                and getattr(decision, "command_urgency", "normal") != "protective_reduction"):
            retained = self._committed_normal_retention(self._read_input(), decision)
            await self._checkpoint_unsent_cleanup(decision)
            self.last_decision = retained if retained is not None else replace(decision,
                mode="local_submission_deferred", command=None, service=None, should_call_service=False,
                reason="Local admission is deferred; no remote command was attempted.")
            self._schedule_overload_deadline(self.last_decision)
            self.last_service_result = "not_sent_local_deferred"
            self.notify()
            return
        readiness_context = getattr(decision, "readiness_context", None)
        readiness_deadline = (_finite_current_number(getattr(readiness_context, "deadline", None))
            if decision.mode.removeprefix("dry_run_") in {"disconnected_ready", "connected_ready"} else None)
        if (positive_submission and readiness_deadline is not None
                and self.hass.loop.time() >= readiness_deadline):
            await self._checkpoint_unsent_cleanup(decision)
            self.last_service_result = "not_sent"
            self.last_decision = replace(decision, mode="waiting_for_latest_sources",
                reason="The readiness proposal expired before dispatch; recalculating from current sources.",
                should_call_service=False)
            self._handle_source_change(None)
            self.notify()
            return
        raw_response_deadline = getattr(decision, "raw_budget_response_deadline_ts", None)
        if (positive_submission and decision.raw_budget_rise_context is not None
                and raw_response_deadline is not None
                and self.hass.loop.time() >= raw_response_deadline):
            # This proposed rise has not been sent. Discard it without turning
            # a delayed checkpoint into either fresh authority or a new zero.
            await self._checkpoint_unsent_cleanup(decision)
            self.last_service_result = "not_sent"
            self.last_decision = replace(decision, mode="waiting_for_latest_sources",
                reason="The raw-grid proposal expired before dispatch; recalculating from current sources.",
                should_call_service=False)
            self._handle_source_change(None)
            self.notify()
            return
        response_deadline = decision.nominal_response_deadline_ts
        if (positive_submission and response_deadline is not None
                and self.hass.loop.time() >= response_deadline):
            # Checkpoint verification may have consumed the entire window.
            # Re-evaluate to record the failure before issuing protection;
            # an old positive plan must never acquire a fresh dispatch window.
            latest = self._read_input()
            decision = self.regulator.evaluate(latest)
            inp = latest
            plan_source_diagnostics = dict(self.source_diagnostics)
            exposure = self.regulator.export_overload_state()
            if self.settings.meter_envelope_control and (decision.current_p1 > 0 or decision.command == "resume"):
                # Only the electrical supervisor can request a zero. Expiry
                # invalidates this old dispatch plan, not a newly evaluated
                # meter-safe offer. Recalculate it outside the expired request.
                self.last_decision = replace(decision, command=None, service=None,
                    should_call_service=False,
                    reason="The previous electrical dispatch deadline passed; recalculating from the current electrical supervisor.")
                self.last_service_result = "not_sent"
                self._schedule_overload_deadline(self.last_decision)
                self._handle_source_change(None)
                self.notify()
                return
            sync_hold = bool(decision.mode.removeprefix("dry_run_") == "normal_shedding_sync"
                and decision.command is None and not decision.should_call_service
                and decision.nominal_sync_deadline_ts is not None
                and self.hass.loop.time() < decision.nominal_sync_deadline_ts)
            grid_hold = bool(decision.mode.removeprefix("dry_run_") in {
                    "normal_shedding_grid_wait", "overload_grid_wait", "low_grid_grid_wait", "ordinary_grid_wait", "startup_grid_wait"}
                and decision.command is None and not decision.should_call_service
                and decision.grid_confirmation_deadline_ts is not None
                and self.hass.loop.time() < decision.grid_confirmation_deadline_ts
                and self.regulator.last_target is not None
                and decision.current_p1 == self.regulator.last_target[0] > 0)
            ordinary_hold = bool(decision.mode.removeprefix("dry_run_") == "ordinary_response_hold"
                and decision.command is None and not decision.should_call_service
                and decision.ordinary_response_deadline_ts is not None
                and self.hass.loop.time() < decision.ordinary_response_deadline_ts
                and self.regulator.last_target is not None
                and decision.current_p1 == self.regulator.last_target[0] > 0)
            raw_hold = bool(decision.mode.removeprefix("dry_run_") == "low_grid_raw_hold"
                and not decision.should_call_service and decision.command in {None, "set_limit"}
                and decision.nominal_response_deadline_ts is None
                and self.regulator.last_target is not None
                and decision.current_p1 == self.regulator.last_target[0] > 0)
            ordinary_replan = bool(decision.mode.removeprefix("dry_run_") in {
                    "ordinary_lower_offer", "ordinary_lower_complete"}
                and latest.data_valid and not latest.data_error
                and decision.house_load_basis == "raw_bounded_ordinary_observation"
                and decision.grid_l1 is not None and decision.grid_l1 <= PEAK_RESPONSE_TRIGGER_AMPS
                and decision.grid_l1+decision.current_p1
                    <= self.settings.main_fuse_amps-self.settings.buffer_amps
                and decision.current_p1 > 0 and decision.nominal_response_deadline_ts is None)
            if ordinary_replan:
                # Discard the OLD expired electrical plan. The freshly bounded
                # ordinary decrease is evaluated again before a later actual
                # dispatch; it neither reopens the peak timer nor needs a zero.
                decision = replace(decision, command=None, service=None,
                    should_call_service=False,
                    reason="The old peak dispatch expired; recalculating the independently raw-bounded ordinary decrease without an electrical countdown.")
            # The re-evaluation can retire the old response deadline using a
            # fresh, whole-authority raw bound. Retain that existing offer
            # without dispatching the old request or manufacturing a zero.
            if (decision.current_p1 > 0 or decision.command == "resume") and not (sync_hold or raw_hold or grid_hold or ordinary_hold or ordinary_replan):
                decision = replace(
                    decision, mode="normal_shedding_stop",
                    reason="The fixed nominal response deadline expired before dispatch; requesting 0 A.",
                    command="set_limit", current_p1=0, current_p2=0, current_p3=0,
                    should_call_service=self.settings.active_control,
                )
            positive_submission = False
            self.last_decision = decision
            self._schedule_overload_deadline(decision)
            self._handle_source_change(None)
            if not decision.should_call_service or decision.command is None:
                self.last_service_result = "not_sent"
                self.notify()
                return
        if positive_submission:
            # A checkpoint write above can yield to newer telemetry. Never
            # execute any stale positive offer, including a reduction or a
            # lease renewal. A new hard load/fault can require zero instead.
            latest = self._read_input()
            if self.settings.overload_enabled or self.settings.meter_envelope_control:
                problem = self._runtime_offline_problem(latest)
                pending_safe = self._pending_checkpoint_allows_normal(decision, latest)
                budget_fault = self._budget_error if not pending_safe else None
                if problem or budget_fault:
                    latest = self._critical_runtime_input(latest, budget_fault or problem)
            if self._input_revision(latest) != self._input_revision(inp):
                if (self.settings.meter_envelope_control
                        and self.regulator.can_continue_meter_dispatch(inp, latest, decision)):
                    inp = latest
                    plan_source_diagnostics = dict(self.source_diagnostics)
                else:
                    await self._checkpoint_unsent_cleanup(decision)
                    self.last_service_result = "not_sent"
                    self.last_decision = replace(
                        decision, mode="waiting_for_latest_sources",
                        reason="New telemetry arrived during checkpoint verification; recalculating before a positive offer.",
                        should_call_service=False,
                    )
                    self._handle_source_change(None)
                    self.notify()
                    return
        evidence = self._zero_readback_evidence(inp)
        self._delivery.observe(evidence["phases"], evidence["after_submission"])
        latest_receipt = self._delivery.latest
        confirmed_zero = self._delivery.can_skip_zero(**evidence)
        # Unchanged trusted EV=0 is allowed for command ordering only. Requiring
        # a newly changed EV event here could deadlock a car already at zero.
        # A fresh native cap event and matching delivery (when correlatable)
        # still precede a new offer. This makes no physical-response claim.
        ordered_zero = self._zero_ordering_confirmed(inp)
        if (decision.command == "set_limit" and decision.current_p1 == 0 and confirmed_zero):
            # No API call and no acknowledge: a cached zero must never renew
            # a lease, a response timer, or a thermal allowance.
            self._protective_zero_pending = False
            self.last_service_result = "not_sent_confirmed_zero"
            self.last_service_error = None
            self.notify()
            return
        zero_barrier = bool(latest_receipt and latest_receipt.phases == (0, 0, 0)
                            and (latest_receipt.http_status is not None or latest_receipt.state in {
                                "submission_failed_unverified", "superseded_unverified", "vendor_rejected"})
                            and not ordered_zero)
        zero_barrier = zero_barrier or self._delivery.unresolved_correlated_zero
        if positive_submission and (zero_barrier or self.hass.loop.time() < self._positive_retry_after):
            await self._checkpoint_unsent_cleanup(decision)
            self.last_decision = replace(decision, should_call_service=False, mode="waiting_for_zero_confirmation",
                reason="Waiting for the preceding 0 A command and a fresh native zero transition before offering current.")
            self.last_service_result = "not_sent_waiting_for_zero_confirmation"
            self._request_current_snapshot()
            self.notify()
            return
        attempt = receipt_record = None
        capture_stop = False

        def actual_dispatch() -> bool:
            # The adapter calls synchronously after local admission, directly
            # before SDK entry. A proposal/checkpoint/queue is not a submission.
            nonlocal attempt, receipt_record, capture_stop, inp, plan_source_diagnostics, exposure
            if (self._dispatch_started or self._stopped or generation != self._generation
                    or self._dispatch_superseded):
                return False
            latest = self._read_input()
            if decision.meter_quiet_normal_retention:
                if (self._protective_zero_pending or self._delivery.unresolved_correlated_zero
                        or self._budget_error not in {None,"overload_budget_save_pending"}
                        or self._runtime_offline_problem(latest)
                        or not self.regulator.can_continue_meter_dispatch(inp,latest,decision)):
                    return False
            if positive_submission and self._input_revision(latest) != self._input_revision(inp):
                ready = decision.mode.removeprefix("dry_run_") == "disconnected_ready"
                if self.settings.meter_envelope_control:
                    if not self.regulator.can_continue_meter_dispatch(inp, latest, decision):
                        return False
                elif not ready or not self.regulator.can_continue_readiness_dispatch(inp, latest, decision):
                    return False
            if (self.settings.meter_envelope_control and positive_submission
                    and decision.meter_overload_authority
                    and not self.regulator.meter_overload_admitted(latest, decision)):
                # The standalone episode owns post-restore admission. A local
                # refusal is unsent work, not a new electrical zero or an
                # alternate legacy aggregate-current response deadline.
                return False
            if not self.regulator.begin_submission(decision, self.hass.loop.time(), inp=latest):
                return False
            self._dispatch_started = True
            inp = latest
            self._dispatch_input = latest
            plan_source_diagnostics = dict(self.source_diagnostics)
            exposure = self.regulator.export_overload_state()
            attempt = {
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "completed_at_utc": None, "command": decision.command,
                "target_p1": decision.current_p1, "target_p2": decision.current_p2,
                "target_p3": decision.current_p3, "ttl_seconds": decision.ttl_seconds,
                "result": "pending", "error": None,
            }
            self.last_command_attempt = attempt
            capture_stop = (decision.command == "pause" or (
                decision.command == "set_limit" and decision.current_p1 == 0)) and self._stop_context_armed
            if capture_stop:
                def number(value: Any) -> float | None:
                    try:
                        parsed = float(value)
                        return parsed if not isinstance(value, bool) and isfinite(parsed) else None
                    except (TypeError, ValueError, OverflowError):
                        return None

                def report_utc(value: Any) -> str | None:
                    parsed = number(value)
                    try:
                        return datetime.fromtimestamp(parsed, timezone.utc).isoformat() if parsed is not None else None
                    except (ValueError, OverflowError, OSError):
                        return None

                # Keep the first request's cause through subsequent inconsistent
                # feedback/retries. A newly accepted positive offer arms a new
                # incident. These are diagnostic receipts, never physical proof.
                preceding_receipts = [_limit_receipt_summary(item) for item in
                    [item for item in self._recent_commands if item.get("command") in {"set_limit", "resume"}
                     and type(item.get("target_p1")) in (int, float) and item["target_p1"] > 0][-4:]]
                delivery_by_sequence = {record.sequence: record.public() for record in self._delivery.records}
                for item in preceding_receipts:
                    sequence = item.get("delivery", {}).get("sequence")
                    if sequence in delivery_by_sequence:
                        item["delivery"] = _limit_receipt_summary({"delivery": delivery_by_sequence[sequence]})["delivery"]
                self.last_stop_context = {
                    "plan_utc": report_utc(inp.wall_now_ts),
                    "reason": decision.reason, "mode": decision.mode,
                    "grid_l1": number(inp.grid_l1),
                    "charger_current": number(inp.charger_current), "status": inp.status,
                    "grid_report_utc": report_utc(inp.grid_report_ts),
                    "charger_report_utc": report_utc(inp.charger_report_ts),
                    "charger_report_family": inp.charger_report_family,
                    "charger_max_observation_ids": plan_source_diagnostics.get("effective_current_max_observation_ids"),
                    "source_error": inp.data_error,
                    "product_update_received_at_utc": plan_source_diagnostics.get("easee_stream_pulse_utc"),
                    "previous_submitted_limit_p1": (self.last_accepted_limit.get("target_p1")
                                                    if self.last_accepted_limit else None),
                    "preceding_positive_limit_receipts": preceding_receipts,
                    "overload_used_seconds": exposure["used_seconds"],
                    "overload_failure_reason": exposure["failure_reason"],
                    "overload_effective_used_seconds": self.regulator.effective_overload_used_seconds,
                    "overload_effective_failure_reason": self.regulator.effective_overload_failure_reason,
                    **{key: value for key, value in self.regulator.trial_diagnostics().items()
                       if key in {"overload_trial_id", "overload_trial_state",
                                  "overload_trial_used_seconds", "overload_trial_remaining_seconds",
                                  "overload_trial_failure_reason", "overload_lifetime_used_seconds",
                                  "overload_historical_failure_reason"}},
                    "command_started_at_utc": attempt["started_at_utc"],
                    "command_completed_at_utc": None,
                    "command_result": "pending", "command_error": None,
                    "clock_scope": "validated_grid_sample_and_local_HA_receipts_not_physical_stop_proof",
                }
                self.last_stop_nominal_diagnostics = {
                    "plan_utc": self.last_stop_context["plan_utc"],
                    "nominal_rejection_code": decision.nominal_rejection_code,
                    "source_report_skew_seconds": decision.source_report_skew_seconds,
                    "grid_report_age_seconds": decision.grid_report_age_seconds,
                    "charger_report_age_seconds": decision.charger_report_age_seconds,
                    "source_alignment_window_seconds": decision.source_alignment_window_seconds,
                    "source_max_age_seconds": self.settings.max_sensor_age_seconds,
                }
                self._stop_context_armed = False
            receipt_record = self._delivery.begin(decision.command,
                (decision.current_p1, decision.current_p2, decision.current_p3), self.hass.loop.time())
            return True

        self.last_service_error = None
        self._dispatch_input = inp
        self._dispatch_decision = decision
        self._dispatch_positive = positive_submission
        self._dispatch_started = False
        self._dispatch_superseded = False
        try:
            action_timeout = ACTION_TIMEOUT_SECONDS
            if positive_submission and readiness_deadline is not None:
                action_timeout = min(action_timeout, max(0.001, readiness_deadline - self.hass.loop.time()))
            if positive_submission and response_deadline is not None:
                action_timeout = min(action_timeout, max(0.001, response_deadline - self.hass.loop.time()))
            if positive_submission and raw_response_deadline is not None:
                action_timeout = min(action_timeout, max(0.001, raw_response_deadline - self.hass.loop.time()))
            execute = getattr(self._adapter, "async_execute_with_dispatch", None)
            if callable(execute):
                coroutine = execute(decision, actual_dispatch)
            else:
                # Older offline adapters have no preflight queue. Keep their
                # entry boundary explicit rather than inventing an early ACK.
                if not actual_dispatch():
                    raise CommandNotSent("dispatch_context_rejected", retry_after_seconds=1.)
                coroutine = self._adapter.async_execute(decision)
            self._dispatch_task = self.hass.async_create_task(coroutine)
            receipt = await asyncio.wait_for(self._dispatch_task, action_timeout)
            if not self._dispatch_started or receipt_record is None:
                raise RuntimeError("adapter_completed_without_dispatch_boundary")
            if self._dispatch_superseded or self._stopped or generation != self._generation:
                raise RuntimeError("superseded_command_completion")
        except asyncio.CancelledError:
            if not self._dispatch_superseded:
                raise
            if not self._dispatch_started:
                await self._checkpoint_unsent_cleanup(decision)
                self.last_service_result = "not_sent_superseded"
            else:
                self._delivery.failed(receipt_record, superseded=True)
                self.regulator.reject_submission(decision, self.hass.loop.time())
                self.last_service_result = "superseded_unverified"
                attempt.update(result="superseded_unverified", error="source_changed_during_dispatch")
                self.last_service_error = "source_changed_during_dispatch"
            self._handle_source_change(None)
        except Exception as err:
            if isinstance(err, CommandNotSent) and not self._dispatch_started:
                # Only the typed pre-SDK contract proves no command escaped.
                # Do not create delivery records, ACKs or retry attempts.
                await self._checkpoint_unsent_cleanup(decision)
                self.last_service_result = "not_sent_local"
                self.last_service_error = None
                self._transport_events.append({
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                    "kind": "known_not_sent", "code": err.code,
                    "command": decision.command, "target_p1": decision.current_p1,
                    "retry_after_seconds": err.retry_after_seconds,
                    "scope": "local_pre_SDK_no_remote_submission_or_ACK",
                })
                self._defer_local_submission(err.retry_after_seconds)
                latest = self._read_input()
                retained = self._committed_normal_retention(latest, decision)
                self.last_decision = retained if retained is not None else replace(decision,
                    command=None, service=None, should_call_service=False,
                    reason="Local preflight refused the request before SDK entry; recalculation is deferred.")
                self._schedule_overload_deadline(self.last_decision)
                self._queue_incident_journal()
                if positive_submission and (not latest.data_valid or latest.data_error):
                    # A proven local refusal removes transport ambiguity; it
                    # does not postpone a separate source-fault zero.
                    self._handle_source_change(None)
            else:
                # An adapter which fails without honoring the boundary, or
                # fails after it, provides no proof of non-delivery.
                if receipt_record is None:
                    attempt = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                        "completed_at_utc": None, "command": decision.command,
                        "target_p1": decision.current_p1, "target_p2": decision.current_p2,
                        "target_p3": decision.current_p3, "ttl_seconds": decision.ttl_seconds,
                        "result": "pending", "error": None,
                        "boundary_scope": "adapter_contract_unknown"}
                    self.last_command_attempt = attempt
                    receipt_record = self._delivery.begin(decision.command,
                        (decision.current_p1, decision.current_p2, decision.current_p3), self.hass.loop.time())
                self._delivery.failed(receipt_record, superseded=self._dispatch_superseded)
                if positive_submission or decision.command == "set_limit" and decision.current_p1 == 0:
                    self._protective_zero_pending = True
                rejected_retry = self.regulator.reject_submission(decision, self.hass.loop.time())
                self.last_service_result = "error"
                self.last_service_error = type(err).__name__
                attempt.update(result="error", error=self.last_service_error)
                self.last_decision = replace(decision,
                    reason=f"Command submission failed ({type(err).__name__}); charger state is unverified.")
                _LOGGER.warning("1FuseLoader command submission failed (%s)", type(err).__name__)
                if rejected_retry or self._dispatch_superseded or positive_submission:
                    self._handle_source_change(None)
        else:
            self._delivery.accepted(receipt_record, receipt, self.hass.loop.time())
            self.regulator.acknowledge(decision, self.hass.loop.time())
            self._cancel_local_retry()
            if decision.command == "set_limit" and decision.current_p1 == 0:
                self._protective_zero_pending = False
            self.last_service_result = "submitted_unverified"
            attempt["result"] = "submitted_unverified"
        finally:
            self._dispatch_task = None
            self._dispatch_input = None
            self._dispatch_decision = None
            self._dispatch_positive = False
            self._dispatch_started = False
        if attempt is None or receipt_record is None:
            self.notify()
            return
        attempt["delivery"] = receipt_record.public()
        attempt["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        journal_record = {
            **attempt, "mode": decision.mode, "reason": decision.reason,
            "grid_l1": decision.grid_l1, "charger_current": decision.charger_current,
            "grid_report_utc": plan_source_diagnostics.get("grid_reported_at_utc"),
            "charger_report_utc": plan_source_diagnostics.get("effective_charger_reported_at_utc"),
            "charger_clock_scope": plan_source_diagnostics.get("effective_current_clock_scope"),
            "charger_report_family": inp.charger_report_family,
            "charger_max_observation_ids": plan_source_diagnostics.get("effective_current_max_observation_ids"),
            "snapshot_source_timestamp_utc": plan_source_diagnostics.get("charger_snapshot_current_source_timestamp_utc"),
            "reported_cap_p1": plan_source_diagnostics.get("reported_circuit_p1"),
        }
        self._recent_commands.append(journal_record)
        if attempt["result"] == "submitted_unverified":
            self.last_accepted_command = dict(attempt)
            if decision.command in {"set_limit", "resume"}:
                self.last_accepted_limit = dict(attempt)
            if positive_submission:
                self._stop_context_armed = True
                # API acceptance stays accurately recorded even if sources
                # changed while awaiting it. Immediately plan from new data;
                # do not wait for another periodic tick or replay this offer.
                latest = self._read_input()
                if self.settings.overload_enabled or self.settings.meter_envelope_control:
                    problem = self._runtime_offline_problem(latest)
                    budget_fault = (self._budget_error
                        if self._budget_error != "overload_budget_save_pending" else None)
                    if problem or budget_fault:
                        latest = self._critical_runtime_input(latest, budget_fault or problem)
                if (self._input_revision(latest) != self._input_revision(inp)
                        or response_deadline is not None or raw_response_deadline is not None):
                    self._handle_source_change(None)
        if capture_stop and self.last_stop_context is not None:
            self.last_stop_context.update({
                "command_completed_at_utc": attempt["completed_at_utc"],
                "command_result": attempt["result"], "command_error": attempt["error"],
            })
            self._recent_stop_contexts.append({**self.last_stop_context,
                "decision_diagnostics": {
                    **private_plan_context,
                    "house_load_basis": decision.house_load_basis,
                    "house_load_l1": _finite_current_number(decision.house_load_l1),
                    "native_committed": inp.charger_native_committed is True,
                    "snapshot_used": inp.charger_snapshot_verified is True,
                    **({"meter_control_after": self.regulator.meter_control_diagnostics()}
                       if self.settings.meter_envelope_control else {
                           "established_current_after": _finite_current_number(self.regulator._ev_established_value),
                           "established_value_match_after": self.regulator._ev_established_value is not None
                               and inp.charger_current == self.regulator._ev_established_value,
                           "established_binding_match_after": self.regulator._ev_established_binding is not None
                               and inp.control_source_id == self.regulator._ev_established_binding,
                       }),
                }})
            self._queue_incident_journal()
        self.notify()

    def _queue_incident_journal(self) -> None:
        """Persist bounded diagnostic evidence off the protective command path.

        This journal has no control authority. A failed diagnostic write never
        changes exposure accounting, cap ownership or retry policy.
        """
        self._incident_journal_writer["revision"] += 1
        revision = self._incident_journal_writer["revision"]
        self._incident_journal_pending = {
            "revision": revision,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "API_and_local_receipts_not_physical_acquisition_or_delivery_proof",
            "commands": [dict(item) for item in self._recent_commands],
            "stops": [dict(item) for item in self._recent_stop_contexts],
            "native_events": [dict(item) for item in self._native_telemetry_events],
            "native_latest_diagnostics": self._passive_diagnostic_context(),
            "transport_events": [dict(item) for item in self._transport_events],
            "pilot_zero_observations": [dict(item) for item in self._pilot_zero_observations],
        }
        if self._incident_journal_task is not None and not self._incident_journal_task.done():
            return

        async def save() -> None:
            try:
                store = Store(self.hass, 1, f"{DOMAIN}.{self.entry.entry_id}.incident_journal", atomic_writes=True)
                while self._incident_journal_pending is not None:
                    payload = self._incident_journal_pending
                    self._incident_journal_pending = None
                    try:
                        async with self._incident_journal_writer["lock"]:
                            if payload["revision"] != self._incident_journal_writer["revision"]:
                                continue
                            # Keep the actual I/O serialized across reloads.
                            # A timeout must not release the lock while an
                            # executor-backed old write can still finish last.
                            write = self.hass.async_create_task(store.async_save(payload))
                            try:
                                try:
                                    await asyncio.wait_for(asyncio.shield(write), EXPOSURE_STORE_TIMEOUT_SECONDS)
                                except TimeoutError:
                                    self._incident_journal_error = "TimeoutError"
                                    _LOGGER.warning("1FuseLoader diagnostic journal write is still pending")
                                    await asyncio.shield(write)
                            except asyncio.CancelledError:
                                # Cancellation of this diagnostic worker must
                                # not free the entry lock while old I/O lives.
                                while not write.done():
                                    try:
                                        await asyncio.shield(write)
                                    except asyncio.CancelledError:
                                        continue
                                    except Exception:
                                        break
                                if not write.cancelled():
                                    write.exception()  # retrieve any completed I/O failure
                                raise
                    except Exception as err:
                        self._incident_journal_error = type(err).__name__
                        _LOGGER.warning("1FuseLoader diagnostic journal write failed (%s)", type(err).__name__)
                    else:
                        self._incident_journal_error = None
            finally:
                if self._incident_journal_task is asyncio.current_task():
                    self._incident_journal_task = None

        self._incident_journal_task = self.hass.async_create_task(save())

    def _schedule_overload_deadline(self, decision: Any, elapsed: float = 0.0) -> None:
        self._deadline_token = None
        if self._unsub_deadline:
            self._unsub_deadline()
            self._unsub_deadline = None
        if self._stopped:
            return
        delays = []
        if self.settings.overload_enabled and decision.overload_transition_remaining_seconds is not None:
            delays.append(decision.overload_transition_remaining_seconds)
        if decision.current_p1 > 0 and decision.nominal_response_deadline_ts is not None:
            # Absolute monotonic time includes source age and every await.
            delays.append(decision.nominal_response_deadline_ts - self.hass.loop.time() + elapsed)
        sync_deadline = getattr(decision, "nominal_sync_deadline_ts", None)
        if decision.current_p1 > 0 and sync_deadline is not None:
            # Separate, nonrenewable measurement synchronization; the original
            # high-load response allowance is never extended by this timer.
            delays.append(sync_deadline - self.hass.loop.time() + elapsed)
        for name in ("raw_budget_response_deadline_ts", "source_processing_deadline_ts", "grid_confirmation_deadline_ts", "ordinary_response_deadline_ts", "positive_response_deadline_ts", "readiness_deadline_ts", "normal_continuity_deadline_ts"):
            deadline = getattr(decision, name, None)
            if decision.current_p1 > 0 and deadline is not None:
                # Fixed source/response clocks survive event churn and every
                # await; neither timer renews a limit or adds electrical time.
                delays.append(deadline - self.hass.loop.time() + elapsed)
        if (self.settings.overload_enabled and (
                decision.meter_overload_authority if self.settings.meter_envelope_control
                else decision.mode.removeprefix("dry_run_").startswith("overload"))
                and decision.current_p1 > 0):
            delays.append(decision.overload_remaining_seconds - OVERLOAD_LEASE_RESERVE_SECONDS)
        if not delays:
            return
        token = self._deadline_token = object()
        generation = self._generation

        async def deadline_tick(_now: datetime) -> None:
            if (self._stopped or generation != self._generation
                    or token is not self._deadline_token):
                return
            self._deadline_token = None
            # The event loop and remote command still have delivery latency;
            # this removes the avoidable five-second watchdog rounding only.
            self._handle_source_change(None)

        # A near-deadline source event must not replace the fixed post-response
        # expiry with another 50 ms on every update.
        minimum_delay = 0.0 if (
            getattr(decision, "grid_confirmation_deadline_ts", None) is not None
            or getattr(decision, "ordinary_response_deadline_ts", None) is not None
            or getattr(decision, "normal_continuity_deadline_ts", None) is not None) else 0.05
        self._unsub_deadline = async_call_later(self.hass, max(minimum_delay, min(delays) - elapsed), deadline_tick)


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
