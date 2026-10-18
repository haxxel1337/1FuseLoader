"""Conservative lower-cap response to asynchronous positive current reports.

Synthetic offline cases keep accepted API limits separate from measured EV
draw. No Home Assistant, network or private household history is used.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_positive_shedding_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class PositiveSheddingTests(unittest.TestCase):
    def sample(self, r, now, grid, ev, *, grid_ts=None, ev_ts=None,
               status="charging", accept=False, **extra):
        inp = dict(now_ts=now, wall_now_ts=1000+now, status=status,
                   grid_l1=grid, charger_current=ev,
                   grid_report_ts=1000+now if grid_ts is None else grid_ts,
                   charger_report_ts=1000+now if ev_ts is None else ev_ts)
        inp.update(extra)
        d = r.evaluate(logic.RegulatorInput(**inp))
        if accept:r.acknowledge(d,now)
        return d

    def pending(self, **options):
        options.setdefault("measurement_settle_seconds",25)
        options.setdefault("overload_enabled",True)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True,dry_run=False,**options))
        self.sample(r,0,3.3,0.012,status="awaiting_start",accept=True)
        d = self.sample(r,60,3.3,0.012,status="awaiting_start",accept=True)
        self.assertEqual((d.current_p1,r.last_target[0],r._positive_cap),(16,16,16))
        d = self.sample(r,70,3.3,2.0,grid_ts=1060,ev_ts=1070)
        self.assertEqual((d.mode,d.current_p1),("settling",16))
        self.assertEqual(r._positive_skew_since,70)
        return r

    def shedding(self, r, *, now=72, grid=15.5, ev=3.96, **extra):
        return self.sample(r,now,grid,ev,grid_ts=1000+now-0.1,
                           ev_ts=1000+now,**extra)

    def test_startup_mixed_sources_request_seven_without_zero(self):
        r = self.pending()
        d = self.shedding(r)
        self.assertEqual((d.mode,d.command,d.current_p1),("alignment_shedding","set_limit",7))
        self.assertTrue(d.should_call_service)
        self.assertEqual(d.house_load_basis,"raw_grid_positive_reduction")
        self.assertEqual(d.house_load_l1,15.5)
        self.assertEqual(d.available_l1,7)
        self.assertEqual(d.measurement_alignment_remaining_seconds,23)
        self.assertEqual(r.last_target[0],16)
        self.assertEqual(r._positive_cap,16)

    def test_full_raw_buffer_boundary_six_and_zero(self):
        for grid,target in ((17.0,6),(17.00001,0)):
            with self.subTest(grid=grid):
                d = self.shedding(self.pending(),grid=grid)
                self.assertEqual(d.current_p1,target)
                self.assertEqual(d.mode,"alignment_shedding" if target else "unavailable")

    def test_lower_configured_maximum_is_honored(self):
        r = self.pending()
        r.settings.max_charge_amps=6
        d = self.shedding(r)
        self.assertEqual((d.mode,d.current_p1),("alignment_shedding",6))

    def test_below_configured_minimum_cannot_be_offered(self):
        r = self.pending()
        r.settings.min_charge_amps=8
        d = self.shedding(r)
        self.assertEqual((d.mode,d.current_p1),("unavailable",0))

    def test_waiting_vehicle_does_not_receive_a_new_start_offer(self):
        for status in ("ready_to_charge","awaiting_start","awaiting_scheduled_start","paused"):
            with self.subTest(status=status):
                r = self.pending()
                d = self.shedding(r,status=status)
                self.assertNotEqual(d.mode,"alignment_shedding")
                self.assertNotEqual(d.command,"resume")
                self.assertEqual(r.last_target[0],16)

    def test_leakage_current_cannot_qualify_as_continuing_charging(self):
        r = self.pending()
        d = self.sample(r,72,15.5,0.18,grid_ts=1069,ev_ts=1072)
        self.assertEqual((d.mode,d.current_p1),("unavailable",0))

    def test_fault_invalid_and_negative_inputs_still_request_zero(self):
        for extra in ({"data_valid":False,"data_error":"source_stale"},
                      {"status":"offline"},{"grid_l1":-1},{"charger_current":-1}):
            with self.subTest(extra=extra):
                r = self.pending()
                d = self.shedding(r,**extra)
                self.assertEqual((d.mode,d.current_p1),("unavailable",0))

    def test_missing_future_stale_and_backward_receipts_cannot_grant_fallback(self):
        cases = [
            {"grid_report_ts":None},{"grid_report_ts":1069,"charger_report_ts":None},{"wall_now_ts":None},
            {"grid_report_ts":True},{"grid_report_ts":1069,"charger_report_ts":False},
            {"grid_report_ts":float("nan")},{"grid_report_ts":1069,"charger_report_ts":float("inf")},
            {"grid_report_ts":1059},{"grid_report_ts":1080,"charger_report_ts":1081},
            {"charger_report_ts":1080},{"wall_now_ts":1200},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                d = self.shedding(self.pending(),**extra)
                self.assertEqual(d.current_p1,0)
                self.assertNotEqual(d.mode,"alignment_shedding")

    def test_soft_cap_combines_with_the_stricter_raw_safe_cap(self):
        for soft,target,mode in ((10,7,"alignment_shedding"),(6,6,"soft_limit"),(0,0,"soft_limit")):
            with self.subTest(soft=soft):
                r = self.pending(soft_limit_amps=9,soft_for_seconds=0,soft_current_amps=soft)
                d = self.shedding(r)
                self.assertEqual((d.mode,d.current_p1),(mode,target))

    def test_pause_guard_precedes_the_lower_cap_fallback(self):
        r = self.pending(pause_limit_amps=9,pause_for_seconds=0)
        d = self.shedding(r)
        self.assertEqual((d.mode,d.command),("pause","pause"))

    def test_urgent_pause_cannot_be_suppressed_by_recent_positive_offer(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True,dry_run=False,measurement_settle_seconds=25,
            pause_limit_amps=9,pause_for_seconds=0))
        self.sample(r,0,3.3,0.012,status="awaiting_start",accept=True)
        self.sample(r,60,3.3,0.012,status="awaiting_start",accept=True)
        d = self.sample(r,62,15.5,3.96,grid_ts=1061.9,ev_ts=1062)
        self.assertEqual((d.mode,d.command),("pause","pause"))
        self.assertTrue(d.should_call_service)
        self.assertEqual(r.last_target[0],16)
        r.acknowledge(d,62)
        self.assertTrue(r.paused_by_us)

    def test_non_exceeded_positive_hold_keeps_existing_pause_cooldown(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True,dry_run=False,measurement_settle_seconds=25,
            pause_limit_amps=2,pause_for_seconds=0))
        self.sample(r,0,3.3,0.012,status="awaiting_start",accept=True)
        self.sample(r,60,3.3,0.012,status="awaiting_start",accept=True)
        d = self.sample(r,62,3.3,2,grid_ts=1060,ev_ts=1062)
        self.assertEqual((d.mode,d.command),("pause","pause"))
        self.assertFalse(d.should_call_service)
        later = self.sample(r,65,3.3,2,grid_ts=1060,ev_ts=1065)
        self.assertTrue(later.should_call_service)

    def test_owned_resume_remains_subject_to_the_existing_action_cooldown(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True,dry_run=False,measurement_settle_seconds=25,
            pause_limit_amps=9,pause_for_seconds=0,resume_for_seconds=0,
            restart_stable_seconds=0))
        self.sample(r,0,3.3,0.012,status="awaiting_start",accept=True)
        paused = self.sample(r,60,18.7,16,accept=True)
        self.assertEqual(paused.command,"pause")
        self.assertTrue(r.paused_by_us)
        cap = self.sample(r,61,3.3,0,status="paused",accept=True)
        self.assertEqual(cap.command,"set_limit")
        resume = self.sample(r,62,3.3,0,status="paused")
        self.assertEqual(resume.command,"resume")
        self.assertFalse(resume.should_call_service)
        self.assertTrue(r.paused_by_us)

    def test_hard_overload_guard_precedes_the_lower_cap_fallback(self):
        d = self.shedding(self.pending(),grid=39)
        self.assertEqual((d.mode,d.current_p1),("overload_stop",0))
        self.assertTrue(d.overload_failure_latched)

    def test_experimental_minimum_envelope_keeps_its_existing_policy(self):
        d = self.shedding(self.pending(),grid=26)
        self.assertEqual((d.mode,d.current_p1),("overload_shedding",6))
        self.assertEqual(d.overload_transition_remaining_seconds,10)

    def test_excess_current_is_not_reinterpreted_as_safe_shedding(self):
        d = self.shedding(self.pending(),ev=16.50001)
        self.assertEqual((d.mode,d.current_p1),("unavailable",0))
        self.assertIn("excess_current",d.reason)

    def test_original_positive_timeout_still_requests_zero(self):
        d = self.shedding(self.pending(),now=95)
        self.assertEqual((d.mode,d.current_p1),("unavailable",0))
        self.assertIn("timeout",d.reason)

    def test_last_instant_before_deadline_does_not_grant_a_new_window(self):
        r = self.pending()
        before = self.shedding(r,now=94.999)
        self.assertEqual(before.current_p1,7)
        expired = self.shedding(r,now=95)
        self.assertEqual((expired.mode,expired.current_p1),("unavailable",0))

    def test_failed_or_unsent_lower_proposal_changes_no_accepted_state(self):
        r = self.pending()
        for now in (72,73,80):
            d = self.sample(r,now,15.5,3.96,grid_ts=1071.9,ev_ts=1000+now)
            self.assertEqual(d.current_p1,7)
            self.assertEqual((r.last_target[0],r._positive_cap),(16,16))
            self.assertIsNone(r._accepted_reduction_cap)
            self.assertEqual(r._positive_skew_since,70)
        self.assertEqual(self.sample(r,95,15.5,3.96,grid_ts=1071.9,ev_ts=1095).current_p1,0)

    def test_safety_reduction_bypasses_increase_cooldown(self):
        r = self.pending()
        r.settings.min_seconds_between_actions=300
        d = self.shedding(r)
        self.assertTrue(d.should_call_service)
        self.assertEqual(d.current_p1,7)

    def test_accepted_lower_cap_transfers_original_deadline_including_api_latency(self):
        r = self.pending()
        d = self.shedding(r)
        r.acknowledge(d,74.5)
        self.assertEqual((r.last_target[0],r._accepted_reduction_cap,r._reduction_previous_cap),(7,7,16))
        self.assertIsNone(r._positive_cap)
        self.assertEqual((r._reduction_grid_ts,r._reduction_charger_ts),(1071.9,1072))
        self.assertEqual(r._house_before_reduction,15.5)
        self.assertEqual(r._partial_reduction_used_seconds,4.5)
        self.assertEqual(r._reduction_guard_deadline,95)
        late = self.sample(r,76,9.8,16.08,grid_ts=1076,ev_ts=1075)
        self.assertEqual((late.mode,late.current_p1),("settling",7))
        self.assertEqual(late.measurement_alignment_remaining_seconds,19)

    def test_identical_lower_ttl_renewals_cannot_reset_transferred_time(self):
        r = self.pending(resend_limit_every_seconds=5)
        d = self.shedding(r)
        r.acknowledge(d,74.5)
        self.sample(r,76,9.8,16.08,grid_ts=1076,ev_ts=1075)
        renewal = self.sample(r,82,9.8,16.08,grid_ts=1082,ev_ts=1082,accept=True)
        self.assertTrue(renewal.should_call_service)
        self.assertEqual(r._reduction_guard_deadline,95)
        self.assertEqual(r._partial_reduction_used_seconds,4.5)
        expired = self.sample(r,95,9.8,16.08)
        self.assertEqual((expired.mode,expired.current_p1),("unavailable",0))

    def test_ack_after_deadline_cannot_borrow_more_response_time(self):
        r = self.pending()
        d = self.shedding(r)
        r.acknowledge(d,96)
        self.assertEqual(r._partial_reduction_used_seconds,26)
        self.assertEqual(r._reduction_guard_deadline,95)
        late = self.sample(r,97,9.8,16.08)
        self.assertEqual((late.mode,late.current_p1),("unavailable",0))

    def test_previously_spent_positive_partial_time_is_not_refunded(self):
        r = self.pending()
        self.sample(r,72,5.3,2,grid_ts=1072,ev_ts=1070)
        self.assertEqual(r._positive_alignment_used_seconds,2)
        self.sample(r,74,5.3,3.96,grid_ts=1072,ev_ts=1074)
        d = self.shedding(r,now=76,ev=5)
        self.assertEqual(d.current_p1,7)
        r.acknowledge(d,78)
        self.assertEqual(r._partial_reduction_used_seconds,6)
        self.assertEqual(r._reduction_guard_deadline,97)

    def test_real_lower_response_then_later_grid_allows_normal_recovery(self):
        r = self.pending()
        d = self.shedding(r)
        r.acknowledge(d,74.5)
        self.sample(r,75,15.5,6.99,grid_ts=1071.9,ev_ts=1075)
        confirmed = self.sample(r,76,10.3,6.99,grid_ts=1076,ev_ts=1075)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(confirmed.current_p1,16)
        self.assertFalse(confirmed.should_call_service)
        restored = self.sample(r,80,10.3,6.99,accept=True)
        self.assertEqual((restored.current_p1,r.last_target[0]),(16,16))
        self.assertIsNone(r._accepted_reduction_cap)

    def test_holding_the_original_offer_when_it_still_fits_is_unchanged(self):
        r = self.pending()
        d = self.shedding(r,grid=3.3)
        self.assertEqual((d.mode,d.current_p1),("settling",16))
        self.assertEqual(r.last_target[0],16)


if __name__ == "__main__":
    unittest.main()
