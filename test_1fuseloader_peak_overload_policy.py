"""Peak admission is separate from ordinary explicit-period current control.

Synthetic validated source inputs only; no HA or electrical hardware access.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_trial_epochs as fixtures


class PeakOverloadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ExplicitTrialTests()

    def active(self, **options):
        settings = dict(grid_confirmation_seconds=30, peak_only_response=True)
        settings.update(options)
        regulator, inp = self.helper.ready(**settings)
        self.helper.activate(regulator, inp)
        return regulator

    def step(self, regulator, now, grid=27, ev=16.1, *, accept=True, **metadata):
        inp = self.helper.frame(now, grid, ev, **metadata)
        # Mirrors the validated-input observation hook. Re-observing the same
        # source sample in evaluate must be idempotent and cannot refund time.
        regulator.observe_peak_response(inp)
        result = regulator.evaluate(inp)
        if accept and result.should_call_service:
            regulator.acknowledge(result, now)
        return result

    def stable_minimum(self):
        regulator = self.active()
        first = self.step(regulator, 3)
        self.assertEqual(first.current_p1, 7)
        response = self.step(regulator, 5, 27, 7.1, gt=3)
        self.assertEqual(response.current_p1, 7)
        confirmed = self.step(regulator, 6, 26.5, 7.1, et=5)
        self.assertEqual((confirmed.mode, confirmed.current_p1), ("overload_minimum", 7))
        self.assertIsNone(regulator._overload_transition_started)
        return regulator

    def test_ordinary_26_to_28_has_no_invented_ten_second_admission(self):
        for grid in (26, 27, 28):
            with self.subTest(grid=grid):
                regulator = self.active()
                first = self.step(regulator, 3, grid)
                self.assertEqual(first.current_p1, 7)
                self.assertIsNone(regulator.peak_response_deadline())
                later = self.step(regulator, 15, grid, 16.1, et=14)
                self.assertEqual(later.current_p1, 7)
                self.assertIsNone(regulator.peak_response_deadline())
                self.assertFalse(later.overload_failure_latched)
                self.assertEqual(self.helper.epoch(regulator)["state"], "active")

    def test_default_off_preserves_legacy_ordinary_response_deadline(self):
        regulator, inp = self.helper.ready(grid_confirmation_seconds=30)
        self.assertFalse(regulator.settings.peak_only_response)
        self.helper.activate(regulator, inp)
        first = self.step(regulator, 3, 27, 16.1)
        self.assertEqual(first.current_p1, 7)
        self.assertEqual(self.step(regulator, 12.9, 27, 16.1).current_p1, 7)
        stopped = self.step(regulator, 13, 27, 16.1, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIn("response_timeout", stopped.reason)

    def test_late_ordinary_minimum_can_start_fixed_grid_wait_without_peak_timer(self):
        regulator = self.active()
        self.step(regulator, 3)
        self.step(regulator, 14, 27, 16.1)
        waiting = self.step(regulator, 16.2, 27, 7.1, gt=14, et=16, accept=False)
        self.assertEqual((waiting.mode, waiting.current_p1, waiting.command),
                         ("overload_grid_wait", 7, None))
        self.assertAlmostEqual(waiting.grid_confirmation_deadline_ts, 46, places=6)
        self.assertIsNone(regulator.peak_response_deadline())
        later = self.step(regulator, 30, 27, 7.1, gt=14, et=16, accept=False)
        self.assertEqual((later.mode, later.current_p1, later.command), ("overload_grid_wait", 7, None))
        self.assertEqual(later.grid_confirmation_deadline_ts, waiting.grid_confirmation_deadline_ts)
        completed = self.step(regulator, 31, 25.1, 7.1, et=16, accept=False)
        self.assertEqual((completed.mode, completed.current_p1), ("overload_minimum", 7))
        self.assertIsNone(regulator._minimum_confirmation)

    def test_true_peak_owns_ten_seconds_and_unanswered_peak_stops_at_expiry(self):
        regulator = self.active()
        started = self.step(regulator, 3, 28.001)
        self.assertEqual(started.current_p1, 7)
        self.assertAlmostEqual(regulator.peak_response_deadline(), 13, places=6)
        self.assertEqual(self.step(regulator, 12.9, 28.001).current_p1, 7)
        stopped = self.step(regulator, 13, 28.001, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNotNone(regulator.effective_overload_failure_reason)

    def test_unspent_legacy_seven_minimum_also_reduces_peak_even_if_projection_fits(self):
        regulator = fixtures.logic.OneFuseRegulator(fixtures.logic.RegulatorSettings(
            active_control=True, dry_run=False, overload_enabled=True, min_charge_amps=7,
            peak_only_response=True,
            enable_soft_guard=False, enable_pause_resume=False, measurement_settle_seconds=25))
        self.step(regulator, 0, 20, 16.1)
        peak = self.step(regulator, 3, 28.001, 16.1)
        self.assertEqual(peak.current_p1, 7)
        self.assertIsNone(regulator._trial_epoch)
        self.assertAlmostEqual(regulator.peak_response_deadline(), 13, places=6)

    def test_peak_sample_age_and_repeated_high_frames_do_not_refund_owner(self):
        regulator = self.active()
        first = self.step(regulator, 9, 31, 16.1, gt=5)
        self.assertEqual(first.current_p1, 7)
        deadline = regulator.peak_response_deadline()
        self.assertAlmostEqual(deadline, 15, places=6)
        self.step(regulator, 11, 31, 16.1, gt=10)
        self.assertEqual(regulator.peak_response_deadline(), deadline)
        self.assertEqual(self.step(regulator, deadline, 31, accept=False).current_p1, 0)

    def test_dip_below_28_does_not_refund_an_unresolved_peak_on_recrossing(self):
        regulator = self.active()
        self.step(regulator, 3, 31)
        deadline = regulator.peak_response_deadline()
        self.assertEqual(self.step(regulator, 14, 27, 16.1, et=13).current_p1, 7)
        self.assertEqual(regulator.peak_response_deadline(), deadline)
        stopped = self.step(regulator, 15, 31, 16.1, et=14, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual(regulator.peak_response_deadline(), deadline)

    def test_timely_peak_response_can_wait_for_grid_past_peak_admission(self):
        regulator = self.active()
        self.step(regulator, 9, 31, 16.1, gt=5)
        response = self.step(regulator, 12, 31, 7.1, gt=5, accept=False)
        self.assertEqual((response.mode, response.current_p1), ("overload_grid_wait", 7))
        self.assertAlmostEqual(response.grid_confirmation_deadline_ts, 42, places=6)
        waiting = self.step(regulator, 16, 31, 7.1, gt=5, et=12, accept=False)
        self.assertEqual((waiting.mode, waiting.current_p1, waiting.command), ("overload_grid_wait", 7, None))
        completed = self.step(regulator, 18, 28.5, 7.1, et=12, accept=False)
        self.assertEqual((completed.mode, completed.current_p1), ("overload_minimum", 7))
        self.assertFalse(regulator.peak_response_active)

    def test_already_proven_seven_peak_holds_only_original_clock_without_fresh_ack(self):
        regulator = self.stable_minimum()
        accepted_at = regulator.last_set_limit_ts
        peak = self.step(regulator, 8, 31, 7.1, et=5, accept=False)
        self.assertEqual((peak.mode, peak.current_p1, peak.command), ("overload_peak_hold", 7, None))
        self.assertFalse(peak.should_call_service)
        self.assertIsNone(regulator._minimum_confirmation)
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        deadline = regulator.peak_response_deadline()
        self.assertAlmostEqual(deadline, 18, places=6)
        settled = self.step(regulator, 11, 28.5, 7.1, et=5, accept=False)
        self.assertEqual(settled.current_p1, 7)
        self.assertFalse(regulator.peak_response_active)
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)

    def test_already_minimum_peak_that_does_not_settle_stops(self):
        regulator = self.stable_minimum()
        self.step(regulator, 8, 31, 7.1, et=5, accept=False)
        deadline = regulator.peak_response_deadline()
        stopped = self.step(regulator, deadline, 31, 7.1, et=5, accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_existing_lease_expiry_outranks_the_later_peak_deadline(self):
        regulator = self.stable_minimum()
        self.step(regulator, 59, 31, 7.1, et=58, accept=False)
        self.assertAlmostEqual(regulator.peak_response_deadline(), 69, places=6)
        stopped = self.step(regulator, 63, 31, 7.1, et=62, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertFalse(regulator.trial_diagnostics()["overload_trial_activation_committed"])

    def test_cached_minimum_without_post_ack_credit_never_opens_thirty_seconds(self):
        regulator = self.active()
        self.step(regulator, 3, 27, 7.1, et=2)
        pending = self.step(regulator, 5, 31, 7.1, et=2, accept=False)
        self.assertEqual(pending.current_p1, 7)
        self.assertIsNone(regulator._minimum_confirmation)
        stopped = self.step(regulator, regulator.peak_response_deadline(), 31, 7.1, et=2, accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_new_above_trial_ceiling_after_real_minimum_remains_an_immediate_guard(self):
        regulator = self.active()
        self.step(regulator, 3, 31)
        self.step(regulator, 5, 31, 7.1, gt=3)
        stopped = self.step(regulator, 6, 30, 7.1, et=5, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIn("minimum_observed_above_ceiling", stopped.reason)

    def test_above_38_source_fault_and_manual_pause_override_period(self):
        for grid, metadata in (
            (38.001, {}), (27, {"data_valid": False, "data_error": "source_fault"}),
            (27, {"status": "paused"}),
        ):
            with self.subTest(grid=grid, metadata=metadata):
                regulator = self.active()
                stopped = self.step(regulator, 3, grid, accept=False, **metadata)
                self.assertEqual(stopped.current_p1, 0)
                self.assertFalse(regulator.trial_diagnostics()["overload_trial_activation_committed"])

    def test_cumulative_budget_still_stops_ordinary_27_without_any_peak(self):
        regulator = self.stable_minimum()
        for now in range(33, 1803, 30):
            self.assertEqual(self.step(regulator, now, 27, 7.1, et=now-1).current_p1, 7)
        stopped = self.step(regulator, 1803, 27, 7.1, et=1802, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(regulator.peak_response_deadline())
        self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], fixtures.LIFETIME+1800)
        self.assertEqual(regulator.export_overload_state()["failure_reason"], "response_timeout")

    def test_no_epoch_spent_history_does_not_gain_a_new_29_amp_allowance(self):
        regulator, _ = self.helper.ready(grid_confirmation_seconds=30)
        before = regulator.export_overload_state()["used_seconds"]
        first = self.step(regulator, 3, 27, 7.1, et=2)
        self.assertNotIn(first.current_p1, (13, 16))
        stopped = self.step(regulator, 6, 27, 7.1, et=5, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(regulator._trial_epoch)
        self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], before)
        self.assertEqual(regulator.export_overload_state()["failure_reason"], "response_timeout")


if __name__ == "__main__":
    unittest.main()
