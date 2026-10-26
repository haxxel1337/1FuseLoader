"""Accepted Ready startup owns mixed ramp reports before ordinary fallback.

Public synthetic controller traces, not household-history replay. Ready and
renewal authority comes from actual fixture adapter acknowledgements. No
positive owner, source proof or response clock is privately seeded.
"""
import asyncio
import unittest

import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_runtime as runtime_fixtures


class ReadyOwnerPriorityControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = ready.DisconnectedReadinessRuntimeTests.update_entry
    wall = ready.DisconnectedReadinessRuntimeTests.wall
    schedule = ready.DisconnectedReadinessRuntimeTests.schedule
    active_timers = ready.DisconnectedReadinessRuntimeTests.active_timers
    asyncSetUp = ready.DisconnectedReadinessRuntimeTests.asyncSetUp
    asyncTearDown = ready.DisconnectedReadinessRuntimeTests.asyncTearDown
    set_native = ready.DisconnectedReadinessRuntimeTests.set_native
    snapshot = ready.DisconnectedReadinessRuntimeTests.snapshot
    install = ready.DisconnectedReadinessRuntimeTests.install
    native_vector = ready.DisconnectedReadinessRuntimeTests.native_vector
    grid = ready.DisconnectedReadinessRuntimeTests.grid
    configure = ready.DisconnectedReadinessRuntimeTests.configure
    publish = ready.DisconnectedReadinessRuntimeTests.publish
    tick = ready.DisconnectedReadinessRuntimeTests.tick
    ready = ready.DisconnectedReadinessRuntimeTests.ready

    async def connected(self):
        r = await self.ready(raw=5.5, current=0.)
        self.publish(110., "awaiting_start", 2.5, .16,
            receipt=109.7, grid_at=109., changed=109.5)
        await self.tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(r._positive_cap, 16)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.adapter.calls.clear()
        return r

    async def partial(self):
        r = await self.connected()
        self.publish(120., "charging", 2.5, 5.5,
            receipt=119.7, grid_at=119., changed=119.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16,
            self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "positive_ramp_hold")
        return r

    def assert_normal_history(self, r):
        state = r.export_overload_state()
        self.assertEqual(state["used_seconds"], 0)
        self.assertEqual(state["automatic_cycle"]["state"], "ready")
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0)
        self.assertIsNone(state["automatic_cycle"]["failure_reason"])
        self.assertNotIn("trial_epoch", state)

    async def finish_ramp(self, r, *, first, deadline, accepted):
        for at, value, receipt, grid, grid_at in (
                (first+5., 9.2, first+4.7, 8.8, first+4.),
                (first+10., 16.1, first+9.7, 18.6, first+9.)):
            self.publish(at, "charging", grid, value,
                receipt=receipt, grid_at=grid_at)
            await self.tick()
            self.assertEqual(self.controller.last_decision.current_p1, 16)
            self.assertEqual(self.adapter.calls, [])
            self.assertEqual(r.last_set_limit_ts, accepted)
            self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, deadline)
        self.now = first+10.5
        self.grid(18.6)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls))
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)
        self.assert_normal_history(r)

    async def test_ready_connection_partial_and_later_full_grid_never_request_seven_or_zero(self):
        r = await self.partial()
        deadline = self.controller.last_decision.positive_response_deadline_ts
        self.assertAlmostEqual(deadline, 144.7, places=6)
        await self.finish_ramp(r, first=120., deadline=deadline, accepted=100.)

    async def test_partial_arriving_during_same_sixteen_ttl_request_keeps_original_owner(self):
        r = await self.connected()
        self.adapter.gate = asyncio.Event()
        self.adapter.entered.clear()
        self.now = 135.
        self.grid(2.5, 134.)
        tick = asyncio.create_task(self.tick())
        await asyncio.wait_for(self.adapter.entered.wait(), 2.)
        try:
            self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
            self.publish(135.4, "charging", 2.5, 5.5,
                receipt=135.1, grid_at=134., changed=135.)
            runtime_fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.sleep(0)
            self.assertFalse(self.controller._dispatch_superseded)
        finally:
            self.adapter.gate.set()
            await tick
            if self.controller._refresh_task is not None:
                await self.controller._refresh_task
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertEqual(self.controller.last_decision.current_p1, 16,
            self.controller.last_decision)
        self.assertEqual(self.controller.last_decision.mode, "positive_ramp_hold")
        deadline = self.controller.last_decision.positive_response_deadline_ts
        self.assertAlmostEqual(deadline, 160.1, places=6)
        self.adapter.calls.clear()
        await self.finish_ramp(r, first=135.4, deadline=deadline, accepted=135.)

    async def test_hard_peak_source_future_and_rebind_still_override_ramp_owner(self):
        for fault in ("peak", "native", "future", "binding"):
            with self.subTest(fault=fault):
                if fault != "peak":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.partial()
                self.now = 121.
                self.grid(2.5)
                if fault == "peak":
                    self.grid(39.)
                elif fault == "native":
                    self.native.error = "native_sdk_current_invalid"
                elif fault == "future":
                    self.native.received_at = self.wall(127.)
                    self.native.observation_report_times = ((182, self.wall(127.)), (183, self.wall(127.)))
                else:
                    self.controller._configured_control_identity.return_value = "other-source"
                await self.tick()
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_expired_ready_lease_cannot_create_a_connected_ramp_owner(self):
        r = await self.ready(raw=5.5, current=16.)
        expiry = r.last_set_limit_ts+r.last_target[3]
        self.adapter.calls.clear()
        self.publish(expiry+.3, "charging", 21., 16., receipt=90., changed=expiry+.1)
        await self.tick()
        self.assertLess(self.controller.last_decision.current_p1, 16)
        self.assertFalse(any(d.current_p1 == 16 for d in self.adapter.calls))
        self.assertIsNone(r._ready_owner)

    async def test_no_unfinished_owner_means_ordinary_lower_offer_remains_available(self):
        r = await self.connected()
        self.publish(115., "charging", 18.6, 16.1,
            receipt=114.7, grid_at=115., changed=114.5)
        await self.tick()
        self.assertIsNone(r._positive_cap)
        self.adapter.calls.clear()
        self.publish(120., "charging", 2.5, 8.5, receipt=119.7, grid_at=119.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_lower_offer")
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assert_normal_history(r)


if __name__ == "__main__":
    unittest.main()
