"""Explicit software trial periods; synthetic clocks and no HA or hardware.

Counters exercise operational policy, never fuse cooling or physical delivery.
The pre-existing lifetime exposure and first historical failure stay intact.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import unittest

import test_1fuseloader_overload as fixtures

logic = fixtures.logic
EPOCH = 1_700_000_000.0
LIFETIME = 2400.0
SOURCE = ("synthetic-charger", "synthetic-grid", "L1")
TRIAL_A = "00000000-0000-4000-8000-000000000001"
TRIAL_B = "00000000-0000-4000-8000-000000000002"


class ExplicitTrialTests(unittest.TestCase):
    def regulator(self, *, historical_failure="response_timeout", **options):
        settings = dict(active_control=True, dry_run=False, overload_enabled=True,
                        enable_soft_guard=False, enable_pause_resume=False,
                        bounded_nominal_shedding=True, measurement_settle_seconds=25)
        settings.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**settings))
        self.assertTrue(r.import_overload_state(dict(
            version=1, used_seconds=LIFETIME, exposure_active=False,
            transition_pending=False, minimum_held=False,
            failure_reason=historical_failure)))
        return r

    def frame(self, now, grid=20, ev=16.1, *, gt=None, et=None,
              status="charging", source=SOURCE, **metadata):
        values = dict(now_ts=now, status=status, grid_l1=grid, charger_current=ev,
                      wall_now_ts=EPOCH+now, grid_report_ts=EPOCH+(now if gt is None else gt),
                      charger_report_ts=EPOCH+(now if et is None else et),
                      control_source_id=source)
        values.update(metadata)
        return logic.RegulatorInput(**values)

    def step(self, r, now, grid=20, ev=16.1, *, accept=True, **metadata):
        d = r.evaluate(self.frame(now, grid, ev, **metadata))
        if accept and d.should_call_service:
            r.acknowledge(d, now)
        return d

    def ready(self, **options):
        r = self.regulator(**options)
        self.assertEqual(self.step(r, 0).current_p1, 16)
        self.step(r, 1, et=0)
        inp = self.frame(2, et=1)
        self.assertEqual(r.evaluate(inp).current_p1, 16)
        return r, inp

    def prepare(self, r, inp, trial_id=TRIAL_A):
        stamp = datetime.fromtimestamp(inp.wall_now_ts, timezone.utc).isoformat()
        r.prepare_overload_trial(inp, trial_id=trial_id, started_at_utc=stamp)

    def activate(self, r, inp, trial_id=TRIAL_A):
        self.prepare(r, inp, trial_id)
        r.stage_overload_trial_activation(trial_id, inp)
        r.commit_overload_trial_activation(trial_id, inp)

    def epoch(self, r):
        return r.export_overload_state()["trial_epoch"]

    def assert_lifetime_preserved(self, r, minimum=LIFETIME):
        state = r.export_overload_state()
        self.assertGreaterEqual(state["used_seconds"], minimum)
        self.assertEqual(state["failure_reason"], "response_timeout")

    def active(self):
        r, inp = self.ready()
        self.activate(r, inp)
        return r

    def minimum(self):
        r = self.active()
        initial = self.step(r, 3, 38, 16.1)
        self.assertEqual((initial.mode, initial.current_p1), ("overload_shedding", 7))
        self.assertEqual(self.step(r, 5, 38, 7.1, gt=3).current_p1, 7)
        confirmed = self.step(r, 7, 29, 7.1, et=5)
        self.assertEqual((confirmed.mode, confirmed.current_p1), ("overload_minimum", 7))
        return r

    def test_prepare_and_staged_checkpoint_grant_no_overload_permission(self):
        r, inp = self.ready()
        self.prepare(r, inp)
        self.assertEqual(self.epoch(r)["state"], "pending")
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertEqual(r.overload_minimum_current, 6)
        r.stage_overload_trial_activation(TRIAL_A, inp)
        self.assertEqual(self.epoch(r)["state"], "active")
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertEqual(r.overload_minimum_current, 6)
        r.commit_overload_trial_activation(TRIAL_A, inp)
        self.assertTrue(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertEqual(r.overload_minimum_current, 7)
        self.assertEqual(r.settings.min_charge_amps, 6)
        self.assert_lifetime_preserved(r)

    def test_explicit_period_uses_own_budget_and_failure_without_erasing_history(self):
        r = self.active()
        self.assertEqual(r.effective_overload_used_seconds, 0)
        self.assertIsNone(r.effective_overload_failure_reason)
        self.assertEqual(self.epoch(r)["baseline_used_seconds"], LIFETIME)
        self.assertEqual(self.epoch(r)["source_id"], list(SOURCE))
        self.assert_lifetime_preserved(r)

    def test_duplicate_start_cannot_refill_or_replace_an_active_period(self):
        r = self.minimum()
        before = deepcopy(self.epoch(r))
        used = r.effective_overload_used_seconds
        for trial_id in (TRIAL_A, TRIAL_B):
            with self.subTest(trial_id=trial_id), self.assertRaises(ValueError):
                self.prepare(r, self.frame(8, 20, 7.1, et=7), trial_id)
        self.assertEqual(self.epoch(r), before)
        self.assertEqual(r.effective_overload_used_seconds, used)
        self.assert_lifetime_preserved(r)

    def test_missing_stage_and_wrong_activation_id_are_rejected(self):
        r, inp = self.ready()
        self.prepare(r, inp)
        with self.assertRaises(ValueError):
            r.commit_overload_trial_activation(TRIAL_A, inp)
        with self.assertRaises(ValueError):
            r.stage_overload_trial_activation(TRIAL_B, inp)
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assert_lifetime_preserved(r)

    def test_checkpoint_rejection_is_terminal_for_that_period(self):
        r, inp = self.ready()
        self.prepare(r, inp)
        r.reject_overload_trial_activation(TRIAL_A, "checkpoint_failed")
        self.assertIn(self.epoch(r)["state"], {"failed", "interrupted"})
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        with self.assertRaises(ValueError):
            r.stage_overload_trial_activation(TRIAL_A, inp)
        self.assertEqual(self.step(r, 3, 38, 16.1).current_p1, 0)
        self.assert_lifetime_preserved(r)

    def test_pending_or_staged_durability_cannot_grant_a_seven_amp_overload(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                r, inp = self.ready()
                self.prepare(r, inp)
                if staged:
                    r.stage_overload_trial_activation(TRIAL_A, inp)
                self.assertEqual(self.step(r, 3, 38, 16.1).current_p1, 0)
                self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
                self.assert_lifetime_preserved(r)

    def test_each_activation_checkpoint_revalidates_the_current_gate(self):
        for invalid_at in ("stage", "commit"):
            for changes in ({"grid_l1": 29}, {"data_valid": False},
                            {"control_source_id": ("other",)},
                            {"status": "awaiting_start", "charger_current": 0}):
                with self.subTest(invalid_at=invalid_at, changes=changes):
                    r, inp = self.ready()
                    self.prepare(r, inp)
                    if invalid_at == "commit":
                        r.stage_overload_trial_activation(TRIAL_A, inp)
                    bad = replace(self.frame(3, et=2), **changes)
                    method = (r.stage_overload_trial_activation if invalid_at == "stage"
                              else r.commit_overload_trial_activation)
                    with self.assertRaises(ValueError):
                        method(TRIAL_A, bad)
                    self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
                    self.assert_lifetime_preserved(r)

    def test_checkpoint_latency_uses_current_lease_without_moving_period_baseline(self):
        r, inp = self.ready()
        self.prepare(r, inp)
        baseline = self.epoch(r)["baseline_used_seconds"]
        r.stage_overload_trial_activation(TRIAL_A, self.frame(3, et=2))
        r.commit_overload_trial_activation(TRIAL_A, self.frame(4, et=3))
        self.assertEqual(self.epoch(r)["baseline_used_seconds"], baseline)
        self.assertEqual(r.effective_overload_used_seconds, 0)
        self.assertEqual(r.last_set_limit_ts, 0)
        self.assertTrue(r.trial_diagnostics()["overload_trial_activation_committed"])

        r, inp = self.ready()
        self.prepare(r, inp)
        r.stage_overload_trial_activation(TRIAL_A, self.frame(3, et=2))
        with self.assertRaises(ValueError):
            r.commit_overload_trial_activation(TRIAL_A, self.frame(60, et=59))
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assert_lifetime_preserved(r)

    def test_start_requires_real_existing_charging_fresh_binding_and_normal_raw_load(self):
        cases = ({"status": "awaiting_start", "charger_current": 0},
                 {"grid_l1": 23.0001}, {"source": ("other",)},
                 {"source": None}, {"data_valid": False},
                 {"charger_current_trusted": False}, {"grid_report_ts": None},
                 {"charger_report_ts": None}, {"grid_report_ts": EPOCH-200},
                 {"charger_report_ts": EPOCH+20})
        for changes in cases:
            with self.subTest(changes=changes):
                r, inp = self.ready()
                if "source" in changes:
                    changes = dict(changes)
                    changes["control_source_id"] = changes.pop("source")
                bad = replace(inp, **changes)
                with self.assertRaises(ValueError):
                    self.prepare(r, bad)
                self.assert_lifetime_preserved(r)

    def test_expired_lease_and_existing_normal_recovery_barrier_cannot_be_cleared_by_start(self):
        r, _ = self.ready()
        with self.assertRaises(ValueError):
            self.prepare(r, self.frame(60, et=59))
        r, _ = self.ready()
        self.step(r, 3, data_valid=False)
        before = deepcopy(r.export_overload_state()["normal_control"])
        with self.assertRaises(ValueError):
            self.prepare(r, self.frame(4, et=3))
        self.assertEqual(r.export_overload_state()["normal_control"], before)
        self.assert_lifetime_preserved(r)

    def test_invalid_and_future_explicit_metadata_cannot_create_a_period(self):
        cases = (("", None), (True, None), (TRIAL_A, "not-a-time"),
                 (TRIAL_A, "2099-01-01T00:00:00+00:00"))
        for trial_id, stamp in cases:
            with self.subTest(trial_id=trial_id, stamp=stamp):
                r, inp = self.ready()
                if stamp is None:
                    stamp = datetime.fromtimestamp(inp.wall_now_ts, timezone.utc).isoformat()
                with self.assertRaises(ValueError):
                    r.prepare_overload_trial(inp, trial_id=trial_id, started_at_utc=stamp)
                self.assert_lifetime_preserved(r)

    def test_trial_policy_rejects_nonfinite_settings_and_boolean_numeric_fields(self):
        for key, value in (("buffer_amps", float("nan")),
                           ("minimum_reserve_amps", float("inf")),
                           ("buffer_amps", True), ("normal_ttl_seconds", True),
                           ("measurement_settle_seconds", True),
                           ("max_sensor_age_seconds", True)):
            with self.subTest(key=key, value=value):
                r, inp = self.ready()
                with self.assertRaises(ValueError):
                    r.update_settings(replace(r.settings, **{key: value}))
                    self.prepare(r, inp)
                self.assert_lifetime_preserved(r)

    def test_legacy_without_explicit_period_retains_spent_latch_and_six_amp_policy(self):
        r, _ = self.ready()
        self.assertEqual(r.overload_minimum_current, 6)
        self.assertEqual(r.effective_overload_failure_reason, "response_timeout")
        self.assertEqual(r.effective_overload_used_seconds, LIFETIME)
        legacy = self.step(r, 3, 38, 16.1)
        self.assertEqual((legacy.mode, legacy.current_p1), ("normal_shedding", 7))
        self.assertEqual(self.step(r, 13, 38, 16.1).current_p1, 0)
        self.assert_lifetime_preserved(r)

    def test_minimum_response_requires_post_ack_event_and_later_grid(self):
        r = self.active()
        first = self.step(r, 3, 38, 16.1, accept=False)
        r.acknowledge(first, 3.5)
        old = self.step(r, 5, 29, 7.1, et=3.2)
        self.assertEqual(old.mode, "overload_shedding")
        ev_first = self.step(r, 6, 29, 7.1, gt=5)
        self.assertEqual(ev_first.mode, "overload_shedding")
        later_grid = self.step(r, 7, 29, 7.1, et=6)
        self.assertEqual((later_grid.mode, later_grid.current_p1), ("overload_minimum", 7))
        self.assert_lifetime_preserved(r)

    def test_api_acceptance_and_refresh_cannot_extend_original_ten_seconds(self):
        r = self.active()
        for now in (3, 6, 9, 12):
            d = self.step(r, now, 38, 16.1)
            self.assertEqual((d.current_p1, d.overload_transition_remaining_seconds), (7, 13-now))
        stopped = self.step(r, 13, 38, 16.1)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assert_lifetime_preserved(r)

    def test_delayed_high_grid_frame_consumes_the_same_peak_deadline(self):
        r = self.active()
        first = self.step(r, 10, 38, 16.1, gt=4, et=9)
        self.assertEqual((first.current_p1, first.overload_transition_remaining_seconds), (7, 4))
        self.assertEqual(self.step(r, 13, 38, 16.1, gt=4, et=12).current_p1, 7)
        stopped = self.step(r, 14, 38, 16.1, gt=4, et=13)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assert_lifetime_preserved(r)

    def test_unchanged_trusted_minimum_event_grants_no_fresh_response_time(self):
        r = self.minimum()
        for now in (30, 33, 60, 63, 90, 93, 130):
            d = self.step(r, now, 29, 7.1, et=5)
            self.assertEqual((d.mode, d.current_p1), ("overload_minimum", 7))
            self.assertIsNone(d.overload_transition_remaining_seconds)
            self.assertEqual(self.epoch(r)["state"], "active")
        self.assertEqual(r.effective_overload_used_seconds, 127)
        self.assert_lifetime_preserved(r, LIFETIME+127)

    def test_38_exact_is_bounded_but_epsilon_above_is_terminal(self):
        r = self.active()
        self.assertEqual(self.step(r, 3, 38, 16.1).current_p1, 7)
        d = self.step(r, 4, 38.001, 16.1)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assert_lifetime_preserved(r)

    def test_late_old_minimum_reply_cannot_reopen_a_failed_period(self):
        r = self.active()
        old = self.step(r, 3, 38, 16.1, accept=False)
        self.assertEqual(self.step(r, 4, 38.001, 16.1).current_p1, 0)
        epoch = deepcopy(self.epoch(r))
        r.acknowledge(old, 4.5)
        d = self.step(r, 5, 29, 7.1)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(self.epoch(r), epoch)
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assert_lifetime_preserved(r)

    def test_29_exact_after_response_is_allowed_but_epsilon_above_is_terminal(self):
        r = self.minimum()
        self.assertEqual(self.step(r, 8, 29, 7.1).current_p1, 7)
        d = self.step(r, 9, 29.001, 7.1)
        self.assertEqual(d.current_p1, 0)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assert_lifetime_preserved(r)

    def _assert_trial_floor_entry_and_response(self, grid, current):
        r = self.active()
        first = self.step(r, 3, grid, current)
        self.assertEqual((first.mode, first.current_p1), ("overload_shedding", 7))
        ev_first = self.step(r, 4, grid, 7.1, gt=3)
        self.assertEqual((ev_first.mode, ev_first.current_p1), ("overload_shedding", 7))
        response = self.step(r, 5, 24, 7.1, et=4)
        self.assertEqual((response.mode, response.current_p1), ("overload_minimum", 7))
        self.assertIsNone(response.overload_transition_remaining_seconds)
        self.assertEqual(r.effective_overload_used_seconds, 2)
        self.assert_lifetime_preserved(r, LIFETIME+2)

    def test_trial_entry_just_above_25_keeps_seven_amp_floor_after_response(self):
        self._assert_trial_floor_entry_and_response(25.1, 8.1)

    def test_trial_entry_at_27_keeps_seven_amp_floor_after_response(self):
        self._assert_trial_floor_entry_and_response(27, 10)

    def test_cumulative_time_does_not_refund_from_short_nominal_dips(self):
        r = self.minimum()
        used_before = r.effective_overload_used_seconds
        self.step(r, 17, 25, 7.1)
        self.assertAlmostEqual(r.effective_overload_used_seconds, used_before+10)
        dip = r.effective_overload_used_seconds
        self.step(r, 18, 29, 7.1)
        self.assertEqual(r.effective_overload_used_seconds, dip)
        self.step(r, 28, 25, 7.1)
        self.assertAlmostEqual(r.effective_overload_used_seconds, dip+10)
        self.assertEqual(self.epoch(r)["baseline_used_seconds"], LIFETIME)
        self.assert_lifetime_preserved(r, LIFETIME+dip+10)

    def test_exact_1800_seconds_is_exhausted_without_replacing_lifetime(self):
        r = self.minimum()
        # First overload sample was t3; renew the live lease independently of
        # the cumulative counter and keep the car at its observed lower draw.
        for now in range(30, 1800, 30):
            self.step(r, now, 29, 7.1)
        self.assertEqual(self.step(r, 1802, 29, 7.1).current_p1, 7)
        d = self.step(r, 1803, 29, 7.1)
        self.assertEqual(d.current_p1, 0)
        self.assertEqual(r.effective_overload_used_seconds, 1800)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assert_lifetime_preserved(r, LIFETIME+1800)

    def test_active_and_pending_imports_are_interrupted_never_rearmed(self):
        for stage in ("pending", "staged", "committed"):
            with self.subTest(stage=stage):
                r, inp = self.ready()
                self.prepare(r, inp)
                if stage != "pending":
                    r.stage_overload_trial_activation(TRIAL_A, inp)
                if stage == "committed":
                    r.commit_overload_trial_activation(TRIAL_A, inp)
                saved = deepcopy(r.export_overload_state())
                restored = self.regulator()
                self.assertTrue(restored.import_overload_state(saved))
                self.assertEqual(self.epoch(restored)["state"], "interrupted")
                self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                with self.assertRaises(ValueError):
                    restored.commit_overload_trial_activation(TRIAL_A, inp)
                self.assert_lifetime_preserved(restored)

    def test_unknown_downtime_is_charged_before_interrupted_trial_is_restored(self):
        r = self.minimum()
        saved = deepcopy(r.export_overload_state())
        old_used = saved["used_seconds"]
        restored = self.regulator()
        self.assertTrue(restored.import_overload_state(saved, elapsed_seconds=60))
        self.assertEqual(self.epoch(restored)["state"], "interrupted")
        self.assertEqual(restored.export_overload_state()["used_seconds"], old_used+60)
        self.assertEqual(self.epoch(restored)["baseline_used_seconds"], LIFETIME)
        self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
        self.assert_lifetime_preserved(restored, old_used+60)

    def test_same_instance_legacy_import_cannot_remove_or_refund_a_live_trial(self):
        r = self.minimum()
        before = r.export_overload_state()["used_seconds"]
        old = deepcopy(r.export_overload_state())
        del old["trial_epoch"]
        old.update(used_seconds=1.0, exposure_active=False,
                   transition_pending=False, minimum_held=False, failure_reason=None)
        self.assertFalse(r.import_overload_state(old))
        self.assertEqual(self.epoch(r)["state"], "interrupted")
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assert_lifetime_preserved(r, before)

    def test_source_fault_disconnect_and_settings_change_interrupt_without_time_reset(self):
        for mutation in ("source_fault", "disconnect", "settings", "reset", "budget_reset"):
            with self.subTest(mutation=mutation):
                r = self.minimum()
                before = r.export_overload_state()["used_seconds"]
                if mutation == "settings":
                    r.update_settings(replace(r.settings, buffer_amps=2.5))
                elif mutation == "reset":
                    r.reset()
                elif mutation == "budget_reset":
                    r.reset_overload_budget()
                else:
                    kw = {"data_valid": False} if mutation == "source_fault" else {"status": "disconnected"}
                    self.step(r, 8, 29, 7.1, **kw)
                self.assertEqual(self.epoch(r)["state"], "interrupted")
                self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
                self.assert_lifetime_preserved(r, before)

    def test_active_trial_cannot_survive_binding_trust_or_source_clock_failure(self):
        cases = ({"source": ("other",)}, {"charger_current_trusted": False},
                 {"grid_report_ts": None}, {"charger_report_ts": None},
                 {"grid_report_ts": EPOCH+2}, {"charger_report_ts": EPOCH+30})
        for changes in cases:
            with self.subTest(changes=changes):
                r = self.minimum()
                used = r.export_overload_state()["used_seconds"]
                d = self.step(r, 8, 29, 7.1, **changes)
                self.assertEqual(d.current_p1, 0)
                self.assertEqual(self.epoch(r)["state"], "interrupted")
                self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
                self.assert_lifetime_preserved(r, used+1)

    def test_manual_pause_does_not_receive_an_automatic_overload_restart(self):
        r = self.minimum()
        d = self.step(r, 8, 29, 7.1, status="paused")
        self.assertEqual(d.current_p1, 0)
        self.assertIn(self.epoch(r)["state"], {"failed", "interrupted"})
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertNotEqual(d.command, "resume")
        self.assert_lifetime_preserved(r)

    def test_malformed_trial_metadata_fails_closed_without_refunding_history(self):
        r = self.active()
        saved = deepcopy(r.export_overload_state())
        for key, value in (("baseline_used_seconds", LIFETIME+1),
                           ("baseline_used_seconds", float("nan")),
                           ("baseline_used_seconds", True), ("state", "running"),
                           ("started_at_utc", "2099-01-01T00:00:00+00:00"),
                           ("trial_id", ""), ("source_id", [])):
            with self.subTest(key=key, value=value):
                bad = deepcopy(saved)
                bad["trial_epoch"][key] = value
                restored = self.regulator()
                self.assertFalse(restored.import_overload_state(bad))
                self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                self.assert_lifetime_preserved(restored)

    def test_persisted_policy_cannot_change_the_seven_amp_contract(self):
        r = self.active()
        saved = deepcopy(r.export_overload_state())
        for key, value in (("minimum_current", 6), ("ceiling_amps", 30),
                           ("budget_seconds", 1801), ("transition_limit_amps", 39),
                           ("response_seconds", 11)):
            with self.subTest(key=key, value=value):
                bad = deepcopy(saved)
                bad["trial_epoch"]["policy"][key] = value
                restored = self.regulator()
                self.assertFalse(restored.import_overload_state(bad))
                self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                self.assert_lifetime_preserved(restored)

    def test_end_does_not_start_another_period_and_next_baseline_uses_current_lifetime(self):
        r = self.minimum()
        end = self.frame(17, 23, 7.1, et=16)
        r.end_overload_trial(end)
        lifetime = r.export_overload_state()["used_seconds"]
        self.assertEqual(self.epoch(r)["state"], "ended")
        self.assertFalse(r.trial_diagnostics()["overload_trial_activation_committed"])
        self.assertGreater(lifetime, LIFETIME)
        self.step(r, 18, 23, 7.1, et=17)
        with self.assertRaises(ValueError):
            self.prepare(r, self.frame(19, 23, 7.1, et=18), TRIAL_A)
        self.prepare(r, self.frame(19, 23, 7.1, et=18), TRIAL_B)
        self.assertEqual(self.epoch(r)["baseline_used_seconds"], lifetime)
        self.assertEqual(self.epoch(r)["trial_id"], TRIAL_B)
        self.assertEqual(self.epoch(r)["state"], "pending")
        self.assert_lifetime_preserved(r, lifetime)


if __name__ == "__main__":
    unittest.main()
