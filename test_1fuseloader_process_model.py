"""Synthetic single-phase process and transport model; never uses live records.

Plant truth, device offer, API acceptance, observation source time and local
receipt time are distinct. Import-only tests explicitly assume a verified,
nonnegative L1 import meter and a single L1 EV load; they do not establish those
assumptions for any installation. A source timestamp is observation metadata,
not a guarantee of simultaneous physical acquisition.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_process_model_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)
EPOCH = 1_700_000_000.


@dataclass(frozen=True)
class Observation:
    value: float
    status: str
    sampled_at: float
    source_timestamp: float
    received_at: float

    def receive_again(self, received_at):
        """A cloud GET can return the same observation with a new receipt."""
        return Observation(self.value, self.status, self.sampled_at,
                           self.source_timestamp, received_at)


class SinglePhasePlant:
    """A deterministic physical plant independent of the regulator's math."""

    def __init__(self, house=4., demand=0., applied_cap=0.):
        self.time = 0.
        self.house = float(house)
        self.demand = float(demand)
        self.applied_cap = float(applied_cap)
        self._sequence = 0
        self._active_lease = None
        self._events = []
        self.grid = self.capture_grid(0.)
        self.ev = self.capture_ev(0.)
        self.accepted = []

    @property
    def actual_ev(self):
        return max(0., min(self.demand, self.applied_cap))

    @property
    def actual_grid(self):
        return self.house + self.actual_ev

    @property
    def actual_status(self):
        return "charging" if self.actual_ev >= 1 else "awaiting_start"

    def advance(self, now):
        if now < self.time:
            raise ValueError("Physical time cannot move backwards")
        while self._events and min(self._events)[0] <= now:
            at, sequence, kind, cap = min(self._events)
            self._events.remove((at, sequence, kind, cap))
            self.time = at
            if kind == "apply":
                self.applied_cap = cap
                self._active_lease = sequence
            elif self._active_lease == sequence:
                self.applied_cap = 0.
        self.time = float(now)

    def capture_grid(self, now, *, deliver=True):
        self.advance(now)
        obs = Observation(self.actual_grid, self.actual_status, now, now, now)
        if deliver:
            self.grid = obs
        return obs

    def capture_ev(self, now, *, deliver=True):
        self.advance(now)
        obs = Observation(self.actual_ev, self.actual_status, now, now, now)
        if deliver:
            self.ev = obs
        return obs

    def offer(self, cap, now, *, delay=2., ttl=60., accepted=True,
              applied_despite_timeout=False):
        """The model chooses reaction/expiry; neither is inferred from HTTP."""
        self.advance(now)
        if accepted:
            self.accepted.append((now, cap))
        if accepted or applied_despite_timeout:
            self._sequence += 1
            sequence = self._sequence
            self._events += [(now + delay, sequence, "apply", float(cap)),
                             (now + max(delay, ttl), sequence, "expire", 0.)]


