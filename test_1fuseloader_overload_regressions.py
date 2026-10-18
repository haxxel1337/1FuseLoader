"""Synthetic asynchronous-source regressions for the optional overload policy.

These inputs describe software policy and HA report receipts. They do not model
fuse temperature, guarantee a vehicle response, or contact external services.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
SPEC = importlib.util.spec_from_file_location("onefuse_overload_regression_logic", PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)

EPOCH = 1_700_000_000.0


class OverloadRegressionTests(unittest.TestCase):
    def regulator(self, **options):
        settings = {
            "active_control": True,
            "dry_run": False,
            "main_fuse_amps": 25,
            "buffer_amps": 2,
            "minimum_reserve_amps": 0.5,
            "min_charge_amps": 6,
            "startup_charge_amps": 7,
            "max_charge_amps": 16,
            "measurement_settle_seconds": 25,
            "overload_enabled": True,
            "overload_margin_amps": 4,
            "overload_budget_seconds": 1800,
            "overload_transition_limit_amps": 38,
            "overload_response_seconds": 10,
        }
        settings.update(options)
        return logic.OneFuseRegulator(logic.RegulatorSettings(**settings))

    def sample(self, regulator, now, grid, ev, *, grid_at=None, ev_at=None, **metadata):
        reports = {
            "wall_now_ts": EPOCH + now,
            "grid_report_ts": EPOCH + (now if grid_at is None else grid_at),
            "charger_report_ts": EPOCH + (now if ev_at is None else ev_at),
        }
        reports.update(metadata)
        decision = regulator.evaluate(logic.RegulatorInput(
            now_ts=now, status="charging", grid_l1=grid,
            charger_current=ev, **reports))
        self.assertNotIn(decision.current_p1, range(1, 6))
        regulator.acknowledge(decision, now)
        return decision

    def assert_minimum(self, decision, mode=None):
        self.assertEqual((decision.command, decision.current_p1), ("set_limit", 6))
        self.assertFalse(decision.overload_failure_latched)
        if mode is not None:
            self.assertEqual(decision.mode, mode)

    def assert_stopped(self, decision, reason):
        self.assertEqual((decision.mode, decision.command, decision.current_p1),
                         ("overload_stop", "set_limit", 0))
        self.assertTrue(decision.overload_failure_latched)
        self.assertIn(reason, decision.reason)

    def test_partial_grid_above_ceiling_waits_for_post_minimum_grid(self):
        regulator = self.regulator()
        samples = [
            self.sample(regulator, 0, 38, 16),
            self.sample(regulator, 3, 38, 10, grid_at=0),
            self.sample(regulator, 5, 32, 10, ev_at=3),
            self.sample(regulator, 7, 32, 6, grid_at=5),
            self.sample(regulator, 8, 28, 6, ev_at=7),
        ]
        for decision in samples:
            self.assert_minimum(decision)
        self.assertEqual(samples[-2].mode, "overload_shedding")
        self.assertEqual(samples[-2].overload_transition_remaining_seconds, 3)
        self.assertEqual(samples[-1].mode, "overload_minimum")
        self.assertEqual(samples[-1].overload_used_seconds, 8)

    def test_grid_first_partial_reduction_reaches_minimum_without_zero(self):
        regulator = self.regulator()
        samples = [
            self.sample(regulator, 0, 38, 16),
            self.sample(regulator, 3, 32, 16, ev_at=0),
            self.sample(regulator, 5, 32, 10, grid_at=3),
            self.sample(regulator, 7, 28, 10, ev_at=5),
            self.sample(regulator, 8, 28, 6, grid_at=7),
            self.sample(regulator, 9, 28, 6, ev_at=8),
        ]
        for decision in samples:
            self.assert_minimum(decision)
        self.assertEqual(samples[-2].mode, "overload_shedding")
        self.assertEqual(samples[-1].mode, "overload_minimum")

    def test_post_minimum_high_grid_stops_without_waiting_out_deadline(self):
        regulator = self.regulator()
        self.sample(regulator, 0, 38, 16)
        self.sample(regulator, 3, 38, 10, grid_at=0)
        self.sample(regulator, 5, 32, 10, ev_at=3)
        self.assert_minimum(self.sample(regulator, 7, 32, 6, grid_at=5))
        self.assert_stopped(self.sample(regulator, 8, 31, 6, ev_at=7),
                            "minimum_observed_above_ceiling")
        self.assert_stopped(self.sample(regulator, 9, 28, 6),
                            "minimum_observed_above_ceiling")

    def test_partial_reports_and_renewals_do_not_extend_ten_second_deadline(self):
        regulator = self.regulator(resend_limit_every_seconds=1)
        self.sample(regulator, 0, 38, 16)
        self.sample(regulator, 3, 38, 10, grid_at=0)
        self.sample(regulator, 5, 32, 10, ev_at=3)
        for now in (7, 8, 9):
            pending = self.sample(regulator, now, 32, 6, grid_at=5)
            self.assert_minimum(pending, "overload_shedding")
            self.assertEqual(pending.overload_transition_remaining_seconds, 10 - now)
        self.assert_stopped(self.sample(regulator, 10, 28, 6), "response_timeout")

    def test_thirty_amp_entry_reduces_to_minimum_then_recovers_real_headroom(self):
        regulator = self.regulator()
        initial = self.sample(regulator, 0, 30, 16)
        partial = self.sample(regulator, 1, 30, 10, grid_at=0)
        grid_partial = self.sample(regulator, 4, 24, 10, ev_at=1)
        final = self.sample(regulator, 7, 20, 6)
        for decision in (initial, partial, grid_partial):
            self.assert_minimum(decision, "overload_shedding")
        self.assertEqual(initial.available_l1, 9)
        self.assertGreaterEqual(final.current_p1, 6)
        self.assertLessEqual(final.current_p1, 9)
        self.assertNotEqual(final.command, "pause")
        self.assertFalse(final.overload_failure_latched)

    def test_opt_in_retains_six_across_normal_reserve_boundary(self):
        for grid in (24.5, 24.51, 25.0, 28.999, 29.0):
            with self.subTest(grid=grid):
                enabled = self.sample(self.regulator(), 0, grid, 6)
                self.assert_minimum(enabled)
                disabled = self.sample(self.regulator(overload_enabled=False), 0, grid, 6)
                self.assertEqual(disabled.current_p1, 6 if grid <= 24.5 else 0)

    def test_twenty_nine_amp_hold_bypasses_normal_two_minute_guard(self):
        regulator = self.regulator()
        for now in (0, 21, 121, 600):
            decision = self.sample(regulator, now, 29, 6)
            self.assert_minimum(decision, "overload_minimum")
            self.assertEqual(decision.overload_used_seconds, now)
            self.assertEqual(decision.overload_remaining_seconds, 1800 - now)

    def test_transition_ceiling_and_post_response_ceiling_are_separate(self):
        self.assert_minimum(self.sample(self.regulator(), 0, 38, 16), "overload_shedding")
        self.assert_stopped(self.sample(self.regulator(), 0, 38.001, 16),
                            "transition_current_exceeded")
        self.assert_minimum(self.sample(self.regulator(), 0, 29, 6), "overload_minimum")
        self.assert_stopped(self.sample(self.regulator(), 0, 29.001, 6),
                            "minimum_projection_exceeds_ceiling")

    def test_identical_grid_receipts_preserve_positive_alignment_without_refund(self):
        regulator = self.regulator()
        initial = self.sample(regulator, 0, 8, 6)
        self.assertEqual(initial.current_p1, 16)
        # EV arrives before its grid response. Identical valid grid receipts
        # must not manufacture a load change or restart the alignment budget.
        first = self.sample(regulator, 1, 8, 10, grid_at=0)
        self.assertEqual((first.mode, first.current_p1), ("settling", 16))
        for now in (2, 5, 20, 25):
            same = self.sample(regulator, now, 8, 10, ev_at=1)
            self.assertEqual((same.mode, same.current_p1), ("settling", 16))
            self.assertFalse(same.overload_failure_latched)
        expired = self.sample(regulator, 26, 8, 10, ev_at=1)
        self.assertEqual((expired.mode, expired.current_p1), ("unavailable", 0))

    def test_invalid_caller_source_verdict_overrides_pending_overload(self):
        # Absolute grid age/future checks belong to the runtime input gate.
        # Event-driven stable EV values alone are not a transport failure.
        for timestamp, reason in ((-121, "grid report stale"), (8, "grid report is in future")):
            with self.subTest(reason=reason):
                regulator = self.regulator()
                self.sample(regulator, 0, 38, 16)
                invalid = self.sample(regulator, 2, 32, 6, grid_at=timestamp,
                                      data_valid=False, data_error=reason)
                self.assertEqual((invalid.mode, invalid.current_p1), ("unavailable", 0))
                self.assertTrue(invalid.overload_failure_latched)
                self.assert_stopped(self.sample(regulator, 3, 28, 6), "invalid_telemetry_during_overload")


if __name__ == "__main__":
    unittest.main()
