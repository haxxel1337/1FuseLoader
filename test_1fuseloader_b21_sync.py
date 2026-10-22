"""Bounded publication-order tolerance; never physical simultaneity proof."""
from dataclasses import replace
from copy import deepcopy
import unittest

import test_1fuseloader_b19_alignment as fixtures

logic = fixtures.logic
EPOCH = fixtures.fixtures.EPOCH


class NominalSyncTests(unittest.TestCase):
    sample = fixtures.LowGridAlignmentTests.sample
    regulator = fixtures.LowGridAlignmentTests.regulator
    running = fixtures.LowGridAlignmentTests.running

    def pending(self, *, accept=True, grid_ready=True, **options):
        r = self.running(**options)
        if options.get("normal_ttl_seconds", 60) < 40:
            self.sample(r, 35, 21.7, 16.2)
        d = self.sample(r, 40, 27.7, 16.2, et=35, accept=False)
        self.assertEqual((d.mode, d.current_p1, d.nominal_response_deadline_ts), ("normal_shedding", 7, 50))
        if accept:
            r.acknowledge(d, 40.4)
        if grid_ready:
            self.sample(r, 45, 18.6, 16.2, et=35)
        return r

    def syncing(self, **options):
        r = self.pending(**options)
        d = self.sample(r, 48.2, 18.6, 7.079, gt=45)
        self.assertEqual((d.mode, d.current_p1, d.command), ("normal_shedding_sync", 7, None))
        self.assertAlmostEqual(d.nominal_sync_deadline_ts, 53.2, places=5)
        return r

    def test_timely_low_ev_waits_for_next_grid_without_zero_at_original_deadline(self):
        r = self.syncing()
        d = self.sample(r, 50, 18.6, 7.079, gt=45, et=48.2)
        self.assertEqual((d.mode, d.current_p1, d.command), ("normal_shedding_sync", 7, None))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertIsNone(d.nominal_response_remaining_seconds)
        self.assertEqual(r._normal_shed_deadline, 50)
        self.assertTrue(d.current_refresh_required)
        d = self.sample(r, 50.3, 18.6, 7.044, gt=50, et=50.1)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_complete", 7))
        self.assertLessEqual(d.house_load_l1+7, 23)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")
        self.assertGreaterEqual(r.export_overload_state()["used_seconds"], 2000)

    def test_sync_emits_no_write_resume_or_ttl_renewal(self):
        r = self.syncing(resend_limit_every_seconds=1)
        last_ack = r.last_set_limit_ts
        for t in (49, 50, 51, 52):
            d = self.sample(r, t, 18.6, 7.079, gt=45, et=48.2)
            self.assertEqual((d.current_p1, d.command, d.service, d.should_call_service), (7, None, None, False))
            self.assertEqual(r.last_set_limit_ts, last_ack)

    def test_first_response_owns_five_seconds_despite_new_receipts(self):
        r = self.syncing()
        deadline = r._normal_shed_sync_deadline
        for t in (49, 50, 51, 52):
            d = self.sample(r, t, 18.6, 7.044, gt=45, et=t)
            self.assertEqual(d.nominal_sync_deadline_ts, deadline)
            self.assertEqual(r._normal_shed_sync_floor, EPOCH+48.2)
        d = self.sample(r, deadline, 18.6, 7.044, gt=45, et=52)
        self.assertEqual((d.current_p1, d.mode), (0, "normal_shedding_stop"))
        self.assertIn("sync_timeout", d.reason)

    def test_original_alignment_window_can_shorten_sync(self):
        r = self.pending(measurement_settle_seconds=12)
        d = self.sample(r, 48.2, 18.6, 7.079, gt=45)
        self.assertEqual(d.nominal_sync_deadline_ts, 52)
        self.assertEqual(self.sample(r, 52, 18.6, 7.079, gt=45, et=48.2).current_p1, 0)

    def test_late_get_with_old_timely_event_does_not_backfill_phase(self):
        r = self.pending()
        d = self.sample(r, 51, 18.6, 7.079, gt=45, et=48.2, charger_snapshot_verified=True)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.nominal_sync_deadline_ts)
        self.assertIsNone(r._normal_shed_response_monotonic)

    def test_ev_event_at_or_after_physical_deadline_never_qualifies(self):
        for t in (50, 51):
            r = self.pending()
            d = self.sample(r, t, 18.6, 7.079, gt=45)
            self.assertEqual(d.current_p1, 0)
            self.assertIsNone(d.nominal_sync_deadline_ts)

    def test_no_api_ack_or_pre_ack_low_event_cannot_enter_sync(self):
        r = self.pending(accept=False)
        d = self.sample(r, 48.2, 18.6, 7.079, gt=45, accept=False)
        self.assertNotEqual(d.mode, "normal_shedding_sync")
        self.assertEqual(self.sample(r, 50, 18.6, 7.079, gt=45, accept=False).current_p1, 0)
        r = self.pending()
        d = self.sample(r, 48.2, 18.6, 7.079, gt=45, et=40.2)
        self.assertNotEqual(d.mode, "normal_shedding_sync")

    def test_fully_ordered_response_before_original_deadline_is_not_shortened(self):
        r = self.pending(grid_ready=False)
        self.sample(r, 43, 27.7, 7.079)
        d = self.sample(r, 49, 18.6, 7.079, et=43)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_complete", 7))

    def test_unused_expired_sync_candidate_does_not_shorten_original_window(self):
        r = self.pending(grid_ready=False)
        self.sample(r, 43, 27.7, 7.079)
        d = self.sample(r, 48.6, 18.6, 7.079, gt=48.5)
        self.assertEqual((d.mode, d.nominal_response_deadline_ts), ("normal_shedding", 50))
        self.assertIsNone(d.nominal_sync_deadline_ts)
        d = self.sample(r, 49, 18.6, 7.079, et=48.6)
        self.assertEqual(d.mode, "normal_shedding_complete")

    def test_jitter_does_not_move_semantic_floor_past_the_next_grid(self):
        r = self.syncing()
        d = self.sample(r, 50.3, 18.6, 7.044, gt=50, et=50.1)
        self.assertEqual(d.mode, "normal_shedding_complete")

    def test_substantive_low_then_high_requires_grid_after_the_latest_value_change(self):
        r = self.syncing()
        deadline = r._normal_shed_sync_deadline
        self.sample(r, 49, 18.6, 1, gt=45)
        d = self.sample(r, 52, 17, 7, gt=51)
        self.assertEqual(d.mode, "normal_shedding_sync")
        self.assertEqual(d.nominal_sync_deadline_ts, deadline)
        self.assertEqual(r._normal_shed_sync_floor, EPOCH+52)
        d = self.sample(r, 52.5, 27, 7, et=52)
        self.assertEqual(d.current_p1, 0)

    def test_substantive_changed_value_cannot_share_an_old_event_timestamp(self):
        r = self.syncing()
        d = self.sample(r, 49, 18.6, 6, gt=45, et=48.2)
        self.assertEqual(d.current_p1, 0)

    def test_raw_exact_buffer_boundary_is_allowed_but_epsilon_above_stops(self):
        r = self.syncing()
        self.assertEqual(self.sample(r, 49, 23, 7.079, gt=47, et=48.2).mode, "normal_shedding_sync")
        d = self.sample(r, 49.1, 23.0001, 7.079, gt=47.1, et=48.2)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(self.sample(r, 49.2, 18.6, 7.079, gt=47.1, et=48.2, accept=False).current_p1, 0)

    def test_excess_current_fault_and_manual_wait_stop_immediately(self):
        for kw, current in (({}, 7.501), ({"data_valid": False}, 7.079),
                            ({"charger_current_trusted": False}, 7.079),
                            ({"status": "paused"}, 7.079),
                            ({"status": "waiting_for_schedule"}, 7.079)):
            r = self.syncing()
            self.assertEqual(self.sample(r, 49, 18.6, current, gt=45, **kw).current_p1, 0)

    def test_missing_future_and_regressed_source_clocks_stop(self):
        for kw in ({"charger_report_ts": None}, {"grid_report_ts": None},
                   {"wall_now_ts": None}, {"charger_report_ts": EPOCH+50},
                   {"charger_report_ts": EPOCH+47}, {"grid_report_ts": EPOCH+44}):
            r = self.syncing()
            self.assertEqual(self.sample(r, 49, 18.6, 7.079, gt=45, et=48.2, **kw).current_p1, 0)

    def test_future_first_response_does_not_create_sync(self):
        r = self.pending()
        d = self.sample(r, 48.2, 18.6, 7.079, gt=45, et=49)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.nominal_sync_deadline_ts)

    def test_rebound_source_identity_stops(self):
        r = self.syncing()
        d = self.sample(r, 49, 18.6, 7.079, gt=45, et=48.2, identity=("different",))
        self.assertEqual(d.current_p1, 0)

    def test_settings_change_cannot_extend_or_continue_sync(self):
        r = self.syncing()
        r.update_settings(replace(r.settings, measurement_settle_seconds=30))
        d = self.sample(r, 49, 18.6, 7.079, gt=45, et=48.2)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("settings_changed_during_sync", d.reason)

    def test_expired_lease_stops_even_inside_sync_deadline(self):
        # Deliberately short pure-logic lease exercises the independent bound;
        # the production adapter requires the usual minute-based cloud TTL.
        r = self.syncing(normal_ttl_seconds=10)
        self.assertEqual(self.sample(r, 50.4, 18.6, 7.079, gt=45, et=48.2).current_p1, 0)

    def test_persisted_sync_is_interrupted_not_restored_as_a_new_window(self):
        r = self.syncing()
        state = deepcopy(r.export_overload_state())
        self.assertTrue(state["normal_control"]["response_pending"])
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        d = self.sample(restored, 60, 2.4, .02, status="awaiting_start")
        self.assertEqual((d.current_p1, d.measurement_uncertainty_stops), (0, 1))
        self.assertIsNone(d.nominal_sync_deadline_ts)
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")

    def test_reset_does_not_grant_another_sync_window(self):
        r = self.syncing()
        r.reset()
        d = self.sample(r, 49, 18.6, 7.079, gt=45, et=48.2)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.nominal_sync_deadline_ts)

    def test_sync_failure_does_not_permanently_block_qualified_capped_recovery(self):
        r = self.syncing()
        end = r._normal_shed_sync_deadline
        self.sample(r, end, 18.6, 7.079, gt=45, et=48.2)
        for t in (54, 74, 94):
            self.assertEqual(self.sample(r, t, 9, .02, status="awaiting_start").current_p1, 0)
        d = self.sample(r, 114, 9, .02, status="awaiting_start")
        self.assertEqual(d.current_p1, 7)
        self.assertTrue(d.should_call_service)
        self.assertIsNone(r._normal_shed_sync_deadline)


if __name__ == "__main__":
    unittest.main()
