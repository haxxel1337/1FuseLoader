"""Synthetic rejection diagnostics preserve the existing charging policy."""
from __future__ import annotations

import math
import unittest

import test_1fuseloader_blocked_overload_normal as fixtures


class NominalDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.BlockedOverloadNormalTests()

    def rejected(self, **metadata):
        r = self.case.running_sixteen()
        d = self.case.sample(r, 40, 25.1, 16.227, accept=False, **metadata)
        self.assertEqual((d.mode, d.current_p1), ("overload_stop", 0))
        self.assertIn("response_timeout", d.reason)
        self.case.assert_latch_preserved(r)
        return r, d

    def test_numeric_skew_explains_nominal_rejection_separately_from_old_latch(self):
        _, d = self.rejected(et=10.755340)
        self.assertEqual(d.nominal_rejection_code, "report_skew_exceeds_alignment_window")
        self.assertAlmostEqual(d.source_report_skew_seconds, 29.244660, places=5)
        self.assertAlmostEqual(d.charger_report_age_seconds, 29.244660, places=5)
        self.assertEqual((d.grid_report_age_seconds, d.source_alignment_window_seconds), (0, 25))
        self.assertIn(d.nominal_rejection_code, d.reason)

    def test_fresh_receipt_allows_only_normal_fourteen_with_latch_unchanged(self):
        r = self.case.running_sixteen()
        d = self.case.sample(r, 40, 25.1, 16.227, et=39)
        self.assertEqual((d.mode, d.current_p1), ("normal", 14))
        self.assertIsNone(d.nominal_rejection_code)
        self.assertLessEqual(d.house_load_l1+14, 23)
        self.case.assert_latch_preserved(r)

    def test_rejection_code_is_cleared_on_every_evaluation(self):
        r, previous = self.rejected(et=10)
        d = self.case.sample(r, 41, 20, 16, accept=False)
        self.assertIsNotNone(previous.nominal_rejection_code)
        self.assertIsNone(d.nominal_rejection_code)

    def test_absolute_age_and_future_checks_have_distinct_codes(self):
        cases = [({"et": -81}, "charger_receipt_stale"),
                 ({"gt": -81}, "grid_receipt_stale"),
                 ({"et": 46}, "charger_receipt_in_future"),
                 ({"gt": 46}, "grid_receipt_in_future"),
                 ({"charger_report_ts": None}, "charger_receipt_missing_or_invalid"),
                 ({"grid_report_ts": None}, "grid_receipt_missing_or_invalid"),
                 ({"wall_now_ts": None}, "wall_clock_missing_or_invalid")]
        for metadata, code in cases:
            with self.subTest(code=code):
                _, d = self.rejected(**metadata)
                self.assertEqual(d.nominal_rejection_code, code)

    def test_nan_and_infinite_configuration_never_turn_freshness_true(self):
        for name, code in (("measurement_settle_seconds", "source_alignment_window_invalid"),
                           ("max_sensor_age_seconds", "source_age_limit_invalid")):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(name=name, value=value):
                    r = self.case.running_sixteen()
                    setattr(r.settings, name, value)
                    d = self.case.sample(r, 40, 25.1, 16.227, accept=False)
                    self.assertEqual(d.current_p1, 0)
                    self.assertFalse(r._fast_sources_fresh())
                    self.assertEqual(d.nominal_rejection_code, code)

    def test_genuine_household_load_cannot_be_relabelled_as_numeric_skew(self):
        r = self.case.running_sixteen()
        d = self.case.sample(r, 40, 28, 6)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(d.nominal_rejection_code, "no_buffered_nominal_reduction")
        self.case.assert_latch_preserved(r)

    def test_cap_excess_and_raw_ceiling_rejections_remain_distinct(self):
        for grid, ev, code in ((26.2, 18, "current_exceeds_known_offer"),
                               (29.001, 16.1, "raw_grid_above_post_response_ceiling")):
            with self.subTest(code=code):
                r = self.case.running_sixteen()
                d = self.case.sample(r, 40, grid, ev)
                self.assertEqual((d.current_p1, d.nominal_rejection_code), (0, code))

    def test_expired_normal_response_is_not_reported_as_new_experimental_timeout(self):
        r = self.case.running_sixteen()
        self.case.sample(r, 10, 26.2, 16.165)
        d = self.case.sample(r, 35, 26.2, 16.165)
        self.assertEqual((d.current_p1, d.nominal_rejection_code), (0, "nominal_response_window_expired"))
        self.case.assert_latch_preserved(r)


if __name__ == "__main__":
    unittest.main()
