"""Synthetic state/cache uncertainty; no cloud receipt is a physical sample."""
from __future__ import annotations

from dataclasses import replace
import math
import unittest

import test_1fuseloader_blocked_overload_normal as fixtures

logic = fixtures.logic


class MeasurementUncertaintyTests(unittest.TestCase):
    sample = fixtures.BlockedOverloadNormalTests.sample
    running_sixteen = fixtures.BlockedOverloadNormalTests.running_sixteen

    def regulator(self, **options):
        options.setdefault("nominal_import_only_fallback", True)
        return fixtures.BlockedOverloadNormalTests.regulator(self, **options)

    def reduced(self, **options):
        r = self.running_sixteen(**options)
        d = self.sample(r, 10, 26, 16.2)
        self.assertEqual((d.current_p1, r.last_target[0]), (13, 13))
        return r

    def degraded(self, **options):
        r = self.reduced(**options)
        d = self.sample(r, 12, 14.8, 16.173, et=11)
        self.assertEqual((d.mode, d.current_p1, r.last_target[0]), ("measurement_uncertainty", 8, 8))
        return r

    def test_optin_raw_bound_lowers_thirteen_to_eight_without_ev_credit(self):
        r = self.reduced()
        before = r.export_overload_state()
        d = self.sample(r, 12, 14.8, 16.173, et=11)
        self.assertEqual((d.current_p1, d.house_load_l1, d.available_l1), (8, 14.8, 8))
        self.assertEqual(d.house_load_basis, "verified_import_only_raw_bound")
        self.assertLessEqual(14.8 + d.current_p1, 23)
        self.assertEqual(r.export_overload_state()["failure_reason"], before["failure_reason"])
        self.assertGreaterEqual(r.export_overload_state()["used_seconds"], before["used_seconds"])

    def test_optout_preserves_protective_zero_but_enters_bounded_recovery(self):
        r = self.reduced(nominal_import_only_fallback=False)
        d = self.sample(r, 12, 14.8, 16.173, et=11)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("inconsistent", d.reason)
        self.assertEqual(d.measurement_uncertainty_stops, 1)

    def test_positive_context_old_ev_does_not_stop_safe_lower_offer(self):
        r = self.reduced()
        r._positive_cap = 13
        d = self.sample(r, 12, 14.8, 16.173)
        self.assertEqual((d.mode, d.current_p1), ("measurement_uncertainty", 8))

    def test_excess_positive_response_zero_counts_after_context_is_retired(self):
        r = self.reduced(nominal_import_only_fallback=False)
        r._positive_cap = 13
        d = self.sample(r, 12, 18, 16.2)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("excess_current", d.reason)
        self.assertEqual(d.measurement_uncertainty_stops, 1)

    def test_cascade_preserves_original_known_offer_without_raising(self):
        r = self.degraded()
        first = self.sample(r, 15, 16.9, 16.173)
        self.assertEqual(first.current_p1, 6)
        second = self.sample(r, 18, 12, 16.173)
        self.assertEqual(second.current_p1, 6)
        self.assertEqual(r._uncertainty_previous_offer, 16)

    def test_untrusted_cache_uses_no_ev_subtraction(self):
        r = self.reduced()
        d = self.sample(r, 12, 14.8, 16.173, charger_current_trusted=False)
        self.assertEqual((d.current_p1, d.house_load_l1), (8, 14.8))
        self.assertFalse(d.charger_current_trusted)

    def test_untrusted_cache_without_optin_stops(self):
        r = self.reduced(nominal_import_only_fallback=False)
        d = self.sample(r, 12, 14.8, 16.173, charger_current_trusted=False)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("not trusted", d.reason)

    def test_no_accepted_positive_offer_cannot_start_from_raw_fallback(self):
        r = self.regulator()
        d = self.sample(r, 0, 14.8, 16.173, charger_current_trusted=False)
        self.assertEqual(d.current_p1, 0)

    def test_expired_lease_cannot_be_reanimated_by_uncertainty_fallback(self):
        r = self.reduced()
        d = self.sample(r, 71, 14.8, 16.173)
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(d.mode, "measurement_uncertainty")

    def test_coherent_cached_state_cannot_retire_mode_after_lease_expiry(self):
        r = self.degraded()
        for now in (20, 40):
            self.sample(r, now, 10.5, 8, et=14, accept=False)
        d = self.sample(r, 73, 10.5, 8, et=14, accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("lease expired", d.reason)
        self.assertEqual(d.measurement_uncertainty_stops, 1)

    def test_no_safe_six_is_a_stop_not_blind_minimum(self):
        r = self.reduced()
        d = self.sample(r, 12, 17.1, 16.173, charger_current_trusted=False)
        self.assertEqual(d.current_p1, 0)

    def test_full_buffer_boundaries(self):
        for raw, expected in ((17, 6), (17.00001, 0)):
            with self.subTest(raw=raw):
                r = self.reduced()
                d = self.sample(r, 12, raw, 16.173, charger_current_trusted=False)
                self.assertEqual(d.current_p1, expected)

    def test_impossible_current_above_known_offer_still_stops(self):
        r = self.reduced()
        d = self.sample(r, 12, 14.8, 25, charger_current_trusted=False)
        self.assertEqual(d.current_p1, 0)

    def test_fault_and_invalid_current_never_use_fallback(self):
        for metadata in ({"data_valid": False}, {"data_error": "source fault"},
                         {"status": "fault"}, {"charger_current": math.nan},
                         {"charger_current": True}, {"grid_l1": True}):
            with self.subTest(metadata=metadata):
                r = self.reduced()
                fields = dict(now_ts=12, status="charging", grid_l1=14.8,
                              charger_current=16.173, wall_now_ts=fixtures.EPOCH+12,
                              grid_report_ts=fixtures.EPOCH+12, charger_report_ts=fixtures.EPOCH+12)
                fields.update(metadata)
                d = r.evaluate(logic.RegulatorInput(**fields))
                self.assertEqual(d.current_p1, 0)

    def test_missing_future_stale_raw_clock_never_uses_fallback(self):
        for metadata in ({"grid_report_ts": None}, {"gt": 18}, {"gt": -109},
                         {"wall_now_ts": None}, {"grid_report_ts": True}):
            with self.subTest(metadata=metadata):
                r = self.reduced()
                d = self.sample(r, 12, 14.8, 16.173, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.mode, "measurement_uncertainty")

    def test_updated_cap_bounds_are_authoritative(self):
        for limits in ({"min_charge_amps": 14}, {"max_charge_amps": 6}):
            with self.subTest(limits=limits):
                r = self.reduced()
                r.update_settings(replace(r.settings, **limits))
                d = self.sample(r, 12, 14.8, 16.173)
                self.assertEqual(d.current_p1, 0)

    def test_manual_pause_schedule_and_auth_cannot_resume(self):
        for status in ("paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(status=status):
                r = self.reduced()
                d = self.sample(r, 12, 14.8, 16.173, status=status)
                self.assertNotEqual(d.mode, "measurement_uncertainty")
                self.assertNotEqual(d.command, "resume")

    def test_soft_and_pause_guards_win(self):
        r = self.reduced()
        r.update_settings(replace(r.settings, enable_soft_guard=True, soft_limit_amps=9,
                                  soft_for_seconds=0, soft_current_amps=0))
        d = self.sample(r, 12, 14.8, 16.173)
        self.assertEqual((d.mode, d.current_p1), ("soft_limit", 0))
        r = self.reduced()
        r.update_settings(replace(r.settings, enable_pause_resume=True, pause_limit_amps=9, pause_for_seconds=0))
        d = self.sample(r, 12, 14.8, 16.173)
        self.assertEqual((d.command, d.current_p1, d.should_call_service), ("pause", 0, True))

    def test_active_experimental_deadline_is_not_rescued(self):
        r = self.running_sixteen(latched=False)
        first = self.sample(r, 10, 30, 16.2)
        self.assertEqual(first.current_p1, 6)
        d = self.sample(r, 20, 14.8, 16.173)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_real_hard_raw_grid_remains_protective(self):
        for raw in (29.1, 38.1, 40):
            with self.subTest(raw=raw):
                r = self.degraded()
                d = self.sample(r, 14, raw, 16.173)
                self.assertEqual(d.current_p1, 0)

    def test_same_failed_cap_is_rate_limited_but_lower_protection_is_immediate(self):
        r = self.reduced()
        a = self.sample(r, 12, 14.8, 16.173, accept=False)
        b = self.sample(r, 13, 14.8, 16.173, accept=False)
        lower = self.sample(r, 14, 16.8, 16.173, accept=False)
        self.assertTrue(a.should_call_service)
        self.assertFalse(b.should_call_service)
        self.assertEqual((lower.current_p1, lower.should_call_service), (6, True))
        retry = self.sample(r, 19, 16.8, 16.173, accept=False)
        self.assertTrue(retry.should_call_service)

    def test_unaccepted_lower_offer_cannot_be_retired_by_coherent_old_cap(self):
        r = self.reduced()
        self.sample(r, 12, 14.8, 16.173, accept=False)
        for now in (15, 25, 35, 45):
            d = self.sample(r, now, 15, 13, et=14, accept=False)
            self.assertEqual((d.current_p1, d.measurement_uncertainty_active), (8, True))
            self.assertEqual(r.last_target[0], 13)

    def test_ttl_and_receipts_alone_do_not_recover(self):
        r = self.degraded()
        for now in (20, 40, 70, 100):
            d = self.sample(r, now, 14.8, 16.173)
            self.assertEqual(d.current_p1, 8)
            self.assertTrue(d.measurement_uncertainty_active)

    def test_unchanged_trusted_event_and_new_grid_can_recover_without_faking_age(self):
        r = self.degraded()
        for now in (15, 25, 35):
            d = self.sample(r, now, 10.5, 8.0, et=14)
            self.assertEqual(d.current_p1, 8)
        d = self.sample(r, 45, 10.5, 8.0, et=14)
        self.assertFalse(d.measurement_uncertainty_active)
        self.assertEqual(d.current_p1, 8)  # old event cannot credit an increase
        self.assertAlmostEqual(d.charger_report_age_seconds, 31)

    def test_old_trusted_native_event_never_earns_high_raw_ev_credit(self):
        r = self.running_sixteen()
        d = self.sample(r, 10, 25.1, 16.227, et=-3600)
        self.assertEqual(d.current_p1, 0)
        self.assertFalse(r._fast_sources_fresh())
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_stable_old_native_event_is_not_itself_a_charging_fault(self):
        r = self.running_sixteen()
        for now in (10, 30, 60, 90):
            d = self.sample(r, now, 18, 16, et=-3600)
            self.assertEqual(d.current_p1, 16)
            self.assertFalse(r._fast_sources_fresh())

    def test_untrusted_events_cannot_count_as_recovery(self):
        r = self.degraded()
        for now in (15, 25, 45, 55):
            d = self.sample(r, now, 10.5, 8, charger_current_trusted=False)
            self.assertTrue(d.measurement_uncertainty_active)
            self.assertEqual(d.current_p1, 8)

    def test_first_uncertainty_restart_waits_sixty_then_offers_only_seven(self):
        r = self.reduced(nominal_import_only_fallback=False)
        self.sample(r, 12, 14.8, 16.173)
        for now in (13, 42, 71):
            d = self.sample(r, now, 3, .023, status="awaiting_start")
            self.assertEqual(d.current_p1, 0)
        d = self.sample(r, 73, 3, .023, status="awaiting_start")
        self.assertEqual((d.current_p1, r.last_target[0]), (7, 7))
        self.assertEqual(r._uncertainty_stops, 1)

    def test_third_failed_measurement_attempt_needs_attention_not_endless_pulses(self):
        r = self.reduced(nominal_import_only_fallback=False)
        self.sample(r, 12, 14.8, 16.173)
        for now in (13, 73):
            self.sample(r, now, 3, .023, status="awaiting_start")
        self.assertEqual(r.last_target[0], 7)
        self.sample(r, 75, 5, 7.2, charger_current_trusted=False)
        self.assertEqual(r._uncertainty_stops, 2)
        for now in (76, 136, 194):
            self.assertEqual(self.sample(r, now, 3, .023, status="awaiting_start").current_p1, 0)
        d = self.sample(r, 196, 3, .023, status="awaiting_start")
        self.assertEqual(d.current_p1, 7)
        d = self.sample(r, 198, 5, 7.2, charger_current_trusted=False)
        self.assertEqual((d.mode, d.current_p1, d.measurement_uncertainty_stops),
                         ("measurement_recovery_blocked", 0, 3))
        for now in (199, 400, 1000):
            d = self.sample(r, now, 3, .023, status="awaiting_start")
            self.assertEqual(d.current_p1, 0)
            self.assertEqual(d.measurement_uncertainty_stops, 3)
        self.sample(r, 1001, 3, 0, status="disconnected")
        self.assertEqual(r._uncertainty_stops, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")


if __name__ == "__main__":
    unittest.main()
