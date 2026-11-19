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
import logging
from math import isfinite
import re
from uuid import uuid4

from .logic import ElectricalStopCause, RegulatorDecision
from .easee_adapter import CommandNotSent

BRIDGE_CAP_AMPS = 7
BRIDGE_TTL_SECONDS = 300
MANAGED_PREFLIGHT_TIMEOUT_SECONDS = 90
MANAGED_COMMIT_TIMEOUT_SECONDS = 16
STORE_TIMEOUT_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 6
FIRST_GRID_WAIT = "L1 MQTT source is waiting for a fresh non-retained sample."
_STATES = {"prepared", "acknowledged", "claimed", "completed", "aborted", "not_sent"}
_ALLOWED = {"charging", "awaiting_start", "ready_to_charge", "start_charging", "awaiting_load_balancing"}
_LOGGER = logging.getLogger(__name__)


class _BridgeGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = None


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _grid_number(value):
    """HA entity states are text. The MQTT source already supplies a number."""
    if type(value) not in (str, int, float):
        return None
    try:
        return _number(float(value))
    except (ValueError, OverflowError):
        return None


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
        self._bridge_dispatch = None
        self._phase = "disabled"
        self._error = None
        self._prepare_stage = None
        self._failed_stage = None
        self._error_type = None
        self._prepare_started_mono = None
        self._prepare_deadline_wall = None
        self._prepare_finished_mono = None
        self._physical_response_wall = None
        self._grid_response_wall = None
        self._managed = False
        self._managed_ready = None
        self._managed_commit = asyncio.Event()
        self._managed_minimum = None

    @property
    def preparing(self):
        return self._preparing

    @property
    def armed(self):
        return self._armed

    @property
    def managed_preflight(self):
        return self._managed

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
        # Real ConfigEntry.data is a read-only mappingproxy, not a JSON dict.
        # Copy only for canonical serialization; never mutate the HA entry.
        payload = dict(entry=self.c.entry.entry_id, data=dict(self.c.entry.data),
            settings=asdict(self.c.settings), circuit=getattr(circuit, "id", None),
            configured=configured, selection=selection,
            mqtt=asdict(source) if source is not None else None)
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
            allow_nan=False, separators=(",", ":")).encode()).hexdigest()

    def _ensure_store(self):
        if self._store is not None:
            return
        from homeassistant.helpers.storage import Store
        self._store = Store(self.c.hass, 1, self.storage_key, atomic_writes=True)
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
                        or snapshot["state"] in {"completed", "aborted", "not_sent"}
                           and current.get("state") in {"completed", "aborted", "not_sent"}
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
                or type(marker["version"]) is not int or marker["version"] not in {1, 2}
                or type(marker["state"]) is not str or marker["state"] not in _STATES
                or not isinstance(marker["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", marker["nonce"]) is None
                or not isinstance(marker["revision"], str) or re.fullmatch(r"[0-9a-f]{32}", marker["revision"]) is None
                or marker["binding"] != self._binding()
                or type(marker["cap"]) is not int or marker["cap"] != 7
                or type(marker["ttl_seconds"]) is not int or marker["ttl_seconds"] != 300):
            return False
        if marker["state"] == "not_sent" and (marker["version"] != 2
                or marker["dispatch_wall"] is not None or marker["ended_wall"] is None):
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
                    or expiry != dispatch + 300):
                return False
            if marker["version"] == 1:
                ordered = prepared <= dispatch < physical < grid <= wall
            else:
                ordered = (0 < dispatch - physical <= 30
                    and physical < grid <= dispatch and prepared <= dispatch <= wall)
            if not ordered:
                return False
        for key in ("dispatch_wall", "physical_response_wall", "grid_response_wall", "claimed_wall", "ended_wall"):
            value = marker[key]
            lower = 0 if marker["version"] == 2 and key in {
                "physical_response_wall", "grid_response_wall"} else prepared
            if value is not None and (_number(value) is None or value < lower or value > wall):
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
        raw = _grid_number(inp.grid_l1)
        wall, gt = map(_number, (inp.wall_now_ts, inp.grid_report_ts))
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
        raw = _grid_number(inp.grid_l1)
        return RegulatorDecision(mode=mode, reason=(
            "One finite graceful-shutdown allowance; no renewal, new response evidence or overload permission."),
            command=command, service="easee.set_circuit_dynamic_limit" if command else None,
            current_p1=cap, current_p2=0, current_p3=0, ttl_seconds=ttl,
            available_l1=cap, house_load_l1=max(0., (raw or 0.)-(_number(inp.charger_current) or 0.)),
            grid_l1=raw, charger_current=_number(inp.charger_current) or 0.,
            paused_by_us=False, should_call_service=bool(command), dry_run=False,
            meter_envelope=True, meter_state=mode, meter_overload_authority=False,
            meter_quiet_peak_reduction=False, meter_peak_reduction_ceiling_amps=None,
            meter_peak_reduction_lease_origin_ts=None,
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
                inp = self.c._read_input()
                proof = self._managed_ready[1] if self._managed_ready is not None else None
                current = self._managed_proof(inp) if proof is not None else None
                if (self._source_problem(inp)
                        or proof is not None and (current is None or current[0] != proof[0])):
                    self._abort = True
                    self._grant_guard["valid"] = False
                    self._abort_event.set()
            except Exception:
                self._abort = True
                self._grant_guard["valid"] = False
                self._abort_event.set()

    def _producer_problem(self, inp):
        c, r = self.c, self.c.regulator
        source_problem = self._source_problem(inp)
        if source_problem is not None:
            return f"bridge_qualification_{source_problem.value}"
        if self._closed or (inp.status or "").strip().lower() != "charging":
            return "bridge_qualification_not_charging"
        p = r._get_meter_envelope_policy()
        now = _number(inp.now_ts)
        if (p.state.value != "NORMAL" or p.operational_fault is not None or p.recovery.required
                or p.episode is not None and p.episode.unresolved):
            return "bridge_qualification_not_normal"
        if r._dispatch_records or c._dispatch_task is not None:
            return "bridge_qualification_dispatch_pending"
        if (now is None or not p.lease.live(now) or not p.lease.normal_admitted
                or not 7 <= p.lease.cap <= 16 or inp.control_source_id is None
                or p.lease.source_id != inp.control_source_id):
            return "bridge_qualification_normal_limit_missing"
        if p.cold_unknown_pending:
            return "bridge_qualification_previous_authority_unknown"
        for key, (cap, expiry) in p.possible_positive.items():
            if expiry <= now:
                continue
            # Every unexpired request must be an acknowledged normal offer
            # from this very source. An older higher offer can still exist
            # while we attempt a strictly lower handoff. Keep its horizon and
            # require a new native readback before arming that handoff.
            prior = p.lease.normal_commands.get(key)
            if (prior is None or prior["source_id"] != inp.control_source_id
                    or prior["cap"] != cap or prior["expires"] != expiry
                    or (cap > p.lease.cap and expiry >= p.lease.expires)):
                return "bridge_qualification_previous_authority_unknown"
        if not getattr(c._adapter, "offline_limits_verified_zero", False):
            return "bridge_qualification_offline_fallback_unverified"
        if not c._budget_store.state_is_durable(r.export_overload_state()):
            return "bridge_qualification_checkpoint_unverified"
        # A steady pre-shutdown EV value may be old. It grants no subtraction
        # or response credit here: only its valid mapped boundary is pinned.
        # Arming still requires a genuinely new physical reduction afterward.
        if r._controlled_l1_boundary(inp) is None:
            return "bridge_qualification_mapping_unverified"
        return None

    def _producer_eligible(self, inp):
        return self._producer_problem(inp) is None

    async def _send(self):
        c = self.c
        inp = c._read_input()
        decision = self._decision(inp, mode="restart_bridge_shutdown", command="set_limit")
        record = attempt = None
        generation = c._generation
        rejected = None
        problem = self._producer_problem(inp)
        if problem is not None:
            raise BridgeRefused(problem)
        lease = c.regulator._get_meter_envelope_policy().lease
        previous = (lease.cap, lease.origin, lease.expires, lease.source_id)

        def dispatch():
            nonlocal record, attempt, rejected
            latest = c._read_input()
            if c._stopped or self._abort or c._generation != generation:
                return False
            rejected = self._producer_problem(latest)
            if rejected is not None:
                return False
            managed_proof = self._managed_proof(latest)
            if managed_proof is None:
                rejected = "bridge_preflight_proof_lost"
                return False
            lease = c.regulator._get_meter_envelope_policy().lease
            if (lease.cap, lease.origin, lease.expires, lease.source_id) != previous:
                rejected = "bridge_qualification_normal_limit_changed"
                return False
            adapter_identity = c._adapter.control_identity
            if adapter_identity is None:
                rejected = "bridge_qualification_actuator_identity_missing"
                return False
            wall, mono = self._clocks(latest)
            self._bridge_dispatch = dict(wall=wall, mono=mono, managed_proof=managed_proof,
                source_id=latest.control_source_id, adapter_identity=adapter_identity)
            record = c._delivery.begin("set_limit", (BRIDGE_CAP_AMPS, 0, 0), mono)
            attempt = dict(started_at_utc=_iso(wall), completed_at_utc=None,
                command="set_limit", target_p1=BRIDGE_CAP_AMPS, target_p2=0, target_p3=0,
                ttl_seconds=BRIDGE_TTL_SECONDS, result="pending", error=None)
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
            aborted = asyncio.create_task(self._abort_event.wait())
            try:
                done, _ = await asyncio.wait({self._request_task, aborted}, timeout=REQUEST_TIMEOUT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED)
                if aborted in done:
                    raise BridgeRefused("bridge_source_preempted_request")
                if self._request_task not in done:
                    raise TimeoutError
                try:
                    receipt = self._request_task.result()
                except CommandNotSent:
                    if rejected is not None:
                        raise BridgeRefused(rejected) from None
                    raise
            finally:
                pending = [task for task in (self._request_task, aborted) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self._request_task = None
            if record is None or type(getattr(receipt, "http_status", None)) is not int or not 200 <= receipt.http_status < 300:
                raise BridgeRefused("bridge_http_acceptance_missing")
            c._delivery.accepted(record, receipt, c.hass.loop.time())
            if record.vendor_accepted is False:
                raise BridgeRefused("bridge_vendor_rejected")
            attempt["result"] = "submitted_unverified"
            c.last_service_result, c.last_service_error = "submitted_unverified", None
        except BaseException:
            if record is not None:
                if record.vendor_accepted is not False:
                    c._delivery.failed(record, superseded=self._abort)
                attempt.update(result="error", error="bridge_submission_unverified")
                c.last_service_result, c.last_service_error = "error", "bridge_submission_unverified"
            raise
        finally:
            if record is not None:
                wall, _ = self._clocks()
                attempt.update(completed_at_utc=_iso(wall), delivery=record.public())
                c._recent_commands.append({**deepcopy(attempt), "mode": decision.mode,
                    "reason": decision.reason, "grid_l1": _grid_number(inp.grid_l1),
                    "charger_current": _number(inp.charger_current)})
                if attempt["result"] == "submitted_unverified":
                    c.last_accepted_command = deepcopy(attempt)
                    c.last_accepted_limit = deepcopy(attempt)
                c._queue_incident_journal()
        return self._bridge_dispatch

    def _managed_proof(self, inp):
        """Return measured minimum plus a later meter packet and native cap."""
        policy = self.c.regulator._get_meter_envelope_policy()
        lease = policy.lease
        if (self._producer_problem(inp) is not None or lease.cap != BRIDGE_CAP_AMPS
                or lease.normal_origin is None or _grid_number(inp.grid_l1) > 23):
            return None
        regulator = self.c.regulator
        ev = regulator._controlled_l1_projection(inp)
        if not regulator._controlled_l1_after(ev, policy.accepted_l1_floor):
            return None
        wall, mono = self._clocks(inp)
        meter = _number(inp.grid_report_ts)
        raw = _grid_number(inp.grid_l1)
        if not 1 <= ev.current <= min(7.5, raw + 1) or not 0 <= wall - ev.received_at_ts <= 30:
            self._managed_minimum = None
            return None
        key = regulator._controlled_l1_key(ev), lease.normal_origin
        pinned = self._managed_minimum
        if pinned is None or pinned[0] != key or wall - pinned[1] > 30:
            self._managed_minimum = pinned = (key, ev.received_at_ts, ev.current)
        physical = pinned[1]
        limits = getattr(self.c._adapter, "reported_limits", None)
        receipts = getattr(self.c._adapter, "reported_limit_receipts", None)
        receipt = _number(receipts[0]) if isinstance(receipts, tuple) and len(receipts) == 3 else None
        if (meter is None or not physical < meter <= wall
                or ev.received_at_ts < physical
                or raw + max(0, BRIDGE_CAP_AMPS - min(pinned[2], ev.current)) > 23
                or limits != (7, 0, 0) or receipt is None
                or not lease.normal_origin < receipt <= mono):
            return None
        return key, physical, meter

    def _managed_source_problem(self, inp):
        problem = self._source_problem(inp)
        if problem is None and _grid_number(inp.grid_l1) > 23:
            return ElectricalStopCause.GRID_CEILING
        return problem

    async def async_restart_with_charging(self, request_restart):
        if self._managed or self._preparing or self._active or self._armed or self._closed:
            raise BridgeRefused("bridge_restart_already_in_progress")
        inp = self.c._read_input()
        problem = self._producer_problem(inp)
        # A controller tick or pending budget write can finish before the
        # first proof check. Do not turn those transient states into a stop.
        if problem not in {None, "bridge_qualification_dispatch_pending",
                           "bridge_qualification_checkpoint_unverified",
                           "bridge_qualification_persistence"}:
            raise BridgeRefused(problem)
        if self._managed_source_problem(inp) not in {None, ElectricalStopCause.PERSISTENCE}:
            raise BridgeRefused("bridge_preflight_source_or_load")
        self._managed = True
        self._managed_minimum = None
        self._phase, self._prepare_stage = "preflight", "wait_normal_minimum"
        self._failed_stage = self._error_type = self._error = None
        self._managed_commit.clear()
        self._prepare_started_mono = self._prepare_deadline_wall = self._prepare_finished_mono = None
        self._physical_response_wall = self._grid_response_wall = None
        self._bridge_dispatch = None
        self._abort = False
        self._abort_event.clear()
        self._grant_guard = {"valid": True}
        accepted = False
        try:
            wall, mono = self._clocks()
            binding, generation = self._binding(), self.c._generation
            self._prepare_started_mono = mono
            self._prepare_deadline_wall = wall + MANAGED_PREFLIGHT_TIMEOUT_SECONDS
            async with asyncio.timeout(MANAGED_PREFLIGHT_TIMEOUT_SECONDS):
                # Ordinary controller writes and all electrical rules remain
                # active while waiting for the normal finite 7 A reduction.
                self.c._handle_source_change(None)
                while True:
                    inp = self.c._read_input()
                    latest_wall, latest_mono = self._clocks(inp)
                    if (self._closed or self.c._stopped or generation != self.c._generation
                            or binding != self._binding() or latest_wall < wall or latest_mono < mono
                            or abs((latest_wall-wall)-(latest_mono-mono)) > 2
                            or (inp.status or "").lower() != "charging"
                            or self._managed_source_problem(inp) not in {None, ElectricalStopCause.PERSISTENCE}):
                        raise BridgeRefused("bridge_preflight_source_or_load")
                    proof = self._managed_proof(inp)
                    expiry = self.c.regulator._get_meter_envelope_policy().lease.expires
                    if proof is not None and latest_mono + MANAGED_COMMIT_TIMEOUT_SECONDS < expiry:
                        break
                    await asyncio.sleep(.1)
            async with asyncio.timeout(MANAGED_COMMIT_TIMEOUT_SECONDS):
                async with self.c._lock:
                    self._preparing = True
                    self._prepare_binding, self._prepare_generation = binding, generation
                    inp = self.c._read_input()
                    proof = self._managed_proof(inp)
                    if proof is None or generation != self.c._generation or binding != self._binding():
                        raise BridgeRefused("bridge_preflight_proof_lost")
                    self._prepare_stage = "save_prepared"
                    wall, mono = self._clocks(inp)
                    self._last_wall, self._last_mono = wall, mono
                    await self._save(dict(version=2, state="prepared", nonce=uuid4().hex,
                        binding=binding, cap=7, ttl_seconds=300,
                        prepared_wall=wall, dispatch_wall=None, expires_wall=wall+300,
                        possible_until_wall=wall+316, physical_response_wall=None,
                        grid_response_wall=None, claimed_wall=None, ended_wall=None,
                        unobserved_seconds=0., revision=uuid4().hex))
                    self._prepare_stage = "dispatch_verified_minimum"
                    boundary = await self._send()
                    accepted = True
                    proof = boundary["managed_proof"]
                    current = self._managed_proof(self.c._read_input())
                    if current is None or current[0] != proof[0] or self._abort:
                        raise BridgeRefused("bridge_preflight_proof_lost")
                    self._physical_response_wall, self._grid_response_wall = proof[1:]
                    self._managed_ready = (boundary, proof)
                    self._phase, self._prepare_stage = "preflight_ready", "wait_shutdown"

                async def restart():
                    try:
                        await request_restart()
                    except Exception:
                        if not self._armed:
                            raise
                        _LOGGER.warning("1FuseLoader restart transport closed after handoff was committed")

                request = asyncio.create_task(restart())
                abort = asyncio.create_task(self._abort_event.wait())
                commit = asyncio.create_task(self._managed_commit.wait())
                try:
                    done, _ = await asyncio.wait({request, abort, commit}, return_when=asyncio.FIRST_COMPLETED)
                    if not self._armed:
                        if request in done:
                            request.result()
                            await asyncio.wait({abort, commit}, return_when=asyncio.FIRST_COMPLETED)
                    if not self._armed:
                        raise BridgeRefused("bridge_managed_shutdown_not_committed")
                finally:
                    closing = (abort, commit) if self._armed else (request, abort, commit)
                    for task in closing:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*closing, return_exceptions=True)
        except BaseException as err:
            self._abort = True
            self._grant_guard["valid"] = False
            self._phase, self._failed_stage, self._error_type = "refused", self._prepare_stage, type(err).__name__
            self._error = str(err) if isinstance(err, BridgeRefused) else (
                f"bridge_prepare_timeout_{self._prepare_stage}" if isinstance(err, TimeoutError)
                else f"bridge_prepare_error_{self._prepare_stage}")
            _LOGGER.warning("1FuseLoader managed restart refused at %s (%s, %s)",
                self._failed_stage, self._error, self._error_type)
            if isinstance(err, (BridgeRefused, asyncio.CancelledError)):
                raise
            raise BridgeRefused(self._error) from err
        finally:
            try:
                if not self._armed:
                    self._abort_managed_attempt(accepted)
            finally:
                self._managed_ready = self._managed_minimum = None
                self._managed = self._preparing = False
                self._prepare_binding = self._prepare_generation = None
                self._prepare_finished_mono = _number(self.c.hass.loop.time())
                if not self.c._stopped:
                    self.c._handle_source_change(None)

    def _abort_managed_attempt(self, accepted):
        marker = self._marker
        if marker is None or marker["version"] != 2 or marker["state"] not in {"prepared", "acknowledged", "aborted"}:
            return
        boundary = self._bridge_dispatch
        try:
            latest = self.c._read_input()
            wall, _ = self._clocks(latest)
            tombstone = {**marker, "state": "not_sent" if boundary is None else "aborted",
                         "ended_wall": wall}
            if boundary is not None:
                tombstone.update(dispatch_wall=boundary["wall"], expires_wall=boundary["wall"]+300)
                policy = self.c.regulator._get_meter_envelope_policy()
                key = ("managed_restart", marker["nonce"])
                expiry = boundary["mono"] + 300
                policy.possible_positive[key] = (7, expiry)
                if accepted and boundary["source_id"] == latest.control_source_id:
                    policy.lease.normal_commands[key] = dict(cap=7, expires=expiry,
                        source_id=boundary["source_id"])
            if boundary is not None and (not accepted or self._managed_proof(latest) is None):
                self.c._protective_zero_pending = True
            self._queue_save(tombstone)
        except Exception:
            if boundary is not None:
                self.c._protective_zero_pending = True

    async def async_commit_managed_shutdown(self):
        """A raw shutdown with no live managed request cannot grant a lease."""
        if self._managed_ready is None or self._abort or self._closed:
            return False
        async with self.c._lock:
            if self._managed_ready is None or self._armed or self._abort or self._closed:
                return False
            boundary, proof = self._managed_ready
            inp = self.c._read_input()
            wall, mono = self._clocks(inp)
            current = self._managed_proof(inp)
            if (self._abort or current is None or current[0] != proof[0]
                    or wall < boundary["wall"] or mono < boundary["mono"]
                    or abs((wall-boundary["wall"])-(mono-boundary["mono"])) > 2
                    or wall-boundary["wall"] >= MANAGED_COMMIT_TIMEOUT_SECONDS):
                return False
            self._prepare_stage = "save_acknowledged"
            marker = {**self._marker, "state": "acknowledged",
                "dispatch_wall": boundary["wall"], "expires_wall": boundary["wall"]+300,
                "physical_response_wall": proof[1], "grid_response_wall": proof[2]}
            if not self._valid_marker(marker, wall):
                return False
            await self._save(marker)
            # A successful store write is not the source revalidation.
            current = self._managed_proof(self.c._read_input())
            if self._abort or current is None or current[0] != proof[0]:
                wall, _ = self._clocks()
                await self._save({**self._marker, "state": "aborted", "ended_wall": wall},
                                 expected_nonce=self._marker["nonce"])
                return False
            self._armed = True
            self._phase = self._prepare_stage = "armed"
            self._managed_commit.set()
            return True

    async def async_protect_failed_shutdown(self):
        """Withdraw a submitted bridge on a real fault before closing its adapter."""
        boundary = self._bridge_dispatch
        if boundary is None or self._armed or self.c._stopped:
            return
        async with self.c._lock:
            if (self._armed or self.c._stopped
                    or self.c.hass.loop.time() >= boundary["mono"] + BRIDGE_TTL_SECONDS
                    or self.c._adapter.control_identity != boundary["adapter_identity"]):
                return
            inp = self.c._read_input()
            cause = self._managed_source_problem(inp)
            if cause is None and inp.control_source_id != boundary["source_id"]:
                cause = ElectricalStopCause.CONTROL_SOURCE_CHANGED
            if cause is not None:
                self.c._protective_zero_pending = True
                await self.c._async_tick_locked(
                    shutdown_protection=(cause, boundary["adapter_identity"]))

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
            restart_bridge_last_error=self._error, restart_bridge_preparing=self._managed or self._preparing,
            restart_bridge_prepare_phase=self._prepare_stage,
            restart_bridge_failed_phase=self._failed_stage,
            restart_bridge_error_type=self._error_type,
            restart_bridge_prepare_deadline_utc=_iso(self._prepare_deadline_wall),
            restart_bridge_prepare_elapsed_seconds=(max(0.,
                (self._prepare_finished_mono if self._prepare_finished_mono is not None else mono)
                - self._prepare_started_mono) if self._prepare_started_mono is not None and mono is not None else 0.),
            restart_bridge_physical_response_at_utc=_iso(self._physical_response_wall),
            restart_bridge_grid_response_at_utc=_iso(self._grid_response_wall),
            restart_bridge_unobserved_seconds=self._marker.get("unobserved_seconds", 0.) if self._marker else 0.)
