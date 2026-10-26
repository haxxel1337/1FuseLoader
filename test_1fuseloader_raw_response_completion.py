"""Qualified raw-rise completion must precede the unconfirmed timeout branch.

Synthetic inputs only; no HA, charger or network calls. The live b28 stop had
different grid/EV ordering and is not claimed to be reproduced by these cases.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_raw_budget_recovery as fixtures


class RawResponseCompletionTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RawBudgetRecoveryTests()

    def prepared(self):
        regulator = self.helper.steady_six(max_charge_amps=12)
        proposal = self.helper.sample(regulator, 205, 10.5)
        self.assertEqual(proposal.current_p1, 12)
        self.assertEqual(regulator._positive_raw_deadline, 230)
        held = self.helper.sample(regulator, 230, 10.5)
        self.assertEqual(held.current_p1, 12)
        self.assertEqual(regulator._positive_raw_deadline, 230)
        return regulator

    def response(self, regulator, now=230.8, grid=16.5, current=12.05, **metadata):
        fields = dict(et=now-.2, gt=now-.1, sequence=3, accept=False)
        fields.update(metadata)
        return self.helper.sample(regulator, now, grid, current, **fields)

    def test_complete_response_after_independent_raw_hold_retires_same_owner(self):
        regulator = self.prepared()
        history = regulator.export_overload_state()
        accepted, accepted_at = regulator.last_target, regulator.last_set_limit_ts
        completed = self.response(regulator)
        self.assertEqual(completed.current_p1, 12)
        self.assertIsNone(regulator._positive_cap)
        self.assertIsNone(regulator._positive_raw_deadline)
        self.assertEqual((regulator.last_target, regulator.last_set_limit_ts), (accepted, accepted_at))
        self.assertEqual(regulator.export_overload_state(), history)
        self.assertFalse(regulator._restart_required)

    def test_fully_qualified_pair_at_exact_expiry_does_not_time_out(self):
        regulator = self.prepared()
        self.assertEqual(self.response(regulator, now=230).current_p1, 12)
        self.assertIsNone(regulator._positive_cap)
        self.assertIsNone(regulator._positive_raw_deadline)

    def test_grid_before_or_equal_to_ev_cannot_complete_expired_owner(self):
        for grid_at in (230.5, 230.6):
            with self.subTest(grid_at=grid_at):
                regulator = self.prepared()
                result = self.response(regulator, et=230.6, gt=grid_at)
                self.assertEqual(result.current_p1, 0)
                self.assertTrue(regulator._restart_required)

    def test_safe_minimum_handoff_transfers_unproven_higher_authority(self):
        for grid_at in (230.5, 230.6):
            with self.subTest(grid_at=grid_at):
                regulator = self.prepared()
                result = self.response(regulator, grid=15.1, et=230.6, gt=grid_at)
                self.assertEqual((result.mode, result.current_p1), ("normal_shedding", 7))
                self.assertLessEqual(result.grid_l1 + result.current_p1, 23)
                # This is a decrease proposal, not completion/forgetting of
                # the unproven 12 A offer or new time to continue at 12 A.
                self.assertEqual(regulator.last_target[0], 12)
                self.assertEqual(regulator._normal_shed_previous, 12)
                self.assertEqual(regulator._normal_shed_entry_cap, 12)
                self.assertEqual(regulator._uncertainty_previous_offer, 12)
                self.assertIsNone(regulator._normal_shed_accepted_at)
                self.assertIsNone(regulator._minimum_confirmation)

    def test_safe_handoff_requires_both_explicit_optins(self):
        for option in ("bounded_nominal_shedding", "nominal_import_only_fallback"):
            with self.subTest(option=option):
                regulator = self.prepared()
                setattr(regulator.settings, option, False)
                result = self.response(regulator, grid=15.1, et=230.6, gt=230.5)
                self.assertEqual(result.current_p1, 0)
                self.assertTrue(regulator._restart_required)
                self.assertIsNone(regulator._normal_shed_deadline)

    def test_unchanged_sequence_with_later_receipt_is_not_new_response(self):
        regulator = self.prepared()
        result = self.response(regulator, sequence=2)
        self.assertEqual(result.current_p1, 0)
        self.assertTrue(regulator._restart_required)

    def test_cached_get_does_not_manufacture_response_credit(self):
        regulator = self.prepared()
        result = self.response(regulator, et=1, sequence=2,
            charger_snapshot_verified=True, charger_event_source_known=True,
            charger_event_source_ts=fixtures.EPOCH+1)
        self.assertEqual(result.current_p1, 0)
        self.assertTrue(regulator._restart_required)

    def test_no_new_event_can_only_decrease_when_full_raw_minimum_fits(self):
        for metadata in (
            {"sequence": 2},
            {"et": 1, "sequence": 2, "charger_snapshot_verified": True,
             "charger_event_source_known": True, "charger_event_source_ts": fixtures.EPOCH+1},
        ):
            with self.subTest(metadata=metadata):
                regulator = self.prepared()
                result = self.response(regulator, grid=15.1, **metadata)
                self.assertEqual((result.mode, result.current_p1), ("normal_shedding", 7))
                self.assertLessEqual(result.grid_l1 + result.current_p1, 23)
                self.assertEqual(regulator.last_target[0], 12)
                self.assertEqual(regulator._normal_shed_previous, 12)
                self.assertIsNone(regulator._normal_shed_accepted_at)
                self.assertIsNone(regulator._minimum_confirmation)

    def test_expired_lease_blocks_even_fresh_coherent_pair(self):
        regulator = self.prepared()
        result = self.response(regulator, now=266, grid=15.1)
        self.assertEqual(result.current_p1, 0)
        self.assertTrue(regulator._restart_required)

    def test_changed_source_cannot_retire_prior_owner(self):
        regulator = self.prepared()
        result = self.response(regulator, grid=15.1, control_source_id="different-source")
        self.assertEqual(result.current_p1, 0)
        self.assertTrue(regulator._restart_required)

    def test_terminal_zero_cannot_be_revived_by_later_good_pair(self):
        regulator = self.prepared()
        stopped = self.helper.sample(regulator, 230.1, 16.5, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertTrue(regulator._restart_required)
        self.assertIsNone(regulator._positive_cap)
        result = self.response(regulator, now=231, grid=15.1)
        self.assertEqual(result.current_p1, 0)
        self.assertTrue(regulator._restart_required)

    def test_true_nominal_budget_excess_keeps_protective_reduction(self):
        regulator = self.prepared()
        result = self.response(regulator, grid=24)
        self.assertLess(result.current_p1, 12)
        self.assertNotEqual(result.current_p1, 16)


if __name__ == "__main__":
    unittest.main()
