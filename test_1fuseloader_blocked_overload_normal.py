"""Synthetic nominal control while an optional overload allowance is blocked.

No HA, network, household export or electrical load is accessed. Receipt order
is uncertainty evidence, never proof of synchronized physical sampling.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_blocked_normal_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)
EPOCH = 1_700_000_000.0


class BlockedOverloadNormalTests(unittest.TestCase):
    def regulator(self, *, latched=True, **options):
        settings = dict(active_control=True, dry_run=False, overload_enabled=True,
                        measurement_settle_seconds=25, enable_soft_guard=False,
                        enable_pause_resume=False)
        settings.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**settings))
        if latched:
            r.import_overload_state(dict(version=1, used_seconds=2000.0, exposure_active=False,
                                         transition_pending=False, minimum_held=False,
                                         failure_reason="response_timeout"))
        return r

    def sample(self, r, now, grid, ev, *, status="charging", gt=None, et=None,
               accept=True, **metadata):
        reports = dict(wall_now_ts=EPOCH+now, grid_report_ts=EPOCH+(now if gt is None else gt),
                       charger_report_ts=EPOCH+(now if et is None else et))
        reports.update(metadata)
        d = r.evaluate(logic.RegulatorInput(now_ts=now, status=status, grid_l1=grid,
                                            charger_current=ev, **reports))
        if accept:
            r.acknowledge(d, now)
        return d

    def running_sixteen(self, **options):
        r = self.regulator(**options)
        d = self.sample(r, 0, 21.7, 16.165)
        self.assertEqual(d.current_p1, 16)
        self.assertEqual(r.last_target[0], 16)
        return r

    def startup(self, **options):
        r = self.regulator(**options)
        self.sample(r, 0, 5.4, .023, status="awaiting_start")
        d = self.sample(r, 60, 5.4, .023, status="awaiting_start")
        self.assertEqual((d.current_p1, r._positive_cap), (16, 16))
        self.sample(r, 64, 5.6, .023, et=60)
        d = self.sample(r, 65, 5.6, 2.352, gt=64)
        self.assertEqual((d.current_p1, r._positive_skew_since), (16, 65))
        return r

    def unconfirmed_zero(self, **options):
        r = self.regulator(**options)
        self.assertEqual(self.sample(r, 0, 23, 11).current_p1, 11)
        stopped = self.sample(r, 1, 23, 11, data_valid=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual((r.last_target[0], r._awaiting_reduction_target), (0, 0))
        return r

    def grid_first(self, r, now=69.5, grid=21.4, **extra):
        return self.sample(r, now, grid, 2.352, et=65, **extra)

    def assert_latch_preserved(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2000)

    def test_fresh_nominal_reduction_floors_to_twelve_without_using_overload(self):
        r = self.running_sixteen()
        d = self.sample(r, 10, 26.2, 16.165, et=5)
        self.assertEqual((d.mode, d.current_p1), ("normal", 12))
        self.assertTrue(d.should_call_service)
        self.assertLessEqual(d.house_load_l1+12, 23)
        self.assertIn("experimental overload remains blocked", d.reason)
        self.assertIsNone(d.overload_transition_remaining_seconds)
        self.assert_latch_preserved(r)

    def test_accepted_lower_cap_waits_for_response_and_does_not_jump_back(self):
        r = self.running_sixteen()
        self.sample(r, 10, 26.2, 16.165, et=5)
        waiting = self.sample(r, 11, 26.2, 16.165, gt=10, et=5)
        self.assertEqual(waiting.current_p1, 12)
        self.assertFalse(waiting.should_call_service)
        ev_first = self.sample(r, 13, 26.2, 12.1, gt=10)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("settling", 12))
        coherent = self.sample(r, 15, 22.2, 12.1, et=13)
        self.assertGreaterEqual(coherent.current_p1, 6)
        self.assertLessEqual(coherent.house_load_l1+coherent.current_p1, 23)
        self.assert_latch_preserved(r)

    def test_full_ev_first_reduction_uses_accepted_nominal_house_anchor(self):
        r = self.running_sixteen()
        self.sample(r, 10, 26.2, 16.165, et=5)
        d = self.sample(r, 12, 26.2, 6, gt=10)
        self.assertEqual((d.mode, d.current_p1), ("settling", 12))
        self.assertLessEqual(r._house_before_reduction+12, 23)
        self.assert_latch_preserved(r)

    def test_normal_shedding_attempt_has_one_nonrenewable_window(self):
        r = self.running_sixteen(resend_limit_every_seconds=1)
        self.sample(r, 10, 26.2, 16.165)
        for now in (11, 15, 25, 34.9):
            d = self.sample(r, now, 26.2, 16.165)
            self.assertEqual(d.current_p1, 12)
            self.assertEqual(r._nominal_shedding_started, 10)
            self.assertIsNone(r._positive_cap)
        d = self.sample(r, 35, 26.2, 16.165, accept=False)
        self.assertEqual((d.mode, d.current_p1), ("overload_stop", 0))
        self.assert_latch_preserved(r)

    def test_failed_nominal_call_does_not_refund_attempt_time(self):
        r = self.running_sixteen()
        self.assertEqual(self.sample(r, 10, 26.2, 16.165, accept=False).current_p1, 12)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.sample(r, 34, 26.2, 16.165, accept=False).current_p1, 12)
        self.assertEqual(self.sample(r, 35, 26.2, 16.165, accept=False).current_p1, 0)

    def test_freshness_and_known_offer_are_required_for_nominal_ev_credit(self):
        cases = [dict(charger_report_ts=None), dict(grid_report_ts=None), dict(wall_now_ts=None),
                 dict(charger_report_ts=True), dict(wall_now_ts=float("nan")),
                 dict(charger_report_ts=EPOCH-200), dict(charger_report_ts=EPOCH+30)]
        for case in cases:
            with self.subTest(case=case):
                d = self.sample(self.running_sixteen(), 10, 26.2, 16.165, **case)
                self.assertEqual(d.current_p1, 0)
        for current in (32, 0.01):
            with self.subTest(current=current):
                self.assertEqual(self.sample(self.running_sixteen(), 10, 26.2, current).current_p1, 0)

    def test_genuine_household_increase_outside_nominal_and_ceiling_still_stops(self):
        for grid, ev in ((29.001, 16.165), (38.001, 16.165), (26.2, 6)):
            with self.subTest(grid=grid, ev=ev):
                self.assertEqual(self.sample(self.running_sixteen(), 10, grid, ev).current_p1, 0)

    def test_live_experimental_deadline_is_not_reclassified_as_nominal(self):
        r = self.regulator(latched=False)
        start = self.sample(r, 0, 38, 16)
        self.assertEqual(start.mode, "overload_shedding")
        at_deadline = self.sample(r, 10, 27, 16)
        self.assertEqual((at_deadline.mode, at_deadline.current_p1), ("overload_stop", 0))
        self.assertIn("response_timeout", at_deadline.reason)

    def test_grid_first_partial_start_holds_only_accepted_sixteen_then_aligns(self):
        r = self.startup()
        held = self.grid_first(r)
        self.assertEqual((held.mode, held.current_p1), ("settling", 16))
        self.assertEqual(held.house_load_basis, "prior_coherent_house_grid_first_hold")
        self.assertTrue(held.current_refresh_required)
        self.assertEqual(r._positive_skew_since, 65)
        self.assertAlmostEqual(held.measurement_alignment_remaining_seconds, 20.5)
        rise = self.sample(r, 69.646, 21.4, 16.157, gt=69.5)
        self.assertEqual(rise.current_p1, 16)
        self.assertEqual(r._positive_skew_since, 65)
        confirmed = self.sample(r, 74, 21.4, 16.157, et=69.646)
        self.assertEqual((confirmed.mode, confirmed.current_p1), ("normal", 16))
        self.assertIsNone(r._positive_cap)
        self.assert_latch_preserved(r)

    def test_same_ambiguous_first_snapshot_but_fresh_low_ev_fails_closed(self):
        r = self.startup()
        self.assertEqual(self.grid_first(r).current_p1, 16)
        low_confirmed = self.sample(r, 70, 21.4, 2.352, gt=69.5)
        self.assertEqual(low_confirmed.current_p1, 0)
        self.assert_latch_preserved(r)

    def test_growing_or_unexplained_raw_grid_cannot_use_startup_hold(self):
        for grid in (23.0001, 22.2, 38.001):
            with self.subTest(grid=grid):
                r = self.startup()
                self.grid_first(r)
                d = self.grid_first(r, now=70, grid=grid)
                self.assertEqual(d.current_p1, 0)

    def test_repeated_grid_and_ttl_reports_cannot_renew_startup_window(self):
        r = self.startup(resend_limit_every_seconds=1)
        self.grid_first(r)
        for now in (70, 75, 85, 89.9):
            d = self.grid_first(r, now=now)
            self.assertEqual(d.current_p1, 16)
            self.assertEqual(r._positive_skew_since, 65)
            self.assertEqual(r._positive_grid_first_ts, EPOCH+69.5)
        self.assertEqual(self.grid_first(r, now=90).current_p1, 0)

    def test_previous_partial_alignment_time_is_not_refunded(self):
        r = self.startup()
        r._positive_alignment_used_seconds = 21
        self.assertEqual(self.grid_first(r).current_p1, 0)

    def test_grid_first_requires_valid_fresh_nonfuture_numeric_reports(self):
        cases = [dict(grid_report_ts=None), dict(charger_report_ts=None), dict(wall_now_ts=None),
                 dict(charger_report_ts=False), dict(grid_report_ts=float("nan")),
                 dict(charger_report_ts=EPOCH-200), dict(grid_report_ts=EPOCH+100)]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.grid_first(self.startup(), **case).current_p1, 0)

    def test_grid_first_cannot_override_fault_cap_excess_or_updated_bounds(self):
        for metadata in (dict(data_valid=False), dict(status="fault")):
            with self.subTest(metadata=metadata):
                r = self.startup()
                self.assertEqual(self.grid_first(r, **metadata).current_p1, 0)
        r = self.startup()
        self.assertEqual(self.sample(r, 69.5, 21.4, 16.6, et=65).current_p1, 0)
        r = self.startup()
        r.settings.max_charge_amps = 14
        self.assertNotEqual(self.grid_first(r).current_p1, 16)

    def test_custom_soft_and_pause_guards_win_over_grid_first_hold(self):
        for soft in (0, 7, 10):
            with self.subTest(soft=soft):
                r = self.startup()
                r.settings.enable_soft_guard = True
                r.settings.soft_limit_amps = 9
                r.settings.soft_for_seconds = 0
                r.settings.soft_current_amps = soft
                d = self.grid_first(r)
                self.assertEqual((d.mode, d.current_p1), ("soft_limit", soft))
        r = self.startup()
        r.settings.enable_pause_resume = True
        r.settings.pause_limit_amps = 9
        r.settings.pause_for_seconds = 0
        d = self.grid_first(r)
        self.assertEqual((d.mode, d.command, d.should_call_service), ("pause", "pause", True))

    def test_custom_pause_cannot_be_suppressed_by_recent_nominal_shedding(self):
        r = self.running_sixteen()
        self.sample(r, 10, 26.2, 16.165)
        r.settings.enable_pause_resume = True
        r.settings.pause_limit_amps = 9
        r.settings.pause_for_seconds = 0
        d = self.sample(r, 11, 26.2, 12.1, gt=10)
        self.assertEqual((d.command, d.should_call_service), ("pause", True))

    def test_backward_grid_or_ev_receipt_cannot_extend_the_new_hold(self):
        r = self.startup()
        self.grid_first(r)
        d = self.sample(r, 70, 21.4, 2.352, gt=69, et=65)
        self.assertEqual(d.current_p1, 0)
        r = self.startup()
        d = self.sample(r, 69.5, 21.4, 2.352, et=64.5)
        self.assertEqual(d.current_p1, 0)

    def test_exact_full_buffer_raw_boundary_is_retained_but_never_exceeded(self):
        for grid in (23.0, 23.00001):
            with self.subTest(grid=grid):
                r = self.regulator()
                self.sample(r, 0, 7, .023, status="awaiting_start")
                self.sample(r, 60, 7, .023, status="awaiting_start")
                self.sample(r, 65, 7, 2.352, gt=60)
                d = self.sample(r, 69, grid, 2.352, et=65)
                self.assertEqual(d.current_p1, 16 if grid == 23 else 0)

    def test_unaccepted_positive_proposal_is_not_an_alignment_anchor(self):
        r = self.regulator()
        self.sample(r, 0, 5.4, .023, status="awaiting_start")
        proposal = self.sample(r, 60, 5.4, .023, status="awaiting_start", accept=False)
        self.assertEqual(proposal.current_p1, 16)
        self.assertIsNone(r._positive_cap)
        self.sample(r, 65, 5.6, 2.352, gt=64, accept=False)
        d = self.sample(r, 69.5, 21.4, 2.352, et=65, accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(d.house_load_basis, "prior_coherent_house_grid_first_hold")

    def test_later_accepted_lower_offer_cannot_be_replaced_by_old_start_cap(self):
        r = self.startup()
        r.settings.enable_soft_guard = True
        r.settings.soft_limit_amps = 9
        r.settings.soft_for_seconds = 0
        r.settings.soft_current_amps = 7
        self.assertEqual(self.grid_first(r).current_p1, 7)
        self.assertEqual(r.last_target[0], 7)
        later = self.sample(r, 70, 21.4, 16.157, gt=69.5)
        self.assertLessEqual(later.current_p1, 7)
        self.assertNotEqual(later.command, "resume")

    def test_inactive_dry_run_and_manual_wait_never_issue_a_new_hold_command(self):
        for option in ("active_control", "dry_run"):
            with self.subTest(option=option):
                r = self.startup()
                setattr(r.settings, option, option == "dry_run")
                d = self.grid_first(r)
                self.assertFalse(d.should_call_service and not d.dry_run)
        for status in ("paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(status=status):
                r = self.startup()
                d = self.grid_first(r, status=status)
                self.assertNotEqual(d.house_load_basis, "prior_coherent_house_grid_first_hold")
                self.assertNotEqual(d.command, "resume")

    def test_disabled_limiter_cannot_retain_context_or_bypass_blocked_overload(self):
        r = self.running_sixteen()
        r.settings.enable_dynamic_limit = False
        d = self.sample(r, 10, 26.2, 16.165)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._nominal_shedding_started)
        r = self.startup()
        r.settings.enable_dynamic_limit = False
        d = self.grid_first(r, accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(d.house_load_basis, "prior_coherent_house_grid_first_hold")
        self.assertIsNone(r._positive_grid_first_ts)

    def test_zero_measurement_window_cannot_grant_even_first_nominal_bypass(self):
        r = self.running_sixteen()
        r.settings.measurement_settle_seconds = 0
        d = self.sample(r, 10, 26.2, 16.165)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(r._nominal_shedding_started)
        r = self.startup()
        r.settings.measurement_settle_seconds = 0
        self.assertEqual(self.grid_first(r).current_p1, 0)

    def test_fresh_negative_ev_then_receipt_regression_cannot_reopen_failed_zero(self):
        r = self.startup()
        self.grid_first(r, accept=False)
        stopped = self.sample(r, 70, 21.4, 2.352, gt=69.5, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertTrue(r._restart_required)
        self.assertEqual(r.last_target[0], 16)  # zero submission was NOT accepted
        regressed = self.sample(r, 71, 21.4, 2.352, et=65, accept=False)
        self.assertEqual(regressed.current_p1, 0)
        self.assertNotEqual(regressed.house_load_basis, "prior_coherent_house_grid_first_hold")
        self.assertIsNone(r._positive_cap)
        fresh_high = self.sample(r, 72, 21.4, 16.157, accept=False)
        self.assertEqual(fresh_high.current_p1, 0)
        self.assertNotEqual(fresh_high.command, "resume")

    def test_raw_envelope_recovery_cannot_reopen_exception_after_unaccepted_zero(self):
        for excess in (23.0001, 22.2, 38.001):
            with self.subTest(excess=excess):
                r = self.startup()
                self.grid_first(r, accept=False)
                self.assertEqual(self.grid_first(r, now=70, grid=excess, accept=False).current_p1, 0)
                later = self.grid_first(r, now=71, accept=False)
                self.assertEqual(later.current_p1, 0)
                self.assertIsNone(r._positive_cap)

    def test_exception_is_consumed_even_when_intermediate_nominal_snapshot_needs_no_zero(self):
        r = self.startup()
        self.grid_first(r, accept=False)
        coherent_partial = self.sample(r, 70, 8, 2.352, et=65, accept=False)
        self.assertEqual(coherent_partial.current_p1, 16)
        self.assertTrue(r._positive_grid_first_closed)
        later = self.grid_first(r, now=71, accept=False)
        self.assertEqual(later.current_p1, 0)

    def test_nominal_hard_failure_cannot_reopen_old_accepted_twelve_without_zero_ack(self):
        r = self.running_sixteen()
        self.sample(r, 10, 26.2, 16.165)
        self.assertEqual(r.last_target[0], 12)
        stopped = self.sample(r, 11, 29.1, 16.165, accept=False)
        self.assertEqual(stopped.current_p1, 0)
        self.assertTrue(r._restart_required)
        self.assertEqual(r.last_target[0], 12)
        returned = self.sample(r, 12, 26.2, 16.165, accept=False)
        self.assertEqual(returned.current_p1, 0)
        # Even an EV-first decrease with a now nominal raw reading must obey
        # restart observation, rather than taking an old reduction early exit.
        ev_first = self.sample(r, 13, 22, 6, gt=12, accept=False)
        self.assertEqual(ev_first.current_p1, 0)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assert_latch_preserved(r)

    def test_zero_soft_guard_attempt_is_terminal_before_http_ack(self):
        r = self.startup()
        r.settings.enable_soft_guard = True
        r.settings.soft_limit_amps = 9
        r.settings.soft_for_seconds = 0
        r.settings.soft_current_amps = 0
        self.assertEqual(self.grid_first(r, accept=False).current_p1, 0)
        r.settings.enable_soft_guard = False
        later = self.grid_first(r, now=71, accept=False)
        self.assertEqual(later.current_p1, 0)

    def test_accepted_same_cap_restart_retires_only_previous_nominal_attempt(self):
        r = self.running_sixteen()
        self.sample(r, 10, 26.2, 16.165)
        self.assertEqual(r.last_target[0], 12)
        self.sample(r, 11, 29.1, 16.165, accept=False)
        self.assertEqual(r._nominal_shedding_started, 10)
        self.sample(r, 12, 11, .023, status="awaiting_start", accept=False)
        proposed = self.sample(r, 72, 11, .023, status="awaiting_start", accept=False)
        self.assertEqual((proposed.current_p1, proposed.should_call_service), (12, True))
        self.assertEqual(r._nominal_shedding_started, 10)
        self.assertTrue(r._restart_required)
        r.acknowledge(proposed, 72)
        self.assertIsNone(r._nominal_shedding_started)
        self.assertFalse(r._restart_required)
        self.assertEqual((r._positive_cap, r._positive_house), (12, 11))
        ev_first = self.sample(r, 76, 11, 12, gt=72)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("settling", 12))
        self.assertEqual(ev_first.house_load_l1, 11)
        self.assertFalse(ev_first.should_call_service)
        self.sample(r, 78, 23, 12, et=76)
        self.assertIsNone(r._positive_cap)
        new_shedding = self.sample(r, 80, 26, 12)
        self.assertEqual((new_shedding.mode, new_shedding.current_p1), ("normal", 9))
        self.assertEqual(r._nominal_shedding_started, 80)
        self.assert_latch_preserved(r)

    def test_unconfirmed_zero_residual_can_restart_only_after_full_raw_observation(self):
        r = self.unconfirmed_zero()
        for now, et in ((2, 2), (31, 2), (61.9, 32)):
            d = self.sample(r, now, 10, .697, status="awaiting_start", et=et)
            self.assertEqual(d.current_p1, 0)
            self.assertEqual(r._awaiting_reduction_target, 0)
            self.assertEqual(d.house_load_basis, "raw_grid_unconfirmed_zero_restart")
            self.assertEqual(d.house_load_l1, 10)
            self.assertEqual(r._restart_ready_since, 2)
        offered = self.sample(r, 62, 10, .697, status="awaiting_start", et=32, accept=False)
        self.assertEqual((offered.current_p1, offered.should_call_service), (13, True))
        self.assertEqual(r._awaiting_reduction_target, 0)
        self.assertEqual(r.last_target[0], 0)  # failure/proposal proves no response
        r.acknowledge(offered, 62)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual((r._positive_cap, r._positive_house), (13, 10))
        self.assert_latch_preserved(r)

    def test_unconfirmed_zero_restart_uses_lowest_whole_raw_cap_throughout_period(self):
        r = self.unconfirmed_zero()
        self.sample(r, 2, 10, .697, status="awaiting_start")
        self.sample(r, 31, 16, .697, status="awaiting_start")
        d = self.sample(r, 62, 7, .697, status="awaiting_start")
        self.assertEqual(d.current_p1, 7)
        self.assertEqual((r._positive_cap, r._positive_house), (7, 7))
        self.assertLessEqual(16 + d.current_p1, 23)

    def test_unconfirmed_zero_insufficient_raw_budget_resets_restart_observation(self):
        r = self.unconfirmed_zero()
        self.sample(r, 2, 10, .697, status="awaiting_start")
        no_room = self.sample(r, 31, 16.1, .697, status="awaiting_start")
        self.assertEqual(no_room.current_p1, 0)
        self.assertIsNone(r._restart_ready_since)
        self.assertEqual(self.sample(r, 62, 10, .697, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(r, 122, 10, .697, status="awaiting_start").current_p1, 13)

    def test_unconfirmed_zero_freshness_fault_or_forbidden_status_resets_observation(self):
        cases = [dict(data_valid=False), dict(charger_report_ts=None), dict(grid_report_ts=None),
                 dict(wall_now_ts=None), dict(charger_report_ts=True),
                 dict(grid_report_ts=EPOCH-200), dict(charger_report_ts=EPOCH+40),
                 dict(status="paused"), dict(status="awaiting_authorization"),
                 dict(status="awaiting_scheduled_start"), dict(status="paused_due_to_equalizer")]
        for case in cases:
            with self.subTest(case=case):
                r = self.unconfirmed_zero()
                self.sample(r, 2, 10, .697, status="awaiting_start")
                options = dict(status="awaiting_start")
                options.update(case)
                d = self.sample(r, 31, 10, .697, **options)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(r._restart_ready_since)
                self.assertEqual(self.sample(r, 62, 10, .697, status="awaiting_start").current_p1, 0)

    def test_unconfirmed_zero_still_cannot_claim_meaningful_charging_is_stopped(self):
        r = self.unconfirmed_zero()
        for now in (2, 62, 602):
            d = self.sample(r, now, 10, 1.1)
            self.assertEqual(d.current_p1, 0)
            self.assertIsNone(r._restart_ready_since)
            self.assertEqual(r._awaiting_reduction_target, 0)

    def test_unconfirmed_zero_respects_custom_soft_limit_and_pause_ownership(self):
        for setting in ("soft", "owned_pause", "disabled"):
            with self.subTest(setting=setting):
                r = self.unconfirmed_zero()
                if setting == "soft":
                    r.settings.enable_soft_guard = True
                    r.settings.soft_limit_amps = 9
                    r.settings.soft_for_seconds = 0
                    r.settings.soft_current_amps = 0
                elif setting == "owned_pause":
                    r.paused_by_us = True
                else:
                    r.settings.enable_dynamic_limit = False
                for now in (2, 62, 602):
                    d = self.sample(r, now, 10, .697, status="awaiting_start")
                    self.assertEqual(d.current_p1, 0)
                    self.assertNotEqual(d.command, "resume")
                    self.assertIsNone(r._restart_ready_since)

    def test_disabling_limiter_during_raw_zero_observation_consumes_no_ready_credit(self):
        r = self.unconfirmed_zero()
        self.sample(r, 2, 10, .697, status="awaiting_start")
        self.assertEqual(r._restart_ready_since, 2)
        r.settings.enable_dynamic_limit = False
        self.sample(r, 31, 10, .697, status="awaiting_start")
        self.assertIsNone(r._restart_ready_since)
        self.assertIsNone(r._raw_zero_restart_cap)
        r.settings.enable_dynamic_limit = True
        self.assertEqual(self.sample(r, 62, 10, .697, status="awaiting_start").current_p1, 0)
        self.assertEqual(self.sample(r, 122, 10, .697, status="awaiting_start").current_p1, 13)

    def test_aged_numeric_ev_gap_preserves_raw_observation_but_offers_only_zero(self):
        for ev, status in ((.697, "awaiting_start"), (16, "charging")):
            with self.subTest(ev=ev, status=status):
                r = self.unconfirmed_zero()
                self.sample(r, 2, 10, .697, status="awaiting_start")
                for now in (31, 61, 62):
                    d = self.sample(r, now, 10, ev, status=status, et=-200)
                    self.assertEqual(d.current_p1, 0)
                    self.assertEqual(r._awaiting_reduction_target, 0)
                    self.assertEqual(r._restart_ready_since, 2)
                    self.assertIsNone(r._positive_cap)
                resumed = self.sample(r, 63, 10, .697, status="awaiting_start")
                self.assertEqual(resumed.current_p1, 13)
                self.assertEqual((r._positive_cap, r._positive_house), (13, 10))

    def test_aged_numeric_ev_cannot_begin_raw_observation_or_hide_fresh_contradiction(self):
        r = self.unconfirmed_zero()
        for now in (2, 62, 602):
            d = self.sample(r, now, 10, .697, status="awaiting_start", et=-200)
            self.assertEqual(d.current_p1, 0)
            self.assertIsNone(r._restart_ready_since)
        self.sample(r, 603, 10, .697, status="awaiting_start")
        contradiction = self.sample(r, 604, 10, 16)
        self.assertEqual(contradiction.current_p1, 0)
        self.assertIsNone(r._restart_ready_since)
        self.assertEqual(self.sample(r, 663, 10, .697, status="awaiting_start").current_p1, 0)

    def test_aged_ev_gap_retains_raw_soft_and_pause_observation(self):
        for guard in ("soft", "pause"):
            with self.subTest(guard=guard):
                r = self.unconfirmed_zero()
                r.settings.enable_soft_guard = guard == "soft"
                r.settings.soft_limit_amps = 9
                r.settings.soft_for_seconds = 30
                r.settings.soft_current_amps = 0
                r.settings.enable_pause_resume = guard == "pause"
                r.settings.pause_limit_amps = 9
                r.settings.pause_for_seconds = 30
                self.sample(r, 2, 10, .697, status="awaiting_start")
                for now in (20, 40, 61):
                    self.assertEqual(self.sample(r, now, 10, 16, et=-200).current_p1, 0)
                blocked = self.sample(r, 62, 10, .697, status="awaiting_start")
                self.assertEqual(blocked.current_p1, 0)
                self.assertIsNone(r._restart_ready_since)
                self.assertIsNone(r._positive_cap)

    def test_old_half_amp_zero_confirmation_keeps_its_existing_receipt_contract(self):
        r = self.unconfirmed_zero()
        self.sample(r, 2, 10, .49, status="awaiting_start")
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertIsNone(r._raw_zero_restart_cap)
        self.assertEqual(self.sample(r, 62, 10, .49, status="awaiting_start").current_p1, 13)


if __name__ == "__main__":
    unittest.main(verbosity=2)
