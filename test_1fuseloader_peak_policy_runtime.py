"""Opt-in peak-only policy through the actual controller, entirely offline.

Helpers are reused without inheriting older test classes. Conductor values,
source bindings, clocks and API responses are synthetic; no private proof or
household time-series is imported into these public regressions.
"""
from dataclasses import replace
import unittest

import test_1fuseloader_submission_attempts as attempts
import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class PeakPolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = cohorts.ZeroCohortRuntimeTests.update_entry
    wall = cohorts.ZeroCohortRuntimeTests.wall
    schedule = cohorts.ZeroCohortRuntimeTests.schedule
    active_timers = cohorts.ZeroCohortRuntimeTests.active_timers
    asyncTearDown = cohorts.ZeroCohortRuntimeTests.asyncTearDown
    set_native = cohorts.ZeroCohortRuntimeTests.set_native
    snapshot = cohorts.ZeroCohortRuntimeTests.snapshot
    install = cohorts.ZeroCohortRuntimeTests.install
    native_vector = cohorts.ZeroCohortRuntimeTests.native_vector
    grid = cohorts.ZeroCohortRuntimeTests.grid
    establish = attempts.SubmissionAttemptTests.establish

    async def asyncSetUp(self):
        await cohorts.ZeroCohortRuntimeTests.asyncSetUp(self)
        self.entry.options["peak_only_response"] = True
        self.controller.regulator.update_settings(replace(
            self.controller.settings, peak_only_response=True))

    async def ordinary(self):
        r = await self.establish()
        self.assertTrue(self.entry.options["peak_only_response"])
        self.assertTrue(self.controller.settings.peak_only_response)
        self.grid(13.7, 200.)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertEqual(self.controller.last_decision.mode, "ordinary_lower_offer")
        self.assertIsNone(self.controller.last_decision.nominal_response_deadline_ts)
        return r

    async def minimum_wait(self, receipt=215.):
        r = await self.ordinary()
        self.adapter.calls.clear()
        self.now = receipt+.3
        self.native_vector(float32(7.1), (182, 183),
            {182: receipt, 183: receipt}, {182: 3, 183: 3}, receipt)
        self.grid(13.7, receipt-.5)
        await self.controller.async_tick()
        self.assertEqual((self.controller.last_decision.mode,
            self.controller.last_decision.command), ("ordinary_grid_wait", None))
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts, receipt+30., places=6)
        self.assertEqual(self.adapter.calls, [])
        return r

    def assert_no_trial(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertGreaterEqual(state["used_seconds"], 3601.)
        self.assertNotIn("trial_epoch", state)

    async def test_raw_bounded_conflict_holds_lower_offer_past_old_cutoffs(self):
        r = await self.ordinary()
        original_ack = r.last_set_limit_ts
        original_observation_end = r._ordinary_lower.observation_deadline
        for at in (211., 226., 240.):
            self.now = at
            self.grid(13.7)
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 7)
            self.assertIn(self.controller.last_decision.mode,
                {"ordinary_lower_offer", "ordinary_response_hold"})
            if self.controller.last_decision.mode == "ordinary_response_hold":
                self.assertIsNone(self.controller.last_decision.command)
                self.assertFalse(self.controller.last_decision.should_call_service)
                self.assertEqual(r.last_set_limit_ts, original_ack)
                self.assertEqual(self.controller.last_decision.ordinary_response_deadline_ts,
                    original_observation_end)
            self.assertIsNone(self.controller.last_decision.nominal_response_deadline_ts)
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls))
        self.assertEqual(r._ordinary_lower.previous, 16)
        self.assert_no_trial(r)

    async def test_checkpoint_revision_abort_spends_no_attempt_or_zero(self):
        r = await self.establish()
        self.grid(13.7)
        save = self.controller._budget_store.async_save
        changed = False

        async def fresh_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 200.05
                self.grid(13.6, 200.)
            return result

        self.controller._budget_store.async_save = fresh_grid
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._ordinary_lower)  # known unsent proposal is discarded
        self.assertIsNone(r._low_grid_attempt_at)
        self.assertIsNone(r._normal_shed_attempt_at)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.assertFalse(self.controller._protective_zero_pending)
        self.controller._budget_store.async_save = save
        self.now = 200.1
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertEqual(r._ordinary_lower.accepted_at, 200.1)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 0)

    async def test_minimum_after_ten_seconds_enters_fixed_no_write_phase(self):
        r = await self.minimum_wait()
        accepted = r.last_set_limit_ts
        self.now = 230.
        self.grid(13.7, 214.5)
        await self.controller.async_tick()
        decision = self.controller.last_decision
        self.assertEqual((decision.current_p1, decision.command,
            decision.service, decision.should_call_service), (7, None, None, False))
        self.assertAlmostEqual(decision.grid_confirmation_deadline_ts, 245., places=6)
        self.assertIsNone(decision.nominal_response_deadline_ts)
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual(self.adapter.calls, [])

    async def test_observation_expiry_can_keep_independent_raw_safe_offer(self):
        r = await self.minimum_wait()
        self.now = self.controller.last_decision.grid_confirmation_deadline_ts
        self.grid(13.7, 214.5)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertTrue(r._ordinary_lower.confirmation_expired)
        self.now = 246.3
        self.native_vector(float32(7.08), (182, 183),
            {182: 246., 183: 246.}, {182: 4, 183: 4}, 246.)
        self.grid(13.7, 245.5)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        self.assert_no_trial(r)

    async def test_native_fault_and_changed_binding_still_send_zero(self):
        for fault in ("native", "binding"):
            with self.subTest(fault=fault):
                # Each subcase gets a fresh actual controller and accepted chain.
                if fault == "binding":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.ordinary()
                self.adapter.calls.clear()
                self.now = 201.
                self.grid(13.7)
                if fault == "native":
                    self.native.error = "native_sdk_current_invalid"
                else:
                    self.controller._configured_control_identity.return_value = "different-source"
                await self.controller.async_tick()
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
                self.assertEqual(self.controller.regulator.last_target[0], 0)

    async def test_hard_limit_overrides_ordinary_observation(self):
        await self.ordinary()
        self.adapter.calls.clear()
        self.now = 201.
        self.grid(38.01)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_real_peak_still_expires_original_ten_seconds(self):
        r = await self.establish()
        self.grid(30.4, 199.)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertIsNone(r._ordinary_lower)
        self.assertAlmostEqual(r.peak_response_deadline(), 209.)
        self.now = 209.
        self.grid(30.4, 208.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assert_no_trial(r)

    async def test_real_fourteen_to_eight_then_seven_response_releases_anchor(self):
        r = await self.establish()
        self.grid(25., 200.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 14)
        self.now = 202.3
        self.native_vector(float32(4.), (182, 183),
            {182: 202., 183: 202.}, {182: 3, 183: 3}, 202.)
        self.grid(18.1)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 8)
        self.now = 203.7
        self.native_vector(float32(7.9), (182, 183),
            {182: 203., 183: 203.}, {182: 4, 183: 4}, 203.)
        self.grid(13.3)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.now = 205.3
        self.native_vector(float32(7.05), (182, 183),
            {182: 205., 183: 205.}, {182: 5, 183: 5}, 205.)
        self.grid(13.3, 204.8)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_grid_wait")
        self.now = 207.
        self.grid(12., 206.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_lower_complete")
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._house_before_reduction)
        self.now = 210.
        self.grid(12., 209.8)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))

    async def test_intermediate_ten_then_timely_seven_does_not_inherit_false_cutoff(self):
        r = await self.establish()
        self.native_vector(float32(4.), (182, 183),
            {182: 199., 183: 199.}, {182: 3, 183: 3}, 199.)
        self.grid(17., 200.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 10)
        self.now = 205.
        self.native_vector(float32(16.1), (182, 183),
            {182: 204., 183: 204.}, {182: 4, 183: 4}, 204.)
        self.grid(9.1, 205.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.now = 213.3
        self.native_vector(float32(6.94), (182, 183),
            {182: 213., 183: 213.}, {182: 5, 183: 5}, 213.)
        self.grid(9.1, 209.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_grid_wait")
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts, 243., places=6)
        self.now = 215.1
        self.grid(9.1, 209.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertEqual(self.controller.last_decision.command, None)
        self.now = 216.2
        self.grid(9.3, 216.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_lower_complete")
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))


if __name__ == "__main__":
    unittest.main()
