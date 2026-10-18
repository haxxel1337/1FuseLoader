"""Recovery policy with separately verified cloud snapshots; no external I/O.

All timestamps below are synthetic source-receipt clocks. They do not establish
physical acquisition time or promise synchronized cloud/meter measurements.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_b9_recovery_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class B9RecoveryTests(unittest.TestCase):
    def regulator(self, **options):
        return logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, **options))

    def sample(self, r, now, grid, ev=0.011, status="ready_to_charge", *, accept=True, **extra):
        reports = {"wall_now_ts": 1000 + now, "grid_report_ts": 1000 + now,
                   "charger_report_ts": 1000 + now}
        reports.update(extra)
        d = r.evaluate(logic.RegulatorInput(now_ts=now, status=status, grid_l1=grid,
                                           charger_current=ev, **reports))
        self.assertNotIn(d.current_p1, range(1, 6))
        if accept:
            r.acknowledge(d, now)
        return d

    def waiting_offer(self, grid=2.6, **options):
        r = self.regulator(**options)
        self.assertEqual(self.sample(r, 0, grid).current_p1, 0)
        return r, self.sample(r, 60, grid)

    def test_start_threshold_seven_allows_full_safe_offer_then_stays_sixteen(self):
        r, offered = self.waiting_offer()
        self.assertEqual((r.startup_current, offered.current_p1), (7, 16))
        for now in (90, 360, 1200):
            d = self.sample(r, now, 2.6)
            self.assertEqual(d.current_p1, 16)
            self.assertNotIn(d.command, {"pause", "resume"})
        self.assertEqual(d.mode, "waiting_for_vehicle_stalled")

    def test_start_still_requires_seven_amps_of_stable_buffered_capacity(self):
        r = self.regulator()
        for now in (0, 60, 600):
            self.assertEqual(self.sample(r, now, 17).current_p1, 0)

    def test_waiting_offer_uses_raw_grid_not_retained_high_ev_value(self):
        r = self.regulator()
        for now in (0, 60):
            d = self.sample(r, now, 10, 8, charger_report_ts=800)
        # An old high current also leaves the earlier zero-cap response
        # unverified, so startup must wait for the real read, not use its credit.
        self.assertEqual((d.current_p1, d.available_l1, d.house_load_l1), (0, 13, 10))
        self.assertTrue(d.current_refresh_required)

    def test_unverified_old_current_cannot_seed_initial_sixteen_amp_takeover(self):
        for extra in ({"charger_report_ts": 800}, {"charger_report_ts": None},
                      {"wall_now_ts": None}, {"charger_report_ts": 1010}):
            with self.subTest(extra=extra):
                d = self.sample(self.regulator(), 0, 20, 16, "charging", **extra)
                self.assertEqual((d.current_p1, d.house_load_l1), (0, 20))
                self.assertTrue(d.current_refresh_required)

    def test_old_current_holds_accepted_cap_and_requests_real_refresh(self):
        r = self.regulator()
        self.assertEqual(self.sample(r, 0, 23, 10, "charging").current_p1, 10)
        blocked = self.sample(r, 30, 14, 10, "charging", charger_report_ts=800)
        self.assertEqual(blocked.current_p1, 10)
        self.assertTrue(blocked.current_refresh_required)
        refreshed = self.sample(r, 31, 14, 10, "charging", charger_report_ts=1030,
                                charger_snapshot_verified=True)
        self.assertEqual(refreshed.current_p1, 16)
        self.assertFalse(refreshed.current_refresh_required)

    def test_snapshot_flag_does_not_make_old_future_or_skewed_times_fresh(self):
        for report in (800, 1010, None):
            with self.subTest(report=report):
                d = self.sample(self.regulator(), 0, 20, 16, "charging",
                                charger_report_ts=report, charger_snapshot_verified=True)
                self.assertEqual(d.current_p1, 0)
                self.assertTrue(d.current_refresh_required)

    def test_lower_grid_alone_cannot_discard_old_house_envelope(self):
        r, d = self.waiting_offer(14.4)
        self.assertEqual(d.current_p1, 8)
        d = self.sample(r, 120, 2.6)
        self.assertAlmostEqual(d.house_load_l1, 14.4)
        self.assertEqual((d.available_l1, d.current_p1), (8, 8))
        self.assertEqual(d.house_load_basis, "positive_cap_house_envelope")
        self.assertTrue(d.current_refresh_required)

    def test_verified_snapshot_then_grid_rebases_house_without_erasing_late_start(self):
        r, _ = self.waiting_offer(14.4)
        baseline, original_receipt = r._positive_before_current, r._positive_ev_ts
        d = self.sample(r, 120, 2.6, charger_report_ts=1119,
                        charger_snapshot_verified=True, accept=False)
        self.assertEqual((d.current_p1, d.available_l1), (16, 16))
        self.assertAlmostEqual(d.house_load_l1, 2.6)
        self.assertEqual(r._positive_cap, 8)
        self.assertEqual((r._positive_before_current, r._positive_ev_ts), (baseline, original_receipt))

    def test_snapshot_without_later_grid_cannot_rebase_anchor(self):
        r, _ = self.waiting_offer(14.4)
        d = self.sample(r, 120, 2.6, grid_report_ts=1119,
                        charger_snapshot_verified=True)
        self.assertEqual(d.current_p1, 8)
        self.assertAlmostEqual(r._positive_house, 14.4)

    def test_delayed_full_start_keeps_ev_first_alignment_after_zero_snapshot(self):
        r, _ = self.waiting_offer()
        self.sample(r, 600, 2.6, charger_report_ts=1599, charger_snapshot_verified=True)
        self.assertEqual(r._positive_cap, 16)
        ev_first = self.sample(r, 601, 2.6, 16, "charging", grid_report_ts=1600)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("settling", 16))
        aligned = self.sample(r, 602, 18.6, 16, "charging", charger_report_ts=1601)
        self.assertEqual(aligned.current_p1, 16)
        self.assertIsNone(r._positive_cap)

    def test_rebase_preserves_spent_alignment_budget_and_partial_response_baseline(self):
        r, _ = self.waiting_offer(14.4)
        self.sample(r, 61, 14.4, 3, "charging", grid_report_ts=1060)
        self.sample(r, 63, 17.4, 3, "charging", charger_report_ts=1061)
        spent = r._positive_alignment_used_seconds
        before = r._positive_before_current
        self.assertGreater(spent, 0)
        d = self.sample(r, 120, 5, 3, "charging", charger_report_ts=1119,
                        charger_snapshot_verified=True, accept=False)
        self.assertEqual(d.current_p1, 16)
        self.assertEqual(r._positive_alignment_used_seconds, spent)
        self.assertEqual(r._positive_before_current, before)

    def test_active_alignment_is_not_rebased_by_snapshot(self):
        r, _ = self.waiting_offer(14.4)
        self.sample(r, 61, 14.4, 3, "charging", grid_report_ts=1060)
        d = self.sample(r, 62, 14.4, 3, "charging", grid_report_ts=1060,
                        charger_snapshot_verified=True)
        self.assertEqual((d.mode, d.current_p1), ("settling", 8))
        self.assertAlmostEqual(r._positive_house, 14.4)

    def expired_partial(self):
        r, _ = self.waiting_offer(14.4)
        self.sample(r, 61, 14.4, 3, "charging", grid_report_ts=1060)
        d = self.sample(r, 72, 5.6, 3, "charging", grid_report_ts=1060,
                        charger_report_ts=1061)
        self.assertEqual((d.mode, d.current_p1), ("waiting_for_aligned_sources", 8))
        return r

    def test_new_verified_read_and_later_grid_confirm_expired_partial_without_time_refund(self):
        r = self.expired_partial()
        before = r._positive_before_current
        d = self.sample(r, 80, 5.6, 3, "charging", charger_report_ts=1079,
                        charger_snapshot_verified=True, accept=False)
        self.assertEqual(d.current_p1, 16)
        self.assertAlmostEqual(d.house_load_l1, 2.6)
        self.assertEqual(r._positive_alignment_used_seconds, 19)
        self.assertEqual(r._positive_before_current, before)
        self.assertEqual(r._positive_cap, 8)

    def test_old_or_incoherent_snapshot_cannot_confirm_expired_partial(self):
        for extra in ({"charger_report_ts": 800},
                      {"grid_report_ts": 1078, "charger_report_ts": 1079},
                      {"grid": 1, "charger_report_ts": 1079}):
            with self.subTest(extra=extra):
                r = self.expired_partial()
                args = {"grid": 5.6, "ev": 3, "status": "charging",
                        "charger_snapshot_verified": True, "accept": False}
                args.update(extra)
                d = self.sample(r, 80, **args)
                self.assertLessEqual(d.current_p1, 8)
                self.assertNotEqual(d.house_load_basis, "verified_snapshot_then_grid")

    def test_verified_zero_after_running_current_tracks_a_late_partial_start_in_both_orders(self):
        for grid_first in (False, True):
            with self.subTest(grid_first=grid_first):
                r = self.regulator()
                self.assertEqual(self.sample(r, 0, 14, 10, "charging").current_p1, 16)
                self.sample(r, 60, 2.6, 0, charger_report_ts=1059,
                            charger_snapshot_verified=True)
                self.assertEqual(r._positive_before_current, 10)
                self.assertEqual(r._positive_verified_low_current, 0)
                if grid_first:
                    first = self.sample(r, 61, 10.6, 0, charger_report_ts=1059,
                                        charger_snapshot_verified=True)
                    self.assertGreaterEqual(first.current_p1, 6)
                    response = self.sample(r, 62, 10.6, 8, "charging")
                else:
                    response = self.sample(r, 61, 2.6, 8, "charging", grid_report_ts=1060)
                    self.assertEqual(response.mode, "settling")
                self.assertGreaterEqual(response.current_p1, 6)

    def test_pending_reduction_still_blocks_snapshot_recovery(self):
        r = self.regulator()
        self.sample(r, 0, 20, 16, "charging")
        self.sample(r, 5, 34, 16, "charging")
        d = self.sample(r, 10, 20, 16, "charging", charger_report_ts=1009,
                        charger_snapshot_verified=True)
        self.assertEqual(d.current_p1, 6)
        self.assertEqual(r._awaiting_reduction_target, 6)

    def test_new_verified_drop_after_partial_response_allows_distinct_rise_without_budget_refund(self):
        r = self.regulator()
        self.sample(r, 0, 14, 10, "charging")
        self.sample(r, 60, 2.6, 0, charger_report_ts=1059, charger_snapshot_verified=True)
        self.sample(r, 61, 2.6, 8, "charging", grid_report_ts=1060)
        self.sample(r, 62, 10.6, 8, "charging", charger_report_ts=1061)
        self.assertEqual(r._positive_alignment_used_seconds, 1)
        self.sample(r, 70, 6.6, 4, "charging", charger_report_ts=1069,
                    charger_snapshot_verified=True)
        self.assertEqual(r._positive_verified_low_current, 4)
        rise = self.sample(r, 71, 6.6, 8, "charging", grid_report_ts=1070)
        self.assertEqual((rise.mode, rise.current_p1), ("settling", 16))
        self.sample(r, 72, 10.6, 8, "charging", charger_report_ts=1071)
        self.assertEqual(r._positive_alignment_used_seconds, 2)
        self.assertEqual(r._positive_before_current, 10)

    def test_smaller_waiting_offer_keeps_memory_for_its_late_physical_start(self):
        r, _ = self.waiting_offer()
        lowered = self.sample(r, 65, 10)
        self.assertEqual(lowered.current_p1, 13)
        self.assertEqual(r._positive_cap, 13)
        self.sample(r, 66, 10, 0, charger_report_ts=1065,
                    charger_snapshot_verified=True)
        late = self.sample(r, 67, 10, 13, "charging", grid_report_ts=1066)
        self.assertEqual((late.mode, late.current_p1), ("settling", 13))

    def test_owned_pause_needs_fresh_current_status_before_resume_after_read_failure(self):
        r = self.regulator(resume_for_seconds=0)
        r.paused_by_us = True
        r._pause_observed = True
        r.last_target = (7, 0, 0, 60)
        r.last_set_limit_ts = 0
        for now in (0, 5):
            d = self.sample(r, now, 2.6, 0, "paused", wall_now_ts=2000 + now,
                            grid_report_ts=2000 + now, charger_report_ts=1000)
            self.assertEqual((d.mode, d.command, d.current_p1),
                             ("waiting_for_fresh_sources", "set_limit", 16))
            self.assertTrue(d.current_refresh_required)
            self.assertTrue(r.paused_by_us)
        verified = self.sample(r, 10, 2.6, 0, "paused", charger_snapshot_verified=True)
        self.assertEqual((verified.command, verified.current_p1), ("resume", 16))

    def test_invalid_data_and_manual_or_scheduled_waits_do_not_get_new_start_offer(self):
        d = self.sample(self.regulator(), 0, 2, data_valid=False,
                        charger_snapshot_verified=True)
        self.assertEqual((d.mode, d.current_p1), ("unavailable", 0))
        for status in ("paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(status=status):
                r = self.regulator()
                for now in (0, 60, 120):
                    d = self.sample(r, now, 2, 0, status, charger_snapshot_verified=True)
                    self.assertLessEqual(d.current_p1, 6)
                    self.assertNotEqual(d.command, "resume")


if __name__ == "__main__":
    unittest.main(verbosity=2)
