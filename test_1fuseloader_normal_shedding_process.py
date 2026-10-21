"""Independent synthetic plant tests for the opt-in b18 normal decrease.

The plant uses additive, nonnegative L1 import solely as a synthetic assumption.
Its actual EV draw, applied offer, HTTP acceptance, physical capture time,
vendor event time and local receipt are separate. These tests establish neither
an installation thermal envelope nor reliable real-world cloud response times.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("normal_shedding_process_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)
EPOCH = 1_700_000_000.0


@dataclass(frozen=True)
class Event:
    value: float
    status: str
    captured: float
    source: float
    receipt: float

    def get_again(self, now):
        """A GET receipt cannot move the retained vendor event/capture time."""
        return replace(self, receipt=float(now))


class Plant:
    """A deliberately simple physical plant, independent of regulator state."""

    def __init__(self, house=2.0, demand=16.0, applied=16.0):
        self.now = 0.0
        self.house = float(house)
        self.demand = float(demand)
        self.applied = float(applied)
        self.commands = []
        self._events = []
        self._sequence = 0
        self._lease = None
        self.grid = self.capture("grid", 0)
        self.ev = self.capture("ev", 0)

    @property
    def current(self):
        return max(0.0, min(self.demand, self.applied))

    @property
    def total(self):
        return self.house + self.current

    @property
    def status(self):
        return "charging" if self.current >= 1 else "awaiting_start"

    def advance(self, now):
        if now < self.now:
            raise ValueError("The physical model cannot travel backwards")
        while self._events and min(self._events)[0] <= now:
            at, sequence, operation, cap = min(self._events)
            self._events.remove((at, sequence, operation, cap))
            self.now = at
            if operation == "apply":
                self.applied, self._lease = cap, sequence
            elif self._lease == sequence:
                self.applied = 0.0
        self.now = float(now)

    def capture(self, source, now, *, deliver=True):
        self.advance(now)
        event = Event(self.total if source == "grid" else self.current,
                      self.status, now, now, now)
        if deliver:
            setattr(self, source, event)
        return event

    def apply_request(self, decision, accepted_at, *, physical_delay=0.0,
                      accepted=True, applies_despite_failure=False):
        self.advance(accepted_at)
        if not decision.should_call_service:
            return
        self.commands.append((accepted_at, decision.command, decision.current_p1, accepted))
        if decision.command != "set_limit" or not (accepted or applies_despite_failure):
            return
        self._sequence += 1
        seq = self._sequence
        self._events.extend(((accepted_at + physical_delay, seq, "apply", decision.current_p1),
                             (accepted_at + max(physical_delay, decision.ttl_seconds), seq, "expire", 0.0)))
        self.advance(accepted_at)


class NormalSheddingProcessTests(unittest.TestCase):
    def setup_running(self, **settings):
        options = dict(active_control=True, dry_run=False, enable_dynamic_limit=True,
                       overload_enabled=True, bounded_nominal_shedding=True,
                       nominal_import_only_fallback=False, measurement_settle_seconds=25,
                       restart_stable_seconds=60, enable_soft_guard=False,
                       enable_pause_resume=False)
        options.update(settings)
        regulator = logic.OneFuseRegulator(logic.RegulatorSettings(**options))
        self.assertTrue(regulator.import_overload_state({
            "version": 1, "used_seconds": 4087.0, "exposure_active": False,
            "transition_pending": False, "minimum_held": False,
            "failure_reason": "response_timeout"}))
        plant = Plant()
        initial = self.tick(regulator, plant, 0, grid=True, ev=True)
        self.assertEqual(initial.current_p1, 16)
        self.accept(regulator, plant, initial, 0)
        return regulator, plant

    def tick(self, r, p, now, *, grid=True, ev=False, status=None,
             trusted=True, data_valid=True, data_error=None, verified_get=False,
             **overrides):
        p.advance(now)
        if grid:
            p.capture("grid", now)
        if ev:
            p.capture("ev", now)
        # The external source-health contract knows when the grid is too old.
        grid_valid = 0 <= now - p.grid.source <= r.settings.max_sensor_age_seconds
        values = dict(now_ts=now, wall_now_ts=EPOCH + now,
                      grid_l1=p.grid.value, charger_current=p.ev.value,
                      status=status or p.ev.status,
                      grid_report_ts=EPOCH + p.grid.source,
                      charger_report_ts=EPOCH + p.ev.source,
                      charger_snapshot_verified=verified_get,
                      charger_current_trusted=trusted,
                      data_valid=data_valid and grid_valid,
                      data_error=data_error or (None if grid_valid else "grid_stale"))
        values.update(overrides)
        return r.evaluate(logic.RegulatorInput(**values))

    def accept(self, r, p, d, at, **options):
        notify_rejection = options.pop("notify_rejection", True)
        p.apply_request(d, at, **options)
        if d.should_call_service and options.get("accepted", True):
            r.acknowledge(d, at)
        elif d.should_call_service and notify_rejection:
            # Mirror the public runtime error callback, never an accepted cap.
            r.reject_submission(d, at)

    def spike(self, r, p, now=40, *, house=10.6, demand=16.0, accept=True,
              physical_delay=0.0, **overrides):
        p.house, p.demand = float(house), float(demand)
        d = self.tick(r, p, now, **overrides)
        if accept:
            self.accept(r, p, d, now, physical_delay=physical_delay)
        return d

    def assert_preserved(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 4087.0)
        self.assertFalse(state["transition_pending"])
        self.assertFalse(state["minimum_held"])

    def assert_decrease(self, d):
        self.assertEqual((d.command, d.current_p1), ("set_limit", 7))
        self.assertNotEqual(d.command, "resume")
        self.assertIsNotNone(d.nominal_response_deadline_ts)

    def test_explicit_active_option_reduces_actual_sixteen_then_verifies_seven(self):
        r, p = self.setup_running()
        d = self.spike(r, p, physical_delay=2)
        self.assert_decrease(d)
        self.assertAlmostEqual(p.current, 16)
        self.assertAlmostEqual(p.total, 26.6)
        p.capture("ev", 47)
        pending = self.tick(r, p, 47, grid=False)
        self.assertEqual(p.current, 7)
        self.assertEqual(p.total, 17.6)
        self.assertLessEqual(pending.current_p1, 7)
        complete = self.tick(r, p, 48)
        self.assertEqual(complete.current_p1, 7)
        self.assertIsNone(complete.nominal_response_deadline_ts)
        self.assert_preserved(r)

    def test_disabled_option_keeps_protective_baseline_not_a_claimed_fix(self):
        for options in ({"bounded_nominal_shedding": False}, {"overload_enabled": False}):
            with self.subTest(options=options):
                r, p = self.setup_running(**options)
                d = self.spike(r, p)
                baseline_options = dict(options, bounded_nominal_shedding=False)
                baseline, baseline_plant = self.setup_running(**baseline_options)
                ordinary = self.spike(baseline, baseline_plant)
                self.assertEqual((d.current_p1, d.mode, d.command),
                                 (ordinary.current_p1, ordinary.mode, ordinary.command))
                self.assertIsNone(d.nominal_response_deadline_ts)
                if options.get("overload_enabled", True):
                    self.assertEqual(d.current_p1, 0)
                self.assert_preserved(r)

    def test_same_cached_sixteen_can_hide_zero_ev_and_twenty_six_house(self):
        r, p = self.setup_running()
        d = self.spike(r, p, house=26.6, demand=0)
        self.assert_decrease(d)
        self.assertEqual((p.current, p.total, p.ev.value), (0, 26.6, 16))
        # Even a real new zero EV sample cannot prove a nominal seven-amp offer fits.
        self.tick(r, p, 47, ev=True)
        end = self.tick(r, p, 50)
        self.assertEqual(end.current_p1, 0)
        self.assert_preserved(r)

    def test_hidden_zero_then_late_seven_must_not_hold_thirty_three_amps(self):
        r, p = self.setup_running()
        self.assert_decrease(self.spike(r, p, house=26.0, demand=0))
        p.demand = 16
        self.assertEqual(p.total, 33)
        self.tick(r, p, 46, ev=True)
        self.tick(r, p, 47)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_new_low_ev_event_does_not_enter_old_high_state_exception(self):
        r, p = self.setup_running()
        p.house, p.demand = 24.04, 0.16
        p.capture("ev", 40)
        # The independent native status can still say charging while its new
        # numerical event already reports only0.16 A, as in the observed family.
        d = self.tick(r, p, 40, status="charging")
        self.assertAlmostEqual(p.total, 24.2)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertEqual(d.measurement_uncertainty_stops, 1)
        self.assert_preserved(r)

    def test_observed_peak_boundary_is_separate_from_post_response_budget(self):
        for raw, allowed in ((37.999, True), (38.0, True), (38.001, False)):
            with self.subTest(raw=raw):
                r, p = self.setup_running()
                d = self.spike(r, p, house=raw - 16)
                self.assertEqual(d.current_p1, 7 if allowed else 0)
                if allowed:
                    # Actual seven can still exceed the nominal 23 A budget.
                    self.tick(r, p, 46, ev=True)
                    self.tick(r, p, 47)
                    self.assertEqual(self.tick(r, p, 50).current_p1, 0)
                self.assert_preserved(r)

    def test_source_sample_age_and_slow_http_share_original_ten_seconds(self):
        r, p = self.setup_running()
        p.house = 10.6
        p.capture("grid", 37)
        d = self.tick(r, p, 40, grid=False)
        self.assert_decrease(d)
        self.assertAlmostEqual(d.nominal_response_deadline_ts, 47)
        self.accept(r, p, d, 46)
        self.assertEqual(self.tick(r, p, 47).current_p1, 0)
        self.assert_preserved(r)

    def test_late_api_acceptance_cannot_create_a_new_response_window(self):
        r, p = self.setup_running()
        d = self.spike(r, p, accept=False)
        self.assert_decrease(d)
        self.accept(r, p, d, 51)
        p.capture("ev", 51.1)
        self.assertEqual(self.tick(r, p, 51.2).current_p1, 0)
        self.assert_preserved(r)

    def test_http_ack_and_limit_readback_alone_do_not_prove_physical_response(self):
        r, p = self.setup_running()
        d = self.spike(r, p, physical_delay=100)
        deadline = d.nominal_response_deadline_ts
        for now in (41, 43, 45, 49):
            p.ev = p.ev.get_again(now)
            d = self.tick(r, p, now, verified_get=True)
            self.assertEqual(d.current_p1, 7)
            self.assertEqual(d.nominal_response_deadline_ts, deadline)
            self.accept(r, p, d, now, physical_delay=100)
        self.assertEqual(p.current, 16)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_expired_accepted_lease_cannot_open_or_continue_shedding(self):
        r, p = self.setup_running()
        # The independent physical model expires the accepted60-second lease.
        p.house = 26.6
        d = self.tick(r, p, 61)
        self.assertEqual((p.current, d.current_p1), (0, 0))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assert_preserved(r)
        r, p = self.setup_running()
        request = self.spike(r, p, now=55, accept=False)
        self.assert_decrease(request)
        self.accept(r, p, request, 55, accepted=False)
        # The old lease expires before the response deadline65.
        self.assertEqual(self.tick(r, p, 60).current_p1, 0)
        self.assert_preserved(r)

    def test_old_event_750_seconds_can_only_get_fixed_lowering_attempt(self):
        r, p = self.setup_running()
        for now in range(30, 751, 30):
            d = self.tick(r, p, now)
            self.assertEqual(d.current_p1, 16)
            self.accept(r, p, d, now)
        self.assertEqual(p.ev.source, 0)
        old_get = p.ev.get_again(755)
        p.ev = old_get
        d = self.spike(r, p, now=755, physical_delay=100, verified_get=True)
        self.assert_decrease(d)
        self.assertEqual(d.nominal_response_deadline_ts, 765)
        self.assertEqual(p.ev.source, 0)
        self.assertEqual(self.tick(r, p, 765).current_p1, 0)
        self.assert_preserved(r)

    def test_new_peak_above_envelope_cancels_before_response_deadline(self):
        r, p = self.setup_running()
        self.spike(r, p, physical_delay=100)
        p.house = 22.001
        self.assertAlmostEqual(p.total, 38.001)
        d = self.tick(r, p, 42)
        self.assertEqual(d.current_p1, 0)
        self.assert_preserved(r)

    def test_option_clock_increase_or_more_gets_cannot_extend_deadline(self):
        r, p = self.setup_running(overload_response_seconds=5)
        start = self.spike(r, p, physical_delay=100)
        r.update_settings(replace(r.settings, overload_response_seconds=10,
                                  measurement_settle_seconds=30))
        for now in (42, 43, 44):
            p.ev = p.ev.get_again(now)
            d = self.tick(r, p, now, verified_get=True)
            self.assertEqual(d.nominal_response_deadline_ts, start.nominal_response_deadline_ts)
            self.assertEqual(d.current_p1, 7)
            self.accept(r, p, d, now, physical_delay=100)
        self.assertEqual(self.tick(r, p, 45).current_p1, 0)
        self.assert_preserved(r)

    def test_unaccepted_request_and_independent_taper_do_not_complete_episode(self):
        r, p = self.setup_running()
        d = self.spike(r, p, accept=False)
        self.accept(r, p, d, 40, accepted=False)
        p.demand = 7
        self.tick(r, p, 46, ev=True)
        self.tick(r, p, 47)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_partial_physical_response_does_not_extend_deadline(self):
        r, p = self.setup_running()
        d = self.spike(r, p, physical_delay=100)
        deadline = d.nominal_response_deadline_ts
        for now, demand in ((43, 12), (46, 9), (49, 8)):
            p.demand = demand
            d = self.tick(r, p, now, ev=True)
            self.assertLessEqual(d.current_p1, 7)
            if d.current_p1 > 0:
                self.assertEqual(d.nominal_response_deadline_ts, deadline)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_old_low_event_redelivery_after_ack_is_not_response(self):
        r, p = self.setup_running()
        p.demand = 6.9
        low = p.capture("ev", 1, deliver=False)
        p.demand = 16
        self.assert_decrease(self.spike(r, p))
        p.ev = low.get_again(46)
        self.tick(r, p, 46, grid=False, verified_get=True)
        self.tick(r, p, 47)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_grid_first_drop_needs_another_grid_after_fresh_ev(self):
        r, p = self.setup_running()
        self.assert_decrease(self.spike(r, p))
        first = self.tick(r, p, 45)
        self.assertIsNotNone(first.nominal_response_deadline_ts)
        p.capture("ev", 46)
        second = self.tick(r, p, 46, grid=False)
        self.assertIsNotNone(second.nominal_response_deadline_ts)
        complete = self.tick(r, p, 47)
        self.assertEqual(complete.current_p1, 7)
        self.assertIsNone(complete.nominal_response_deadline_ts)
        self.assert_preserved(r)

    def test_same_time_ev_and_grid_do_not_satisfy_strict_later_grid(self):
        r, p = self.setup_running()
        self.spike(r, p)
        simultaneous = self.tick(r, p, 46, ev=True)
        self.assertIsNotNone(simultaneous.nominal_response_deadline_ts)
        complete = self.tick(r, p, 47)
        self.assertIsNone(complete.nominal_response_deadline_ts)
        self.assert_preserved(r)

    def test_first_ev_response_does_not_qualify_grid_older_than_latest_ev(self):
        r, p = self.setup_running()
        self.spike(r, p)
        self.tick(r, p, 43, grid=False, ev=True)  # First real seven-amp event.
        p.house, p.demand = 16, 1
        older_grid = p.capture("grid", 44, deliver=False)  # Physical total17.
        p.house, p.demand = 20, 7
        p.capture("ev", 45)  # Later EV7, now physical total27.
        p.grid = older_grid.get_again(45)
        pending = self.tick(r, p, 45, grid=False)
        self.assertEqual((p.current, p.total), (7, 27))
        self.assertIsNotNone(pending.nominal_response_deadline_ts)
        self.assertNotEqual(pending.mode, "normal_shedding_complete")
        # A genuinely later current grid cannot establish nominal headroom.
        self.tick(r, p, 46)
        self.assertEqual(self.tick(r, p, 50).current_p1, 0)
        self.assert_preserved(r)

    def test_source_fault_stale_grid_and_manual_policy_interrupt_pending(self):
        cases = ({"data_valid": False, "data_error": "heartbeat_stale"},
                 {"trusted": False}, {"status": "fault"},
                 {"status": "paused"}, {"status": "awaiting_scheduled_start"},
                 {"status": "awaiting_authorization"},
                 {"grid_report_ts": EPOCH - 100},
                 {"grid_report_ts": EPOCH + 60},
                 {"grid_report_ts": None})
        for values in cases:
            with self.subTest(values=values):
                r, p = self.setup_running()
                self.spike(r, p, physical_delay=100)
                d = self.tick(r, p, 42, **values)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.command, "resume")
                self.assert_preserved(r)

    def test_updated_bounds_or_disabled_control_cannot_keep_old_seven_permission(self):
        for options in ({"bounded_nominal_shedding": False}, {"overload_enabled": False},
                        {"enable_dynamic_limit": False, "overload_enabled": False}, {"min_charge_amps": 8},
                        {"max_charge_amps": 6}):
            with self.subTest(options=options):
                r, p = self.setup_running()
                self.spike(r, p, physical_delay=100)
                r.update_settings(replace(r.settings, **options))
                d = self.tick(r, p, 42)
                self.assertEqual(d.current_p1, 0)
                self.assert_preserved(r)

    def test_soft_and_pause_guards_win_during_pending(self):
        for options in ({"enable_soft_guard": True, "soft_limit_amps": 20,
                         "soft_for_seconds": 0, "soft_current_amps": 0},
                        {"enable_pause_resume": True, "pause_limit_amps": 20,
                         "pause_for_seconds": 0}):
            with self.subTest(options=options):
                r, p = self.setup_running()
                self.spike(r, p, physical_delay=100)
                r.update_settings(replace(r.settings, **options))
                d = self.tick(r, p, 42)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.command, "resume")
                self.assert_preserved(r)

    def test_failed_response_restarts_only_at_seven_after_full_stability(self):
        r, p = self.setup_running()
        self.spike(r, p, physical_delay=100)
        stop = self.tick(r, p, 50)
        self.assertEqual(stop.current_p1, 0)
        self.accept(r, p, stop, 50)
        p.house = 2
        first = self.tick(r, p, 51, ev=True)
        self.assertEqual(first.current_p1, 0)
        for now in (61, 81, 101):
            d = self.tick(r, p, now, ev=True)
            self.assertEqual(d.current_p1, 0)
        restart = self.tick(r, p, 112, ev=True)
        self.assertEqual(restart.current_p1, 7)
        self.assertNotEqual(restart.command, "resume")
        self.accept(r, p, restart, 112)
        self.assertLessEqual(r.last_target[0], 7)
        self.assert_preserved(r)

    def test_two_capped_retries_then_third_source_failure_requires_attention(self):
        r, p = self.setup_running()
        # HTTP accepts the initial decrease; the device never applies it.
        self.spike(r, p, physical_delay=10_000)
        stop = self.tick(r, p, 50)
        self.accept(r, p, stop, 50)
        self.assertEqual(stop.measurement_uncertainty_stops, 1)
        p.house = 2
        for now in (51, 71, 91, 110):
            self.assertEqual(self.tick(r, p, now, ev=True).current_p1, 0)
        restart1 = self.tick(r, p, 112, ev=True)
        self.assertEqual(restart1.current_p1, 7)
        self.accept(r, p, restart1, 112)
        # A genuinely invalid provenance event must still cause protective0.
        stop2 = self.tick(r, p, 113, ev=True, trusted=False)
        self.assertEqual((stop2.current_p1, stop2.measurement_uncertainty_stops), (0, 2))
        self.accept(r, p, stop2, 113)
        for now in (114, 154, 194, 232):
            self.assertEqual(self.tick(r, p, now, ev=True).current_p1, 0)
        restart2 = self.tick(r, p, 234, ev=True)
        self.assertEqual(restart2.current_p1, 7)
        self.accept(r, p, restart2, 234)
        stop3 = self.tick(r, p, 235, ev=True, trusted=False)
        self.assertEqual((stop3.current_p1, stop3.measurement_uncertainty_stops), (0, 3))
        self.accept(r, p, stop3, 235)
        for now in (236, 300, 600):
            blocked = self.tick(r, p, now, ev=True)
            self.assertEqual((blocked.current_p1, blocked.measurement_uncertainty_stops), (0, 3))
            self.assertNotEqual(blocked.command, "resume")
        positive_after_failure = [cap for at, command, cap, accepted in p.commands
                                  if at > 50 and command == "set_limit" and cap > 0]
        self.assertEqual(positive_after_failure, [7, 7])
        self.assert_preserved(r)
        # A real exported block survives a new process/monotonic-clock origin.
        saved = r.export_overload_state()
        restored = logic.OneFuseRegulator(r.settings)
        self.assertTrue(restored.import_overload_state(saved))
        fresh_plant = Plant(demand=0, applied=0)
        for now in (0, 60, 180, 600):
            after_reload = self.tick(restored, fresh_plant, now, ev=True)
            self.assertEqual((after_reload.current_p1, after_reload.measurement_uncertainty_stops), (0, 3))
        self.assert_preserved(restored)

    def test_process_reload_interrupts_pending_without_new_window_or_budget(self):
        r, p = self.setup_running()
        self.spike(r, p, physical_delay=10_000)
        saved = r.export_overload_state()
        self.assertTrue(saved["normal_control"]["response_pending"])
        restored = logic.OneFuseRegulator(r.settings)
        self.assertTrue(restored.import_overload_state(saved, elapsed_seconds=5))
        fresh_plant = Plant(demand=0, applied=0)
        for now in (0, 20, 40, 59):
            d = self.tick(restored, fresh_plant, now, ev=True)
            self.assertEqual(d.current_p1, 0)
            self.assertIsNone(d.nominal_response_deadline_ts)
            self.assertEqual(d.measurement_uncertainty_stops, 1)
        restart = self.tick(restored, fresh_plant, 61, ev=True)
        self.assertEqual(restart.current_p1, 7)
        self.assert_preserved(restored)
        self.assertGreaterEqual(restored.export_overload_state()["used_seconds"], 4092)

    def test_repeated_no_ack_recovery_submissions_are_bounded_without_crash(self):
        r, p = self.setup_running()
        self.spike(r, p, physical_delay=10_000)
        stop = self.tick(r, p, 50)
        self.accept(r, p, stop, 50)
        p.house = 2
        positive_attempts = []
        final = stop
        for now in range(51, 602):
            final = self.tick(r, p, now, ev=True)
            if final.should_call_service and final.command == "set_limit" and final.current_p1 > 0:
                positive_attempts.append((now, final.current_p1))
            # Every HTTP request fails. Its public rejection callback receives
            # the outcome, without inventing an ACK or applying the offer.
            self.accept(r, p, final, now, accepted=False)
        self.assertEqual(p.current, 0)
        self.assertEqual(len(positive_attempts), 2, positive_attempts)
        self.assertTrue(all(cap == 7 for _, cap in positive_attempts))
        self.assertGreaterEqual(positive_attempts[1][0] - positive_attempts[0][0], 60)
        self.assertEqual((final.current_p1, final.measurement_uncertainty_stops), (0, 3))
        self.assert_preserved(r)

    def test_missing_outcome_callback_cannot_repeat_the_reserved_positive_retry(self):
        r, p = self.setup_running()
        self.spike(r, p, physical_delay=10_000)
        stop = self.tick(r, p, 50)
        self.accept(r, p, stop, 50)
        p.house = 2
        positive_attempts = []
        for now in range(51, 602):
            d = self.tick(r, p, now, ev=True)
            if d.should_call_service and d.command == "set_limit" and d.current_p1 > 0:
                positive_attempts.append((now, d.current_p1))
            self.accept(r, p, d, now, accepted=False, notify_rejection=False)
        self.assertEqual(positive_attempts, [(111, 7)])
        self.assertEqual((p.current, r.last_target[0]), (0, 0))
        self.assertTrue(r.export_overload_state()["normal_control"]["retry_submission_pending"])
        self.assert_preserved(r)


if __name__ == "__main__":
    unittest.main()
