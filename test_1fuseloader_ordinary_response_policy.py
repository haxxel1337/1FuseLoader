"""Synthetic ordinary observation policy, distinct from actual peak response.

No Home Assistant, fileshares, network calls or household replay is involved.
The native-commit and event clocks below are explicitly supplied test evidence.
"""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_clock_provenance as clocks

logic = clocks.logic


class OrdinaryResponsePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = clocks.ClockProvenanceTests()

    def running(self, **settings):
        r = self.fixture.explicit_running_sixteen()
        settings.setdefault("peak_only_response", True)
        r.update_settings(replace(r.settings, grid_confirmation_seconds=30, **settings))
        return r

    def frame(self, r, now, grid, current, **metadata):
        metadata.setdefault("charger_native_committed", True)
        return self.fixture.step(r, now, grid, current, **metadata)

    def lower(self, grid=15.):
        r = self.running()
        d = self.frame(r, 110., grid, 16.2, et=102.)
        self.assertEqual((d.mode, d.current_p1), ("ordinary_lower_offer", 7))
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r._ordinary_lower.previous, 16)
        self.assertEqual(r._ordinary_lower.accepted_at, 110.)
        return r, d

    def waiting(self):
        r, _ = self.lower()
        d = self.frame(r, 119., 15., 7.1, gt=118., et=119.)
        self.assertEqual((d.mode, d.current_p1, d.command), ("ordinary_grid_wait", 7, None))
        self.assertEqual(d.grid_confirmation_deadline_ts, 149.)
        self.assertIsNone(d.nominal_response_deadline_ts)
        return r, d

    def assert_history(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(state["used_seconds"], 2400.)
        self.assertNotIn("trial_epoch", state)
        self.assertFalse(state["minimum_held"])
        self.assertFalse(state["transition_pending"])

    def test_raw_bounded_lower_offer_survives_ten_and_twentyfive_seconds(self):
        for grid in (9.2, 13.7, 15.):
            with self.subTest(grid=grid):
                r, initial = self.lower(grid)
                ctx = r._ordinary_lower
                self.assertEqual(initial.house_load_l1, grid)
                self.assertEqual(initial.house_load_basis, "raw_bounded_ordinary_observation")
                self.assertIsNone(initial.nominal_response_deadline_ts)
                for now in (121., 136., 159., 180.):
                    d = self.frame(r, now, grid, 16.2, et=102.)
                    self.assertEqual(d.current_p1, 7)
                    if now < ctx.observation_deadline:
                        self.assertEqual((d.mode, d.command, d.should_call_service),
                            ("ordinary_response_hold", None, False))
                    else:
                        self.assertEqual(d.mode, "ordinary_lower_offer")
                    self.assertIsNone(d.nominal_response_deadline_ts)
                    self.assertIsNone(d.grid_confirmation_deadline_ts)
                    self.assertIs(r._ordinary_lower, ctx)
                    self.assertEqual(ctx.accepted_at, 110.)
                    self.assertEqual(ctx.previous, 16)
                    self.assertLessEqual(d.grid_l1+7, 23.)
                self.assert_history(r)

    def test_whole_raw_normal_offer_needs_no_lower_handoff(self):
        r = self.running()
        original_ev_floor = r._accepted_control_ev_ts
        for now in (110., 121., 136., 159., 180.):
            d = self.frame(r, now, 2.4, 16.2, et=102.)
            self.assertEqual((d.current_p1, r.last_target[0]), (16, 16))
            self.assertLessEqual(d.grid_l1+16, 23.)
            self.assertEqual(r._accepted_control_ev_ts, original_ev_floor)
            self.assertIsNone(r._ordinary_lower)
            self.assertIsNone(d.nominal_response_deadline_ts)
            self.assertIsNone(d.grid_confirmation_deadline_ts)
            self.assertLess(now, r.last_set_limit_ts+r.last_target[3])
        self.assert_history(r)

    def test_unaccepted_lower_proposal_does_not_become_an_accepted_seven(self):
        r = self.running()
        d = self.frame(r, 110., 15., 16.2, et=102., accept=False)
        self.assertEqual((d.mode, d.current_p1), ("ordinary_lower_offer", 7))
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._ordinary_lower.accepted_at)
        later = self.frame(r, 111., 15., 7.1, et=111., accept=False)
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(later.grid_confirmation_deadline_ts)
        self.assertNotEqual(later.mode, "ordinary_lower_complete")

    def test_genuine_lower_response_enters_only_fixed_no_write_confirmation(self):
        r, first = self.waiting()
        accepted = r.last_set_limit_ts
        for now in (120., 125., 140., 148.999):
            d = self.frame(r, now, 15., 7.1, gt=118., et=119.)
            self.assertEqual((d.current_p1, d.command, d.service, d.should_call_service),
                             (7, None, None, False))
            self.assertEqual(d.grid_confirmation_deadline_ts, first.grid_confirmation_deadline_ts)
            self.assertIsNone(d.nominal_response_deadline_ts)
            self.assertEqual(r.last_set_limit_ts, accepted)
        self.assert_history(r)

    def test_later_grid_retires_higher_authority_and_allows_normal_recovery(self):
        r, _ = self.waiting()
        done = self.frame(r, 121., 12., 7.1, gt=120., et=119.)
        self.assertEqual((done.mode, done.current_p1), ("ordinary_lower_complete", 7))
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._minimum_confirmation)
        self.assertIsNone(r._uncertainty_ceiling)
        self.assertIsNone(r._uncertainty_previous_offer)
        recovered = self.frame(r, 122., 12., 7.1, et=119.)
        self.assertEqual(recovered.current_p1, 16)
        self.assert_history(r)

    def test_expired_observation_keeps_raw_safe_seven_without_a_second_window(self):
        r, _ = self.waiting()
        expired = self.frame(r, 149., 15., 7.1, gt=118., et=119.)
        self.assertEqual((expired.mode, expired.current_p1), ("ordinary_lower_offer", 7))
        self.assertTrue(r._ordinary_lower.confirmation_expired)
        self.assertIsNone(expired.grid_confirmation_deadline_ts)
        for now in (150., 155., 180.):
            d = self.frame(r, now, 15., 7.08, gt=now-1, et=now)
            self.assertEqual(d.current_p1, 7)
            self.assertIsNone(d.grid_confirmation_deadline_ts)
            self.assertTrue(r._ordinary_lower.confirmation_expired)
        self.assert_history(r)

    def test_get_cannot_supply_post_ack_minimum_proof(self):
        r, _ = self.lower()
        d = self.frame(r, 119., 15., 7.1, gt=118., et=119., charger_snapshot_verified=True)
        self.assertEqual((d.mode, d.current_p1), ("ordinary_lower_offer", 7))
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertIsNone(r._ordinary_lower.first_response)

    def test_checkpoint_exposes_pending_ordinary_authority_without_resetting_history(self):
        r, _ = self.lower()
        state = deepcopy(r.export_overload_state())
        self.assertTrue(state["normal_control"]["response_pending"])
        self.assertEqual(state["normal_control"]["cap"], 7)
        self.assertEqual(state["normal_control"]["previous_offer"], 16)
        self.assertEqual(state["normal_control"]["stops"], 0)
        self.assert_history(r)

    def test_opt_out_or_uncommitted_native_source_does_not_enter_new_policy(self):
        for metadata, settings in ((dict(charger_native_committed=False), {}),
                ({}, dict(nominal_import_only_fallback=False)),
                ({}, dict(bounded_nominal_shedding=False)),
                ({}, dict(peak_only_response=False))):
            with self.subTest(metadata=metadata, settings=settings):
                r = self.running(**settings)
                d = self.frame(r, 110., 15., 16.2, et=102., **metadata)
                self.assertIsNone(r._ordinary_lower)
                self.assertNotEqual(d.mode, "ordinary_lower_offer")

    def test_source_fault_expired_lease_or_terminal_stop_still_protects(self):
        for kind in ("fault", "untrusted", "expired_lease", "rebound"):
            with self.subTest(kind=kind):
                r, _ = self.lower()
                extra = {}
                if kind == "fault":
                    extra.update(data_valid=False, data_error="synthetic_source_fault")
                elif kind == "untrusted":
                    extra["charger_current_trusted"] = False
                elif kind == "rebound":
                    extra["control_source_id"] = "other_verified_source"
                else:
                    r.last_set_limit_ts = 50.  # Accepted60s lease is already expired at111.
                stopped = self.frame(r, 111., 15., 16.2, et=102., accept=False, **extra)
                self.assertEqual(stopped.current_p1, 0)
                self.assertNotEqual(stopped.mode, "ordinary_lower_offer")

    def test_real_peak_is_not_covered_by_an_ordinary_observation_clock(self):
        r, _ = self.lower()
        d = self.frame(r, 120., 28.001, 16.2, et=102., accept=False)
        self.assertIsNone(r._ordinary_lower)
        self.assertNotIn(d.mode, {"ordinary_lower_offer", "ordinary_grid_wait"})
        self.assertNotIn("trial_epoch", r.export_overload_state())

    def test_known_terminal_source_stop_cannot_be_reopened_by_next_raw_safe_frame(self):
        r, _ = self.lower()
        stopped = self.frame(r, 111., 15., 16.2, et=102.,
            data_valid=False, data_error="synthetic_source_fault")
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual(r.last_target[0], 0)
        next_frame = self.frame(r, 112., 15., 7.1, et=112., accept=False)
        self.assertEqual(next_frame.current_p1, 0)
        self.assertNotEqual(next_frame.mode, "ordinary_lower_offer")
        self.assertTrue(r._restart_required)
        self.assert_history(r)

    def test_genuine_eight_response_and_later_grid_need_no_extra_seven(self):
        r = self.running()
        fourteen = self.frame(r, 110., 25., 16.2, et=102.)
        self.assertEqual((fourteen.current_p1, r.last_target[0]), (14, 14))
        eight = self.frame(r, 112., 18.1, 4., et=111.)
        self.assertEqual((eight.current_p1, r.last_target[0]), (8, 8))
        self.assertEqual(r._awaiting_reduction_target, 8)
        response = self.frame(r, 113., 13.3, 7.9, et=112.5)
        # A genuine response to8 plus its later grid resolves the earlier14.
        # It can propose16, while ordinary action spacing retains actual8.
        self.assertEqual((response.mode, response.current_p1), ("normal", 16))
        self.assertFalse(response.should_call_service)
        self.assertEqual(r.last_target[0], 8)
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._house_before_reduction)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertIsNone(r._committed_normal.unresolved_cap)
        for now, raw, gt in ((114., 13.3, 113.), (115., 12., 115.)):
            d = self.frame(r, now, raw, 7.05, gt=gt, et=114.)
            self.assertEqual(r.last_target[0], 8)
            self.assertGreaterEqual(d.current_p1, 8)
            self.assertIsNone(d.grid_confirmation_deadline_ts)
        raised = self.frame(r, 119., 12., 7.05, et=114.)
        self.assertEqual((raised.current_p1, r.last_target[0]), (16, 16))
        self.assert_history(r)


if __name__ == "__main__":
    unittest.main()
