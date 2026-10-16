"""Accepted-cap/report-order scenarios; no HA, network or vehicle commands.

Receipt timestamps deliberately exercise ordering rather than claiming physical
acquisition times. Acknowledge simulates HTTP acceptance, never car response.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_startup_alignment_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class StartupAlignmentTests(unittest.TestCase):
    def regulator(self, **settings):
        return logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, **settings))

    def sample(self, r, now, grid, ev, status="charging", *, grid_ts=None,
               ev_ts=None, accept=True, **extra):
        d = r.evaluate(logic.RegulatorInput(
            now_ts=now, status=status, grid_l1=grid, charger_current=ev,
            grid_report_ts=grid_ts, charger_report_ts=ev_ts, **extra))
        if accept:
            r.acknowledge(d, now)
        return d

    def started(self, **settings):
        r = self.regulator(**settings)
        self.assertEqual(self.sample(r, 0, 2.6, 0.011, "awaiting_start",
                                    grid_ts=100, ev_ts=100).current_p1, 0)
        d = self.sample(r, 60, 2.6, 0.011, "ready_to_charge", grid_ts=160, ev_ts=160)
        self.assertEqual(d.current_p1, r.startup_current)
        self.assertEqual(d.mode, "starting")
        return r

    def accepted_twelve(self, **settings):
        r = self.regulator(**settings)
        self.assertEqual(self.sample(r, 0, 13.2, 11, grid_ts=100, ev_ts=100).current_p1, 11)
        self.sample(r, 1, 13.2, 11, grid_ts=101, ev_ts=101)
        self.assertEqual(self.sample(r, 31, 13.2, 11, grid_ts=131, ev_ts=131).current_p1, 12)
        return r

    def test_start_defaults_seven_while_running_minimum_remains_six(self):
        r = self.started()
        self.assertEqual((r.settings.min_charge_amps, r.startup_current), (6, 7))
        self.sample(r, 61, 9.6, 7, grid_ts=161.1, ev_ts=161)
        lowered = self.sample(r, 62, 25, 7, grid_ts=162, ev_ts=162)
        self.assertEqual((lowered.mode, lowered.current_p1), ("minimum_continuity", 6))

    def test_effective_start_clamps_to_existing_minimum_and_maximum(self):
        for options, expected in (({"max_charge_amps": 6}, 6),
                                  ({"startup_charge_amps": 6}, 6),
                                  ({"startup_charge_amps": 16, "max_charge_amps": 10}, 10),
                                  ({"startup_charge_amps": 6, "min_charge_amps": 8}, 8)):
            with self.subTest(options=options):
                r = self.regulator(**options)
                self.assertEqual(r.startup_current, expected)

    def test_invalid_start_setting_rejected(self):
        for value in (True, 5, 17, 7.5, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.regulator(startup_charge_amps=value)

    def test_only_six_amps_headroom_does_not_start_at_seven(self):
        r = self.regulator()
        for at in (0, 60, 600):
            d = self.sample(r, at, 17, 0.011, "awaiting_start", grid_ts=100+at, ev_ts=100+at)
            self.assertEqual((d.mode, d.current_p1), ("start_headroom_wait", 0))
            self.assertNotEqual(d.command, "resume")

    def test_waiting_does_not_subtract_stale_positive_ev_to_fabricate_start_capacity(self):
        r = self.regulator()
        for at in (0, 60, 600):
            d = self.sample(r, at, 20, 16, "ready_to_charge", grid_ts=100+at, ev_ts=100)
            self.assertEqual((d.current_p1, d.available_l1, d.house_load_l1), (0, 0, 20))

    def test_existing_six_offer_upgrades_once_after_full_buffered_capacity_is_stable(self):
        r = self.regulator(startup_charge_amps=6)
        self.sample(r, 0, 2.6, 0.011, "awaiting_start", grid_ts=100, ev_ts=100)
        self.sample(r, 60, 2.6, 0.011, "ready_to_charge", grid_ts=160, ev_ts=160)
        r.update_settings(logic.RegulatorSettings(active_control=True, dry_run=False, startup_charge_amps=7))
        self.assertEqual(self.sample(r, 61, 2.6, 0.011, "awaiting_start", grid_ts=161, ev_ts=160).current_p1, 6)
        self.assertEqual(self.sample(r, 120, 2.6, 0.011, "ready_to_charge", grid_ts=220, ev_ts=160).current_p1, 6)
        self.assertEqual(self.sample(r, 121, 2.6, 0.011, "ready_to_charge", grid_ts=221, ev_ts=160).current_p1, 7)
        for at in (180, 400, 1200):
            d = self.sample(r, at, 2.6, 0.011, "awaiting_start", grid_ts=100+at, ev_ts=160)
            self.assertEqual(d.current_p1, 7)
            self.assertNotIn(d.command, {"pause", "resume"})
        self.assertEqual(d.mode, "waiting_for_vehicle_stalled")

    def test_existing_six_offer_stays_six_if_start_capacity_is_not_available(self):
        r = self.regulator(startup_charge_amps=6)
        self.sample(r, 0, 17, 0, "awaiting_start")
        self.sample(r, 60, 17, 0, "ready_to_charge")
        r.update_settings(logic.RegulatorSettings(active_control=True, dry_run=False, startup_charge_amps=7))
        for at in (61, 121, 600):
            d = self.sample(r, at, 17, 0, "ready_to_charge")
            self.assertEqual((d.mode, d.current_p1), ("start_headroom_wait", 6))

    def test_idle_leakage_does_not_unlock_ramp(self):
        r = self.started()
        for at in (61, 91, 180, 400):
            d = self.sample(r, at, 2.6, 0.181, "charging", grid_ts=100+at, ev_ts=100+at)
            self.assertEqual(d.current_p1, 7)
            self.assertNotEqual(d.mode, "ramp_up")

    def test_accepted_seven_stays_seven_during_long_stable_idle_wait(self):
        r = self.started()
        for at in (61, 90, 120, 360, 1200):
            status = "ready_to_charge" if at % 2 else "awaiting_start"
            d = self.sample(r, at, 2.6, 0.011, status, grid_ts=100+at, ev_ts=100+at)
            self.assertEqual(d.current_p1, 7)
            self.assertNotIn(d.command, {"pause", "resume"})
        self.assertEqual(d.mode, "waiting_for_vehicle_stalled")

    def test_scheduled_authorization_and_manual_waits_are_not_upgraded_or_resumed(self):
        statuses = ("paused", "awaiting_smart_start", "awaiting_scheduled_start",
                    "waiting_for_schedule", "awaiting_authorization", "authenticating",
                    "paused_due_to_equalizer")
        for status in statuses:
            with self.subTest(status=status):
                r = self.regulator()
                for at in (0, 60, 600):
                    d = self.sample(r, at, 2.6, 0, status, grid_ts=100+at, ev_ts=100+at)
                    self.assertLessEqual(d.current_p1, 6)
                    self.assertNotIn(d.command, {"pause", "resume"})

    def test_owned_pause_still_does_not_resume_through_schedule(self):
        r = self.regulator(resume_for_seconds=0)
        r.paused_by_us = True
        r._pause_observed = True
        for at in (0, 60, 600):
            d = self.sample(r, at, 2.6, 0, "awaiting_scheduled_start")
            self.assertNotEqual(d.command, "resume")

    def owned_paused_offer(self):
        r = self.regulator()
        r.paused_by_us = True
        r._pause_observed = True
        self.sample(r, 0, 2.6, 0, "paused", grid_ts=1000, ev_ts=1000)
        cap = self.sample(r, 60, 2.6, 0, "paused", grid_ts=1060, ev_ts=1060)
        self.assertEqual((cap.command, cap.current_p1), ("set_limit", 7))
        refreshed = self.sample(r, 121, 2.6, 0, "paused", grid_ts=1121, ev_ts=1121)
        self.assertEqual((refreshed.command, refreshed.current_p1), ("set_limit", 7))
        resumed = self.sample(r, 126, 2.6, 0, "paused", grid_ts=1126, ev_ts=1126)
        self.assertEqual((resumed.command, resumed.current_p1), ("resume", 7))
        self.assertFalse(r.paused_by_us)
        return r

    def test_owned_paused_resumes_with_seven_and_preserves_alignment_through_lagged_paused_feedback(self):
        r = self.owned_paused_offer()
        paused_feedback = self.sample(r, 127, 2.6, 0, "paused", grid_ts=1127, ev_ts=1127)
        self.assertEqual(paused_feedback.current_p1, 7)
        self.assertNotEqual(paused_feedback.command, "resume")
        self.assertIsNotNone(r._positive_cap)
        ev_first = self.sample(r, 127.1, 2.6, 7, grid_ts=1127, ev_ts=1127.1)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("settling", 7))
        grid_later = self.sample(r, 127.3, 9.6, 7, grid_ts=1127.3, ev_ts=1127.1)
        self.assertEqual(grid_later.current_p1, 7)
        self.assertNotEqual(grid_later.command, "resume")

    def test_owned_paused_does_not_resume_without_full_start_buffer(self):
        r = self.regulator()
        r.paused_by_us = True
        r._pause_observed = True
        for at in (0, 60, 121, 300):
            d = self.sample(r, at, 17, 0, "paused", grid_ts=1000+at, ev_ts=1000+at)
            self.assertEqual(d.current_p1, 0)
            self.assertIn(d.mode, {"start_headroom_wait", "paused"})
            self.assertNotEqual(d.command, "resume")

    def test_owned_pause_losing_seven_amp_headroom_cannot_resume_with_renewed_six_cap(self):
        r = self.regulator()
        r.paused_by_us = True
        r._pause_observed = True
        self.sample(r, 0, 2.6, 0, "paused", grid_ts=1000, ev_ts=1000)
        first = self.sample(r, 60, 2.6, 0, "paused", grid_ts=1060, ev_ts=1060)
        self.assertEqual(first.current_p1, 7)
        lower = self.sample(r, 65, 17, 0, "paused", grid_ts=1065, ev_ts=1065)
        self.assertEqual((lower.mode, lower.current_p1), ("start_headroom_wait", 6))
        renewed = self.sample(r, 121, 17, 0, "paused", grid_ts=1121, ev_ts=1121)
        self.assertEqual((renewed.command, renewed.current_p1), ("set_limit", 6))
        refused = self.sample(r, 126, 17, 0, "paused", grid_ts=1126, ev_ts=1126)
        self.assertEqual((refused.mode, refused.command, refused.current_p1),
                         ("start_headroom_wait", "set_limit", 6))
        self.assertIn("7 A startup requires full buffered headroom", refused.reason)
        self.assertTrue(r.paused_by_us)

    def test_unowned_manual_paused_never_acquires_start_or_resume_permission(self):
        r = self.regulator()
        for at in (0, 60, 121, 300, 600):
            d = self.sample(r, at, 2.6, 0, "paused", grid_ts=1000+at, ev_ts=1000+at)
            self.assertLessEqual(d.current_p1, 6)
            self.assertNotEqual(d.command, "resume")
            self.assertFalse(r.paused_by_us)

    def test_post_resume_alignment_context_expires_without_reacquiring_pause_ownership(self):
        r = self.owned_paused_offer()
        self.sample(r, 127, 2.6, 0, "paused", grid_ts=1127, ev_ts=1127)
        expired = self.sample(r, 157, 2.6, 0, "paused", grid_ts=1157, ev_ts=1157)
        self.assertIsNone(r._positive_cap)
        self.assertNotEqual(expired.command, "resume")
        self.assertFalse(r.paused_by_us)

    def test_delayed_start_response_aligns_even_after_submission_window_has_elapsed(self):
        r = self.started()
        partial = self.sample(r, 74, 2.6, 3.115, grid_ts=173, ev_ts=174)
        self.assertEqual((partial.mode, partial.current_p1), ("settling", 7))
        full = self.sample(r, 74.066, 2.6, 7.01, grid_ts=173, ev_ts=174.066)
        self.assertEqual((full.mode, full.current_p1), ("settling", 7))
        self.assertAlmostEqual(full.measurement_alignment_remaining_seconds, 9.934)
        coherent = self.sample(r, 74.252, 9.6, 7.01, grid_ts=174.252, ev_ts=174.066)
        self.assertEqual(coherent.current_p1, 7)
        self.assertNotEqual(coherent.mode, "unavailable")
        self.assertIsNone(coherent.measurement_alignment_remaining_seconds)
        self.assertEqual(self.sample(r, 104.252, 9.6, 7.01, grid_ts=204.252, ev_ts=204).current_p1, 8)

    def test_positive_twelve_amp_ev_first_response_does_not_request_zero(self):
        r = self.accepted_twelve()
        mixed = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((mixed.mode, mixed.current_p1), ("settling", 12))
        self.assertAlmostEqual(mixed.house_load_l1, 2.2)
        coherent = self.sample(r, 45.901, 13.8, 12.168, grid_ts=145.901, ev_ts=144.5)
        self.assertEqual(coherent.current_p1, 12)
        self.assertNotEqual(coherent.mode, "unavailable")

    def test_coherent_partial_charging_does_not_trigger_timeout_stop_retry(self):
        r = self.started()
        self.sample(r, 74, 2.6, 3, grid_ts=173, ev_ts=174)
        coherent = self.sample(r, 75, 5.6, 3, grid_ts=175, ev_ts=174)
        self.assertEqual(coherent.current_p1, 7)
        for at in (85, 180, 400):
            d = self.sample(r, at, 5.6, 3, grid_ts=100+at, ev_ts=100+at)
            self.assertGreaterEqual(d.current_p1, 7)
            self.assertNotIn(d.command, {"pause", "resume"})
            self.assertNotEqual(d.mode, "unavailable")

    def test_maximum_seven_allows_later_distinct_response_after_long_partial_coherence(self):
        r = self.started(max_charge_amps=7)
        first = self.sample(r, 74, 2.6, 3, grid_ts=173, ev_ts=174)
        self.assertEqual((first.mode, first.current_p1), ("settling", 7))
        self.sample(r, 75, 5.6, 3, grid_ts=175, ev_ts=174)
        self.sample(r, 180, 5.6, 3, grid_ts=280, ev_ts=280)
        second = self.sample(r, 181, 5.6, 7, grid_ts=280, ev_ts=281)
        self.assertEqual((second.mode, second.current_p1), ("settling", 7))
        self.assertEqual(second.measurement_alignment_remaining_seconds, 9)
        coherent = self.sample(r, 181.2, 9.6, 7, grid_ts=281.2, ev_ts=281)
        self.assertEqual((coherent.mode, coherent.current_p1), ("normal", 7))

    def test_partial_episodes_share_budget_and_still_expire_without_a_new_grid_report(self):
        r = self.started(max_charge_amps=7, resend_limit_every_seconds=5)
        self.sample(r, 74, 2.6, 3, grid_ts=173, ev_ts=174)
        self.sample(r, 78, 5.6, 3, grid_ts=178, ev_ts=174)
        self.sample(r, 180, 5.6, 3, grid_ts=280, ev_ts=280)
        second = self.sample(r, 181, 5.6, 6, grid_ts=280, ev_ts=281)
        self.assertEqual(second.measurement_alignment_remaining_seconds, 6)
        self.sample(r, 184, 5.6, 6.6, grid_ts=280, ev_ts=284)
        late = self.sample(r, 187, 5.6, 7, grid_ts=280, ev_ts=287)
        self.assertEqual((late.mode, late.current_p1), ("unavailable", 0))
        self.assertIn("timeout", late.reason)

    def test_same_value_cannot_reopen_a_closed_partial_episode_after_grid_declines(self):
        r = self.started(max_charge_amps=7)
        self.sample(r, 74, 2.6, 3, grid_ts=173, ev_ts=174)
        self.sample(r, 75, 5.6, 3, grid_ts=175, ev_ts=174)
        self.sample(r, 180, 5.6, 3, grid_ts=280, ev_ts=280)
        same_ev = self.sample(r, 181, 1, 3, grid_ts=281, ev_ts=281)
        self.assertEqual((same_ev.mode, same_ev.current_p1), ("unavailable", 0))

    def test_first_grid_confirmation_after_expired_budget_does_not_refund_it(self):
        r = self.started(max_charge_amps=7)
        self.sample(r, 74, 2.6, 7, grid_ts=173, ev_ts=174)
        late_grid = self.sample(r, 85, 9.6, 7, grid_ts=185, ev_ts=174)
        self.assertEqual((late_grid.mode, late_grid.current_p1), ("unavailable", 0))
        self.assertIn("timeout", late_grid.reason)

    def test_ev_first_below_grid_does_not_manufacture_headroom_or_allow_next_rise(self):
        r = self.accepted_twelve()
        mixed = self.sample(r, 44.5, 13.2, 12, grid_ts=141, ev_ts=144.5)
        self.assertEqual((mixed.mode, mixed.current_p1), ("settling", 12))
        self.assertAlmostEqual(mixed.house_load_l1, 2.2)
        self.assertEqual(self.sample(r, 49, 13.2, 12, grid_ts=141, ev_ts=149).current_p1, 12)
        coherent = self.sample(r, 50, 14.2, 12, grid_ts=150, ev_ts=149)
        self.assertEqual(coherent.current_p1, 12)
        self.assertEqual(self.sample(r, 79, 14.2, 12, grid_ts=179, ev_ts=179).current_p1, 12)
        self.assertEqual(self.sample(r, 80, 14.2, 12, grid_ts=180, ev_ts=180).current_p1, 13)

    def test_reverse_source_order_retains_accepted_cap(self):
        r = self.accepted_twelve()
        self.assertEqual(self.sample(r, 44.5, 13.8, 11.349, grid_ts=144.5, ev_ts=141).current_p1, 12)
        self.assertEqual(self.sample(r, 45.901, 13.8, 12.168, grid_ts=144.5, ev_ts=145.901).current_p1, 12)
        self.assertEqual(self.sample(r, 49, 13.8, 12.168, grid_ts=149, ev_ts=145.901).current_p1, 12)

    def test_fixed_deadline_survives_ttl_renewals_and_new_same_value_ev_reports(self):
        r = self.accepted_twelve(resend_limit_every_seconds=5)
        self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        renewed = self.sample(r, 49.5, 10.9, 12.168, grid_ts=141, ev_ts=149.5)
        self.assertTrue(renewed.should_call_service)
        self.assertAlmostEqual(renewed.measurement_alignment_remaining_seconds, 5)
        expired = self.sample(r, 54.5, 10.9, 12.168, grid_ts=141, ev_ts=154.5)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))
        self.assertIn("timeout", expired.reason)
        self.assertEqual(self.sample(r, 54.6, 10.9, 12.168, grid_ts=141, ev_ts=154.6).current_p1, 0)

    def test_alignment_cannot_reenter_from_same_accepted_cap_after_completion(self):
        r = self.accepted_twelve()
        self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.sample(r, 45.9, 13.8, 12.168, grid_ts=145.9, ev_ts=144.5)
        self.sample(r, 61, 13.8, 12.168, grid_ts=161, ev_ts=161)
        d = self.sample(r, 62, 2.6, 12.168, grid_ts=162, ev_ts=162)
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_no_grace_without_successfully_accepted_increase(self):
        r = self.regulator()
        self.sample(r, 0, 13.2, 11, grid_ts=100, ev_ts=100)
        self.sample(r, 1, 13.2, 11, grid_ts=101, ev_ts=101)
        self.sample(r, 31, 13.2, 11, grid_ts=131, ev_ts=131, accept=False)
        d = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_missing_source_timestamps_cannot_enable_grace(self):
        r = self.regulator()
        self.sample(r, 0, 13.2, 11)
        self.sample(r, 1, 13.2, 11)
        self.sample(r, 31, 13.2, 11)
        d = self.sample(r, 44.5, 10.9, 12.168)
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_report_without_an_actual_rise_does_not_start_or_renew_grace(self):
        r = self.accepted_twelve()
        for at in (32, 34, 40):
            d = self.sample(r, at, 13.2, 11, grid_ts=100+at, ev_ts=100+at)
            self.assertIsNone(d.measurement_alignment_remaining_seconds)
        d = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.current_p1), ("settling", 12))

    def test_high_raw_grid_sheds_immediately_instead_of_holding_prior_cap(self):
        r = self.accepted_twelve()
        self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        d = self.sample(r, 45, 26, 12, grid_ts=145, ev_ts=145)
        self.assertEqual(d.current_p1, 9)
        self.assertNotEqual(d.mode, "settling")

    def test_high_household_load_still_requests_zero(self):
        r = self.accepted_twelve()
        d = self.sample(r, 44.5, 32, 12, grid_ts=144.5, ev_ts=144.5)
        self.assertEqual(d.current_p1, 0)

    def test_current_exceeding_accepted_cap_does_not_receive_grace(self):
        r = self.accepted_twelve()
        d = self.sample(r, 44.5, 13.8, 12.6, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))
        self.assertIn("excess_current", d.reason)

    def test_stale_invalid_and_fault_input_override_pending_grace(self):
        for extra in ({"data_valid": False, "data_error": "stale"},
                      {"grid": "unavailable"}, {"status": "error"}):
            with self.subTest(extra=extra):
                r = self.accepted_twelve()
                self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
                args = {"grid": 10.9, "ev": 12.168, "status": "charging"}
                args.update(extra)
                d = self.sample(r, 45, grid_ts=141, ev_ts=145, **args)
                self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))

    def test_soft_zero_guard_is_not_bypassed_by_alignment(self):
        r = self.accepted_twelve()
        settings = r.settings
        settings.soft_limit_amps = 10
        settings.soft_for_seconds = 0
        settings.soft_current_amps = 0
        d = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.current_p1), ("soft_limit", 0))

    def test_pause_guard_is_not_bypassed_by_alignment(self):
        r = self.accepted_twelve()
        r.settings.pause_limit_amps = 10
        r.settings.pause_for_seconds = 0
        d = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.command), ("pause", "pause"))

    def test_alignment_never_resumes_an_owned_pause(self):
        r = self.accepted_twelve(resume_for_seconds=0)
        r.paused_by_us = True
        r._pause_observed = True
        d = self.sample(r, 44.5, 10.9, 12.168, grid_ts=141, ev_ts=144.5)
        self.assertEqual((d.mode, d.command, d.current_p1), ("settling", "set_limit", 12))

    def test_complete_or_disconnect_clears_old_positive_alignment(self):
        for status in ("completed", "disconnected"):
            with self.subTest(status=status):
                r = self.accepted_twelve()
                d = self.sample(r, 44.5, 2.6, 12.168, status, grid_ts=144.5, ev_ts=141)
                self.assertEqual((d.mode, d.command), ("idle", None))
                self.assertIsNone(r._positive_cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
