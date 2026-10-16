"""Load-step, minimum-current continuity and recovery policy regressions.

These are physical scenarios with accepted-command and subsequent telemetry
steps. They never contact Home Assistant, Easee, or a charging vehicle.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_continuity_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class ContinuityTests(unittest.TestCase):
    def regulator(self, **options):
        # Keep the original 6 A start policy scenarios explicit; the separate
        # startup/alignment suite covers the new default and start/run split.
        options.setdefault("startup_charge_amps", 6)
        options.setdefault("fast_recovery", False)
        return logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, **options,
        ))

    def sample(self, regulator, now, house=8, ev=16, status="charging", *,
               accept=True, grid=None, **metadata):
        decision = regulator.evaluate(logic.RegulatorInput(
            now_ts=now, status=status,
            grid_l1=house + ev if grid is None else grid,
            charger_current=ev, **metadata,
        ))
        self.assertNotIn(decision.current_p1, range(1, 6))
        if accept:
            regulator.acknowledge(decision, now)
        return decision

    def pending_reduction(self, *, house=17, accept=True, **options):
        regulator = self.regulator(**options)
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=house, accept=accept,
                    grid_report_ts=101, charger_report_ts=100)
        return regulator

    def test_appliance_boost_sheds_to_seven_instead_of_stopping(self):
        regulator = self.regulator()
        self.assertEqual(self.sample(regulator, 0, house=5).current_p1, 16)
        decrease = self.sample(regulator, 1, house=16)
        self.assertEqual(decrease.current_p1, 7)
        self.assertTrue(decrease.should_call_service)

    def test_minimum_uses_buffer_without_intentional_overload(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        minimum = self.sample(regulator, 1, house=18)
        self.assertEqual((minimum.mode, minimum.current_p1), ("minimum_continuity", 6))
        for now in (10, 60, 180):
            self.assertEqual(self.sample(regulator, now, house=18, ev=6).current_p1, 6)

    def test_minimum_reserve_boundary_is_respected(self):
        for house, expected in ((18.5, 6), (18.51, 0), (19, 0), (20, 0)):
            with self.subTest(house=house):
                regulator = self.regulator()
                self.sample(regulator, 0, house=5)
                self.assertEqual(self.sample(regulator, 1, house=house).current_p1, expected)

    def test_house_jitter_around_buffer_boundary_keeps_minimum(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=17, ev=6)
        for now, house in enumerate((17.2, 16.8, 17.1, 17.4, 16.9), start=10):
            self.assertEqual(self.sample(regulator, now, house=house, ev=6).current_p1, 6)

    def test_buffer_cannot_start_a_car_that_is_not_charging(self):
        for status, ev in (("awaiting_start", 0), ("paused", 0), ("charging", 0)):
            with self.subTest(status=status):
                regulator = self.regulator()
                self.assertEqual(self.sample(regulator, 0, house=18, ev=ev, status=status).current_p1, 0)
                self.assertEqual(self.sample(regulator, 120, house=18, ev=ev, status=status).current_p1, 0)

    def test_takeover_seeds_from_measured_current_without_maximum_jump(self):
        for measured, expected in ((8, 8), (8.9, 8), (5.8, 6), (16, 16)):
            with self.subTest(measured=measured):
                self.assertEqual(self.sample(self.regulator(), 0, house=5, ev=measured).current_p1, expected)

    def test_rise_waits_for_stability_then_one_amp_per_interval(self):
        regulator = self.regulator()
        self.assertEqual(self.sample(regulator, 0, house=5, ev=8).current_p1, 8)
        self.assertEqual(self.sample(regulator, 5, house=5, ev=8).current_p1, 8)
        self.assertEqual(self.sample(regulator, 34, house=5, ev=8).current_p1, 8)
        self.assertEqual(self.sample(regulator, 35, house=5, ev=8).current_p1, 9)
        self.assertEqual(self.sample(regulator, 49, house=5, ev=9).current_p1, 9)
        self.assertEqual(self.sample(regulator, 50, house=5, ev=9).current_p1, 10)
        self.assertEqual(self.sample(regulator, 65, house=5, ev=10).current_p1, 11)

    def test_ttl_refresh_does_not_reset_rise_stability(self):
        regulator = self.regulator(increase_stable_seconds=60)
        self.sample(regulator, 0, house=5, ev=8)
        self.sample(regulator, 1, house=5, ev=8)
        refresh = self.sample(regulator, 30, house=5, ev=8)
        self.assertTrue(refresh.should_call_service)
        self.assertEqual(self.sample(regulator, 61, house=5, ev=8).current_p1, 9)

    def test_reduced_headroom_restarts_the_rise_stability_period(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, ev=6)
        self.sample(regulator, 1, house=5, ev=6)
        self.assertEqual(self.sample(regulator, 25, house=12, ev=6).current_p1, 6)
        self.assertEqual(self.sample(regulator, 26, house=12, ev=6).current_p1, 6)
        self.assertEqual(self.sample(regulator, 55, house=12, ev=6).current_p1, 6)
        self.assertEqual(self.sample(regulator, 56, house=12, ev=6).current_p1, 7)

    def test_boost_decrease_bypasses_rise_interval_and_action_cooldown(self):
        regulator = self.regulator(min_seconds_between_actions=20)
        self.sample(regulator, 0, house=5, ev=8)
        self.sample(regulator, 1, house=5, ev=8)
        self.sample(regulator, 31, house=5, ev=8)
        decision = self.sample(regulator, 32, house=16, ev=9)
        self.assertEqual(decision.current_p1, 7)
        self.assertTrue(decision.should_call_service)

    def test_recovery_from_zero_waits_then_starts_at_six(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        self.assertEqual(self.sample(regulator, 1, house=20).current_p1, 0)
        self.assertEqual(self.sample(regulator, 5, house=5, ev=0, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(regulator, 64, house=5, ev=0, status="awaiting_start").current_p1, 0)
        first = self.sample(regulator, 65, house=5, ev=0, status="awaiting_start")
        self.assertEqual(first.current_p1, 6)
        self.assertNotEqual(first.command, "resume")
        self.assertEqual(self.sample(regulator, 70, house=5, ev=6).current_p1, 6)
        self.assertEqual(self.sample(regulator, 100, house=5, ev=6).current_p1, 7)

    def test_zero_ttl_refresh_does_not_reset_restart_observation(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=20, ev=0)
        self.sample(regulator, 5, house=5, ev=0, status="awaiting_start")
        self.assertEqual(self.sample(regulator, 35, house=5, ev=0, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(regulator, 65, house=5, ev=0, status="awaiting_start").current_p1, 6)

    def test_unstable_restart_capacity_resets_sixty_second_observation(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, ev=0, status="awaiting_start")
        self.sample(regulator, 55, house=18, ev=0, status="awaiting_start")
        self.sample(regulator, 56, house=5, ev=0, status="awaiting_start")
        self.assertEqual(self.sample(regulator, 115, house=5, ev=0, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(regulator, 116, house=5, ev=0, status="awaiting_start").current_p1, 6)

    def test_zero_limit_must_be_physically_observed_before_restarting(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        self.sample(regulator, 1, house=20)
        self.assertEqual(self.sample(regulator, 70, house=5, ev=16).current_p1, 0)
        self.assertEqual(self.sample(regulator, 71, house=5, ev=0, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(regulator, 131, house=5, ev=0, status="awaiting_start").current_p1, 6)

    def test_no_rise_until_accepted_decrease_is_observed(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        self.sample(regulator, 1, house=16)
        for now in (5, 40, 100):
            self.assertEqual(self.sample(regulator, now, house=5, ev=16).current_p1, 7)
        self.assertEqual(self.sample(regulator, 105, house=5, ev=7).current_p1, 7)
        self.assertEqual(self.sample(regulator, 135, house=5, ev=7).current_p1, 8)

    def test_new_ev_report_waits_briefly_for_grid_report_after_decrease(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        mixed = self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual((mixed.mode, mixed.current_p1), ("settling", 6))
        self.assertFalse(mixed.should_call_service)
        coherent = self.sample(regulator, 3, house=18, ev=6, grid_report_ts=103, charger_report_ts=102)
        self.assertEqual(coherent.current_p1, 6)
        self.assertNotEqual(coherent.mode, "settling")

    def test_grid_settling_expires_after_three_seconds(self):
        regulator = self.regulator(measurement_settle_seconds=3)
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual(self.sample(regulator, 4, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 6)
        self.assertEqual(self.sample(regulator, 5, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 0)

    def test_default_settling_waits_for_five_second_grid_cadence(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual(self.sample(regulator, 7, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 6)
        coherent = self.sample(regulator, 8, house=18, ev=6, grid_report_ts=108, charger_report_ts=102)
        self.assertEqual(coherent.current_p1, 6)
        self.assertNotEqual(coherent.mode, "settling")

    def test_default_settling_expires_after_ten_seconds(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual(self.sample(regulator, 11, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 6)
        self.assertEqual(self.sample(regulator, 12, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 0)

    def test_settling_window_cannot_exceed_maximum_sensor_age(self):
        regulator = self.regulator(measurement_settle_seconds=30, max_sensor_age_seconds=15)
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual(self.sample(regulator, 16, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 6)
        self.assertEqual(self.sample(regulator, 17, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102).current_p1, 0)

    def test_new_high_grid_report_is_not_hidden_by_settling(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        self.assertEqual(self.sample(regulator, 3, house=20, ev=6, grid_report_ts=103, charger_report_ts=102).current_p1, 0)

    def test_data_error_overrides_pending_grid_settling(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, grid_report_ts=100, charger_report_ts=100)
        self.sample(regulator, 1, house=18, grid_report_ts=101, charger_report_ts=100)
        self.sample(regulator, 2, grid=34, ev=6, grid_report_ts=101, charger_report_ts=102)
        fault = self.sample(regulator, 2.1, grid=34, ev=6, data_valid=False,
                            grid_report_ts=101, charger_report_ts=102)
        self.assertEqual((fault.mode, fault.current_p1), ("unavailable", 0))

    def test_missing_timestamps_use_conservative_zero_for_mixed_snapshot(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        self.sample(regulator, 1, house=18)
        self.assertEqual(self.sample(regulator, 2, grid=34, ev=6).current_p1, 0)

    def test_grid_first_reduction_holds_accepted_cap_until_ev_report_arrives(self):
        regulator = self.pending_reduction()
        mixed = self.sample(regulator, 2, grid=11, ev=16,
                            grid_report_ts=102, charger_report_ts=100)
        self.assertEqual((mixed.mode, mixed.command, mixed.current_p1), ("settling", "set_limit", 6))
        self.assertEqual(mixed.ttl_seconds, 60)
        self.assertFalse(mixed.should_call_service)
        coherent = self.sample(regulator, 3, house=5, ev=6,
                               grid_report_ts=102, charger_report_ts=103)
        self.assertEqual(coherent.current_p1, 6)
        self.assertNotEqual(coherent.mode, "unavailable")
        self.assertEqual(self.sample(regulator, 33, house=5, ev=6,
                                     grid_report_ts=133, charger_report_ts=103).current_p1, 7)

    def test_grid_first_hold_requires_conservative_grid_plus_cap_budget(self):
        for grid, expected in ((14.5, 10), (14.6, 0)):
            with self.subTest(grid=grid):
                regulator = self.pending_reduction(house=13)
                decision = self.sample(regulator, 2, grid=grid, ev=16,
                                       grid_report_ts=102, charger_report_ts=100)
                self.assertEqual(decision.current_p1, expected)
                self.assertEqual(decision.mode, "settling" if expected else "unavailable")

    def test_grid_first_hold_requires_an_accepted_positive_reduction(self):
        for setup in ({"accept": False}, {"house": 20}):
            with self.subTest(setup=setup):
                regulator = self.pending_reduction(**setup)
                decision = self.sample(regulator, 2, grid=11, ev=16,
                                       grid_report_ts=102, charger_report_ts=100)
                self.assertEqual((decision.mode, decision.current_p1), ("unavailable", 0))

    def test_grid_first_hold_requires_new_grid_and_known_old_ev_timestamps(self):
        for metadata in ({"grid_report_ts": None, "charger_report_ts": 100},
                         {"grid_report_ts": 102, "charger_report_ts": None},
                         {}, {"grid_report_ts": 101, "charger_report_ts": 100},
                         {"grid_report_ts": 102, "charger_report_ts": 102}):
            with self.subTest(metadata=metadata):
                regulator = self.pending_reduction()
                decision = self.sample(regulator, 2, grid=11, ev=16, **metadata)
                self.assertEqual((decision.mode, decision.current_p1), ("unavailable", 0))

    def test_grid_first_hold_expires_without_reset_by_grid_updates_or_ttl_refresh(self):
        regulator = self.pending_reduction(resend_limit_every_seconds=5)
        self.sample(regulator, 2, grid=11, ev=16, grid_report_ts=102, charger_report_ts=100)
        renewed = self.sample(regulator, 6, grid=10, ev=16,
                              grid_report_ts=106, charger_report_ts=100)
        self.assertTrue(renewed.should_call_service)
        self.assertEqual(renewed.current_p1, 6)
        self.assertEqual(self.sample(regulator, 11, grid=9, ev=16,
                                     grid_report_ts=111, charger_report_ts=100).current_p1, 6)
        expired = self.sample(regulator, 12, grid=8, ev=16,
                              grid_report_ts=112, charger_report_ts=100)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))

    def test_grid_first_hold_ends_when_new_grid_exceeds_its_budget(self):
        regulator = self.pending_reduction(house=13)
        self.sample(regulator, 2, grid=14.5, ev=16,
                    grid_report_ts=102, charger_report_ts=100)
        unsafe = self.sample(regulator, 3, grid=14.6, ev=16,
                             grid_report_ts=103, charger_report_ts=100)
        self.assertEqual((unsafe.mode, unsafe.current_p1), ("unavailable", 0))

    def test_grid_first_hold_never_overrides_stale_invalid_or_fault_telemetry(self):
        for metadata in ({"data_valid": False, "data_error": "stale"},
                         {"status": "offline"}, {"grid": "unavailable"}):
            with self.subTest(metadata=metadata):
                regulator = self.pending_reduction()
                sample = {"grid": 11, "ev": 16, "grid_report_ts": 102, "charger_report_ts": 100}
                sample.update(metadata)
                fault = self.sample(regulator, 2, **sample)
                self.assertEqual((fault.mode, fault.current_p1), ("unavailable", 0))

    def test_grid_first_hold_never_issues_resume(self):
        regulator = self.pending_reduction(resume_for_seconds=0)
        regulator.paused_by_us = True
        held = self.sample(regulator, 2, grid=11, ev=16, status="awaiting_start",
                           grid_report_ts=102, charger_report_ts=100)
        self.assertEqual((held.command, held.current_p1), ("set_limit", 6))
        self.assertTrue(regulator.paused_by_us)

    def test_grid_first_hold_updates_continuous_grid_guard_observations(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=13, ev=16, grid_report_ts=100, charger_report_ts=100)
        self.assertEqual(regulator.above_pause_since, 0)
        low = self.sample(regulator, 119, grid=14, ev=16,
                          grid_report_ts=200, charger_report_ts=100)
        self.assertEqual((low.mode, low.current_p1), ("settling", 10))
        self.assertIsNone(regulator.above_soft_since)
        self.assertIsNone(regulator.above_pause_since)
        self.assertEqual(regulator.below_resume_since, 119)
        high_again = self.sample(regulator, 120, house=13, ev=16,
                                 grid_report_ts=201, charger_report_ts=201)
        self.assertNotEqual(high_again.command, "pause")
        self.assertEqual(regulator.above_pause_since, 120)
        self.assertIsNone(regulator.below_resume_since)
        sustained = self.sample(regulator, 240, house=13, ev=16,
                                grid_report_ts=202, charger_report_ts=202)
        self.assertEqual(sustained.command, "pause")

    def test_idle_feedback_precedes_consistency_but_not_invalid_or_stale_data(self):
        for status, valid in (("completed", True), ("disconnected", True), ("completed", False)):
            with self.subTest(status=status, valid=valid):
                regulator = self.pending_reduction()
                idle = self.sample(regulator, 2, grid=2.7, ev=10.672, status=status,
                                   data_valid=valid,
                                   grid_report_ts=102, charger_report_ts=100)
                if valid:
                    self.assertEqual((idle.mode, idle.command), ("idle", None))
                    self.assertFalse(idle.should_call_service)
                else:
                    self.assertEqual((idle.mode, idle.current_p1), ("unavailable", 0))

    def test_manual_pause_is_not_resumed_and_not_ramped_up(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5, ev=0, status="paused")
        for now in (60, 120, 180):
            paused = self.sample(regulator, now, house=5, ev=0, status="paused")
            self.assertEqual(paused.current_p1, 6)
            self.assertNotEqual(paused.command, "resume")
            self.assertFalse(regulator.paused_by_us)

    def test_explicit_owned_pause_requires_fresh_positive_cap_before_resume(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=23, ev=6)
        paused = self.sample(regulator, 121, house=23, ev=6)
        self.assertEqual(paused.command, "pause")
        self.sample(regulator, 125, house=5, ev=0, status="awaiting_start")
        self.sample(regulator, 185, house=5, ev=0, status="awaiting_start")
        refresh = self.sample(regulator, 246, house=5, ev=0, status="awaiting_start")
        self.assertEqual((refresh.command, refresh.current_p1), ("set_limit", 6))
        resume = self.sample(regulator, 251, house=5, ev=0, status="awaiting_start")
        self.assertEqual((resume.command, resume.current_p1), ("resume", 6))

    def test_soft_zero_is_not_undone_by_minimum_continuity(self):
        regulator = self.regulator(soft_limit_amps=20, soft_for_seconds=0, soft_current_amps=0)
        self.assertEqual(self.sample(regulator, 0, house=16, ev=6).current_p1, 0)

    def test_disconnect_requires_new_stable_start_instead_of_old_full_cap(self):
        regulator = self.regulator()
        self.sample(regulator, 0, house=5)
        self.sample(regulator, 1, house=5, ev=0, status="disconnected")
        self.assertEqual(self.sample(regulator, 5, house=5, ev=0, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(regulator, 65, house=5, ev=0, status="awaiting_start").current_p1, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
