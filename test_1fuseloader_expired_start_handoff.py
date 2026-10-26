"""Expired startup ownership may hand off to a RAW-safe lower offer only.

Synthetic source clocks, real regulator decisions/ACKs, no network or device.
The expired high offer receives neither another hold nor a new response clock.
"""
from copy import deepcopy
import unittest

import test_1fuseloader_positive_owner_priority as fixtures

logic = fixtures.logic


class ExpiredStartHandoffTests(unittest.TestCase):
    frame = fixtures.PositiveOwnerPriorityTests.frame
    bootstrap = fixtures.PositiveOwnerPriorityTests.bootstrap

    def expired_start(self):
        r = self.bootstrap()
        # A genuine grid-first mismatch starts the original fixed25s clock;
        # no current event or physical vehicle response is invented.
        d = r.evaluate(self.frame(65., grid=22.5, current=.012,
            receipt=None, grid_at=65., sequence=0, status="awaiting_start"))
        self.assertEqual((d.mode, d.current_p1, d.command), ("positive_ramp_hold", 16, None))
        self.assertAlmostEqual(d.positive_response_deadline_ts, 90.)
        self.assertIsNone(r._positive_response_ts)
        return r

    def expired_frame(self, now=90., *, receipt=89.8, current=6.4,
                      grid=11., grid_at=89.5, sequence=1, **metadata):
        return self.frame(now, grid=grid, current=current, receipt=receipt,
            grid_at=grid_at, sequence=sequence, **metadata)

    def test_consistent_partial_expiry_hands_off_only_raw_safe_lower_cap(self):
        r = self.expired_start()
        before = deepcopy(r.export_overload_state())
        d = r.evaluate(self.expired_frame())
        self.assertEqual((d.mode, d.current_p1, d.command), ("ordinary_lower_offer", 7, "set_limit"))
        self.assertLessEqual(d.grid_l1+d.current_p1, 23.)
        self.assertEqual(d.house_load_l1, 11.)
        self.assertIsNone(d.positive_response_deadline_ts)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(r._positive_ramp_deadline)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(r.last_set_limit_ts, 60.)
        self.assertEqual((r._ordinary_lower.cap, r._ordinary_lower.previous), (7, 16))
        self.assertIsNone(r._ordinary_lower.accepted_at)
        after = r.export_overload_state()
        self.assertEqual((after["used_seconds"], after["failure_reason"]),
            (before["used_seconds"], before["failure_reason"]))
        self.assertEqual(after["automatic_cycle"]["state"], "ready")
        self.assertEqual(after["automatic_cycle"]["used_seconds"], 0.)

    def test_late_or_missing_partial_cannot_prove_response_but_may_lower(self):
        for current, receipt, sequence in ((6.4, 90.05, 1), (.012, None, 0)):
            with self.subTest(current=current, receipt=receipt):
                r = self.expired_start()
                d = r.evaluate(self.expired_frame(90.1, current=current,
                    receipt=receipt, sequence=sequence))
                self.assertEqual((d.mode, d.current_p1), ("ordinary_lower_offer", 7))
                self.assertIsNone(r._positive_cap)
                self.assertIsNone(r._ordinary_lower.accepted_at)
                self.assertIsNone(r._ordinary_lower.first_response)
                self.assertIsNone(r._minimum_confirmation)

    def test_unsent_or_failed_lower_request_cannot_become_accepted_or_raise(self):
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                r = self.expired_start()
                proposed = r.evaluate(self.expired_frame())
                self.assertEqual(proposed.current_p1, 7)
                if attempted:
                    self.assertTrue(r.begin_submission(proposed, 90.))
                r.reject_submission(proposed, 90.1)
                # A fresh7 report before any successful7 ACK is not its reply.
                for now in (91., 94.):
                    d = r.evaluate(self.expired_frame(now, current=7.1,
                        receipt=now-.2, grid_at=now, sequence=int(now)))
                    if attempted:
                        self.assertEqual((d.mode, d.current_p1), ("ordinary_lower_offer", 7))
                        self.assertIsNone(r._ordinary_lower.accepted_at)
                        self.assertIsNone(r._ordinary_lower.first_response)
                    else:
                        # No lower request left the regulator. This new pair
                        # proves the WHOLE actual16 offer fits normal budget;
                        # retain it without sending/ACKing either16 or7.
                        if now == 91.:
                            self.assertEqual((d.mode, d.current_p1, d.command), ("normal_continuity", 16, None))
                            self.assertFalse(d.should_call_service)
                        else:
                            self.assertEqual((d.mode, d.current_p1, d.command), ("normal", 16, "set_limit"))
                            self.assertTrue(d.should_call_service)
                        self.assertIsNone(r._ordinary_lower)
                    self.assertEqual(r.last_target[0], 16)
                    self.assertEqual(r.last_set_limit_ts, 60.)
                    self.assertIsNone(r._minimum_confirmation)
                    self.assertIsNone(r._positive_cap)

    def test_only_new_ack_and_genuine_response_with_later_grid_retire_high_authority(self):
        r = self.expired_start()
        proposed = r.evaluate(self.expired_frame())
        r.acknowledge(proposed, 90.2)
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r._ordinary_lower.previous, 16)
        old = r.evaluate(self.expired_frame(91., grid_at=91.))
        self.assertEqual(old.current_p1, 7)
        self.assertIsNone(r._ordinary_lower.first_response)
        self.assertIsNone(r._minimum_confirmation)
        fresh = r.evaluate(self.expired_frame(92., current=7.1,
            receipt=91.8, grid_at=91., sequence=2))
        self.assertEqual((fresh.mode, fresh.current_p1, fresh.command), ("ordinary_grid_wait", 7, None))
        self.assertAlmostEqual(fresh.grid_confirmation_deadline_ts, 121.8)
        self.assertEqual(r._ordinary_lower.previous, 16)
        self.assertEqual(r.last_set_limit_ts, 90.2)
        completed = r.evaluate(self.expired_frame(93., current=7.1,
            receipt=91.8, grid_at=93., sequence=2))
        self.assertEqual((completed.mode, completed.current_p1), ("ordinary_lower_complete", 7))
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._uncertainty_ceiling)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(r._uncertainty_stops, 0)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_raw_source_lease_and_peak_guards_still_withhold_this_handoff(self):
        for extra in ({"grid": 16.01}, {"now": 120., "grid_at": 119.5, "receipt": 119.8},
                {"data_valid": False}, {"charger_current_trusted": False},
                {"charger_native_committed": False}, {"charger_publication_pending": True},
                {"control_source_id": ("different-source",)}, {"receipt": 96.},
                {"status": "paused"}, {"grid": 29.}, {"grid": 39.}):
            with self.subTest(extra=extra):
                r = self.expired_start()
                d = r.evaluate(self.expired_frame(**extra))
                self.assertNotEqual(d.mode, "ordinary_lower_offer")
                self.assertIsNone(r._ordinary_lower)

    def test_an_orphan_expired_timestamp_cannot_request_this_handoff(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(65., grid=13., current=8.,
            receipt=64., grid_at=65., sequence=1))
        self.assertEqual(d.current_p1, 16)
        self.assertIsNone(r._positive_cap)
        # Invalid old timing metadata alone establishes no positive owner.
        r._positive_ramp_deadline = 70.
        d = r.evaluate(self.expired_frame())
        self.assertNotEqual(d.mode, "ordinary_lower_offer")
        self.assertIsNone(r._ordinary_lower)

    def test_complete_old_high_wave_uses_same_lower_owner_and_fixed_birth_bound(self):
        r = self.expired_start()
        proposal = r.evaluate(self.expired_frame())
        r.acknowledge(proposal, 90.2)
        original = r._ordinary_lower.observation_deadline
        self.assertAlmostEqual(original, 114.5)
        for now, current, receipt, sequence, extra in (
                (91., 6.4, 89.8, 1, {}),
                (92., 15.2, 91.8, 2, {}),
                (93., 15.2, 91.8, 2,
                 {"charger_publication_pending": True, "charger_native_committed": False})):
            d = r.evaluate(self.expired_frame(now, grid=23.2, current=current,
                receipt=receipt, grid_at=now, sequence=sequence, **extra))
            self.assertEqual((d.mode, d.current_p1, d.command), ("ordinary_response_hold", 7, None))
            self.assertFalse(d.should_call_service)
            self.assertEqual(d.ordinary_response_deadline_ts, original)
            self.assertEqual(r.last_set_limit_ts, 90.2)
            self.assertIsNone(r._ordinary_lower.first_response)
            self.assertEqual(r._ordinary_lower.previous, 16)
        response = r.evaluate(self.expired_frame(95., grid=23.2, current=7.1,
            receipt=94.8, grid_at=94.5, sequence=3))
        self.assertEqual((response.mode, response.current_p1, response.command), ("ordinary_grid_wait", 7, None))
        self.assertIsNone(response.ordinary_response_deadline_ts)
        self.assertAlmostEqual(response.grid_confirmation_deadline_ts, 124.8)
        done = r.evaluate(self.expired_frame(96., grid=15.2, current=7.1,
            receipt=94.8, grid_at=96., sequence=3))
        self.assertEqual(done.mode, "ordinary_lower_complete")
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._uncertainty_ceiling)
        self.assertEqual(r._uncertainty_stops, 0)

    def test_lower_deadline_expires_or_yields_without_new_phase_and_preserves_history(self):
        r = self.expired_start()
        proposal = r.evaluate(self.expired_frame())
        r.acknowledge(proposal, 90.2)
        deadline = r._ordinary_lower.observation_deadline
        history = r.export_overload_state()["used_seconds"]
        d = r.evaluate(self.expired_frame(deadline, grid=23.2, current=15.2,
            receipt=deadline-.2, grid_at=deadline, sequence=2))
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._minimum_confirmation)
        self.assertEqual(r.export_overload_state()["used_seconds"], history)
        r = self.expired_start()
        proposal = r.evaluate(self.expired_frame())
        r.acknowledge(proposal, 90.2)
        r.evaluate(self.expired_frame(92., grid=26., current=15.2,
            receipt=91.8, grid_at=92., sequence=2))
        self.assertIsNone(r._ordinary_lower)
        self.assertFalse(r._minimum_confirmation and r._minimum_confirmation.owner == "ordinary")


if __name__ == "__main__":
    unittest.main()
