"""Public synthetic stale-state recovery under a full raw-current budget."""
from dataclasses import replace
import unittest

import test_1fuseloader_clock_provenance as clocks

logic = clocks.logic
EPOCH = clocks.EPOCH
SOURCE = clocks.SOURCE


class RawBudgetRecoveryTests(unittest.TestCase):
    def regulator(self, **options):
        settings = logic.RegulatorSettings(active_control=True, dry_run=False,
            overload_enabled=True, nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, measurement_settle_seconds=25,
            enable_soft_guard=False, enable_pause_resume=False)
        r = logic.OneFuseRegulator(replace(settings, **options))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        return r

    def sample(self, r, now, grid, current=5.985, *, et=1, gt=None, sequence=2,
               accept=True, **metadata):
        fields = dict(now_ts=now, wall_now_ts=EPOCH+now, status="charging",
            grid_l1=grid, charger_current=current, grid_report_ts=EPOCH+(now if gt is None else gt),
            charger_report_ts=None if et is None else EPOCH+et, charger_current_trusted=True,
            control_source_id=SOURCE, charger_report_family="native_terminal_receipt",
            charger_report_provenance=("receiver", "max_terminals", 183),
            charger_observation_report_times=() if et is None else ((183, EPOCH+et),),
            charger_observation_sequences=((182, int(sequence > 0)), (183, sequence), (184, int(sequence > 0)), (185, int(sequence > 0))))
        # This permits running the same public trace against the b26 baseline
        # to record the original veto before the new input field exists.
        if "charger_native_committed" in logic.RegulatorInput.__dataclass_fields__:
            fields["charger_native_committed"] = True
        fields.update(metadata)
        decision = r.evaluate(logic.RegulatorInput(**fields))
        if accept and decision.should_call_service:
            r.acknowledge(decision, now)
        return decision

    def steady_six(self, **options):
        r = self.regulator(**options)
        # The first takeover fits raw17 + the entire6 A offer without EV
        # subtraction. A genuine next receiver event establishes that cache.
        self.assertEqual(self.sample(r, 0, 17., et=0, sequence=1).current_p1, 6)
        self.assertEqual(self.sample(r, 1, 23.).current_p1, 6)
        for now in (30, 60, 90, 120, 150, 180):
            self.assertEqual(self.sample(r, now, 23.).current_p1, 6)
        self.assertEqual(r.last_target[0], 6)
        self.assertIsNone(r._positive_cap)
        return r

    def test_stale_six_uses_full_raw_budget_for_twelve_not_six_or_sixteen(self):
        r = self.steady_six()
        old_event = r._last_input_charger_ts
        before = r.export_overload_state()
        proposal = self.sample(r, 205, 10.5, accept=False)
        self.assertEqual(proposal.current_p1, 12)
        self.assertTrue(proposal.should_call_service)
        self.assertEqual(proposal.house_load_l1, 10.5)
        self.assertLessEqual(proposal.house_load_l1+proposal.current_p1, 23.)
        self.assertEqual(r._last_input_charger_ts, old_event)
        self.assertEqual(r.export_overload_state(), before)

    def test_grid_first_then_actual_response_and_later_grid_permit_sixteen(self):
        r = self.steady_six()
        self.assertEqual(self.sample(r, 205, 10.5).current_p1, 12)
        pending = self.sample(r, 207, 16.5, accept=False)
        self.assertEqual((pending.current_p1, pending.command), (12, None))
        self.assertEqual(r._last_input_charger_ts, EPOCH+1)
        ev = self.sample(r, 208, 16.5, 12.05, et=208, gt=207, sequence=3, accept=False)
        self.assertEqual(ev.current_p1, 12)
        later = self.sample(r, 210, 16.5, 12.05, et=208, sequence=3, accept=False)
        self.assertEqual((later.mode, later.current_p1, later.command), ("normal_continuity", 12, None))
        self.assertFalse(later.should_call_service)
        self.assertIsNone(r._positive_cap)
        rise = self.sample(r, 211, 16.5, 12.05, et=208, sequence=3, accept=False)
        self.assertEqual(rise.current_p1, 16)
        self.assertTrue(rise.should_call_service)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")
        self.assertNotIn("trial_epoch", r.export_overload_state())

    def test_ev_first_also_waits_for_later_grid_before_sixteen(self):
        r = self.steady_six()
        self.sample(r, 205, 10.5)
        first = self.sample(r, 207, 10.5, 12.05, et=207, gt=205, sequence=3, accept=False)
        self.assertEqual((first.current_p1, first.command), (12, None))
        later = self.sample(r, 210, 16.5, 12.05, et=207, sequence=3, accept=False)
        self.assertEqual((later.current_p1, later.command), (12, None))
        self.assertIsNone(r._positive_cap)
        rise = self.sample(r, 211, 16.5, 12.05, et=207, sequence=3, accept=False)
        self.assertEqual(rise.current_p1, 16)

    def test_bootstrap_numeric_only_tuple_needs_one_actual_post_ack_event(self):
        r = self.regulator()
        self.assertEqual(self.sample(r, 0, 17., et=None, sequence=0).current_p1, 6)
        self.assertEqual(self.sample(r, 40, 10.5, et=None, sequence=0).current_p1, 12)
        self.assertIsNone(r._positive_ev_ts)
        pending = self.sample(r, 42, 16.5, et=None, sequence=0, accept=False)
        self.assertEqual((pending.current_p1, pending.command), (12, None))
        self.sample(r, 43, 16.5, 12.05, et=43, gt=42, sequence=1, accept=False)
        done = self.sample(r, 45, 16.5, 12.05, et=43, sequence=1, accept=False)
        self.assertEqual((done.current_p1, done.command), (12, None))
        self.assertIsNone(r._positive_cap)
        rise = self.sample(r, 46, 16.5, 12.05, et=43, sequence=1, accept=False)
        self.assertEqual(rise.current_p1, 16)

    def test_declines_missing_opt_in_commit_trust_identity_or_complete_tuple(self):
        cases = [dict(charger_native_committed=False), dict(charger_native_committed="yes"),
            dict(charger_increase_ready=False), dict(charger_publication_pending=True),
            dict(charger_current_trusted=False), dict(control_source_id="other_source"),
            dict(charger_observation_sequences=((183, 2),)),
            dict(data_valid=False, data_error="stream_disconnected"),
            dict(charger_event_source_known=True, charger_event_source_ts=None),
            dict(charger_event_source_known=True, charger_event_source_ts=EPOCH+220)]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                r = self.steady_six()
                d = self.sample(r, 205, 10.5, accept=False, **metadata)
                self.assertLessEqual(d.current_p1, 6)
        r = self.steady_six(nominal_import_only_fallback=False)
        self.assertEqual(self.sample(r, 205, 10.5, accept=False).current_p1, 6)

    def test_source_status_peak_and_lease_failures_do_not_gain_authority(self):
        for metadata in (dict(status="paused"), dict(status="disconnected"), dict(status="completed"),
                         dict(status="fault"), dict(gt=179), dict(gt=206)):
            with self.subTest(metadata=metadata):
                r = self.steady_six()
                self.assertLessEqual(self.sample(r, 205, 10.5, accept=False, **metadata).current_p1, 6)
        r = self.steady_six()
        self.assertLessEqual(self.sample(r, 241, 10.5, accept=False).current_p1, 6)
        r = self.steady_six()
        self.assertEqual(self.sample(r, 205, 39., accept=False).current_p1, 0)

    def test_raw_rounding_boundary_credits_no_ev_amps(self):
        for grid, expected in ((11., 12), (11.001, 11), (17.1, 6)):
            with self.subTest(grid=grid):
                r = self.steady_six()
                d = self.sample(r, 205, grid, accept=False)
                self.assertEqual(d.current_p1, expected)
                if d.current_p1 > 6:
                    self.assertLessEqual(grid+d.current_p1, 23.)
                    self.assertEqual(d.house_load_l1, grid)

    def test_grid_sample_age_and_api_latency_spend_same_original_clock(self):
        r = self.steady_six()
        proposal = self.sample(r, 205, 10.5, gt=200, accept=False)
        self.assertEqual(proposal.current_p1, 12)
        r.acknowledge(proposal, 208)
        self.assertEqual(r._positive_raw_deadline, 225.)
        self.assertEqual(self.sample(r, 224.9, 16.5, accept=False).current_p1, 12)
        self.assertEqual(self.sample(r, 225., 16.5, accept=False).current_p1, 0)
        for gt in (180, 179):
            r = self.steady_six()
            self.assertEqual(self.sample(r, 205, 10.5, gt=gt, accept=False).current_p1, 6)

    def test_late_ack_records_truth_but_does_not_restart_response_clock(self):
        r = self.steady_six()
        proposal = self.sample(r, 205, 10.5, accept=False)
        r.acknowledge(proposal, 231)
        self.assertEqual(r.last_target[0], 12)
        self.assertEqual(proposal.raw_budget_rise_context.response_deadline_ts, 230.)
        self.assertIsNone(r._positive_raw_deadline)
        self.assertIsNone(r._positive_cap)
        self.assertTrue(r._restart_required)
        stopped = self.sample(r, 231.1, 16.5, accept=False)
        self.assertEqual(stopped.current_p1, 0)

    def test_old_raw_reply_cannot_reopen_after_terminal_zero_without_zero_ack(self):
        r = self.steady_six()
        proposal = self.sample(r, 205, 10.5, accept=False)
        stop = self.sample(r, 207, 10.5, data_valid=False, data_error="source_fault", accept=False)
        self.assertEqual(stop.current_p1, 0)
        self.assertTrue(r._restart_required)
        r.acknowledge(proposal, 207.1)  # Remote outcome arrives after our terminal decision.
        self.assertEqual(r.last_target[0], 12)  # Accepted cloud authority is recorded truthfully.
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._positive_cap)

    def test_old_raw_reply_cannot_rebind_or_label_newer_grid_as_original_sample(self):
        r = self.steady_six()
        proposal = self.sample(r, 205, 10.5, accept=False)
        self.sample(r, 207, 10.5, accept=False)
        r.acknowledge(proposal, 208)
        self.assertEqual(r._positive_raw_deadline, 230.)
        self.assertEqual(r._positive_grid_ts, EPOCH+205)
        r = self.steady_six()
        proposal = self.sample(r, 205, 10.5, accept=False)
        self.sample(r, 207, 10.5, control_source_id="changed_source", accept=False)
        r.acknowledge(proposal, 208)
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._positive_cap)

    def test_independent_raw_budget_retains_expired_owner_without_time_refund(self):
        r = self.steady_six()
        self.sample(r, 205, 10.5)
        deadline = r._positive_raw_deadline
        for now in (230, 235, 265):
            d = self.sample(r, now, 10.5)
            self.assertEqual(d.current_p1, 12)
            self.assertEqual(r._positive_raw_deadline, deadline)
            self.assertEqual(r._positive_ev_ts, EPOCH+1)
        late_response = self.sample(r, 266, 16.5, 12.05, et=266, sequence=3, accept=False)
        self.assertEqual(late_response.current_p1, 0)

    def test_custom_soft_and_pause_guards_override_response_hold(self):
        r = self.steady_six(enable_soft_guard=True, soft_limit_amps=15, soft_for_seconds=1, soft_current_amps=7)
        self.sample(r, 205, 10.5)
        self.sample(r, 207, 16.5, accept=False)
        soft = self.sample(r, 208, 16.5)
        self.assertEqual((soft.mode, soft.current_p1), ("soft_limit", 7))
        self.assertEqual(r._reduction_guard_deadline, 230.)
        r = self.steady_six()
        r.update_settings(replace(r.settings, enable_pause_resume=True, pause_limit_amps=15, pause_for_seconds=1))
        self.sample(r, 205, 10.5)
        self.sample(r, 207, 16.5, accept=False)
        pause = self.sample(r, 208, 16.5, accept=False)
        self.assertEqual(pause.command, "pause")

    def test_later_high_raw_reduction_inherits_existing_raw_rise_deadline(self):
        r = self.steady_six()
        self.sample(r, 205, 10.5)
        reduced = self.sample(r, 228, 26.1)
        self.assertEqual(reduced.current_p1, 7)
        self.assertEqual(reduced.nominal_response_deadline_ts, 230.)
        self.assertEqual(self.sample(r, 230, 26.1, accept=False).current_p1, 0)

    def test_finished_nominal_reduction_does_not_permanently_veto_raw_rise(self):
        r = self.regulator(overload_enabled=False)
        self.sample(r, 0, 17., et=0, sequence=1)
        self.sample(r, 1, 23.)
        self.assertEqual(self.sample(r, 10, 10.5).current_p1, 16)
        self.assertEqual(r.last_target[0], 16)
        self.sample(r, 12, 20.5, 16., et=12, sequence=3)
        self.sample(r, 13, 20.5, 16., et=12, sequence=3)
        self.assertEqual(self.sample(r, 16, 32.5, 16., et=12, sequence=3).current_p1, 6)
        self.sample(r, 17, 32.5, et=17, gt=16, sequence=4)
        self.sample(r, 18, 22.5, et=17, sequence=4)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(r._accepted_reduction_cap, 6)
        self.assertTrue(r._reduction_response_completed)
        for now in (30, 60, 90, 120, 150, 180):
            self.assertEqual(self.sample(r, now, 22.5, et=17, sequence=4).current_p1, 6)
        self.assertEqual(self.sample(r, 205, 10.5, et=17, sequence=4, accept=False).current_p1, 12)


if __name__ == "__main__":
    unittest.main()
