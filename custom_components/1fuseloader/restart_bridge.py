"""One finite, explicit graceful-shutdown allowance, never a restored lease.

This option permits a short blind interval. It is not active fuse protection
while HA is stopped, a physical TTL certificate, or overload permission.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import re
from uuid import uuid4

from .logic import ElectricalStopCause, RegulatorDecision
from .easee_adapter import CommandNotSent

BRIDGE_CAP_AMPS = 7
BRIDGE_TTL_SECONDS = 300
PREPARE_TIMEOUT_SECONDS = 16
STORE_TIMEOUT_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 6
FIRST_GRID_WAIT = "L1 MQTT source is waiting for a fresh non-retained sample."
_STATES = {"prepared", "acknowledged", "claimed", "completed", "aborted"}
_ALLOWED = {"charging", "awaiting_start", "ready_to_charge", "start_charging", "awaiting_load_balancing"}


class _BridgeGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = None


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class BridgeRefused(RuntimeError):
    """Fixed, private-data-free operational refusal."""


class GracefulRestartBridge:
    def __init__(self, controller, *, store=None, read_back=None):
        self.c = controller
        self.storage_key = f"1fuseloader.{controller.entry.entry_id}.restart_bridge"
        runtime_data = getattr(controller.hass, "data", None)
        if runtime_data is None and getattr(controller.settings, "graceful_restart_bridge", False):
            runtime_data = {}
            controller.hass.data = runtime_data
        gates = (runtime_data.setdefault("1fuseloader_restart_bridge_gates", {})
                 if runtime_data is not None else {})
        self._gate = gates.setdefault(controller.entry.entry_id, _BridgeGate())
        self._store_lock = self._gate.lock
        self._store = store
        self._read_back = read_back
        self._marker = None
        self._active = False
        self._preparing = False
        self._armed = False
        self._abort = False
        self._abort_event = asyncio.Event()
        self._grant_guard = {"valid": True}
        self._request_task = None
        self._prepare_binding = None
        self._prepare_generation = None
        self._closed = False
        self._restored = False
        self._planner = None
        self._expires_mono = None
        self._last_wall = None
        self._last_mono = None
        self._first_fresh = False
        self._actuator_ready_seen = False
        self._unknown_until = None
        self._background_save = None
        self._last_dispatch = None
        self._bridge_dispatch = None
        self._phase = "disabled"
        self._error = None

    @property
    def preparing(self):
        return self._preparing

    @property
    def armed(self):
        return self._armed

    def _enabled(self):
        s = self.c.settings
        return bool(getattr(s, "graceful_restart_bridge", False) is True
            and s.meter_envelope_control and s.nominal_import_only_fallback
            and s.active_control and not s.dry_run and s.enable_dynamic_limit
            and s.main_fuse_amps == 25 and s.buffer_amps == 2
            and s.min_charge_amps <= 7 <= s.max_charge_amps <= 16)

    def _clocks(self, inp=None):
        inp = inp if inp is not None else self.c._read_input()
        wall, mono = _number(inp.wall_now_ts), _number(self.c.hass.loop.time())
        if wall is None or mono is None or wall <= 0:
            raise BridgeRefused("bridge_clock_invalid")
        return wall, mono

    def _binding(self):
        # This integration's entry contains selected sources, not credentials.
        # Store only a digest, not household/device identifiers.
        adapter = self.c._adapter
        circuit = getattr(adapter, "_circuit", None)
        configured = self.c._configured_control_identity()
        if configured is None:
            raise BridgeRefused("bridge_binding_unverified")
        selection = self.c._electrical_selection()
        source = getattr(self.c, "_l1_source_config", None)
        payload = dict(entry=self.c.entry.entry_id, data=self.c.entry.data,
            settings=asdict(self.c.settings), circuit=getattr(circuit, "id", None),
            configured=configured, selection=selection,
            mqtt=asdict(source) if source is not None else None)
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
            allow_nan=False, separators=(",", ":")).encode()).hexdigest()

    def _ensure_store(self):
        if self._store is not None:
            return
        from homeassistant.helpers.storage import Store
        self._store = Store(self.c.hass, 1, self.storage_key)
        self._read_back = lambda: Store(self.c.hass, 1, self.storage_key, read_only=True).async_load()

    async def _read_raw(self):
        self._ensure_store()
        reader = self._read_back or self._store.async_load
        return await reader()

    async def _wait_pending(self):
        while (pending := self._gate.pending) is not None:
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if pending.done() and self._gate.pending is pending:
                    self._gate.pending = None

    async def _load(self):
        await self._wait_pending()
        return await asyncio.wait_for(self._read_raw(), STORE_TIMEOUT_SECONDS)

    async def _write_verified(self, snapshot, grant_guard):
        await self._store.async_save(snapshot)
        if await self._read_raw() != snapshot:
            raise BridgeRefused("bridge_checkpoint_not_verified")
        if snapshot["state"] == "acknowledged" and (
                self._abort or self._closed or not grant_guard["valid"]):
            # A caller timeout cannot leave a late eligible grant behind. This
            # tombstone is part of the same tracked, shielded transaction;
            # another controller must await it before any load or claim.
            try:
                wall, _ = self._clocks()
            except BridgeRefused:
                wall = max(snapshot["prepared_wall"], self._last_wall or snapshot["prepared_wall"])
            snapshot = {**snapshot, "state": "aborted", "ended_wall": wall,
                        "revision": uuid4().hex}
            await self._store.async_save(snapshot)
            if await self._read_raw() != snapshot:
                raise BridgeRefused("bridge_abort_not_verified")
        return snapshot

    async def _save_locked(self, snapshot):
        await self._wait_pending()
        create = getattr(self.c.hass, "async_create_task", asyncio.create_task)
        grant_guard = self._grant_guard
        task = create(self._write_verified(snapshot, grant_guard))
        self._gate.pending = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), STORE_TIMEOUT_SECONDS)
        except BaseException:
            if snapshot["state"] == "acknowledged":
                self._abort = True
                grant_guard["valid"] = False
            raise

    async def _save(self, marker, *, expected_nonce=None):
        self._ensure_store()
        snapshot = deepcopy(marker)
        snapshot["revision"] = uuid4().hex
        async with self._store_lock:
            await self._wait_pending()
            if expected_nonce is not None or snapshot["state"] == "acknowledged":
                current = await self._read_raw()
                expected = expected_nonce or snapshot["nonce"]
                if (type(current) is not dict or current.get("nonce") != expected
                        or snapshot["state"] == "acknowledged" and current.get("state") != "prepared"
                        or snapshot["state"] in {"completed", "aborted"}
                           and current.get("state") in {"completed", "aborted"}
                           and current["state"] != snapshot["state"]):
                    if snapshot["state"] == "acknowledged":
                        raise BridgeRefused("bridge_prepared_owner_changed")
                    return None
            result = await self._save_locked(snapshot)
        self._marker = result
        if snapshot["state"] == "acknowledged" and result["state"] != "acknowledged":
            raise BridgeRefused("bridge_grant_aborted")
        return result

    def _valid_marker(self, marker, wall):
        required = {"version", "state", "nonce", "binding", "cap", "ttl_seconds",
            "prepared_wall", "dispatch_wall", "expires_wall", "possible_until_wall",
            "physical_response_wall", "grid_response_wall", "claimed_wall", "ended_wall",
            "unobserved_seconds", "revision"}
        if (type(marker) is not dict or set(marker) != required
                or type(marker["version"]) is not int or marker["version"] != 1
                or type(marker["state"]) is not str or marker["state"] not in _STATES
                or not isinstance(marker["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", marker["nonce"]) is None
                or not isinstance(marker["revision"], str) or re.fullmatch(r"[0-9a-f]{32}", marker["revision"]) is None
                or marker["binding"] != self._binding()
                or type(marker["cap"]) is not int or marker["cap"] != 7
                or type(marker["ttl_seconds"]) is not int or marker["ttl_seconds"] != 300):
            return False
        prepared, expiry, possible = map(_number, (marker["prepared_wall"], marker["expires_wall"], marker["possible_until_wall"]))
        if (prepared is None or prepared <= 0 or prepared > wall
                or expiry is None or possible is None
                or not prepared < expiry <= possible <= prepared + 316
                or _number(marker["unobserved_seconds"]) is None
                or not 0 <= marker["unobserved_seconds"] <= 316):
            return False
        if marker["state"] in {"acknowledged", "claimed", "completed"}:
            dispatch, physical, grid = map(_number, (marker["dispatch_wall"], marker["physical_response_wall"], marker["grid_response_wall"]))
            if (dispatch is None or physical is None or grid is None
                    or not prepared <= dispatch < physical < grid <= wall
                    or expiry != dispatch + 300):
                return False
        for key in ("dispatch_wall", "physical_response_wall", "grid_response_wall", "claimed_wall", "ended_wall"):
            value = marker[key]
            if value is not None and (_number(value) is None or value < prepared or value > wall):
                return False
        return True

    async def async_restore(self):
        self._closed = False
        if self._restored or not self._enabled():
            return
        self._restored = True
        try:
            wall, mono = self._clocks()
            policy = self.c.regulator._get_meter_envelope_policy()
            if (self.c._budget_error or self.c._budget_store.verification_pending
                    or self.c._protective_zero_pending
                    or policy.operational_fault is not None or policy.recovery.required
                    or policy.episode is not None and policy.episode.unresolved):
                self._phase, self._error = "rejected", "bridge_restored_operational_fault"
                self.c._protective_zero_pending = True
                return
            self._ensure_store()
            # One public disk claim at a time, including a rapid config reload.
            async with self._store_lock:
                marker = await self._load()
                if marker is None:
                    self._phase = "not_armed"
                    return
                if not self._valid_marker(marker, wall):
                    self._error, self._phase = "bridge_marker_invalid", "rejected"
                    return
                self._marker = deepcopy(marker)
                if marker["state"] != "acknowledged" or marker["expires_wall"] <= wall:
                    self._phase = "expired" if marker["expires_wall"] <= wall else "not_reusable"
                    if marker["state"] in {"prepared", "aborted"} and marker["possible_until_wall"] > wall:
                        self._unknown_until = marker["possible_until_wall"]
                        self.c._protective_zero_pending = True
                    return
                claimed = {**marker, "state": "claimed", "claimed_wall": wall,
                    "unobserved_seconds": wall - marker["dispatch_wall"], "revision": uuid4().hex}
                self._marker = await self._save_locked(claimed)
            self._active = True
            self._phase = "bootstrap_wait"
            self._expires_mono = mono + marker["expires_wall"] - wall
            self._last_wall, self._last_mono = wall, mono
        except Exception as err:
            self._active = False
            self._phase = "rejected"
            self._error = str(err) if isinstance(err, BridgeRefused) else "bridge_restore_failed"

    def _source_problem(self, inp, *, allow_missing=False):
        if not self._enabled():
            return ElectricalStopCause.CONFIGURATION
        if self.c._budget_error or self.c._budget_store.verification_pending:
            return ElectricalStopCause.PERSISTENCE
        if self.c._protective_zero_pending:
            return ElectricalStopCause.UNKNOWN_DELIVERY
        if self._preparing and self._prepare_binding is not None and (
                self._prepare_binding != self._binding()
                or self.c._generation != self._prepare_generation):
            return ElectricalStopCause.CONTROL_SOURCE_CHANGED
        transport = getattr(self.c._adapter, "transport_block_reason", None)
        native_wait = allow_missing and self._native_initial_wait(inp)
        if ((getattr(inp.meter_actuator_health, "valid", False) is not True or transport)
                and not native_wait):
            return ElectricalStopCause.ACTUATOR_INVALID
        if getattr(inp.meter_actuator_health, "valid", False) is True and not transport:
            self._actuator_ready_seen = True
        if ((inp.status or "").strip().lower() not in _ALLOWED
                and not (native_wait and (inp.status or "").strip().lower() in {"", "unknown", "unavailable"})):
            return ElectricalStopCause.ACTUATOR_INVALID
        if getattr(inp.meter_grid_health, "valid", False) is not True:
            if allow_missing and self._missing_first_sample(inp):
                return None
            return ElectricalStopCause.GRID_INVALID
        raw, wall, gt = map(_number, (inp.grid_l1, inp.wall_now_ts, inp.grid_report_ts))
        if raw is None or wall is None or gt is None or not 0 <= wall-gt <= min(15, self.c.settings.max_sensor_age_seconds) or raw < 0:
            return ElectricalStopCause.GRID_INVALID
        # The consumer retains an already acknowledged 7 A allocation without
        # a write while measured total is still within the nominal fuse. This
        # cannot grant overload or extend the original bridge. The producer
        # requires the full normal buffer before creating that blind allowance.
        if raw > 38:
            return ElectricalStopCause.HARD_LIMIT
        if raw > (23 if self._preparing else 25):
            return ElectricalStopCause.GRID_CEILING
        return None

    def _native_initial_wait(self, inp):
        if self._preparing or self._actuator_ready_seen or not self._active:
            return False
        adapter = self.c._adapter
        if getattr(adapter, "control_block_reason", None) is not None:
            return False
        reason = getattr(inp.meter_actuator_health, "reason", None)
        if reason not in {"easee_stream_disconnected", "easee_charger_offline",
                          "easee_heartbeat_missing", "easee_telemetry_missing"}:
            return False
        product = getattr(adapter, "_product_data", None)
        state = getattr(product, "state", None)
        if not isinstance(state, dict) or state.get("latestPulse") is not None:
            return False
        if getattr(self.c, "_native_telemetry_events", ()):
            return False
        current = getattr(adapter, "native_current_observation", None)
        if (current is None or getattr(current, "received_at", None) is not None
                or getattr(current, "error", None) not in {None, "native_terminal_values_unavailable"}):
            return False
        sequences = getattr(current, "observation_sequences", ())
        return isinstance(sequences, tuple) and all(type(sequence) is int and sequence == 0
            for _, sequence in sequences)

    def _missing_first_sample(self, inp):
        if self._first_fresh or self.c._l1_source_error:
            return False
        source = self.c._l1_source
        if source is None:
            return False
        sample = source.snapshot()
        return bool(sample.error == FIRST_GRID_WAIT and sample.current is None
            and sample.receipt_ts is None and sample.sample_ts is None
            and inp.grid_l1 is None and inp.grid_report_ts is None)

    def _decision(self, inp, *, mode, cap=7, command=None, ttl=300):
        return RegulatorDecision(mode=mode, reason=(
            "One finite graceful-shutdown allowance; no renewal, new response evidence or overload permission."),
            command=command, service="easee.set_circuit_dynamic_limit" if command else None,
            current_p1=cap, current_p2=0, current_p3=0, ttl_seconds=ttl,
            available_l1=cap, house_load_l1=max(0., (_number(inp.grid_l1) or 0.)-(_number(inp.charger_current) or 0.)),
            grid_l1=_number(inp.grid_l1), charger_current=_number(inp.charger_current) or 0.,
            paused_by_us=False, should_call_service=bool(command), dry_run=False,
            meter_envelope=True, meter_state=mode, meter_overload_authority=False,
            normal_continuity_deadline_ts=self._expires_mono)

    def _withdraw(self, reason):
        self._active = False
        self._error, self._phase = reason, "withdrawn"
        if self._marker is not None and self._marker["state"] == "claimed":
            try:
                wall, _ = self._clocks()
            except BridgeRefused:
                wall = self._last_wall
            if wall is None:
                return
            update = {**self._marker, "state": "aborted", "ended_wall": wall,
                "unobserved_seconds": min(316., max(0., wall-self._marker["dispatch_wall"]))}
            self._queue_save(update)

    def _queue_save(self, marker):
        async def save():
            try:
                await self._save(marker, expected_nonce=marker["nonce"])
            except Exception:
                self._error = "bridge_checkpoint_not_verified"
        creator = getattr(self.c.hass, "async_create_task", asyncio.create_task)
        self._background_save = creator(save())

    async def async_tick_override(self, inp):
        if self._preparing:
            problem = self._source_problem(inp)
            if problem:
                self._abort = True
            # The producer owns protective cancellation while its lock is held.
            return self._decision(inp, mode="restart_bridge_preparing_hold")
        if not self._active and self._unknown_until is None:
            return None
        try:
            wall, mono = self._clocks(inp)
        except BridgeRefused:
            self._withdraw("bridge_clock_invalid")
            return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.CLOCK_INVALID)
        if self._unknown_until is not None and wall < self._unknown_until:
            return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.UNKNOWN_DELIVERY)
        if not self._active:
            return None
        if (self._last_wall is not None and (
                wall < self._last_wall or mono < self._last_mono
                or abs((wall-self._last_wall)-(mono-self._last_mono)) > 2)):
            self._withdraw("bridge_clock_invalid")
            return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.CLOCK_INVALID)
        self._last_wall, self._last_mono = wall, mono
        if wall >= self._marker["expires_wall"] or mono >= self._expires_mono:
            self._withdraw("bridge_expired")
            return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.LEASE_EXPIRED)
        if self._binding() != self._marker["binding"]:
            self._withdraw("bridge_binding_changed")
            return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.CONTROL_SOURCE_CHANGED)
        problem = self._source_problem(inp, allow_missing=True)
        if problem:
            self._withdraw("bridge_source_or_control_invalid")
            return self.c.regulator.meter_envelope_fault(inp, problem)
        if self._missing_first_sample(inp) or self._native_initial_wait(inp):
            self._phase = "bootstrap_wait"
            return self._decision(inp, mode="restart_bridge_bootstrap_hold")
        self._first_fresh = True
        if self._planner is None:
            self._planner = deepcopy(self.c.regulator)
            p = self._planner._get_meter_envelope_policy()
            p.initialize(self._planner, inp, mono)
            # Explicit possible prior 7 A authority, not a new accepted lease.
            # The producer verified the reduction before arming the marker.
            p.cold_unknown_pending = False
            p.cold_upper_until = None
            p.upper = 7.
            p.possible_positive[("restart_bridge", self._marker["nonce"])] = (7, self._expires_mono)
        decision = self._planner.evaluate(inp)
        if decision.meter_zero_cause is not None:
            self._withdraw("bridge_normal_protection")
            self.c.regulator = self._planner
            return decision
        if decision.command == "set_limit" and decision.current_p1 > 0 and decision.should_call_service:
            if decision.meter_overload_authority:
                self._withdraw("bridge_overload_takeover_refused")
                return self.c.regulator.meter_envelope_fault(inp, ElectricalStopCause.OVERLOAD_NOT_ADMITTED)
            self._phase = "ordinary_takeover_pending"
            self.c.regulator = self._planner
            return decision
        self._phase = "normal_recovery_wait"
        return self._decision(inp, mode="restart_bridge_recovery_hold")

    def note_submission(self, decision, accepted):
        if not self._active:
            return
        if accepted and decision.command == "set_limit" and decision.current_p1 > 0 and not decision.meter_overload_authority:
            wall, _ = self._clocks()
            self._active = False
            self._phase = "ordinary_takeover_complete"
            self._queue_save({**self._marker, "state": "completed", "ended_wall": wall,
                "unobserved_seconds": min(316., max(0., wall-self._marker["dispatch_wall"]))})
        elif not accepted:
            self._withdraw("bridge_takeover_delivery_unknown")
            self.c._protective_zero_pending = True

    def source_changed(self):
        if self._preparing:
            try:
                if self._source_problem(self.c._read_input()):
                    self._abort = True
                    self._grant_guard["valid"] = False
                    self._abort_event.set()
            except Exception:
                self._abort = True
                self._grant_guard["valid"] = False
                self._abort_event.set()

    def _producer_eligible(self, inp):
        c, r = self.c, self.c.regulator
        if self._source_problem(inp) or inp.status.lower() != "charging" or self._closed:
            return False
        p = r._get_meter_envelope_policy()
        now = _number(inp.now_ts)
        if (p.state.value != "NORMAL" or p.operational_fault is not None or p.recovery.required
                or p.episode is not None and p.episode.unresolved or p.cold_unknown_pending
                or not p.lease.live(now) or not p.lease.normal_admitted or not 7 <= p.lease.cap <= 16
                or any(cap > p.lease.cap or key not in p.lease.normal_commands
                       for key, (cap, expiry) in p.possible_positive.items() if expiry > now)
                or r._dispatch_records or c._dispatch_task is not None
                or not getattr(c._adapter, "offline_limits_verified_zero", False)
                or not c._budget_store.state_is_durable(r.export_overload_state())):
            return False
        # A steady pre-shutdown EV value may be old. It grants no subtraction
        # or response credit here: only its valid mapped boundary is pinned.
        # Arming still requires a genuinely new physical reduction afterward.
        return bool(r._controlled_l1_boundary(inp) is not None)

    async def _send(self, cap, ttl, *, protective=False, timeout=REQUEST_TIMEOUT_SECONDS):
        c = self.c
        inp = c._read_input()
        decision = self._decision(inp, mode="restart_bridge_shutdown" if cap else "restart_bridge_abort", cap=cap, command="set_limit", ttl=ttl)
        record = attempt = None
        generation = c._generation

        def dispatch():
            nonlocal record, attempt
            latest = c._read_input()
            if c._stopped or c._generation != generation or not protective and (self._abort or self._source_problem(latest)):
                return False
            wall, mono = self._clocks(latest)
            self._last_dispatch = dict(wall=wall, mono=mono,
                floor=c.regulator._controlled_l1_boundary(latest))
            if cap:
                self._bridge_dispatch = self._last_dispatch
            record = c._delivery.begin("set_limit", (cap, 0, 0), mono)
            attempt = dict(started_at_utc=_iso(wall), completed_at_utc=None,
                command="set_limit", target_p1=cap, target_p2=0, target_p3=0,
                ttl_seconds=ttl, result="pending", error=None)
            c.last_command_attempt = attempt
            return True

        try:
            execute = getattr(c._adapter, "async_execute_with_dispatch", None)
            if callable(execute):
                coroutine = execute(decision, dispatch)
            else:
                if not dispatch():
                    raise BridgeRefused("bridge_dispatch_rejected")
                coroutine = c._adapter.async_execute(decision)
            self._request_task = asyncio.create_task(coroutine)
            aborted = asyncio.create_task(self._abort_event.wait()) if not protective else None
            try:
                waiting = {self._request_task} | ({aborted} if aborted is not None else set())
                done, _ = await asyncio.wait(waiting, timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED)
                if aborted is not None and aborted in done:
                    raise BridgeRefused("bridge_source_preempted_request")
                if self._request_task not in done:
                    raise TimeoutError
                receipt = self._request_task.result()
            finally:
                pending = [task for task in (self._request_task, aborted) if task is not None and not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self._request_task = None
            if record is None or type(getattr(receipt, "http_status", None)) is not int or not 200 <= receipt.http_status < 300:
                raise BridgeRefused("bridge_http_acceptance_missing")
            c._delivery.accepted(record, receipt, c.hass.loop.time())
            attempt["result"] = "submitted_unverified"
            c.last_service_result, c.last_service_error = "submitted_unverified", None
        except BaseException:
            if record is not None:
                c._delivery.failed(record, superseded=self._abort)
                attempt.update(result="error", error="bridge_submission_unverified")
                c.last_service_result, c.last_service_error = "error", "bridge_submission_unverified"
            raise
        finally:
            if record is not None:
                wall, _ = self._clocks()
                attempt.update(completed_at_utc=_iso(wall), delivery=record.public())
                c._recent_commands.append({**deepcopy(attempt), "mode": decision.mode,
                    "reason": decision.reason, "grid_l1": _number(inp.grid_l1),
                    "charger_current": _number(inp.charger_current)})
                if attempt["result"] == "submitted_unverified":
                    c.last_accepted_command = deepcopy(attempt)
                    c.last_accepted_limit = deepcopy(attempt)
        return self._last_dispatch

    async def async_prepare_shutdown(self):
        if not self._enabled() or self._preparing or self._active or self._armed or self._closed:
            return False
        self._preparing, self._abort = True, False
        self._grant_guard = {"valid": True}
        self._abort_event.clear()
        self._last_dispatch = None
        self._bridge_dispatch = None
        self._prepare_binding = None
        self._prepare_generation = None
        self._phase = "preparing"
        possible = False
        try:
            async with asyncio.timeout(PREPARE_TIMEOUT_SECONDS - 2):
                async with self.c._lock:
                    inp = self.c._read_input()
                    if not self._producer_eligible(inp):
                        raise BridgeRefused("bridge_normal_qualification_required")
                    self._prepare_binding = self._binding()
                    self._prepare_generation = self.c._generation
                    wall, mono = self._clocks(inp)
                    self._last_wall, self._last_mono = wall, mono
                    marker = dict(version=1, state="prepared", nonce=uuid4().hex,
                        binding=self._binding(), cap=7, ttl_seconds=300,
                        prepared_wall=wall, dispatch_wall=None, expires_wall=wall+300,
                        possible_until_wall=wall+316, physical_response_wall=None,
                        grid_response_wall=None, claimed_wall=None, ended_wall=None,
                        unobserved_seconds=0., revision=uuid4().hex)
                    await self._save(marker)
                    boundary = await self._send(7, 300)
                    possible = True
                    physical = grid = None
                    while not self._abort:
                        latest = self.c._read_input()
                        if self._source_problem(latest):
                            raise BridgeRefused("bridge_source_changed_during_prepare")
                        e = self.c.regulator._controlled_l1_projection(latest)
                        if (e is not None and 0 < e.current <= 7.5
                                and self.c.regulator._controlled_l1_after(e, boundary["floor"])):
                            physical = e.received_at_ts
                            gt = _number(latest.grid_report_ts)
                            if gt is not None and gt > physical:
                                grid = gt
                                break
                        await asyncio.sleep(.05)
                    if self._abort or physical is None or grid is None:
                        raise BridgeRefused("bridge_physical_response_unverified")
                    self._marker = await self._save({**self._marker, "state": "acknowledged",
                        "dispatch_wall": boundary["wall"], "expires_wall": boundary["wall"]+300,
                        "physical_response_wall": physical, "grid_response_wall": grid})
                    if self._abort or self._source_problem(self.c._read_input()):
                        raise BridgeRefused("bridge_final_source_check_failed")
                    self._armed = True
                    self._phase = "armed"
                    return True
        except BaseException as err:
            self._abort = True
            self._grant_guard["valid"] = False
            self._armed = False
            self._phase = "refused"
            self._error = str(err) if isinstance(err, BridgeRefused) else "bridge_prepare_timeout_or_failure"
            possible = possible or self._bridge_dispatch is not None
            if self._marker is not None and self._marker["state"] in {"prepared", "acknowledged"}:
                wall, _ = self._clocks()
                # Queue a tombstone before attempting another cloud command.
                # It serializes behind even a timed-out executor write.
                tombstone = {**self._marker, "state": "aborted", "ended_wall": wall}
                if self._bridge_dispatch is not None:
                    tombstone.update(dispatch_wall=self._bridge_dispatch["wall"],
                        expires_wall=self._bridge_dispatch["wall"]+300)
                self._queue_save(tombstone)
            if isinstance(err, asyncio.CancelledError):
                if possible:
                    self.c._protective_zero_pending = True
                raise
            if isinstance(err, CommandNotSent):
                possible = False
            latest = self.c._read_input()
            critical = self._source_problem(latest)
            known_actuator_fault = (getattr(latest.meter_actuator_health, "valid", False) is not True
                or getattr(self.c._adapter, "transport_block_reason", None) is not None)
            actual_critical = bool(
                critical in {ElectricalStopCause.HARD_LIMIT, ElectricalStopCause.GRID_INVALID,
                             ElectricalStopCause.UNKNOWN_DELIVERY, ElectricalStopCause.CLOCK_INVALID}
                or critical == ElectricalStopCause.ACTUATOR_INVALID and known_actuator_fault
                or critical == ElectricalStopCause.PERSISTENCE and self.c._budget_error
                   and self.c._budget_error != "overload_budget_save_pending"
                or self._abort_event.is_set() and critical not in {None, ElectricalStopCause.CONTROL_SOURCE_CHANGED})
            if possible or actual_critical:
                self.c._protective_zero_pending = True
                cause = critical or (ElectricalStopCause.PERSISTENCE if isinstance(err, OSError)
                    or isinstance(err, BridgeRefused) and "checkpoint" in str(err)
                    else ElectricalStopCause.UNKNOWN_DELIVERY)
                # Use the existing observer before recording a stop: a real
                # high-load interval must survive shutdown as exposure, rather
                # than becoming fault history beside a false inactive ledger.
                self.c.regulator.evaluate(latest)
                raw, wall, gt = map(_number, (latest.grid_l1, latest.wall_now_ts, latest.grid_report_ts))
                if (getattr(latest.meter_grid_health, "valid", False) is True
                        and raw is not None and raw > self.c.settings.main_fuse_amps
                        and wall is not None and gt is not None and 0 <= wall-gt <= 15):
                    self.c.regulator._overload_exposure_active = True
                self.c.regulator.meter_envelope_fault(latest, cause)
                try:
                    await asyncio.wait_for(self.c._budget_store.async_save(
                        self.c.regulator.export_overload_state()), .5)
                except Exception:
                    self.c._budget_error = ("overload_budget_save_pending"
                        if self.c._budget_store.verification_pending else "overload_budget_save_failed")
                try:
                    await self._send(0, 60, protective=True, timeout=1.)
                except Exception:
                    pass
            if self._background_save is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._background_save), .5)
                except Exception:
                    pass
            return False
        finally:
            self._preparing = False
            self._prepare_binding = None
            self._prepare_generation = None

    async def async_close(self):
        self._closed = True
        self._active = False
        self._planner = None
        self._restored = False
        self._abort = self._abort or not self._armed
        self._grant_guard["valid"] = False
        self._abort_event.set()
        if self._request_task is not None and not self._request_task.done():
            self._request_task.cancel()
        try:
            await asyncio.wait_for(self._wait_pending(), STORE_TIMEOUT_SECONDS)
        except (Exception, asyncio.CancelledError):
            pass
        if self._background_save is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._background_save), STORE_TIMEOUT_SECONDS)
            except (Exception, asyncio.CancelledError):
                pass

    def diagnostics(self):
        # _read_input publishes these diagnostics: never recurse into it.
        mono = _number(self.c.hass.loop.time())
        wall = (self._last_wall + mono-self._last_mono
                if self._last_wall is not None and self._last_mono is not None
                and mono is not None and mono >= self._last_mono else None)
        expiry = self._marker.get("expires_wall") if self._marker else None
        return dict(restart_bridge_state=self._phase,
            restart_bridge_expires_at_utc=_iso(expiry),
            restart_bridge_remaining_seconds=max(0., expiry-wall) if expiry is not None and wall is not None else 0.,
            restart_bridge_last_error=self._error, restart_bridge_preparing=self._preparing,
            restart_bridge_unobserved_seconds=self._marker.get("unobserved_seconds", 0.) if self._marker else 0.)
