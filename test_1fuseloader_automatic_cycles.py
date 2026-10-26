"""Automatic operational cycles using synthetic clocks, never thermal ratings."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_trial_epochs as fixtures

logic = fixtures.logic


class AutomaticCycleTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ExplicitTrialTests()

    def ready(self, **options):
        settings = dict(peak_only_response=True, automatic_overload_cycles=True,
            nominal_import_only_fallback=True, grid_confirmation_seconds=30)
        settings.update(options)
        return self.helper.ready(**settings)[0]

    def observe(self, r, now, grid=27., current=16.1, **metadata):
        # The observation hook owns no commands; evaluate invokes accounting
        # immediately before it. Full controller races have a separate suite.
        inp = self.helper.frame(now, grid, current, charger_native_committed=True, **metadata)
        valid = inp.data_valid and not inp.data_error
        self.assertTrue(r._observe_overload_time(now, grid, valid))
        r.observe_automatic_cycle(inp)
        return r.automatic_cycle_diagnostics()

    def high_run(self, r, start=10., end=40.):
        result = None
        for at in range(int(start), int(end)+1, 5):
            result = self.observe(r, float(at))
        return result

    def cool(self, r, start, end, grid=22.9):
        result = None
        for at in range(int(start), int(end)+1, 5):
            result = self.observe(r, float(at), grid, 0., status="disconnected")
        return result

    def test_first_thirty_seconds_are_same_budget_and_history_is_not_reset(self):
        r = self.ready()
        initial = r.export_overload_state()
        first = self.observe(r, 10.)
        self.assertEqual(first["automatic_cycle_state"], "qualifying")
        self.assertTrue(r.automatic_cycle_requires_checkpoint)
        self.assertIn("automatic_cycle", r.export_overload_state())
        self.assertIsNone(r.effective_overload_failure_reason)
        result = self.high_run(r, 15, 40)
        self.assertEqual(result["automatic_cycle_state"], "active")
        self.assertEqual(result["automatic_cycle_used_seconds"], 30.)
        self.assertEqual(result["automatic_cycle_remaining_seconds"], 1770.)
        after = r.export_overload_state()
        self.assertEqual(after["used_seconds"], initial["used_seconds"]+30.)
        self.assertEqual(after["failure_reason"], initial["failure_reason"])

    def test_drop_at_twenty_five_restarts_qualification_but_keeps_used(self):
        r = self.ready()
        self.high_run(r, 10, 25)
        self.observe(r, 30., 25.)
        self.assertEqual(r.effective_overload_used_seconds, 20.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.high_run(r, 35, 55)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "qualifying")
        self.assertEqual(r.effective_overload_used_seconds, 40.)

    def test_cached_packets_cannot_finish_qualification_or_cooling(self):
        r = self.ready()
        self.observe(r, 10.)
        for now in (15., 20., 25.):
            self.observe(r, now, gt=10.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)
        self.observe(r, 40., gt=10.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.cool(r, 45, 60)
        for now in (65., 70., 75.):
            self.observe(r, now, 22., 0., gt=60., status="disconnected")
        self.assertLess(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 1800.)

    def test_short_source_fault_breaks_qualification_and_cooling(self):
        r = self.ready()
        self.high_run(r, 10, 25)
        self.observe(r, 30., data_valid=False, data_error="synthetic_fault")
        self.observe(r, 35.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)
        self.cool(r, 40, 60)
        self.observe(r, 65., 22., 0., status="disconnected", data_valid=False)
        self.observe(r, 70., 22., 0., status="disconnected")
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 0.)

    def test_grid_gap_and_rebind_cannot_count_as_continuity(self):
        r = self.ready()
        self.high_run(r, 10, 25)
        self.observe(r, 45.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)
        other = ("other-synthetic", "synthetic-grid", "L1")
        self.observe(r, 50., source=other)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)
        self.assertFalse(r.automatic_cycle_requires_checkpoint)

    def test_strict_twenty_three_boundary_and_full_thirty_minutes_rearms_only_own_budget(self):
        r = self.ready()
        self.high_run(r)
        self.cool(r, 45, 940)
        self.observe(r, 945., 23., 0., status="disconnected")
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 0.)
        before = r.export_overload_state()
        self.cool(r, 950, 2745)
        self.assertGreater(r.effective_overload_used_seconds, 0.)
        self.observe(r, 2750., 22.9, 0., status="disconnected")
        after = r.export_overload_state()
        self.assertEqual(r.effective_overload_used_seconds, 0.)
        self.assertEqual(after["automatic_cycle"]["state"], "ready")
        self.assertGreater(after["automatic_cycle"]["cycle_id"], before["automatic_cycle"]["cycle_id"])
        self.assertEqual(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], before["failure_reason"])

    def test_exhaustion_withdraws_overload_and_cooling_rearms_without_manual_reset(self):
        r = self.ready()
        self.high_run(r)
        # Same-source observations continue; only accounting is advanced by
        # direct monotonic ticks, no artificially changed persisted proof.
        for at in range(45, 1820, 5):
            self.observe(r, float(at), 27., 0., status="disconnected")
        self.assertGreaterEqual(r.effective_overload_used_seconds, 1800.)
        self.assertEqual(r.effective_overload_failure_reason, "cumulative_budget_exhausted")
        self.cool(r, 1820, 3620)
        self.assertEqual(r.effective_overload_used_seconds, 0.)
        self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_not_admitted")

    def test_restore_keeps_usage_and_charges_offline_exposure_but_not_cooling(self):
        r = self.ready()
        self.high_run(r)
        state = r.export_overload_state()
        restored = self.helper.regulator(peak_only_response=True, automatic_overload_cycles=True)
        self.assertTrue(restored.import_overload_state(state, elapsed_seconds=120.))
        self.assertEqual(restored.effective_overload_used_seconds, 150.)
        self.assertEqual(restored.export_overload_state()["used_seconds"], state["used_seconds"]+120.)
        self.assertEqual(restored.effective_overload_failure_reason, "interrupted_automatic_cycle")
        self.assertEqual(restored.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 0.)
        self.assertEqual(restored.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)

    def test_reset_and_settings_toggle_never_refund_or_keep_continuity(self):
        r = self.ready()
        self.high_run(r, 10, 25)
        used = r.effective_overload_used_seconds
        r.reset()
        self.assertEqual(r.effective_overload_used_seconds, used)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 0.)
        r.update_settings(replace(r.settings, overload_enabled=False))
        self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_disabled")
        self.assertFalse(r.automatic_cycle_requires_checkpoint)
        self.assertEqual(r.effective_overload_used_seconds, used)
        r.update_settings(replace(r.settings, overload_enabled=True))
        self.assertEqual(r.effective_overload_used_seconds, used)

    def test_idle_unknown_native_or_no_live_authority_cannot_admit_period(self):
        for metadata in (dict(status="disconnected"), dict(charger_native_committed=False),
                         dict(charger_publication_pending=True), dict(charger_current_trusted=False)):
            r = self.ready()
            inp = self.helper.frame(10., 27., 16.1, charger_native_committed=True)
            inp = replace(inp, **metadata)
            r.observe_automatic_cycle(inp)
            self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_not_admitted")
        r = self.ready()
        self.observe(r, 65.)  # Original accepted lease has expired.
        self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_not_admitted")

    def test_manual_activation_and_reset_cannot_refill_automatic_budget(self):
        r = self.ready()
        self.high_run(r, 10, 25)
        before = deepcopy(r.export_overload_state())
        r.reset_overload_budget()
        self.assertEqual(r.export_overload_state(), before)
        with self.assertRaises(ValueError):
            self.helper.prepare(r, self.helper.frame(26.))

    def test_invalid_optional_cycle_fails_closed_without_erasing_history(self):
        r = self.ready()
        state = r.export_overload_state()
        history = (state["used_seconds"], state["failure_reason"])
        state["automatic_cycle"]["used_seconds"] = True
        self.assertFalse(r.import_overload_state(state))
        self.assertEqual((r.export_overload_state()["used_seconds"], r.export_overload_state()["failure_reason"]), history)
        self.assertEqual(r.effective_overload_failure_reason, "invalid_persisted_automatic_cycle")

    def test_default_off_and_malformed_option_validation(self):
        r, _ = self.helper.ready()
        self.assertNotIn("automatic_cycle", r.export_overload_state())
        self.assertEqual(r.effective_overload_failure_reason, "response_timeout")
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                logic.OneFuseRegulator(logic.RegulatorSettings(automatic_overload_cycles=value))
        with self.assertRaises(ValueError):
            logic.OneFuseRegulator(logic.RegulatorSettings(automatic_overload_cycles=True))

    def test_real_evaluate_qualification_does_not_force_minimum_before_a_peak(self):
        r = self.ready()
        ordinary = self.helper.step(r, 3., 26.6, 16.1, charger_native_committed=True)
        self.assertEqual((ordinary.mode, ordinary.current_p1), ("normal", 12))
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "qualifying")
        self.assertIsNone(ordinary.nominal_response_deadline_ts)
        peak = self.helper.step(r, 4., 28.5, 16.1, charger_native_committed=True)
        self.assertEqual((peak.mode, peak.current_p1), ("overload_shedding", 7))
        self.assertEqual(peak.nominal_response_deadline_ts, 14.)
        response = self.helper.step(r, 5., 28.5, 7.1, charger_native_committed=True)
        self.assertEqual((response.mode, response.command), ("overload_grid_wait", None))
        self.assertEqual(response.grid_confirmation_deadline_ts, 35.)
        settled = self.helper.step(r, 6., 27., 7.1, charger_native_committed=True)
        self.assertEqual((settled.mode, settled.current_p1), ("overload_minimum", 7))
        self.assertEqual(r.effective_overload_used_seconds, 3.)

    def test_blocked_automatic_permission_preserves_independent_normal_reduction(self):
        for grid, expected in ((20., 16), (26.6, 12)):
            r = self.ready()
            state = r.export_overload_state()
            state["automatic_cycle"].update(state="blocked", used_seconds=55.,
                failure_reason="interrupted_automatic_cycle")
            self.assertTrue(r.import_overload_state(state))
            history = (state["used_seconds"], state["failure_reason"])
            normal = self.helper.step(r, 3., grid, 16.1, charger_native_committed=True)
            self.assertEqual((normal.mode, normal.current_p1), ("normal", expected))
            self.assertEqual(r.effective_overload_failure_reason, "interrupted_automatic_cycle")
            self.assertEqual((r.export_overload_state()["used_seconds"],
                r.export_overload_state()["failure_reason"]), history)

    def test_aged_first_samples_cannot_shorten_local_qualification_or_cooling(self):
        r = self.ready()
        self.observe(r, 20., gt=10.)
        self.high_run(r, 25, 40)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "qualifying")
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 20.)
        self.high_run(r, 45, 50)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "active")
        self.assertEqual(r.effective_overload_used_seconds, 30.)
        self.observe(r, 65., 22., 0., gt=55., status="disconnected")
        self.cool(r, 70, 1855)
        self.assertGreater(r.effective_overload_used_seconds, 0.)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 1790.)
        self.cool(r, 1860, 1865)
        self.assertEqual(r.effective_overload_used_seconds, 0.)

    def test_idle_cold_restart_can_cool_same_persisted_source_without_charging_authority(self):
        r = self.ready()
        self.high_run(r)
        state = r.export_overload_state()
        restored = self.helper.regulator(peak_only_response=True, automatic_overload_cycles=True)
        self.assertTrue(restored.import_overload_state(state))
        self.assertIsNone(restored.last_target)
        self.assertIsNone(restored._accepted_control_source_id)
        self.cool(restored, 100, 1900)
        self.assertEqual(restored.effective_overload_used_seconds, 0.)
        self.assertEqual(restored.effective_overload_failure_reason, "automatic_cycle_not_admitted")
        self.assertIsNone(restored.last_target)
        self.assertFalse(restored.automatic_cycle_requires_checkpoint)


if __name__ == "__main__":
    unittest.main()
