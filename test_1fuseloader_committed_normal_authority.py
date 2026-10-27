"""Actual normal authority, causal dispatch and unsent intent are distinct."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_cached_native_normal as cached
import test_1fuseloader_startup_minimum_confirmation as startup

logic = cached.logic
EPOCH = cached.EPOCH


class CommittedNormalAuthorityTests(unittest.TestCase):
    frame = cached.CachedNativeNormalTests.frame
    def normal(self):
        fixture = cached.CachedNativeNormalTests()
        return fixture.prepared(), fixture

    def test_connected_status_bridge_keeps_only_actual_cap_and_first_deadline(self):
        r, f = self.normal()
        first = r.evaluate(f.native(973., status="awaiting_start"))
        self.assertEqual((first.mode, first.current_p1, first.command), ("normal_continuity", 14, None))
        lease, deadline = r.last_set_limit_ts, first.normal_continuity_deadline_ts
        self.assertAlmostEqual(deadline, 973.+r.settings.measurement_settle_seconds)
        repeated = r.evaluate(f.native(980., status="ready_to_charge"))
        self.assertEqual(repeated.normal_continuity_deadline_ts, deadline)
        self.assertEqual(r.last_set_limit_ts, lease)
        self.assertIsNone(repeated.command)
        expired = r.evaluate(f.native(deadline, status="awaiting_start"))
        self.assertNotEqual(expired.mode, "normal_continuity")

    def test_detached_retention_does_not_spend_lease_or_modify_source_proof(self):
        r, f = self.normal()
        inp = f.native(973.)
        d = r.evaluate(inp)
        before = deepcopy(r.__dict__)
        held = r.committed_normal_retention(inp, d)
        self.assertIsNotNone(held)
        self.assertEqual((held.current_p1, held.command, held.should_call_service), (14, None, False))
        self.assertEqual(r.__dict__, before)
        for change in ({"data_valid": False}, {"charger_native_committed": False},
                       {"control_source_id": ("other",)}, {"status": "paused"},
                       {"grid": 39.}, {"source": 980.}):
            with self.subTest(change=change):
                self.assertIsNone(r.committed_normal_retention(f.native(974., **change), d))

    def test_identical_settings_preserve_fixed_minimum_phase(self):
        f = startup.StartupMinimumConfirmationTests()
        r = f.prepared()
        d = f.response(r)
        before = deepcopy(r.__dict__)
        r.update_settings(deepcopy(r.settings))
        self.assertEqual(r.__dict__, before)
        after = f.response(r, 91., grid_at=90.)
        self.assertGreaterEqual(after.current_p1, 7)
        self.assertEqual(r._uncertainty_stops, 0)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 119.8)

    def test_real_dispatch_uses_native_counter_not_provider_ack_comparison(self):
        r, f = self.normal()
        proposal = r.evaluate(f.native(995.))
        self.assertTrue(proposal.should_call_service)
        inp = f.native(995.)
        self.assertTrue(r.begin_submission(proposal, 995., inp=inp))
        record = deepcopy(r._dispatch_records[proposal.plan_id]["ev_owner"])
        # A provider clock rounded to a second is older than the HTTP ACK,
        # while this genuine receiver observation advanced after dispatch.
        event = f.native(995.3, current=14.1, source=995.,
            charger_report_provenance=("receiver", "max_terminals", 182),
            charger_report_ts=EPOCH+995.2,
            charger_observation_report_times=((182, EPOCH+995.2), (183, EPOCH+65.8)),
            charger_observation_sequences=((182, 3), (183, 1), (184, 0), (185, 0)))
        r._observe_ev_clock(event)
        self.assertTrue(r._ev_vector_after_owner(record))
        old = replace(event, charger_report_ts=inp.charger_report_ts,
            charger_observation_report_times=inp.charger_observation_report_times,
            charger_observation_sequences=inp.charger_observation_sequences)
        shadow = deepcopy(r)
        shadow._observe_ev_clock(old)
        self.assertFalse(shadow._ev_vector_after_owner(record))
        r.acknowledge(proposal, 997.)
        self.assertEqual(r.last_set_limit_ts, 995.)
        self.assertEqual(r._committed_normal.lease_expires, 1055.)

    def test_invalid_dispatch_never_debits_attempt_but_zero_still_dispatches(self):
        r, f = self.normal()
        d = r.evaluate(f.native(995.))
        invalid = f.native(995., source=1002.)
        before = (r._low_grid_attempt_at, r._normal_shed_attempt_at, r._overload_minimum_attempted_at)
        self.assertFalse(r.begin_submission(d, 995., inp=replace(invalid,
            charger_report_ts=EPOCH+1002., charger_observation_report_times=((182, EPOCH+1002.),))))
        self.assertEqual(before, (r._low_grid_attempt_at, r._normal_shed_attempt_at, r._overload_minimum_attempted_at))
        zero = replace(d, current_p1=0, command_urgency="zero")
        self.assertTrue(r.begin_submission(zero, 995., inp=invalid))

    def test_late_positive_ack_cannot_reopen_a_newer_terminal_source_stop(self):
        r, f = self.normal()
        d = r.evaluate(f.native(995.))
        self.assertTrue(r.begin_submission(d, 995., inp=f.native(995.)))
        stop = r.evaluate(f.native(996., data_valid=False, data_error="synthetic_source_fault"))
        self.assertEqual(stop.current_p1, 0)
        r.acknowledge(d, 997.)
        self.assertEqual(r.last_target[0], 14)  # truthful old HTTP acceptance
        self.assertEqual(r.last_set_limit_ts, 995.)
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._committed_normal)
        self.assertIsNone(r.committed_normal_retention(f.native(998.), d))

    def test_ambiguous_report_keeps_numeric_state_but_no_measurement_credit(self):
        r, f = self.normal()
        old = r._ev_established_value
        inp = f.native(973., current=14.1, winner=183,
            charger_observation_evidence_credit=((182, True), (183, False), (184, True), (185, True)))
        d = r.evaluate(inp)
        self.assertEqual(d.current_p1, 14)
        self.assertEqual(r._ev_established_value, old)
        self.assertFalse(r._ev_clock_credit)
        self.assertFalse(r._ev_advanced_since(r._accepted_control_ev_ts, "_accepted_control_ev_ts"))
        held = r.committed_normal_retention(inp, d)
        self.assertIsNotNone(held)
        self.assertEqual((held.current_p1, held.command), (14, None))

    def test_lost_or_malformed_native_evidence_is_not_a_new_normal_certificate(self):
        r, f = self.normal()
        d = r.evaluate(f.native(995.))
        for credit in (((182, "false"),), ((182, True), (182, False))):
            with self.subTest(credit=credit):
                inp = f.native(995., charger_observation_evidence_credit=credit)
                self.assertIsNone(r.committed_normal_retention(inp, d))

    def test_automatic_policy_bounds_are_validated_without_clamping_manual_mode(self):
        settings = logic.RegulatorSettings(automatic_overload_cycles=True, peak_only_response=True)
        for values in ({"min_charge_amps": 8}, {"max_charge_amps": 6}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                logic.OneFuseRegulator(replace(settings, **values))
        logic.OneFuseRegulator(replace(settings, automatic_overload_cycles=False, min_charge_amps=8))

    def test_expired_ready_observation_can_choose_only_an_independent_lower_offer(self):
        frame = cached.CachedNativeNormalTests().frame
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False,
            disconnected_readiness=True, peak_only_response=True, overload_enabled=True,
            automatic_overload_cycles=True, nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, measurement_settle_seconds=25,
            enable_soft_guard=False, enable_pause_resume=False))
        inp = frame(100., status="disconnected", grid=5., current=.02, receipt=None, sequence=0)
        d = r.evaluate(inp)
        self.assertEqual(d.current_p1, 16)
        self.assertTrue(r.begin_submission(d, 100., inp=inp))
        r.acknowledge(d, 100.)
        waiting = r.evaluate(frame(105., status="awaiting_start", grid=21., current=16., receipt=90., sequence=1))
        self.assertEqual((waiting.mode, waiting.current_p1), ("readiness_wait", 16))
        self.assertAlmostEqual(waiting.readiness_deadline_ts, 130.)
        lower = r.evaluate(frame(130., status="awaiting_start", grid=11., current=16., receipt=90., sequence=1))
        self.assertEqual((lower.mode, lower.current_p1, lower.command), ("ordinary_lower_offer", 7, "set_limit"))
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._ready_owner)
        self.assertIsNone(r._ordinary_lower.accepted_at)
        self.assertEqual(r._ordinary_lower.previous, 16)
        self.assertLessEqual(lower.grid_l1+lower.current_p1, 23.)

    def test_old_overload_permission_states_do_not_block_independent_normal_cold_start(self):
        frame = cached.CachedNativeNormalTests().frame
        for state, used, reason in (("exhausted", 1800., "cumulative_budget_exhausted"),
                ("blocked", 55., "source_control_invalid"),
                ("blocked", 55., "hard_peak_historical"),
                ("blocked", 55., "source_unavailable")):
            with self.subTest(state=state, reason=reason):
                settings = logic.RegulatorSettings(active_control=True, dry_run=False,
                    automatic_overload_cycles=True, peak_only_response=True, overload_enabled=True,
                    nominal_import_only_fallback=True, bounded_nominal_shedding=True,
                    enable_pause_resume=False, enable_soft_guard=False)
                initial = logic.OneFuseRegulator(settings).export_overload_state()
                initial.update(used_seconds=4000., failure_reason="response_timeout")
                initial["automatic_cycle"].update(state=state, used_seconds=used, failure_reason=reason)
                r = logic.OneFuseRegulator(settings)
                self.assertTrue(r.import_overload_state(initial))
                first = r.evaluate(frame(0., status="awaiting_start", grid=5., current=0., receipt=None, sequence=0))
                r.acknowledge(first, 0.)
                normal = r.evaluate(frame(60., status="awaiting_start", grid=5., current=0., receipt=59., sequence=1))
                self.assertEqual(normal.current_p1, 16)
                after = r.export_overload_state()
                self.assertEqual(after["used_seconds"], 4000.)
                self.assertEqual(after["failure_reason"], "response_timeout")
                self.assertEqual((after["automatic_cycle"]["state"], after["automatic_cycle"]["used_seconds"],
                    after["automatic_cycle"]["failure_reason"]), (state, used, reason))

    def test_failed_actual_lower_is_not_an_active_owner_in_a_later_physical_session(self):
        f = startup.fixtures.ExpiredStartHandoffTests()
        r = f.expired_start()
        inp = f.expired_frame()
        lower = r.evaluate(inp)
        self.assertEqual(lower.current_p1, 7)
        self.assertTrue(r.begin_submission(lower, 90., inp=inp))
        counts = r._uncertainty_stops
        self.assertFalse(r.reject_submission(lower, 90.1))  # no reserved recovery debit
        self.assertEqual(r._uncertainty_stops, counts)
        self.assertNotIn(lower.plan_id, r._dispatch_records)
        self.assertIsNone(r._committed_normal)  # outcome remains UNKNOWN
        self.assertFalse(r.reject_unsubmitted(lower, 90.1))
        invalid = f.expired_frame(90.2, data_valid=False, data_error="synthetic_transport_uncertainty")
        zero = r.evaluate(invalid)
        self.assertEqual(zero.current_p1, 0)
        self.assertTrue(r.begin_submission(zero, 90.2, inp=invalid))
        r.acknowledge(zero, 90.3)
        r.evaluate(f.frame(91., status="disconnected", grid=5., current=0., receipt=90.8, sequence=2))
        r.evaluate(f.frame(92., status="awaiting_start", grid=5., current=0., receipt=91.8, sequence=3))
        new_input = f.frame(152., status="awaiting_start", grid=5., current=0., receipt=151.8, sequence=4)
        new_offer = r.evaluate(new_input)
        self.assertEqual(new_offer.current_p1, 16)
        self.assertTrue(r.begin_submission(new_offer, 152., inp=new_input))
        r.acknowledge(new_offer, 152.)
        normal_input = f.frame(153., grid=21., current=16., receipt=152.5, sequence=5)
        normal = r.evaluate(normal_input)
        self.assertEqual(normal.current_p1, 16)
        self.assertIsNotNone(r.committed_normal_retention(normal_input, normal))
        self.assertEqual(r._dispatch_records, {})
        # A late reply to the old SDK-entered lower is recorded, but cannot
        # turn UNKNOWN delivery history into a new qualified normal owner.
        r.acknowledge(lower, 154.)
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._committed_normal)
        self.assertEqual(r.last_set_limit_ts, 90.)
        self.assertEqual(r.export_overload_state()["used_seconds"], 3601.)

    def test_failed_dispatch_tombstones_are_bounded_and_evicted_reply_cannot_revive(self):
        r, f = self.normal()
        first = None
        for index in range(80):
            now = 995.+index*31.
            inp = f.native(now)
            d = r.evaluate(inp)
            self.assertTrue(d.should_call_service)
            self.assertTrue(r.begin_submission(d, now, inp=inp))
            if first is None:
                first = d
            r.reject_submission(d, now+.1)
            self.assertFalse(r._dispatch_records)
        self.assertLessEqual(len(r._retired_dispatch_records), 64)
        self.assertNotIn(first.plan_id, r._retired_dispatch_records)
        r.acknowledge(first, 5000.)
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._committed_normal)

    def normal_decrease(self, **options):
        f = startup.fixtures.ExpiredStartHandoffTests()
        r = f.bootstrap(**options)
        normal = r.evaluate(self.frame(66., grid=21.2, current=16.2, receipt=65., sequence=1))
        self.assertEqual(normal.current_p1, 16)
        inp = self.frame(200., grid=24.2, current=16.2, receipt=65., sequence=1)
        lower = r.evaluate(inp)
        self.assertEqual((lower.mode, lower.current_p1), ("normal", 15))
        self.assertTrue(r.begin_submission(lower, 200., inp=inp))
        r.acknowledge(lower, 200.2)
        self.assertAlmostEqual(r._committed_normal.observation_deadline, 225.)
        return r

    def partial_decrease(self, now=210., **extra):
        fields = dict(grid=24.2, current=16.198, receipt=65.1, sequence=1,
            charger_increase_ready=False,
            charger_report_provenance=("receiver", "max_terminals", 183),
            charger_observation_report_times=((182, EPOCH+now), (183, EPOCH+65.1)),
            charger_observation_sequences=((182, int(now)), (183, 1), (184, 0), (185, 0)))
        fields.update(extra)
        return self.frame(now, **fields)

    def test_high_normal_decrease_partial_batch_has_only_its_fixed_process_fence(self):
        r = self.normal_decrease()
        old_lease = r.last_set_limit_ts
        first = r.evaluate(self.partial_decrease())
        self.assertEqual((first.mode, first.current_p1, first.command), ("waiting_for_current_cohort", 15, None))
        self.assertAlmostEqual(first.source_processing_deadline_ts, 211.)
        self.assertIsNone(r._positive_response_ts)
        churn = r.evaluate(self.partial_decrease(210.5))
        self.assertEqual(churn.source_processing_deadline_ts, first.source_processing_deadline_ts)
        self.assertEqual(r.last_set_limit_ts, old_lease)
        expired = r.evaluate(self.partial_decrease(211.01))
        self.assertEqual(expired.current_p1, 0)

    def test_high_normal_publication_and_stronger_guards_share_the_original_end(self):
        r = self.normal_decrease()
        pending = r.evaluate(self.partial_decrease(charger_native_committed=False,
            charger_publication_pending=True))
        self.assertEqual((pending.mode, pending.current_p1, pending.command), ("waiting_for_current_cohort", 15, None))
        for change in (dict(grid=25.001), dict(grid=39.), dict(data_valid=False),
                       dict(control_source_id=("other",)), dict(status="paused")):
            with self.subTest(change=change):
                r = self.normal_decrease()
                d = r.evaluate(self.partial_decrease(**change))
                self.assertNotEqual(d.mode, "waiting_for_current_cohort")
        r = self.normal_decrease()
        late = r.evaluate(self.partial_decrease(225.))
        self.assertEqual(late.current_p1, 0)
        self.assertIsNone(late.source_processing_deadline_ts)

    def test_completed_high_normal_batch_is_independent_proof_after_store_await(self):
        r = self.normal_decrease()
        first = r.evaluate(self.partial_decrease())
        inp = self.frame(212., grid=23.2, current=15.2, receipt=210.2, grid_at=212.,
            sequence=2, charger_report_provenance=("receiver", "max_terminals", 182, 183),
            charger_observation_sequences=((182, 211), (183, 2), (184, 0), (185, 0)))
        before = deepcopy(r.__dict__)
        held = r.committed_normal_retention(inp, first)
        self.assertIsNotNone(held)
        self.assertEqual((held.mode, held.current_p1, held.command), ("normal_continuity", 15, None))
        self.assertIsNone(held.source_processing_deadline_ts)
        self.assertLessEqual(held.house_load_l1+held.current_p1, 23.)
        self.assertEqual(r.__dict__, before)
        repeated = r.committed_normal_retention(inp, held)
        self.assertIsNotNone(repeated)
        self.assertEqual(r.__dict__, before)

    def test_late_lower_after_original_bound_cannot_be_retained_by_pending_fallback(self):
        r = self.normal_decrease(resend_limit_every_seconds=15)
        renewal_input = self.frame(215., grid=24.2, current=16.2, receipt=65., sequence=1)
        renewal = r.evaluate(renewal_input)
        self.assertEqual((renewal.mode, renewal.current_p1, renewal.should_call_service), ("normal", 15, True))
        self.assertTrue(r.begin_submission(renewal, 215., inp=renewal_input))
        r.acknowledge(renewal, 215.2)
        self.assertEqual(r._committed_normal.observation_deadline, 225.)
        first = r.evaluate(self.partial_decrease(224.5))
        self.assertAlmostEqual(first.source_processing_deadline_ts, 225.)
        # The new pair is outside normal even at the accepted15 offer:
        #23.3 -15.2 +15 =23.1 A. The expired phase supplies no extension.
        late = self.frame(226., grid=23.3, current=15.2, receipt=225.2, grid_at=226.,
            sequence=2, charger_report_provenance=("receiver", "max_terminals", 182, 183),
            charger_observation_sequences=((182, 225), (183, 2), (184, 0), (185, 0)))
        self.assertIsNone(r.committed_normal_retention(late, first))

    def test_fresh_normal_pair_after_observation_expiry_retains_only_live_cap_without_mutation(self):
        r = self.normal_decrease(resend_limit_every_seconds=15)
        renewal_input = self.frame(215., grid=24.2, current=16.2, receipt=65., sequence=1)
        renewal = r.evaluate(renewal_input)
        self.assertTrue(r.begin_submission(renewal, 215., inp=renewal_input))
        r.acknowledge(renewal, 215.2)
        self.assertEqual(r._committed_normal.observation_deadline, 225.)
        first = r.evaluate(self.partial_decrease(224.5))
        late = self.frame(226., grid=23.2, current=15.2, receipt=225.2, grid_at=226.,
            sequence=2, charger_report_provenance=("receiver", "max_terminals", 182, 183),
            charger_observation_sequences=((182, 225), (183, 2), (184, 0), (185, 0)))
        before = deepcopy(r.__dict__)
        held = r.committed_normal_retention(late, first)
        self.assertIsNotNone(held)
        self.assertEqual((held.mode, held.current_p1, held.command, held.should_call_service),
                         ("normal_continuity", 15, None, False))
        self.assertAlmostEqual(held.house_load_l1+held.current_p1, 23.)
        self.assertEqual(held.normal_continuity_deadline_ts, 275.)
        # This is current operating permission, not a claim that response
        # happened before225 or that any request renewed the original phase.
        self.assertEqual(r.__dict__, before)
        self.assertEqual(r.last_set_limit_ts, 215.)
        self.assertEqual(r._committed_normal.observation_deadline, 225.)
        self.assertEqual(r._committed_normal.unresolved_cap, 16)

    def test_high_raw_cannot_extend_a_general_connected_status_bridge(self):
        r, f = self.normal()
        first = r.evaluate(f.native(973., status="awaiting_start"))
        self.assertEqual(first.mode, "normal_continuity")
        high = f.native(974., status="awaiting_start", grid=24.2)
        self.assertIsNone(r.committed_normal_retention(high, first))
        d = r.evaluate(high)
        self.assertNotEqual(d.mode, "normal_continuity")


if __name__ == "__main__":
    unittest.main()
