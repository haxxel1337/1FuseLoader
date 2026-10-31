"""Narrow compatibility adapter for the audited Easee/pyeasee versions.

Easee 0.9.74 drops identical service calls before they reach pyeasee, so its
services cannot renew a finite circuit TTL. Reuse its authenticated public
pyeasee objects instead. Never read, copy, log, or persist their credentials.
An HTTP acceptance is submission to the cloud, not physical charger feedback.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from math import ceil, isfinite
import re
import sys
import time
import weakref
from typing import Any

_monotonic = time.monotonic

SUPPORTED_EASEE_VERSION = "0.9.74"
SUPPORTED_PYEASEE_VERSION = "0.8.17"
SUPPORTED_PYEASEE_VERSIONS = frozenset({"0.8.17", "0.8.19"})
NONZERO_ADMISSION_LIMIT = 8
PROTECTIVE_NONZERO_ADMISSION_LIMIT = 11
ADMISSION_PERIOD_SECONDS = 60.01
REQUEST_TIMEOUT_SECONDS = 10
MAX_TELEMETRY_AGE_SECONDS = 120
OBSERVATION_FUTURE_TOLERANCE_SECONDS = 2
NATIVE_PUBLICATION_MAX_DIVERGENCE_SECONDS = 1.0
SNAPSHOT_OBSERVATION_IDS = (109, 182, 183, 184, 185)
PRIVATE_TELEMETRY_OBSERVATION_IDS = frozenset({21, 38, 48, 96, 100, 109, 110, 111, 112, 113, 114, 120, 182, 183, 184, 185})

# Raw numeric modes in the audited pyeasee 0.8.17 operation-mode enum.
# Keep terminal, fault and schedule states distinct from start-ready states.
CHARGER_OPERATION_MODES = {
    0: "offline",
    1: "disconnected",
    2: "awaiting_start",
    3: "charging",
    4: "completed",
    5: "error",
    6: "ready_to_charge",
    7: "awaiting_authorization",
    8: "de_authorizing",
    100: "start_charging",
    101: "stop_charging",
    102: "offline",
    103: "awaiting_load_balancing",
    104: "awaiting_authorization",
    105: "awaiting_smart_start",
    106: "awaiting_scheduled_start",
    107: "authenticating",
    108: "paused_due_to_equalizer",
    109: "searching_for_master",
    157: "erratic_ev",
    158: "error_temperature_too_high",
    159: "error_dead_powerboard",
    160: "error_overcurrent",
    161: "error_pen_fault",
}


class EaseeAdapterError(RuntimeError):
    """A safe, credential-free failure reported by the adapter."""


class CommandNotSent(EaseeAdapterError):
    """A local rejection before SDK dispatch; no remote command was attempted.

    This is deliberately distinct from an SDK exception or HTTP timeout. Once
    the SDK coroutine begins, its internal queue/HTTP side effects are unknown.
    """

    def __init__(self, code: str, *, retry_after_seconds: float = 0.) -> None:
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]{1,80}", code) is None:
            code = "local_preflight_rejected"
        if (type(retry_after_seconds) not in (int, float)
                or not isfinite(retry_after_seconds) or not 0 <= retry_after_seconds <= 3600):
            retry_after_seconds = 0.
        self.code = code
        self.retry_after_seconds = float(retry_after_seconds)
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CommandSubmission:
    """Cloud receipt only: neither delivery nor physical-current proof."""

    http_status: int
    requested_phases: tuple[int, int, int] | None
    request_started_monotonic: float
    request_completed_monotonic: float
    correlation_ticks: tuple[str, ...] = ()
    command_type: int | None = None
    accepted_at: datetime | None = None

    @property
    def correlation_ids(self) -> tuple[tuple[int, str], ...]:
        return tuple((self.command_type, tick) for tick in self.correlation_ticks) if self.command_type else ()


@dataclass(frozen=True, slots=True)
class CommandDelivery:
    """Allowlisted vendor delivery metadata; no identifiers or raw payload."""

    command_type: int
    ticks: str
    was_accepted: bool
    result_code: int | None
    provider_timestamp: str | None
    delivered_at: str | None
    received_monotonic: float


@dataclass(frozen=True, slots=True)
class NativeTelemetryObservation:
    """Small passive diagnostic event, never a current/control proof source."""

    observation_id: int
    value: float | str | None
    source_at_utc: str | None
    received_at_utc: str
    received_monotonic: float
    batch_sequence: int
    receiver_generation: int
    error: str | None = None
    origin: str = "unknown"

    def public(self) -> dict[str, Any]:
        # No mid, serial, site, account, headers or raw provider payload.
        return {"observation_id": self.observation_id, "value": self.value,
                "source_at_utc": self.source_at_utc, "received_at_utc": self.received_at_utc,
                "received_monotonic": self.received_monotonic,
                "batch_sequence": self.batch_sequence, "receiver_generation": self.receiver_generation,
                "error": self.error,
                "origin": (self.origin if self.origin == "signalr_product_update_callback" else "unknown"),
                "control_evidence": False,
                "scope": "callback_receipt_may_contain_cached_provider_data_not_physical_or_control_proof"}


@dataclass(frozen=True, slots=True)
class ChargerCurrentSnapshot:
    """Cloud values, local receipt and optional vendor observation metadata.

    Source timestamps describe the server's observations, not synchronized
    physical acquisition. A new GET receipt must never renew these timestamps.
    """

    current: float
    status: str
    received_at: datetime
    source_identity: tuple[int, ...] = field(repr=False)
    current_source_timestamp: datetime | None = None
    status_source_timestamp: datetime | None = None
    current_source_timestamps: tuple[tuple[int, datetime | None], ...] = ()
    source_timestamp_error: str | None = None
    # The maximum can move between terminals while every individual source
    # clock remains monotonic. Its selected timestamp is comparable only for
    # the same set of equally maximal observation IDs.
    current_source_observation_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeCurrentObservation:
    """Selected maximum's actual local terminal-event receipts, not HA writes."""

    current: float | None
    observation_ids: tuple[int, ...]
    received_at: datetime | None
    source_identity: tuple[int, ...] = field(repr=False)
    source_timestamps: tuple[tuple[int, datetime | None], ...] = ()
    observation_report_times: tuple[tuple[int, datetime], ...] = ()
    observation_sequences: tuple[tuple[int, int], ...] = ()
    observation_evidence_credit: tuple[tuple[int, bool], ...] = ()
    source_metadata_highwaters: tuple[tuple[int, datetime], ...] = ()
    error: str | None = None
    last_tuple_change_monotonic: float | None = None
    # Numeric state and event clocks are one committed raw ProductUpdate view.
    # Bootstrap values have no receipt/sequence proof. Live SDK publication is
    # a separate projection and cannot overwrite this observation's identity.
    committed: bool = False
    sdk_current: float | None = None
    native_publication_pending: bool = False
    divergence_started_monotonic: float | None = None
    publication_generation: int = 0


