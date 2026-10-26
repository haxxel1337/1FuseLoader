"""Automatic operational cycles through the real controller, entirely offline.

All readings, source identities, clocks, storage and adapter replies are
synthetic. The cooling interval is a software admission rule, not a fuse
temperature model. No private household records or live services are used.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_submission_attempts as attempts
import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class AutomaticCyclesRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
        self.sequence = 2

    async def automatic(self):
        r = await self.establish()
        options = dict(automatic_overload_cycles=True, startup_charge_amps=7,
            overload_margin_amps=4, overload_budget_seconds=1800,
            overload_transition_limit_amps=38, overload_response_seconds=10)
        self.entry.options.update(options)
        r.update_settings(replace(self.controller.settings, **options))
        self.adapter.calls.clear()
        return r

    def publish(self, at, grid, current, *, grid_at=None, received=None):
        self.now = at
        self.sequence += 1
        receipt = at-.3 if received is None else received
        self.native_vector(float32(current), (182, 183),
            {182: receipt, 183: receipt},
            {182: self.sequence, 183: self.sequence}, receipt)
        self.grid(grid, at if grid_at is None else grid_at)

    async def enter_minimum(self):
        r = await self.automatic()
        self.publish(200., 30.4, 16.1)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "qualifying")
        self.publish(200.8, 28.5, 7.1)
        await self.controller.async_tick()
        self.assertGreater(self.controller.last_decision.current_p1, 0)
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls))
        return r

    async def active(self):
        r = await self.enter_minimum()
        for at in (210., 220., 230.):
            self.publish(at, 28.5, 7.1)
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "active")
        return r

    def assert_history(self, r, minimum=3601.):
        state = r.export_overload_state()
        self.assertGreaterEqual(state["used_seconds"], minimum)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertNotIn("trial_epoch", state)
        return state

    async def test_qualification_checkpoint_precedes_first_real_minimum_dispatch(self):
        r = await self.automatic()
        entered, release = asyncio.Event(), asyncio.Event()
        save = self.controller._budget_store.async_save
        execute = self.adapter.async_execute
        at_dispatch = []

        async def gated_save(state):
            self.assertEqual(state["automatic_cycle"]["state"], "qualifying")
            entered.set()
            await release.wait()
            return await save(state)

        async def checked_dispatch(decision):
            state = r.export_overload_state()
            at_dispatch.append((decision.current_p1,
                self.controller._budget_store.state_is_durable(state),
                deepcopy(state["automatic_cycle"])))
            return await execute(decision)

        self.controller._budget_store.async_save = gated_save
        self.adapter.async_execute = checked_dispatch
        self.publish(200., 30.4, 16.1)
        task = asyncio.create_task(self.controller.async_tick())
        await entered.wait()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 16)
        release.set()
        await task
        self.assertEqual([(amp, durable) for amp, durable, _ in at_dispatch], [(7, True)])
        self.assertEqual(at_dispatch[0][2]["state"], "qualifying")
        self.assert_history(r)

    async def test_failed_checkpoint_never_uses_in_memory_cycle_permission(self):
        r = await self.automatic()
        self.controller._budget_store.store.failure = True
        self.publish(200., 30.4, 16.1)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")
        self.assertIsNone(r._overload_minimum_accepted_at)
        self.assertFalse(self.controller._budget_store.state_is_durable(r.export_overload_state()))
        self.assert_history(r)

    async def test_unverified_readback_never_grants_positive_permission(self):
        r = await self.automatic()
        old = deepcopy(self.controller._budget_store.store.data)

        async def stale_readback():
            return old

        self.controller._budget_store._read_back = stale_readback
        self.publish(200., 30.4, 16.1)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(r._overload_minimum_accepted_at)
        self.assert_history(r)

    async def test_source_revision_after_checkpoint_does_not_dispatch_or_spend_attempt(self):
        r = await self.automatic()
        save = self.controller._budget_store.async_save
        changed = False

        async def newer_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 200.05
                self.grid(30.3, 200.)
            return result

        self.controller._budget_store.async_save = newer_grid
        self.publish(200., 30.4, 16.1)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(r._overload_minimum_attempted_at)
        self.assertIsNone(r._overload_minimum_accepted_at)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.assertFalse(self.controller._protective_zero_pending)
        self.controller._budget_store.async_save = save
        self.now = 200.1
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assert_history(r)

    async def test_first_thirty_seconds_spend_same_budget_and_keep_history(self):
        r = await self.active()
        state = self.assert_history(r, minimum=3631.)
        self.assertAlmostEqual(state["automatic_cycle"]["used_seconds"], 30., places=6)
        self.assertEqual(r.effective_overload_failure_reason, None)
        self.assertAlmostEqual(r.effective_overload_used_seconds, 30., places=6)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_qualification_seconds"], 30.)
        self.assertTrue(self.controller._budget_store.state_is_durable(state))

    async def test_repeated_grid_clock_cannot_qualify_or_refund_exposure(self):
        r = await self.enter_minimum()
        self.publish(209., 28.5, 7.1, grid_at=200.8)
        await self.controller.async_tick()
        diagnostics = r.automatic_cycle_diagnostics()
        self.assertEqual(diagnostics["automatic_cycle_state"], "qualifying")
        self.assertAlmostEqual(diagnostics["automatic_qualification_seconds"], .8, places=6)
        self.assertAlmostEqual(diagnostics["automatic_cycle_used_seconds"], 9., places=6)
        self.assert_history(r, minimum=3610.)

    async def test_full_cumulative_budget_cannot_be_restarted_by_dips_below_twenty_five(self):
        r = await self.active()
        self.publish(240., 24., 7.1)
        await self.controller.async_tick()
        self.publish(250., 24., 7.1)
        await self.controller.async_tick()
        self.assertAlmostEqual(r.effective_overload_used_seconds, 40., places=6)
        cycle_id = r.automatic_cycle_diagnostics()["automatic_cycle_id"]
        # A short non-overload dip stops charging time, but does not replenish
        # the same cycle. Subsequent high samples spend the remaining budget.
        for at in range(260, 2021, 10):
            self.publish(float(at), 28.5, 7.1)
            await self.controller.async_tick()
        state = self.assert_history(r, minimum=5401.)
        self.assertEqual(state["automatic_cycle"]["cycle_id"], cycle_id)
        self.assertGreaterEqual(state["automatic_cycle"]["used_seconds"], 1800.)
        self.assertIsNotNone(r.effective_overload_failure_reason)
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertTrue(any(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_restore_keeps_used_time_and_blocks_interrupted_cycle_without_cooling_credit(self):
        r = await self.active()
        state = self.assert_history(r)
        before_used = state["used_seconds"]
        before_cycle_used = state["automatic_cycle"]["used_seconds"]
        await self.controller.async_stop()
        self.now = 250.
        self.adapter.calls.clear()
        self.controller = fixtures.runtime.OneFuseLoaderController(
            self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        self.publish(250., 22.9, 7.1)
        await self.controller.async_start()
        restored = self.assert_history(self.controller.regulator, minimum=before_used+20.)
        cycle = restored["automatic_cycle"]
        self.assertEqual(cycle["state"], "blocked")
        self.assertEqual(cycle["failure_reason"], "interrupted_automatic_cycle")
        self.assertGreaterEqual(cycle["used_seconds"], before_cycle_used+20.)
        diagnostics = self.controller.regulator.automatic_cycle_diagnostics()
        self.assertEqual(diagnostics["automatic_qualification_seconds"], 0)
        self.assertEqual(diagnostics["automatic_cooling_seconds"], 0)
        self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_store_load_charges_both_counters_without_mutating_verified_payload(self):
        r = await self.active()
        state = self.assert_history(r)
        store = self.controller._budget_store
        payload = deepcopy(store.store.data)
        self.now = 250.
        loaded = await store.async_load()
        self.assertEqual(store.store.data, payload)
        self.assertAlmostEqual(loaded["used_seconds"], state["used_seconds"]+20., places=6)
        self.assertAlmostEqual(loaded["automatic_cycle"]["used_seconds"], 50., places=6)
        self.assertFalse(store.state_is_durable(loaded))
        await store.async_save(loaded)
        self.assertTrue(store.state_is_durable(loaded))
        self.assertEqual(store.store.data["regulator"], loaded)
        self.assertEqual(store.store.data["regulator"]["failure_reason"], "response_timeout")
        self.assertNotIn("trial_epoch", loaded)

    async def test_store_load_rejects_invalid_nested_cycle_accounting(self):
        await self.active()
        store = self.controller._budget_store
        payload = deepcopy(store.store.data)
        self.now = 250.
        for invalid in (True, float("nan"), float("inf"), -1.):
            with self.subTest(invalid=invalid):
                malformed = deepcopy(payload)
                malformed["regulator"]["automatic_cycle"]["used_seconds"] = invalid
                store.store.data = malformed
                with self.assertRaises(ValueError):
                    await store.async_load()
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls))

    async def test_gap_in_otherwise_low_samples_breaks_cooling_continuity(self):
        r = await self.active()
        self.publish(240., 22.9, 7.1)
        await self.controller.async_tick()
        self.publish(250., 22.9, 7.1)
        await self.controller.async_tick()
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 10.)
        # Two individually fresh reports separated by >15 s do not prove the
        # missing interval was below the threshold, even on the same source.
        self.publish(270., 22.9, 7.1)
        await self.controller.async_tick()
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 0)
        self.assertGreater(r.effective_overload_used_seconds, 0)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_id"], 1)
        self.assert_history(r)

    async def test_cooling_is_strictly_below_twenty_three_for_complete_fresh_interval(self):
        r = await self.active()
        cycle_id = r.automatic_cycle_diagnostics()["automatic_cycle_id"]
        self.publish(240., 22.9, 7.1)
        await self.controller.async_tick()
        for at in range(250, 2031, 10):
            self.publish(float(at), 22.9, 7.1)
            await self.controller.async_tick()
            self.assertGreater(self.controller.last_decision.current_p1, 0)
        before = self.assert_history(r, minimum=3641.)
        self.assertEqual(before["automatic_cycle"]["cycle_id"], cycle_id)
        self.assertGreater(before["automatic_cycle"]["used_seconds"], 0)
        self.publish(2040., 23., 7.1)
        await self.controller.async_tick()
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cooling_seconds"], 0)
        self.publish(2050., 22.9, 7.1)
        await self.controller.async_tick()
        for at in range(2060, 3851, 10):
            self.publish(float(at), 22.9, 7.1)
            await self.controller.async_tick()
        after = self.assert_history(r, minimum=before["used_seconds"])
        self.assertEqual(after["automatic_cycle"]["cycle_id"], cycle_id+1)
        self.assertEqual(after["automatic_cycle"]["state"], "ready")
        self.assertEqual(after["automatic_cycle"]["used_seconds"], 0)
        self.assertIsNone(after["automatic_cycle"]["failure_reason"])
        self.assertTrue(self.controller._budget_store.state_is_durable(after))

    async def test_idle_high_load_does_not_start_charge_or_create_manual_trial(self):
        r = await self.automatic()
        self.states["sensor.status"].state = "car_disconnected"
        self.states["sensor.status"].last_changed = self.wall()
        self.publish(200., 27., 0.)
        await self.controller.async_tick()
        self.assertTrue(all(d.current_p1 == 0 and d.command != "resume" for d in self.adapter.calls))
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.adapter.calls.clear()
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertEqual(self.adapter.calls, [])
        self.assert_history(r)

    async def test_current_source_fault_and_changed_control_binding_still_stop(self):
        for fault in ("native", "binding"):
            with self.subTest(fault=fault):
                if fault == "binding":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.enter_minimum()
                self.adapter.calls.clear()
                self.publish(202., 28.5, 7.1)
                if fault == "native":
                    self.native.error = "native_sdk_current_invalid"
                else:
                    self.controller._configured_control_identity.return_value = "other-source"
                await self.controller.async_tick()
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
                self.assertIsNotNone(r.effective_overload_failure_reason)
                self.assert_history(r)

    async def test_hard_peak_and_original_ten_second_limit_still_override_cycle(self):
        for peak in ("hard", "timeout"):
            with self.subTest(peak=peak):
                if peak == "timeout":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.automatic()
                self.publish(200., 30.4, 16.1)
                await self.controller.async_tick()
                self.assertEqual(self.adapter.calls[-1].current_p1, 7)
                self.publish(201. if peak == "hard" else 210.,
                    38.01 if peak == "hard" else 30.4, 16.1)
                await self.controller.async_tick()
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)
                self.assertIsNotNone(r.effective_overload_failure_reason)
                self.assert_history(r)


if __name__ == "__main__":
    unittest.main()
