"""Managed restart through the controller with independent synthetic sources.

The fixture runs the production controller, policy, delivery boundary, and
Store. No running Home Assistant, charger or household data is used.
"""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

import test_1fuseloader_b50_shutdown_handoff as handoff


class ManagedRestartTests(handoff.B50ShutdownHandoffTests):
    async def begin_managed(self, restart, *, stale_readback=None):
        self.bridge_reply = False
        dispatched = asyncio.Event()
        execute = self.adapter.async_execute_with_dispatch

        async def record_native_limit(decision, on_dispatch):
            previous_receipts = self.adapter.reported_limit_receipts
            receipt = await execute(decision, on_dispatch)
            self.adapter.reported_limits = (
                decision.current_p1, decision.current_p2, decision.current_p3
            )
            if decision.current_p1 == 7:
                if decision.ttl_seconds == 300:
                    # A same-value lifetime change need not publish another 111.
                    self.adapter.reported_limit_receipts = previous_receipts
                elif stale_readback is not None:
                    self.adapter.reported_limit_receipts = (stale_readback, None, None)
                dispatched.set()
            return receipt

        self.adapter.async_execute_with_dispatch = record_native_limit
        task = asyncio.create_task(self.controller.async_restart_with_charging(restart))
        dispatch_wait = asyncio.create_task(dispatched.wait())
        try:
            # Source-driven refresh is mocked in the common fixture. Drive
            # the next real controller tick with the next independent frame.
            await asyncio.sleep(0)
            at = self.now + 1.
            self.independent_grid(at, 20., at)
            await self.controller.async_tick()
            done, _ = await asyncio.wait(
                (task, dispatch_wait), timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                self.fail("Managed restart finished before the minimum offer")
            self.assertIn(dispatch_wait, done, "Managed restart never offered 7 A")
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            dispatch_wait.cancel()
            await asyncio.gather(dispatch_wait, return_exceptions=True)
        return task

    async def test_delayed_physical_proof_restarts_once_and_boot_takes_over(self):
        regulator = await self.healthy_session()
        before = deepcopy(regulator.export_overload_state())

        async def restart():
            self.assertFalse(self.controller._stopped)
            for key, value in self.hass._test_store_disk.items():
                if "restart" in key:
                    self.assertNotEqual(value["state"], "acknowledged", key)
            previous_calls = list(self.adapter.calls)
            await self.stage_one_shutdown()
            self.assertEqual(
                self.adapter.calls, previous_calls,
                "Core shutdown must not repeat the verified minimum offer",
            )
            self.assertTrue(self.controller._stopped)

        restart_action = AsyncMock(side_effect=restart)
        task = await self.begin_managed(restart_action)
        started_at = self.dispatches[-1]["at"]
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 60)])
        try:
            for offset in (5, 10, 15, 19):
                self.independent_grid(started_at + offset, 20., started_at + offset - .1)
                await asyncio.wait_for(self.controller.async_tick(), .6)
                await asyncio.sleep(.06)
            restart_action.assert_not_awaited()
            self.assertFalse(self.controller._stopped)
            self.assertTrue(all(d.current_p1 == 7 and d.ttl_seconds == 60 for d in self.adapter.calls))
            self.native_only(started_at + 20., 7.1)
            await asyncio.sleep(.06)
            restart_action.assert_not_awaited()
            self.assertFalse(any(d.ttl_seconds == 300 for d in self.adapter.calls))
            self.independent_grid(started_at + 21., 11., started_at + 20.5)
            await self.finish_or_cancel(task)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        restart_action.assert_awaited_once()
        self.assertEqual([d.current_p1 for d in self.adapter.calls if d.ttl_seconds == 300], [7])
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker["state"], "acknowledged")
        self.assertGreater(marker["physical_response_wall"] - self.wall(started_at).timestamp(), 16.)
        self.assertLessEqual(marker["expires_wall"] - marker["dispatch_wall"], 300.)
        self.assertGreater(marker["grid_response_wall"], marker["physical_response_wall"])

        self.new_boot(downtime=25.)
        await self.controller.async_start()
        claimed = self.persisted_bridge()[1]
        self.assertEqual(claimed["state"], "claimed")
        self.assertEqual(claimed["expires_wall"], marker["expires_wall"])
        self.assertFalse(self.adapter.calls)
        for at in range(5, 201, 5):
            self.report(float(at), "charging", 11., 7., at-.2)
            await self.controller.async_tick()
            self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
            if self.adapter.calls:
                break
        self.assertTrue(self.adapter.calls, "Normal control must take over before the original expiry")
        self.assertTrue(all(d.ttl_seconds == 60 for d in self.adapter.calls))
        after = self.controller.regulator.export_overload_state()
        for key in ("used_seconds", "failure_reason"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["automatic_cycle"]["cycle_id"], before["automatic_cycle"]["cycle_id"])

    async def test_critical_grid_during_managed_preflight_aborts_restart(self):
        await self.healthy_session()
        restart_action = AsyncMock()
        task = await self.begin_managed(restart_action)
        try:
            at = self.now + 1.
            self.independent_grid(at, 39., at)
            await asyncio.wait_for(self.controller.async_tick(), .6)
            with self.assertRaises(self.bridge_refused):
                await self.finish_or_cancel(task)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        restart_action.assert_not_awaited()
        self.assertFalse(self.controller._stopped)
        self.assertTrue(any(d.current_p1 == 0 for d in self.adapter.calls),
                        "A real hard limit still requires protection")
        self.assertFalse(self.controller._restart_bridge.armed)

    async def test_missing_physical_proof_refuses_restart_without_a_zero(self):
        regulator = await self.healthy_session()
        restart_action = AsyncMock()
        with self.managed_preflight_timeout(.2):
            task = await self.begin_managed(restart_action)
            with self.assertRaises(self.bridge_refused):
                await self.finish_or_cancel(task)
        restart_action.assert_not_awaited()
        self.assertFalse(self.controller._stopped)
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertFalse(any(d.current_p1 == 0 or d.ttl_seconds == 300 for d in self.adapter.calls))
        at = self.now + 5.
        self.report(at, "charging", 11., 7., at-.2)
        await self.controller.async_tick()
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertIsNone(regulator._get_meter_envelope_policy().operational_fault)

    async def test_old_matching_readback_does_not_extend_the_minimum_offer(self):
        await self.healthy_session()
        restart_action = AsyncMock()
        with self.managed_preflight_timeout(.3):
            task = await self.begin_managed(restart_action, stale_readback=self.now - 1.)
            try:
                at = self.now + 1.
                self.native_only(at, 7.1)
                await asyncio.sleep(.06)
                self.independent_grid(at + 1., 11., at + .5)
                with self.assertRaises(self.bridge_refused):
                    await self.finish_or_cancel(task)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        restart_action.assert_not_awaited()
        self.assertFalse(self.controller._stopped)
        self.assertFalse(any(d.current_p1 == 0 or d.ttl_seconds == 300 for d in self.adapter.calls))

    async def test_existing_measured_total_needs_headroom_for_the_whole_offer(self):
        await self.healthy_session()
        restart_action = AsyncMock()
        with self.managed_preflight_timeout(.3):
            task = await self.begin_managed(restart_action)
            at = self.now + 1.
            self.native_only(at, 4.)
            self.independent_grid(at + 1., 22.5, at + .5)
            with self.assertRaises(self.bridge_refused):
                await self.finish_or_cancel(task)
        restart_action.assert_not_awaited()
        self.assertFalse(self.controller._stopped)
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 60)])

    async def test_rejected_core_request_leaves_no_claimable_marker(self):
        await self.healthy_session()
        restart_action = AsyncMock(side_effect=RuntimeError("synthetic Core restart refused"))
        task = await self.begin_managed(restart_action)
        try:
            at = self.now + 1.
            self.native_only(at, 7.1)
            self.independent_grid(at + 1., 11., at + .5)
            with self.assertRaises(self.bridge_refused):
                await self.finish_or_cancel(task)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.controller._restart_bridge._background_save
        restart_action.assert_awaited_once()
        self.assertFalse(self.controller._stopped)
        self.assertEqual(self.persisted_bridge()[1]["state"], "aborted")
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 60), (7, 300)])
        self.assertFalse(self.controller._protective_zero_pending)

    async def test_direct_shutdown_does_not_create_a_charging_handoff(self):
        await self.healthy_session()
        await self.stage_one_shutdown()
        self.assertFalse(self.adapter.calls)
        self.assertFalse(any("restart" in key for key in self.hass._test_store_disk))
        await self.assert_quiescent()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        ManagedRestartTests(name) for name in ManagedRestartTests.__dict__ if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()
