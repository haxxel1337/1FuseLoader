"""One fixed post-minimum phase across the low-grid route and pure engine.

All times, values and source identities are synthetic. These are policy
regressions, not a byte-for-byte replay of household history or hardware.
"""
from dataclasses import replace
from copy import deepcopy
import unittest

import test_1fuseloader_b19_alignment as fixtures
import test_1fuseloader_clock_provenance as clock_fixtures

logic = fixtures.logic
EPOCH = fixtures.fixtures.EPOCH


class PureMinimumConfirmationTests(unittest.TestCase):
    def start(self, **changes):
        values = dict(owner="low_grid", source_id=("synthetic", "source"), cap=7,
            now=19., accepted_at=10., response_at=19., response_deadline=20.,
            seconds=30, lease_expires=70., ev_ts=EPOCH+19., grid_ts=EPOCH+18., current=7.1)
        values.update(changes)
        return logic.MinimumGridConfirmation.start(**values)

    def observe(self, window, **changes):
        values = dict(now=20., grid=2.4, current=7.1, grid_ts=EPOCH+18.,
            ev_ts=EPOCH+19., ceiling=23., event_advanced=False,
            event_regressed=False, explicit_clock=True)
        values.update(changes)
        return window.observe(**values)

    def test_start_requires_response_strictly_after_ack_and_before_original_deadline(self):
        for changed in (dict(response_at=10.), dict(response_at=9.9),
                        dict(response_at=20.), dict(response_at=20.1),
                        dict(seconds=0), dict(seconds=31), dict(seconds=True),
                        dict(current=True), dict(now=float("nan"))):
            with self.subTest(changed=changed):
                self.assertIsNone(self.start(**changed))

    def test_one_deadline_is_first_response_plus_window_capped_by_accepted_lease(self):
        for lease, deadline in ((70., 49.), (40., 40.), (19.5, 19.5)):
            with self.subTest(lease=lease):
                window = self.start(lease_expires=lease)
                self.assertEqual(window.deadline, deadline)
        self.assertIsNone(self.start(lease_expires=19.))

    def test_observation_table_never_renews_first_deadline(self):
        window = self.start()
        for now, current, event, advanced in ((20., 7.1, 19., False),
                (21., 7.1, 21., True), (25., 7.12, 25., True),
                (35., 7.09, 35., True), (48.999, 7.1, 35., False)):
            with self.subTest(now=now):
                self.assertEqual(self.observe(window, now=now, current=current,
                    ev_ts=EPOCH+event, event_advanced=advanced), "wait")
                self.assertEqual(window.deadline, 49.)
                self.assertEqual(window.floor, EPOCH+19.)
        self.assertEqual(self.observe(window, now=49.), "timeout")

    def test_same_value_and_small_jitter_do_not_starve_later_grid(self):
        window = self.start()
        self.assertEqual(self.observe(window, now=21., current=7.12,
            ev_ts=EPOCH+21., event_advanced=True), "wait")
        self.assertEqual(self.observe(window, now=22., grid=20.5,
            grid_ts=EPOCH+20., current=7.12, ev_ts=EPOCH+21.), "complete")
        self.assertEqual(window.deadline, 49.)

    def test_substantive_new_value_needs_a_later_grid_without_refunding_time(self):
        window = self.start()
        self.assertEqual(self.observe(window, now=23., grid=20.5,
            grid_ts=EPOCH+22., current=7.3, ev_ts=EPOCH+23., event_advanced=True), "wait")
        self.assertEqual(window.floor, EPOCH+23.)
        self.assertEqual(self.observe(window, now=24.1, grid=20.5,
            grid_ts=EPOCH+24., current=7.3, ev_ts=EPOCH+23.), "complete")
        self.assertEqual(window.deadline, 49.)

    def test_changed_value_without_event_does_not_supply_a_new_response(self):
        for explicit, expected in ((True, "wait"), (False, "value_changed_without_event")):
            with self.subTest(explicit=explicit):
                window = self.start()
                self.assertEqual(self.observe(window, now=23., current=7.3,
                    ev_ts=EPOCH+19., explicit_clock=explicit), expected)
                self.assertEqual(window.floor, EPOCH+19.)
                self.assertEqual(window.deadline, 49.)

    def test_clock_regression_and_post_response_ceiling_are_independent_guards(self):
        for changes, expected in ((dict(event_regressed=True), "source_clock_regressed"),
                (dict(grid_ts=EPOCH+17.), "source_clock_regressed"),
                (dict(grid=23.001, grid_ts=EPOCH+20.), "outside_ceiling"),
                (dict(grid=6., current=7.4, grid_ts=EPOCH+20., event_advanced=True), "response_outside_ceiling")):
            with self.subTest(changes=changes):
                self.assertEqual(self.observe(self.start(), **changes), expected)


class SharedLowGridConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LowGridAlignmentTests()

    def pending(self, *, accept=True, **options):
        r = self.fixture.running(grid_confirmation_seconds=30, **options)
        decision = self.fixture.sample(r, 10., 2.4, 16.2, et=0., accept=accept)
        self.assertEqual((decision.mode, decision.current_p1), ("low_grid_alignment", 7))
        self.assertEqual(decision.nominal_response_deadline_ts, 20.)
        self.assertEqual(r.last_target[0], 7 if accept else 16)
        return r

    def waiting(self, **options):
        r = self.pending(**options)
        decision = self.fixture.sample(r, 19., 2.4, 7.1, gt=18., et=19.)
        self.assertEqual((decision.mode, decision.current_p1, decision.command),
                         ("low_grid_grid_wait", 7, None))
        self.assertFalse(decision.should_call_service)
        self.assertEqual(decision.grid_confirmation_deadline_ts, 49.)
        self.assertIsNone(decision.nominal_response_deadline_ts)
        self.assertIsInstance(r._minimum_confirmation, logic.MinimumGridConfirmation)
        return r, decision

    def assert_no_new_allowance(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(state["used_seconds"], 2000.)
        self.assertNotIn("trial_epoch", state)
        self.assertFalse(state["minimum_held"])
        self.assertFalse(state["transition_pending"])

    def test_old_response_deadline_cannot_zero_a_timely_minimum_awaiting_grid(self):
        r, first = self.waiting(resend_limit_every_seconds=1)
        accepted_at = r.last_set_limit_ts
        before = deepcopy(r.export_overload_state())
        for now in (20., 25., 35., 48.999):
            decision = self.fixture.sample(r, now, 2.4, 7.1, gt=18., et=19.)
            self.assertEqual((decision.current_p1, decision.command,
                              decision.service, decision.should_call_service), (7, None, None, False))
            self.assertEqual(decision.grid_confirmation_deadline_ts, first.grid_confirmation_deadline_ts)
            self.assertIsNone(decision.nominal_response_deadline_ts)
            self.assertEqual(r.last_set_limit_ts, accepted_at)
        self.assertEqual(r.export_overload_state()["used_seconds"], before["used_seconds"])
        self.assert_no_new_allowance(r)

    def test_later_coherent_grid_completes_same_accepted_seven(self):
        r, _ = self.waiting()
        decision = self.fixture.sample(r, 22., 20.5, 7.1, gt=21., et=19.)
        self.assertEqual((decision.mode, decision.current_p1), ("low_grid_alignment_complete", 7))
        self.assertIsNone(decision.grid_confirmation_deadline_ts)
        self.assertIsNone(r._minimum_confirmation)
        self.assertLessEqual(decision.house_load_l1+7, 23.)
        self.assert_no_new_allowance(r)

    def test_expired_fixed_window_is_terminal_even_when_low_grid_arrives_next(self):
        r, _ = self.waiting()
        stopped = self.fixture.sample(r, 49., 2.4, 7.1, gt=18., et=19., accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIn("grid_confirmation_timeout", stopped.reason)
        later = self.fixture.sample(r, 49.1, 12., 7.1, gt=49., et=19., accept=False)
        self.assertEqual(later.current_p1, 0)
        self.assertTrue(r._restart_required)

    def test_no_ack_get_or_missing_minimum_cannot_purchase_confirmation(self):
        for variant in ("no_ack", "get", "no_minimum", "late_minimum"):
            with self.subTest(variant=variant):
                r = self.pending(accept=variant != "no_ack")
                current = 16.2 if variant == "no_minimum" else 7.1
                event = 20. if variant == "late_minimum" else 19.
                now = 20. if variant == "late_minimum" else 19.
                decision = self.fixture.sample(r, now, 20.5, current, gt=18., et=event,
                    accept=False, charger_snapshot_verified=variant == "get")
                self.assertIsNone(decision.grid_confirmation_deadline_ts)
                stopped = self.fixture.sample(r, 20., 20.5, current, gt=18., et=event,
                    accept=False, charger_snapshot_verified=variant == "get")
                self.assertEqual(stopped.current_p1, 0)

    def test_genuine_timely_event_can_be_evaluated_after_old_deadline(self):
        r = self.pending()
        decision = self.fixture.sample(r, 21., 2.4, 7.1, gt=18., et=19.)
        self.assertEqual((decision.mode, decision.grid_confirmation_deadline_ts),
                         ("low_grid_grid_wait", 49.))
        self.assertFalse(decision.should_call_service)

    def test_phase_cannot_hide_source_fault_rebind_peak_or_later_high_grid(self):
        variants = ((2.4, dict(data_valid=False)), (2.4, dict(charger_current_trusted=False)),
                    (2.4, dict(identity=("changed_source",))), (38.001, dict(gt=18.)),
                    (23.001, dict(gt=20.)))
        for grid, options in variants:
            with self.subTest(grid=grid, options=options):
                r, _ = self.waiting()
                decision = self.fixture.sample(r, 21., grid, 7.1, et=19., accept=False, **options)
                self.assertEqual(decision.current_p1, 0)
                self.assertNotEqual(decision.mode, "low_grid_grid_wait")

    def test_expired_accepted_lease_cannot_be_renewed_by_the_phase(self):
        r, _ = self.waiting()
        r.last_set_limit_ts = -41.  # Explicitly invalidate the accepted lease; no new ACK exists.
        decision = self.fixture.sample(r, 20., 2.4, 7.1, gt=18., et=19., accept=False)
        self.assertEqual(decision.current_p1, 0)
        self.assertIsNone(decision.grid_confirmation_deadline_ts)

    def test_same_value_gets_and_jitter_never_rearm_first_phase(self):
        r, first = self.waiting()
        for now in (21., 25., 30., 40.):
            decision = self.fixture.sample(r, now, 2.4, 7.08, gt=18., et=now,
                charger_snapshot_verified=True)
            self.assertEqual(decision.grid_confirmation_deadline_ts, first.grid_confirmation_deadline_ts)
            self.assertFalse(decision.should_call_service)
        stopped = self.fixture.sample(r, 49., 2.4, 7.08, gt=18., et=40., accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_pre_ack_provider_bound_cannot_be_relabelled_as_timely_response(self):
        fixture = clock_fixtures.ClockProvenanceTests()
        r = fixture.explicit_running_sixteen()
        r.update_settings(replace(r.settings, grid_confirmation_seconds=30))
        lower = fixture.step(r, 110., 2.4, 16.2, et=102.)
        self.assertEqual((lower.mode, lower.current_p1), ("low_grid_alignment", 7))
        rejected = fixture.step(r, 119., 20.5, 7.1, et=119.,
            source_known=True, source_ts=109., accept=False)
        self.assertIsNone(rejected.grid_confirmation_deadline_ts)
        stopped = fixture.step(r, 120., 20.5, 7.1, et=119.,
            source_known=True, source_ts=109., accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_new_winner_without_ack_cohort_floor_cannot_supply_response_credit(self):
        fixture = clock_fixtures.ClockProvenanceTests()
        r = fixture.explicit_running_sixteen()
        r.update_settings(replace(r.settings, grid_confirmation_seconds=30))
        lower = fixture.step(r, 110., 2.4, 16.2, et=102.)
        self.assertEqual((lower.mode, lower.current_p1), ("low_grid_alignment", 7))
        # This fixture has only183 in the ACK's observed vector, with no
        # explicit all-terminal sequence0 bootstrap for182. A later maximum
        # on182 is not proof relative to an invented182 owner floor.
        changed = fixture.step(r, 119., 20.5, 7.1, et=119., winner=(182,),
            vector=((182, 119.),), accept=False)
        self.assertIsNone(changed.grid_confirmation_deadline_ts)
        stopped = fixture.step(r, 120., 20.5, 7.1, et=119., winner=(182,),
            vector=((182, 119.),), accept=False)
        self.assertEqual(stopped.current_p1, 0)


if __name__ == "__main__":
    unittest.main()
