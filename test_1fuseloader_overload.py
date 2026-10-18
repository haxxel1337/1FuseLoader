"""Opt-in overload envelope, response deadline and persisted exposure tests.

Synthetic electrical inputs verify control policy, not installation ampacity or
fuse temperature. No HA instance, Easee service or vehicle is contacted.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_overload_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class OverloadTests(unittest.TestCase):
    def regulator(self, **options):
        settings = {"active_control": True, "dry_run": False, "overload_enabled": True}
        settings.update(options)
        return logic.OneFuseRegulator(logic.RegulatorSettings(**settings))

    def sample(self, regulator, now, grid=28, ev=6, *, accept=True, status="charging", **metadata):
        reports = {"grid_report_ts": 1000 + now, "charger_report_ts": 1000 + now,
                   "wall_now_ts": 1000 + now}
        reports.update(metadata)
        decision = regulator.evaluate(logic.RegulatorInput(
            now_ts=now, status=status, grid_l1=grid, charger_current=ev, **reports))
        self.assertNotIn(decision.current_p1, range(1, 6))
        if accept:
            regulator.acknowledge(decision, now)
        return decision

    def assert_stop(self, decision, reason):
        self.assertEqual((decision.mode, decision.command, decision.current_p1), ("overload_stop", "set_limit", 0))
        self.assertIn(reason, decision.reason)
        self.assertTrue(decision.overload_failure_latched)

    def test_feature_disabled_retains_original_zero_at_38(self):
        decision = self.sample(self.regulator(overload_enabled=False), 0, grid=38, ev=16)
        self.assertEqual(decision.current_p1, 0)
        self.assertFalse(decision.mode.startswith("overload"))

    def test_38_to_28_ev_first_never_stops_and_counts_transition(self):
        regulator = self.regulator()
        initial = self.sample(regulator, 0, grid=38, ev=16)
        mixed = self.sample(regulator, 3, grid=38, ev=6, grid_report_ts=1000)
        final = self.sample(regulator, 7, grid=28, ev=6, charger_report_ts=1003)
        self.assertEqual([d.current_p1 for d in (initial, mixed, final)], [6, 6, 6])
        self.assertEqual([d.mode for d in (initial, mixed, final)],
                         ["overload_shedding", "overload_shedding", "overload_minimum"])
        self.assertEqual(mixed.house_load_l1, 22)
        self.assertEqual(final.overload_used_seconds, 7)
        self.assertEqual(initial.overload_transition_remaining_seconds, 10)
        self.assertEqual(mixed.overload_transition_remaining_seconds, 7)
        self.assertIsNone(final.overload_transition_remaining_seconds)

    def test_grid_first_reduction_waits_for_ev_observation(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        pending = self.sample(regulator, 3, grid=28, ev=16, charger_report_ts=1000)
        final = self.sample(regulator, 7, grid=28, ev=6, grid_report_ts=1003)
        self.assertEqual(pending.mode, "overload_shedding")
        self.assertEqual(pending.house_load_l1, 22)
        self.assertEqual((final.mode, final.current_p1), ("overload_minimum", 6))

    def test_fresh_minimum_and_fresh_high_grid_stop_before_response_timeout(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        self.assert_stop(self.sample(regulator, 2, grid=31, ev=6), "minimum_observed_above_ceiling")
        self.assert_stop(self.sample(regulator, 3, grid=28, ev=6), "minimum_observed_above_ceiling")

    def test_api_acknowledgement_is_not_physical_response(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        self.assertEqual(self.sample(regulator, 9, grid=38, ev=16).current_p1, 6)
        self.assert_stop(self.sample(regulator, 10, grid=38, ev=16), "response_timeout")

    def test_failed_api_attempt_does_not_delay_deadline_start(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16, accept=False)
        self.sample(regulator, 8, grid=38, ev=16, accept=False)
        self.assert_stop(self.sample(regulator, 10, grid=38, ev=16, accept=False), "response_timeout")

    def test_report_and_lease_refreshes_never_extend_response_deadline(self):
        regulator = self.regulator(resend_limit_every_seconds=1)
        for now in (0, 1, 2, 5, 9):
            pending = self.sample(regulator, now, grid=38, ev=16)
            self.assertEqual(pending.overload_transition_remaining_seconds, 10 - now)
        self.assert_stop(self.sample(regulator, 10, grid=28, ev=6), "response_timeout")

    def test_response_strictly_before_deadline_is_accepted(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        self.assertEqual(self.sample(regulator, 9.999, grid=28, ev=6).mode, "overload_minimum")

    def test_raw_30_with_ordinary_nine_amp_headroom_still_uses_bounded_transition(self):
        decision = self.sample(self.regulator(), 0, grid=30, ev=16)
        self.assertEqual((decision.mode, decision.current_p1), ("overload_shedding", 6))
        self.assertEqual(decision.available_l1, 9)

    def test_raw_below_29_retains_normal_reduction_when_nominal_headroom_exists(self):
        decision = self.sample(self.regulator(), 0, grid=27, ev=16)
        self.assertEqual(decision.current_p1, 12)
        self.assertFalse(decision.mode.startswith("overload"))

    def test_post_response_29_holds_past_old_two_minute_pause_guard(self):
        regulator = self.regulator()
        for now in (0, 20, 120, 500, 1799):
            decision = self.sample(regulator, now, grid=29, ev=6)
            self.assertEqual((decision.mode, decision.command, decision.current_p1), ("overload_minimum", "set_limit", 6))
            self.assertEqual(decision.ttl_seconds, 60)
        self.assert_stop(self.sample(regulator, 1800, grid=29, ev=6), "cumulative_budget_exhausted")

    def test_minimum_observed_29_point_1_is_rejected_without_transition(self):
        self.assert_stop(self.sample(self.regulator(), 0, grid=29.1, ev=6), "minimum_projection_exceeds_ceiling")

    def test_transition_current_above_38_is_rejected(self):
        self.assert_stop(self.sample(self.regulator(), 0, grid=38.01, ev=16), "transition_current_exceeded")

    def test_minimum_projection_above_29_is_rejected_before_shedding(self):
        self.assert_stop(self.sample(self.regulator(), 0, grid=38, ev=14), "minimum_projection_exceeds_ceiling")

    def test_short_nominal_dip_does_not_refund_budget(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        self.assertEqual(self.sample(regulator, 900, grid=24).overload_used_seconds, 900)
        self.assertEqual(self.sample(regulator, 901).overload_used_seconds, 900)
        self.assertEqual(self.sample(regulator, 1800).overload_used_seconds, 1799)
        self.assert_stop(self.sample(regulator, 1801), "cumulative_budget_exhausted")

    def test_nominal_reserve_can_be_borrowed_without_claiming_overload_time(self):
        decision = self.sample(self.regulator(), 0, grid=24.8, ev=6)
        self.assertEqual((decision.mode, decision.current_p1), ("overload_minimum", 6))
        self.assertEqual(decision.overload_used_seconds, 0)

    def test_no_overload_start_of_stopped_car(self):
        for status in ("awaiting_start", "paused", "charging"):
            with self.subTest(status=status):
                decision = self.sample(self.regulator(), 0, grid=22, ev=0, status=status)
                self.assertEqual(decision.current_p1, 0)
                self.assertNotEqual(decision.command, "resume")

    def test_manual_pause_during_transition_stops_and_latches(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        decision = self.sample(regulator, 2, grid=22, ev=0, status="paused")
        self.assert_stop(decision, "charging_interrupted_or_paused")
        self.assertNotEqual(decision.command, "resume")

    def test_minimum_current_lost_cannot_open_another_transition(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=28, ev=6)
        self.assert_stop(self.sample(regulator, 2, grid=30, ev=8), "minimum_response_lost")

    def test_nominal_recovery_can_open_new_transition_but_does_not_reset_time(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=28, ev=6)
        self.sample(regulator, 10, grid=20, ev=6)
        decision = self.sample(regulator, 20, grid=38, ev=16)
        self.assertEqual(decision.mode, "overload_shedding")
        self.assertEqual(decision.overload_used_seconds, 10)

    def test_missing_report_timestamp_cannot_grant_or_extend_transition(self):
        for first in (True, False):
            with self.subTest(first=first):
                regulator = self.regulator()
                if not first:
                    self.sample(regulator, 0, grid=38, ev=16)
                self.assert_stop(self.sample(regulator, 2, grid=38, ev=16, grid_report_ts=None), "missing_report_timestamps")

    def test_stale_or_fault_data_overrides_pending_and_latches_allowance(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        unavailable = self.sample(regulator, 2, grid=38, ev=6, data_valid=False, data_error="stale")
        self.assertEqual((unavailable.mode, unavailable.current_p1), ("unavailable", 0))
        self.assertTrue(unavailable.overload_failure_latched)
        self.assert_stop(self.sample(regulator, 3, grid=28, ev=6), "invalid_telemetry_during_overload")

    def test_unknown_interval_preserves_previous_overload_exposure(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=28)
        self.sample(regulator, 20, grid=None, data_valid=False)
        self.assertEqual(self.sample(regulator, 30, grid=24).overload_used_seconds, 30)

    def test_backward_clock_stops_and_latches(self):
        regulator = self.regulator()
        self.sample(regulator, 10)
        backwards = self.sample(regulator, 9)
        self.assertEqual((backwards.mode, backwards.current_p1), ("unavailable", 0))
        self.assertTrue(backwards.overload_failure_latched)

    def test_reset_settings_and_disconnect_do_not_refund_time(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        self.sample(regulator, 100)
        regulator.reset()
        regulator.update_settings(regulator.settings)
        self.sample(regulator, 110, grid=10, ev=0, status="disconnected")
        self.assertEqual(regulator.export_overload_state()["used_seconds"], 110)

    def test_disable_and_dry_run_changes_do_not_refund_active_exposure(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        regulator.settings.overload_enabled = False
        regulator.settings.dry_run = True
        self.sample(regulator, 100)
        regulator.settings.overload_enabled = True
        self.assertEqual(self.sample(regulator, 200).overload_used_seconds, 200)

    def test_disabled_policy_counts_later_peaks_after_nominal_dip_once_budget_used(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        self.sample(regulator, 10, grid=20)
        regulator.settings.overload_enabled = False
        self.sample(regulator, 20, grid=28)
        self.sample(regulator, 30, grid=20)
        self.assertEqual(regulator.export_overload_state()["used_seconds"], 20)

    def test_reset_during_transition_latches_instead_of_reopening_deadline(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        regulator.reset()
        self.assert_stop(self.sample(regulator, 1, grid=38, ev=16), "interrupted_transition")

    def test_persisted_exposure_charges_unknown_downtime_and_never_restores_transition(self):
        regulator = self.regulator()
        self.sample(regulator, 0, grid=38, ev=16)
        self.sample(regulator, 3, grid=38, ev=16)
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(regulator.export_overload_state(), elapsed_seconds=60))
        decision = self.sample(restored, 100, grid=28)
        self.assertEqual(decision.overload_used_seconds, 63)
        self.assert_stop(decision, "interrupted_transition")

    def test_persisted_nominal_interval_does_not_consume_downtime(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        self.sample(regulator, 100, grid=20)
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(regulator.export_overload_state(), elapsed_seconds=500))
        self.assertEqual(self.sample(restored, 1000).overload_used_seconds, 100)

    def test_persisted_minimum_hold_cannot_reopen_lost_response_transition(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        self.sample(regulator, 10)
        state = regulator.export_overload_state()
        self.assertTrue(state["minimum_held"])
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=5))
        self.assert_stop(self.sample(restored, 100, grid=30, ev=8), "minimum_response_lost")

    def test_reset_cannot_erase_lost_response_protection_of_stable_hold(self):
        regulator = self.regulator()
        self.sample(regulator, 0)
        regulator.reset()
        self.assert_stop(self.sample(regulator, 1, grid=30, ev=8), "minimum_response_lost")

    def test_corrupt_saved_exposure_fails_closed(self):
        for state in (None, {}, {"version": 1, "used_seconds": float("nan")},
                      {"version": 1, "used_seconds": True, "exposure_active": False, "transition_pending": False}):
            with self.subTest(state=state):
                regulator = self.regulator()
                self.assertFalse(regulator.import_overload_state(state))
                self.assert_stop(self.sample(regulator, 0), "invalid_persisted_exposure")

    def test_failed_allowance_does_not_disable_safe_nominal_recovery(self):
        # Retain this regression's original nominal 6 A start configuration;
        # the independent startup suite covers the new configurable default.
        regulator = self.regulator(startup_charge_amps=6)
        self.sample(regulator, 0, grid=38.1, ev=16)
        nominal = self.sample(regulator, 1, grid=5, ev=0, status="awaiting_start")
        self.assertEqual(nominal.current_p1, 0)
        recovered = self.sample(regulator, 61, grid=5, ev=0, status="awaiting_start")
        self.assertEqual(recovered.current_p1, 16)
        self.assertTrue(recovered.overload_failure_latched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
