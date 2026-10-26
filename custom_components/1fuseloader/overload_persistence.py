"""Persist and verify operational exposure through HA's public Store API.

No sensor history, credentials or household identifiers are serialized.
Downtime after overload is charged conservatively, never assumed cooling.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
from time import time
from typing import Any
from uuid import uuid4


class CheckpointVerificationError(RuntimeError):
    """The requested exposure checkpoint was not verified."""


class CheckpointGate:
    """Shared across controllers for this HA instance and ledger key."""
    def __init__(self) -> None:
        self.pending: asyncio.Task | None = None
        self.durable_hash: str | None = None
        self.verified_at: float | None = None
        self.revision: str | None = None
        self.error_type: str | None = None
        self.next_sequence = 0
        self.committed_sequence = -1
        self.highest_used_seconds = 0.0


class OverloadBudgetStore:
    def __init__(self, store: Any, clock=time, *,
                 read_back: Callable[[], Awaitable[dict | None]] | None = None,
                 pending_writes: MutableMapping[str, CheckpointGate] | None = None,
                 pending_key: str = "exposure",
                 create_task: Callable = asyncio.create_task,
                 write_allowed: Callable[[], bool] = lambda: True,
                 ledger_exists: Callable[[], Awaitable[bool | None]] | None = None) -> None:
        self.store = store
        self.clock = clock
        self._read_back = read_back or store.async_load
        gates = pending_writes if pending_writes is not None else {}
        self._gate = gates.setdefault(pending_key, CheckpointGate())
        self._create_task = create_task
        self._write_allowed = write_allowed
        self._ledger_exists = ledger_exists

    @property
    def last_verified_at(self) -> float | None:
        return self._gate.verified_at

    @property
    def last_verified_revision(self) -> str | None:
        return self._gate.revision

    @property
    def last_error_type(self) -> str | None:
        return self._gate.error_type

    @property
    def verification_pending(self) -> bool:
        return self._gate.pending is not None and not self._gate.pending.done()

    def add_pending_callback(self, callback: Callable[[asyncio.Task], None]) -> bool:
        """Observe completion without cancelling or bypassing the shared gate."""
        if not self.verification_pending:
            return False
        assert self._gate.pending is not None
        self._gate.pending.add_done_callback(callback)
        return True

    @staticmethod
    def fingerprint(value: Any) -> str:
        """Hash every field without rounding away running exposure."""
        return sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                 separators=(",", ":")).encode()).hexdigest()

    async def _wait_for_pending(self) -> None:
        while (task := self._gate.pending) is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The earlier caller records failure. A later transaction may
                # retry or load the actual older ledger, without overlap.
                pass
            finally:
                if task.done() and self._gate.pending is task:
                    self._gate.pending = None
            # Another waiter may have started a newer transaction meanwhile.

    def _start_transaction(self, coroutine) -> asyncio.Task:
        # No yield occurs after the waiter's last gate check. All reads/writes
        # for this key are serialized on HA's event loop, across reloads.
        assert self._gate.pending is None
        task = self._create_task(coroutine)
        self._gate.pending = task

        def completed(done: asyncio.Task) -> None:
            if self._gate.pending is done:
                self._gate.pending = None
            if done.cancelled():
                self._gate.durable_hash = None
            elif (error := done.exception()) is not None:
                self._gate.error_type = type(error).__name__
                self._gate.durable_hash = None

        task.add_done_callback(completed)
        return task

    async def _await_transaction(self, task: asyncio.Task):
        try:
            # Shield both the Store operation and the separate readback. A
            # bounded caller timeout cannot cancel executor I/O still running.
            return await asyncio.shield(task)
        finally:
            if task.done() and self._gate.pending is task:
                self._gate.pending = None

    async def async_load(self) -> dict[str, Any] | None:
        await self._wait_for_pending()
        return await self._await_transaction(self._start_transaction(self._load()))

    async def _load(self) -> dict[str, Any] | None:
        existed = await self._ledger_exists() if self._ledger_exists else None
        payload = await self._read_back()
        if payload is None:
            if existed:
                raise CheckpointVerificationError("Existing exposure ledger could not be loaded")
            self._gate.durable_hash = None
            return None
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                or payload["version"] != 1):
            raise ValueError("Invalid exposure envelope")
        saved_at, now = payload.get("saved_at"), self.clock()
        if (isinstance(saved_at, bool) or not isinstance(saved_at, (int, float))
                or not isfinite(saved_at) or not isfinite(now) or saved_at < 0
                or saved_at > now + 5):
            raise ValueError("Invalid exposure wall clock")
        state = payload.get("regulator")
        if not isinstance(state, dict):
            raise ValueError("Invalid exposure state")
        state = dict(state)
        used = state.get("used_seconds")
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not isfinite(used) or used < 0
                or type(state.get("exposure_active")) is not bool
                or type(state.get("minimum_held")) is not bool
                or type(state.get("transition_pending")) is not bool):
            raise ValueError("Invalid exposure accounting")
        if state["exposure_active"]:
            gap = max(0.0, now - saved_at)
            state["used_seconds"] = used + gap
            cycle = state.get("automatic_cycle")
            if cycle is not None:
                cycle_used = cycle.get("used_seconds") if isinstance(cycle, dict) else None
                if (isinstance(cycle_used, bool) or not isinstance(cycle_used, (int, float))
                        or not isfinite(cycle_used) or cycle_used < 0):
                    raise ValueError("Invalid automatic cycle exposure accounting")
                # Production import receives this already charged snapshot.
                # Detach the nested state so the durable fingerprint below
                # still describes the ORIGINAL on-disk checkpoint. A later
                # verified save must commit both conservative counters.
                state["automatic_cycle"] = {**cycle, "used_seconds": cycle_used + gap}
        self._gate.highest_used_seconds = max(self._gate.highest_used_seconds, state["used_seconds"])
        self._gate.durable_hash = self.fingerprint(payload["regulator"])
        self._gate.revision = payload.get("checkpoint_revision")
        self._gate.error_type = None
        return state

    def state_is_durable(self, state: dict[str, Any]) -> bool:
        return (self._gate.pending is None
                and state["used_seconds"] >= self._gate.highest_used_seconds
                and self._gate.durable_hash == self.fingerprint(state))

    async def _write_verified(self, state: dict[str, Any], state_hash: str,
                              saved_at: float, sequence: int) -> None:
        if not self._write_allowed():
            raise CheckpointVerificationError("Storage is stopping or unavailable")
        if (sequence < self._gate.committed_sequence
                or state["used_seconds"] < self._gate.highest_used_seconds):
            raise CheckpointVerificationError("An older exposure snapshot cannot replace a newer checkpoint")
        revision = uuid4().hex
        payload = {"version":1, "saved_at":saved_at,
                   "checkpoint_revision":revision, "regulator":state}
        expected_hash = self.fingerprint(payload)
        await self.store.async_save(payload)
        # HA Store may log and swallow write errors. Production supplies a
        # fresh public read-only Store, not this writer's pending in-memory
        # data. The unique revision rejects stale/deferred matching payloads.
        actual = await self._read_back()
        if self.fingerprint(actual) != expected_hash:
            raise CheckpointVerificationError("Exposure checkpoint readback did not match")
        self._gate.durable_hash = state_hash
        self._gate.verified_at = self.clock()
        self._gate.revision = revision
        self._gate.error_type = None
        self._gate.committed_sequence = sequence
        self._gate.highest_used_seconds = max(self._gate.highest_used_seconds, state["used_seconds"])

    async def async_save(self, state: dict[str, Any]) -> bool:
        snapshot = deepcopy(state)
        # Anchor wall time before any gate wait. A queued old snapshot must
        # not acquire a later wall clock and refund its unaccounted wait.
        saved_at = self.clock()
        sequence = self._gate.next_sequence
        self._gate.next_sequence += 1
        state_hash = self.fingerprint(snapshot)
        await self._wait_for_pending()
        if (sequence < self._gate.committed_sequence
                or snapshot["used_seconds"] < self._gate.highest_used_seconds):
            raise CheckpointVerificationError("Exposure checkpoint would refund known usage")
        if self._gate.durable_hash == state_hash:
            return False
        await self._await_transaction(
            self._start_transaction(self._write_verified(snapshot, state_hash, saved_at, sequence)))
        return True

    async def async_wait_pending(self) -> None:
        await self._wait_for_pending()
