"""Bounded command receipts, without credentials or physical-response claims.

Vendor clocks are not compared with the local request clock. Only exact
command-type/Ticks correlation can attach a delivery event to a submission.
Old events may explain history, but never confirm a newer limit or its TTL.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _ticks(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        return None
    return text if 0 < int(text) < 2**63 else None


@dataclass(slots=True)
class SubmissionRecord:
    sequence: int
    command: str
    phases: tuple[int, int, int]
    started: float
    state: str = "pending_http"
    completed: float | None = None
    http_status: int | None = None
    correlations: tuple[tuple[int, str], ...] = ()
    vendor_accepted: bool | None = None
    delivery_received: float | None = None
    readback_matches: bool | None = None
    readback_after_submission: bool = False
    superseded: bool = False

    def public(self) -> dict[str, Any]:
        # Deliberately omit correlation values and all product identifiers.
        return {"sequence": self.sequence, "command": self.command,
                "requested_phases": list(self.phases), "state": self.state,
                "http_status": self.http_status,
                "correlation_available": bool(self.correlations),
                "vendor_accepted": self.vendor_accepted,
                "delivery_received": self.delivery_received is not None,
                "readback_matches": self.readback_matches,
                "readback_after_submission": self.readback_after_submission,
                "superseded": self.superseded,
                "physical_response_verified": False, "device_ttl_verified": False}


class CommandDeliveryTracker:
    """One controller generation's bounded diagnostic command ledger."""

    def __init__(self) -> None:
        self._sequence = 0
        self.records: deque[SubmissionRecord] = deque(maxlen=32)
        self._early: deque[tuple[float, int, str, bool]] = deque(maxlen=16)
        self._lost_unresolved_zero = False

    def reset(self) -> None:
        self.records.clear()
        self._early.clear()
        self._lost_unresolved_zero = False

    @property
    def latest(self) -> SubmissionRecord | None:
        return self.records[-1] if self.records else None

    def begin(self, command: str, phases: tuple[int, int, int], now: float) -> SubmissionRecord:
        if self.latest is not None:
            self.latest.superseded = True
        self._sequence += 1
        if len(self.records) == self.records.maxlen:
            evicted = self.records[0]
            if evicted.phases == (0, 0, 0) and evicted.correlations and evicted.vendor_accepted is None:
                self._lost_unresolved_zero = True
        record = SubmissionRecord(self._sequence, command, phases, now)
        self.records.append(record)
        return record

    def accepted(self, record: SubmissionRecord, receipt: Any, now: float) -> None:
        if record not in self.records or record.state != "pending_http":
            return
        record.completed = now
        record.state = "http_accepted_unverified"
        status = _get(receipt, "http_status")
        record.http_status = status if type(status) is int and 200 <= status < 300 else None
        ids = _get(receipt, "correlation_ids", ()) or ()
        correlations = []
        for item in ids if isinstance(ids, (tuple, list)) else ():
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            kind, raw = item
            ticks = _ticks(raw)
            if type(kind) is int and kind >= 0 and ticks is not None:
                key = (kind, ticks)
                # A reused vendor ID is ambiguous, even if its arrival time
                # resembles this request. It must not confirm a new limit.
                if not any(key in previous.correlations for previous in self.records if previous is not record):
                    correlations.append(key)
        record.correlations = tuple(correlations[:8])
        for received, kind, ticks, accepted in list(self._early):
            if record.started <= received <= now and (kind, ticks) in record.correlations:
                self._apply(record, accepted, received)
        self._expire(now)

    def failed(self, record: SubmissionRecord, *, superseded: bool = False) -> None:
        if record not in self.records:
            return
        record.state = "superseded_unverified" if superseded else "submission_failed_unverified"
        record.superseded = record.superseded or superseded

    def delivery(self, event: Any, now: float) -> bool:
        kind, ticks, accepted = (_get(event, "command_type"),
                                 _ticks(_get(event, "ticks")), _get(event, "was_accepted"))
        if type(kind) is not int or ticks is None or type(accepted) is not bool:
            return False
        self._expire(now)
        for record in reversed(self.records):
            if (kind, ticks) in record.correlations:
                if record.state in {"submission_failed_unverified", "superseded_unverified"}:
                    return False
                self._apply(record, accepted, now)
                return record is self.latest
        self._early.append((now, kind, ticks, accepted))
        return False

    @staticmethod
    def _apply(record: SubmissionRecord, accepted: bool, now: float) -> None:
        # A duplicate or conflicting later delivery cannot turn rejection into
        # acceptance or renew the local delivery receipt clock.
        if record.vendor_accepted is not None:
            return
        record.vendor_accepted = accepted
        record.delivery_received = now
        record.state = "vendor_accepted_unverified" if accepted else "vendor_rejected"

    def _expire(self, now: float) -> None:
        while self._early and now - self._early[0][0] > 60:
            self._early.popleft()

    def observe(self, phases: tuple[float, float, float] | None, after_submission: bool) -> None:
        record = self.latest
        if record is None:
            return
        record.readback_matches = None if phases is None else phases == record.phases
        record.readback_after_submission = bool(phases is not None and after_submission)

    def can_skip_zero(self, *, phases: tuple[float, float, float] | None,
                      after_submission: bool, report_age: float | None,
                      ev_current: Any, healthy: bool, offline_zero: bool,
                      max_age: float) -> bool:
        record = self.latest
        if (record is None or record.phases != (0, 0, 0) or record.command != "set_limit"
                or record.state not in {"http_accepted_unverified", "vendor_accepted_unverified"}
                or record.vendor_accepted is False or not healthy or not offline_zero
                or (record.correlations and record.vendor_accepted is not True)
                or phases != (0, 0, 0) or not after_submission
                or report_age is None or not 0 <= report_age <= max_age):
            return False
        try:
            return not isinstance(ev_current, bool) and isfinite(float(ev_current)) and 0 <= float(ev_current) <= .5
        except (TypeError, ValueError, OverflowError):
            return False

    def diagnostics(self) -> dict[str, Any]:
        return {"command_delivery": self.latest.public() if self.latest else None,
                "recent_command_deliveries": [record.public() for record in self.records][-8:],
                "unresolved_correlated_zero": self.unresolved_correlated_zero,
                "delivery_scope": "exact_vendor_command_correlation_and_local_receipts_not_physical_response_or_TTL"}

    @property
    def unresolved_correlated_zero(self) -> bool:
        return self._lost_unresolved_zero or any(
            record.phases == (0, 0, 0) and record.correlations and record.vendor_accepted is None
            for record in self.records)
