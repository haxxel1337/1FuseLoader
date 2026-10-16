"""Fast recovery requires explicit fresh receipts and coherent actual charging.

These pure-regulator scenarios submit no commands outside the in-memory model.
UTC epoch report clocks are kept separate from monotonic control timers.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_fast_recovery_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class FastRecoveryTests(unittest.TestCase):
    def regulator(self, **settings):
        return logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False, **settings))

    def sample(self, r, now, grid, ev, status="charging", *, accept=True, **extra):
        data = {"grid_report_ts": 1000+now, "charger_report_ts": 1000+now, "wall_now_ts": 1000+now}
        data.update(extra)
        d = r.evaluate(logic.RegulatorInput(now_ts=now, status=status, grid_l1=grid,
                                           charger_current=ev, **data))
        if accept:
            r.acknowledge(d, now)
        return d

    def waiting_start(self, **settings):
        r = self.regulator(**settings)
        self.sample(r, 0, 2.6, 0.013, "awaiting_start")
        offered = self.sample(r, 60, 2.6, 0.013, "ready_to_charge")
        self.assertEqual(offered.current_p1, 7)
        return r

    def test_default_fast_recovery_ten_amp_draw_and_fourteen_grid_goes_directly_sixteen(self):
        r = self.regulator()
        d = self.sample(r, 0, 14, 10)
        self.assertTrue(r.settings.fast_recovery)
        self.assertEqual((d.mode, d.command, d.current_p1, d.available_l1), ("normal", "set_limit", 16, 16))
        self.assertTrue(d.should_call_service)
        self.assertEqual((d.current_p2, d.current_p3), (0, 0))
        self.assertEqual(r._positive_cap, 16)

    def test_fast_recovery_honors_configured_car_maximum(self):
        d = self.sample(self.regulator(max_charge_amps=14), 0, 14, 10)
        self.assertEqual(d.current_p1, 14)

    def test_fast_recovery_uses_house_load_and_full_buffer_not_raw_headroom_label(self):
        d = self.sample(self.regulator(), 0, 22, 10)
        self.assertEqual((d.house_load_l1, d.available_l1, d.current_p1), (12, 11, 11))

    def test_fresh_headroom_can_increase_accepted_ten_directly_to_sixteen_without_legacy_delay(self):
        r = self.regulator()
        self.sample(r, 0, 23, 10)
        raised = self.sample(r, 5, 14, 10)
        self.assertEqual(raised.current_p1, 16)
        self.assertTrue(raised.should_call_service)

    def test_positive_fast_jump_retains_alignment_safeguards(self):
        r = self.regulator()
        self.sample(r, 0, 14, 10)
        ev_first = self.sample(r, 5, 14, 16, grid_report_ts=1004, charger_report_ts=1005)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("settling", 16))
        aligned = self.sample(r, 6, 20, 16, grid_report_ts=1006, charger_report_ts=1005)
        self.assertEqual(aligned.current_p1, 16)
        self.assertIsNone(r._positive_cap)

    def test_high_household_load_lowers_immediately_without_fast_ramp_delay(self):
        r = self.regulator()
        self.sample(r, 0, 14, 10)
        self.sample(r, 6, 20, 16, grid_report_ts=1006, charger_report_ts=1005)
        lower = self.sample(r, 7, 34, 16)
        self.assertEqual(lower.current_p1, 6)
        self.assertTrue(lower.should_call_service)

    def test_insufficient_household_budget_remains_zero(self):
        r = self.regulator()
        self.sample(r, 0, 14, 10)
        d = self.sample(r, 7, 36, 16)
        self.assertEqual(d.current_p1, 0)

    def test_invalid_and_stale_data_never_fast_increases(self):
        for extra in ({"data_valid": False, "data_error": "stale"},):
            d = self.sample(self.regulator(), 0, 14, 10, **extra)
            self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_fault_status_never_fast_increases(self):
        d = self.sample(self.regulator(), 0, 14, 10, "error")
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_missing_or_nonfinite_absolute_clock_waits_at_measured_takeover_cap(self):
        for wall in (None, True, float("nan"), float("inf")):
            with self.subTest(wall=wall):
                d = self.sample(self.regulator(), 0, 14, 10, wall_now_ts=wall)
                self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_missing_or_invalid_report_timestamps_never_open_fast_gate(self):
        for extra in ({"grid_report_ts": None}, {"charger_report_ts": None},
                      {"grid_report_ts": True}, {"charger_report_ts": float("nan")}):
            with self.subTest(extra=extra):
                d = self.sample(self.regulator(), 0, 14, 10, **extra)
                self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_old_ev_receipt_is_not_made_fresh_by_grid_or_stream_health(self):
        d = self.sample(self.regulator(), 0, 14, 10, wall_now_ts=2000,
                        grid_report_ts=2000, charger_report_ts=1000)
        self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_two_equally_old_reports_fail_absolute_age_check_even_with_zero_skew(self):
        d = self.sample(self.regulator(), 0, 14, 10, wall_now_ts=2000,
                        grid_report_ts=1000, charger_report_ts=1000)
        self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_future_reports_outside_clock_tolerance_never_open_fast_gate(self):
        d = self.sample(self.regulator(), 0, 14, 10, grid_report_ts=1006, charger_report_ts=1006)
        self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_report_skew_beyond_alignment_window_waits_at_current_cap(self):
        d = self.sample(self.regulator(), 0, 14, 10, grid_report_ts=1000, charger_report_ts=989)
        self.assertEqual((d.mode, d.current_p1), ("waiting_for_fresh_sources", 10))

    def test_explicit_fresh_report_after_freshness_wait_can_recover_directly(self):
        r = self.regulator()
        self.sample(r, 0, 14, 10, charger_report_ts=800)
        d = self.sample(r, 5, 14, 10)
        self.assertEqual(d.current_p1, 16)
        self.assertTrue(d.should_call_service)

    def test_unobserved_accepted_reduction_prevents_fast_increase(self):
        r = self.regulator()
        self.sample(r, 0, 21, 16)
        self.assertEqual(self.sample(r, 5, 34, 16).current_p1, 6)
        d = self.sample(r, 30, 21, 16)
        self.assertEqual(d.current_p1, 6)
        self.assertEqual(r._awaiting_reduction_target, 6)

    def test_partial_positive_response_does_not_unlock_fast_recovery(self):
        r = self.waiting_start()
        self.sample(r, 66, 2.6, 3, grid_report_ts=1065, charger_report_ts=1066)
        partial = self.sample(r, 67, 5.6, 3, grid_report_ts=1067, charger_report_ts=1066)
        self.assertTrue(r._positive_reports_advanced())
        self.assertIsNotNone(r._positive_cap)
        self.assertEqual((partial.mode, partial.current_p1), ("waiting_for_aligned_sources", 7))

    def test_full_coherent_start_response_can_recover_directly_to_sixteen(self):
        r = self.waiting_start()
        self.sample(r, 66, 2.6, 6.94, grid_report_ts=1065, charger_report_ts=1066)
        complete = self.sample(r, 67, 9.6, 6.94, grid_report_ts=1067, charger_report_ts=1066)
        self.assertEqual(complete.current_p1, 16)
        self.assertTrue(complete.should_call_service)
        self.assertEqual(r._positive_cap, 16)

    def test_idle_and_leakage_never_jump_to_maximum(self):
        for status, ev in (("ready_to_charge", 0.013), ("awaiting_start", 0.013), ("charging", 0.181)):
            with self.subTest(status=status):
                r = self.regulator()
                for at in (0, 60, 120, 600):
                    d = self.sample(r, at, 2.6, ev, status)
                    self.assertLessEqual(d.current_p1, 7)
                    self.assertNotEqual(d.command, "resume")

    def test_manual_and_scheduled_waits_remain_protected(self):
        for status in ("paused", "awaiting_smart_start", "awaiting_scheduled_start",
                       "awaiting_authorization", "paused_due_to_equalizer"):
            with self.subTest(status=status):
                r = self.regulator()
                for at in (0, 60, 120, 600):
                    d = self.sample(r, at, 2.6, 0.013, status)
                    self.assertLessEqual(d.current_p1, 6)
                    self.assertNotEqual(d.command, "resume")

    def test_legacy_mode_retains_gradual_takeover_and_one_amp_recovery(self):
        r = self.regulator(fast_recovery=False)
        self.assertEqual(self.sample(r, 0, 14, 10).current_p1, 10)
        self.sample(r, 1, 14, 10)
        self.assertEqual(self.sample(r, 31, 14, 10).current_p1, 11)

    def test_fast_recovery_requires_a_boolean_setting(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.regulator(fast_recovery=value)

    def test_pending_alignment_timeout_safe_hold_blocks_fast_recovery_and_resume(self):
        r = self.waiting_start(resume_for_seconds=0)
        self.sample(r, 66, 2.6, 6.94, grid_report_ts=1065, charger_report_ts=1066)
        r.paused_by_us = True
        r._pause_observed = True
        d = self.sample(r, 77, 9.3, 6.94, "awaiting_start",
                        grid_report_ts=1065, charger_report_ts=1066)
        self.assertEqual((d.mode, d.command, d.current_p1),
                         ("waiting_for_aligned_sources", "set_limit", 7))
        self.assertIsNotNone(r._positive_cap)
        self.assertEqual(d.measurement_alignment_remaining_seconds, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
