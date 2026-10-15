"""Narrow compatibility adapter for the audited Easee/pyeasee versions.

Easee 0.9.74 drops identical service calls before they reach pyeasee, so its
services cannot renew a finite circuit TTL. Reuse its authenticated public
pyeasee objects instead. Never read, copy, log, or persist their credentials.
An HTTP acceptance is submission to the cloud, not physical charger feedback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from math import ceil, isfinite
import sys
from typing import Any

SUPPORTED_EASEE_VERSION = "0.9.74"
SUPPORTED_PYEASEE_VERSION = "0.8.17"
REQUEST_TIMEOUT_SECONDS = 10
MAX_TELEMETRY_AGE_SECONDS = 120


class EaseeAdapterError(RuntimeError):
    """A safe, credential-free failure reported by the adapter."""


class EaseeCommandAdapter:
    """Version-gated access to the charger selected in the HA device registry."""

    def __init__(self, hass: Any, device_id: str) -> None:
        self.hass = hass
        self.device_id = device_id
        self._charger: Any = None
        self._circuit: Any = None
        self._product_data: Any = None

    async def async_initialize(self) -> None:
        """Validate already loaded objects without making any network request."""
        self._resolve_runtime()

    @property
    def control_block_reason(self) -> str | None:
        """Recheck on every use so an Easee reload cannot leave stale objects."""
        return self._resolve_runtime()

    def _resolve_runtime(self) -> str | None:
        self._charger = self._circuit = self._product_data = None
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
        except Exception:
            # Exception text can include a remote URL or private account data.
            return "easee_runtime_incompatible"
        return None

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

        Easee sets latestPulse locally whenever a SignalR message arrives. This
        is a connection/stream heartbeat, not proof of each physical sample.
        Missing heartbeat conservatively prevents an increase or resume.
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

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                if command in {"set_limit", "resume"}:
                    result = await self._circuit.set_dynamic_current(*currents, ttl_minutes)
                    self._require_accepted(result)
                if command in {"pause", "resume"}:
                    # Reapply a safe finite cap immediately before resume. A
                    # failed/unknown cap submission must never be followed by
                    # resume, even if an earlier tick submitted a cap.
                    result = await getattr(self._charger, command)()
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
