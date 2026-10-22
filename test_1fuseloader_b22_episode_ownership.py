"""Public regulator routing across delayed positive and lower-cap responses.

All frames are synthetic. API acknowledgement is simulated acceptance, never
proof of physical delivery or synchronized current acquisition.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_b19_alignment as fixtures

logic = fixtures.logic
EPOCH = fixtures.fixtures.EPOCH
SOURCE = ("selected-charger", "selected-grid", "current", "status", "L1")
SPENT = 2400.0


class EpisodeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.submissions = {}

    def regulator(self, **options):
        values = dict(active_control=True, dry_run=False, overload_enabled=True,
                      bounded_nominal_shedding=True, nominal_import_only_fallback=True,
                      measurement_settle_seconds=25, enable_soft_guard=False,
                      enable_pause_resume=False)
        values.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**values))
        self.assertTrue(r.import_overload_state(dict(
            version=1, used_seconds=SPENT, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout")))
        self.submissions[id(r)] = []
        return r

    def step(self, r, now, grid, current, *, gt=None, et=None, status="charging",
             identity=SOURCE, accept=True, **metadata):
        values = dict(now_ts=now, status=status, grid_l1=grid, charger_current=current,
                      wall_now_ts=EPOCH+now, grid_report_ts=EPOCH+(now if gt is None else gt),
                      charger_report_ts=EPOCH+(now if et is None else et),
                      control_source_id=identity)
        values.update(metadata)
        d = r.evaluate(logic.RegulatorInput(**values))
        if accept and d.should_call_service:
            self.submissions[id(r)].append((d.command, d.current_p1, now))
            r.acknowledge(d, now)
        return d

    def accepted_rise(self, **options):
        r = self.regulator(**options)
        self.assertEqual(self.step(r, 0, 23, 7.08).current_p1, 7)
        self.step(r, 1, 23, 7.08, et=0)
        rise = self.step(r, 6, 9.4, 7.08)
        self.assertEqual((rise.current_p1, rise.should_call_service), (16, True))
        renewal = self.step(r, 36, 9.4, 7.08, et=6)
        self.assertEqual((renewal.current_p1, renewal.should_call_service), (16, True))
        return r

    def ev_first(self, r, *, now=56, grid=8.8, **kw):
        return self.step(r, now, grid, 13.909, gt=now-1, **kw)

    def lower_pending(self, *, grid=8.8, current=13.909):
        r = self.regulator()
        self.assertEqual(self.step(r, 0, 21.7, 16.2).current_p1, 16)
        d = self.step(r, 10, grid, current, gt=9)
        self.assertEqual((d.mode, d.current_p1, d.nominal_response_deadline_ts),
                         ("low_grid_alignment", 7, 19))
        return r

    def assert_old_exposure(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], SPENT)

    def assert_no_new_low_grid_time(self, d, old_deadline):
        self.assertIn(d.nominal_response_deadline_ts, (None, old_deadline))
        self.assertIn(d.nominal_response_remaining_seconds, (None, 0))
        self.assertIsNone(d.nominal_sync_deadline_ts)

    def test_ev_first_rise_keeps_original_sixteen_owner_then_later_grid_aligns(self):
        r = self.accepted_rise()
        mixed = self.ev_first(r)
        self.assertEqual(mixed.current_p1, 16)
        self.assertFalse(mixed.should_call_service)
        self.assertFalse(mixed.measurement_uncertainty_active)
        self.assertIsNone(mixed.nominal_response_deadline_ts)
        self.assertAlmostEqual(mixed.measurement_alignment_remaining_seconds, 25)
        aligned = self.step(r, 57.2, 17.6, 13.909, et=56)
        self.assertEqual(aligned.current_p1, 16)
        self.assertFalse(aligned.should_call_service)
        self.assertEqual([cap for _, cap, _ in self.submissions[id(r)]], [7, 16, 16])
        self.assert_old_exposure(r)

    def test_identical_ttl_acceptance_never_restarts_positive_response_window(self):
        r = self.accepted_rise()
        self.ev_first(r)
        renewal = self.step(r, 67, 8.8, 13.909, gt=66, et=56)
        self.assertEqual((renewal.current_p1, renewal.should_call_service), (16, True))
        self.assertAlmostEqual(renewal.measurement_alignment_remaining_seconds, 14)
        expired = self.step(r, 81, 8.8, 13.909, gt=80, et=81)
        self.assertEqual(expired.current_p1, 0)
        self.assertNotEqual(expired.mode, "low_grid_alignment")
        self.assert_old_exposure(r)

    def test_aged_positive_owner_can_use_existing_whole_offer_hold_without_new_time(self):
        r = self.accepted_rise()
        self.ev_first(r, grid=5.1)
        hold = self.step(r, 81, 5.1, 13.909, gt=80)
        self.assertEqual(hold.current_p1, 16)
        self.assertEqual(hold.mode, "waiting_for_aligned_sources")
        self.assertIn(hold.measurement_alignment_remaining_seconds, (None, 0))
        self.assertIsNone(hold.nominal_response_deadline_ts)
        self.assertLessEqual(hold.grid_l1+hold.current_p1, 23)
        self.assert_old_exposure(r)

    def test_whole_offer_safe_positive_hold_does_not_require_forged_new_ev_event(self):
        r = self.accepted_rise()
        self.ev_first(r, grid=5.1)
        self.assertEqual(self.step(r, 81, 5.1, 13.909, gt=80, et=56).current_p1, 16)
        aged = self.step(r, 83, 5.1, 13.909, et=56)
        self.assertGreater(aged.source_report_skew_seconds, 25)
        self.assertEqual((aged.mode, aged.current_p1), ("waiting_for_aligned_sources", 16))
        self.assertIsNone(aged.nominal_response_deadline_ts)
        self.assertIn(aged.measurement_alignment_remaining_seconds, (None, 0))
        self.assert_old_exposure(r)

    def test_invalid_sources_cannot_claim_the_positive_owner(self):
        cases = ({"charger_current_trusted": False}, {"data_valid": False},
                 {"charger_report_ts": None}, {"grid_report_ts": None},
                 {"wall_now_ts": None}, {"charger_report_ts": EPOCH+70},
                 {"grid_report_ts": EPOCH-100}, {"identity": ("other-source",)})
        for metadata in cases:
            with self.subTest(metadata=metadata):
                r = self.accepted_rise()
                stopped = self.ev_first(r, **metadata)
                self.assertEqual(stopped.current_p1, 0)
                self.assertNotEqual(stopped.house_load_basis, "positive_response_anchor")
                self.assert_old_exposure(r)

    def test_expired_accepted_lease_cannot_claim_the_positive_owner(self):
        r = self.accepted_rise()
        stopped = self.ev_first(r, now=96)
        self.assertEqual(stopped.current_p1, 0)
        self.assertNotEqual(stopped.mode, "settling")
        self.assert_old_exposure(r)

    def test_excess_current_and_manual_pause_never_receive_a_positive_hold(self):
        for status, current in (("charging", 16.501), ("paused", 13.909),
                                ("awaiting_scheduled_start", 13.909)):
            with self.subTest(status=status, current=current):
                r = self.accepted_rise()
                d = self.step(r, 56, 8.8, current, gt=55, status=status)
                self.assertNotEqual((d.command, d.current_p1), ("resume", 16))
                self.assertNotEqual(d.house_load_basis, "positive_response_anchor")
                self.assertLess(d.current_p1, 16)
                self.assert_old_exposure(r)

    def test_raw_above_normal_ceiling_cannot_hide_behind_old_positive_anchor(self):
        r = self.accepted_rise()
        d = self.ev_first(r, grid=23.0001)
        self.assertNotEqual(d.house_load_basis, "positive_response_anchor")
        self.assertLess(d.current_p1, 16)
        self.assert_old_exposure(r)

    def test_coherent_grid_hands_lower_cap_to_remaining_reduction_guard(self):
        r = self.lower_pending()
        handoff = self.step(r, 11, 17.6, 13.909, et=10)
        self.assertEqual(handoff.current_p1, 7)
        self.assertFalse(handoff.should_call_service)
        self.assertIsNotNone(handoff.measurement_alignment_remaining_seconds)
        self.assertLessEqual(handoff.measurement_alignment_remaining_seconds, 8)
        response = self.step(r, 12, 17.6, 7.079, gt=11)
        self.assertEqual(response.current_p1, 7)
        pending_grid = self.step(r, 12.7, 17.6, 7.079, gt=11, et=12)
        self.assertEqual(pending_grid.current_p1, 7)
        self.assertEqual(pending_grid.nominal_response_deadline_ts, 19)
        aligned = self.step(r, 13, 11.5, 7.079, et=12)
        self.assertEqual(aligned.current_p1, 7)
        self.assertNotEqual(aligned.command, "resume")
        self.assertEqual([cap for _, cap, _ in self.submissions[id(r)]], [16, 7])
        self.assert_old_exposure(r)

    def test_handoff_cannot_purchase_a_fresh_twenty_five_seconds(self):
        r = self.lower_pending()
        self.step(r, 11, 17.6, 13.909, et=10)
        d = self.step(r, 19, 17.6, 13.909, et=10)
        self.assertEqual(d.current_p1, 0)
        self.assert_old_exposure(r)

    def test_pre_ack_lower_event_cannot_retire_the_shared_reduction_guard(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        proposal = self.step(r, 10, 8.8, 13.909, gt=9, accept=False)
        r.acknowledge(proposal, 10.4)
        self.assertEqual(self.step(r, 11, 17.6, 13.909, et=10).current_p1, 7)
        # This event advanced relative to the entry, but was acquired/reported
        # before the lower request's ACK floor. It cannot finish that response.
        for then in (12, 12.2):
            d = self.step(r, then, 17.6, 7.079, et=10.2)
            self.assertEqual(d.current_p1, 7)
            self.assertNotEqual(d.mode, "low_grid_alignment_complete")
            self.assertEqual(d.nominal_response_deadline_ts, 19)
        self.assert_old_exposure(r)

    def test_lower_response_completion_cannot_offer_seven_above_nominal_house_budget(self):
        r = self.lower_pending()
        self.step(r, 11, 8.8, 0, gt=9, status="awaiting_start")
        d = self.step(r, 11.1, 23, 0, et=11, status="awaiting_start")
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(d.mode, "low_grid_alignment_complete")
        self.assert_old_exposure(r)

    def test_two_amp_grid_does_not_timeout_safe_existing_seven(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        pending = self.step(r, 10, 5.1, 16.2, et=0)
        self.assertEqual(pending.current_p1, 7)
        self.step(r, 18, 2, 7.048, gt=15)
        for t in (20, 21, 25, 40, 41):
            d = self.step(r, t, 2, 7.048, et=18)
            self.assertEqual(d.current_p1, 7)
            self.assertNotEqual(d.command, "resume")
            self.assert_no_new_low_grid_time(d, 20)
        self.assertTrue(all(cap in {7, 16} for _, cap, _ in self.submissions[id(r)]))
        self.assert_old_exposure(r)

    def test_whole_previous_authority_fits_without_fabricated_ev_response(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        self.step(r, 10, 5.1, 16.2, et=0)
        d = self.step(r, 20, 5.1, 16.2, et=0)
        self.assertEqual(d.current_p1, 7)
        self.assertEqual(d.charger_current, 16.2)
        self.assertLessEqual(d.grid_l1+16, 23)
        self.assert_no_new_low_grid_time(d, 20)
        self.assertEqual(d.measurement_uncertainty_cap, 7)
        self.assert_old_exposure(r)

    def test_only_lower_cap_fitting_does_not_erase_unresolved_previous_authority(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        self.step(r, 10, 5.1, 16.2, et=0)
        self.assertEqual(self.step(r, 20, 5.1, 16.2, et=0).current_p1, 7)
        d = self.step(r, 21, 7.0001, 16.2, et=0)
        self.assertLess(d.grid_l1+7, 23)
        self.assertGreater(d.grid_l1+16, 23)
        self.assertEqual(d.current_p1, 0)
        self.assert_old_exposure(r)

    def test_whole_authority_hold_keeps_source_and_lease_guards(self):
        for metadata, now in (({"charger_current_trusted": False}, 21),
                              ({"data_valid": False}, 21),
                              ({"identity": ("other-source",)}, 21),
                              ({"grid_report_ts": None}, 21), ({}, 70)):
            with self.subTest(metadata=metadata, now=now):
                r = self.regulator()
                self.step(r, 0, 21.7, 16.2)
                self.step(r, 10, 5.1, 16.2, et=0)
                self.assertEqual(self.step(r, 20, 5.1, 16.2, et=0).current_p1, 7)
                d = self.step(r, now, 5.1, 16.2, et=0, **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assert_old_exposure(r)

    def test_old_raw_hold_ack_cannot_reverse_a_newer_protective_zero(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        self.step(r, 10, 5.1, 16.2, et=0)
        self.step(r, 20, 5.1, 16.2, et=0)
        pending = self.step(r, 40, 5.1, 16.2, et=0, accept=False)
        self.assertEqual((pending.mode, pending.current_p1, pending.should_call_service),
                         ("low_grid_raw_hold", 7, True))
        stopped = self.step(r, 41, 7.1, 16.2, et=0)
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual(r.last_target[0], 0)
        r.acknowledge(pending, 42)
        # Truthfully retain that late API acceptance, while preserving zero as
        # the required next outcome rather than silently accepting a restart.
        self.assertEqual(r.last_target[0], 7)
        protected = self.step(r, 43, 2, .02, status="awaiting_start")
        self.assertEqual(protected.current_p1, 0)
        self.assertNotEqual(protected.command, "resume")
        self.assertIsNone(protected.nominal_response_deadline_ts)
        self.assertIsNone(protected.nominal_sync_deadline_ts)
        self.assert_old_exposure(r)

    def test_raw_hold_ack_after_old_lease_expiry_cannot_recreate_authority(self):
        r = self.regulator()
        self.step(r, 0, 21.7, 16.2)
        self.step(r, 10, 5.1, 16.2, et=0)
        self.step(r, 20, 5.1, 16.2, et=0)
        pending = self.step(r, 40, 5.1, 16.2, et=0, accept=False)
        self.assertTrue(pending.should_call_service)
        self.assertEqual(r.last_set_limit_ts, 10)
        r.acknowledge(pending, 70.1)
        self.assertEqual(r.last_target[0], 7)
        protected = self.step(r, 70.2, 2, 7.048)
        self.assertEqual(protected.current_p1, 0)
        self.assertTrue(protected.should_call_service)
        self.assertNotEqual(protected.command, "resume")
        self.assertIsNone(protected.nominal_response_deadline_ts)
        self.assert_old_exposure(r)


if __name__ == "__main__":
    unittest.main()
