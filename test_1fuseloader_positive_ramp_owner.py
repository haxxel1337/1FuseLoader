"""Source/receiver-clock edges of a genuine accepted startup owner, offline."""
from copy import deepcopy
import unittest

import test_1fuseloader_trial_epochs as fixtures

logic = fixtures.logic
EPOCH, SOURCE = fixtures.EPOCH, fixtures.SOURCE


class PositiveRampOwnerTests(unittest.TestCase):
    def frame(self, now, grid=22.6, current=1.8, *, receipt=67., grid_at=None,
              sequence=1, status="charging", **metadata):
        values = dict(now_ts=now, status=status, grid_l1=grid, charger_current=current,
            wall_now_ts=EPOCH+now, grid_report_ts=EPOCH+(now if grid_at is None else grid_at),
            charger_report_ts=None if receipt is None else EPOCH+receipt,
            control_source_id=SOURCE, charger_native_committed=True,
            charger_report_family="native_terminal_receipt",
            charger_report_provenance=("receiver", "max_terminals", 182, 183),
            charger_observation_report_times=() if receipt is None else
                ((182, EPOCH+receipt), (183, EPOCH+receipt)),
            charger_observation_sequences=((182, sequence), (183, sequence), (184, 0), (185, 0)))
        values.update(metadata)
        return logic.RegulatorInput(**values)

    def bootstrap(self, **options):
        values = dict(active_control=True, dry_run=False, overload_enabled=True,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=True,
            enable_soft_guard=False, enable_pause_resume=False, measurement_settle_seconds=25,
            grid_confirmation_seconds=30)
        values.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**values))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        for now in (0., 60.):
            d = r.evaluate(self.frame(now, 8.4, .012, receipt=None, sequence=0,
                status="awaiting_start"))
            if d.should_call_service:
                r.acknowledge(d, now)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual((r._positive_cap, r._positive_house), (14, 8.4))
        return r

    def test_first_receiver_receipt_owns_sample_aged_deadline_not_later_evaluation(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid_at=69.))
        self.assertEqual((d.mode, d.current_p1, d.command), ("positive_ramp_hold", 14, None))
        self.assertEqual(d.positive_response_deadline_ts, 92.)
        self.assertEqual(d.measurement_alignment_remaining_seconds, 22.)
        self.assertEqual(r.last_set_limit_ts, 60.)

    def test_provider_clock_does_not_replace_the_native_receiver_order(self):
        for provider in (59., 60.):
            r = self.bootstrap()
            d = r.evaluate(self.frame(70., grid_at=69., charger_event_source_known=True,
                charger_event_source_ts=EPOCH+provider))
            # A genuine native record owns receiver ordering even when the
            # associated provider clock predates the HTTP receipt.
            self.assertEqual(d.mode, "positive_ramp_hold")
            self.assertEqual(d.positive_response_deadline_ts, 92.)
            self.assertEqual(r._positive_response_ts, EPOCH+67.)
            self.assertEqual(r._ev_event_source_ts, EPOCH+provider)
        r = self.bootstrap()
        older = r.evaluate(self.frame(70., grid_at=69., receipt=59.,
            charger_event_source_known=True, charger_event_source_ts=EPOCH+59.))
        self.assertEqual(older.positive_response_deadline_ts, 94.)
        self.assertIsNone(r._positive_response_ts)
        r = self.bootstrap()
        future = r.evaluate(self.frame(70., grid_at=69., charger_event_source_known=True,
            charger_event_source_ts=EPOCH+76.))
        self.assertNotEqual(future.mode, "positive_ramp_hold")
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid_at=69., charger_event_source_known=True,
            charger_event_source_ts=EPOCH+61.))
        self.assertEqual(d.mode, "positive_ramp_hold")
        self.assertEqual(d.positive_response_deadline_ts, 92.)

    def test_unadvanced_receiver_sequence_cannot_manufacture_first_rise(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid_at=69., sequence=0))
        self.assertEqual(d.mode, "positive_ramp_hold")
        self.assertEqual(d.positive_response_deadline_ts, 94.)
        self.assertIsNone(r._positive_response_ts)
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid=8.4, grid_at=69., sequence=0))
        self.assertNotEqual(d.mode, "positive_ramp_hold")
        self.assertIsNone(r._positive_ramp_deadline)

    def test_get_snapshot_neither_starts_nor_completes_native_ramp(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid_at=69., charger_snapshot_verified=True))
        self.assertNotEqual(d.mode, "positive_ramp_hold")
        r = self.bootstrap()
        self.assertEqual(r.evaluate(self.frame(70., grid_at=69.)).mode, "positive_ramp_hold")
        d = r.evaluate(self.frame(75., current=14.1, receipt=73., sequence=2,
            grid_at=74., charger_snapshot_verified=True))
        self.assertEqual(d.mode, "positive_ramp_hold")
        self.assertEqual(d.positive_response_deadline_ts, 92.)
        self.assertEqual(r._positive_cap, 14)

    def test_original_lease_caps_observation_deadline_and_is_never_renewed(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(119., receipt=118., grid_at=118.5))
        self.assertEqual(d.mode, "positive_ramp_hold")
        self.assertEqual(d.positive_response_deadline_ts, 120.)
        self.assertEqual(d.measurement_alignment_remaining_seconds, 1.)
        expired = r.evaluate(self.frame(120., receipt=119., grid_at=120., sequence=2))
        self.assertNotEqual(expired.mode, "positive_ramp_hold")
        self.assertEqual(r.last_set_limit_ts, 60.)

    def test_custom_soft_and_pause_guards_override_without_renewing_owner(self):
        for options, mode in ((dict(enable_soft_guard=True, soft_limit_amps=20.,
                soft_for_seconds=1, soft_current_amps=6), "soft_limit"),
                (dict(enable_pause_resume=True, pause_limit_amps=20., pause_for_seconds=1), "pause")):
            r = self.bootstrap(**options)
            r.evaluate(self.frame(70., grid_at=69.))
            d = r.evaluate(self.frame(72., grid_at=71.))
            self.assertEqual(d.mode, mode)
            self.assertEqual(r.last_set_limit_ts, 60.)

    def test_no_admission_is_not_an_automatic_failure_or_budget_reset(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid=23.1, current=1.8, grid_at=69.))
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.assertIsNone(r.automatic_cycle_diagnostics()["automatic_cycle_failure_reason"])
        self.assertEqual(r.effective_overload_used_seconds, 0.)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_exact_zero_use_spurious_marker_repairs_only_cycle_on_healthy_normal_frame(self):
        r = self.bootstrap()
        state = r.export_overload_state()
        state["automatic_cycle"].update(state="blocked", failure_reason="automatic_cycle_not_admitted")
        self.assertTrue(r.import_overload_state(state))
        before = deepcopy(r.export_overload_state())
        inp = self.frame(70., grid=8., current=.012, receipt=67., grid_at=69., status="awaiting_start")
        r.observe_automatic_cycle(inp)
        after = r.export_overload_state()
        self.assertEqual(after["automatic_cycle"]["state"], "ready")
        self.assertIsNone(after["automatic_cycle"]["failure_reason"])
        for key in ("used_seconds", "failure_reason", "normal_control"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["automatic_cycle"]["cycle_id"], before["automatic_cycle"]["cycle_id"])
        self.assertEqual(after["automatic_cycle"]["used_seconds"], 0.)
        self.assertFalse(r.automatic_cycle_requires_checkpoint)

    def test_spurious_marker_repair_rejects_used_budget_real_fault_and_invalid_source(self):
        for change in ({"used_seconds": .001}, {"failure_reason": "response_timeout"}):
            r = self.bootstrap()
            state = r.export_overload_state()
            state["automatic_cycle"].update(state="blocked", failure_reason="automatic_cycle_not_admitted")
            state["automatic_cycle"].update(change)
            self.assertTrue(r.import_overload_state(state))
            r.observe_automatic_cycle(self.frame(70., grid=8., grid_at=69.))
            self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "blocked")
        r = self.bootstrap()
        state = r.export_overload_state()
        state["automatic_cycle"].update(state="blocked", failure_reason="automatic_cycle_not_admitted")
        self.assertTrue(r.import_overload_state(state))
        r.observe_automatic_cycle(self.frame(70., grid=8., grid_at=69., data_valid=False))
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "blocked")

    def test_genuine_peak_first_requests_minimum_despite_lagging_ev_house_projection(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(70., grid=31., grid_at=69.))
        self.assertEqual((d.mode, d.current_p1), ("overload_shedding", 7))
        self.assertGreater(d.house_load_l1+7, 29.)
        self.assertEqual(d.nominal_response_deadline_ts, 79.)
        r.acknowledge(d, 70.)
        response = r.evaluate(self.frame(72., grid=31., current=7.1, receipt=71., grid_at=69., sequence=2))
        self.assertEqual((response.mode, response.current_p1, response.command), ("overload_grid_wait", 7, None))
        self.assertEqual(response.grid_confirmation_deadline_ts, 101.)
        settled = r.evaluate(self.frame(74., grid=29., current=7.1, receipt=71., grid_at=73., sequence=2))
        self.assertEqual((settled.mode, settled.current_p1), ("overload_minimum", 7))

    def test_true_high_house_after_minimum_or_unanswered_peak_remains_protective(self):
        r = self.bootstrap()
        proposed = r.evaluate(self.frame(70., grid=31., grid_at=69.))
        r.acknowledge(proposed, 70.)
        too_high = r.evaluate(self.frame(74., grid=31., current=7.1, receipt=71., grid_at=73., sequence=2))
        self.assertEqual(too_high.current_p1, 0)
        self.assertIn("ceiling", too_high.reason)
        r = self.bootstrap()
        proposed = r.evaluate(self.frame(70., grid=31., grid_at=69.))
        r.acknowledge(proposed, 70.)
        unanswered = r.evaluate(self.frame(79., grid=31., current=1.8, receipt=67., grid_at=79.))
        self.assertEqual(unanswered.current_p1, 0)
        self.assertIn("response_timeout", unanswered.reason)

    def test_peak_projection_bypass_never_extends_expired_authority_or_bad_source(self):
        for now, metadata in ((121., {}), (70., {"data_valid": False}),
                (70., {"control_source_id": ("other-source",)}),
                (70., {"charger_current_trusted": False}), (70., {"grid_l1": 38.1})):
            r = self.bootstrap()
            d = r.evaluate(self.frame(now, grid=31., grid_at=now-1., receipt=now-3., **metadata))
            self.assertEqual(d.current_p1, 0)

    def test_durability_protective_zero_retires_unsent_peak_before_late_checkpoint(self):
        r = self.bootstrap()
        proposal = r.evaluate(self.frame(70., grid=31., grid_at=69.))
        self.assertEqual(proposal.current_p1, 7)
        history = (r.export_overload_state()["used_seconds"], r.export_overload_state()["failure_reason"])
        r.interrupt_positive_control("checkpoint_unverified_before_positive")
        self.assertIsNone(r._overload_transition_started)
        self.assertIsNone(r._positive_cap)
        self.assertTrue(r._restart_required)
        self.assertEqual((r.export_overload_state()["used_seconds"], r.export_overload_state()["failure_reason"]), history)
        after = r.evaluate(self.frame(71., grid=31., grid_at=71., receipt=67.))
        self.assertEqual(after.current_p1, 0)

    def test_pending_checkpoint_retention_reuses_guard_without_mutating_actual_owner(self):
        r = self.bootstrap()
        decision = r.evaluate(self.frame(70., grid_at=69.))
        before = deepcopy(r.__dict__)
        self.assertTrue(r.can_retain_positive_ramp(self.frame(71., grid=13., grid_at=70.), decision))
        self.assertEqual(r.__dict__, before)
        for metadata in ({"data_valid": False}, {"grid_l1": 31.},
                {"control_source_id": ("other",)}, {"status": "paused"}):
            self.assertFalse(r.can_retain_positive_ramp(self.frame(71., grid_at=70., **metadata), decision))
        self.assertFalse(r.can_retain_positive_ramp(self.frame(93., grid_at=92.), decision))
        self.assertEqual(r.__dict__, before)

    def test_marker_repair_never_clears_existing_owners_or_recovery_faults(self):
        blockers = (dict(_overload_transition_started=60.), dict(_overload_hold_active=True),
            dict(_normal_shed_deadline=80.), dict(_normal_shed_failure="source_invalid"),
            dict(_low_grid_pending=True), dict(_low_grid_failure="source_invalid"),
            dict(_uncertainty_ceiling=7), dict(_uncertainty_stops=1),
            dict(_uncertainty_retry_submission_pending=True), dict(_overload_exposure_active=True),
            dict(_peak_response=logic.PeakResponseOwner(SOURCE, 60., 70., EPOCH+60., 14)))
        for blocker in blockers:
            with self.subTest(blocker=blocker):
                r = self.bootstrap()
                state = r.export_overload_state()
                state["automatic_cycle"].update(state="blocked", failure_reason="automatic_cycle_not_admitted")
                self.assertTrue(r.import_overload_state(state))
                # Inject only negative competing work; no positive authority
                # or source-response proof is manufactured for this check.
                for key, value in blocker.items():
                    setattr(r, key, value)
                r.observe_automatic_cycle(self.frame(70., grid=8., grid_at=69.))
                self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "blocked")

    def test_ordinary_existing_upper_bound_can_be_retained_without_a_renewal(self):
        r = self.bootstrap(max_charge_amps=14)
        # Partial demand is sufficient: later grid independently bounds the
        # entire offered14, without requiring the car to draw14.
        r.evaluate(self.frame(70., grid=13., current=8., grid_at=69.))
        self.assertIsNone(r._positive_cap)
        inp = self.frame(90., grid=13., current=8., receipt=88., grid_at=89., sequence=2)
        renewal = r.evaluate(inp)
        self.assertEqual((renewal.mode, renewal.current_p1, renewal.command), ("normal", 14, "set_limit"))
        self.assertTrue(renewal.should_call_service)
        before = deepcopy(r.__dict__)
        self.assertTrue(r.can_retain_normal_offer(inp, renewal))
        self.assertEqual(r.__dict__, before)
        aged = self.frame(115., grid=13., current=8., receipt=88., grid_at=114., sequence=2)
        self.assertTrue(r.can_retain_normal_offer(aged, renewal))
        self.assertEqual(r.__dict__, before)
        for extra in ({"charger_native_committed": False}, {"charger_event_source_known": True,
                "charger_event_source_ts": EPOCH+121.}, {"receipt": 87., "sequence": 1},
                {"charger_native_committed": False, "charger_current": 8.1}):
            fields = dict(now=115., grid=13., current=8., receipt=88., grid_at=114., sequence=2)
            fields.update(extra)
            self.assertFalse(r.can_retain_normal_offer(self.frame(**fields), renewal))
        for change in ({"data_valid": False}, {"grid_l1": 31.}, {"status": "paused"},
                {"control_source_id": ("other",)}):
            self.assertFalse(r.can_retain_normal_offer(self.frame(91., grid=13., current=8.,
                receipt=88., grid_at=90., sequence=2, **change), renewal))
        self.assertFalse(r.can_retain_normal_offer(self.frame(121., grid=13., current=8.,
            receipt=120., grid_at=121., sequence=3), renewal))
        self.assertEqual(r.__dict__, before)


if __name__ == "__main__":
    unittest.main()
