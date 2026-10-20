"""Narrow compatibility adapter for the audited Easee/pyeasee versions.

Easee 0.9.74 drops identical service calls before they reach pyeasee, so its
services cannot renew a finite circuit TTL. Reuse its authenticated public
pyeasee objects instead. Never read, copy, log, or persist their credentials.
An HTTP acceptance is submission to the cloud, not physical charger feedback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil, isfinite
import re
import sys
from typing import Any

SUPPORTED_EASEE_VERSION = "0.9.74"
SUPPORTED_PYEASEE_VERSION = "0.8.17"
REQUEST_TIMEOUT_SECONDS = 10
MAX_TELEMETRY_AGE_SECONDS = 120
OBSERVATION_FUTURE_TOLERANCE_SECONDS = 2
SNAPSHOT_OBSERVATION_IDS = (109, 182, 183, 184, 185)

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

    async def async_initialize(self) -> None:
        """Validate already loaded objects without making any network request."""
        self._resolve_runtime()

    @property
    def control_block_reason(self) -> str | None:
        """Recheck on every use so an Easee reload cannot leave stale objects."""
        return self._resolve_runtime()

    def _resolve_runtime(self) -> str | None:
        self._charger = self._circuit = self._product_data = None
        self._controller = self._registry_device = None
        # Do not import/start Easee or pyeasee ourselves. after_dependencies
        # ensures normal setup order; a missing integration stays blocked.
        easee_const = sys.modules.get("custom_components.easee.const")
        pyeasee = sys.modules.get("pyeasee")
        if getattr(easee_const, "VERSION", None) != SUPPORTED_EASEE_VERSION:
            return "unsupported_or_unloaded_easee_version"
        if getattr(pyeasee, "__version__", None) != SUPPORTED_PYEASEE_VERSION:
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
        except Exception:
            # Exception text can include a remote URL or private account data.
            return "easee_runtime_incompatible"
        return None

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

    def _require_same_runtime(self, identity: tuple[Any, ...], *, healthy: bool) -> None:
        """Reject results or follow-up commands across an integration reload."""
        problem = self.telemetry_block_reason if healthy else self._resolve_runtime()
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
    def telemetry_block_reason(self) -> str | None:
        """Use stream health, since unchanged status/current entities can be old.

        Easee sets latestPulse locally when ProductUpdate data arrives. It is
        not the transport's own ping clock or a physical acquisition timestamp.
        Missing recent product data conservatively blocks positive commands.
        """
        problem = self._resolve_runtime()
        if problem:
            return problem
        state = self._product_data.state
        if state is None:
            return "easee_telemetry_missing"
        try:
            if state.get("signalRConnected") is not True:
                return "easee_stream_disconnected"
            if state.get("isOnline") is not True:
                return "easee_charger_offline"
            pulse = state.get("latestPulse")
            if not isinstance(pulse, datetime) or pulse.tzinfo is None:
                return "easee_heartbeat_missing"
            age = (datetime.now(timezone.utc) - pulse).total_seconds()
            if age < -5 or age > MAX_TELEMETRY_AGE_SECONDS:
                return "easee_heartbeat_stale"
        except (AttributeError, TypeError, ValueError, OverflowError):
            return "easee_telemetry_invalid"
        return None

    async def async_execute(self, decision: Any) -> None:
        """Submit a limit or pause/resume, raising unless the API accepted it.

        No deduplication: repeating the same circuit limit must renew the TTL.
        Unknown versions never fall back to the silently swallowing HA service.
        """
        problem = self._resolve_runtime()
        if problem:
            raise EaseeAdapterError(problem)
        command = decision.command
        if command in {"set_limit", "resume"}:
            currents = (decision.current_p1, decision.current_p2, decision.current_p3)
            if any(type(value) is not int or not 0 <= value <= 40 for value in currents):
                raise EaseeAdapterError("invalid_phase_current")
            ttl = decision.ttl_seconds
            if (
                isinstance(ttl, bool)
                or not isinstance(ttl, (int, float))
                or not isfinite(ttl)
                or not 0 < ttl <= 1080 * 60
            ):
                raise EaseeAdapterError("finite_ttl_required")
            ttl_minutes = ceil(ttl / 60)
            if command == "resume" and (currents[0] < 6 or currents[1:] != (0, 0)):
                raise EaseeAdapterError("resume_requires_safe_l1_cap")
            if any(currents) or command == "resume":
                health_problem = self.telemetry_block_reason
                if health_problem:
                    raise EaseeAdapterError(health_problem)
        elif command != "pause":
            raise EaseeAdapterError("unsupported_easee_command")

        # A concurrent state read can resolve replacement objects while this
        # command awaits the cloud. Keep the whole sequence on its original
        # charger and never resume a replacement after capping the old circuit.
        identity = self._runtime_identity()
        charger, circuit = self._charger, self._circuit

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                if command in {"set_limit", "resume"}:
                    result = await circuit.set_dynamic_current(*currents, ttl_minutes)
                    self._require_accepted(result)
                if command in {"pause", "resume"}:
                    # Reapply a safe finite cap immediately before resume. A
                    # failed/unknown cap submission must never be followed by
                    # resume, even if an earlier tick submitted a cap.
                    if command == "resume":
                        self._require_same_runtime(identity, healthy=True)
                    result = await getattr(charger, command)()
                    self._require_accepted(result)
        except EaseeAdapterError:
            raise
        except Exception as err:
            raise EaseeAdapterError(f"easee_request_failed:{type(err).__name__}") from None

    @staticmethod
    def _require_accepted(response: Any) -> None:
        # Public pyeasee methods return None after a swallowed server error.
        # Require a real successful HTTP response and release its connection.
        if response is None:
            raise EaseeAdapterError("easee_api_no_acceptance")
        try:
            status = getattr(response, "status", None)
            if type(status) is not int or not 200 <= status < 300:
                raise EaseeAdapterError("easee_api_rejected")
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()
