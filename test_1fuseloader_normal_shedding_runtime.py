"""Response-window dispatch races with the real controller and virtual clocks."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_refresh_deadlines as helpers


class NormalSheddingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = helpers.RefreshDeadlineTests.update_entry
    wall = helpers.RefreshDeadlineTests.wall
    snapshot = helpers.RefreshDeadlineTests.snapshot
    schedule = helpers.RefreshDeadlineTests.schedule
    active_timers = helpers.RefreshDeadlineTests.active_timers

    async def asyncSetUp(self):
        await helpers.RefreshDeadlineTests.asyncSetUp(self)
        self.now = 100.0
        self.controller.settings.overload_enabled = True
        self.controller.settings.bounded_nominal_shedding = True
        self.controller.regulator.import_overload_state(dict(
            version=1, used_seconds=2000.0, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))
        self.regulator = self.controller.regulator
        self.regulator.last_target = (16, 0, 0, 60)
        self.regulator.last_set_limit_ts = 95.0
        self.regulator.last_action_ts = 95.0
        self.regulator._last_limit_change_ts = 95.0
        self.states["sensor.grid"].state = "26.6"
        self.states["sensor.grid"].last_reported = self.wall()
        self.states["sensor.car"].state = "16.0"
        self.states["sensor.car"].last_reported = self.wall(50)
        self.states["sensor.status"].last_reported = self.wall()

    async def asyncTearDown(self):
        await helpers.RefreshDeadlineTests.asyncTearDown(self)

    async def test_pending_checkpoint_is_durable_before_lower_offer(self):
        seen = []
        original = self.adapter.async_execute

        async def inspect(decision):
            disk = self.hass._test_store_disk[self.controller._budget_store.store.key]["regulator"]
            seen.append(disk)
            await original(decision)

        self.adapter.async_execute = inspect
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.assertTrue(seen[-1]["normal_control"]["response_pending"])
        self.assertEqual(seen[-1]["failure_reason"], "response_timeout")
        self.assertGreaterEqual(seen[-1]["used_seconds"], 2000)
        self.assertEqual(self.controller.last_service_result, "submitted_unverified")

    async def test_coherent_default_operation_creates_no_empty_recovery_ledger(self):
        self.entry.options.update(overload_enabled=False, bounded_nominal_shedding=False)
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.states["sensor.grid"].state = "21.7"
        self.states["sensor.car"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertIsNone(self.controller._budget_store.store.data)

    async def test_sample_age_and_checkpoint_delay_reduce_api_window(self):
        self.states["sensor.grid"].last_reported = self.wall(97)
        save = self.controller._budget_store.async_save

        async def delayed_save(state):
            result = await save(state)
            self.now += 2
            return result

        self.controller._budget_store.async_save = delayed_save
        wait_for = asyncio.wait_for
        timeouts = []

        async def observe_timeout(awaitable, timeout):
            timeouts.append(timeout)
            return await wait_for(awaitable, timeout)

        with patch.object(fixtures.runtime.asyncio, "wait_for", observe_timeout):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.assertAlmostEqual(timeouts[-1], 5.0)
        self.assertAlmostEqual(self.controller.last_decision.nominal_response_deadline_ts, 107)

    async def test_checkpoint_passing_deadline_never_dispatches_positive(self):
        save = self.controller._budget_store.async_save

        async def delayed_save(state):
            result = await save(state)
            self.now += 11
            return result

        self.controller._budget_store.async_save = delayed_save
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIsNotNone(self.regulator._normal_shed_failure)

    async def test_late_acceptance_queues_expiry_even_without_new_reports(self):
        original = self.adapter.async_execute

        async def late(decision):
            await original(decision)
            self.now += 11

        self.adapter.async_execute = late
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 7)
        self.controller._handle_source_change.assert_called()
        self.adapter.async_execute = original
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertEqual(self.regulator.export_overload_state()["failure_reason"], "response_timeout")

    async def test_disconnect_during_expired_checkpoint_dispatches_no_command(self):
        save = self.controller._budget_store.async_save

        async def disconnected(state):
            result = await save(state)
            self.now += 11
            self.states["sensor.status"].state = "disconnected"
            self.states["sensor.status"].last_reported = self.wall()
            return result

        self.controller._budget_store.async_save = disconnected
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_service_result, "not_sent")
        self.assertEqual(self.controller.last_decision.mode, "idle")

    async def test_expired_checkpoint_stop_records_latest_hard_peak(self):
        save = self.controller._budget_store.async_save

        async def newer_peak(state):
            result = await save(state)
            self.now += 11
            self.states["sensor.grid"].state = "39"
            self.states["sensor.grid"].last_reported = self.wall()
            return result

        self.controller._budget_store.async_save = newer_peak
        await self.controller.async_tick()
        context = self.controller.last_stop_context
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertEqual(context["grid_l1"], 39)
        self.assertEqual(context["grid_report_utc"], self.wall().isoformat())
        self.assertEqual(context["plan_utc"], self.wall().isoformat())

    async def test_invalid_new_recovery_state_preserves_older_exposure(self):
        self.entry.options.update(overload_enabled=True, bounded_nominal_shedding=True)
        older = dict(version=1, used_seconds=4112.5, exposure_active=False,
                     transition_pending=False, minimum_held=False,
                     failure_reason="response_timeout", normal_control={"version": 999})
        self.controller._budget_store.store.data = dict(version=1, saved_at=time.time(), regulator=older)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_start()
        restored = self.controller.regulator.export_overload_state()
        self.assertEqual(restored["used_seconds"], 4112.5)
        self.assertEqual(restored["failure_reason"], "response_timeout")
        self.assertEqual(self.controller._budget_error, "overload_budget_restore_failed")
        self.assertEqual(self.controller._budget_store.store.data["regulator"], older)

    async def test_ambiguous_timeout_queues_protection_without_claiming_acceptance(self):
        async def timeout(decision):
            self.adapter.calls.append(decision)
            self.now += 10
            raise TimeoutError()

        self.adapter.async_execute = timeout
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual(self.controller.last_service_result, "error")
        self.assertIsNone(self.controller.last_accepted_limit)
        self.controller._handle_source_change.assert_called()
        self.adapter.async_execute = fixtures.FakeAdapter.async_execute.__get__(self.adapter)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_old_deadline_callbacks_are_inert_after_generation_change(self):
        await self.controller.async_tick()
        timer = next(item for item in self.active_timers() if item.due == 110)
        self.controller._handle_source_change.reset_mock()
        self.controller._generation += 1
        self.now = 110
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_not_called()

    async def test_new_hard_peak_during_checkpoint_invalidates_lower_offer(self):
        save = self.controller._budget_store.async_save

        async def newer_peak(state):
            result = await save(state)
            self.now += .1
            self.states["sensor.grid"].state = "39"
            self.states["sensor.grid"].last_reported = self.wall()
            return result

        self.controller._budget_store.async_save = newer_peak
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.controller._handle_source_change.assert_called()
        self.controller._budget_store.async_save = save
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def enter_retry(self):
        await self.controller.async_tick()
        self.now = 110
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.now = 111
        self.states["sensor.grid"].state = "2"
        self.states["sensor.car"].state = "0"
        self.states["sensor.status"].state = "awaiting_start"
        for state in self.states.values():
            state.last_reported = self.wall()
        await self.controller.async_tick()
        self.now = 171
        for state in self.states.values():
            state.last_reported = self.wall()

    async def test_failed_real_retry_spends_reservation_and_backs_off(self):
        await self.enter_retry()
        self.adapter.failure = OSError("unavailable")
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.assertEqual(self.controller.last_service_result, "error")
        recovery = self.regulator.export_overload_state()["normal_control"]
        self.assertEqual(recovery["stops"], 2)
        self.assertFalse(recovery["retry_submission_pending"])
        self.assertGreaterEqual(recovery["retry_wait_seconds"], 120)
        self.adapter.failure = None
        self.now = 176
        for state in self.states.values():
            state.last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(sum(call.current_p1 > 0 for call in self.adapter.calls), 2)

    async def test_retry_aborted_by_new_telemetry_releases_reservation(self):
        await self.enter_retry()
        save = self.controller._budget_store.async_save

        async def new_grid(state):
            result = await save(state)
            self.now += .1
            self.states["sensor.grid"].last_reported = self.wall()
            return result

        self.controller._budget_store.async_save = new_grid
        count = len(self.adapter.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.adapter.calls), count)
        recovery = self.regulator.export_overload_state()["normal_control"]
        self.assertEqual(recovery["stops"], 2)
        self.assertFalse(recovery["retry_submission_pending"])


if __name__ == "__main__":
    unittest.main()
