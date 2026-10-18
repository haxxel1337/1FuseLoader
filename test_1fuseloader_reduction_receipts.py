"""Late pre-reduction receipts cannot cancel an accepted safe lower cap.

Synthetic, offline source-order scenarios. API acceptance and measured response
are separate steps; no household records, Home Assistant or network are used.
"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_reduction_receipt_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class ReductionReceiptTests(unittest.TestCase):
    def regulator(self, **options):
        options.setdefault("measurement_settle_seconds", 25)
        options.setdefault("overload_enabled", True)
        return logic.OneFuseRegulator(logic.RegulatorSettings(
            active_control=True, dry_run=False, **options))

    def sample(self, r, now, grid, ev, *, grid_ts=None, ev_ts=None,
               accept=True, status="charging", **extra):
        inp = dict(now_ts=now, wall_now_ts=1000+now, status=status,
                   grid_l1=grid, charger_current=ev,
                   grid_report_ts=1000+now if grid_ts is None else grid_ts,
                   charger_report_ts=1000+now if ev_ts is None else ev_ts)
        inp.update(extra)
        d = r.evaluate(logic.RegulatorInput(**inp))
        if accept:
            r.acknowledge(d, now)
        return d

    def pending(self, *, cap=7, baseline=2.7, accept=True, **options):
        r = self.regulator(**options)
        first = self.sample(r, 0, baseline+2.9, baseline)
        self.assertEqual((first.current_p1, r.last_target[0]), (16, 16))
        lower = self.sample(r, 6, baseline+23-cap, baseline,
                            grid_ts=1006, ev_ts=1000, accept=accept)
        self.assertEqual(lower.current_p1, cap)
        if accept:
            self.assertEqual(r.last_target[0], cap)
        return r

    def late(self, r, *, now=6.8, ev=16.08, **extra):
        return self.sample(r, now, 18.7, ev, grid_ts=1006,
                           ev_ts=1000+now, **extra)

    def grid_first(self, r, *, now=10.9, grid=9.8, ev=16.08,
                   ev_ts=1006.8, **extra):
        return self.sample(r, now, grid, ev, ev_ts=ev_ts, **extra)

    def test_low_baseline_late_high_then_reduced_grid_holds_only_accepted_seven(self):
        r = self.pending()
        self.assertEqual(self.late(r).current_p1, 7)
        d = self.grid_first(r)
        self.assertEqual((d.mode, d.command, d.current_p1), ("settling", "set_limit", 7))
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(d.house_load_basis, "accepted_lower_cap_raw_grid_hold")
        self.assertEqual(d.house_load_l1, 9.8)
        self.assertEqual(d.measurement_alignment_remaining_seconds, 25)
        self.assertIsNone(r._overload_transition_started)

    def test_unchanged_low_tick_cannot_clear_pending_or_propose_sixteen(self):
        r = self.pending()
        unchanged = self.sample(r, 6.1, 18.7, 2.7, grid_ts=1006, ev_ts=1000)
        self.assertEqual((unchanged.current_p1, r._awaiting_reduction_target), (7, 7))
        late = self.late(r)
        self.assertEqual(late.current_p1, 7)
        self.assertFalse(late.should_call_service)
        self.assertEqual(self.grid_first(r).current_p1, 7)

    def test_new_receipt_of_same_low_value_plus_grid_is_not_response(self):
        r = self.pending()
        self.sample(r, 7, 18.7, 2.7, grid_ts=1006, ev_ts=1007)
        self.sample(r, 8, 9.8, 2.7, grid_ts=1008, ev_ts=1007)
        self.assertEqual(r._awaiting_reduction_target, 7)
        d = self.sample(r, 9, 9.8, 16.08, grid_ts=1008, ev_ts=1009)
        self.assertEqual((d.mode, d.current_p1), ("settling", 7))

    def test_near_cap_baseline_new_receipt_and_later_grid_do_not_pin_seven(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        d = self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(d.current_p1, 16)
        self.assertFalse(d.should_call_service)
        self.assertEqual((r._accepted_reduction_cap,r.last_target[0]), (7,7))

    def test_near_cap_confirmation_keeps_late_guard_until_a_higher_cap_is_accepted(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        late = self.sample(r, 9, 9.8, 16.08, grid_ts=1009, ev_ts=1009)
        self.assertEqual((late.mode,late.current_p1), ("settling",7))
        self.assertEqual(r._reduction_guard_deadline,32)
        expired = self.sample(r, 32, 9.8, 16.08, grid_ts=1032, ev_ts=1032, accept=False)
        self.assertEqual((expired.mode,expired.current_p1), ("unavailable",0))

    def test_failed_higher_submission_does_not_remove_historical_lower_cap(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        proposed = self.sample(r, 11, 13.0, 6.9, accept=False)
        self.assertTrue(proposed.should_call_service)
        self.assertEqual((proposed.current_p1,r.last_target[0]), (16,7))
        late = self.sample(r, 12, 9.8, 16.08, grid_ts=1012, ev_ts=1012)
        self.assertEqual((late.mode,late.current_p1), ("settling",7))

    def test_accepted_higher_cap_replaces_history_with_positive_alignment(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        self.sample(r, 11, 13.0, 6.9)
        self.assertEqual(r.last_target[0],16)
        self.assertIsNone(r._accepted_reduction_cap)
        self.assertEqual(r._positive_cap,16)
        late = self.sample(r, 12, 9.8, 16.08, grid_ts=1012, ev_ts=1012)
        self.assertEqual((late.mode,late.current_p1), ("settling",16))

    def test_historical_guard_can_expire_while_low_reports_are_coherent(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        late = self.sample(r, 100, 9.8, 16.08, grid_ts=1100, ev_ts=1100, accept=False)
        self.assertEqual((late.mode,late.current_p1), ("unavailable",0))

    def test_further_lower_cap_after_confirmation_keeps_historical_deadline(self):
        r = self.pending(baseline=6.9)
        self.sample(r, 7, 13.0, 6.9, grid_ts=1007, ev_ts=1007)
        d = self.sample(r, 8, 13.0, 6.9, grid_ts=1008, ev_ts=1007)
        self.assertIsNone(r._awaiting_reduction_target)
        lower = replace(d, current_p1=6, should_call_service=True)
        r.acknowledge(lower,15)
        self.assertEqual((r._accepted_reduction_cap,r._reduction_previous_cap), (6,16))
        self.assertEqual(r._reduction_guard_deadline,32)
        late = self.sample(r, 32, 9.8, 16.08, grid_ts=1032, ev_ts=1032, accept=False)
        self.assertEqual((late.mode,late.current_p1), ("unavailable",0))

    def test_new_reduction_after_expired_confirmed_guard_has_new_response_baselines(self):
        r = self.pending(baseline=6.9,overload_enabled=False)
        self.sample(r, 7, 13, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13, 6.9, grid_ts=1008, ev_ts=1007)
        self.sample(r, 300, 22.9, 6.9)
        lower = self.sample(r, 306, 24.8, 6.9)
        self.assertEqual((lower.current_p1,r._reduction_previous_cap), (6,7))
        self.assertEqual((r._reduction_grid_ts,r._reduction_charger_ts), (1306,1306))
        self.assertIsNone(r._reduction_guard_deadline)
        self.assertIsNone(r._reduction_low_response_ts)
        response = self.sample(r, 307, 24.8, 6.03, grid_ts=1306, ev_ts=1307)
        self.assertEqual((response.mode,response.current_p1), ("settling",6))
        self.assertEqual(response.measurement_alignment_remaining_seconds,25)
        confirmed = self.sample(r, 308, 23.93, 6.03, grid_ts=1308, ev_ts=1307)
        self.assertEqual(confirmed.current_p1,6)
        self.assertNotEqual(confirmed.mode,"unavailable")

    def test_expired_historical_prior_sixteen_cannot_authorize_a_new_sixteen_receipt(self):
        r = self.pending(baseline=6.9,overload_enabled=False)
        self.sample(r, 7, 13, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13, 6.9, grid_ts=1008, ev_ts=1007)
        self.sample(r, 300, 22.9, 6.9)
        self.sample(r, 306, 24.8, 6.9)
        late = self.sample(r,307,9.8,16.08)
        self.assertEqual(late.current_p1,0)

    def test_unexpired_history_keeps_budget_but_uses_latest_cap_response_receipts(self):
        r = self.pending(baseline=6.9,overload_enabled=False)
        self.sample(r, 7, 13, 6.9, grid_ts=1007, ev_ts=1007)
        self.sample(r, 8, 13, 6.9, grid_ts=1008, ev_ts=1007)
        self.sample(r, 15, 24.8, 6.9)
        self.assertEqual((r.last_target[0],r._reduction_previous_cap), (6,16))
        self.assertEqual((r._reduction_grid_ts,r._reduction_charger_ts), (1015,1015))
        self.assertEqual(r._reduction_guard_deadline,32)
        response = self.sample(r,16,24.8,6.03,grid_ts=1015,ev_ts=1016)
        self.assertEqual((response.mode,response.current_p1), ("settling",6))
        self.assertEqual(response.measurement_alignment_remaining_seconds,16)

    def test_confirmed_ordinary_reduction_without_guard_does_not_pollute_next_reduction(self):
        r = self.regulator(overload_enabled=False)
        self.sample(r,0,21,16)
        self.sample(r,6,29,16,grid_ts=1006,ev_ts=1000)
        self.sample(r,8,23,10,grid_ts=1008,ev_ts=1008)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertIsNone(r._reduction_guard_deadline)
        self.sample(r,300,27.9,10)
        self.assertEqual((r.last_target[0],r._reduction_previous_cap), (6,10))
        d = self.sample(r,301,27.9,6.03,grid_ts=1300,ev_ts=1301)
        self.assertEqual((d.mode,d.current_p1), ("settling",6))

    def test_legitimate_rise_near_accepted_cap_then_grid_allows_recovery(self):
        r = self.pending()
        d = self.sample(r, 7.5, 18.7, 6.99, grid_ts=1006, ev_ts=1007.5)
        self.assertEqual((d.mode, d.current_p1), ("settling", 7))
        d = self.sample(r, 8, 9.8, 6.99, grid_ts=1008, ev_ts=1007.5)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(d.current_p1, 16)
        self.assertFalse(d.should_call_service)  # Changed-cap cooldown still applies.
        d = self.sample(r, 11, 9.8, 6.99, grid_ts=1011, ev_ts=1011)
        self.assertEqual((d.current_p1, r.last_target[0]), (16, 16))

    def test_repeated_near_cap_receipts_do_not_move_first_response_confirmation(self):
        r = self.pending()
        self.sample(r, 10, 9.8, 6.99, grid_ts=1009, ev_ts=1010)
        self.assertEqual(r._reduction_low_response_ts, 1010)
        d = self.sample(r, 15, 9.8, 6.99, grid_ts=1014, ev_ts=1015)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(d.current_p1, 16)
        self.assertNotEqual(d.mode, "unavailable")

    def test_high_receipt_invalidates_low_evidence_without_refunding_budget(self):
        r = self.pending()
        self.sample(r, 10, 9.8, 6.99, grid_ts=1009, ev_ts=1010)
        d = self.sample(r, 15, 9.8, 16.08, grid_ts=1014, ev_ts=1015)
        self.assertEqual(d.current_p1, 7)
        self.assertIsNone(r._reduction_low_response_ts)
        self.assertEqual(r._settle_started, 10)
        expired = self.sample(r, 35, 9.8, 16.08, grid_ts=1034, ev_ts=1035, accept=False)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))

    def test_grid_receipt_before_late_high_receipt_is_also_conservatively_safe(self):
        r = self.pending()
        d = self.sample(r, 10, 9.8, 16.08, grid_ts=1009, ev_ts=1010)
        self.assertEqual((d.mode, d.current_p1), ("settling", 7))

    def test_duplicate_high_receipt_after_normal_reduction_can_use_full_buffer(self):
        r = self.regulator(overload_enabled=False)
        self.sample(r, 0, 21, 16)
        self.sample(r, 6, 32, 16, grid_ts=1006, ev_ts=1000)
        self.assertEqual(r.last_target[0], 7)
        d = self.sample(r, 10, 9.8, 16, grid_ts=1010, ev_ts=1010)
        self.assertEqual((d.mode, d.current_p1), ("settling", 7))

    def test_unaccepted_reduction_cannot_grant_safe_hold(self):
        r = self.pending(accept=False)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.grid_first(r).current_p1, 0)

    def test_unknown_previous_accepted_cap_cannot_grant_new_hold(self):
        r = self.pending()
        r._reduction_previous_cap = None
        self.assertEqual(self.grid_first(r).current_p1, 0)

    def test_current_above_previous_accepted_cap_tolerance_is_not_hidden(self):
        for ev, expected in ((16.5, 7), (16.50001, 0), (25, 0)):
            with self.subTest(ev=ev):
                r = self.pending()
                self.assertEqual(self.grid_first(r, ev=ev).current_p1, expected)

    def test_full_buffer_boundary_is_strict_for_new_receipt_path(self):
        for grid, expected in ((13,10), (13.00001,0), (14.5,0)):
            with self.subTest(grid=grid):
                r = self.pending(cap=10)
                d = self.sample(r, 10, grid, 16.08, grid_ts=1010, ev_ts=1009)
                self.assertEqual(d.current_p1, expected)

    def test_new_hold_cannot_exceed_changed_configured_current_bounds(self):
        for options in ({"max_charge_amps":6}, {"min_charge_amps":8}):
            with self.subTest(options=options):
                r = self.pending()
                r.update_settings(replace(r.settings,**options))
                d = self.grid_first(r)
                self.assertEqual(d.current_p1,0)

    def test_historical_legacy_receipt_path_cannot_bypass_changed_current_bounds(self):
        for options in ({"max_charge_amps":6}, {"min_charge_amps":8}):
            for ev_ts in (1000,1009):
                with self.subTest(options=options,ev_ts=ev_ts):
                    r = self.pending(baseline=6.9,overload_enabled=False)
                    self.sample(r,7,13,6.9,grid_ts=1007,ev_ts=1007)
                    self.sample(r,8,13,6.9,grid_ts=1008,ev_ts=1007)
                    r.update_settings(replace(r.settings,**options))
                    d = self.sample(r,9,9.8,16.08,grid_ts=1009,ev_ts=ev_ts)
                    self.assertEqual((d.mode,d.current_p1), ("unavailable",0))

    def test_ev_first_and_partial_hold_cannot_bypass_changed_current_bounds(self):
        for partial in (False,True):
            with self.subTest(partial=partial):
                r = self.pending(overload_enabled=False)
                if partial:self.late(r)
                r.update_settings(replace(r.settings,max_charge_amps=6))
                ev = 12 if partial else 6.99
                d = self.sample(r,10,18.7,ev,grid_ts=1006,ev_ts=1010)
                self.assertEqual((d.mode,d.current_p1), ("unavailable",0))

    def test_no_grace_begins_until_actual_grid_decrease(self):
        r = self.pending()
        self.late(r)
        self.assertIsNone(r._settle_started)
        d = self.sample(r, 10, 18.7, 16.08, grid_ts=1010, ev_ts=1009)
        self.assertNotEqual(d.mode, "settling")
        self.assertIsNone(r._settle_started)

    def test_ttl_renewal_and_identical_gets_do_not_renew_budget(self):
        r = self.pending(resend_limit_every_seconds=5)
        self.grid_first(r)
        renewed = self.grid_first(r, now=16, ev_ts=1016)
        self.assertTrue(renewed.should_call_service)
        self.assertEqual(r._settle_started, 10.9)
        self.assertAlmostEqual(renewed.measurement_alignment_remaining_seconds, 19.9)
        expired = self.grid_first(r, now=35.9, ev_ts=1035.9, accept=False)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))
        self.assertEqual(self.grid_first(r, now=36, ev_ts=1036, accept=False).current_p1, 0)

    def test_large_tick_gap_and_failed_zero_do_not_reopen_the_same_cap_budget(self):
        r = self.pending()
        self.grid_first(r)
        for now in (40,41,42):
            d = self.grid_first(r, now=now, ev_ts=1000+now, accept=False)
            self.assertEqual(d.current_p1, 0)
            self.assertEqual(r.last_target[0], 7)
            self.assertEqual(r._settle_started, 10.9)

    def test_accepted_further_lower_cap_keeps_original_uncertainty_budget(self):
        r = self.pending()
        d = self.grid_first(r)
        lower = replace(d, current_p1=6, should_call_service=True)
        r.acknowledge(lower, 16)
        self.assertEqual((r._awaiting_reduction_target,r._reduction_previous_cap), (6,16))
        self.assertAlmostEqual(r._partial_reduction_used_seconds,5.1)
        self.assertEqual(r._reduction_guard_deadline,35.9)
        d = self.grid_first(r, now=35.9, ev_ts=1035.9, accept=False)
        self.assertEqual((d.mode,d.current_p1), ("unavailable",0))

    def test_grid_first_then_partial_then_full_ev_response_share_one_budget(self):
        r = self.pending()
        self.grid_first(r)
        d = self.sample(r, 15, 9.8, 13, grid_ts=1010.9, ev_ts=1015)
        self.assertEqual((d.mode,d.current_p1), ("settling",7))
        d = self.sample(r, 20, 9.8, 6.99, grid_ts=1010.9, ev_ts=1020)
        self.assertEqual((d.mode,d.current_p1), ("settling",7))
        d = self.sample(r, 35.9, 9.8, 6.99, grid_ts=1010.9, ev_ts=1035.9, accept=False)
        self.assertEqual((d.mode,d.current_p1), ("unavailable",0))

    def test_late_ev_response_and_later_grid_can_complete_with_remaining_budget(self):
        r = self.pending()
        self.grid_first(r)
        self.sample(r, 15, 9.8, 6.99, grid_ts=1010.9, ev_ts=1015)
        d = self.sample(r, 16, 9.8, 6.99, grid_ts=1016, ev_ts=1015)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertNotEqual(d.mode, "unavailable")
        self.assertEqual(d.current_p1, 16)

    def test_missing_invalid_old_future_and_incoherent_receipts_grant_no_new_hold(self):
        cases = [
            {"grid_report_ts":None}, {"charger_report_ts":None},
            {"grid_report_ts":True}, {"charger_report_ts":False},
            {"grid_report_ts":float("nan")}, {"charger_report_ts":float("inf")},
            {"grid_report_ts":1005}, {"charger_report_ts":999},
            {"grid_report_ts":1020}, {"charger_report_ts":1020},
            {"wall_now_ts":None}, {"wall_now_ts":True},
            {"wall_now_ts":1200}, {"wall_now_ts":float("nan")},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                r = self.pending()
                d = self.grid_first(r, **extra)
                self.assertEqual(d.current_p1, 0)

    def test_invalid_fault_and_paused_or_scheduled_status_override_new_hold(self):
        for extra in ({"data_valid":False,"data_error":"source_stale"},
                      {"status":"offline"}, {"status":"paused"},
                      {"status":"awaiting_scheduled_start"}):
            with self.subTest(extra=extra):
                r = self.pending()
                d = self.grid_first(r, **extra)
                self.assertEqual(d.current_p1, 0)
                self.assertNotEqual(d.command, "resume")

    def test_new_hold_can_never_issue_resume(self):
        r = self.pending(resume_for_seconds=0)
        r.paused_by_us = True
        d = self.grid_first(r)
        self.assertEqual((d.command,d.current_p1), ("set_limit",7))
        self.assertTrue(r.paused_by_us)

    def test_custom_soft_and_pause_guards_execute_instead_of_only_observing(self):
        for pause in (False, True):
            with self.subTest(pause=pause):
                r = self.pending()
                if pause:
                    r.settings.pause_limit_amps=9
                    r.settings.pause_for_seconds=0
                else:
                    r.settings.soft_limit_amps=9
                    r.settings.soft_for_seconds=0
                    r.settings.soft_current_amps=0
                d = self.grid_first(r)
                self.assertEqual(d.mode, "pause" if pause else "soft_limit")
                self.assertEqual(d.command, "pause" if pause else "set_limit")
                if not pause:self.assertEqual(d.current_p1,0)

    def test_ev_first_low_response_also_executes_custom_soft_and_pause_guards(self):
        for pause in (False,True):
            with self.subTest(pause=pause):
                r = self.pending(overload_enabled=False,enable_soft_guard=False,
                                 enable_pause_resume=False)
                if pause:
                    r.update_settings(replace(r.settings,enable_pause_resume=True,
                        pause_limit_amps=9,pause_for_seconds=0))
                else:
                    r.update_settings(replace(r.settings,enable_soft_guard=True,
                        soft_limit_amps=9,soft_for_seconds=0,soft_current_amps=0))
                d = self.sample(r,7.5,18.7,6.99,grid_ts=1006,ev_ts=1007.5,accept=False)
                self.assertEqual(d.mode,"pause" if pause else "soft_limit")
                self.assertEqual(d.command,"pause" if pause else "set_limit")
                if not pause:self.assertEqual(d.current_p1,0)

    def test_raw_overload_hard_ceiling_precedes_reduction_hold(self):
        r = self.pending()
        d = self.grid_first(r, grid=39)
        self.assertEqual((d.mode,d.current_p1), ("overload_stop",0))
        self.assertTrue(d.overload_failure_latched)

    def test_overload_transition_policy_is_not_replaced_by_raw_safe_hold(self):
        r = self.pending()
        d = self.grid_first(r, grid=30)
        self.assertEqual((d.mode,d.current_p1), ("overload_shedding",6))
        self.assertEqual(d.overload_transition_remaining_seconds,10)

    def test_new_load_or_raw_grid_growth_cannot_be_hidden_as_old_reduction(self):
        r = self.pending(cap=10)
        self.sample(r, 10, 13, 16.08, grid_ts=1010, ev_ts=1009)
        d = self.sample(r, 11, 15, 16.08, grid_ts=1011, ev_ts=1009)
        self.assertEqual((d.mode,d.current_p1), ("unavailable",0))


if __name__ == "__main__":
    unittest.main()
