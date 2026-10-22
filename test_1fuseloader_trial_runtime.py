"""Explicit period persistence races through the real controller, offline."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_refresh_deadlines as helpers


class TrialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = helpers.RefreshDeadlineTests.update_entry
    wall = helpers.RefreshDeadlineTests.wall
    snapshot = helpers.RefreshDeadlineTests.snapshot
    schedule = helpers.RefreshDeadlineTests.schedule
    active_timers = helpers.RefreshDeadlineTests.active_timers

    async def asyncSetUp(self):
        await helpers.RefreshDeadlineTests.asyncSetUp(self)
        self.now = 100.0
        self.entry.options.update(overload_enabled=True, bounded_nominal_shedding=True,
                                  nominal_import_only_fallback=True)
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._configured_control_identity = lambda: "synthetic-source-binding"
        self.controller._schedule_current_snapshot = Mock()
        self.controller._request_current_snapshot = Mock()
        self.controller._handle_source_change = Mock()
        self.controller._budget_loaded = True
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.adapter.offline_limits_verified_zero = True
        self.regulator = self.controller.regulator
        self.regulator.import_overload_state(dict(
            version=1, used_seconds=2400.0, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))
        self.publish(21.7, 16.1)
        await self.controller.async_tick()
        self.now += 1
        self.publish(21.7, 16.1)
        await self.controller.async_tick()
        self.assertIsNone(self.regulator._awaiting_reduction_target)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])

    async def asyncTearDown(self):
        await helpers.RefreshDeadlineTests.asyncTearDown(self)

    def publish(self, grid, current, *, status="charging"):
        for name, value in (("sensor.grid", grid), ("sensor.car", current), ("sensor.status", status)):
            self.states[name].state = str(value)
            self.states[name].last_reported = self.wall()

    def disk_state(self):
        return deepcopy(self.controller._budget_store.store.data["regulator"])

    def assert_historical_state(self):
        state = self.regulator.export_overload_state()
        self.assertGreaterEqual(state["used_seconds"], 2400)
        self.assertEqual(state["failure_reason"], "response_timeout")

    def assert_no_button_command(self):
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])

    async def test_two_verified_checkpoints_precede_permission_without_charger_command(self):
        observed = []
        save = self.controller._budget_store.async_save

        async def observe(state):
            observed.append((state["trial_epoch"]["state"],
                             self.regulator.effective_overload_failure_reason))
            return await save(state)

        self.controller._budget_store.async_save = observe
        await self.controller.async_start_overload_trial()
        self.assertEqual([phase for phase, _ in observed], ["pending", "active"])
        self.assertTrue(all(failure is not None for _, failure in observed))
        self.assertIsNone(self.regulator.effective_overload_failure_reason)
        self.assertEqual(self.regulator.overload_minimum_current, 7)
        self.assertEqual(self.regulator.settings.min_charge_amps, 6)
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "active")
        self.assertTrue(self.controller._budget_store.state_is_durable(self.regulator.export_overload_state()))
        self.assert_historical_state()
        self.assert_no_button_command()

    async def test_concurrent_clicks_cannot_create_two_periods_or_refill_time(self):
        results = await asyncio.gather(self.controller.async_start_overload_trial(),
                                       self.controller.async_start_overload_trial(), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        before = deepcopy(self.disk_state()["trial_epoch"])
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertEqual(self.disk_state()["trial_epoch"], before)
        self.assert_historical_state()
        self.assert_no_button_command()

    async def test_pending_checkpoint_failure_cannot_grant_permission(self):
        self.controller._budget_store.store.failure = True
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assertEqual(self.regulator.export_overload_state()["trial_epoch"]["state"], "failed")
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")
        self.assert_historical_state()
        self.assert_no_button_command()

    async def test_active_checkpoint_failure_leaves_only_interrupted_restorable_permission(self):
        save = self.controller._budget_store.async_save

        async def fail_active(state):
            if state["trial_epoch"]["state"] == "active":
                raise OSError("synthetic unavailable storage")
            return await save(state)

        self.controller._budget_store.async_save = fail_active
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "pending")
        restored = fixtures.logic.OneFuseRegulator(self.regulator.settings)
        self.assertTrue(restored.import_overload_state(self.disk_state()))
        self.assertIsNotNone(restored.effective_overload_failure_reason)
        self.assertEqual(restored.export_overload_state()["trial_epoch"]["state"], "interrupted")
        self.assert_historical_state()
        self.assert_no_button_command()

    async def changed_during_checkpoint(self, phase, change):
        save = self.controller._budget_store.async_save

        async def changed(state):
            result = await save(state)
            if state["trial_epoch"]["state"] == phase:
                change()
            return result

        self.controller._budget_store.async_save = changed
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assert_historical_state()
        self.assert_no_button_command()

    async def test_new_high_raw_before_staging_prevents_active_checkpoint(self):
        await self.changed_during_checkpoint("pending", lambda: self.publish(26, 16.1))
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "pending")

    async def test_new_high_raw_after_active_save_prevents_memory_permission(self):
        await self.changed_during_checkpoint("active", lambda: self.publish(26, 16.1))
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "active")
        restored = fixtures.logic.OneFuseRegulator(self.regulator.settings)
        self.assertTrue(restored.import_overload_state(self.disk_state()))
        self.assertIsNotNone(restored.effective_overload_failure_reason)

    async def test_stream_failure_between_checkpoints_cannot_grant_permission(self):
        await self.changed_during_checkpoint("pending", lambda: setattr(
            self.adapter, "telemetry_block_reason", "easee_stream_disconnected"))

    async def test_generation_change_after_active_save_cannot_grant_permission(self):
        await self.changed_during_checkpoint("active", lambda: setattr(
            self.controller, "_generation", self.controller._generation+1))

    async def test_lease_expiry_during_active_save_cannot_be_renewed_by_trial_start(self):
        await self.changed_during_checkpoint("active", lambda: setattr(self, "now", 161.0))

    async def test_start_cannot_override_zero_or_storage_barriers(self):
        for attribute, value in (("_protective_zero_pending", True), ("_budget_loaded", False),
                                  ("_budget_error", "overload_budget_restore_failed"),
                                  ("_positive_retry_after", self.now+30)):
            with self.subTest(attribute=attribute):
                original = getattr(self.controller, attribute)
                setattr(self.controller, attribute, value)
                with self.assertRaises(ValueError):
                    await self.controller.async_start_overload_trial()
                setattr(self.controller, attribute, original)
                self.assertNotIn("trial_epoch", self.regulator.export_overload_state())
        self.assert_no_button_command()

    async def test_start_cannot_override_three_prior_normal_recovery_failures(self):
        state = self.regulator.export_overload_state()
        state["normal_control"].update(cap=7, previous_offer=16, stops=3,
                                       stop_recorded=True, restart=True)
        self.regulator.import_overload_state(state)
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertEqual(self.regulator.export_overload_state()["normal_control"]["stops"], 3)
        self.assertNotIn("trial_epoch", self.regulator.export_overload_state())
        self.assert_no_button_command()

    async def test_noncharging_car_cannot_be_started_by_trial_button(self):
        self.publish(4.5, .012, status="awaiting_start")
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertNotIn("trial_epoch", self.regulator.export_overload_state())
        self.assert_no_button_command()

    async def test_offline_fallback_requires_explicit_verification(self):
        self.adapter.offline_limits_verified_zero = False
        with self.assertRaises(ValueError):
            await self.controller.async_start_overload_trial()
        self.assertNotIn("trial_epoch", self.regulator.export_overload_state())
        self.assert_no_button_command()

    async def test_end_withdraws_permission_without_a_charger_command(self):
        await self.controller.async_start_overload_trial()
        await self.controller.async_end_overload_trial()
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "ended")
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assert_historical_state()
        self.assert_no_button_command()

    async def test_end_checkpoint_failure_withdraws_local_permission_anyway(self):
        await self.controller.async_start_overload_trial()
        self.controller._budget_store.store.failure = True
        with self.assertRaises(ValueError):
            await self.controller.async_end_overload_trial()
        self.assertEqual(self.regulator.export_overload_state()["trial_epoch"]["state"], "ended")
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assert_no_button_command()

    async def test_cancelled_active_write_cannot_resurrect_permission_after_restart(self):
        save = self.controller._budget_store.store.async_save
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_save(payload):
            if payload["regulator"]["trial_epoch"]["state"] == "active":
                entered.set()
                await release.wait()
            return await save(payload)

        self.controller._budget_store.store.async_save = blocked_save
        task = asyncio.create_task(self.controller.async_start_overload_trial())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await self.controller._budget_store.async_wait_pending()
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "active")
        restored = fixtures.logic.OneFuseRegulator(self.regulator.settings)
        self.assertTrue(restored.import_overload_state(self.disk_state()))
        self.assertIsNotNone(restored.effective_overload_failure_reason)
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assert_no_button_command()

    async def test_late_active_save_after_timeout_is_followed_by_terminal_checkpoint(self):
        save = self.controller._budget_store.store.async_save
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(payload):
            if payload["regulator"].get("trial_epoch", {}).get("state") == "active":
                entered.set()
                await release.wait()
            return await save(payload)

        self.controller._budget_store.store.async_save = delayed
        with patch.object(fixtures.runtime, "EXPOSURE_STORE_TIMEOUT_SECONDS", .2):
            task = asyncio.create_task(self.controller.async_start_overload_trial())
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaises(ValueError):
                await task
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        self.assert_no_button_command()
        release.set()
        await self.controller._budget_store.async_wait_pending()
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "active")
        self.controller._budget_store.store.async_save = save
        await self.controller.async_tick()
        self.assertEqual(self.disk_state()["trial_epoch"]["state"], "failed")
        self.assertIsNone(self.controller._budget_error)
        self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls[1:]))
        self.assertIsNotNone(self.regulator.effective_overload_failure_reason)
        incident = self.controller.last_stop_context
        self.assertEqual(incident["overload_trial_id"], self.disk_state()["trial_epoch"]["trial_id"])
        self.assertEqual(incident["overload_historical_failure_reason"], "response_timeout")
        self.assertEqual(incident["overload_effective_failure_reason"], "activation_not_verified")
        self.assertEqual(incident["overload_trial_state"], "failed")
        self.assert_historical_state()


if __name__ == "__main__":
    unittest.main()
