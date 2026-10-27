"""Normal continuation is independent of an observation timer's expiry.

All authorities are reached through public evaluation/submission/ACK calls.
Synthetic receiver vectors preserve their original report clocks; no device,
network or household trace is needed for these regressions.
"""
from copy import deepcopy
import unittest

import test_1fuseloader_idle_raw_recovery as idle
import test_1fuseloader_positive_ramp_owner as native

logic, EPOCH = native.logic, native.EPOCH


class NormalObservationProofTests(unittest.TestCase):
    frame = native.PositiveRampOwnerTests.frame

    def send(self, r, at, grid, current=7.009, *, receipt=65., sequence=3,
             status="charging", accept=True, **metadata):
        inp = self.frame(at, grid=grid, current=current, receipt=receipt,
            sequence=sequence, status=status, charger_event_source_known=True,
            charger_event_source_ts=EPOCH+receipt-.2 if receipt is not None else None,
            **metadata)
        d = r.evaluate(inp)
        if accept and d.should_call_service:
            self.assertTrue(r.begin_submission(d, at, inp=inp), d)
            r.acknowledge(d, at+.1)
        return inp, d

    def nine(self):
        r = idle.IdleRawRecoveryTests().recovered_seven()
        for at in range(65, 101, 5):
            self.send(r, float(at), 13.1 if at < 100 else 13.2)
        self.assertEqual(r.last_target[0], 9)
        return r

    def fourteen(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True,
            dry_run=False, nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, peak_only_response=True,
            automatic_overload_cycles=True, overload_enabled=True,
            measurement_settle_seconds=25, enable_soft_guard=False,
            enable_pause_resume=False))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            failure_reason="response_timeout", exposure_active=False,
            transition_pending=False, minimum_held=False)))
        for at in (0., 60.):
            self.send(r, at, 4.7, .012, receipt=None, sequence=0, status="awaiting_start")
        fields = dict(charger_report_provenance=("receiver", "max_terminals", 182),
            charger_observation_report_times=((182,EPOCH+69.), (183,EPOCH+69.2)),
            charger_observation_sequences=((182,1),(183,1),(184,0),(185,0)))
        for at, raw in ((70.,20.7), (75.,20.7), (90.,20.7), (120.,20.7),
                        (145.,21.9), (146.,24.4), (170.,21.3)):
            self.send(r, at, raw, 16.086, receipt=69., sequence=1, **fields)
        self.assertEqual(r.last_target[0], 14)
        return r

    def test_whole_raw_preempts_observation_only_lower_and_zero(self):
        r = self.nine()
        for at in range(105, 181, 5):
            _, d = self.send(r, float(at), 13.2 if at < 180 else 16.5)
            self.assertEqual(d.current_p1, 9, d)
            self.assertEqual(r.last_target[0], 9)
        self.assertEqual(r._last_input_charger_ts, EPOCH+65.)
        self.assertEqual(r._ev_established_value, 7.009)
        self.assertIsNone(r._positive_response_ts)

    def test_expired_work_has_immutable_original_ledger_and_no_live_watchdog(self):
        r = self.nine()
        self.send(r, 105., 13.2)
        self.assertTrue(r._committed_normal.observation_history)
        ledger = r._committed_normal.observation_history
        self.assertEqual(r._committed_normal.unresolved_cap, 9)
        floor = r._accepted_control_ev_ts
        for at in range(110, 311, 10):
            _, d = self.send(r, float(at), 16.5)
            self.assertEqual(d.current_p1, 9)
            self.assertIsNone(d.positive_response_deadline_ts)
            self.assertIsNone(d.ordinary_response_deadline_ts)
            self.assertIsNone(d.measurement_alignment_remaining_seconds)
            self.assertEqual(r._committed_normal.observation_history, ledger)
            self.assertEqual(r._accepted_control_ev_ts, floor)
        with self.assertRaises(AttributeError):
            ledger[0].deadline = 999.

    def test_joint_lower_certificate_retains_full_unresolved_upper(self):
        r = self.fourteen()
        _, d = self.send(r, 173., 21.3, 16.078, receipt=69.2, sequence=1,
            charger_report_provenance=("receiver", "max_terminals", 183),
            charger_observation_report_times=((182,EPOCH+172.7),(183,EPOCH+69.2)),
            charger_observation_sequences=((182,2),(183,1),(184,0),(185,0)))
        self.assertEqual(d.current_p1, 14, d)
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        self.assertLessEqual(r._committed_normal.house+16, 23.)
        self.assertFalse(r._reduction_response_completed)
        self.assertEqual(r._ev_established_value, 16.086)
        self.assertFalse(r._ev_clock_credit)

    def test_cached_normal_proof_never_creates_ev_subtraction_rise(self):
        r = self.nine()
        self.send(r, 105., 13.2)
        for at in range(110, 231, 10):
            _, d = self.send(r, float(at), 16.5)
            self.assertEqual(d.current_p1, 9, d)
            self.assertLessEqual(r.last_set_limit_ts+60.-at, 60.)
            self.assertEqual(r._ev_established_value, 7.009)
        self.assertEqual(r._committed_normal.unresolved_cap, 9)

    def test_precommand_state_supplies_typed_cache_without_new_response_credit(self):
        r = self.nine()
        self.assertEqual(r._committed_normal.native_floor, 7.009)
        self.send(r, 105., 13.2)
        inp, d = self.send(r, 180., 16.5)
        proof = r._normal_authority_evidence(inp)
        self.assertEqual(d.current_p1, 9)
        self.assertEqual(proof.kind, logic.NormalProofKind.STABLE_NATIVE)
        self.assertTrue(proof.permits_renew)
        self.assertFalse(proof.retires_measurement)
        self.assertEqual(r._committed_normal.unresolved_cap, 9)
        self.assertEqual(r._last_input_charger_ts, EPOCH+65.)

    def test_new_partial_rise_before_later_grid_cannot_seed_cached_joint_floor(self):
        r = native.PositiveRampOwnerTests().bootstrap()
        self.send(r, 70., 22.6, 1.8, receipt=67., sequence=1, grid_at=69.)
        self.assertEqual(r._positive_ramp_deadline, 92.)
        inp, _ = self.send(r, 91.9, 11., 6.4, receipt=91.7,
            sequence=2, grid_at=91.5, accept=False)
        self.assertIsNone(r._committed_normal.native_floor)
        self.assertIsNone(r._normal_authority_evidence(inp))
        _, expired = self.send(r, 92.1, 11., 6.4, receipt=91.7,
            sequence=2, grid_at=91.5, accept=False)
        self.assertLess(expired.current_p1, 14)

    def test_store_arrival_spends_first_original_positive_mismatch_clock_once(self):
        r = idle.IdleRawRecoveryTests().recovered_seven()
        for at in range(65, 96, 5):
            self.send(r, float(at), 12., 6.4,
                receipt=64.5 if at < 95 else 94.5, sequence=3 if at < 95 else 4)
        self.assertEqual((r.last_target[0], r._positive_cap), (16,16))
        self.assertIsNone(r._positive_ramp_deadline)
        _, before = self.send(r, 99., 12., 6.4, receipt=94.5, sequence=4, accept=False)
        self.assertEqual(r._positive_cap, 16)
        self.assertIsNone(r._positive_ramp_deadline)
        accepted, target = r.last_set_limit_ts, r.last_target
        first = self.frame(101.5, grid=21., current=6.4, receipt=94.5,
            sequence=4, grid_at=100.5, charger_event_source_known=True,
            charger_event_source_ts=EPOCH+94.3)
        held = r.committed_normal_retention(first, before)
        self.assertIsNotNone(held)
        self.assertEqual((held.current_p1, held.command, held.should_call_service), (16,None,False))
        # The report is one second old at this I/O continuation. Its first
        # source origin100.5, not fresh evaluation101.5, owns the25s clock.
        self.assertEqual(held.normal_continuity_deadline_ts, 125.5)
        self.assertEqual((r._positive_skew_since, r._positive_ramp_deadline), (100.5,125.5))
        later = self.frame(110., grid=22., current=6.4, receipt=94.5,
            sequence=4, charger_event_source_known=True, charger_event_source_ts=EPOCH+94.3)
        again = r.committed_normal_retention(later, before)
        self.assertEqual(again.normal_continuity_deadline_ts, 125.5)
        self.assertEqual((r.last_set_limit_ts, r.last_target), (accepted,target))
        expired = self.frame(125.5, grid=22., current=6.4, receipt=94.5,
            sequence=4, charger_event_source_known=True, charger_event_source_ts=EPOCH+94.3)
        self.assertIsNone(r.committed_normal_retention(expired, before))

    def test_fresh_raw_whole_offer_can_rise_without_claiming_ev_response(self):
        r = self.nine()
        self.send(r, 105., 13.2)
        ledger = r._committed_normal.observation_history
        _, d = self.send(r, 110., 6.8)
        self.assertEqual((d.command, d.current_p1), ("set_limit", 16), d)
        self.assertEqual(d.house_load_basis, "raw_grid_bounded_recovery")
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        self.assertEqual(r._committed_normal.observation_history, ledger)
        self.assertEqual(r._last_input_charger_ts, EPOCH+65.)
        self.assertIsNone(r._positive_response_ts)
        saved = r.export_overload_state()
        self.assertEqual((saved["normal_control"]["cap"],
            saved["normal_control"]["previous_offer"]), (16,16))
        restored = logic.OneFuseRegulator(deepcopy(r.settings))
        self.assertTrue(restored.import_overload_state(saved))
        self.assertEqual(restored._uncertainty_previous_offer, 16)
        self.send(r, 115., 6.8)
        self.assertEqual(r._committed_normal.unresolved_cap, 16)

    def test_real_post_dispatch_response_releases_archived_upper(self):
        r = self.nine()
        self.send(r, 105., 13.2)
        self.assertTrue(r._committed_normal.observation_history)
        self.send(r, 110., 18.2, 9.01, receipt=109., sequence=4)
        self.assertFalse(r._committed_normal.observation_history)
        self.assertIsNone(r._committed_normal.unresolved_cap)
        self.assertEqual(r._last_input_charger_ts, EPOCH+109.)

    def test_coherent_pre_dispatch_event_does_not_claim_response(self):
        r = self.nine()
        inp, d = self.send(r, 105., 13.2, receipt=99., sequence=4)
        self.assertEqual(d.current_p1, 9)
        self.assertEqual(r._last_input_charger_ts, EPOCH+99.)
        self.assertFalse(r._ev_advanced_since(r._accepted_control_ev_ts, "_accepted_control_ev_ts"))
        self.assertEqual(r._committed_normal.unresolved_cap, 9)
        self.assertTrue(r._committed_normal.observation_history)
        self.assertFalse(r._normal_authority_evidence(inp).retires_measurement)

    def test_new_peak_keeps_archived_upper_after_normal_lower_work_closes(self):
        r = self.fourteen()
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        inp = self.frame(174., grid=29., current=16.086, receipt=69., sequence=1)
        r.observe_peak_response(inp)
        self.assertIsNotNone(r._peak_response)
        self.assertEqual(r._peak_response.previous_authority, 16)

    def test_raw_baseline_and_rise_budget_the_archived_upper(self):
        r = self.fourteen()
        inp, _ = self.send(r, 173., 8., 7.009, receipt=69.2, sequence=1,
            charger_report_provenance=("receiver", "max_terminals",183),
            charger_observation_report_times=((182,EPOCH+172.7),(183,EPOCH+69.2)),
            charger_observation_sequences=((182,2),(183,1),(184,0),(185,0)))
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        self.assertFalse(r._ev_clock_credit)
        # 8+14 and 8+15 fit23, but the unresolved16 means neither RAW-only
        # continuation nor an independently RAW-budgeted15 rise is proven.
        self.assertIsNone(r._clock_baseline_raw_hold(173.,8.,7.009,"charging"))
        self.assertIsNone(r._raw_budget_recovery_candidate(173.,8.,7.009,"charging",inp))

    def test_response_completion_reuses_all_source_and_lease_guards(self):
        base = self.nine()
        self.send(base, 105., 13.2)
        cases = dict(native=dict(charger_native_committed=False),
            binding=dict(control_source_id="other_meter"),
            receiver=dict(charger_report_provenance=("other", "max_terminals",182,183)),
            untrusted=dict(charger_current_trusted=False),
            fault=dict(data_valid=False, data_error="synthetic_fault"),
            snapshot=dict(charger_snapshot_verified=True),
            pending=dict(charger_publication_pending=True),
            future=dict(charger_event_source_ts=EPOCH+120.),
            expired=dict(now_ts=200., wall_now_ts=EPOCH+200., grid_report_ts=EPOCH+200.))
        for name, fields in cases.items():
            with self.subTest(name=name):
                r = deepcopy(base)
                inp = self.frame(110., grid=18.2, current=9.01, receipt=109., sequence=4,
                    charger_event_source_known=True, charger_event_source_ts=EPOCH+108.8)
                for field, value in fields.items():
                    setattr(inp, field, value)
                r._observe_ev_clock(inp)
                r._last_input_grid_ts = inp.grid_report_ts
                r._last_input_charger_ts = inp.charger_report_ts
                r._last_input_wall_now_ts = inp.wall_now_ts
                self.assertIsNone(r._normal_authority_evidence(inp, response_only=True))
                before = deepcopy(r._committed_normal)
                r._complete_normal_observation_response(inp)
                self.assertEqual(r._committed_normal, before)

    def test_archived_uncertainty_restore_cannot_reconstruct_live_authority(self):
        r = self.nine()
        self.send(r, 105., 13.2)
        state = r.export_overload_state()
        self.assertTrue(state["normal_control"]["response_pending"])
        self.assertEqual((state["normal_control"]["cap"],
            state["normal_control"]["previous_offer"]), (9,9))
        fresh = logic.OneFuseRegulator(deepcopy(r.settings))
        self.assertTrue(fresh.import_overload_state(state))
        self.assertIsNone(fresh._committed_normal)
        self.assertIsNone(fresh.last_target)
        self.assertTrue(fresh._restart_required)
        self.assertEqual((fresh.export_overload_state()["used_seconds"],
            fresh.export_overload_state()["failure_reason"]), (3601.,"response_timeout"))
        _, d = self.send(fresh, 110., 13.2)
        self.assertFalse(d.current_p1 > 7 and d.should_call_service)


if __name__ == "__main__":
    unittest.main()
