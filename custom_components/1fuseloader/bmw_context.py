"""Read-only, optional BMW CarData context for diagnostics.

CarData is cloud telemetry, so none of these fields belongs in the current
regulator's protective feedback loop. Registry matching uses CarData's public
descriptor tail, never a vehicle name, VIN, or installation-specific entity ID.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from homeassistant.helpers import entity_registry as er


# Descriptor order gives genuine BMW displayed/HV SoC priority over estimates.
# Do not include CarData's locally predicted SOC or inferred battery capacity.
FIELD_DESCRIPTORS: dict[str, tuple[str, ...]] = {
    "charging_state": ("vehicle.drivetrain.electricEngine.charging.status",),
    "plug_state": (
        "vehicle.body.chargingPort.status",
        "vehicle.powertrain.tractionBattery.charging.port.anyPosition.isPlugged",
    ),
    "phase_count": ("vehicle.drivetrain.electricEngine.charging.phaseNumber",),
    "ac_current": ("vehicle.drivetrain.electricEngine.charging.acAmpere",),
    "ac_voltage": ("vehicle.drivetrain.electricEngine.charging.acVoltage",),
    "state_of_charge": (
        "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
        "vehicle.drivetrain.batteryManagement.header",
    ),
    "end_reason": (
        "vehicle.drivetrain.electricEngine.charging.hvpmFinishReason",
        "vehicle.drivetrain.electricEngine.charging.reasonChargingEnd",
    ),
    "last_result": ("vehicle.drivetrain.electricEngine.charging.lastChargingResult",),
}

# Explicit advisory freshness, independent of the regulator's local sensor age.
# An event reason can legitimately be old; it remains visible but is marked stale.
MAX_AGE_SECONDS = {key: 120 for key in FIELD_DESCRIPTORS}
MAX_AGE_SECONDS.update(state_of_charge=3600, end_reason=3600, last_result=3600)
ACTIVE_CHARGING_STATES = {"CHARGINGACTIVE", "CHARGING_ACTIVE", "CHARGING", "CHARGING_IN_PROGRESS"}
CONNECTED_PLUG_STATES = {"CONNECTED", "ON", "TRUE", "1"}


def _empty(reason: str) -> dict[str, Any]:
    return {
        "value": None, "source_timestamp": None, "age_seconds": None,
        "available": False, "fresh": False, "reason": reason,
    }


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # An offset-free value cannot establish the age of cloud telemetry safely.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _value(field: str, raw: str) -> Any:
    if field == "phase_count":
        return {"1": 1, "2": 2, "3": 3, "1-PHASES": 1, "2-PHASES": 2, "3-PHASES": 3}.get(raw.upper())
    if field in {"ac_current", "ac_voltage", "state_of_charge"}:
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        maximum = {"ac_current": 1000, "ac_voltage": 1000, "state_of_charge": 100}[field]
        return value if isfinite(value) and 0 <= value <= maximum else None
    # Source enum strings only; no source attributes or generated identifiers.
    return raw if raw and len(raw) <= 80 else None


class BMWContext:
    """Expose a bounded, advisory snapshot without polling or writing to BMW."""

    def __init__(self, hass, device_id: str | None) -> None:
        self._hass = hass
        self._device_id = device_id

    def _read(self, field: str, entity_id: str, now: datetime) -> dict[str, Any]:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            return _empty("unavailable")
        if state.attributes.get("restored"):
            return _empty("restored")
        value = _value(field, state.state)
        if value is None:
            return _empty("invalid_value")
        source_time = _timestamp(state.attributes.get("timestamp"))
        if source_time is None:
            return _empty("missing_source_timestamp")
        age = (now - source_time).total_seconds()
        if age < -5:
            return {**_empty("future_source_timestamp"), "source_timestamp": source_time.isoformat()}
        age = round(max(0.0, age), 1)
        fresh = age <= MAX_AGE_SECONDS[field]
        return {
            "value": value, "source_timestamp": source_time.isoformat(),
            "age_seconds": age, "available": True, "fresh": fresh,
            "reason": "fresh" if fresh else "stale",
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return eight selected values and per-field BMW source freshness.

        Values from another device/integration, disabled entities and locally
        predicted SoC cannot enter this snapshot. No registry identifier escapes.
        """
        if not self._device_id:
            return {field: _empty("not_configured") for field in FIELD_DESCRIPTORS}
        registry = er.async_get(self._hass)
        entries = er.async_entries_for_device(registry, self._device_id)
        entries = [entry for entry in entries if entry.platform == "cardata"
                   and not entry.disabled_by and entry.entity_id.split(".", 1)[0] in {"sensor", "binary_sensor"}]
        now = datetime.now(timezone.utc)
        result = {}
        for field, descriptors in FIELD_DESCRIPTORS.items():
            candidates = [
                self._read(field, entry.entity_id, now)
                for descriptor in descriptors
                for entry in entries
                if isinstance(entry.unique_id, str) and entry.unique_id.endswith("_" + descriptor)
            ]
            result[field] = next((item for item in candidates if item["fresh"]),
                                 next((item for item in candidates if item["available"]),
                                      candidates[0] if candidates else _empty("not_found")))
        # BMW can leave a phase count behind after disconnecting, including a
        # count of three. It describes this session only when independent fresh
        # descriptors agree that a plugged-in car is drawing charging current.
        # This qualification is diagnostic and never drives pause/resume.
        state, plug, current = (result[key] for key in ("charging_state", "plug_state", "ac_current"))
        active_session = (
            state["fresh"] and str(state["value"]).upper() in ACTIVE_CHARGING_STATES
            and plug["fresh"] and str(plug["value"]).upper() in CONNECTED_PLUG_STATES
            and current["fresh"] and current["value"] is not None and current["value"] > 0
        )
        if result["phase_count"]["available"] and not active_session:
            result["phase_count"]["fresh"] = False
            result["phase_count"]["reason"] = "not_active_charging_session"
        return result
