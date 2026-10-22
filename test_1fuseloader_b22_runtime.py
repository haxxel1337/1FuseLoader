"""Accepted-rise ownership through controller persistence and dispatch.

Independent native grid/current events; no network, live HA or synthetic GET
is allowed to supply physical-response confirmation.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import test_1fuseloader_refresh_deadlines as helpers


class AcceptedRiseRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = helpers.RefreshDeadlineTests.update_entry
    wall = helpers.RefreshDeadlineTests.wall
    snapshot = helpers.RefreshDeadlineTests.snapshot
    schedule = helpers.RefreshDeadlineTests.schedule
    active_timers = helpers.RefreshDeadlineTests.active_timers

    async def asyncSetUp(self):
        await helpers.RefreshDeadlineTests.asyncSetUp(self)
        self.entry.options.update(overload_enabled=True, bounded_nominal_shedding=True,
                                  nominal_import_only_fallback=True)
        self.controller.settings.overload_enabled = True
        self.controller.settings.bounded_nominal_shedding = True
        self.controller.settings.nominal_import_only_fallback = True
        self.binding = ("selected-charger", "selected-grid", "L1")
        self.controller._configured_control_identity = lambda: self.binding
        self.controller._schedule_current_snapshot = Mock()
        self.controller._request_current_snapshot = Mock()
        self.regulator = self.controller.regulator
        self.regulator.import_overload_state(dict(
            version=1, used_seconds=2000.0, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))

    async def asyncTearDown(self):
        await helpers.RefreshDeadlineTests.asyncTearDown(self)

    async def tick(self, now, grid, current, *, grid_at=None, ev_at=None):
        self.now = now
        self.states["sensor.grid"].state = str(grid)
        self.states["sensor.grid"].last_reported = self.wall(now if grid_at is None else grid_at)
        self.states["sensor.car"].state = str(current)
        self.states["sensor.car"].last_reported = self.wall(now if ev_at is None else ev_at)
        self.states["sensor.status"].last_reported = self.wall()
        await self.controller.async_tick()
        return self.controller.last_decision

    async def accepted_rise(self):
        self.assertEqual((await self.tick(0, 23, 7)).current_p1, 7)
        self.assertEqual((await self.tick(10, 12.1, 7.022)).current_p1, 16)
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 16)
        self.assertEqual((await self.tick(40, 8.8, 7.022, ev_at=10)).current_p1, 16)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 16, 16])

    async def test_ev_first_partial_rise_and_later_grid_never_dispatch_lower_or_zero(self):
        await self.accepted_rise()
        first = await self.tick(60.8, 8.8, 13.909, grid_at=57)
        self.assertEqual((first.mode, first.current_p1), ("settling", 16))
        self.assertEqual(first.house_load_basis, "positive_response_anchor")
        second = await self.tick(62, 17.6, 13.909, ev_at=60.8)
        self.assertEqual(second.current_p1, 16)
        self.assertAlmostEqual(self.regulator._positive_alignment_used_seconds, 1.2)
        await self.tick(70, 17.6, 16.17, grid_at=62)
        complete = await self.tick(72, 19.7, 16.17, ev_at=70)
        self.assertEqual(complete.current_p1, 16)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls[1:]))
        disk = self.hass._test_store_disk[self.controller._budget_store.store.key]["regulator"]
        self.assertEqual((disk["used_seconds"], disk["failure_reason"]), (2000, "response_timeout"))

    async def test_stream_fault_preempts_accepted_rise_and_dispatches_zero(self):
        await self.accepted_rise()
        await self.tick(60.8, 8.8, 13.909, grid_at=57)
        self.adapter.telemetry_block_reason = "easee_stream_disconnected"
        stopped = await self.tick(61, 8.8, 13.909, grid_at=57, ev_at=60.8)
        self.assertEqual((stopped.current_p1, self.adapter.calls[-1].current_p1), (0, 0))
        self.assertIn("easee_stream_disconnected", stopped.reason)

    async def test_new_hard_peak_preempts_accepted_rise_during_checkpoint(self):
        await self.accepted_rise()
        original = self.controller._budget_store.async_save
        checkpoint_changes = []

        async def changed_load(state):
            saved = await original(state)
            checkpoint_changes.append(self.now)
            self.states["sensor.grid"].state = "38.1"
            self.states["sensor.grid"].last_reported = self.wall()
            return saved

        self.controller._budget_store.async_save = changed_load
        await self.tick(60.8, 8.8, 13.909, grid_at=57)
        self.controller._budget_store.async_save = original
        self.assertTrue(checkpoint_changes)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def low_grid_pending(self):
        self.assertEqual((await self.tick(0, 21.7, 16.2)).current_p1, 16)
        pending = await self.tick(10, 2.4, 16.2, ev_at=0)
        self.assertEqual((pending.mode, pending.current_p1), ("low_grid_alignment", 7))
        return pending

    async def test_raw_bound_retains_seven_after_alignment_without_zero_or_increase(self):
        await self.low_grid_pending()
        for then in (20, 21, 39, 40, 41):
            held = await self.tick(then, 2.4, 16.2, ev_at=0)
            self.assertEqual((held.mode, held.current_p1), ("low_grid_raw_hold", 7))
            self.assertIsNone(held.nominal_response_deadline_ts)
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls[1:]))
        # The unresolved larger offer is still part of every raw bound.
        stopped = await self.tick(42, 7.1, 16.2, ev_at=0)
        self.assertEqual(stopped.current_p1, 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_expired_old_dispatch_plan_can_retain_fresh_no_write_raw_hold(self):
        pending = await self.low_grid_pending()
        held = await self.tick(20, 2.4, 16.2, ev_at=0)
        self.assertEqual((held.mode, held.current_p1), ("low_grid_raw_hold", 7))
        self.assertFalse(held.should_call_service)
        calls = len(self.adapter.calls)
        old_plan = replace(pending, should_call_service=True)
        with patch.object(self.regulator, "evaluate", side_effect=[old_plan, held]):
            await self.controller.async_tick()
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(self.controller.last_decision.mode, "low_grid_raw_hold")
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertFalse(self.controller.last_decision.should_call_service)


if __name__ == "__main__":
    unittest.main()
