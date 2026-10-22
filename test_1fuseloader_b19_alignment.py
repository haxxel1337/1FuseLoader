"""Synthetic source-bound low-grid alignment; no household history or API."""
from dataclasses import replace
from copy import deepcopy
import unittest

import test_1fuseloader_blocked_overload_normal as fixtures

logic = fixtures.logic
IDENTITY = ("charger", "grid-l1", "current", "status", "L1")


class LowGridAlignmentTests(unittest.TestCase):
    def regulator(self, **options):
        options.setdefault("nominal_import_only_fallback", True)
        options.setdefault("bounded_nominal_shedding", True)
        return fixtures.BlockedOverloadNormalTests.regulator(self, **options)

    def sample(self, r, now, grid, current, *, identity=IDENTITY, **kw):
        return fixtures.BlockedOverloadNormalTests.sample(
            self, r, now, grid, current, control_source_id=identity, **kw)

    def running(self, **options):
        r = self.regulator(**options)
        self.assertEqual(self.sample(r, 0, 21.7, 16.2).current_p1, 16)
        return r

    def pending(self, **options):
        r = self.running(**options)
        d = self.sample(r, 10, 2.4, 16.2, et=0)
        self.assertEqual((d.mode, d.current_p1, r.last_target[0]), ("low_grid_alignment", 7, 7))
        return r

    def test_live_sixteen_is_only_lowered_to_seven_without_ev_subtraction(self):
        r = self.running()
        d = self.sample(r, 10, 2.4, 16.2, et=0)
        self.assertEqual((d.current_p1, d.house_load_l1), (7, 2.4))
        self.assertEqual(d.house_load_basis, "verified_import_only_low_grid_bound")
        self.assertTrue(d.current_refresh_required)
        self.assertEqual(d.nominal_response_deadline_ts, 20)
        self.assertEqual(d.overload_remaining_seconds, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_ev_first_then_later_grid_does_not_induce_zero(self):
        r = self.pending()
        d = self.sample(r, 12, 2.4, 4.5, gt=10)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment", 7))
        d = self.sample(r, 15, 6.5, 4.5, et=12)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment_complete", 7))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertFalse(r.export_overload_state()["normal_control"].get("alignment_pending", False))

    def test_entry_ev_event_after_accepted_seven_pairs_with_later_grid(self):
        r = self.regulator(max_charge_amps=7)
        self.sample(r, 0, 9.5, 7.1)
        self.assertEqual(r.last_target[0], 7)
        d = self.sample(r, 10, 2.4, 4.5, et=10)
        self.assertEqual(d.mode, "low_grid_alignment")
        d = self.sample(r, 13, 6.5, 4.5, et=10, charger_snapshot_verified=True)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment_complete", 7))

    def test_already_seven_keeps_seven_during_asynchronous_start(self):
        r = self.pending()
        self.sample(r, 15, 2.4, .02, et=14, status="awaiting_start")
        d = self.sample(r, 17, 2.4, 4.5, gt=15)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment", 7))
        self.assertEqual(d.nominal_response_deadline_ts, 20)

    def test_new_idle_event_and_later_grid_confirm_without_resume(self):
        r = self.pending()
        d = self.sample(r, 13, 2.4, 0, et=12, status="awaiting_start")
        self.assertEqual((d.mode, d.current_p1, d.command), ("low_grid_alignment_complete", 7, "set_limit"))
        self.assertFalse(d.should_call_service)

    def test_completed_stable_state_does_not_timeout_from_event_age_alone(self):
        r = self.pending()
        self.sample(r, 15, 6.5, 4.5, et=12)
        for t in (25, 35, 45, 60, 90, 120):
            d = self.sample(r, t, 6.5, 4.5, et=12)
            self.assertGreater(d.current_p1, 0)
            self.assertNotEqual(d.mode, "low_grid_alignment")

    def test_real_conflict_after_completion_cannot_receive_a_new_deadline(self):
        r = self.pending()
        self.sample(r, 15, 6.5, 4.5, et=12)
        d = self.sample(r, 25, 2.4, 4.5, et=12)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_raw_hold", 7))
        self.assertEqual(r._low_grid_deadline, 20)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertLessEqual(d.grid_l1 + r._low_grid_previous, 23)

    def test_source_event_not_get_receipt_is_completion_evidence(self):
        r = self.pending()
        d = self.sample(r, 19, 2.4, .02, et=0, status="awaiting_start", charger_snapshot_verified=True)
        self.assertNotEqual(d.mode, "low_grid_alignment_complete")
        self.assertEqual(r._low_grid_previous, 16)
        d = self.sample(r, 20, 2.4, .02, et=0, status="awaiting_start", charger_snapshot_verified=True)
        self.assertEqual((d.current_p1, d.mode), (7, "low_grid_raw_hold"))
        self.assertEqual(r._low_grid_previous, 16)
        self.assertTrue(r.export_overload_state()["normal_control"]["alignment_pending"])

    def test_new_ev_requires_a_strictly_later_grid(self):
        r = self.pending()
        for gt in (12, 13):
            d = self.sample(r, 13, 6.5, 4.5, gt=gt)
            self.assertEqual(d.mode, "low_grid_alignment")
        d = self.sample(r, 14, 6.5, 4.5, et=13)
        self.assertEqual(d.mode, "low_grid_alignment_complete")

    def test_no_ack_does_not_complete_a_proposed_reduction(self):
        r = self.running()
        self.sample(r, 10, 2.4, 16.2, et=0, accept=False)
        d = self.sample(r, 12, 6.5, 4.5, et=11, accept=False)
        self.assertEqual((d.mode, r.last_target[0]), ("low_grid_alignment", 16))

    def test_sample_age_api_latency_and_ttl_do_not_renew_deadline(self):
        r = self.running()
        d = self.sample(r, 10, 2.4, 16.2, gt=7, et=0, accept=False)
        self.assertEqual(d.nominal_response_deadline_ts, 17)
        r.acknowledge(d, 13)
        d = self.sample(r, 16, 2.4, 16.2, et=0)
        self.assertEqual(d.nominal_response_deadline_ts, 17)
        r.acknowledge(replace(d, should_call_service=True), 16)
        held = self.sample(r, 17, 2.4, 16.2, et=0)
        self.assertEqual((held.mode, held.current_p1), ("low_grid_raw_hold", 7))
        self.assertEqual(r._low_grid_deadline, 17)
        self.assertIsNone(held.nominal_response_deadline_ts)
        self.assertLessEqual(held.grid_l1 + r._low_grid_previous, 23)

    def test_late_ack_cannot_reopen_terminal_zero(self):
        r = self.running()
        old = self.sample(r, 10, 2.4, 16.2, et=0, accept=False)
        self.sample(r, 20, 2.4, 16.2, et=0, accept=False)
        r.acknowledge(old, 21)
        d = self.sample(r, 22, 2.4, 4.5, et=21, accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertTrue(r._restart_required)

    def test_whole_offer_full_buffer_boundary_and_rising_raw(self):
        # Keep this a genuine numeric conflict at the full 16 + 7 A raw
        # boundary. A merely aged coherent event no longer starts a window.
        r = self.regulator(max_charge_amps=32)
        self.assertEqual(self.sample(r, 0, 20, 18).current_p1, 21)
        d = self.sample(r, 10, 16, 18, et=0)
        self.assertEqual(d.current_p1, 7)
        d = self.sample(r, 11, 16.0001, 18, et=0, accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(self.sample(r, 12, 2.4, 18, et=0, accept=False).current_p1, 0)

    def test_rising_raw_cannot_open_another_normal_or_experimental_window(self):
        r = self.pending()
        d = self.sample(r, 12, 26.1, 16.2, et=0, accept=False)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment_stop", 0))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertIsNone(d.overload_transition_remaining_seconds)

    def test_existing_partial_alignment_time_is_not_refunded(self):
        r = self.running()
        r._positive_cap, r._positive_skew_since = 16, 0
        r._positive_alignment_used_seconds = 14
        d = self.sample(r, 10, 2.4, 16.2, et=0)
        self.assertEqual(d.nominal_response_deadline_ts, 11)
        held = self.sample(r, 11, 2.4, 16.2, et=0)
        self.assertEqual((held.mode, held.current_p1), ("low_grid_raw_hold", 7))
        self.assertEqual(r._low_grid_deadline, 11)
        self.assertIsNone(held.nominal_response_deadline_ts)
        self.assertLessEqual(held.grid_l1 + r._low_grid_previous, 23)

    def test_existing_bounded_normal_response_is_not_restarted(self):
        r = self.running()
        d = self.sample(r, 10, 26.1, 16.2, et=0)
        self.assertEqual(d.mode, "normal_shedding")
        d = self.sample(r, 12, 2.4, 16.2, et=0)
        self.assertEqual(d.mode, "normal_shedding_complete")
        self.assertIsNone(r._low_grid_deadline)

    def test_no_new_experimental_window_when_optional_profile_is_disabled(self):
        r = self.pending(overload_enabled=False)
        d = self.sample(r, 12, 2.4, 4.5, gt=10)
        self.assertEqual(d.current_p1, 7)
        self.assertIsNone(d.overload_transition_remaining_seconds)

    def test_source_fault_trust_missing_clock_and_future_grid_stop(self):
        for kw in ({"data_valid": False}, {"charger_current_trusted": False},
                   {"grid_report_ts": None}, {"grid_report_ts": fixtures.EPOCH+15}):
            with self.subTest(kw=kw):
                r = self.pending()
                self.assertEqual(self.sample(r, 12, 2.4, 16.2, et=0, **kw).current_p1, 0)

    def test_invalid_candidate_cannot_fall_through_to_generic_sixteen_hold(self):
        for kw in ({"charger_current_trusted": False}, {"grid_report_ts": None},
                   {"grid_report_ts": fixtures.EPOCH+12}, {"charger_report_ts": fixtures.EPOCH+20}):
            with self.subTest(kw=kw):
                r = self.running()
                self.assertEqual(self.sample(r, 10, 2.4, 16.2, et=0, **kw).current_p1, 0)

    def test_data_fault_zero_cannot_reopen_legacy_reduction_after_failed_zero(self):
        r = self.pending()
        self.sample(r, 12, 2.4, 16.2, et=0, data_valid=False, accept=False)
        self.assertEqual(self.sample(r, 13, 2.4, 16.2, et=0, accept=False).current_p1, 0)

    def test_zero_or_invalid_window_does_not_create_positive_grace(self):
        for window in (0, float("nan"), float("inf")):
            r = self.running()
            r.update_settings(replace(r.settings, measurement_settle_seconds=window))
            self.assertEqual(self.sample(r, 10, 2.4, 16.2, et=0).current_p1, 0)

    def test_unknown_or_rebound_identity_never_enters_new_exception(self):
        for identity in (None, "", (), ("other",), ["not-stable"]):
            r = self.running()
            d = self.sample(r, 10, 2.4, 16.2, et=0, identity=identity)
            self.assertNotEqual(d.mode, "low_grid_alignment")

    def test_active_identity_change_is_terminal(self):
        r = self.pending()
        d = self.sample(r, 12, 2.4, 16.2, et=0, identity=("other",), accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(self.sample(r, 13, 2.4, 16.2, et=0, accept=False).current_p1, 0)

    def test_option_off_preserves_old_protective_behavior(self):
        r = self.running(nominal_import_only_fallback=False)
        d = self.sample(r, 10, 2.4, 16.2, et=0)
        legacy = self.running(nominal_import_only_fallback=False)
        old = self.sample(legacy, 10, 2.4, 16.2, et=0, identity=None)
        self.assertEqual((d.mode, d.current_p1), (old.mode, old.current_p1))
        self.assertNotEqual(d.mode, "low_grid_alignment")

    def test_manual_pause_schedule_and_auth_cannot_use_new_path(self):
        for status in ("paused", "waiting_for_schedule", "awaiting_authorization", "paused_due_to_equalizer"):
            r = self.pending()
            d = self.sample(r, 12, 2.4, 16.2, et=0, status=status)
            self.assertEqual(d.current_p1, 0)
            self.assertNotEqual(d.command, "resume")

    def test_soft_and_pause_guards_override_low_grid_hold(self):
        for options in (dict(enable_soft_guard=True, soft_limit_amps=2, soft_for_seconds=0, soft_current_amps=0),
                        dict(enable_pause_resume=True, pause_limit_amps=2, pause_for_seconds=0)):
            r = self.running()
            r.update_settings(replace(r.settings, **options))
            d = self.sample(r, 10, 2.4, 16.2, et=0)
            self.assertEqual(d.current_p1, 0)

    def test_no_expired_cap_or_unaccepted_higher_offer_can_be_used(self):
        r = self.running()
        d = self.sample(r, 60, 2.4, 16.2, et=0)
        self.assertEqual(d.current_p1, 0)
        r = self.pending()
        self.sample(r, 12, 2.4, 30, et=11)
        self.assertEqual(r.last_target[0], 0)

    def test_restore_pending_consumes_failure_not_a_new_window(self):
        r = self.pending()
        state = deepcopy(r.export_overload_state())
        self.assertTrue(state["normal_control"]["alignment_pending"])
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        d = self.sample(restored, 30, 2.4, 0, status="awaiting_start")
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(d.measurement_uncertainty_stops, 1)
        self.assertGreater(d.measurement_recovery_wait_seconds, 0)
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")

    def test_settings_and_reset_cannot_refund_active_window(self):
        for operation in (lambda r: r.update_settings(replace(r.settings, measurement_settle_seconds=30)), lambda r: r.reset()):
            r = self.pending()
            operation(r)
            d = self.sample(r, 12, 2.4, 16.2, et=0)
            self.assertEqual(d.current_p1, 0)


if __name__ == "__main__":
    unittest.main()
