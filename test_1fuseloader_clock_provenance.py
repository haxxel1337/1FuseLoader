"""Explicit reporting clocks and actual per-conductor event sequences.

All observations, receipt clocks and device identities are synthetic. Local
arrival proof never claims that a vehicle physically acquired data after ACK.
"""
from __future__ import annotations

import unittest

import test_1fuseloader_b22_episode_ownership as fixtures

logic = fixtures.logic
EPOCH = fixtures.EPOCH
SOURCE = fixtures.SOURCE
UNSET = object()
IDS = (182, 183, 184, 185)


class ClockProvenanceTests(unittest.TestCase):
    def regulator(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True,
            dry_run=False, overload_enabled=True, nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, measurement_settle_seconds=25,
            enable_soft_guard=False, enable_pause_resume=False))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=2400.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        return r

    def step(self, r, now, grid, current, *, et=UNSET, gt=None, family="native_terminal_receipt",
             winner=(183,), vector=None, sequences=None, source_known=False,
             source_ts=UNSET, accept=True, **metadata):
        report = now if et is UNSET else et
        values = dict(now_ts=now, wall_now_ts=EPOCH+now, status="charging",
            grid_l1=grid, charger_current=current, grid_report_ts=EPOCH+(now if gt is None else gt),
            charger_report_ts=None if report is None else EPOCH+report,
            control_source_id=SOURCE, charger_report_family=family,
            charger_report_provenance=("receiver", "max_terminals", *winner),
            charger_observation_report_times=None if vector is None else tuple((item,EPOCH+stamp) for item,stamp in vector),
            charger_observation_sequences=sequences, charger_event_source_known=source_known,
            charger_event_source_ts=(None if source_ts is UNSET or source_ts is None else EPOCH+source_ts))
        values.update(metadata)
        d = r.evaluate(logic.RegulatorInput(**values))
        if accept and d.should_call_service:
            r.acknowledge(d, now)
        return d

    def assert_history(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2400.)
        self.assertNotIn("trial_epoch", state)

    def test_unknown_receiver_bootstrap_then_single_post_ack_near_cap_event(self):
        r = self.regulator()
        zero_sequences = tuple((item,0) for item in IDS)
        first = self.step(r, 0, 2.2, .02, et=None, vector=(), sequences=zero_sequences,
                          status="awaiting_start")
        self.assertEqual(first.current_p1, 0)
        start = self.step(r, 60, 2.2, .02, et=None, vector=(), sequences=zero_sequences,
                          status="awaiting_start")
        self.assertEqual(start.current_p1, 16)
        self.assertIsNone(r._positive_ev_ts)  # No fabricated event timestamp.
        response = self.step(r, 65, 2.2, 16.1, gt=64, et=65,
            vector=((183,65),), sequences=((182,0),(183,1),(184,0),(185,0)))
        self.assertEqual(response.current_p1, 16)
        later = self.step(r, 66, 18.3, 16.1, et=65,
            vector=((183,65),), sequences=((182,0),(183,1),(184,0),(185,0)))
        self.assertEqual(later.current_p1, 16)
        self.assertIsNone(r._positive_cap)
        for now in range(90, 550, 30):
            d = self.step(r, now, 18.3, 16.1, et=65,
                vector=((183,65),), sequences=((182,0),(183,1),(184,0),(185,0)))
            self.assertEqual(d.current_p1, 16)
            self.assertNotEqual(d.mode, "unavailable")
        self.assert_history(r)

    def explicit_running_sixteen(self):
        r = self.regulator()
        # Legacy initial takeover, then establish a genuine receiver clock.
        self.step(r, 100, 21.7, 16.2, family="legacy",
                  charger_report_provenance=None)
        self.step(r, 101, 2.0, 16.2, et=101)
        self.step(r, 102, 21.7, 16.2, et=102)
        return r

    def test_native_to_older_vendor_baseline_retains_only_whole_raw_safe_offer(self):
        r = self.explicit_running_sixteen()
        before = r.export_overload_state()
        held = self.step(r, 103, 7.0, 16.2, et=90, family="vendor_event")
        self.assertEqual((held.mode, held.current_p1, held.command), ("low_grid_raw_hold",16,None))
        self.assertFalse(held.should_call_service)
        self.assertEqual(r.export_overload_state(), before)
        unsafe = self.step(r, 104, 7.0001, 16.2, et=90, family="vendor_event")
        self.assertNotEqual(unsafe.house_load_basis, "whole_authority_raw_bound_without_clock_credit")
        self.assertLess(unsafe.current_p1, 16)

    def test_switchback_keeps_real_same_domain_replay_detection(self):
        r = self.explicit_running_sixteen()
        self.step(r, 103, 2.0, 16.2, et=90, family="vendor_event")
        replay = self.step(r, 104, 2.0, 16.2, et=101, family="native_terminal_receipt")
        self.assertEqual(replay.current_p1, 0)
        self.assertIn("charger_clock_regressed", replay.reason)
        self.assert_history(r)

    def test_normal_owner_requires_its_ack_wall_in_the_new_clock_domain(self):
        r = self.explicit_running_sixteen()
        lower = self.step(r, 110, 26.5, 16.2, et=109)
        self.assertEqual((lower.mode,lower.current_p1), ("normal_shedding",7))
        deadline = lower.nominal_response_deadline_ts
        changed = self.step(r,111,26.5,16.2,et=90,family="vendor_event")
        self.assertEqual((changed.mode,changed.current_p1), ("normal_shedding",7))
        pre_ack = self.step(r,112,20.,7.1,et=105,family="vendor_event",
                            source_known=True,source_ts=105)
        self.assertEqual((pre_ack.mode,pre_ack.current_p1), ("normal_shedding",7))
        self.assertEqual(pre_ack.nominal_response_deadline_ts, deadline)
        self.assertIsNone(r._normal_shed_response_ts)
        valid = self.step(r,113,20.,7.1,et=111,family="vendor_event",
                          source_known=True,source_ts=111)
        self.assertEqual((valid.mode,valid.current_p1), ("normal_shedding_complete",7))
        self.assert_history(r)

    def test_low_grid_owner_does_not_compare_vendor_stamp_to_an_old_native_floor(self):
        r = self.explicit_running_sixteen()
        lower = self.step(r,110,5.1,16.2,et=109)
        self.assertEqual((lower.mode,lower.current_p1), ("low_grid_alignment",7))
        deadline = lower.nominal_response_deadline_ts
        changed = self.step(r,111,2.,5.28,et=90,family="vendor_event")
        self.assertEqual((changed.current_p1,changed.command), (7,None))
        pre_ack = self.step(r,112,5.1,5.28,et=105,family="vendor_event",
                            source_known=True,source_ts=105)
        self.assertEqual(pre_ack.current_p1,7)
        self.assertNotEqual(pre_ack.mode,"low_grid_alignment_complete")
        self.assertEqual(pre_ack.nominal_response_deadline_ts,deadline)
        valid = self.step(r,113,5.1,5.28,et=111,family="vendor_event",
                          source_known=True,source_ts=111)
        self.assertEqual((valid.mode,valid.current_p1), ("low_grid_alignment_complete",7))
        self.assert_history(r)

    def test_positive_owner_rejects_vendor_105_before_ack_110_then_accepts_111(self):
        r = self.regulator()
        self.step(r,100,23.,7.1,family="legacy",charger_report_provenance=None)
        rise = self.step(r,105,17.4,7.1,gt=104,et=100,family="legacy",
                         charger_report_provenance=None,accept=False)
        r.acknowledge(rise,110)
        self.step(r,111,5.,7.1,et=100)
        self.step(r,112,5.,7.1,et=90,family="vendor_event")
        pre_ack = self.step(r,113,17.4,8.,et=105,family="vendor_event",
                            source_known=True,source_ts=105)
        self.assertEqual(pre_ack.current_p1,12)
        self.assertIsNone(r._positive_response_current)
        valid = self.step(r,114,17.4,8.,et=111,family="vendor_event",
                          source_known=True,source_ts=111)
        self.assertEqual(valid.current_p1,12)
        self.assertEqual(r._positive_response_current,8.)
        self.assert_history(r)

    def test_experimental_owner_keeps_one_deadline_and_strict_post_ack_source(self):
        r = self.explicit_running_sixteen()
        values = dict(now_ts=103,wall_now_ts=EPOCH+103,status="charging",grid_l1=21.7,
            charger_current=16.2,grid_report_ts=EPOCH+103,charger_report_ts=EPOCH+102,
            control_source_id=SOURCE,charger_report_family="native_terminal_receipt",
            charger_report_provenance=("receiver","max_terminals",183))
        inp = logic.RegulatorInput(**values)
        r.prepare_overload_trial(inp,trial_id="synthetic-period",started_at_utc="2023-11-14T22:15:03+00:00")
        r.stage_overload_trial_activation("synthetic-period",inp)
        r.commit_overload_trial_activation("synthetic-period",inp)
        lower = self.step(r,110,38.,16.2,et=109)
        self.assertEqual((lower.mode,lower.current_p1), ("overload_shedding",7))
        self.step(r,111,38.,16.2,et=90,family="vendor_event")
        pre_ack = self.step(r,112,29.,7.1,et=105,family="vendor_event",source_known=True,source_ts=105)
        self.assertEqual((pre_ack.mode,pre_ack.current_p1), ("overload_shedding",7))
        self.assertEqual(pre_ack.overload_transition_remaining_seconds,8)
        valid = self.step(r,113,29.,7.1,et=111,family="vendor_event",source_known=True,source_ts=111)
        self.assertEqual((valid.mode,valid.current_p1), ("overload_minimum",7))
        self.assertGreaterEqual(r.effective_overload_used_seconds,3)
        self.assertEqual(r.export_overload_state()["failure_reason"],"response_timeout")

    def test_first_post_ack_winner_with_known_pre_ack_source_does_not_confirm(self):
        r = self.regulator()
        zeros=tuple((item,0) for item in IDS)
        self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,60,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        d = self.step(r,65,18.3,16.1,et=65,vector=((183,65),),
            sequences=((182,0),(183,1),(184,0),(185,0)),source_known=True,source_ts=59)
        self.assertIsNone(r._positive_response_current)
        self.assertNotEqual(d.house_load_basis,"prior_coherent_house_ev_first_hold")

    def test_first_post_ack_winner_with_known_post_ack_source_completes(self):
        r = self.regulator()
        zeros=tuple((item,0) for item in IDS)
        self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,60,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,65,2.2,16.1,gt=64,et=65,vector=((183,65),),
            sequences=((182,0),(183,1),(184,0),(185,0)),source_known=True,source_ts=64.5)
        d=self.step(r,66,18.3,16.1,et=65,vector=((183,65),),
            sequences=((182,0),(183,1),(184,0),(185,0)),source_known=True,source_ts=64.5)
        self.assertEqual(d.current_p1,16)
        self.assertIsNone(r._positive_cap)

    def test_known_receiver_sequences_allow_fresh_maximum_winner_churn(self):
        r = self.regulator()
        zeros=tuple((item,0) for item in IDS)
        self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,60,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        first=self.step(r,65,2.2,12.,gt=64,et=65,winner=(182,),vector=((182,65),),
            sequences=((182,1),(183,0),(184,0),(185,0)))
        self.assertEqual(first.current_p1,16)
        changed=self.step(r,66,2.2,14.,gt=64,et=66,winner=(183,),vector=((182,65),(183,66)),
            sequences=((182,1),(183,1),(184,0),(185,0)))
        self.assertEqual(changed.current_p1,16)
        self.assertEqual(r._positive_response_current,14.)
        final=self.step(r,67,18.3,16.1,gt=67,et=66.5,winner=(182,),vector=((182,66.5),(183,66)),
            sequences=((182,2),(183,1),(184,0),(185,0)))
        self.assertEqual(final.current_p1,16)
        self.assertIsNone(r._positive_cap)

    def test_nonwinner_event_cannot_freshen_an_old_maximum_response(self):
        r=self.regulator()
        zeros=tuple((item,0) for item in IDS)
        self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,60,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        # Only another conductor changed; winning183 has no actual receipt.
        d=self.step(r,65,2.2,.02,et=None,vector=((182,65),),
            sequences=((182,1),(183,0),(184,0),(185,0)),status="awaiting_start")
        self.assertIsNone(r._positive_response_current)
        self.assertEqual(d.current_p1,16)
        self.assertFalse(d.should_call_service)

    def test_quiet_period_defers_new_authority_without_spending_a_retry(self):
        r=self.regulator()
        # Legacy frame keeps this test focused on the pre-submission gate.
        self.step(r,0,23.,7.1,family="legacy",charger_report_provenance=None)
        deferred=self.step(r,10,17.4,7.1,family="legacy",charger_report_provenance=None,
                           charger_increase_ready=False)
        self.assertEqual((deferred.mode,deferred.current_p1,deferred.should_call_service),
                         ("waiting_for_stable_current_frame",12,False))
        self.assertEqual(r.last_target[0],7)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        allowed=self.step(r,10.25,17.4,7.1,family="legacy",charger_report_provenance=None,
                          charger_increase_ready=True)
        self.assertEqual((allowed.current_p1,allowed.should_call_service),(12,True))
        protective=self.step(r,11,27.,12.1,family="legacy",charger_report_provenance=None,
                             charger_increase_ready=False)
        self.assertLess(protective.current_p1,12)
        self.assertTrue(protective.should_call_service)

    def test_unknown_clock_restart_does_not_bypass_pause_or_loss_of_headroom(self):
        for status,grid in (("paused",2.2),("completed",2.2),("awaiting_start",17.)):
            with self.subTest(status=status,grid=grid):
                r=self.regulator();zeros=tuple((item,0) for item in IDS)
                self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
                d=self.step(r,60,grid,.02,et=None,vector=(),sequences=zeros,status=status)
                self.assertEqual(d.current_p1,0)
                self.assertNotEqual(d.command,"resume")

    def test_metadata_baseline_cannot_introduce_a_different_ev_subtraction(self):
        r=self.explicit_running_sixteen()
        d=self.step(r,103,20.,5.,et=90,family="vendor_event")
        self.assertEqual(d.current_p1,0)
        self.assertEqual(d.house_load_l1,20.)
        self.assert_history(r)

    def test_raw_hold_preserves_pending_state_and_restore_has_no_positive_lease(self):
        r=self.explicit_running_sixteen()
        self.step(r,110,5.1,16.2,et=109)
        before=r.export_overload_state()
        held=self.step(r,111,2.,5.28,et=90,family="vendor_event")
        self.assertEqual((held.current_p1,held.command),(7,None))
        self.assertEqual(r.export_overload_state(),before)
        restored=self.regulator()
        self.assertTrue(restored.import_overload_state(before))
        d=self.step(restored,112,2.,5.28,et=90,family="vendor_event")
        self.assertEqual(d.current_p1,0)
        self.assertNotEqual(d.house_load_basis,"whole_authority_raw_bound_without_clock_credit")
        self.assertGreaterEqual(d.measurement_uncertainty_stops,1)

    def test_real_per_conductor_sequence_regression_still_stops(self):
        r=self.regulator();zeros=tuple((item,0) for item in IDS)
        self.step(r,0,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,60,2.2,.02,et=None,vector=(),sequences=zeros,status="awaiting_start")
        self.step(r,65,2.2,16.1,gt=64,et=65,vector=((183,65),),
            sequences=((182,0),(183,1),(184,0),(185,0)))
        replay=self.step(r,66,18.3,16.1,et=66,vector=((183,66),),
            sequences=((182,0),(183,0),(184,0),(185,0)))
        self.assertEqual(replay.current_p1,0)
        self.assertIn("charger_clock_regressed",replay.reason)
        self.assert_history(r)

    def test_quiet_defer_does_not_reserve_a_pending_normal_recovery_attempt(self):
        r=self.regulator()
        state=r.export_overload_state()
        state["normal_control"].update(cap=7,previous_offer=16,stops=1,
            stop_recorded=True,restart=True,retry_wait_seconds=0.)
        self.assertTrue(r.import_overload_state(state))
        self.step(r,0,2.,.02,status="awaiting_start",family="legacy",charger_report_provenance=None)
        deferred=self.step(r,60,2.,.02,status="awaiting_start",family="legacy",charger_report_provenance=None,
            charger_increase_ready=False,accept=False)
        self.assertFalse(deferred.should_call_service)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.assertEqual(r._uncertainty_stops,1)
        admitted=self.step(r,60.25,2.,.02,status="awaiting_start",family="legacy",charger_report_provenance=None,
            charger_increase_ready=True,accept=False)
        self.assertTrue(admitted.should_call_service)
        self.assertTrue(r._uncertainty_retry_submission_pending)
        self.assertEqual(r._uncertainty_stops,1)


if __name__ == "__main__":
    unittest.main()
