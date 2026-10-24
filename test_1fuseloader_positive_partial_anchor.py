"""Synthetic partial positive-response alignment; no device or HA access.

An accepted cap is API authority, never proof of delivered current. All frames
use virtual clocks and fabricated normal-load observations.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_b22_episode_ownership as fixtures

logic = fixtures.logic
EPOCH = fixtures.EPOCH
SOURCE = fixtures.SOURCE


class PositivePartialAnchorTests(unittest.TestCase):
    def regulator(self, **options):
        values = dict(active_control=True, dry_run=False, overload_enabled=True,
                      nominal_import_only_fallback=True, bounded_nominal_shedding=True,
                      measurement_settle_seconds=25, enable_soft_guard=False,
                      enable_pause_resume=False)
        values.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**values))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=2400.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        return r

    def step(self, r, now, grid, current, *, gt=None, et=None, source=SOURCE,
             accept=True, **metadata):
        values = dict(now_ts=now, status="charging", grid_l1=grid,
            charger_current=current, wall_now_ts=EPOCH+now,
            grid_report_ts=EPOCH+(now if gt is None else gt),
            charger_report_ts=EPOCH+(now if et is None else et),
            control_source_id=source)
        values.update(metadata)
        d = r.evaluate(logic.RegulatorInput(**values))
        if accept and d.should_call_service:
            r.acknowledge(d, now)
        return d

    def rising_twelve(self, **options):
        r = self.regulator(**options)
        self.assertEqual(self.step(r, 0, 23., 7.1).current_p1, 7)
        rise = self.step(r, 10, 17.4, 7.1, gt=6, et=5)
        self.assertEqual((rise.mode, rise.current_p1), ("normal", 12))
        self.assertAlmostEqual(r._positive_house, 10.3)
        return r

    def assert_historical_allowance(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2400.)
        self.assertNotIn("trial_epoch", state)

    def test_ev_first_partial_response_does_not_create_house_load_from_mixed_pair(self):
        for excess in (.001, .031, .49):
            with self.subTest(excess=excess):
                r = self.rising_twelve()
                current = 21.9+12-(23+excess)
                d = self.step(r, 27, 21.9, current, gt=26, et=26.142)
                self.assertEqual((d.mode, d.current_p1), ("settling", 12))
                self.assertFalse(d.should_call_service)
                self.assertAlmostEqual(d.house_load_l1, 10.3)
                self.assertEqual(r.last_target[0], 12)
                self.assert_historical_allowance(r)

    def test_later_coherent_grid_recomputes_house_and_lowers_normally(self):
        r = self.rising_twelve()
        self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        later = self.step(r, 28, 22.0, 10.869, gt=27.5, et=26.142)
        self.assertEqual((later.mode, later.current_p1), ("normal", 11))
        self.assertNotEqual(later.house_load_basis, "prior_coherent_house_ev_first_hold")
        self.assertGreater(later.house_load_l1, 10.3)
        self.assert_historical_allowance(r)

    def test_new_ev_receipt_does_not_hide_grid_after_the_first_response(self):
        r = self.rising_twelve()
        self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        first = r._positive_anchor_response_ts
        later = self.step(r, 28, 22.7, 11.4, gt=27.5, et=27.6)
        self.assertNotEqual(later.house_load_basis, "prior_coherent_house_ev_first_hold")
        self.assertLess(later.current_p1, 12)
        # A distinct EV rise advanced its latest event, but cannot move the
        # first-response floor to hide this real later grid observation.
        self.assertEqual(first, EPOCH+26.142)
        self.assert_historical_allowance(r)

    def test_repeated_partial_events_and_ttl_ack_cannot_buy_more_time(self):
        r = self.rising_twelve()
        first = self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        self.assertEqual(first.measurement_alignment_remaining_seconds, 25)
        for now, current, event in ((29, 11.4, 28), (40, 11.4, 28), (49, 11.8, 48)):
            d = self.step(r, now, 21.9, current, gt=26, et=event)
            self.assertEqual(d.current_p1, 12)
            self.assertAlmostEqual(d.measurement_alignment_remaining_seconds, 52-now)
            self.assertEqual(r._positive_anchor_response_ts, EPOCH+26.142)
        expired = self.step(r, 52, 21.9, 11.8, gt=26, et=49)
        self.assertEqual(expired.current_p1, 0)
        self.assertIn("alignment timeout", expired.reason)
        self.assert_historical_allowance(r)

    def test_anchor_hold_cannot_outlive_the_unrenewed_accepted_lease(self):
        r = self.rising_twelve()
        held = self.step(r, 65, 21.9, 10.869, gt=64, et=64.142, accept=False)
        self.assertEqual(held.current_p1, 12)
        self.assertEqual(r.last_set_limit_ts, 10)
        expired = self.step(r, 70, 21.9, 10.869, gt=64, et=64.142)
        self.assertEqual(expired.current_p1, 0)
        self.assertNotEqual(expired.house_load_basis, "prior_coherent_house_ev_first_hold")

    def test_grid_above_normal_or_prior_full_offer_cannot_use_anchor(self):
        for grid in (22.8001, 23.0001, 25.0001, 38.0001):
            with self.subTest(grid=grid):
                r = self.rising_twelve()
                d = self.step(r, 27, grid, 10.869, gt=26, et=26.142)
                self.assertNotEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")
                self.assertLess(d.current_p1, 12)
                self.assert_historical_allowance(r)

    def test_raw_prior_full_offer_boundary_remains_bounded(self):
        r = self.rising_twelve()
        boundary = r._positive_house+12+.5
        d = self.step(r, 27, boundary, 10.869, gt=26, et=26.142)
        self.assertEqual((d.mode, d.current_p1), ("settling", 12))
        self.assertEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")

    def test_untrusted_missing_or_rebound_sources_do_not_get_anchor_hold(self):
        for metadata in ({"charger_current_trusted": False}, {"data_valid": False},
                         {"source": ("other-charger",)}, {"grid_report_ts": None},
                         {"charger_report_ts": None}, {"wall_now_ts": None},
                         {"charger_report_ts": EPOCH+30}):
            with self.subTest(metadata=metadata):
                r = self.rising_twelve()
                d = self.step(r, 27, 21.9, 10.869, gt=26, et=26.142, **metadata)
                # Existing nominal protection can choose a conservative
                # decrease when its EV timestamp is missing; it must never
                # receive this source-qualified anchor exception.
                self.assertLess(d.current_p1, 12)
                self.assertNotEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")
                self.assert_historical_allowance(r)

    def test_same_domain_clock_regression_still_terminates_anchor_hold(self):
        for stamps in ({"gt": 25.9, "et": 26.142}, {"gt": 26, "et": 26.1}):
            with self.subTest(stamps=stamps):
                r = self.rising_twelve()
                self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
                d = self.step(r, 28, 21.9, 10.869, **stamps)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")

    def test_waiting_pause_completed_and_disconnect_do_not_get_rise_hold(self):
        for status in ("awaiting_start", "ready_to_charge", "paused", "completed", "disconnected"):
            with self.subTest(status=status):
                r = self.rising_twelve()
                d = self.step(r, 27, 21.9, 10.869, gt=26, et=26.142, status=status)
                self.assertNotEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")
                self.assertNotEqual(d.command, "resume")
                self.assertLessEqual(d.current_p1, 12)

    def test_explicit_installation_contract_is_required(self):
        r = self.rising_twelve(nominal_import_only_fallback=False)
        d = self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(d.house_load_basis, "prior_coherent_house_ev_first_hold")

    def test_reported_excess_or_reversed_response_is_not_hidden_by_anchor(self):
        r = self.rising_twelve()
        d = self.step(r, 27, 21.9, 12.501, gt=26, et=26.142)
        self.assertEqual(d.current_p1, 0)
        self.assert_historical_allowance(r)
        r = self.rising_twelve()
        self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        reversed_response = self.step(r, 28, 21.9, 10.0, gt=26, et=27)
        self.assertNotEqual(reversed_response.house_load_basis, "prior_coherent_house_ev_first_hold")
        self.assertLess(reversed_response.current_p1, 12)

    def test_late_positive_ack_does_not_reverse_the_expired_episode(self):
        r = self.rising_twelve()
        self.step(r, 27, 21.9, 10.869, gt=26, et=26.142)
        renewal = self.step(r, 40, 21.9, 10.869, gt=26, et=26.142, accept=False)
        self.assertTrue(renewal.should_call_service)
        self.assertEqual(self.step(r, 52, 21.9, 10.869, gt=26, et=26.142).current_p1, 0)
        r.acknowledge(renewal, 53)
        later = self.step(r, 54, 18, .02, status="awaiting_start")
        self.assertEqual(later.current_p1, 0)
        self.assertNotEqual(later.house_load_basis, "prior_coherent_house_ev_first_hold")
        self.assert_historical_allowance(r)


if __name__ == "__main__":
    unittest.main()
