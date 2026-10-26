"""Synthetic accepted-offer/report-order checks for overload handoff.

Receipt order is not physical acquisition time. These offline tests exercise
the controller's bounded policy and do not model or contact electrical loads.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_overload_handoff_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)
EPOCH = 1_700_000_000.0


class OverloadHandoffTests(unittest.TestCase):
    def regulator(self, **options):
        settings = dict(active_control=True, dry_run=False, overload_enabled=True,
                        measurement_settle_seconds=25, enable_soft_guard=False,
                        enable_pause_resume=False)
        settings.update(options)
        return logic.OneFuseRegulator(logic.RegulatorSettings(**settings))

    def sample(self, regulator, now, grid, ev, *, grid_at=None, ev_at=None,
               accept=True, accepted_at=None, status="charging", **metadata):
        reports = dict(wall_now_ts=EPOCH + now,
                       grid_report_ts=EPOCH + (now if grid_at is None else grid_at),
                       charger_report_ts=EPOCH + (now if ev_at is None else ev_at))
        reports.update(metadata)
        decision = regulator.evaluate(logic.RegulatorInput(
            now_ts=now, status=status, grid_l1=grid, charger_current=ev, **reports))
        if accept:
            regulator.acknowledge(decision, now if accepted_at is None else accepted_at)
        return decision

    def higher_offer(self, **options):
        regulator = self.regulator(**options)
        offer = self.sample(regulator, 0, 18, 6)
        self.assertEqual((offer.current_p1, offer.should_call_service), (11, True))
        self.assertEqual(regulator.last_target[0], 11)
        return regulator

    def enter(self, regulator, *, accept=True, accepted_at=None):
        decision = self.sample(regulator, 5, 27, 6, ev_at=0,
                               accept=accept, accepted_at=accepted_at)
        self.assertEqual((decision.mode, decision.current_p1), ("overload_shedding", 6))
        self.assertEqual(decision.overload_transition_remaining_seconds, 10)
        return decision

    def existing_minimum(self, **options):
        regulator = self.higher_offer(**options)
        reduced = self.sample(regulator, 5, 23, 6, ev_at=0)
        self.assertEqual((reduced.mode, reduced.current_p1, reduced.should_call_service),
                         ("normal", 6, True))
        self.assertIsNone(regulator._overload_transition_started)
        self.assertFalse(regulator._overload_hold_active)
        self.assertEqual(regulator.last_set_limit_ts, 5)
        return regulator

    def assert_shedding(self, decision):
        self.assertEqual((decision.mode, decision.current_p1), ("overload_shedding", 6))
        self.assertFalse(decision.overload_failure_latched)
        self.assertNotEqual(decision.command, "resume")

    def assert_stop(self, decision, reason):
        self.assertEqual((decision.mode, decision.current_p1), ("overload_stop", 0))
        self.assertIn(reason, decision.reason)
        self.assertTrue(decision.overload_failure_latched)

    def test_low_ev_from_before_higher_offer_is_not_minimum_response(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        unchanged = self.sample(regulator, 6, 27, 6, ev_at=0)
        self.assert_shedding(unchanged)
        self.assertIsNone(regulator._overload_minimum_response_ts)
        self.assertFalse(regulator._overload_hold_active)

    def test_late_higher_ev_cannot_handoff_to_stale_normal_house_envelope(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        late_rise = self.sample(regulator, 5.2, 27, 11, grid_at=5)
        self.assert_shedding(late_rise)
        self.assertEqual(late_rise.house_load_l1, 21)
        minimum = self.sample(regulator, 7, 27, 6.1, grid_at=5)
        self.assert_shedding(minimum)
        confirmed = self.sample(regulator, 9, 22.1, 6.1, ev_at=7)
        self.assertEqual((confirmed.mode, confirmed.current_p1), ("normal", 7))
        self.assertFalse(confirmed.overload_failure_latched)
        self.assertIsNone(regulator._awaiting_reduction_target)
        self.assertIsNone(regulator._house_before_reduction)
        self.assertIsNone(regulator._overload_transition_started)

    def test_failed_minimum_submission_cannot_confirm_even_with_later_reports(self):
        regulator = self.higher_offer()
        self.enter(regulator, accept=False)
        for now in (6, 8, 14):
            self.assert_shedding(self.sample(regulator, now, 22, 6, accept=False))
        self.assertEqual(regulator.last_target[0], 11)
        self.assertIsNone(regulator._overload_minimum_accepted_at)
        self.assert_stop(self.sample(regulator, 15, 22, 6, accept=False), "response_timeout")

    def test_api_latency_uses_original_deadline_and_post_acceptance_ev_floor(self):
        regulator = self.higher_offer()
        self.enter(regulator, accepted_at=8)
        before_ack = self.sample(regulator, 9, 22, 6, ev_at=7)
        self.assert_shedding(before_ack)
        self.assertEqual(before_ack.overload_transition_remaining_seconds, 6)
        self.assertEqual(regulator._overload_minimum_ev_floor, EPOCH + 8)
        self.assertIsNone(regulator._overload_minimum_response_ts)
        first_minimum = self.sample(regulator, 10, 22, 6, grid_at=9)
        self.assert_shedding(first_minimum)
        confirmed = self.sample(regulator, 11, 22, 6, ev_at=10)
        self.assertGreaterEqual(confirmed.current_p1, 6)
        self.assertFalse(confirmed.overload_failure_latched)

    def test_success_after_deadline_does_not_create_more_response_time(self):
        regulator = self.higher_offer()
        self.enter(regulator, accepted_at=15.1)
        self.assert_stop(self.sample(regulator, 15.2, 22, 6), "response_timeout")

    def test_missing_or_invalid_acceptance_clock_cannot_use_pre_ack_ev(self):
        for wall in (None, float("nan"), True, float("inf"), float("-inf")):
            with self.subTest(wall=wall):
                regulator = self.higher_offer(resend_limit_every_seconds=1)
                entry = self.sample(regulator, 5, 27, 6, ev_at=0,
                                    wall_now_ts=wall, accepted_at=8)
                self.assert_shedding(entry)
                self.assertIsNone(regulator._overload_minimum_ev_floor)
                self.assert_shedding(self.sample(regulator, 9, 22, 6, ev_at=7))
                # A later valid TTL acknowledgement must not replace the
                # first acceptance floor or supply a new response window.
                self.assert_shedding(self.sample(regulator, 12, 22, 6, ev_at=10))
                self.assertIsNone(regulator._overload_minimum_ev_floor)
                self.assertEqual(regulator._overload_minimum_accepted_at, 8)
                self.assert_stop(self.sample(regulator, 15, 22, 6), "response_timeout")

    def test_identical_lease_acknowledgements_do_not_move_acceptance_floor(self):
        regulator = self.higher_offer(resend_limit_every_seconds=1)
        self.enter(regulator)
        for now in (6, 7, 9, 14):
            decision = self.sample(regulator, now, 27, 11)
            self.assert_shedding(decision)
            self.assertEqual(decision.overload_transition_remaining_seconds, 15 - now)
            self.assertEqual(regulator._overload_minimum_accepted_at, 5)
            self.assertEqual(regulator._overload_minimum_ev_floor, EPOCH + 5)
            self.assertEqual(regulator._reduction_guard_deadline, 15)
        self.assert_stop(self.sample(regulator, 15, 22, 6), "response_timeout")

    def test_identical_minimum_receipts_do_not_move_first_response_stamp(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 27, 6.1, grid_at=5))
        self.assert_shedding(self.sample(regulator, 8, 27, 6.1, grid_at=5))
        self.assertEqual(regulator._overload_minimum_response_ts, EPOCH + 7)
        confirmed = self.sample(regulator, 9, 22.1, 6.1, grid_at=8.5)
        self.assertGreaterEqual(confirmed.current_p1, 6)
        self.assertIsNone(regulator._overload_transition_started)

    def test_reversed_minimum_response_requires_new_low_and_later_grid(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 27, 6, grid_at=5))
        self.assert_shedding(self.sample(regulator, 8, 27, 11, grid_at=5))
        self.assertIsNone(regulator._overload_minimum_response_ts)
        self.assert_shedding(self.sample(regulator, 9, 22, 6, grid_at=8.5))
        self.assertEqual(regulator._overload_minimum_response_ts, EPOCH + 9)
        self.assertGreaterEqual(self.sample(regulator, 10, 22, 6, ev_at=9).current_p1, 6)

    def test_grid_before_or_equal_to_minimum_response_does_not_release(self):
        for grid_at in (6, 7):
            with self.subTest(grid_at=grid_at):
                regulator = self.higher_offer()
                self.enter(regulator)
                self.assert_shedding(self.sample(regulator, 7, 22, 6, grid_at=grid_at))
                final = self.sample(regulator, 8, 22, 6, ev_at=7)
                self.assertGreaterEqual(final.current_p1, 6)
                self.assertIsNone(regulator._overload_transition_started)

    def test_unchanged_entry_grid_cannot_release(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 27, 6.1, grid_at=5))
        self.assert_shedding(self.sample(regulator, 14, 27, 6.1, grid_at=5, ev_at=7))
        self.assert_stop(self.sample(regulator, 15, 22, 6), "response_timeout")

    def test_post_minimum_grid_above_ceiling_still_stops_immediately(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 27, 6, grid_at=5))
        self.assert_stop(self.sample(regulator, 8, 29.001, 6, ev_at=7),
                         "minimum_observed_above_ceiling")

    def test_high_grid_with_equal_minimum_stamp_is_still_stop_evidence(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_stop(self.sample(regulator, 7, 29.001, 6),
                         "minimum_observed_above_ceiling")

    def test_confirmed_29_amp_minimum_retains_original_exposure_budget(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 29, 6, grid_at=5))
        final = self.sample(regulator, 9, 29, 6, ev_at=7)
        self.assertEqual((final.mode, final.current_p1), ("overload_minimum", 6))
        self.assertEqual(final.overload_used_seconds, 4)
        held = self.sample(regulator, 109, 29, 6)
        self.assertEqual(held.overload_used_seconds, 104)

    def test_38_amp_hard_transition_ceiling_is_unchanged(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 38, 11))
        self.assert_stop(self.sample(regulator, 8, 38.001, 11), "transition_current_exceeded")

    def test_cumulative_exposure_exhaustion_overrides_pending(self):
        regulator = self.higher_offer()
        self.assertTrue(regulator.import_overload_state(dict(
            version=1, used_seconds=1799, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason=None)))
        self.enter(regulator)
        self.assert_stop(self.sample(regulator, 6, 27, 11), "cumulative_budget_exhausted")

    def test_invalid_sources_and_faults_override_pending(self):
        for metadata in (dict(data_valid=False, data_error="source stale"),
                         dict(grid_report_ts=None), dict(charger_report_ts=None),
                         dict(status="fault")):
            with self.subTest(metadata=metadata):
                regulator = self.higher_offer()
                self.enter(regulator)
                decision = self.sample(regulator, 7, 27, 6, **metadata)
                self.assertEqual(decision.current_p1, 0)
                self.assertTrue(decision.overload_failure_latched)

    def test_manual_pause_during_transition_is_not_resumed(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        stopped = self.sample(regulator, 7, 22, 0, status="paused")
        self.assert_stop(stopped, "charging_interrupted_or_paused")
        self.assertNotEqual(stopped.command, "resume")

    def test_leakage_does_not_confirm_minimum_response(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        self.assert_shedding(self.sample(regulator, 7, 20, .01))
        self.assertIsNone(regulator._overload_minimum_response_ts)
        self.assert_stop(self.sample(regulator, 15, 20, .01), "response_timeout")

    def test_safe_takeover_at_already_observed_minimum_keeps_existing_contract(self):
        regulator = self.regulator()
        first = self.sample(regulator, 0, 29, 6)
        self.assertEqual((first.mode, first.current_p1), ("overload_minimum", 6))
        self.assertIsNone(regulator._overload_transition_started)
        self.assertEqual(self.sample(regulator, 120, 29, 6).current_p1, 6)

    def test_pending_normal_alignment_gets_no_larger_post_handoff_window(self):
        regulator = self.higher_offer()
        self.enter(regulator, accepted_at=6)
        self.assertEqual(regulator._reduction_guard_deadline, 15)
        self.assertEqual(regulator._partial_reduction_budget, 10)
        self.assert_shedding(self.sample(regulator, 7, 27, 6.1, grid_at=5))
        self.sample(regulator, 9, 22.1, 6.1, ev_at=7, accept=False)
        # A proposed but unaccepted recovery must not erase the original
        # accepted-six history or grant a new normal alignment window.
        self.assertEqual(regulator.last_target[0], 6)
        self.assertEqual(regulator._reduction_guard_deadline, 15)
        late = self.sample(regulator, 16, 9, 11, accept=False)
        self.assertEqual(late.current_p1, 0)
        self.assertNotEqual(late.command, "resume")

    def test_persisted_timeout_allows_nominal_control_without_clearing_latch(self):
        regulator = self.regulator()
        state = dict(version=1, used_seconds=800.8, exposure_active=False,
                     transition_pending=False, minimum_held=False,
                     failure_reason="response_timeout")
        self.assertTrue(regulator.import_overload_state(state))
        normal = self.sample(regulator, 0, 18, 16)
        self.assertEqual(normal.current_p1, 16)
        self.assertTrue(normal.overload_failure_latched)
        regulator.update_settings(regulator.settings)
        self.assert_stop(self.sample(regulator, 5, 29, 6), "response_timeout")
        self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], 800.8)

    def test_settings_update_does_not_renew_pending_transition(self):
        regulator = self.higher_offer()
        self.enter(regulator)
        regulator.update_settings(regulator.settings)
        self.assert_shedding(self.sample(regulator, 14, 27, 11))
        self.assertEqual(regulator._overload_transition_started, 5)
        self.assert_stop(self.sample(regulator, 15, 22, 6), "response_timeout")

    def test_existing_identical_minimum_lease_gets_new_explicit_acceptance(self):
        for lease in (20, 60):
            with self.subTest(lease=lease):
                regulator = self.existing_minimum(normal_ttl_seconds=lease)
                entry = self.sample(regulator, 6, 30, 9)
                self.assert_shedding(entry)
                self.assertTrue(entry.should_call_service)
                self.assertEqual(regulator._overload_minimum_accepted_at, 6)
                self.assertEqual(regulator._overload_minimum_ev_floor, EPOCH + 6)
                self.assert_shedding(self.sample(regulator, 8, 30, 6, grid_at=6))
                confirmed = self.sample(regulator, 9, 27, 6, ev_at=8)
                self.assertEqual((confirmed.mode, confirmed.current_p1), ("overload_minimum", 6))
                self.assertFalse(confirmed.overload_failure_latched)

    def test_existing_minimum_failed_submission_retries_at_action_rate_only(self):
        regulator = self.existing_minimum()
        first = self.sample(regulator, 6, 30, 9, accept=False)
        self.assertTrue(first.should_call_service)
        self.assertTrue(regulator.begin_submission(first, 6))
        for now in (6.1, 7, 9, 10.999):
            retry = self.sample(regulator, now, 27, 6, accept=False)
            self.assert_shedding(retry)
            self.assertFalse(retry.should_call_service)
        due = self.sample(regulator, 11, 27, 6, accept=False)
        self.assertTrue(due.should_call_service)
        self.assertTrue(regulator.begin_submission(due, 11))
        self.assertIsNone(regulator._overload_minimum_accepted_at)
        self.assertEqual(due.overload_transition_remaining_seconds, 5)
        self.assert_stop(self.sample(regulator, 16, 27, 6, accept=False), "response_timeout")

    def test_retry_acceptance_requires_post_retry_ev_and_later_grid(self):
        regulator = self.existing_minimum()
        failed = self.sample(regulator, 6, 30, 9, accept=False)
        self.assertTrue(regulator.begin_submission(failed, 6))
        before_retry = self.sample(regulator, 8, 27, 6, accept=False)
        self.assertFalse(before_retry.should_call_service)
        accepted = self.sample(regulator, 11, 27, 6, ev_at=8, accept=False)
        self.assertTrue(accepted.should_call_service)
        self.assertTrue(regulator.begin_submission(accepted, 11))
        regulator.acknowledge(accepted, 11)
        self.assertEqual(accepted.overload_transition_remaining_seconds, 5)
        self.assertEqual(regulator._overload_minimum_accepted_at, 11)
        self.assert_shedding(self.sample(regulator, 12, 27, 6, ev_at=8))
        self.assert_shedding(self.sample(regulator, 13, 27, 6, grid_at=12))
        confirmed = self.sample(regulator, 14, 27, 6, ev_at=13)
        self.assertEqual((confirmed.mode, confirmed.current_p1), ("overload_minimum", 6))

    def test_active_off_and_dry_run_do_not_force_or_acknowledge_existing_minimum(self):
        for option in ("active_control", "dry_run"):
            with self.subTest(option=option):
                regulator = self.existing_minimum()
                setattr(regulator.settings, option, option == "dry_run")
                entry = self.sample(regulator, 6, 30, 9)
                self.assertEqual(entry.current_p1, 6)
                self.assertFalse(entry.should_call_service)
                self.assertIsNone(regulator._overload_minimum_attempted_at)
                self.assertIsNone(regulator._overload_minimum_accepted_at)
                self.assertEqual(regulator.last_set_limit_ts, 5)

    def test_first_successful_forced_ack_stops_force_even_if_clock_was_invalid(self):
        regulator = self.existing_minimum()
        entry = self.sample(regulator, 6, 30, 9, wall_now_ts=None)
        self.assertTrue(entry.should_call_service)
        self.assertEqual(regulator._overload_minimum_accepted_at, 6)
        self.assertIsNone(regulator._overload_minimum_ev_floor)
        for now in (7, 11, 15):
            pending = self.sample(regulator, now, 27, 6)
            self.assertFalse(pending.should_call_service)
            self.assertIsNone(regulator._overload_minimum_ev_floor)
            self.assertEqual(regulator._overload_minimum_accepted_at, 6)
        self.assert_stop(self.sample(regulator, 16, 27, 6), "response_timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
