"""Measured partial reductions, ordered receipts and bounded uncertainty.

These are pure controller tests; acknowledgement simulates API acceptance only.
No Home Assistant, network, charger or household commands are used.
"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_partial_reduction_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class PartialReductionTests(unittest.TestCase):
    def regulator(self, **options):
        return logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, **options))

    def sample(self, r, now, grid, ev, *, grid_ts=None, ev_ts=None,
               accept=True, status="charging", **extra):
        d = r.evaluate(logic.RegulatorInput(
            now_ts=now, grid_l1=grid, charger_current=ev, status=status,
            grid_report_ts=grid_ts, charger_report_ts=ev_ts,
            wall_now_ts=1000+now, **extra))
        if accept:
            r.acknowledge(d, now)
        return d

    def pending(self, *, accept=True, **options):
        r = self.regulator(**options)
        self.sample(r, 0, 18.2, 16.2600002288818, grid_ts=1000, ev_ts=1000)
        lower = self.sample(r, 1, 29.8, 16.2600002288818,
                            grid_ts=1001, ev_ts=1000, accept=accept)
        self.assertEqual(lower.current_p1, 9)
        return r

    def partial(self, r, now=2, ev=9.71100044250488, **extra):
        return self.sample(r, now, 29.8, ev, grid_ts=1001, ev_ts=1000+now, **extra)

    def test_real_stove_trace_partial_response_above_cap_tolerance_holds_nine(self):
        r = self.pending(measurement_settle_seconds=25)
        d = self.partial(r, now=3.265)
        self.assertEqual((d.mode, d.current_p1), ("settling", 9))
        self.assertAlmostEqual(d.house_load_l1, 13.5399997711182)
        self.assertFalse(d.should_call_service)
        aligned = self.sample(r, 5.862, 23.251, 9.71100044250488,
                              grid_ts=1005.862, ev_ts=1003.265)
        self.assertEqual(aligned.current_p1, 9)
        self.assertNotEqual(aligned.mode, "unavailable")

    def test_accepted_cap_without_measured_response_starts_no_grace(self):
        r = self.pending()
        d = self.sample(r, 2, 29.8, 16.26, grid_ts=1001, ev_ts=1002)
        self.assertNotEqual(d.mode, "settling")
        self.assertIsNone(r._settle_started)
        self.assertIsNone(r._partial_reduction_current)

    def test_unaccepted_cap_cannot_start_partial_alignment(self):
        r = self.pending(accept=False)
        self.assertEqual(self.partial(r).current_p1, 0)
        self.assertIsNone(r._partial_reduction_current)

    def test_small_noise_is_not_physical_reduction_evidence(self):
        r = self.pending()
        d = self.partial(r, ev=16.0)
        self.assertNotEqual(d.mode, "settling")
        self.assertIsNone(r._partial_reduction_current)

    def test_missing_or_backward_timestamps_grant_no_partial_grace(self):
        for gt, et in ((None,1002),(1001,None),(1000,1002),(1001,999)):
            with self.subTest(grid_ts=gt, ev_ts=et):
                r = self.pending()
                d = self.sample(r, 2, 29.8, 9.711, grid_ts=gt, ev_ts=et)
                self.assertEqual(d.current_p1, 0)

    def test_no_ev_report_advance_cannot_claim_response(self):
        r = self.pending()
        d = self.sample(r, 2, 29.8, 9.711, grid_ts=1001, ev_ts=1000)
        self.assertEqual(d.current_p1, 0)

    def test_repeated_progress_never_moves_first_deadline(self):
        r = self.pending()
        for now, ev in ((2,14),(5,12),(11.999,9.711)):
            self.assertEqual(self.partial(r, now=now, ev=ev).current_p1, 9)
            self.assertEqual(r._settle_started, 2)
        expired = self.partial(r, now=12, ev=9.7)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))

    def test_same_value_receipts_and_ttl_renewals_refund_no_time(self):
        r = self.pending(resend_limit_every_seconds=5)
        self.partial(r)
        renewed = self.partial(r, now=7)
        self.assertTrue(renewed.should_call_service)
        self.assertEqual(r._settle_started, 2)
        self.assertEqual(self.partial(r, now=12).current_p1, 0)

    def test_delayed_initial_physical_response_gets_own_bounded_window(self):
        r = self.pending(enable_soft_guard=False)
        self.assertEqual(self.partial(r, now=60).current_p1, 9)
        self.assertEqual(r._settle_started, 60)
        self.assertEqual(self.partial(r, now=70).current_p1, 0)

    def test_separate_partial_episodes_share_spent_alignment_budget(self):
        r = self.pending()
        self.partial(r, ev=13)
        self.sample(r, 5, 26.54, 13, grid_ts=1005, ev_ts=1002)
        self.assertEqual(r._partial_reduction_used_seconds, 3)
        self.assertIsNone(r._settle_started)
        self.sample(r, 9, 26.54, 11, grid_ts=1005, ev_ts=1009)
        self.assertEqual(self.sample(r, 15.999, 26.54, 11,
                                     grid_ts=1005, ev_ts=1009).current_p1, 9)
        self.assertEqual(self.sample(r, 16, 26.54, 11,
                                     grid_ts=1005, ev_ts=1009).current_p1, 0)

    def test_repeated_same_value_cannot_reopen_coherent_partial_episode(self):
        r = self.pending()
        self.partial(r, ev=13)
        self.sample(r, 3, 26.54, 13, grid_ts=1003, ev_ts=1002)
        d = self.sample(r, 100, 26.54, 13, grid_ts=1003, ev_ts=1100)
        self.assertNotEqual(d.mode, "settling")
        self.assertIsNone(r._partial_reduction_response_ts)

    def test_full_response_after_partial_requires_grid_after_completion(self):
        r = self.pending()
        self.partial(r)
        self.sample(r, 3, 23.251, 9.711, grid_ts=1003, ev_ts=1002)
        full = self.sample(r, 4, 23.251, 9.4, grid_ts=1003, ev_ts=1004)
        self.assertEqual((full.mode, full.current_p1), ("settling", 9))
        self.assertEqual(r._awaiting_reduction_target, 9)
        self.sample(r, 5, 22.94, 9.4, grid_ts=1005, ev_ts=1004)
        self.assertIsNone(r._awaiting_reduction_target)

    def test_grid_before_partial_ev_still_requires_post_response_grid(self):
        r = self.pending()
        self.sample(r, 2, 23.25, 16.26, grid_ts=1002, ev_ts=1000)
        d = self.sample(r, 3, 23.25, 9.711, grid_ts=1002, ev_ts=1003)
        self.assertEqual((d.mode, d.current_p1), ("settling", 9))
        after = self.sample(r, 4, 23.25, 9.711, grid_ts=1004, ev_ts=1003)
        self.assertNotEqual(after.mode, "settling")

    def test_fresh_post_response_higher_house_load_is_evaluated_immediately(self):
        r = self.pending()
        self.partial(r)
        unsafe = self.sample(r, 3, 30, 9.711, grid_ts=1003, ev_ts=1002)
        self.assertEqual(unsafe.current_p1, 0)
        self.assertNotEqual(unsafe.mode, "settling")

    def test_grid_growth_before_response_cannot_be_hidden_by_old_anchor(self):
        r = self.pending()
        self.assertEqual(self.sample(r, 2, 31, 9.711,
                                     grid_ts=1001.5, ev_ts=1002).current_p1, 0)

    def test_reversed_ev_response_cannot_reuse_earlier_decrease(self):
        r = self.pending()
        self.partial(r)
        self.assertEqual(self.partial(r, now=3, ev=11).current_p1, 0)

    def test_partial_hold_cannot_raise_or_resume(self):
        r = self.pending(resume_for_seconds=0)
        r.paused_by_us = True
        d = self.partial(r)
        self.assertEqual((d.command, d.current_p1), ("set_limit", 9))
        self.assertTrue(r.paused_by_us)

    def test_invalid_stale_fault_and_negative_inputs_override_partial_hold(self):
        for extra in ({"data_valid":False,"data_error":"stale"},
                      {"status":"offline"}, {"data_error":"invalid timestamp"}):
            with self.subTest(extra=extra):
                r = self.pending()
                self.partial(r)
                self.assertEqual(self.partial(r, now=3, **extra).current_p1, 0)
        r = self.pending()
        self.assertEqual(self.sample(r, 2, -1, 9.711,
                                     grid_ts=1001, ev_ts=1002).current_p1, 0)

    def test_soft_and_pause_guards_are_not_skipped_during_partial_hold(self):
        for pause in (False,True):
            with self.subTest(pause=pause):
                r = self.pending()
                if pause:
                    r.settings.pause_for_seconds=0
                else:
                    r.settings.soft_for_seconds=0
                    r.settings.soft_current_amps=0
                d = self.partial(r)
                self.assertEqual(d.command, "pause" if pause else "set_limit")
                self.assertEqual(d.mode, "pause" if pause else "soft_limit")
                if not pause:self.assertEqual(d.current_p1,0)

    def test_settings_change_cannot_lengthen_pending_cap_budget(self):
        r = self.pending()
        self.partial(r)
        r.update_settings(replace(r.settings, measurement_settle_seconds=30))
        self.assertEqual(self.partial(r, now=12).current_p1,0)

    def test_hard_experimental_ceiling_still_precedes_partial_alignment(self):
        r = self.pending()
        r.update_settings(replace(r.settings, overload_enabled=True))
        unsafe = self.sample(r, 2, 39, 9.711, grid_ts=1002, ev_ts=1002)
        self.assertEqual((unsafe.mode, unsafe.current_p1), ("overload_stop",0))
        self.assertTrue(unsafe.overload_failure_latched)


if __name__ == "__main__":
    unittest.main()
