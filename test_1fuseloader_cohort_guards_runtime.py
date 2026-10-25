"""Adversarial conductor-cohort guards through the real controller, offline.

All currents, product bindings, clocks and Store I/O are synthetic. HTTP
acceptance supplies no physical-delivery or synchronized-sampling guarantee.
"""
from dataclasses import replace
import unittest

from test_1fuseloader_ha_current_representation import float32
import test_1fuseloader_zero_cohort_runtime as cohort_fixtures


class CohortGuardRuntimeTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup/protocol helpers without inheriting their test methods.
    update_entry = cohort_fixtures.ZeroCohortRuntimeTests.update_entry
    wall = cohort_fixtures.ZeroCohortRuntimeTests.wall
    schedule = cohort_fixtures.ZeroCohortRuntimeTests.schedule
    active_timers = cohort_fixtures.ZeroCohortRuntimeTests.active_timers
    asyncSetUp = cohort_fixtures.ZeroCohortRuntimeTests.asyncSetUp
    asyncTearDown = cohort_fixtures.ZeroCohortRuntimeTests.asyncTearDown
    set_native = cohort_fixtures.ZeroCohortRuntimeTests.set_native
    snapshot = cohort_fixtures.ZeroCohortRuntimeTests.snapshot
    install = cohort_fixtures.ZeroCohortRuntimeTests.install
    native_vector = cohort_fixtures.ZeroCohortRuntimeTests.native_vector
    grid = cohort_fixtures.ZeroCohortRuntimeTests.grid
    reduced_offer = cohort_fixtures.ZeroCohortRuntimeTests.reduced_offer

    def publish_old_winner(self, *, sequence=2, changed_at=None):
        changed_at = self.now-.01 if changed_at is None else changed_at
        self.native_vector(float32(16.139), (183,),
            {182: changed_at, 183: 162.25}, {182: sequence, 183: 1}, changed_at)
        self.grid(23.)

    async def qualified_cohort(self, *, at=232.36):
        await self.reduced_offer()
        self.now = at
        self.publish_old_winner(changed_at=at-.06)
        await self.controller.async_tick()
        d = self.controller.last_decision
        self.assertEqual((d.mode, d.current_p1, d.command),
                         ("waiting_for_current_cohort", 15, None), d)
        self.assertFalse(d.should_call_service)
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNotNone(d.source_processing_deadline_ts)
        self.assertGreater(d.source_processing_deadline_ts, self.now)
        self.assertLessEqual(d.source_processing_deadline_ts, self.now+1.)
        return d.source_processing_deadline_ts

    def assert_history(self):
        state = self.controller.regulator.export_overload_state()
        self.assertGreaterEqual(state["used_seconds"], 3601.)
        self.assertEqual(state["failure_reason"], "response_timeout")

    def assert_not_held(self):
        self.assertNotEqual(self.controller.last_decision.mode,
                            "waiting_for_current_cohort")
        self.assertFalse(any(call.command == "resume" for call in self.adapter.calls))
        self.assert_history()

    async def test_tuple_get_and_attribute_churn_cannot_extend_first_deadline(self):
        deadline = await self.qualified_cohort()
        before = self.controller.regulator.export_overload_state()["normal_control"]["stops"]
        for sequence, at in enumerate((232.5, 232.8, 233.2), start=3):
            self.now = at
            self.publish_old_winner(sequence=sequence)
            self.install(self.snapshot(current=float32(16.139), observed=162.25, ids=(183,)))
            self.states["sensor.car"].attributes["unrelated_led"] = sequence
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.source_processing_deadline_ts, deadline)
            self.assertEqual(self.adapter.calls, [])
            self.assertEqual(self.controller.regulator.export_overload_state()["normal_control"]["stops"], before)
        self.now = deadline
        self.publish_old_winner(sequence=6)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        stopped = self.controller.regulator.export_overload_state()["normal_control"]["stops"]
        self.assertGreater(stopped, before)
        self.now += .1
        self.publish_old_winner(sequence=7)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.regulator.export_overload_state()["normal_control"]["stops"], stopped)
        self.assertTrue(all(call.current_p1 == 0 for call in self.adapter.calls))

    async def test_due_ttl_is_not_renewed_inside_the_cohort(self):
        # The last real 15 A acknowledgement was t228. A normal resend is due
        # at t258; source processing may retain it but cannot renew that lease.
        deadline = await self.qualified_cohort(at=257.95)
        previous = self.controller.regulator.last_set_limit_ts
        self.now = 258.05
        self.publish_old_winner(sequence=3)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.source_processing_deadline_ts, deadline)
        self.assertEqual(self.controller.regulator.last_set_limit_ts, previous)
        self.assertEqual(self.adapter.calls, [])
        self.now = deadline
        self.publish_old_winner(sequence=4)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_original_lease_expiry_shortens_the_one_second_fence(self):
        deadline = await self.qualified_cohort(at=287.8)
        self.assertAlmostEqual(deadline, 288.)
        self.now = deadline
        self.publish_old_winner(sequence=3)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_existing_reduction_guard_shortens_the_processing_fence(self):
        await self.reduced_offer()
        self.now = 232.36
        # Inject a previously established electrical guard, then exercise the
        # actual controller. Source coalescing must not outlive this boundary.
        guard = self.now+.15
        self.controller.regulator._reduction_guard_deadline = guard
        self.publish_old_winner(changed_at=self.now-.06)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.source_processing_deadline_ts, guard)
        self.now = guard
        self.publish_old_winner(sequence=3)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_native_source_fault_cannot_wait_for_the_processing_deadline(self):
        await self.qualified_cohort()
        self.now += .05
        self.native.error = "native_terminal_source_mismatch"
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertTrue(any(call.current_p1 == 0 for call in self.adapter.calls))

    async def test_actual_peak_cannot_wait_for_the_processing_deadline(self):
        await self.qualified_cohort()
        self.now += .05
        self.grid(40.)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertTrue(any(call.current_p1 == 0 for call in self.adapter.calls))

    async def test_raw_above_normal_envelope_cannot_retain_fifteen(self):
        await self.qualified_cohort()
        self.now += .05
        self.grid(23.001)
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertLess(self.controller.last_decision.current_p1, 15)

    async def test_native_commit_loss_cannot_reuse_the_cohort(self):
        await self.qualified_cohort()
        self.now += .05
        self.native.committed = False
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_opt_out_cannot_reuse_the_cohort(self):
        await self.qualified_cohort()
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=False))
        self.now += .05
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_manual_pause_cannot_reuse_the_cohort_or_resume(self):
        await self.qualified_cohort()
        self.now += .05
        self.states["sensor.status"].state = "paused"
        self.states["sensor.status"].last_changed = self.wall()
        await self.controller.async_tick()
        self.assert_not_held()
        self.assertFalse(any(call.current_p1 > 0 for call in self.adapter.calls))

    async def test_custom_soft_limit_has_priority_over_source_processing(self):
        await self.reduced_offer()
        self.controller.regulator.update_settings(replace(self.controller.settings,
            enable_soft_guard=True, soft_limit_amps=20, soft_for_seconds=0,
            soft_current_amps=7))
        self.now = 232.36
        self.publish_old_winner(changed_at=self.now-.06)
        await self.controller.async_tick()
        self.assertEqual((self.controller.last_decision.mode, self.controller.last_decision.current_p1),
                         ("soft_limit", 7))
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [7])
        self.assert_history()

    async def test_custom_pause_has_priority_over_source_processing(self):
        await self.reduced_offer()
        self.controller.regulator.update_settings(replace(self.controller.settings,
            enable_pause_resume=True, pause_limit_amps=20, pause_for_seconds=0))
        self.now = 232.36
        self.publish_old_winner(changed_at=self.now-.06)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.command, "pause")
        self.assertEqual([call.command for call in self.adapter.calls], ["pause"])
        self.assert_history()


if __name__ == "__main__":
    unittest.main()
