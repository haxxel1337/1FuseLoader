"""Shared minimum phase at the real dispatch boundary; entirely offline."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_1fuseloader_submission_attempts as attempts
import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class SharedConfirmationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = cohorts.ZeroCohortRuntimeTests.update_entry
    wall = cohorts.ZeroCohortRuntimeTests.wall
    schedule = cohorts.ZeroCohortRuntimeTests.schedule
    active_timers = cohorts.ZeroCohortRuntimeTests.active_timers
    asyncSetUp = cohorts.ZeroCohortRuntimeTests.asyncSetUp
    asyncTearDown = cohorts.ZeroCohortRuntimeTests.asyncTearDown
    set_native = cohorts.ZeroCohortRuntimeTests.set_native
    snapshot = cohorts.ZeroCohortRuntimeTests.snapshot
    install = cohorts.ZeroCohortRuntimeTests.install
    native_vector = cohorts.ZeroCohortRuntimeTests.native_vector
    grid = cohorts.ZeroCohortRuntimeTests.grid
    establish = attempts.SubmissionAttemptTests.establish

    async def waiting(self):
        r = await self.establish("low_grid")
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.old_plan = self.controller.last_decision
        self.lower_ack = r.last_set_limit_ts
        self.adapter.calls.clear()
        self.now = 203.
        self.native_vector(float32(7.05), (182, 183),
            {182: 202.8, 183: 202.8}, {182: 3, 183: 3}, 202.8)
        await self.controller.async_tick()
        d = self.controller.last_decision
        self.assertEqual((d.mode, d.current_p1, d.command),
                         ("low_grid_grid_wait", 7, None))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 232.8, places=5)
        self.assertEqual(self.adapter.calls, [])
        return r

    async def test_low_grid_wait_crosses_original_deadline_without_dispatch_or_renewal(self):
        r = await self.waiting()
        for at in (204.5, 210., 225., 232.7):
            self.now = at
            await self.controller.async_tick()
            self.assertEqual(self.adapter.calls, [])
            self.assertEqual(r.last_set_limit_ts, self.lower_ack)
            self.assertEqual(self.controller.last_decision.mode, "low_grid_grid_wait")
        self.now = 232.75
        self.grid(9.2)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "low_grid_alignment_complete")
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertIsNone(r._minimum_confirmation)

    async def test_low_grid_wait_suppresses_forged_writes_and_resume(self):
        r = await self.waiting()
        original = self.controller.last_decision
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                with patch.object(r, "evaluate", return_value=replace(
                        original, command=command, should_call_service=True)):
                    await self.controller.async_tick()
                self.assertEqual(self.adapter.calls, [])
                self.assertIsNone(self.controller.last_decision.command)
                self.assertFalse(self.controller.last_decision.should_call_service)
                self.assertEqual(r.last_set_limit_ts, self.lower_ack)

    async def test_low_grid_fixed_deadline_wakes_and_protects_at_expiry(self):
        await self.waiting()
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        timer = next(t for t in self.active_timers() if abs(t.due-deadline) < 1e-6)
        self.now = deadline
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once_with(None)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIn("grid_confirmation_timeout", self.controller.last_decision.reason)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_low_grid_peak_and_new_post_response_overload_remain_protective(self):
        await self.waiting()
        self.now = 204.
        self.grid(38.01)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_old_high_grid_can_enter_the_same_post_minimum_phase(self):
        r = await self.establish("low_grid")
        await self.controller.async_tick()
        self.adapter.calls.clear()
        self.now = 203.
        self.grid(30.4, 202.5)
        self.native_vector(float32(7.05), (182, 183),
            {182: 202.8, 183: 202.8}, {182: 3, 183: 3}, 202.8)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "low_grid_grid_wait")
        self.assertEqual(self.adapter.calls, [])
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        self.now = 210.
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts, deadline)
        self.assertEqual(self.adapter.calls, [])
        self.now = 211.
        self.grid(9.2)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "low_grid_alignment_complete")
        self.assertIsNone(r._minimum_confirmation)

    async def test_later_grid_above_the_normal_ceiling_still_stops(self):
        await self.waiting()
        self.now = 204.
        self.grid(23.01)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIn("outside_ceiling", self.controller.last_decision.reason)

    async def known_post_provider_high_grid(self, kind):
        r = await self.establish(kind)
        await self.controller.async_tick()
        self.adapter.calls.clear()
        self.now = 203.
        self.grid(30.4, 202.5)
        self.native_vector(float32(7.05), (182, 183),
            {182: 202.8, 183: 202.8}, {182: 3, 183: 3}, 201.)
        self.native.source_timestamps = tuple((key, self.wall(201.) if key in (182, 183) else None)
                                              for key in (182, 183, 184, 185))
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIn({"low_grid": "grid_confirmation_outside_ceiling",
                       "normal": "grid_confirmation_outside_nominal_budget",
                       "overload": "minimum_observed_above_ceiling"}[kind],
                      self.controller.last_decision.reason)
        self.assertIsNone(r._minimum_confirmation)

    async def test_low_grid_known_provider_high_sample_cannot_hide_behind_receipt(self):
        await self.known_post_provider_high_grid("low_grid")

    async def test_nominal_known_provider_high_sample_cannot_hide_behind_receipt(self):
        await self.known_post_provider_high_grid("normal")

    async def test_trial_known_provider_high_sample_cannot_hide_behind_receipt(self):
        await self.known_post_provider_high_grid("overload")

    async def test_provider_floor_can_reject_high_grid_but_cannot_create_success(self):
        r = await self.establish("low_grid")
        await self.controller.async_tick()
        self.adapter.calls.clear()
        self.now = 203.
        self.grid(9.2, 202.5)
        self.native_vector(float32(7.05), (182, 183),
            {182: 202.8, 183: 202.8}, {182: 3, 183: 3}, 202.8)
        self.native.source_timestamps = tuple((key, self.wall(201.) if key in (182, 183) else None)
                                              for key in (182, 183, 184, 185))
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "low_grid_grid_wait")
        self.assertEqual(self.adapter.calls, [])
        self.assertAlmostEqual(r._minimum_confirmation.deadline, 232.8, places=5)
        self.assertAlmostEqual(r._minimum_confirmation.floor, self.epoch+202.8, places=5)

    async def test_expired_unsent_plan_recognizes_the_shared_low_grid_wait(self):
        r = await self.waiting()
        self.now = 205.
        genuine = r.evaluate
        first = True

        def old_then_current(inp):
            nonlocal first
            if first:
                first = False
                return replace(self.old_plan, command="set_limit", should_call_service=True)
            return genuine(inp)

        with patch.object(r, "evaluate", side_effect=old_then_current):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "low_grid_grid_wait")
        self.assertEqual(r.last_set_limit_ts, self.lower_ack)


if __name__ == "__main__":
    unittest.main()