class ProcessModelTests(unittest.TestCase):
    def regulator(self, *, import_only=True, **options):
        settings = dict(active_control=True, dry_run=False, overload_enabled=True,
                        measurement_settle_seconds=25, restart_stable_seconds=0,
                        enable_soft_guard=False, enable_pause_resume=False)
        settings.update(options)
        if "nominal_import_only_fallback" in {f.name for f in fields(logic.RegulatorSettings)}:
            settings["nominal_import_only_fallback"] = import_only
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**settings))
        r.import_overload_state(dict(version=1, used_seconds=2000., exposure_active=False,
                                    transition_pending=False, minimum_held=False,
                                    failure_reason="response_timeout"))
        return r

    def step(self, r, plant, now, *, grid=True, ev=False, trusted=True,
             data_valid=True, data_error=None, status=None, accept=True, delay=2.,
             timeout_applies=False, verified_get=False):
        plant.advance(now)
        if grid:
            plant.capture_grid(now)
        if ev:
            plant.capture_ev(now)
        valid_grid = 0 <= now - plant.grid.source_timestamp <= r.settings.max_sensor_age_seconds
        inp = dict(now_ts=now, wall_now_ts=EPOCH+now,
                   status=status or plant.ev.status, grid_l1=plant.grid.value,
                   charger_current=plant.ev.value,
                   grid_report_ts=EPOCH+plant.grid.source_timestamp,
                   charger_report_ts=EPOCH+plant.ev.source_timestamp,
                   charger_snapshot_verified=verified_get,
                   data_valid=data_valid and valid_grid,
                   data_error=data_error or (None if valid_grid else "grid_report_stale"))
        if "charger_current_trusted" in {f.name for f in fields(logic.RegulatorInput)}:
            inp["charger_current_trusted"] = trusted
        d = r.evaluate(logic.RegulatorInput(**inp))
        if d.should_call_service and d.command == "set_limit":
            plant.offer(d.current_p1, now, delay=delay, ttl=d.ttl_seconds,
                        accepted=accept, applied_despite_timeout=timeout_applies)
            if accept:
                r.acknowledge(d, now)
        elif d.should_call_service and accept:
            r.acknowledge(d, now)
        return d

    def assert_latch_preserved(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2000.)
        self.assertIsNone(r._overload_transition_started)

    def original_offer_chain(self, *, import_only=True):
        p, r = SinglePhasePlant(), self.regulator(import_only=import_only)
        first = self.step(r, p, 0, delay=0)
        self.assertEqual(first.current_p1, 16)
        p.demand = 6
        lower = self.step(r, p, 5)
        self.assertEqual(lower.current_p1, 13)
        p.demand = 16
        late_original = p.capture_ev(6, deliver=False)
        p.demand = 13
        lower = self.step(r, p, 10)
        self.assertEqual(lower.current_p1, 6)
        p.ev = late_original.receive_again(11)
        return r, p

    def test_original_sixteen_response_after_thirteen_and_six_is_not_false_excess(self):
        r, p = self.original_offer_chain()
        d = self.step(r, p, 11, grid=False)
        self.assertEqual((p.actual_ev, p.actual_grid), (13, 17))
        self.assertGreater(d.current_p1, 0)
        self.assertLessEqual(d.current_p1, 6)
        self.assertLessEqual(p.grid.value + d.current_p1, 23)
        self.assert_latch_preserved(r)

    def test_same_chain_without_verified_import_model_retains_safe_stop(self):
        r, p = self.original_offer_chain(import_only=False)
        self.assertEqual(self.step(r, p, 11, grid=False).current_p1, 0)
        self.assert_latch_preserved(r)

    def test_fresh_get_of_the_original_sample_cannot_restore_sixteen_offer(self):
        r, p = self.original_offer_chain()
        first = self.step(r, p, 11, grid=False)
        self.assertGreater(first.current_p1, 0)
        for now in (11.25, 11.5, 11.75):
            old_source = p.ev.source_timestamp
            p.ev = p.ev.receive_again(now)
            d = self.step(r, p, now, grid=False, verified_get=True)
            self.assertEqual(p.ev.source_timestamp, old_source)
            self.assertLessEqual(d.current_p1, first.current_p1)
        self.assert_latch_preserved(r)

    def test_report_order_mismatch_can_reduce_only_with_full_raw_import_budget(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.assertEqual(self.step(r, p, 0, ev=True, delay=0).current_p1, 16)
        p.demand = 12
        p.capture_grid(5)
        p.ev = p.ev.receive_again(5)
        d = self.step(r, p, 5, grid=False, verified_get=True)
        self.assertEqual((p.actual_ev, p.actual_grid), (12, 14))
        self.assertGreaterEqual(d.current_p1, 6)
        self.assertLessEqual(d.current_p1, 9)
        self.assertLessEqual(p.grid.value+d.current_p1, 23)
        self.assert_latch_preserved(r)

    def test_low_measured_ev_does_not_make_accepted_offer_a_measured_current(self):
        p = SinglePhasePlant(house=4., demand=8., applied_cap=16.)
        r = self.regulator()
        offered = self.step(r, p, 0, ev=True, delay=0)
        self.assertEqual(offered.current_p1, 16)
        self.assertEqual(p.actual_ev, 8)
        p.demand = 2
        d = self.step(r, p, 5)
        self.assertEqual((p.actual_ev, p.actual_grid), (2, 6))
        self.assertGreater(d.current_p1, 0)
        self.assertLessEqual(p.grid.value+d.current_p1, 23)
        self.assert_latch_preserved(r)

    def test_zero_draw_with_old_high_cloud_value_uses_raw_budget_only(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.demand = 0
        p.ev = p.ev.receive_again(5)
        d = self.step(r, p, 5, verified_get=True, trusted=False)
        self.assertEqual((p.actual_ev,p.actual_grid), (0,2))
        self.assertGreater(d.current_p1, 0)
        self.assertLessEqual(d.current_p1, 16)
        self.assertLessEqual(p.grid.value+d.current_p1, 23)
        self.assert_latch_preserved(r)

    def test_old_cloud_value_cannot_offer_minimum_when_raw_budget_is_too_small(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.house, p.demand = 20, 0
        p.ev = p.ev.receive_again(5)
        d = self.step(r, p, 5, verified_get=True, trusted=False)
        self.assertEqual(d.current_p1, 0)
        self.assert_latch_preserved(r)

    def test_old_native_event_cannot_subtract_ev_during_latched_raw_overload(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        for now in (0, 30, 60, 90, 120):
            self.step(r, p, now, ev=now == 0, delay=0)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(p.ev.source_timestamp, 0)
        # Both worlds retain positive import and a single L1 EV. A healthy
        # transport does not reveal the physical exchange of EV for house load.
        p.house, p.demand = 26, 0
        p.ev = p.ev.receive_again(130)
        d = self.step(r, p, 130, trusted=True, verified_get=True, delay=0)
        self.assertEqual((p.actual_ev, p.actual_grid), (0, 26))
        self.assertEqual(d.current_p1, 0)
        p.demand = 16
        p.advance(130.1)
        self.assertEqual(p.actual_ev, 0)
        self.assertEqual(p.actual_grid, 26)
        self.assert_latch_preserved(r)

    def test_unverified_import_assumptions_do_not_unlock_raw_budget_recovery(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator(import_only=False)
        self.step(r, p, 0, ev=True, delay=0)
        p.house, p.demand = -6, 13  # Signed site flow: PV/export invalidates model.
        p.ev = p.ev.receive_again(5)
        self.assertEqual(self.step(r, p, 5, trusted=False).current_p1, 0)

    def test_stale_grid_stops_despite_new_get_receipts(self):
        p = SinglePhasePlant(house=4., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.ev = p.ev.receive_again(130)
        self.assertEqual(self.step(r, p, 130, grid=False, verified_get=True).current_p1, 0)
        self.assert_latch_preserved(r)

    def test_real_hard_overload_stops_while_old_cloud_value_looks_ordinary(self):
        p = SinglePhasePlant(house=4., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.house = 30
        d = self.step(r, p, 5)
        self.assertEqual(p.actual_grid, 46)
        self.assertEqual(d.current_p1, 0)
        self.assert_latch_preserved(r)

    def test_fault_has_priority_over_full_raw_headroom(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        self.assertEqual(self.step(r, p, 5, status="error").current_p1, 0)
        self.assert_latch_preserved(r)

    def test_source_health_loss_has_priority_over_full_raw_headroom(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        d = self.step(r, p, 5, data_valid=False, data_error="easee_heartbeat_stale")
        self.assertEqual(d.current_p1, 0)
        self.assert_latch_preserved(r)

    def test_manual_schedule_does_not_receive_automatic_resume(self):
        for status in ("paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(status=status):
                p = SinglePhasePlant(house=2., demand=0., applied_cap=16.)
                r = self.regulator()
                d = self.step(r, p, 0, status=status)
                self.assertNotEqual(d.command, "resume")
                self.assertFalse(r.paused_by_us)

    def test_network_timeout_does_not_acknowledge_a_cap_change(self):
        for actually_applied in (False, True):
            with self.subTest(applied=actually_applied):
                p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
                r = self.regulator()
                self.step(r, p, 0, ev=True, delay=0)
                accepted = r.last_target
                p.house = 10
                lower = self.step(r, p, 5, accept=False, timeout_applies=actually_applied)
                self.assertLess(lower.current_p1, accepted[0])
                self.assertEqual(r.last_target, accepted)
                p.advance(7)
                self.assertEqual(p.applied_cap, lower.current_p1 if actually_applied else 16)
                self.assert_latch_preserved(r)

    def test_failed_lower_offer_cannot_recover_to_the_older_accepted_limit(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.demand = 12
        first = self.step(r, p, 5, accept=False)
        self.assertGreater(first.current_p1, 0)
        self.assertLess(first.current_p1, 16)
        self.assertEqual(r.last_target[0], 16)
        # The car tapers independently. This is no acknowledgement that any
        # repeated failed request changed the still accepted16 A cloud offer.
        p.demand = 7
        for now in (6, 16, 26, 36, 46):
            d = self.step(r, p, now, ev=now == 6, accept=False)
            self.assertLessEqual(d.current_p1, first.current_p1)
            self.assertEqual(r.last_target[0], 16)
        self.assert_latch_preserved(r)

    def test_modelled_lease_expiry_is_not_itself_an_ev_observation(self):
        p = SinglePhasePlant(house=2., demand=16., applied_cap=16.)
        r = self.regulator()
        self.step(r, p, 0, ev=True, delay=0)
        p.advance(61)
        self.assertEqual(p.actual_ev, 0)
        self.assertEqual(p.ev.value, 16)
        self.assertEqual(p.ev.source_timestamp, 0)
        p.ev = p.ev.receive_again(61)
        d = self.step(r, p, 61, trusted=False, verified_get=True, accept=False)
        self.assertLessEqual(d.current_p1, 16)
        self.assertNotEqual(d.command, "resume")
        self.assertEqual(p.ev.value, 16)
        self.assert_latch_preserved(r)

    def test_five_second_grid_and_thirty_second_ev_constant_load_never_need_zero(self):
        p = SinglePhasePlant(house=4., demand=16., applied_cap=16.)
        r = self.regulator()
        for now in range(0, 121, 5):
            d = self.step(r, p, now, ev=now % 30 == 0, delay=0)
            self.assertGreater(d.current_p1, 0)
            self.assertLessEqual(p.actual_grid, 23)
            self.assert_latch_preserved(r)


if __name__ == "__main__":
    unittest.main()
