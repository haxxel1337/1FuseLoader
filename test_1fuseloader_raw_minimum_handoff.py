"""An expired raw rise may only hand off to a distinct buffered minimum.

All inputs use explicit synthetic native receiver clocks/counters. No HA,
charger, network, production files or historical allowance is modified.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest

import test_1fuseloader_raw_budget_recovery as fixtures


class RawMinimumHandoffTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.RawBudgetRecoveryTests()

    def prepared(self, **options):
        settings = dict(grid_confirmation_seconds=30)
        settings.update(options)
        regulator = self.helper.steady_six(**settings)
        proposed = self.helper.sample(regulator, 205, 9.2)
        self.assertEqual(proposed.current_p1, 13)
        self.assertEqual(regulator._positive_raw_deadline, 230)
        held = self.helper.sample(regulator, 230, 9.2)
        self.assertEqual(held.current_p1, 13)
        self.assertEqual(regulator._positive_raw_deadline, 230)
        return regulator

    def handoff(self, regulator, *, accept=True, now=230.8, **metadata):
        fields = dict(grid=15.1, current=12.989, et=230.6, gt=230.0,
                      sequence=3, accept=accept)
        fields.update(metadata)
        return self.helper.sample(regulator, now, **fields)

    def minimum(self, regulator):
        self.handoff(regulator)
        response = self.helper.sample(regulator, 232.2, 15.1, 7.1,
            et=232, gt=230, sequence=4, accept=False)
        self.assertEqual((response.mode, response.current_p1), ("normal_shedding_grid_wait", 7))
        return response

    def test_expired_thirteen_hands_only_seven_to_fresh_raw_buffered_phase(self):
        regulator = self.prepared()
        history = deepcopy(regulator.export_overload_state())
        proposed = self.handoff(regulator, accept=False)
        self.assertEqual((proposed.mode, proposed.command, proposed.current_p1),
                         ("normal_shedding", "set_limit", 7))
        self.assertLessEqual(proposed.grid_l1 + proposed.current_p1, 23)
        self.assertEqual(proposed.house_load_l1, proposed.grid_l1)
        self.assertAlmostEqual(proposed.nominal_response_deadline_ts, 240, places=6)
        self.assertIsNone(proposed.raw_budget_response_deadline_ts)
        self.assertIsNone(regulator._positive_cap)
        self.assertEqual(regulator.last_target[0], 13)
        self.assertEqual(regulator._normal_shed_previous, 13)
        self.assertEqual(regulator._normal_shed_entry_cap, 13)
        self.assertEqual(regulator._normal_shed_source_id, fixtures.SOURCE)
        self.assertEqual(regulator.export_overload_state()["used_seconds"], history["used_seconds"])
        self.assertEqual(regulator.export_overload_state()["failure_reason"], history["failure_reason"])

    def test_timely_seven_then_shared_thirty_then_later_grid_completes(self):
        regulator = self.prepared()
        self.minimum(regulator)
        accepted_at = regulator.last_set_limit_ts
        deadline = regulator._minimum_confirmation.deadline
        self.assertAlmostEqual(deadline, 262, places=6)
        self.assertAlmostEqual(regulator._normal_shed_deadline, 240, places=6)
        waiting = self.helper.sample(regulator, 245, 15.1, 7.1,
            et=232, gt=230, sequence=4, accept=False)
        self.assertEqual((waiting.mode, waiting.current_p1, waiting.command),
                         ("normal_shedding_grid_wait", 7, None))
        self.assertFalse(waiting.should_call_service)
        self.assertEqual(waiting.grid_confirmation_deadline_ts, deadline)
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        completed = self.helper.sample(regulator, 250, 10.0, 7.1,
            et=232, gt=249, sequence=4, accept=False)
        self.assertEqual((completed.mode, completed.current_p1), ("normal_shedding_complete", 7))
        self.assertIsNone(regulator._minimum_confirmation)
        self.assertIsNone(regulator._normal_shed_deadline)
        self.assertIsNone(regulator._awaiting_reduction_target)
        self.assertEqual(regulator.last_target[0], 7)

    def test_old_low_grid_handler_does_not_replace_pending_raw_handoff(self):
        regulator = self.prepared()
        proposed = self.handoff(regulator, grid=11.5, current=12.989, accept=False)
        self.assertEqual(proposed.mode, "normal_shedding")
        self.assertEqual(proposed.current_p1, 7)
        self.assertFalse(regulator._low_grid_pending)
        self.assertEqual(regulator._normal_shed_previous, 13)

    def test_latest_grid_sample_age_spends_the_new_minimum_deadline(self):
        regulator = self.prepared()
        first = self.handoff(regulator, gt=221, accept=False)
        self.assertAlmostEqual(first.nominal_response_deadline_ts, 231, places=6)
        repeated = self.handoff(regulator, now=230.9, gt=221, accept=False)
        self.assertEqual(repeated.nominal_response_deadline_ts, first.nominal_response_deadline_ts)
        stopped = self.handoff(regulator, now=first.nominal_response_deadline_ts, gt=221, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(regulator._minimum_confirmation)

    def test_checkpoint_aborts_and_new_reports_do_not_restart_minimum_clock(self):
        regulator = self.prepared()
        first = self.handoff(regulator, accept=False)
        self.assertAlmostEqual(first.nominal_response_deadline_ts, 240, places=6)
        for at in (233, 236, 239):
            # This pair does NOT independently prove the still accepted13A
            # cap fits normal headroom. The unsent minimum keeps its clock.
            attempted = self.handoff(regulator, now=at, grid=22., current=5., gt=at-.1, et=at-.2,
                                     sequence=4+int(at), accept=False)
            self.assertEqual(attempted.current_p1, 7)
            self.assertEqual(attempted.nominal_response_deadline_ts, first.nominal_response_deadline_ts)
            self.assertEqual(regulator.last_target[0], 13)
        stopped = self.handoff(regulator, now=first.nominal_response_deadline_ts, grid=22., current=5., et=239.8, gt=239.9,
                               sequence=300, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertTrue(regulator._restart_required)
        self.assertIsNone(regulator._minimum_confirmation)

    def test_no_ack_does_not_credit_a_low_ev_observation_as_minimum_response(self):
        regulator = self.prepared()
        self.handoff(regulator, accept=False)
        reported = self.helper.sample(regulator, 235, 15.1, 7.1,
            et=234, gt=233, sequence=4, accept=False)
        self.assertEqual(reported.current_p1, 7)
        self.assertIsNone(regulator._normal_shed_accepted_at)
        self.assertIsNone(regulator._minimum_confirmation)
        # A later grid can prove the OLD actual13A normal offer independently;
        # it never claims that the unsent7A command was accepted or responded.
        normal = self.helper.sample(regulator, regulator._normal_shed_deadline, 15.1, 7.1,
            et=234, gt=239, sequence=4, accept=False)
        self.assertEqual((normal.mode, normal.current_p1, normal.command), ("normal_continuity", 13, None))
        self.assertEqual(regulator.last_target[0], 13)
        self.assertIsNone(regulator._normal_shed_accepted_at)
        self.assertIsNone(regulator._minimum_confirmation)
        regulator = self.prepared()
        first = self.handoff(regulator, accept=False)
        self.helper.sample(regulator, 235, 15.1, 7.1, et=234, gt=233, sequence=4, accept=False)
        stopped = self.helper.sample(regulator, first.nominal_response_deadline_ts, 15.1, 7.1,
            et=234, gt=233, sequence=4, accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_late_ack_cannot_purchase_another_minimum_response_window(self):
        regulator = self.prepared()
        proposed = self.handoff(regulator, accept=False)
        regulator.acknowledge(proposed, 241)
        self.assertEqual(regulator.last_target[0], 7)
        stopped = self.helper.sample(regulator, 241.2, 15.1, 7.1,
            et=241.1, gt=241, sequence=4, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(regulator._minimum_confirmation)

    def test_unsafe_raw_plus_seven_does_not_open_a_handoff(self):
        regulator = self.prepared()
        result = self.handoff(regulator, grid=16.01, accept=False)
        self.assertEqual(result.current_p1, 0)
        self.assertIsNone(regulator._normal_shed_deadline)

    def test_invalid_source_and_expired_lease_do_not_open_a_handoff(self):
        for changes in (
            {"data_valid": False, "data_error": "source_fault"},
            {"charger_current_trusted": False},
            {"control_source_id": "other-source"},
            {"now": 266, "et": 265.8, "gt": 265.9},
        ):
            with self.subTest(changes=changes):
                regulator = self.prepared()
                result = self.handoff(regulator, accept=False, **changes)
                self.assertEqual(result.current_p1, 0)
                self.assertIsNone(regulator._normal_shed_deadline)

    def test_disabled_optins_do_not_open_a_handoff(self):
        for key in ("bounded_nominal_shedding", "nominal_import_only_fallback", "overload_enabled"):
            with self.subTest(option=key):
                regulator = self.prepared()
                # Inject guard configuration without clearing the already
                # captured raw owner through the ordinary reconfigure path.
                setattr(regulator.settings, key, False)
                result = self.handoff(regulator, accept=False)
                self.assertEqual(result.current_p1, 0)
                self.assertIsNone(regulator._normal_shed_deadline)

    def test_explicit_epoch_cannot_borrow_a_distinct_legacy_minimum_trial(self):
        regulator = self.prepared()
        used = regulator.export_overload_state()["used_seconds"]
        regulator._trial_epoch = dict(version=1, trial_id="closed-test-period",
            started_at_utc=datetime.now(timezone.utc).isoformat(), source_id=fixtures.SOURCE,
            policy=regulator._trial_policy(regulator.settings), baseline_used_seconds=used,
            state="failed", failure_reason="response_timeout", closed_used_seconds=0.)
        regulator._trial_activation_committed = False
        result = self.handoff(regulator, accept=False)
        self.assertEqual(result.current_p1, 0)
        self.assertIsNone(regulator._normal_shed_deadline)


if __name__ == "__main__":
    unittest.main()
