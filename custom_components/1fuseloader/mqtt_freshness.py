"""Validated Currently One L1 samples through HA's public MQTT API.

HA MQTT can suppress state writes when the decoded current is unchanged. An
entity's last_reported is therefore not a message receipt clock. This optional
adapter observes the selected source itself without changing MQTT discovery or
force_update. Only I1 and its configured UTC sample timestamp are retained.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isfinite
import re
from typing import Any


MAX_PAYLOAD_BYTES = 65536
FUTURE_TOLERANCE_SECONDS = 5


@dataclass(frozen=True, slots=True)
class MqttL1Config:
    entity_id: str
    topic: str
    current_key: str
    timestamp_key: str

    @classmethod
    def from_registry(cls, entity_id: str, registry_entry: Any,
                      device_entry: Any) -> MqttL1Config | None:
        """Bind the verified vendor protocol using both public registries.

        The initial adapter supports Currently One only. An arbitrary entity ID
        or a topic from an unrelated device must never enable this path. Other
        sources keep the existing HA entity report-age policy.
        """
        if (registry_entry is None or registry_entry.platform != "mqtt"
                or registry_entry.entity_id != entity_id
                or getattr(registry_entry, "disabled_by", None)):
            return None
        identity = re.fullmatch(r"I1_([0-9a-fA-F]{12})", str(registry_entry.unique_id))
        if (identity is None or device_entry is None
                or registry_entry.device_id != device_entry.id
                or getattr(device_entry, "disabled_by", None)
                or device_entry.manufacturer != "Stopgap AB"
                or device_entry.model != "Currently One"
                or ("mqtt", f"C1_{identity.group(1)}") not in device_entry.identifiers):
            return None
        return cls(entity_id, f"currently/one/{identity.group(1)}/state", "I1", "sec")


@dataclass(frozen=True, slots=True)
class MqttL1Snapshot:
    current: float | None
    report_ts: float | None
    receipt_ts: float | None
    sample_ts: float | None
    error: str | None


class MqttL1Source:
    """Memory-only source state; every accepted frame is independently checked."""

    def __init__(self, config: MqttL1Config, *, monotonic: Callable[[], float],
                 utc_now: Callable[[], datetime], age_limit: Callable[[], float],
                 on_change: Callable[[], None]) -> None:
        self.config = config
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._age_limit = age_limit
        self._on_change = on_change
        self._active = False
        self._connected = False
        self._unsub_messages: Callable[[], None] | None = None
        self._unsub_connection: Callable[[], None] | None = None
        self._current: float | None = None
        self._receipt_ts: float | None = None
        self._receipt_monotonic: float | None = None
        self._sample_ts: float | None = None
        self._sequence_high_water: float | None = None
        self._error: str | None = "L1 MQTT source is waiting for a fresh non-retained sample."

    async def async_start(self, hass: Any) -> None:
        # Lazy imports keep MQTT optional and the pure validation testable.
        from homeassistant.components import mqtt
        from homeassistant.core import callback

        self._active = True

        @callback
        def message_received(message: Any) -> None:
            self.receive(message)

        @callback
        def connection_changed(connected: bool) -> None:
            self.connection_changed(connected)

        try:
            self._connected = mqtt.is_connected(hass)
            self._unsub_connection = mqtt.async_subscribe_connection_status(hass, connection_changed)
            unsubscribe = await mqtt.async_subscribe(
                hass, self.config.topic, message_received, qos=0, encoding="utf-8",
            )
            # Unload can run while a subscription coroutine is yielding.
            if not self._active:
                unsubscribe()
                return
            self._unsub_messages = unsubscribe
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Reject queued callbacks and remove both listeners on every exit."""
        self._active = False
        self._connected = False
        for name in ("_unsub_messages", "_unsub_connection"):
            unsubscribe = getattr(self, name)
            setattr(self, name, None)
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception:
                    # One failed cleanup must not leave the other callback live.
                    pass
        self._clear("L1 MQTT receipt source is stopped.", notify=False)

    def connection_changed(self, connected: bool) -> None:
        if not self._active:
            return
        self._connected = bool(connected)
        # Reconnection itself is not evidence that an I1 sample arrived.
        self._clear("L1 MQTT source is waiting for a fresh non-retained sample."
                    if connected else "L1 MQTT connection is unavailable.")

    def _clear(self, error: str, *, notify: bool = True) -> None:
        self._current = None
        self._receipt_ts = None
        self._receipt_monotonic = None
        self._sample_ts = None
        self._error = error
        if notify and self._active:
            self._on_change()

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        number = float(value)
        if not isfinite(number):
            raise ValueError
        return number

    def receive(self, message: Any) -> None:
        """Read only the configured current and UTC timestamp from one frame."""
        if not self._active or not self._connected or message.topic != self.config.topic:
            return
        # Broker retained replay cannot establish or extend live source health.
        if getattr(message, "retain", True):
            return
        try:
            payload = message.payload
            if (not isinstance(payload, str) or len(payload) > MAX_PAYLOAD_BYTES
                    or len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES):
                raise ValueError
            frame = json.loads(payload)
            if not isinstance(frame, dict):
                raise ValueError
            current = self._number(frame[self.config.current_key])
            sample_ts = self._number(frame[self.config.timestamp_key])
            if not 0 <= current <= 1000:
                raise ValueError
            receipt_ts = self._utc_now().timestamp()
            sample_age = receipt_ts - sample_ts
            if not -FUTURE_TOLERANCE_SECONDS <= sample_age <= self._age_limit():
                self._clear("L1 MQTT sample timestamp is stale or invalid.")
                return
            if self._sequence_high_water is not None and sample_ts <= self._sequence_high_water:
                if sample_ts == self._sequence_high_water and current == self._current:
                    return  # Duplicate QoS/replay does not renew receipt or grace.
                self._clear("L1 MQTT sample timestamp did not advance consistently.")
                return
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            self._clear("L1 MQTT sample is missing or has invalid current/timestamp fields.")
            return
        self._current = current
        self._sample_ts = sample_ts
        self._sequence_high_water = sample_ts
        self._receipt_ts = receipt_ts
        self._receipt_monotonic = self._monotonic()
        self._error = None
        self._on_change()

    def snapshot(self) -> MqttL1Snapshot:
        error = self._error
        if not self._active or not self._connected:
            error = "L1 MQTT connection or receipt subscription is unavailable."
        elif error is None:
            receipt_age = self._monotonic() - self._receipt_monotonic
            sample_age = self._utc_now().timestamp() - self._sample_ts
            if not 0 <= receipt_age <= self._age_limit():
                error = "L1 MQTT receipt is stale or has an invalid timestamp."
            elif not -FUTURE_TOLERANCE_SECONDS <= sample_age <= self._age_limit():
                error = "L1 MQTT sample timestamp is stale or invalid."
        # The earlier clock is conservative for cross-source alignment; a
        # slightly future meter clock cannot claim a post-EV sample prematurely.
        report_ts = (min(self._sample_ts, self._receipt_ts)
                     if error is None else None)
        return MqttL1Snapshot(
            self._current if error is None else None, report_ts,
            self._receipt_ts, self._sample_ts, error,
        )
