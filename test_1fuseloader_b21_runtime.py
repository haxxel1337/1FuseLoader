"""Native-source sampling races through the real controller, without HA/network.

The fixture publishes independent grid and EV events. No REST snapshot is
invented to align them, and a submitted cap is not physical-response proof.
"""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import Mock

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_refresh_deadlines as helpers
import test_1fuseloader_normal_shedding_runtime as normal_runtime


class NominalSyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = helpers.RefreshDeadlineTests.update_entry
    wall = helpers.RefreshDeadlineTests.wall
    snapshot = helpers.RefreshDeadlineTests.snapshot
    schedule = helpers.RefreshDeadlineTests.schedule
    active_timers = helpers.RefreshDeadlineTests.active_timers

    async def asyncSetUp(self):
        await normal_runtime.NormalSheddingRuntimeTests.asyncSetUp(self)
        self.base = self.now
        self.binding = "synthetic-selected-charger-and-grid"
        self.entry.options.update(overload_enabled=True, bounded_nominal_shedding=True,
                                  nominal_import_only_fallback=False)
        self.bind_native_fixture(self.controller)
        self.regulator._accepted_control_source_id = self.binding
        self.states["sensor.grid"].state = "27.7"
        self.states["sensor.car"].state = "16.202"
        self.states["sensor.car"].last_reported = self.wall(self.base-6.871)

    async def asyncTearDown(self):
        await helpers.RefreshDeadlineTests.asyncTearDown(self)

    def bind_native_fixture(self, controller):
        # Source binding is the already verified public registry contract.
        # Disable the separate GET path so only the explicit native events
        # below can confirm a response or advance either publication clock.
        controller._configured_control_identity = lambda: self.binding
        controller._schedule_current_snapshot = Mock()
        controller._request_current_snapshot = Mock()
        controller._handle_source_change = Mock()

    async def tick(self, offset, *, grid=None, current=None, grid_at=None, ev_at=None):
        self.now = self.base+offset
        if grid is not None:
            self.states["sensor.grid"].state = str(grid)
            self.states["sensor.grid"].last_reported = self.wall(
                self.base+(offset if grid_at is None else grid_at))
        if current is not None:
            self.states["sensor.car"].state = str(current)
            self.states["sensor.car"].last_reported = self.wall(
                self.base+(offset if ev_at is None else ev_at))
        await self.controller.async_tick()
        return self.controller.last_decision

    async def enter_sync(self):
        first = await self.tick(0)
        self.assertEqual((first.mode, first.current_p1), ("normal_shedding", 7))
        self.assertEqual(first.nominal_response_deadline_ts, self.base+10)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        await self.tick(5, grid=18.6)
        response = await self.tick(8.213, current=7.079)
        self.assertEqual((response.mode, response.current_p1), ("normal_shedding_sync", 7))
        self.assertIsNone(response.command)
        self.assertFalse(response.should_call_service)
        self.assertAlmostEqual(response.nominal_sync_deadline_ts, self.base+13.213, places=5)
        self.assertEqual(self.regulator._normal_shed_deadline, self.base+10)
        return response

    def assert_only_existing_offer(self):
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 7)
        self.assertEqual(self.regulator.last_set_limit_ts, self.base)

    async def fire_control_timer(self):
        timer = min(self.active_timers(), key=lambda item: item.due)
        self.now = timer.due
        timer.cancelled = True
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called()
        await self.controller.async_tick()

    async def test_delayed_grid_frame_completes_same_seven_without_any_zero_or_renewal(self):
        await self.enter_sync()
        d = await self.tick(10)
        self.assertEqual((d.mode, d.current_p1), ("normal_shedding_sync", 7))
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertAlmostEqual(d.nominal_sync_remaining_seconds, 3.213, places=5)
        timer = min(self.active_timers(), key=lambda item: item.due)
        self.assertAlmostEqual(timer.due, self.base+13.213, places=5)
        self.assert_only_existing_offer()
        # A frame received 300 ms after the watchdog represents grid time t10.
        complete = await self.tick(10.3, grid=18.6, grid_at=10)
        self.assertEqual((complete.mode, complete.current_p1), ("normal_shedding_complete", 7))
        self.assertIsNone(complete.nominal_sync_deadline_ts)
        self.assertIsNone(complete.nominal_response_deadline_ts)
        self.assert_only_existing_offer()
        budget = self.regulator.export_overload_state()
        self.assertEqual((budget["failure_reason"], budget["used_seconds"]), ("response_timeout", 2005))
        self.assertFalse(budget["normal_control"]["response_pending"])

    async def test_original_response_deadline_cannot_extend_when_load_remains_high(self):
        await self.tick(0)
        await self.tick(8.213, current=7.079)
        stopped = await self.tick(10)
        self.assertEqual((stopped.mode, stopped.current_p1), ("normal_shedding_stop", 0))
        self.assertIsNone(stopped.nominal_sync_deadline_ts)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])

    async def test_already_ordered_response_before_original_deadline_needs_no_sync_grace(self):
        await self.tick(0)
        await self.tick(3, current=7.079)
        # More than five seconds after the EV response, but still inside the
        # original ten-second response window, this pair is already ordered.
        complete = await self.tick(9, grid=18.6)
        self.assertEqual((complete.mode, complete.current_p1), ("normal_shedding_complete", 7))
        self.assertIsNone(complete.nominal_sync_deadline_ts)
        self.assert_only_existing_offer()

    async def test_expired_unused_sync_candidate_does_not_shorten_original_response_window(self):
        await self.tick(0)
        await self.tick(3, current=7.079)
        waiting = await self.tick(8.6, grid=18.6, grid_at=8.5, current=7.079)
        self.assertEqual((waiting.mode, waiting.current_p1), ("normal_shedding", 7))
        self.assertEqual(waiting.nominal_response_deadline_ts, self.base+10)
        self.assertIsNone(waiting.nominal_sync_deadline_ts)
        self.assert_only_existing_offer()
        complete = await self.tick(9, grid=18.6)
        self.assertEqual((complete.mode, complete.current_p1), ("normal_shedding_complete", 7))
        self.assert_only_existing_offer()

    async def test_sync_timer_is_terminal_without_a_later_grid_frame(self):
        await self.enter_sync()
        await self.tick(10)
        await self.fire_control_timer()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertEqual(self.controller.last_decision.mode, "normal_shedding_stop")
        self.assertIn("sync_timeout", self.controller.last_decision.reason)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])

    async def test_raw_above_buffered_nominal_ceiling_terminates_sync_immediately(self):
        await self.enter_sync()
        stopped = await self.tick(10.1, grid=23.0001)
        self.assertEqual((stopped.mode, stopped.current_p1), ("normal_shedding_stop", 0))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertEqual(self.regulator.export_overload_state()["failure_reason"], "response_timeout")

    async def test_stream_fault_during_sync_still_dispatches_protective_zero(self):
        await self.enter_sync()
        self.adapter.telemetry_block_reason = "easee_stream_disconnected"
        stopped = await self.tick(10.1)
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertIn("easee_stream_disconnected", stopped.reason)

    async def test_checkpoint_latency_spends_sync_time_instead_of_renewing_it(self):
        await self.enter_sync()
        save = self.controller._budget_store.async_save

        async def delayed_save(state):
            result = await save(state)
            self.now += 2
            return result

        self.controller._budget_store.async_save = delayed_save
        await self.tick(10)
        self.controller._budget_store.async_save = save
        self.assertEqual(self.now, self.base+12)
        timer = min(self.active_timers(), key=lambda item: item.due)
        self.assertAlmostEqual(timer.due, self.base+13.213, places=5)
        self.assertAlmostEqual(timer.due-self.now, 1.213, places=5)
        self.assert_only_existing_offer()
        await self.fire_control_timer()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])

    async def test_pending_sync_restore_is_interrupted_without_new_positive_authority(self):
        await self.enter_sync()
        await self.tick(10)
        key = self.controller._budget_store.store.key
        saved = deepcopy(self.hass._test_store_disk[key]["regulator"])
        self.assertTrue(saved["normal_control"]["response_pending"])
        await self.controller.async_stop()
        self.now = self.base+10.5
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.regulator = self.controller.regulator
        self.bind_native_fixture(self.controller)
        await self.controller.async_start()
        restored = self.regulator.export_overload_state()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertIsNone(self.controller.last_decision.nominal_sync_deadline_ts)
        self.assertEqual(restored["failure_reason"], saved["failure_reason"])
        self.assertGreaterEqual(restored["used_seconds"], saved["used_seconds"])
        self.assertGreaterEqual(restored["normal_control"]["stops"], 1)


if __name__ == "__main__":
    unittest.main()
