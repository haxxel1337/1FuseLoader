"""An old, unchanged vendor EVENT is not a new numeric contradiction."""
from dataclasses import replace
import unittest

import test_1fuseloader_b19_alignment as fixtures

logic = fixtures.logic
EPOCH = fixtures.fixtures.EPOCH


class StableEventAgeTests(unittest.TestCase):
    sample = fixtures.LowGridAlignmentTests.sample
    regulator = fixtures.LowGridAlignmentTests.regulator
    pending = fixtures.LowGridAlignmentTests.pending
    running = fixtures.LowGridAlignmentTests.running

    def stable_seven(self, **options):
        options.setdefault("max_charge_amps", 7)
        r = self.regulator(**options)
        d = self.sample(r, 0, 9.4, 7.085, et=0)
        self.assertEqual((d.current_p1, r.last_target[0]), (7, 7))
        return r

    def assert_no_new_episode(self, r, d):
        self.assertGreater(d.current_p1, 0)
        self.assertNotIn(d.mode, {"low_grid_alignment", "low_grid_alignment_stop"})
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertIsNone(d.nominal_response_remaining_seconds)
        self.assertFalse(r.export_overload_state()["normal_control"].get("alignment_pending", False))

    def test_first_old_stable_seven_after_import_option_enabled_never_opens_timer(self):
        r = self.stable_seven(nominal_import_only_fallback=False)
        for t in (30, 60, 90, 120):
            self.sample(r, t, 9.4, 7.085, et=0)
        before = r.export_overload_state()
        r.update_settings(replace(r.settings, nominal_import_only_fallback=True))
        for t in (130, 135, 139.9, 140, 145, 160):
            d = self.sample(r, t, 9.4, 7.085, et=0)
            self.assert_no_new_episode(r, d)
            self.assertEqual(d.current_p1, 7)
        after = r.export_overload_state()
        self.assertEqual(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], "response_timeout")
        self.assertEqual(after["normal_control"]["stops"], 0)

    def test_report_skew_alone_does_not_create_first_alignment(self):
        r = self.stable_seven()
        for t in (26, 30, 35, 36, 45):
            d = self.sample(r, t, 9.4, 7.085, et=0)
            self.assertGreater(d.source_report_skew_seconds, 25)
            self.assert_no_new_episode(r, d)

    def test_absolute_event_age_alone_does_not_create_first_alignment(self):
        r = self.stable_seven()
        for t in range(30, 301, 30):
            d = self.sample(r, t, 9.4, 7.085, et=0)
            self.assert_no_new_episode(r, d)
            self.assertEqual(d.current_p1, 7)

    def test_fresh_coherent_pair_can_age_without_first_completing_any_episode(self):
        r = self.stable_seven()
        for t in (5, 15, 25, 26, 30, 40, 60):
            self.assert_no_new_episode(r, self.sample(r, t, 9.4, 7.085, et=0))

    def test_age_alone_does_not_supply_ev_credit_for_a_new_higher_offer(self):
        r = self.stable_seven(nominal_import_only_fallback=False)
        for t in (30, 60, 90, 120):
            self.sample(r, t, 9.4, 7.085, et=0)
        r.update_settings(replace(r.settings, nominal_import_only_fallback=True, max_charge_amps=16))
        for t in (130, 140, 160, 175, 190):
            d = self.sample(r, t, 9.4, 7.085, et=0)
            self.assert_no_new_episode(r, d)
            if d.current_p1 > 7:
                self.assertLessEqual(d.grid_l1 + d.current_p1, 23)
            self.assertLess(d.current_p1, 16)

    def test_true_numeric_conflict_still_lowers_sixteen_with_fixed_deadline(self):
        r = self.running()
        d = self.sample(r, 10, 2.4, 16.2, et=0)
        self.assertEqual((d.mode, d.current_p1, d.nominal_response_deadline_ts), ("low_grid_alignment", 7, 20))
        for t in (12, 16, 19.9):
            d = self.sample(r, t, 2.4, 16.2, et=0)
            self.assertEqual((d.current_p1, d.nominal_response_deadline_ts), (7, 20))
        self.assertEqual(self.sample(r, 20, 2.4, 16.2, et=0).current_p1, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_numeric_seven_conflict_still_times_out_without_a_new_source_event(self):
        r = self.stable_seven()
        d = self.sample(r, 10, 2.4, 7.085, et=0)
        self.assertEqual(d.nominal_response_deadline_ts, 20)
        # Coherent numbers do not erase an already-open, unconfirmed episode.
        d = self.sample(r, 12, 9.4, 7.085, et=0)
        self.assertEqual((d.mode, d.nominal_response_deadline_ts), ("low_grid_alignment", 20))
        self.assertEqual(self.sample(r, 20, 9.4, 7.085, et=0).current_p1, 0)

    def test_completed_episode_reentry_spends_only_original_remaining_time(self):
        r = self.pending()
        complete = self.sample(r, 15, 6.5, 4.5, et=12)
        self.assertEqual(complete.mode, "low_grid_alignment_complete")
        reopened = self.sample(r, 16, 2.4, 4.5, et=12)
        self.assertEqual((reopened.current_p1, reopened.nominal_response_deadline_ts), (7, 20))
        self.assertEqual(reopened.nominal_response_remaining_seconds, 4)
        self.assertEqual(self.sample(r, 20, 2.4, 4.5, et=12).current_p1, 0)

    def test_genuine_conflict_after_completed_deadline_does_not_open_new_time(self):
        r = self.pending()
        self.sample(r, 15, 6.5, 4.5, et=12)
        d = self.sample(r, 25, 2.4, 4.5, et=12, accept=False)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment_stop", 0))
        self.assertEqual(self.sample(r, 26, 2.4, 4.5, et=12, accept=False).current_p1, 0)

    def test_new_event_then_later_grid_can_still_finish_before_original_deadline(self):
        r = self.stable_seven()
        self.sample(r, 10, 2.4, 4.5, et=10)
        d = self.sample(r, 13, 6.5, 4.5, et=10)
        self.assertEqual((d.mode, d.current_p1), ("low_grid_alignment_complete", 7))

    def test_rising_raw_and_product_fault_still_zero_a_pending_episode(self):
        for kw, grid in (({}, 26.1), ({"data_valid": False}, 2.4),
                         ({"charger_current_trusted": False}, 2.4)):
            r = self.pending()
            d = self.sample(r, 12, grid, 16.2, et=0, **kw)
            self.assertEqual(d.current_p1, 0)
            self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_source_fault_and_high_raw_do_not_gain_an_age_only_exception(self):
        for kw, grid in (({"data_valid": False}, 9.4), ({"charger_current_trusted": False}, 26.1),
                         ({}, 40)):
            r = self.stable_seven()
            d = self.sample(r, 30, grid, 7.085, et=0, **kw)
            self.assertEqual(d.current_p1, 0)
            self.assertNotEqual(d.mode, "low_grid_alignment")

    def test_nonnumeric_invalid_source_still_zeroes_instead_of_generic_import_hold(self):
        for kw in ({"charger_current_trusted": False}, {"grid_report_ts": None},
                   {"charger_report_ts": None}, {"wall_now_ts": None},
                   {"grid_report_ts": EPOCH-200},
                   {"grid_report_ts": EPOCH+50},
                   {"charger_report_ts": EPOCH+50}):
            with self.subTest(kw=kw):
                r = self.stable_seven()
                d = self.sample(r, 30, 9.4, 7.085, et=0, **kw)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.nominal_response_deadline_ts)

    def test_current_beyond_every_known_offer_retains_protective_zero(self):
        r = self.stable_seven()
        # The grid/EV difference is below the separate 1 A mismatch threshold.
        d = self.sample(r, 30, 9.4, 8, et=0)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.nominal_response_deadline_ts)

    def test_fresh_nonconflicting_source_keeps_legacy_path(self):
        r = self.stable_seven()
        d = self.sample(r, 30, 9.4, 8, et=30)
        self.assertEqual(d.current_p1, 7)
        self.assertNotEqual(d.mode, "low_grid_alignment_stop")

    def test_pending_restore_still_debits_existing_retry_family(self):
        r = self.pending()
        state = r.export_overload_state()
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        d = self.sample(restored, 30, 2.4, 0, status="awaiting_start")
        self.assertEqual((d.current_p1, d.measurement_uncertainty_stops), (0, 1))
        self.assertGreater(d.measurement_recovery_wait_seconds, 0)
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")

    def test_disabling_optional_overload_does_not_change_stable_event_rule(self):
        r = self.stable_seven(overload_enabled=False)
        for t in (30, 60, 90, 120, 150):
            self.assert_no_new_episode(r, self.sample(r, t, 9.4, 7.085, et=0))


if __name__ == "__main__":
    unittest.main()
