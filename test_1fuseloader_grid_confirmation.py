"""Synthetic post-reduction grid confirmation; no HA, API or household data.

The EV and grid events deliberately have different capture/receipt order.
A reported EV decrease is not proof that the total import is below its limit.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest

import test_1fuseloader_b19_alignment as fixtures
import test_1fuseloader_overload_handoff as overload_fixtures
import test_1fuseloader_trial_epochs as trial_fixtures

logic = fixtures.logic
EPOCH = fixtures.fixtures.EPOCH
CONST_PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "const.py"
CONST_SPEC = importlib.util.spec_from_file_location("grid_confirmation_const", CONST_PATH)
const = importlib.util.module_from_spec(CONST_SPEC)
sys.modules[CONST_SPEC.name] = const
CONST_SPEC.loader.exec_module(const)


class GridConfirmationTests(unittest.TestCase):
    sample = fixtures.LowGridAlignmentTests.sample
    regulator = fixtures.LowGridAlignmentTests.regulator
    running = fixtures.LowGridAlignmentTests.running

    def pending(self, *, accept=True, grid_time=40, **options):
        options.setdefault("grid_confirmation_seconds", 30)
        r = self.running(**options)
        if options.get("normal_ttl_seconds", 60) < 40:
            self.sample(r, 35, 21.7, 16.2)
        d = self.sample(r, 40, 30.2, 16.2, gt=grid_time, et=35,
                        accept=False)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding", 7))
        self.assertEqual(d.nominal_response_deadline_ts,
                         grid_time + options.get("overload_response_seconds", 10))
        if accept:
            r.acknowledge(d, 40.2)
        return r

    def waiting(self, **options):
        r = self.pending(**options)
        d = self.sample(r, 46.5, 30.2, 7.08, gt=44)
        self.assertEqual((d.mode, d.current_p1, d.command),
                         ("normal_shedding_grid_wait", 7, None))
        return r, d

    def assert_preserved(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 2000)
        self.assertIsNone(state.get("trial_epoch"))
        self.assertFalse(state["minimum_held"])
        self.assertFalse(state["transition_pending"])

    def test_timely_ev_response_survives_old_deadline_then_later_grid_completes(self):
        r, d = self.waiting()
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 76.5)
        self.assertIsNone(d.nominal_response_deadline_ts)
        waiting = self.sample(r, 50, 30.2, 7.08, gt=44, et=46.5)
        self.assertEqual((waiting.mode, waiting.current_p1, waiting.command),
                         ("normal_shedding_grid_wait", 7, None))
        complete = self.sample(r, 50.9, 21.3, 7.08, gt=50, et=46.5)
        self.assertEqual((complete.mode, complete.current_p1),
                         ("normal_shedding_complete", 7))
        self.assertLessEqual(complete.house_load_l1 + 7, 23)
        self.assertIsNone(complete.grid_confirmation_deadline_ts)
        self.assert_preserved(r)

    def test_default_opt_out_keeps_the_original_ten_second_deadline(self):
        r = self.pending(grid_confirmation_seconds=0)
        self.sample(r, 46.5, 30.2, 7.08, gt=44)
        stopped = self.sample(r, 50, 30.2, 7.08, gt=44, et=46.5)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(stopped.grid_confirmation_deadline_ts)

    def test_no_timely_ev_response_still_stops_at_ten_seconds(self):
        r = self.pending()
        self.assertEqual(self.sample(r, 49.9, 30.2, 16.2, et=35).current_p1, 7)
        stopped = self.sample(r, 50, 30.2, 16.2, et=35)
        self.assertEqual((stopped.mode, stopped.current_p1),
                         ("normal_shedding_stop", 0))
        self.assertIsNone(stopped.grid_confirmation_deadline_ts)
        self.assertIn("response_timeout", stopped.reason)

    def test_response_at_or_after_physical_deadline_cannot_start_grid_wait(self):
        for when in (50, 50.01, 51):
            with self.subTest(when=when):
                r = self.pending()
                d = self.sample(r, when, 30.2, 7.08, gt=44)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_late_get_cannot_backfill_a_timely_event_after_deadline(self):
        r = self.pending()
        d = self.sample(r, 51, 30.2, 7.08, gt=44, et=46.5,
                        charger_snapshot_verified=True)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_late_ev_and_later_grid_cannot_bypass_the_physical_deadline(self):
        for event in (49.5, 50, 50.5, 51):
            with self.subTest(event=event):
                r = self.pending()
                d = self.sample(r, 51, 21.3, 7.08, gt=51, et=event,
                                charger_snapshot_verified=True)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_no_ack_and_events_before_or_equal_ack_do_not_start_grid_wait(self):
        r = self.pending(accept=False)
        d = self.sample(r, 46.5, 30.2, 7.08, gt=44, accept=False)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertEqual(self.sample(r, 50, 30.2, 7.08, gt=44,
                                     accept=False).current_p1, 0)
        for event in (40, 40.2):
            with self.subTest(event=event):
                r = self.pending()
                d = self.sample(r, 46.5, 30.2, 7.08, gt=44, et=event)
                self.assertIsNone(d.grid_confirmation_deadline_ts)
                self.assertEqual(self.sample(r, 50, 30.2, 7.08, gt=44,
                                             et=event).current_p1, 0)

    def test_wait_emits_no_write_resume_or_lease_renewal(self):
        r, first = self.waiting(resend_limit_every_seconds=1)
        accepted_at = r.last_set_limit_ts
        for now in (47, 50, 60, 70, 76.4):
            d = self.sample(r, now, 30.2, 7.08, gt=44, et=46.5)
            self.assertEqual((d.current_p1, d.command, d.service,
                              d.should_call_service), (7, None, None, False))
            self.assertEqual(d.grid_confirmation_deadline_ts,
                             first.grid_confirmation_deadline_ts)
            self.assertEqual(r.last_set_limit_ts, accepted_at)
        self.assert_preserved(r)

    def test_new_ev_receipts_gets_and_jitter_do_not_renew_first_deadline(self):
        r, first = self.waiting()
        for now in (48, 50, 60, 70):
            d = self.sample(r, now, 30.2, 7.04, gt=44, et=now,
                            charger_snapshot_verified=True)
            self.assertEqual(d.current_p1, 7)
            self.assertEqual(d.grid_confirmation_deadline_ts,
                             first.grid_confirmation_deadline_ts)
            self.assertFalse(d.should_call_service)
        stopped = self.sample(r, 76.5, 30.2, 7.04, gt=44, et=70)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIn("grid_confirmation_timeout", stopped.reason)
        self.assert_preserved(r)

    def test_unchanged_current_events_do_not_starve_a_later_grid_proof(self):
        r, first = self.waiting()
        self.sample(r, 48, 30.2, 7.04, gt=44)
        d = self.sample(r, 50.5, 21.3, 7.04, gt=50, et=50.4)
        self.assertEqual((d.mode, d.current_p1),
                         ("normal_shedding_complete", 7))
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertEqual(first.grid_confirmation_deadline_ts, 76.5)

    def test_substantive_current_changes_need_later_grid_but_do_not_refund_time(self):
        r, first = self.waiting()
        self.sample(r, 48, 30.2, 1, gt=44)
        d = self.sample(r, 50.2, 21.3, 7.08, gt=49, et=50)
        self.assertEqual((d.mode, d.current_p1),
                         ("normal_shedding_grid_wait", 7))
        self.assertEqual(d.grid_confirmation_deadline_ts,
                         first.grid_confirmation_deadline_ts)
        complete = self.sample(r, 50.3, 21.3, 7.08, gt=50.1, et=50)
        self.assertEqual((complete.mode, complete.current_p1),
                         ("normal_shedding_complete", 7))

    def test_changed_current_cannot_reuse_the_same_retained_source_event(self):
        r, _ = self.waiting()
        d = self.sample(r, 48, 30.2, 6, gt=44, et=46.5)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_changed_current_cannot_hide_a_new_high_grid_after_first_minimum(self):
        r, _ = self.waiting()
        d = self.sample(r, 48, 30.2, 1, gt=47)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_wait_timeout_is_terminal_even_if_low_grid_arrives_next(self):
        r, _ = self.waiting()
        stopped = self.sample(r, 76.5, 30.2, 7.08, gt=44, et=46.5)
        self.assertEqual((stopped.mode, stopped.current_p1),
                         ("normal_shedding_stop", 0))
        self.assertIn("grid_confirmation_timeout", stopped.reason)
        late = self.sample(r, 76.6, 21.3, 7.08, gt=76.6, et=46.5)
        self.assertEqual(late.current_p1, 0)
        self.assertIsNone(late.grid_confirmation_deadline_ts)

    def test_new_high_grid_is_not_hidden_inside_thirty_second_wait(self):
        for grid in (23.001, 25, 28, 30.2, 38, 38.001):
            with self.subTest(grid=grid):
                r, _ = self.waiting()
                d = self.sample(r, 48, grid, 7.08, gt=47, et=46.5)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)
                self.assert_preserved(r)

    def test_equal_ev_grid_clock_is_not_later_confirmation(self):
        r, _ = self.waiting()
        equal = self.sample(r, 48, 21.3, 7.08, gt=46.5, et=46.5)
        self.assertEqual((equal.mode, equal.current_p1),
                         ("normal_shedding_grid_wait", 7))
        d = self.sample(r, 48.1, 21.3, 7.08, gt=46.6, et=46.5)
        self.assertEqual((d.mode, d.current_p1),
                         ("normal_shedding_complete", 7))

    def test_equal_clock_cannot_order_high_grid_but_strictly_later_high_is_protective(self):
        r, _ = self.waiting()
        d = self.sample(r, 48, 30.2, 7.08, gt=46.5, et=46.5)
        self.assertEqual((d.mode, d.current_p1, d.command), ("normal_shedding_grid_wait", 7, None))
        deadline = d.grid_confirmation_deadline_ts
        later = self.sample(r, 48.1, 30.2, 7.08, gt=46.6, et=46.5)
        self.assertEqual(later.current_p1, 0)
        self.assertIsNotNone(deadline)

    def test_exact_buffer_boundary_can_confirm_nominal_response(self):
        r, _ = self.waiting()
        d = self.sample(r, 48, 23, 7.08, gt=47, et=46.5)
        self.assertEqual((d.mode, d.current_p1),
                         ("normal_shedding_complete", 7))
        self.assertLessEqual(d.house_load_l1 + 7, 23)

    def test_raw_peak_ceiling_applies_even_to_a_pre_response_grid_clock(self):
        r, _ = self.waiting()
        d = self.sample(r, 48, 38.001, 7.08, gt=44, et=46.5)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("raw_peak_exceeded", d.reason)

    def test_current_above_accepted_cap_is_not_a_continuing_response(self):
        for ev in (7.501, 9, 16.2):
            with self.subTest(ev=ev):
                r, _ = self.waiting()
                d = self.sample(r, 48, 30.2, ev, gt=44)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_faults_manual_wait_and_untrusted_current_stop_immediately(self):
        for metadata in ({"data_valid": False},
                         {"data_error": "synthetic_source_fault"},
                         {"charger_current_trusted": False},
                         {"status": "paused"},
                         {"status": "awaiting_scheduled_start"}):
            with self.subTest(metadata=metadata):
                r, _ = self.waiting()
                d = self.sample(r, 48, 30.2, 7.08, gt=44, et=46.5,
                                **metadata)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_missing_future_regressed_and_changed_source_clocks_stop(self):
        cases = ({"grid_report_ts": None}, {"charger_report_ts": None},
                 {"wall_now_ts": None}, {"gt": 49}, {"et": 49},
                 {"gt": 43}, {"et": 45}, {"identity": ("other-source",)})
        for metadata in cases:
            with self.subTest(metadata=metadata):
                r, _ = self.waiting()
                values = dict(gt=44, et=46.5)
                values.update(metadata)
                d = self.sample(r, 48, 30.2, 7.08, **values)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_post_response_window_is_aged_from_event_not_later_receipt(self):
        r = self.pending(grid_time=36)
        d = self.sample(r, 45.8, 30.2, 7.08, gt=44, et=45.5)
        self.assertEqual(d.mode, "normal_shedding_grid_wait")
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 75.5)
        self.assertEqual(self.sample(r, d.grid_confirmation_deadline_ts,
                                     30.2, 7.08, gt=44,
                                     et=45.5).current_p1, 0)

    def test_independent_live_lease_can_shorten_the_post_response_window(self):
        r, d = self.waiting(normal_ttl_seconds=12)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 52.2)
        self.assertEqual(self.sample(r, 52.2, 30.2, 7.08, gt=44,
                                     et=46.5).current_p1, 0)

    def test_setting_changes_do_not_renew_or_reopen_an_existing_wait(self):
        for changes in ({"grid_confirmation_seconds": 0},
                        {"grid_confirmation_seconds": 15},
                        {"measurement_settle_seconds": 30},
                        {"overload_response_seconds": 5}):
            with self.subTest(changes=changes):
                r, _ = self.waiting()
                r.update_settings(replace(r.settings, **changes))
                d = self.sample(r, 48, 30.2, 7.08, gt=44, et=46.5)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_reset_does_not_create_another_grid_confirmation_period(self):
        r, _ = self.waiting()
        r.reset()
        d = self.sample(r, 48, 30.2, 7.08, gt=44, et=46.5)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assert_preserved(r)

    def test_restart_interrupts_pending_wait_without_refunding_history(self):
        r, _ = self.waiting()
        state = deepcopy(r.export_overload_state())
        self.assertTrue(state["normal_control"]["response_pending"])
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=100))
        d = self.sample(restored, 1000, 21.3, 7.08, et=999)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertGreaterEqual(restored.export_overload_state()["used_seconds"],
                                state["used_seconds"])
        self.assertEqual(restored.export_overload_state()["failure_reason"],
                         "response_timeout")


class ActiveOverloadGridConfirmationTests(unittest.TestCase):
    regulator = overload_fixtures.OverloadHandoffTests.regulator
    higher_offer = overload_fixtures.OverloadHandoffTests.higher_offer

    def sample(self, regulator, now, grid, ev, **metadata):
        metadata.setdefault("control_source_id", ("synthetic-charger", "synthetic-grid", "L1"))
        return overload_fixtures.OverloadHandoffTests.sample(
            self, regulator, now, grid, ev, **metadata)

    def pending(self, *, accept=True, used_seconds=0, accepted_ttl=None, **options):
        options.setdefault("grid_confirmation_seconds", 30)
        r = self.higher_offer(**options)
        if used_seconds:
            self.assertTrue(r.import_overload_state(dict(
                version=1, used_seconds=used_seconds, exposure_active=False,
                transition_pending=False, minimum_held=False,
                failure_reason=None)))
        d = self.sample(r, 5, 30.2, 11, ev_at=0, accept=False)
        self.assertEqual((d.mode, d.current_p1), ("overload_shedding", 6))
        self.assertEqual(d.overload_transition_remaining_seconds, 10)
        if accept:
            # A deliberately shorter accepted pure-logic lease tests the
            # independent bound; the real overload adapter uses sixty seconds.
            accepted = d if accepted_ttl is None else replace(d, ttl_seconds=accepted_ttl)
            r.acknowledge(accepted, 5.2)
        return r

    def waiting(self, **options):
        r = self.pending(**options)
        d = self.sample(r, 11.5, 30.2, 6.08, grid_at=9)
        self.assertEqual((d.mode, d.current_p1, d.command),
                         ("overload_grid_wait", 6, None))
        return r, d

    def test_active_allowance_waits_for_grid_after_timely_minimum(self):
        r, first = self.waiting()
        self.assertAlmostEqual(first.grid_confirmation_deadline_ts, 41.5)
        held = self.sample(r, 15, 30.2, 6.08, grid_at=9, ev_at=11.5)
        self.assertEqual((held.mode, held.current_p1, held.command),
                         ("overload_grid_wait", 6, None))
        final = self.sample(r, 15.9, 28.5, 6.08, grid_at=15, ev_at=11.5)
        self.assertEqual((final.mode, final.current_p1), ("overload_minimum", 6))
        self.assertIsNone(final.grid_confirmation_deadline_ts)
        self.assertFalse(final.overload_failure_latched)
        self.assertAlmostEqual(final.overload_used_seconds, 10.9)

    def test_no_ev_response_keeps_original_ten_second_cutoff(self):
        r = self.pending()
        self.assertEqual(self.sample(r, 14.9, 30.2, 11, ev_at=0).current_p1, 6)
        d = self.sample(r, 15, 30.2, 11, ev_at=0)
        self.assertEqual((d.mode, d.current_p1), ("overload_stop", 0))
        self.assertIn("response_timeout", d.reason)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_late_ev_or_get_cannot_create_post_response_time(self):
        for event in (14.5, 15, 15.5, 16):
            with self.subTest(event=event):
                r = self.pending()
                d = self.sample(r, 16, 28.5, 6.08, grid_at=16,
                                ev_at=event, charger_snapshot_verified=True)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_wait_neither_renews_lease_nor_refunds_exposure_time(self):
        r, first = self.waiting(resend_limit_every_seconds=1)
        accepted_at = r.last_set_limit_ts
        for now in (12, 15, 25, 35, 41.4):
            d = self.sample(r, now, 30.2, 6.04, grid_at=9,
                            ev_at=now, charger_snapshot_verified=True)
            self.assertEqual((d.current_p1, d.command, d.should_call_service),
                             (6, None, False))
            self.assertEqual(d.grid_confirmation_deadline_ts,
                             first.grid_confirmation_deadline_ts)
            self.assertEqual(r.last_set_limit_ts, accepted_at)
            self.assertAlmostEqual(d.overload_used_seconds, now - 5)
        d = self.sample(r, 41.5, 30.2, 6.04, grid_at=9, ev_at=41.4)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("grid_confirmation_timeout", d.reason)
        self.assertAlmostEqual(d.overload_used_seconds, 36.5)

    def test_new_post_minimum_grid_above_twenty_nine_stops(self):
        for grid in (29.001, 30.2, 38, 38.001):
            with self.subTest(grid=grid):
                r, _ = self.waiting()
                d = self.sample(r, 12, grid, 6.08, grid_at=12, ev_at=11.5)
                self.assertEqual(d.current_p1, 0)
                self.assertTrue(d.overload_failure_latched)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_twenty_nine_exactly_can_complete_the_confirmed_minimum(self):
        r, _ = self.waiting()
        d = self.sample(r, 12, 29, 6.08, grid_at=12, ev_at=11.5)
        self.assertEqual((d.mode, d.current_p1), ("overload_minimum", 6))
        self.assertFalse(d.overload_failure_latched)

    def test_hard_peak_and_current_reversal_override_old_grid_wait(self):
        r, _ = self.waiting()
        d = self.sample(r, 12, 38.001, 6.08, grid_at=9, ev_at=11.5)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("transition_current_exceeded", d.reason)
        r, _ = self.waiting()
        d = self.sample(r, 12, 30.2, 11, grid_at=9)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_finite_exposure_budget_still_exhausts_inside_grid_wait(self):
        r, _ = self.waiting(used_seconds=1775)
        d = self.sample(r, 31, 30.2, 6.08, grid_at=9, ev_at=11.5)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("cumulative_budget_exhausted", d.reason)
        self.assertGreaterEqual(d.overload_used_seconds, 1800)

    def test_fault_pause_clock_and_binding_changes_override_grid_wait(self):
        cases = ({"data_valid": False}, {"charger_current_trusted": False},
                 {"status": "paused"}, {"grid_report_ts": None},
                 {"charger_report_ts": None}, {"grid_at": 8},
                 {"ev_at": 10}, {"control_source_id": ("other-source",)})
        for metadata in cases:
            with self.subTest(metadata=metadata):
                r, _ = self.waiting()
                values = dict(grid_at=9, ev_at=11.5)
                values.update(metadata)
                d = self.sample(r, 12, 30.2, 6.08, **values)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_accepted_lease_can_expire_before_the_new_confirmation_window(self):
        r, d = self.waiting(accepted_ttl=10)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 15.2)
        stopped = self.sample(r, 15.2, 30.2, 6.08, grid_at=9, ev_at=11.5)
        self.assertEqual(stopped.current_p1, 0)

    def test_restart_does_not_restore_an_active_observation_window(self):
        r, _ = self.waiting()
        state = deepcopy(r.export_overload_state())
        self.assertTrue(state["transition_pending"])
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=100))
        d = self.sample(restored, 1000, 28.5, 6.08, ev_at=999)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertGreaterEqual(restored.export_overload_state()["used_seconds"],
                                state["used_seconds"])


class ExplicitTrialGridConfirmationTests(unittest.TestCase):
    regulator = trial_fixtures.ExplicitTrialTests.regulator
    frame = trial_fixtures.ExplicitTrialTests.frame
    step = trial_fixtures.ExplicitTrialTests.step
    ready = trial_fixtures.ExplicitTrialTests.ready
    prepare = trial_fixtures.ExplicitTrialTests.prepare
    activate = trial_fixtures.ExplicitTrialTests.activate
    epoch = trial_fixtures.ExplicitTrialTests.epoch

    def pending(self):
        r, inp = self.ready(grid_confirmation_seconds=30)
        self.activate(r, inp)
        d = self.step(r, 3, 30.2, 16.1, accept=False)
        self.assertEqual((d.mode, d.current_p1), ("overload_shedding", 7))
        r.acknowledge(d, 3.2)
        return r

    def waiting(self):
        r = self.pending()
        d = self.step(r, 9.5, 30.2, 7.08, gt=7)
        self.assertEqual((d.mode, d.current_p1, d.command),
                         ("overload_grid_wait", 7, None))
        return r, d

    def assert_history(self, r):
        state = r.export_overload_state()
        self.assertGreaterEqual(state["used_seconds"], trial_fixtures.LIFETIME)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(self.epoch(r)["trial_id"], trial_fixtures.TRIAL_A)
        self.assertEqual(self.epoch(r)["baseline_used_seconds"], trial_fixtures.LIFETIME)

    def test_explicit_trial_uses_seven_and_same_fixed_post_response_window(self):
        r, first = self.waiting()
        self.assertAlmostEqual(first.grid_confirmation_deadline_ts, 39.5)
        held = self.step(r, 13, 30.2, 7.08, gt=7, et=9.5)
        self.assertEqual((held.mode, held.current_p1, held.command),
                         ("overload_grid_wait", 7, None))
        d = self.step(r, 13.9, 28.5, 7.08, gt=13, et=9.5)
        self.assertEqual((d.mode, d.current_p1), ("overload_minimum", 7))
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertAlmostEqual(r.effective_overload_used_seconds, 10.9)
        self.assertEqual(self.epoch(r)["state"], "active")
        self.assert_history(r)

    def test_no_minimum_response_still_stops_in_original_ten_seconds(self):
        r = self.pending()
        d = self.step(r, 13, 30.2, 16.1, et=3)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("response_timeout", d.reason)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assert_history(r)

    def test_event_storm_cannot_renew_trial_or_confirmation_budget(self):
        r, first = self.waiting()
        accepted_at = r.last_set_limit_ts
        for now in (10, 13, 20, 30, 39.4):
            d = self.step(r, now, 30.2, 7.04, gt=7, et=now,
                          charger_snapshot_verified=True)
            self.assertEqual((d.current_p1, d.command, d.should_call_service),
                             (7, None, False))
            self.assertEqual(d.grid_confirmation_deadline_ts,
                             first.grid_confirmation_deadline_ts)
            self.assertEqual(r.last_set_limit_ts, accepted_at)
            self.assertAlmostEqual(r.effective_overload_used_seconds, now - 3)
            self.assert_history(r)
        d = self.step(r, 39.5, 30.2, 7.04, gt=7, et=39.4)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("grid_confirmation_timeout", d.reason)
        self.assertAlmostEqual(r.effective_overload_used_seconds, 36.5)
        self.assert_history(r)

    def test_fresh_high_grid_and_hard_peak_remain_protective(self):
        for grid, gt in ((29.001, 10), (38.001, 7)):
            with self.subTest(grid=grid):
                r, _ = self.waiting()
                d = self.step(r, 10, grid, 7.08, gt=gt, et=9.5)
                self.assertEqual(d.current_p1, 0)
                self.assertIsNone(d.grid_confirmation_deadline_ts)
                self.assert_history(r)

    def test_restored_trial_is_interrupted_without_new_confirmation_time(self):
        r, _ = self.waiting()
        state = deepcopy(r.export_overload_state())
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=100))
        self.assertEqual(self.epoch(restored)["state"], "interrupted")
        d = self.step(restored, 1000, 28.5, 7.08, et=999)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assert_history(restored)


class LegacyTrialPolicyGridConfirmationTests(unittest.TestCase):
    regulator = trial_fixtures.ExplicitTrialTests.regulator
    frame = trial_fixtures.ExplicitTrialTests.frame
    step = trial_fixtures.ExplicitTrialTests.step
    ready = trial_fixtures.ExplicitTrialTests.ready
    prepare = trial_fixtures.ExplicitTrialTests.prepare
    activate = trial_fixtures.ExplicitTrialTests.activate
    epoch = trial_fixtures.ExplicitTrialTests.epoch
    minimum = trial_fixtures.ExplicitTrialTests.minimum

    def active(self):
        r, inp = self.ready(grid_confirmation_seconds=0)
        self.activate(r, inp)
        return r

    def legacy(self, state):
        r = self.minimum()
        if state == "ended":
            r.end_overload_trial(self.frame(8, 23, 7.1, et=7))
        elif state == "failed":
            self.assertEqual(self.step(r, 8, 38.001, 7.1, et=7).current_p1, 0)
        self.assertEqual(self.epoch(r)["state"], state)
        saved = deepcopy(r.export_overload_state())
        self.assertEqual(saved["trial_epoch"]["policy"]["settings"].pop(
            "grid_confirmation_seconds"), 0)
        return saved

    def assert_saved_history(self, restored, saved):
        after = restored.export_overload_state()
        self.assertEqual(after["used_seconds"], saved["used_seconds"])
        self.assertEqual(after["failure_reason"], saved["failure_reason"])
        self.assertEqual(after["trial_epoch"]["baseline_used_seconds"],
                         saved["trial_epoch"]["baseline_used_seconds"])
        self.assertEqual(after["trial_epoch"]["trial_id"],
                         saved["trial_epoch"]["trial_id"])
        self.assertEqual(after["trial_epoch"]["policy"],
                         saved["trial_epoch"]["policy"])
        self.assertNotIn("grid_confirmation_seconds",
                         after["trial_epoch"]["policy"]["settings"])
        self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertEqual(restored.overload_minimum_current, 6)

    def test_legacy_ended_policy_import_preserves_the_original_frozen_record(self):
        saved = self.legacy("ended")
        original = deepcopy(saved)
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(saved))
        self.assert_saved_history(restored, original)
        self.assertEqual(self.epoch(restored), original["trial_epoch"])
        self.assertEqual(saved, original)
        self.assertEqual(self.step(restored, 1000, 38, 16.1).current_p1, 0)

    def test_legacy_failed_policy_import_preserves_closed_usage_and_failure(self):
        saved = self.legacy("failed")
        original = deepcopy(saved)
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(saved))
        self.assert_saved_history(restored, original)
        self.assertEqual(self.epoch(restored), original["trial_epoch"])
        self.assertEqual(saved, original)
        self.assertEqual(self.step(restored, 1000, 38, 16.1).current_p1, 0)

    def test_legacy_active_import_is_interrupted_without_rewriting_its_policy(self):
        saved = self.legacy("active")
        original = deepcopy(saved)
        restored = self.regulator(grid_confirmation_seconds=30)
        self.assertTrue(restored.import_overload_state(saved))
        self.assert_saved_history(restored, original)
        expected = deepcopy(original["trial_epoch"])
        expected.update(state="interrupted", failure_reason="interrupted_after_restore",
                        closed_used_seconds=original["used_seconds"]
                        - original["trial_epoch"]["baseline_used_seconds"])
        self.assertEqual(self.epoch(restored), expected)
        self.assertEqual(saved, original)
        self.assertEqual(self.step(restored, 1000, 38, 16.1).current_p1, 0)

    def test_missing_other_old_setting_is_not_treated_as_legacy_grid_default(self):
        for state in ("ended", "failed", "active"):
            for missing in ("buffer_amps", "normal_ttl_seconds", "overload_response_seconds"):
                with self.subTest(state=state, missing=missing):
                    bad = self.legacy(state)
                    del bad["trial_epoch"]["policy"]["settings"][missing]
                    original = deepcopy(bad)
                    restored = self.regulator(grid_confirmation_seconds=30)
                    self.assertFalse(restored.import_overload_state(bad))
                    self.assertEqual(restored.export_overload_state()["used_seconds"],
                                     original["used_seconds"])
                    self.assertEqual(restored.export_overload_state()["failure_reason"],
                                     original["failure_reason"])
                    self.assertEqual(self.epoch(restored)["failure_reason"],
                                     "invalid_persisted_trial")
                    self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                    self.assertEqual(bad, original)

    def test_unknown_or_invalid_new_policy_fields_are_not_silently_normalized(self):
        for state in ("ended", "failed", "active"):
            for field, value in (("grid_confirmation_seconds", 31),
                                 ("grid_confirmation_seconds", -1),
                                 ("grid_confirmation_seconds", False),
                                 ("grid_confirmation_seconds", 0.5),
                                 ("invented_grid_confirmation", 0)):
                with self.subTest(state=state, field=field, value=value):
                    bad = self.legacy(state)
                    bad["trial_epoch"]["policy"]["settings"][field] = value
                    original = deepcopy(bad)
                    restored = self.regulator(grid_confirmation_seconds=30)
                    self.assertFalse(restored.import_overload_state(bad))
                    self.assertEqual(restored.export_overload_state()["used_seconds"],
                                     original["used_seconds"])
                    self.assertEqual(restored.export_overload_state()["failure_reason"],
                                     original["failure_reason"])
                    self.assertEqual(self.epoch(restored)["failure_reason"],
                                     "invalid_persisted_trial")
                    self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                    self.assertEqual(bad, original)

    def test_new_frozen_thirty_second_policy_is_preserved_without_retroactive_activation(self):
        r, inp = self.ready(grid_confirmation_seconds=30)
        self.activate(r, inp)
        r.end_overload_trial(self.frame(3, 20, 16.1, et=2))
        saved = deepcopy(r.export_overload_state())
        original = deepcopy(saved)
        restored = self.regulator(grid_confirmation_seconds=0)
        self.assertTrue(restored.import_overload_state(saved))
        self.assertEqual(self.epoch(restored), original["trial_epoch"])
        self.assertEqual(self.epoch(restored)["policy"]["settings"]["grid_confirmation_seconds"], 30)
        self.assertEqual(restored.export_overload_state()["used_seconds"], original["used_seconds"])
        self.assertEqual(restored.export_overload_state()["failure_reason"], "response_timeout")
        self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertEqual(saved, original)


class GridConfirmationConfigurationTests(unittest.TestCase):
    def test_legacy_options_default_to_no_extra_grid_wait(self):
        old = {"overload_response_seconds": 10, "overload_enabled": True}
        before = deepcopy(old)
        options = const.validate_options(old)
        self.assertEqual(options["grid_confirmation_seconds"], 0)
        self.assertEqual(old, before)
        self.assertEqual(logic.RegulatorSettings().grid_confirmation_seconds, 0)

    def test_thirty_seconds_is_a_separate_integer_setting(self):
        for value in (0, 1, 15, 30):
            with self.subTest(value=value):
                normalized = const.validate_option_value("grid_confirmation_seconds", value)
                self.assertEqual(normalized, value)
                self.assertIs(type(normalized), int)
        options = const.validate_options({"grid_confirmation_seconds": 30})
        self.assertEqual(options["grid_confirmation_seconds"], 30)
        self.assertEqual(options["overload_response_seconds"], 10)
        self.assertEqual(options["overload_transition_limit_amps"], 38)
        self.assertEqual(options["overload_budget_seconds"], 1800)
        self.assertEqual(const.OPTION_NUMBER_RANGES["grid_confirmation_seconds"],
                         (0, 30, 1))

    def test_invalid_confirmation_lengths_are_rejected(self):
        for value in (-1, 31, 0.5, 29.9, True, False, float("nan"),
                      float("inf"), float("-inf"), None, ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    const.validate_option_value("grid_confirmation_seconds", value)

    def test_physical_response_window_remains_bounded_by_ten(self):
        for value in (11, 30):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    const.validate_options({"overload_response_seconds": value,
                                            "grid_confirmation_seconds": 30})
        self.assertEqual(const.OPTION_NUMBER_RANGES["overload_response_seconds"],
                         (1, 10, 1))


if __name__ == "__main__":
    unittest.main()
