"""Synthetic accepted-decrease ownership across a later high-grid report.

No installation history, network, HA API or physical charger is used.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_b22_episode_ownership as fixtures

logic = fixtures.logic
EPOCH = fixtures.EPOCH
SOURCE = fixtures.SOURCE


class ReductionOwnerTests(unittest.TestCase):
    def regulator(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, overload_enabled=True,
            bounded_nominal_shedding=True, nominal_import_only_fallback=True,
            measurement_settle_seconds=25, enable_soft_guard=False,
            enable_pause_resume=False))
        self.assertTrue(r.import_overload_state(dict(
            version=1, used_seconds=2400.0, exposure_active=False,
            transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        return r

    def step(self, r, now, grid, current, *, gt=None, et=None,
             source=SOURCE, accept=True, **metadata):
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

    def pending_fifteen(self):
        r = self.regulator()
        self.assertEqual(self.step(r, 0, 21.7, 16.2).current_p1, 16)
        lower = self.step(r, 10, 24.1, 16.2, et=0)
        self.assertEqual((lower.mode, lower.current_p1), ("normal", 15))
        self.assertEqual((r._awaiting_reduction_target, r._reduction_previous_cap), (15, 16))
        return r

    def assert_history(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2400.0)
        self.assertNotIn("trial_epoch", state)

    def test_previous_sixteen_is_not_an_impossible_current_after_fifteen_ack(self):
        for current in (16.2, 15.9):
            with self.subTest(current=current):
                r = self.pending_fifteen()
                d = self.step(r, 15, 26.0, current, et=0)
                self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
                self.assertEqual(d.nominal_response_deadline_ts, 25)
                self.assertEqual(r._normal_shed_previous, 16)
                held = self.step(r, 16, 26.0, current, et=0)
                self.assertEqual((held.mode, held.current_p1), ("normal_shedding", 7))
                self.assert_history(r)

    def test_seventy_five_to_eighty_second_unchanged_event_still_owns_previous_sixteen(self):
        for current in (16.2, 15.9):
            with self.subTest(current=current):
                r = self.regulator()
                self.assertEqual(self.step(r, 0, 21.7, current).current_p1, 16)
                self.step(r, 30, 21.7, current, et=0)
                self.step(r, 60, 21.7, current, et=0)
                lower = self.step(r, 75, current+7.8, current, et=0)
                self.assertEqual((lower.current_p1, lower.charger_report_age_seconds), (15, 75))
                d = self.step(r, 80, 26.0, current, et=0)
                self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
                self.assertEqual((d.charger_report_age_seconds, d.nominal_response_deadline_ts), (80, 90))
                self.assertEqual(r._normal_shed_previous, 16)
                response = self.step(r, 83, 18, 7.1, et=82)
                self.assertEqual((response.mode, response.current_p1), ("normal_shedding_complete", 7))
                self.assert_history(r)

    def test_prior_intermediate_decreases_keep_one_previous_offer_and_origin(self):
        r = self.pending_fifteen()
        lower = self.step(r, 15, 24.8, 16.2, et=0)
        self.assertEqual(lower.current_p1, 14)
        self.assertEqual(r._reduction_chain_origin, 10)
        self.assertEqual(r._reduction_previous_cap, 16)
        d = self.step(r, 20, 27.0, 16.2, et=0)
        self.assertEqual((d.current_p1, d.nominal_response_deadline_ts), (7, 30))
        self.assertEqual(r._normal_shed_previous, 16)
        for now in (21, 25, 29.9):
            held = self.step(r, now, 27.0, 16.2, et=0)
            self.assertEqual(held.current_p1, 7)
            self.assertEqual(held.nominal_response_deadline_ts, 30)
        stopped = self.step(r, 30, 27.0, 16.2, et=0)
        self.assertEqual(stopped.current_p1, 0)
        self.assert_history(r)

    def test_handoff_uses_the_original_chain_lifetime_if_it_expires_first(self):
        r = self.pending_fifteen()
        d = self.step(r, 30, 26.0, 16.2, et=0)
        self.assertEqual((d.mode, d.current_p1, d.nominal_response_deadline_ts),
                         ("normal_shedding", 7, 35))
        held = self.step(r, 34.9, 26.0, 16.2, et=0)
        self.assertEqual(held.nominal_response_deadline_ts, 35)
        self.assertEqual(self.step(r, 35, 26.0, 16.2, et=0).current_p1, 0)
        favorable = self.step(r, 36, 18, 7.1, et=34)
        self.assertEqual(favorable.current_p1, 0)
        self.assert_history(r)

    def test_distinct_post_ack_lower_response_retires_the_old_unresolved_offer(self):
        r = self.pending_fifteen()
        d = self.step(r, 40, 26.1, 15.2, et=39)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
        self.assertEqual(d.nominal_response_deadline_ts, 50)
        self.assertEqual(r._normal_shed_previous, 15)
        self.assert_history(r)

    def test_lease_renewal_cannot_move_the_original_decrease_origin(self):
        r = self.pending_fifteen()
        renewed = self.step(r, 40, 24.1, 16.2, et=0)
        self.assertEqual((renewed.current_p1, renewed.should_call_service), (15, True))
        self.assertEqual(r._reduction_chain_origin, 10)
        d = self.step(r, 41, 26.0, 16.2, et=0)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("inherited_response_window_expired", d.reason)
        self.assert_history(r)

    def test_delayed_high_grid_sample_spends_the_same_ten_second_window(self):
        r = self.pending_fifteen()
        d = self.step(r, 20, 26.0, 16.2, gt=15, et=0)
        self.assertEqual((d.current_p1, d.nominal_response_deadline_ts), (7, 25))
        self.assertEqual(self.step(r, 25, 26.0, 16.2, et=0).current_p1, 0)

    def test_pre_ack_low_event_cannot_complete_the_lower_seven_response(self):
        r = self.pending_fifteen()
        proposed = self.step(r, 15, 26.0, 16.2, et=0, accept=False)
        r.acknowledge(proposed, 16)
        mixed = self.step(r, 17, 18, 7.1, et=15.5)
        self.assertEqual((mixed.mode, mixed.current_p1), ("normal_shedding", 7))
        self.assertEqual(mixed.nominal_response_deadline_ts, 25)
        ev_first = self.step(r, 18, 26.0, 7.1, gt=17)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("normal_shedding", 7))
        complete = self.step(r, 19, 18, 7.1, et=18)
        self.assertEqual((complete.mode, complete.current_p1), ("normal_shedding_complete", 7))
        self.assertLessEqual(complete.house_load_l1 + 7, 23)
        self.assert_history(r)

    def test_late_ack_cannot_reopen_the_terminal_handoff(self):
        r = self.pending_fifteen()
        old = self.step(r, 15, 26.0, 16.2, et=0, accept=False)
        self.assertEqual(self.step(r, 25, 26.0, 16.2, et=0).current_p1, 0)
        r.acknowledge(old, 26)
        d = self.step(r, 27, 18, 7.1, et=26)
        self.assertEqual(d.current_p1, 0)
        self.assert_history(r)

    def test_previous_offer_must_be_real_not_inferred_from_a_high_current(self):
        r = self.regulator()
        self.assertEqual(self.step(r, 0, 23.8, 15.9).current_p1, 15)
        self.assertIsNone(r._reduction_previous_cap)
        d = self.step(r, 5, 26.0, 15.9, et=0)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("source_or_prior_control_invalid", d.reason)

    def test_entry_retains_lease_identity_trust_and_previous_current_bounds(self):
        cases = ((71, {}), (15, {"source": ("other-charger",)}),
                 (15, {"charger_current_trusted": False}),
                 (15, {"data_valid": False}), (15, {"grid_report_ts": None}),
                 (15, {"charger_report_ts": None}),
                 (15, {"wall_now_ts": None}), (15, {"current": 16.501}))
        for now, metadata in cases:
            with self.subTest(now=now, metadata=metadata):
                r = self.pending_fifteen()
                metadata = dict(metadata)
                current = metadata.pop("current", 16.2)
                d = self.step(r, now, 26.0, current, et=0, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.mode, "normal_shedding")
                self.assert_history(r)

    def test_rebound_or_clock_regression_cannot_keep_the_handoff_owner(self):
        for metadata in ({"source": ("other-charger",)}, {"et": -1}, {"gt": 15}):
            with self.subTest(metadata=metadata):
                r = self.pending_fifteen()
                self.assertEqual(self.step(r, 15, 26.0, 16.2, et=0).current_p1, 7)
                self.step(r, 16, 26.0, 16.2, et=0)
                args = dict(et=0)
                args.update(metadata)
                d = self.step(r, 17, 26.0, 16.2, **args)
                self.assertEqual(d.current_p1, 0)
                self.assert_history(r)

    def test_real_peak_source_failure_and_old_report_do_not_gain_a_handoff(self):
        for now, grid, metadata in ((15, 38.001, {}),
                                   (15, 26.0, {"data_error": "source_failed", "data_valid": False}),
                                   (150, 26.0, {"gt": 10})):
            with self.subTest(now=now, grid=grid):
                r = self.pending_fifteen()
                d = self.step(r, now, grid, 16.2, et=0, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assert_history(r)

    def test_restored_handoff_does_not_create_another_response_window(self):
        r = self.pending_fifteen()
        self.step(r, 15, 26.0, 16.2, et=0)
        state = r.export_overload_state()
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        d = self.step(restored, 16, 18, 7.1, et=15)
        self.assertEqual(d.current_p1, 0)
        self.assert_history(restored)


if __name__ == "__main__":
    unittest.main()