@dataclass(frozen=True, slots=True)
class NativeControlledL1Observation:
    """Input T3 evidence under a separately bound TN N+L1 output context.

    The neutral/MAX observation stays independent. A phase witness adopts a
    mapping epoch; it never creates a current receipt or response sample.
    """

    current: float | None
    received_at: datetime | None
    sequence: int
    evidence_credit: bool
    source_identity: tuple[int, ...] = field(repr=False)
    mapping_epoch: int
    output_phase: int | None
    mapping_adopted_at: datetime | None
    mapping_sequence: int
    source_at: datetime | None = None
    source_highwater_at: datetime | None = None
    mapping_source_at: datetime | None = None
    mapping_source_highwater_at: datetime | None = None
    error: str | None = None
    observation_id: int = 183
    committed: bool = False
    current_error: str | None = None


class EaseeCommandAdapter:
    """Version-gated access to the charger selected in the HA device registry."""

    def __init__(self, hass: Any, device_id: str) -> None:
        self.hass = hass
        self.device_id = device_id
        self._charger: Any = None
        self._circuit: Any = None
        self._product_data: Any = None
        self._controller: Any = None
        self._registry_device: Any = None
        self._observation_timestamp_identity: tuple[int, ...] | None = None
        self._observation_timestamp_history: dict[int, tuple[datetime, float]] = {}
        self.last_submission_receipt: CommandSubmission | None = None
        self._delivery_callback: Any = None
        self._hook_connection: Any = None
        self._hook_generation = 0
        self._hook_identity: tuple[int, ...] | None = None
        self._limit_observed_values: list[float | None] = [None, None, None]
        self._limit_receipts: list[float | None] = [None, None, None]
        self._current_observation_callback: Any = None
        self._telemetry_callback: Any = None
        self._telemetry_batch_sequence = 0
        self._native_current_identity: tuple[int, ...] | None = None
        self._native_current_values: dict[int, float] = {}
        self._native_current_sequences = {key: 0 for key in (182, 183, 184, 185)}
        self._native_current_receipts: dict[int, datetime] = {}
        self._native_current_source_times: dict[int, datetime] = {}
        self._native_current_source_highwaters: dict[int, datetime] = {}
        self._native_current_bucket_values: dict[int, set[float]] = {}
        self._native_current_evidence_credit: dict[int, bool] = {}
        self._native_current_errors: dict[int, str] = {}
        self._native_current_changed_at: float | None = None
        self._native_current_seeded = False
        self._native_divergence_started: float | None = None
        self._native_publication_generation = 0
        self._controlled_mapping_epoch = 0
        self._controlled_mapping_mode: int | None = None
        self._controlled_mapping_adopted: datetime | None = None
        self._controlled_mapping_sequence = 0
        self._controlled_mapping_floor = 0
        self._controlled_mapping_source: datetime | None = None
        self._controlled_mapping_highwater: datetime | None = None
        self._controlled_mapping_bucket: set[int] = set()
        self._controlled_mapping_error: str | None = None
        self._controlled_mapping_bootstrap = True
        self._controlled_mapping_broken = False
        self._closed = False
        self._request_lock = asyncio.Lock()
        self._request_starts: deque[float] = deque()
        self._pending_requests: set[asyncio.Task] = set()

    async def async_initialize(self) -> None:
        """Validate already loaded objects without making any network request."""
        self._resolve_runtime()

    @property
    def control_block_reason(self) -> str | None:
        """Recheck on every use so an Easee reload cannot leave stale objects."""
        return self._resolve_runtime()

    def _resolve_runtime(self) -> str | None:
        if self._closed:
            return "easee_adapter_closed"
        self._charger = self._circuit = self._product_data = None
        self._controller = self._registry_device = None
        # Do not import/start Easee or pyeasee ourselves. after_dependencies
        # ensures normal setup order; a missing integration stays blocked.
        easee_const = sys.modules.get("custom_components.easee.const")
        pyeasee = sys.modules.get("pyeasee")
        if getattr(easee_const, "VERSION", None) != SUPPORTED_EASEE_VERSION:
            return "unsupported_or_unloaded_easee_version"
        if getattr(pyeasee, "__version__", None) not in SUPPORTED_PYEASEE_VERSIONS:
            return "unsupported_or_unloaded_pyeasee_version"
        if not self.device_id:
            return "missing_easee_device"

        try:
            from homeassistant.helpers import device_registry as dr

            device = dr.async_get(self.hass).async_get(self.device_id)
            if device is None:
                return "easee_device_not_found"
            product_ids = {
                value for domain, value in device.identifiers if domain == "easee"
            }
            if len(product_ids) != 1:
                return "ambiguous_or_missing_easee_identifier"
            controller = self.hass.data.get("easee", {}).get("controller")
            if controller is None:
                return "easee_controller_unavailable"
            chargers = [
                charger
                for charger in controller.get_chargers()
                if charger.id in product_ids
            ]
            if len(chargers) != 1:
                return "easee_charger_not_unique"
            charger = chargers[0]
            circuit = charger.circuit
            if circuit is None or not callable(
                getattr(circuit, "set_dynamic_current", None)
            ):
                return "easee_circuit_unavailable"
            if not all(callable(getattr(charger, name, None)) for name in ("pause", "resume")):
                return "easee_command_api_unavailable"
            products = [
                product
                for product in controller.chargers_data
                if product.product is charger
            ]
            if len(products) != 1:
                return "easee_telemetry_source_not_unique"
            self._charger, self._circuit, self._product_data = charger, circuit, products[0]
            self._controller, self._registry_device = controller, device
            self._bind_delivery_hook()
        except Exception:
            # Exception text can include a remote URL or private account data.
            return "easee_runtime_incompatible"
        return None

    def set_delivery_callback(self, callback: Any):
        """Register one synchronous listener; return a local unsubscriber.

        SignalR.on is additive in the audited pysignalr versions. There is no
        public off method: stale callbacks hold only a weak adapter reference
        and become inert on connection replacement or close. Never replace
        pyeasee's existing callbacks or transport/reconnect implementation.
        """
        if callback is not None and not callable(callback):
            raise TypeError("delivery_callback_must_be_callable")
        self._delivery_callback = callback
        self._resolve_runtime()

        def unsubscribe():
            if self._delivery_callback is callback:
                self._delivery_callback = None
        return unsubscribe

    set_delivery_listener = set_delivery_callback

    def set_current_observation_callback(self, callback: Any):
        """Wake the local planner after an entire raw ProductUpdate payload."""
        if callback is not None and not callable(callback):
            raise TypeError("current_observation_callback_must_be_callable")
        self._current_observation_callback = callback
        self._resolve_runtime()

        def unsubscribe():
            if self._current_observation_callback is callback:
                self._current_observation_callback = None
        return unsubscribe

    def set_telemetry_callback(self, callback: Any):
        """Subscribe to a bounded scalar allowlist without changing control."""
        if callback is not None and not callable(callback):
            raise TypeError("telemetry_callback_must_be_callable")
        self._telemetry_callback = callback
        self._resolve_runtime()

        def unsubscribe():
            if self._telemetry_callback is callback:
                self._telemetry_callback = None
        return unsubscribe

    @property
    def delivery_listener_available(self) -> bool:
        return self._resolve_runtime() is None and self._hook_connection is not None

    def _bind_delivery_hook(self) -> None:
        connection = getattr(getattr(self._charger, "easee", None), "sr_connection", None)
        identity = self._snapshot_source_identity()
        if identity != self._native_current_identity:
            self._break_controlled_mapping("controlled_receiver_changed", bootstrap=True)
            self._controlled_mapping_highwater = None
            self._controlled_mapping_source = None
            self._controlled_mapping_bucket.clear()
            self._native_current_identity = identity
            self._native_current_values.clear()
            self._native_current_sequences = {key: 0 for key in (182, 183, 184, 185)}
            self._native_current_receipts.clear()
            self._native_current_source_times.clear()
            self._native_current_source_highwaters.clear()
            self._native_current_bucket_values.clear()
            self._native_current_evidence_credit.clear()
            self._native_current_errors.clear()
            self._native_current_changed_at = None
            self._native_current_seeded = False
            self._native_divergence_started = None
            self._native_publication_generation = 0
        self._seed_native_current()
        if connection is self._hook_connection and identity == self._hook_identity:
            return
        if self._hook_connection is not None:
            self._break_controlled_mapping("controlled_connection_changed", bootstrap=True)
        self._hook_generation += 1
        self._hook_connection = None
        self._hook_identity = None
        self._limit_observed_values = [None, None, None]
        self._limit_receipts = [None, None, None]
        if connection is None or not callable(getattr(connection, "on", None)):
            return
        generation = self._hook_generation
        charger_id = self._charger.id
        adapter_ref = weakref.ref(self)

        async def received(payload):
            owner = adapter_ref()
            if owner is None or owner._closed or generation != owner._hook_generation:
                return
            if owner._resolve_runtime() is not None:
                return
            if generation != owner._hook_generation or identity != owner._snapshot_source_identity():
                return
            owner._receive_delivery(payload, charger_id)

        async def observed(payload):
            owner = adapter_ref()
            if owner is None or owner._closed or generation != owner._hook_generation:
                return
            if owner._resolve_runtime() is not None:
                return
            if generation != owner._hook_generation or identity != owner._snapshot_source_identity():
                return
            owner._receive_limit_observation(payload, charger_id)
            mapping_changed = owner._receive_controlled_mapping(payload, charger_id)
            owner._receive_current_observations(payload, charger_id)
            if mapping_changed and callable(owner._current_observation_callback):
                try:
                    owner._current_observation_callback()
                except Exception:
                    pass
            owner._receive_telemetry_observations(payload, charger_id,
                origin="signalr_product_update_callback")

        try:
            connection.on("CommandResponse", received)
            connection.on("ProductUpdate", observed)
        except Exception:
            # Telemetry commands still work, but delivery remains unverified.
            return
        self._hook_connection = connection
        self._hook_identity = identity

    def _receive_telemetry_observations(self, payload: Any, charger_id: str, *, origin: str = "unknown") -> None:
        callback = self._telemetry_callback
        if not callable(callback):
            return
        records = payload if type(payload) is list else [payload]
        received = datetime.now(timezone.utc)
        self._telemetry_batch_sequence += 1
        events = []
        for record in records[:64]:
            if not isinstance(record, Mapping) or record.get("mid") != charger_id:
                continue
            key = record.get("id")
            if type(key) is not int or key not in PRIVATE_TELEMETRY_OBSERVATION_IDS:
                continue
            value, error = None, None
            if key == 100:
                raw = record.get("value")
                if isinstance(raw, str) and re.fullmatch(r"[A-Fa-f]", raw):
                    value = raw.upper()
                else:
                    error = "invalid_diagnostic_pilot"
            else:
                try:
                    value = self._snapshot_number(record.get("value"), "invalid_diagnostic_number",
                        integer=key in {21, 38, 96, 109, 110})
                    if not 0 <= value <= (10000 if key in {21, 38, 96, 109, 110} else 1000):
                        raise EaseeAdapterError("invalid_diagnostic_number")
                except EaseeAdapterError:
                    value, error = None, "invalid_diagnostic_number"
            source = None
            if record.get("timestamp") is not None:
                source, source_error = self._observation_timestamp(record["timestamp"], received)
                error = error or source_error
            events.append(NativeTelemetryObservation(key, value,
                source.isoformat() if source is not None else None, received.isoformat(),
                _monotonic(), self._telemetry_batch_sequence, self._hook_generation, error, origin))
        if events:
            try:
                callback(tuple(events))
            except Exception:
                # Diagnostics cannot interrupt raw current or native handlers.
                pass

    def _receive_limit_observation(self, payload: Any, charger_id: str) -> None:
        """Retain only selected native phase events, never GET/cache reads.

        This is a local receipt clock. A ProductUpdate is still vendor data,
        not synchronized physical acquisition or a command delivery receipt.
        """
        records = payload if type(payload) is list else [payload]
        for record in records[:64]:
            if not isinstance(record, Mapping) or record.get("mid") != charger_id:
                continue
            observation_id = record.get("id")
            if type(observation_id) is not int or observation_id not in (111, 112, 113):
                continue
            if type(record.get("dataType")) is not int or record["dataType"] not in (3, 4):
                continue
            try:
                value = self._snapshot_number(record.get("value"), "invalid_limit_observation")
            except EaseeAdapterError:
                continue
            if not 0 <= value <= 1000:
                continue
            self._limit_observed_values[observation_id - 111] = value
            self._limit_receipts[observation_id - 111] = _monotonic()

    def _break_controlled_mapping(self, error: str, *, bootstrap: bool = False) -> None:
        if not self._controlled_mapping_broken:
            self._controlled_mapping_epoch += 1
        self._controlled_mapping_broken = True
        self._controlled_mapping_mode = None
        self._controlled_mapping_adopted = None
        self._controlled_mapping_floor = self._native_current_sequences.get(183, 0)
        self._controlled_mapping_error = error
        self._controlled_mapping_bootstrap = bootstrap

    def _adopt_controlled_mapping(self, mode: int, received: datetime) -> None:
        if self._controlled_mapping_mode != mode or self._controlled_mapping_adopted is None:
            self._controlled_mapping_epoch += 1
            self._controlled_mapping_adopted = received
            self._controlled_mapping_floor = self._native_current_sequences.get(183, 0)
        self._controlled_mapping_mode = mode
        self._controlled_mapping_broken = False
        self._controlled_mapping_bootstrap = False
        self._controlled_mapping_error = (None if mode == 10 else "controlled_output_phase_unsupported")

    def _receive_controlled_mapping(self, payload: Any, charger_id: str) -> bool:
        """Consume actual mode110 before current events from the same batch.

        A stable phase context does not need another event per current sample.
        Regressed/conflicting phase data cannot revive an earlier low terminal.
        """
        records = payload if type(payload) is list else [payload]
        if len(records) > 64:
            # The bounded prefix cannot certify that mode110 did not change
            # later in this atomic native publication. Re-adoption may use the
            # complete native SDK context, but never this prefix's183 sample.
            self._break_controlled_mapping("controlled_mapping_payload_truncated", bootstrap=True)
            return True
        selected = [item for item in records[:64] if isinstance(item, Mapping)
                    and item.get("mid") == charger_id and type(item.get("id")) is int
                    and item["id"] == 110]
        if not selected:
            return False
        if len({(repr(item.get("dataType")), str(item.get("value")), str(item.get("timestamp")))
                for item in selected}) != 1:
            self._break_controlled_mapping("controlled_mapping_conflict")
            return True
        item, received = selected[0], datetime.now(timezone.utc)
        try:
            if type(item.get("dataType")) is not int or item["dataType"] != 4:
                raise EaseeAdapterError("controlled_mapping_type_invalid")
            mode = self._snapshot_number(item.get("value"), "controlled_mapping_value_invalid", integer=True)
            if not 0 <= mode <= 255:
                raise EaseeAdapterError("controlled_mapping_value_invalid")
            stamp = None
            if item.get("timestamp") is not None:
                stamp, error = self._observation_timestamp(item["timestamp"], received)
                if error:
                    raise EaseeAdapterError("controlled_mapping_timestamp_invalid")
            high = self._controlled_mapping_highwater
            if stamp is not None and high is not None and stamp < high:
                raise EaseeAdapterError("controlled_mapping_replayed")
            if stamp is None and (
                    high is not None and (mode != self._controlled_mapping_mode or self._controlled_mapping_broken)
                    or self._controlled_mapping_adopted is not None and mode != self._controlled_mapping_mode):
                raise EaseeAdapterError("controlled_mapping_timestamp_missing")
            same_bucket = stamp is not None and stamp == high
            if same_bucket and self._controlled_mapping_bucket and mode not in self._controlled_mapping_bucket:
                self._controlled_mapping_bucket.add(mode)
                raise EaseeAdapterError("controlled_mapping_bucket_conflict")
            if same_bucket and len(self._controlled_mapping_bucket) > 1:
                raise EaseeAdapterError("controlled_mapping_bucket_conflict")
            if self._controlled_mapping_broken and not self._controlled_mapping_bootstrap and (
                    stamp is None or high is not None and stamp <= high):
                raise EaseeAdapterError("controlled_mapping_recovery_unverified")
            changed = (mode != self._controlled_mapping_mode or self._controlled_mapping_adopted is None
                       or stamp is not None and (high is None or stamp > high))
            self._adopt_controlled_mapping(int(mode), received)
            if changed:
                self._controlled_mapping_sequence += 1
            if stamp is not None:
                if not same_bucket:
                    self._controlled_mapping_bucket = {int(mode)}
                self._controlled_mapping_source = stamp
                self._controlled_mapping_highwater = stamp
        except EaseeAdapterError as err:
            self._break_controlled_mapping(str(err))
        return True

    def _receive_current_observations(self, payload: Any, charger_id: str) -> None:
        """Track each conductor independently of HA aggregate-attribute fanout.

        The public SDK requires only mid/id/dataType/value. Optional timestamp
        metadata is validated when present, never invented from arrival time.
        An identical cached event does not renew its terminal receipt clock.
        """
        self._seed_native_current()
        records = payload if type(payload) is list else [payload]
        received = datetime.now(timezone.utc)
        changed = False
        seen: dict[int, tuple] = {}
        conflicts: set[int] = set()
        for record in records[:64]:
            if not isinstance(record, Mapping) or record.get("mid") != charger_id:
                continue
            key = record.get("id")
            if type(key) is int and key in (182, 183, 184, 185):
                signature = (record.get("dataType"), record.get("value"), record.get("timestamp"))
                if key in seen and seen[key] != signature:
                    conflicts.add(key)
                seen[key] = signature
        for record in records[:64]:
            if not isinstance(record, Mapping) or record.get("mid") != charger_id:
                continue
            key = record.get("id")
            if type(key) is not int or key not in (182, 183, 184, 185):
                continue
            if key in conflicts:
                self._native_current_errors[key] = "conflicting_terminal_observations"
                changed = True
                continue
            if type(record.get("dataType")) is not int or record["dataType"] != 3:
                self._native_current_errors[key] = "invalid_terminal_current_type"
                changed = True
                continue
            try:
                number = self._snapshot_number(record.get("value"), "invalid_terminal_current")
                if not 0 <= number <= 1000:
                    raise EaseeAdapterError("invalid_terminal_current")
            except EaseeAdapterError:
                self._native_current_errors[key] = "invalid_terminal_current"
                changed = True
                continue
            stamp = None
            previous_highwater = self._native_current_source_highwaters.get(key)
            if record.get("timestamp") is not None:
                stamp, error = self._observation_timestamp(record["timestamp"], received)
                if stamp is not None and previous_highwater is not None:
                    if stamp < previous_highwater:
                        error = "easee_observation_timestamp_regressed"
                if error:
                    self._native_current_errors[key] = error
                    changed = True
                    continue
            previous_number = self._native_current_values.get(key)
            previous_stamp = self._native_current_source_times.get(key)
            previous_receipt = self._native_current_receipts.get(key)
            value_changed = previous_number != number
            first_observation = previous_receipt is None
            same_bucket = stamp is not None and stamp == previous_highwater
            bucket = self._native_current_bucket_values.get(key, set())
            # Provider timestamps are not unique event IDs. In one coarse
            # bucket, an unseen numeric raw observation is ordered new input.
            # A return to an already seen value (or overflow) is numeric state
            # only: never relabel the intervening value's clock as new proof.
            ambiguous = bool(same_bucket and value_changed
                and (number in bucket or len(bucket) >= 64))
            # Timestamp-only cache replies cannot label an intervening new A
            # value as an older A event. A later raw observation can establish
            # a NEW event in an unbound value epoch only when its source UTC is
            # strictly after both that epoch's receipt and prior source highwater.
            # Its new local receipt/sequence never rewrites the earlier event.
            source_advanced = stamp is not None and (
                previous_stamp is not None and stamp > previous_stamp
                or previous_stamp is None and previous_receipt is not None and stamp > previous_receipt
                   and (previous_highwater is None or stamp > previous_highwater))
            advanced = value_changed or source_advanced or first_observation
            recovered = self._native_current_errors.pop(key, None) is not None
            changed = changed or recovered
            if advanced:
                self._native_current_values[key] = number
                self._native_current_receipts[key] = received
                self._native_current_sequences[key] += 1
                self._native_current_evidence_credit[key] = not ambiguous
                # A genuinely new unchanged-value source event can also be
                # one frame of a multi-conductor burst. Defer only new offer
                # authority until that evidence burst has had time to settle.
                self._native_current_changed_at = _monotonic()
                changed = True
            if value_changed and stamp is None:
                self._native_current_source_times.pop(key, None)
            if stamp is not None and (value_changed or previous_stamp is not None or source_advanced or first_observation):
                self._native_current_source_times[key] = stamp
            if stamp is not None:
                self._native_current_source_highwaters[key] = stamp
                if not same_bucket:
                    self._native_current_bucket_values[key] = {number}
                elif len(bucket) < 64:
                    bucket.add(number)
                    self._native_current_bucket_values[key] = bucket
        # Only a complete valid raw publication can finish a divergence
        # episode. A poll briefly returning the old tuple cannot renew the
        # watchdog by making an intermediate getter appear coherent.
        if seen and not self._native_current_errors and self._sdk_current_tuple() == self._native_current_values:
            self._native_divergence_started = None
        if changed and callable(self._current_observation_callback):
            try:
                self._current_observation_callback()
            except Exception:
                # Observer failure cannot break native SDK stream dispatch.
                pass

    def _sdk_current_tuple(self) -> dict[int, float] | None:
        """Read the SDK projection only; never turn a poll into event evidence."""
        try:
            state = self._product_data.state
            numbers = {key: state[f"inCurrentT{key - 180}"] for key in (182, 183, 184, 185)}
            if any(type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1000
                   for value in numbers.values()):
                raise ValueError("invalid_terminal_current")
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return numbers

    def _seed_native_current(self) -> None:
        if not self._native_current_seeded:
            numbers = self._sdk_current_tuple()
            if numbers is not None:
                # A one-time coherent bootstrap tuple supplies numeric bounds,
                # never a receipt, event sequence, response or fresh authority.
                self._native_current_values.update(numbers)
                self._native_current_seeded = True

    @property
    def native_current_observation(self) -> NativeCurrentObservation | None:
        """Return a committed raw tuple, independent of earlier HA publication.

        The SDK's first registered handler can publish several native sensors
        and await unrelated work before our additive handler gets that same
        batch. Its partial projection is diagnostic, not a replacement clock.
        """
        if self._resolve_runtime() is not None:
            return None
        identity = self._snapshot_source_identity()
        sdk = self._sdk_current_tuple()
        numbers = dict(self._native_current_values)
        committed = self._native_current_seeded and len(numbers) == 4
        if not committed:
            return NativeCurrentObservation(None, (), None, identity,
                error="native_terminal_values_unavailable",
                observation_sequences=tuple(sorted(self._native_current_sequences.items())))
        current = max(numbers.values())
        maxima = tuple(key for key in (182, 183, 184, 185) if numbers[key] == current)
        # Any invalid conductor could have become the maximum. Its previous
        # lower value cannot hide malformed new input in a committed tuple.
        error = next((self._native_current_errors[key] for key in sorted(self._native_current_errors)), None)
        if sdk is not None and sdk != numbers:
            if self._native_divergence_started is None:
                self._native_divergence_started = _monotonic()
                self._native_publication_generation += 1
        pending = self._native_divergence_started is not None
        if pending and _monotonic() - self._native_divergence_started >= NATIVE_PUBLICATION_MAX_DIVERGENCE_SECONDS:
            error = error or "native_publication_diverged"
        if sdk is None:
            error = "native_sdk_current_invalid"
        matched = error is None and all(self._native_current_values.get(key) == numbers[key]
                    and key in self._native_current_receipts for key in maxima)
        receipt = min(self._native_current_receipts[key] for key in maxima) if matched else None
        return NativeCurrentObservation(float(current), maxima, receipt, identity,
            source_timestamps=tuple((key, self._native_current_source_times.get(key)) for key in numbers),
            observation_report_times=tuple((key, self._native_current_receipts[key]) for key in numbers
                if self._native_current_values.get(key) == numbers[key]
                and key in self._native_current_receipts and key not in self._native_current_errors),
            observation_sequences=tuple(sorted(self._native_current_sequences.items())),
            observation_evidence_credit=tuple((key, self._native_current_evidence_credit.get(key, False))
                for key in sorted(numbers)),
            source_metadata_highwaters=tuple(sorted(self._native_current_source_highwaters.items())),
            error=error, last_tuple_change_monotonic=self._native_current_changed_at,
            committed=True, sdk_current=max(sdk.values()) if sdk is not None else None,
            native_publication_pending=pending,
            divergence_started_monotonic=self._native_divergence_started,
            publication_generation=self._native_publication_generation)

    @property
    def native_controlled_l1_observation(self) -> NativeControlledL1Observation | None:
        """Return only a real183 receipt under the current mode10 epoch.

        Bootstrap mode is explicit native context, never a default or current
        sample. After adoption/gap/switch an independently new183 is required.
        """
        problem = self.telemetry_block_reason
        if problem:
            return None
        identity = self._snapshot_source_identity()
        mode = self._product_data.state.get("outputPhase")
        sdk_mode = mode if type(mode) is int and 0 <= mode <= 255 else None
        if self._controlled_mapping_bootstrap:
            if sdk_mode is not None:
                self._adopt_controlled_mapping(sdk_mode, datetime.now(timezone.utc))
            else:
                self._controlled_mapping_error = "controlled_mapping_unavailable"
        elif not self._controlled_mapping_broken and sdk_mode != self._controlled_mapping_mode:
            # A partial native publication withdraws the old epoch. Once the
            # verified SDK context is complete it may be adopted afresh, but
            # its cached183 cannot become the response to an earlier request.
            self._break_controlled_mapping("controlled_mapping_publication_pending", bootstrap=True)
        error = self._controlled_mapping_error
        if self._controlled_mapping_mode != 10:
            error = error or "controlled_output_phase_unsupported"
        value = self._native_current_values.get(183)
        receipt = self._native_current_receipts.get(183)
        sequence = self._native_current_sequences.get(183, 0)
        adopted = self._controlled_mapping_adopted
        current_error = self._native_current_errors.get(183)
        if (value is None or receipt is None or adopted is None or receipt <= adopted
                or sequence <= self._controlled_mapping_floor):
            current_error = current_error or "controlled_current_after_mapping_required"
        sdk_l1 = self._product_data.state.get("inCurrentT3")
        if (type(sdk_l1) not in (int, float) or not isfinite(sdk_l1)
                or not 0 <= sdk_l1 <= 1000 or sdk_l1 != value):
            current_error = current_error or "controlled_current_publication_pending"
        credit = self._native_current_evidence_credit.get(183, False)
        if credit is not True:
            current_error = current_error or "controlled_current_event_unqualified"
        return NativeControlledL1Observation(value, receipt, sequence, credit, identity,
            self._controlled_mapping_epoch, self._controlled_mapping_mode, adopted,
            self._controlled_mapping_sequence,
            source_at=self._native_current_source_times.get(183),
            source_highwater_at=self._native_current_source_highwaters.get(183),
            mapping_source_at=self._controlled_mapping_source,
            mapping_source_highwater_at=self._controlled_mapping_highwater,
            error=error, committed=error is None and current_error is None,
            current_error=current_error)

    @property
    def reported_limit_receipts(self) -> tuple[float | None, float | None, float | None]:
        """Local native-event receipts, fenced against a replaced readback."""
        limits = self.reported_limits
        if limits is None:
            return (None, None, None)
        return tuple(receipt if observed == current else None for receipt, observed, current
                     in zip(self._limit_receipts, self._limit_observed_values, limits))

    @staticmethod
    def _positive_integer(value: Any) -> int | None:
        if type(value) is int:
            return value if 0 < value < 2**63 else None
        if type(value) is str and re.fullmatch(r"[0-9]{1,19}", value):
            number = int(value)
            return number if 0 < number < 2**63 else None
        return None

    @staticmethod
    def _safe_vendor_timestamp(value: Any) -> str | None:
        if type(value) is not str or len(value) > 40:
            return None
        stamp, error = EaseeCommandAdapter._observation_timestamp(value, datetime.now(timezone.utc))
        return stamp.isoformat() if error is None else None

    def _receive_delivery(self, payload: Any, charger_id: str) -> None:
        callback = self._delivery_callback
        if callback is None:
            return
        records = payload if type(payload) is list else [payload]
        for record in records[:32]:
            if not isinstance(record, Mapping):
                continue
            # Missing or foreign device identity must never confirm our command.
            serial = record.get("serialNumber", record.get("SerialNumber"))
            if serial != charger_id:
                continue
            command_type = self._positive_integer(record.get("id", record.get("Id")))
            ticks = self._positive_integer(record.get("ticks", record.get("Ticks")))
            accepted = record.get("wasAccepted", record.get("WasAccepted"))
            result = record.get("resultCode", record.get("ResultCode"))
            if command_type is None or ticks is None or type(accepted) is not bool:
                continue
            if result is not None and (type(result) is not int or not -32768 <= result <= 32767):
                continue
            event = CommandDelivery(
                command_type, str(ticks), accepted, result,
                self._safe_vendor_timestamp(record.get("timestamp", record.get("Timestamp"))),
                self._safe_vendor_timestamp(record.get("deliveredAt", record.get("DeliveredAt"))),
                _monotonic(),
            )
            try:
                callback(event)
            except Exception:
                # A diagnostic listener must not break native stream dispatch.
                continue

    async def async_close(self) -> None:
        self._closed = True
        self._hook_generation += 1
        self._hook_connection = None
        self._delivery_callback = None
        self._current_observation_callback = None
        self._telemetry_callback = None
        self._limit_observed_values = [None, None, None]
        self._limit_receipts = [None, None, None]
        self._native_current_values.clear()
        self._native_current_sequences = {key: 0 for key in (182, 183, 184, 185)}
        self._native_current_receipts.clear()
        self._native_current_source_times.clear()
        self._native_current_source_highwaters.clear()
        self._native_current_errors.clear()
        self._native_current_changed_at = None
        self._native_current_seeded = False
        self._native_divergence_started = None
        self._native_publication_generation = 0
        pending = tuple(self._pending_requests)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def reported_limits(self) -> tuple[float, float, float] | None:
        """Cached native readback only; no acquisition/delivery assertion."""
        if self._resolve_runtime() is not None or not isinstance(self._product_data.state, Mapping):
            return None
        result = []
        for phase in (1, 2, 3):
            value = self._product_data.state.get(f"dynamicCircuitCurrentP{phase}")
            if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1000:
                return None
            result.append(float(value))
        return tuple(result)

    @property
    def offline_limits_verified_zero(self) -> bool:
        if self._resolve_runtime() is not None or not isinstance(self._product_data.state, Mapping):
            return False
        return all(type(self._product_data.state.get(f"offlineMaxCircuitCurrentP{phase}")) in (int, float)
                   and self._product_data.state[f"offlineMaxCircuitCurrentP{phase}"] == 0
                   for phase in (1, 2, 3))

    @property
    def control_identity(self) -> tuple[int, ...] | None:
        """Opaque process-local fence; callers must never persist this tuple."""
        if self._resolve_runtime() is not None:
            return None
        return tuple(id(value) for value in (
            self._registry_device, self._controller, self._charger,
            self._circuit, self._product_data, self._hook_connection,
        ))

    def _runtime_identity(self) -> tuple[Any, ...]:
        """Capture selection values and object identities before an await."""
        return (
            self.device_id,
            self._charger.id,
            self._registry_device,
            self._controller,
            self._charger,
            self._circuit,
            self._product_data,
        )

    def _require_same_runtime(self, identity: tuple[Any, ...], *, healthy: bool, allow_product_quiet: bool = False) -> None:
        """Reject results or follow-up commands across an integration reload."""
        problem = (self.transport_block_reason if allow_product_quiet else self.telemetry_block_reason) if healthy else self._resolve_runtime()
        if problem:
            raise EaseeAdapterError(problem)
        current = self._runtime_identity()
        if current[:2] != identity[:2] or any(
            old is not new for old, new in zip(identity[2:], current[2:])
        ):
            raise EaseeAdapterError("easee_runtime_changed_during_request")

    def _snapshot_source_identity(self) -> tuple[int, ...]:
        """Opaque process-local identity; never expose objects or credentials."""
        return tuple(id(value) for value in (
            self._controller, self._charger, self._circuit, self._product_data
        ))

    @property
    def observation_clock_identity(self) -> tuple[int, ...] | None:
        """Opaque process-local owner of current clocks, never persisted.

        A SignalR reconnect alone does not replace the selected product's
        observation clocks. Replaced SDK/controller objects do.
        """
        if self._resolve_runtime() is not None:
            return None
        return self._snapshot_source_identity()

    def snapshot_is_current(self, snapshot: ChargerCurrentSnapshot) -> bool:
        """Revalidate a cached receipt against healthy, currently loaded objects."""
        try:
            return (
                self.telemetry_block_reason is None
                and snapshot.source_identity == self._snapshot_source_identity()
            )
        except Exception:
            return False

    async def async_read_current_snapshot(self) -> ChargerCurrentSnapshot:
        """Read selected native observations without issuing charger commands.

        Stream health is required before and after the bounded GET. The receipt
        time only records when this request returned locally: the server
        returns last-reported observations, not a new physical sample.
        Callers must separately reject results superseded by concurrent events.
        """
        try:
            problem = self.telemetry_block_reason
            if problem:
                raise EaseeAdapterError(problem)
            identity = self._runtime_identity()
            charger = self._charger
            if not callable(getattr(charger, "get_observations", None)):
                raise EaseeAdapterError("easee_snapshot_api_unavailable")
            async with asyncio.timeout(min(REQUEST_TIMEOUT_SECONDS, 10)):
                state = await charger.get_observations(*SNAPSHOT_OBSERVATION_IDS)
            received_at = datetime.now(timezone.utc)
            self._require_same_runtime(identity, healthy=True)
            if not isinstance(state, Mapping):
                raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
            observations = state.get("observations")
            if type(observations) is not list or len(observations) < 5:
                raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
            values = {}
            timestamp_values = {}
            for observation in observations:
                if not isinstance(observation, Mapping):
                    raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
                observation_id = observation.get("id")
                data_type = observation.get("dataType")
                if (type(observation_id) is not int or observation_id <= 0
                        or type(data_type) is not int or "value" not in observation):
                    raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
                if observation_id not in SNAPSHOT_OBSERVATION_IDS:
                    # Some servers may return extra observations despite the
                    # requested filter. Never use offered or unrelated values.
                    continue
                if observation_id in values:
                    raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
                # Pinned pyeasee: dataType3 is Double, dataType4 is Integer.
                if data_type != (4 if observation_id == 109 else 3):
                    raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
                values[observation_id] = observation.get("value")
                timestamp_values[observation_id] = observation.get("timestamp")
            if set(values) != set(SNAPSHOT_OBSERVATION_IDS):
                raise EaseeAdapterError("easee_snapshot_missing_or_invalid")
            # Match Easee 0.9.74's current sensor: max of all input terminals.
            # Offered pilot current (observation114) is deliberately absent.
            terminal_currents = []
            for observation_id in SNAPSHOT_OBSERVATION_IDS[1:]:
                number = self._snapshot_number(values[observation_id], "easee_snapshot_invalid_current")
                if not 0 <= number <= 1000:
                    raise EaseeAdapterError("easee_snapshot_invalid_current")
                terminal_currents.append(number)
            current = max(terminal_currents)
            mode = self._snapshot_number(
                values[109], "easee_snapshot_invalid_operation_mode", integer=True
            )
            status = CHARGER_OPERATION_MODES.get(mode)
            if status is None:
                raise EaseeAdapterError("easee_snapshot_invalid_operation_mode")
            source_identity = self._snapshot_source_identity()
            if source_identity != self._observation_timestamp_identity:
                self._observation_timestamp_identity = source_identity
                self._observation_timestamp_history.clear()
            numbers = dict(zip(SNAPSHOT_OBSERVATION_IDS[1:], terminal_currents))
            numbers[109] = mode
            timestamps = {}
            timestamp_errors = {}
            for observation_id in SNAPSHOT_OBSERVATION_IDS:
                stamp, error = self._observation_timestamp(
                    timestamp_values[observation_id], received_at)
                previous = self._observation_timestamp_history.get(observation_id)
                if stamp is not None and previous is not None:
                    if stamp < previous[0]:
                        stamp, error = None, "easee_observation_timestamp_regressed"
                    elif stamp == previous[0] and numbers[observation_id] != previous[1]:
                        stamp, error = None, "easee_observation_timestamp_value_changed"
                if stamp is not None:
                    self._observation_timestamp_history[observation_id] = (stamp, numbers[observation_id])
                timestamps[observation_id] = stamp
                timestamp_errors[observation_id] = error
            # All equally maximal terminals can explain the returned current.
            # Do not let a newer zero on an unused terminal renew an old high
            # current. For all-zero readings this remains conservative: no
            # active terminal or physical zero response is inferred here.
            maxima = [key for key in SNAPSHOT_OBSERVATION_IDS[1:] if numbers[key] == current]
            source_error = next((timestamp_errors[key] for key in maxima if timestamp_errors[key]), None)
            current_timestamp = (min(timestamps[key] for key in maxima)
                                 if source_error is None else None)
            return ChargerCurrentSnapshot(
                current, status, received_at, source_identity,
                current_source_timestamp=current_timestamp,
                status_source_timestamp=timestamps[109],
                current_source_timestamps=tuple((key, timestamps[key]) for key in SNAPSHOT_OBSERVATION_IDS[1:]),
                source_timestamp_error=source_error,
                current_source_observation_ids=tuple(maxima),
            )
        except EaseeAdapterError:
            raise
        except Exception as err:
            raise EaseeAdapterError(f"easee_snapshot_failed:{type(err).__name__}") from None

    @staticmethod
    def _observation_timestamp(value: Any, received_at: datetime) -> tuple[datetime | None, str | None]:
        """Parse only aware RFC3339 metadata; never substitute a local clock."""
        if value is None:
            return None, "easee_observation_timestamp_missing"
        if (type(value) is not str or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:\.[0-9]{1,9})?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value) is None
                or value.endswith("-00:00")):
            return None, "easee_observation_timestamp_invalid"
        try:
            # ISO parsers may normalize forms such as 24:00 differently across
            # Python versions. Validate the original civil components first;
            # no normalization may turn malformed metadata into time credit.
            datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]),
                     int(value[11:13]), int(value[14:16]), int(value[17:19]))
            stamp = datetime.fromisoformat(
                value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value
            ).astimezone(timezone.utc)
            if (stamp - received_at).total_seconds() > OBSERVATION_FUTURE_TOLERANCE_SECONDS:
                return None, "easee_observation_timestamp_future"
        except (ValueError, TypeError, OverflowError):
            return None, "easee_observation_timestamp_invalid"
        return stamp, None

    @staticmethod
    def _snapshot_number(value: Any, error_code: str, *, integer: bool = False) -> float:
        """Parse declared observation types without bool/default/coercion leaks."""
        if type(value) is str:
            pattern = (r"[+-]?[0-9]+" if integer else
                       r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
            if re.fullmatch(pattern, value) is None:
                raise EaseeAdapterError(error_code)
        elif type(value) not in (int, float):
            raise EaseeAdapterError(error_code)
        try:
            number = float(value)
        except (ValueError, OverflowError):
            raise EaseeAdapterError(error_code) from None
        if not isfinite(number) or (integer and not number.is_integer()):
            raise EaseeAdapterError(error_code)
        return number

    @property
    def overload_block_reason(self) -> str | None:
        """Gate optional overload against read-back zero offline circuit limits."""
        problem = self.telemetry_block_reason
        if problem:
            return problem
        return self.offline_fallback_block_reason

    @property
    def offline_fallback_block_reason(self) -> str | None:
        """Actual transport and offline limits, independent of product silence."""
        problem = self.transport_block_reason
        if problem:
            return problem
        for phase in (1, 2, 3):
            value = self._product_data.state.get(f"offlineMaxCircuitCurrentP{phase}")
            if isinstance(value, bool):
                return "offline_fallback_not_zero_or_unverified"
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return "offline_fallback_not_zero_or_unverified"
            if not isfinite(number) or number != 0:
                return "offline_fallback_not_zero_or_unverified"
        return None

    @property
    def transport_block_reason(self) -> str | None:
        """Hard selected-source faults; ProductUpdate silence is separate."""
        problem = self._resolve_runtime()
        if problem:
            return self._controlled_health_result(problem)
        state = self._product_data.state
        if state is None:
            return self._controlled_health_result("easee_telemetry_missing")
        try:
            if state.get("signalRConnected") is not True:
                return self._controlled_health_result("easee_stream_disconnected")
            if state.get("isOnline") is not True:
                return self._controlled_health_result("easee_charger_offline")
            pulse = state.get("latestPulse")
            if not isinstance(pulse, datetime) or pulse.tzinfo is None:
                return self._controlled_health_result("easee_heartbeat_missing")
            age = (datetime.now(timezone.utc) - pulse).total_seconds()
            if age < -5:
                return self._controlled_health_result("easee_heartbeat_stale")
        except (AttributeError, TypeError, ValueError, OverflowError):
            return self._controlled_health_result("easee_telemetry_invalid")
        return None

    @property
    def product_data_quiet(self) -> bool:
        if self.transport_block_reason is not None:
            return False
        return (datetime.now(timezone.utc)-self._product_data.state["latestPulse"]).total_seconds()>MAX_TELEMETRY_AGE_SECONDS

    @property
    def telemetry_block_reason(self) -> str | None:
        """Legacy strict data gate. Silence alone never destroys phase scope."""
        problem=self.transport_block_reason
        if problem:
            return problem
        return "easee_heartbeat_stale" if self.product_data_quiet else None

    @staticmethod
    def _quiet_normal_permit(decision: Any) -> bool:
        """Only a controller-revalidated same normal offer may use this lane."""
        if getattr(decision,"meter_quiet_normal_retention",False) is not True:
            return False
        cap=getattr(decision,"current_p1",None)
        raw=getattr(decision,"grid_l1",None)
        upper=getattr(decision,"meter_quiet_upper_amps",None)
        budget=getattr(decision,"meter_quiet_normal_budget_amps",None)
        return bool(getattr(decision,"meter_envelope",False) is True
            and decision.command=="set_limit" and decision.mode=="meter_quiet_normal"
            and type(cap) is int and cap>0 and decision.committed_current_p1==cap
            and decision.current_p2==decision.current_p3==0
            and getattr(decision,"meter_overload_authority",False) is False
            and all(type(v) in (int,float) and isfinite(v) for v in (raw,upper,budget))
            and 0<=raw and cap<=upper<=40 and 0<budget<=23 and raw+upper<=budget)

    def _controlled_health_result(self, problem: str) -> str:
        # An observed transport/online gap cannot preserve a phase-to-current
        # capability. Legacy health checks and fallback behavior are unchanged.
        self._break_controlled_mapping(problem, bootstrap=True)
        return problem

    async def async_execute_with_dispatch(self, decision: Any, on_dispatch: Any) -> CommandSubmission:
        """Expose the exact local admission/SDK boundary to the controller."""
        if not callable(on_dispatch):
            raise CommandNotSent("invalid_dispatch_callback")
        return await self.async_execute(decision, on_dispatch=on_dispatch)

    async def async_execute(self, decision: Any, *, on_dispatch: Any = None) -> CommandSubmission:
        """Submit a limit or pause/resume, raising unless the API accepted it.

        No deduplication: repeating the same circuit limit must renew the TTL.
        Unknown versions never fall back to the silently swallowing HA service.
        """
        problem = self._resolve_runtime()
        if problem:
            raise CommandNotSent(problem)
        command = decision.command
        quiet_permit=self._quiet_normal_permit(decision)
        if getattr(decision,"meter_quiet_normal_retention",False) and (not quiet_permit or not callable(on_dispatch)):
            raise CommandNotSent("quiet_normal_dispatch_proof_required")
        if command in {"set_limit", "resume"}:
            currents = (decision.current_p1, decision.current_p2, decision.current_p3)
            if any(type(value) is not int or not 0 <= value <= 40 for value in currents):
                raise CommandNotSent("invalid_phase_current")
            ttl = decision.ttl_seconds
            if (
                isinstance(ttl, bool)
                or not isinstance(ttl, (int, float))
                or not isfinite(ttl)
                or not 0 < ttl <= 1080 * 60
            ):
                raise CommandNotSent("finite_ttl_required")
            ttl_minutes = ceil(ttl / 60)
            if command == "resume" and (currents[0] < 6 or currents[1:] != (0, 0)):
                raise CommandNotSent("resume_requires_safe_l1_cap")
            if any(currents) or command == "resume":
                health_problem = self.transport_block_reason if quiet_permit else self.telemetry_block_reason
                if health_problem:
                    raise CommandNotSent(health_problem)
        elif command != "pause":
            raise CommandNotSent("unsupported_easee_command")

        # A concurrent state read can resolve replacement objects while this
        # command awaits the cloud. Keep the whole sequence on its original
        # charger and never resume a replacement after capping the old circuit.
        identity = self._runtime_identity()
        charger, circuit = self._charger, self._circuit
        started = None
        self.last_submission_receipt = None
        sdk_started = False

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._request_lock:
                    # Queueing (including our lock and the SDK's opaque shared
                    # throttler) consumes the same deadline. No detached task
                    # may continue writing after cancellation or close.
                    self._require_same_runtime(identity, healthy=command == "resume" or
                                               (command == "set_limit" and any(currents)), allow_product_quiet=quiet_permit)
                    if quiet_permit and self.offline_fallback_block_reason:
                        raise CommandNotSent(self.offline_fallback_block_reason)
                    if command in {"set_limit", "resume"}:
                        previous = getattr(decision, "committed_current_p1", None)
                        protective = bool(command == "set_limit"
                            and getattr(decision, "command_urgency", "normal") == "protective_reduction"
                            and type(previous) is int and 0 < currents[0] <= 7 and currents[0] < previous
                            and currents[1:] == (0, 0))
                        self._admit_limit(any(currents), protective=protective, record=False)
                    if on_dispatch is not None and on_dispatch() is not True:
                        raise CommandNotSent("dispatch_context_rejected")
                    if command in {"set_limit", "resume"}:
                        # No await separates admission from this record and
                        # SDK entry. A rejected callback spends no rate slot.
                        self._request_starts.append(_monotonic())
                    started = _monotonic()
                    sdk_started = True
                    if command in {"set_limit", "resume"}:
                        result = await self._await_sdk(circuit.set_dynamic_current(*currents, ttl_minutes))
                        receipt = await self._read_submission(result, currents, started, 22)
                    if command in {"pause", "resume"}:
                        if command == "resume":
                            self._require_same_runtime(identity, healthy=True)
                        result = await self._await_sdk(getattr(charger, command)())
                        command_receipt = await self._read_submission(result, None, started, None)
                        if command == "pause":
                            receipt = command_receipt
                    self._require_same_runtime(identity, healthy=False)
                    self.last_submission_receipt = receipt
                    return receipt
        except EaseeAdapterError as err:
            if not sdk_started:
                raise CommandNotSent(str(err), retry_after_seconds=getattr(err, "retry_after_seconds", 0.)) from None
            raise
        except Exception as err:
            if not sdk_started:
                raise CommandNotSent("local_dispatch_"+type(err).__name__.lower(), retry_after_seconds=1.) from None
            raise EaseeAdapterError(f"easee_request_failed:{type(err).__name__}") from None

    def _admit_limit(self, nonzero: bool, *, protective: bool = False, record: bool = True) -> None:
        """Reserve three SDK slots for a qualified decrease and one for zero.

        Native/manual users may share the SDK queue; this is admission for our
        own requests, not a guarantee of available cloud or device capacity.
        Zero never waits for local admission. The enclosing deadline still
        cancels an SDK-throttled zero if its queue cannot finish in time.
        """
        now = _monotonic()
        while self._request_starts and now - self._request_starts[0] > ADMISSION_PERIOD_SECONDS:
            self._request_starts.popleft()
        limit = PROTECTIVE_NONZERO_ADMISSION_LIMIT if protective else NONZERO_ADMISSION_LIMIT
        if nonzero and len(self._request_starts) >= limit:
            # Enough preceding records must age out to fall strictly below
            # this class's bound; a higher-priority burst can use reserve slots.
            release = self._request_starts[len(self._request_starts)-limit]
            delay = max(.001, release+ADMISSION_PERIOD_SECONDS-now+.001)
            raise CommandNotSent("positive_rate_capacity_exhausted", retry_after_seconds=delay)
        if record:
            self._request_starts.append(now)

    async def _await_sdk(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._pending_requests.add(task)
        try:
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._pending_requests.discard(task)

    async def _read_submission(self, response: Any, phases, started: float, command_type) -> CommandSubmission:
        """Consume only bounded correlation metadata; release on every path."""
        if response is None:
            raise EaseeAdapterError("easee_api_no_acceptance")
        accepted_at = datetime.now(timezone.utc)
        ticks = ()
        try:
            status = getattr(response, "status", None)
            if type(status) is not int or not 200 <= status < 300:
                raise EaseeAdapterError("easee_api_rejected")
            if status == 202 and command_type is not None:
                try:
                    content = getattr(response, "content", None)
                    if content is not None and callable(getattr(content, "read", None)):
                        chunks = bytearray()
                        while len(chunks) <= 8192:
                            part = await content.read(8193 - len(chunks))
                            if not part:
                                break
                            chunks.extend(part)
                        body = json.loads(chunks) if len(chunks) <= 8192 else None
                    elif callable(getattr(response, "json", None)):
                        body = await response.json()
                    else:
                        body = None
                    records = body if type(body) is list else [body]
                    accepted_ticks = []
                    for item in records[:16]:
                        if not isinstance(item, Mapping):
                            continue
                        kind = self._positive_integer(item.get("commandId", item.get("CommandId")))
                        tick = self._positive_integer(item.get("ticks", item.get("Ticks")))
                        if kind == command_type and tick is not None:
                            accepted_ticks.append(str(tick))
                    ticks = tuple(dict.fromkeys(accepted_ticks))
                except Exception:
                    # An accepted request with no valid correlation remains
                    # unverified. Never expose a JSON/error/response body.
                    ticks = ()
            return CommandSubmission(status, phases, started, _monotonic(), ticks,
                                     command_type, accepted_at)
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()
