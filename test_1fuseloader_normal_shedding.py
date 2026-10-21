"""Synthetic bounded normal decreases, separate from an overload allowance."""
from dataclasses import replace
from copy import deepcopy
import unittest

import test_1fuseloader_blocked_overload_normal as fixtures

logic = fixtures.logic


class NormalSheddingTests(unittest.TestCase):
    sample = fixtures.BlockedOverloadNormalTests.sample
    running_sixteen = fixtures.BlockedOverloadNormalTests.running_sixteen

    def regulator(self, **options):
        options.setdefault("bounded_nominal_shedding", True)
        return fixtures.BlockedOverloadNormalTests.regulator(self, **options)

    def pending(self, *, accept=True, **options):
        r = self.running_sixteen(**options)
        d = self.sample(r, 40, 26.1, 16.2, et=-10, accept=accept)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
        self.assertEqual(d.nominal_response_deadline_ts, 40+options.get("overload_response_seconds", 10))
        return r

    def test_entry_is_only_a_lower_offer_with_no_ev_credit(self):
        r = self.pending()
        d = self.sample(r, 41, 26.1, 16.2, et=-10)
        self.assertEqual((d.current_p1, d.house_load_l1), (7, 26.1))
        self.assertEqual(d.nominal_response_remaining_seconds, 9)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")
        self.assertGreaterEqual(r.export_overload_state()["used_seconds"], 2001)

    def test_genuinely_old_state_may_enter_but_does_not_confirm_response(self):
        r = self.running_sixteen()
        d = self.sample(r, 40, 26.6, 16, et=-700)
        self.assertEqual(d.current_p1, 7)
        d = self.sample(r, 49, 26.6, 16, et=-700)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
        d = self.sample(r, 50, 26.6, 16, et=-700)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_stop", 0))

    def test_initial_grid_age_and_api_latency_spend_original_time(self):
        r = self.running_sixteen()
        d = self.sample(r, 40, 26.1, 16.2, gt=36, et=-10, accept=False)
        self.assertEqual((d.nominal_response_deadline_ts, d.nominal_response_remaining_seconds), (46, 6))
        r.acknowledge(d, 43)
        self.assertEqual(r._normal_shed_deadline, 46)
        self.assertEqual(r._normal_shed_ev_floor, fixtures.EPOCH+43)
        d = self.sample(r, 46, 17, 7, et=45)
        self.assertEqual(d.current_p1, 0)

    def test_fresh_post_ack_ev_then_later_grid_complete_nominally(self):
        r = self.pending()
        self.assertEqual(self.sample(r, 43, 26.1, 7, gt=40).mode, "normal_shedding")
        d = self.sample(r, 45, 17.8, 7, et=43)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_complete", 7))
        self.assertLessEqual(d.house_load_l1+7, 23)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_newer_ev_requires_grid_after_that_same_evidence(self):
        r = self.pending()
        self.sample(r, 43, 26.1, 7, gt=40)
        d = self.sample(r, 45, 17, 7, gt=44, et=45)
        self.assertEqual(d.mode, "normal_shedding")
        d = self.sample(r, 46, 17, 7, gt=46, et=45)
        self.assertEqual(d.mode, "normal_shedding_complete")

    def test_pre_ack_and_equal_ack_events_never_confirm_response(self):
        r = self.pending(accept=False)
        d = self.sample(r, 42, 26.1, 16.2, et=-10, accept=False)
        r.acknowledge(d, 43)
        for et in (42, 43):
            d = self.sample(r, 44, 17.8, 7, et=et)
            self.assertEqual(d.mode, "normal_shedding")

    def test_no_ack_is_not_a_physical_response_or_completion(self):
        r = self.pending(accept=False)
        d = self.sample(r, 45, 17, 7, et=44, accept=False)
        self.assertEqual(d.mode, "normal_shedding")
        d = self.sample(r, 50, 17, 7, et=49, accept=False)
        self.assertEqual(d.current_p1, 0)

    def test_late_api_ack_does_not_create_new_window(self):
        r = self.running_sixteen()
        d = self.sample(r, 40, 26.1, 16.2, et=-10, accept=False)
        r.acknowledge(d, 51)
        d = self.sample(r, 52, 17, 7, et=52)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(r._normal_shed_deadline, 50)

    def test_old_ack_after_failed_zero_cannot_become_a_new_restart(self):
        r = self.running_sixteen()
        proposal = self.sample(r, 40, 26.1, 16.2, et=-10, accept=False)
        self.assertEqual(self.sample(r, 50, 26.1, 16.2, et=-10, accept=False).current_p1, 0)
        r.acknowledge(proposal, 51)
        self.assertTrue(r._restart_required)
        self.assertEqual(r._normal_shed_deadline, 50)
        self.assertEqual(r._uncertainty_stops, 1)
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(self.sample(r, 52, 17, 7).current_p1, 0)

    def test_raw_drop_alone_needs_separate_verified_contract(self):
        r = self.pending()
        d = self.sample(r, 43, 15.9, 16.2, et=-10)
        self.assertEqual(d.mode, "normal_shedding")
        r = self.pending(nominal_import_only_fallback=True)
        d = self.sample(r, 43, 15.9, 16.2, et=-10)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_complete", 7))
        self.assertEqual(d.house_load_l1, 15.9)

    def test_sustained_overload_cannot_become_another_thirty_minutes(self):
        r = self.pending()
        self.sample(r, 43, 28, 7, et=42)
        d = self.sample(r, 45, 28, 7, et=43)
        self.assertEqual(d.mode, "normal_shedding")
        d = self.sample(r, 50, 28, 7, et=43)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_fresh_low_ev_cannot_enter_a_new_positive_attempt(self):
        r = self.running_sixteen()
        d = self.sample(r, 40, 24.2, .16)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._normal_shed_deadline)
        self.assertEqual(d.measurement_uncertainty_stops, 1)

    def test_feature_and_experimental_options_must_both_be_on(self):
        r = self.running_sixteen(bounded_nominal_shedding=False)
        d = self.sample(r, 40, 26.1, 16.2, et=-10)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._normal_shed_deadline)
        r = self.running_sixteen(overload_enabled=False)
        self.sample(r, 40, 26.1, 16.2, et=-10)
        self.assertIsNone(r._normal_shed_deadline)

    def test_fresh_nominal_current_is_not_required_but_health_and_clock_are(self):
        for metadata in ({"data_valid": False}, {"charger_current_trusted": False},
                         {"grid_report_ts": None}, {"wall_now_ts": None}, {"gt": 41},
                         {"charger_report_ts": None}, {"et": 43}):
            with self.subTest(metadata=metadata):
                r = self.running_sixteen()
                d = self.sample(r, 40, 26.1, 16.2, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(r._normal_shed_deadline)

    def test_expired_old_lease_never_creates_new_authority(self):
        r = self.running_sixteen()
        d = self.sample(r, 61, 26.1, 16.2, et=0)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._normal_shed_deadline)

    def test_pending_fault_manual_pause_or_untrusted_source_is_terminal(self):
        for metadata in ({"data_valid": False}, {"charger_current_trusted": False},
                         {"status": "paused"}, {"status": "awaiting_scheduled_start"}):
            with self.subTest(metadata=metadata):
                r = self.pending()
                d = self.sample(r, 42, 26.1, 16.2, et=-10, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNotNone(r._normal_shed_failure)
                d = self.sample(r, 43, 26.1, 16.2, et=-10, accept=False)
                self.assertEqual(d.current_p1, 0)

    def test_hard_peak_is_strictly_above_configured_ceiling(self):
        r = self.pending()
        self.assertEqual(self.sample(r, 42, 38, 16.2, et=-10).current_p1, 7)
        self.assertEqual(self.sample(r, 43, 38.0001, 16.2, et=-10).current_p1, 0)

    def test_failed_proposal_retries_only_after_action_interval(self):
        r = self.pending(accept=False)
        self.assertFalse(self.sample(r, 41, 26.1, 16.2, et=-10, accept=False).should_call_service)
        self.assertTrue(self.sample(r, 45, 26.1, 16.2, et=-10, accept=False).should_call_service)
        self.assertEqual(self.sample(r, 50, 26.1, 16.2, et=-10).current_p1, 0)

    def test_soft_pause_and_bound_changes_override_response(self):
        for settings in ({"enable_soft_guard": True, "soft_limit_amps": 9, "soft_for_seconds": 0},
                         {"enable_pause_resume": True, "pause_limit_amps": 9, "pause_for_seconds": 0},
                         {"max_charge_amps": 6}, {"bounded_nominal_shedding": False},
                         {"overload_enabled": False}):
            with self.subTest(settings=settings):
                r = self.pending()
                r.update_settings(replace(r.settings, **settings))
                d = self.sample(r, 42, 26.1, 16.2, et=-10)
                self.assertEqual(d.current_p1, 0)
                self.assertEqual(r._normal_shed_deadline, 50)

    def test_option_changes_and_partial_reports_cannot_refund_time(self):
        r = self.pending(overload_response_seconds=5)
        self.assertEqual(r._normal_shed_deadline, 45)
        r.update_settings(replace(r.settings, overload_response_seconds=10))
        self.sample(r, 43, 23, 11, et=42)
        self.assertEqual(self.sample(r, 45, 17, 7, et=44).current_p1, 0)

    def test_shorter_config_applies_without_moving_origin(self):
        r = self.pending()
        r.update_settings(replace(r.settings, overload_response_seconds=1))
        self.assertEqual(self.sample(r, 42, 17, 7, et=41).current_p1, 0)
        self.assertEqual(r._normal_shed_deadline, 41)

    def test_pending_restore_is_interrupted_never_another_ten_seconds(self):
        r = self.pending()
        state = r.export_overload_state()
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=100))
        d = self.sample(restored, 1000, 26.1, 16.2, et=0)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(d.measurement_uncertainty_stops, 1)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertGreaterEqual(restored.export_overload_state()["used_seconds"], state["used_seconds"])
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")

    def test_routine_reset_and_setting_toggle_do_not_grant_new_attempt(self):
        r = self.pending()
        r.update_settings(replace(r.settings, bounded_nominal_shedding=False))
        r.reset()
        r.update_settings(replace(r.settings, bounded_nominal_shedding=True))
        d = self.sample(r, 42, 26.1, 16.2, et=-10)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(d.measurement_uncertainty_stops, 1)

    def test_bad_additive_persistence_never_refunds_valid_overload_state(self):
        for change in ({"stops": -1}, {"stops": True}, {"response_pending": "yes"},
                       {"retry_wait_seconds": float("nan")}, {"cap": None}):
            with self.subTest(change=change):
                r = self.pending()
                state = deepcopy(r.export_overload_state())
                state["normal_control"].update(change)
                restored = self.regulator()
                self.assertFalse(restored.import_overload_state(state))
                self.assertEqual(restored.export_overload_state()["used_seconds"], state["used_seconds"])
                self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")
                self.assertEqual(restored._uncertainty_stops, 3)

    def test_legacy_persistence_is_accepted_without_normal_state(self):
        r = self.running_sixteen()
        state = r.export_overload_state()
        del state["normal_control"]
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")

    def test_supported_thirtytwo_amp_previous_offer_roundtrips(self):
        options = dict(max_charge_amps=32, main_fuse_amps=32, buffer_amps=0)
        r = self.regulator(**options)
        d = self.sample(r, 0, 32, 32)
        self.assertEqual(d.current_p1, 32)
        self.sample(r, 40, 35, 32, et=-10)
        state = r.export_overload_state()
        self.assertEqual(state["normal_control"]["previous_offer"], 32)
        restored = self.regulator(**options)
        self.assertTrue(restored.import_overload_state(state))

    def test_reserved_retry_survives_crash_without_replaying_attempt_forever(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        proposal = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertEqual((proposal.current_p1, proposal.should_call_service), (7, True))
        state = r.export_overload_state()
        self.assertTrue(state["normal_control"]["retry_submission_pending"])
        self.assertEqual(state["normal_control"]["stops"], 1)
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        self.assertEqual(restored._uncertainty_stops, 2)
        self.sample(restored, 1000, 3, .023, status="awaiting_start")
        proposal = self.sample(restored, 1120, 3, .023, status="awaiting_start", accept=False)
        self.assertEqual((proposal.current_p1, proposal.should_call_service), (7, True))
        state2 = restored.export_overload_state()
        again = self.regulator()
        self.assertTrue(again.import_overload_state(state2))
        self.assertEqual(again._uncertainty_stops, 3)
        for now in (2000, 2300, 3000):
            d = self.sample(again, now, 3, .023, status="awaiting_start")
            self.assertEqual(d.current_p1, 0)

    def test_acknowledged_retry_clears_only_its_submission_reservation(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        d = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertTrue(r.export_overload_state()["normal_control"]["retry_submission_pending"])
        r.acknowledge(d, 112)
        self.assertFalse(r.export_overload_state()["normal_control"]["retry_submission_pending"])
        self.assertEqual(r._uncertainty_stops, 1)

    def test_no_ack_without_outcome_callback_cannot_submit_another_retry(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        first = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertTrue(first.should_call_service)
        for now in (112, 116, 121, 172, 232, 600):
            d = self.sample(r, now, 3, .023, status="awaiting_start", accept=False)
            self.assertFalse(d.should_call_service)
            self.assertEqual(d.mode, "waiting_for_command_result")

    def test_rejected_retry_backoff_then_second_rejection_needs_attention(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        first = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertTrue(r.reject_submission(first, 112))
        self.assertFalse(r.reject_submission(first, 113))
        self.assertEqual(r._uncertainty_stops, 2)
        for now in (113, 173, 231):
            self.assertEqual(self.sample(r, now, 3, .023, status="awaiting_start").current_p1, 0)
        second = self.sample(r, 232, 3, .023, status="awaiting_start", accept=False)
        self.assertEqual((second.current_p1, second.should_call_service), (7, True))
        self.assertTrue(r.reject_submission(second, 233))
        for now in (234, 400, 1000):
            d = self.sample(r, now, 3, .023, status="awaiting_start")
            self.assertEqual((d.mode, d.current_p1), ("measurement_recovery_blocked", 0))
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_terminal_deadline_is_not_inherited_by_a_qualified_retry(self):
        r = self.pending()
        stopped = self.sample(r, 50, 26.1, 16.2, et=-10)
        self.assertIsNone(stopped.nominal_response_deadline_ts)
        self.assertIsNone(stopped.nominal_response_remaining_seconds)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        retry = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertTrue(retry.should_call_service)
        self.assertIsNone(retry.nominal_response_deadline_ts)
        self.assertEqual(retry.recovery_submission_ts, 111)

    def test_obsolete_rejection_or_ack_cannot_retire_the_next_retry(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        first = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        self.assertTrue(r.reject_submission(first, 112))
        self.sample(r, 113, 3, .023, status="awaiting_start")
        second = self.sample(r, 232, 3, .023, status="awaiting_start", accept=False)
        self.assertEqual(second.recovery_submission_ts, 232)
        self.assertFalse(r.reject_submission(first, 233))
        self.assertTrue(r._uncertainty_retry_submission_pending)
        self.assertEqual(r._uncertainty_stops, 2)
        r.acknowledge(first, 234)
        self.assertTrue(r._uncertainty_retry_submission_pending)
        self.assertTrue(r._restart_required)
        self.assertEqual(r._uncertainty_stops, 2)
        r.acknowledge(second, 235)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.assertFalse(r._restart_required)

    def test_protective_replacement_ack_is_not_an_obsolete_positive_reply(self):
        r = self.pending()
        self.sample(r, 50, 26.1, 16.2, et=-10)
        self.sample(r, 51, 3, .023, status="awaiting_start")
        retry = self.sample(r, 111, 3, .023, status="awaiting_start", accept=False)
        r.reject_submission(retry, 112)
        zero = replace(retry, mode="unavailable", current_p1=0)
        r.acknowledge(zero, 113)
        self.assertEqual(r._normal_shed_failure, "positive_retry_submission_failed")
        pause = replace(retry, mode="pause", command="pause", current_p1=0)
        r.acknowledge(pause, 114)
        self.assertTrue(r.paused_by_us)


if __name__ == "__main__":
    unittest.main()
