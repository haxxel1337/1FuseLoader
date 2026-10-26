"""Expired startup observation hands off only through an actual lower ACK.

Public synthetic controller traces, not household-history replay. All accepted
offers are created at the real controller's fixture adapter boundary; source
proof, positive owners and response floors are never privately seeded.
"""
import asyncio
import unittest

import test_1fuseloader_disconnected_readiness_runtime as ready


class ExpiredStartHandoffControllerTests(unittest.IsolatedAsyncioTestCase):
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

    async def late_partial(self, *, prearmed=False):
        if prearmed:
            r = await self.ready(raw=5.5, current=0.)
            self.publish(110., "awaiting_start", 5.5, .02,
                receipt=109.7, changed=109.5)
            await self.tick()
            first, cap = 112., 16
        else:
            self.configure(enabled=False)
            for at in (100., 160.):
                self.publish(at, "awaiting_start", 7.5, .02,
                    receipt=None, changed=90.)
                await self.tick()
            r = self.controller.regulator
            self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 15])
            self.adapter.calls.pop(0)  # Exclude the explicit cold-start guard.
            first, cap = 170., 15
        self.assertEqual(r.last_target[0], cap)
        accepted = r.last_set_limit_ts
        self.publish(first, "charging", 11., .02,
            receipt=109.7 if prearmed else None,
            grid_at=first-1., changed=first-1.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "positive_ramp_hold")
        deadline = self.controller.last_decision.positive_response_deadline_ts
        self.assertAlmostEqual(deadline, first+24.)
        self.publish(deadline-.8, "charging", 11., 6.4,
            receipt=deadline-1., grid_at=deadline-2.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, cap)
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, deadline)
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap])
        return r, cap, deadline

    async def expire(self, deadline):
        self.now = deadline+.1
        await self.tick()

    def assert_lower_ack(self, r, cap):
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7],
            self.controller.last_decision)
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 7)
        self.assertIsNone(self.controller.last_decision.nominal_response_deadline_ts)
        self.assertEqual(r._ordinary_lower.previous, cap)
        self.assertIsNotNone(r._ordinary_lower.accepted_at)
        self.assertIsNone(r._ordinary_lower.first_response)
        self.assertIsNone(r._minimum_confirmation)
        state = r.export_overload_state()
        self.assertEqual(state["used_seconds"], 0)
        self.assertEqual(state["automatic_cycle"]["state"], "ready")
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0)
        self.assertNotIn("trial_epoch", state)

    async def test_cold_grid_first_expiry_uses_real_seven_ack_then_genuine_response_and_grid(self):
        r, cap, deadline = await self.late_partial()
        await self.expire(deadline)
        self.assert_lower_ack(r, cap)
        lower_ack = r.last_set_limit_ts
        # The same pre-decrease6.4 A frame is not a response to the new7 A.
        self.now = deadline+1.
        self.grid(11., deadline+.5)
        await self.tick()
        self.assertEqual(r.last_target[0], 7)
        self.assertIsNone(r._ordinary_lower.first_response)
        self.assertIsNone(r._minimum_confirmation)
        self.assertEqual(r._ordinary_lower.previous, cap)
        self.assertEqual(r.last_set_limit_ts, lower_ack)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7])
        # An actual later native event arms the observation phase. Its older
        # grid cannot retire higher authority or authorize a rise.
        self.publish(deadline+2., "charging", 11., 6.9,
            receipt=deadline+1.7, grid_at=deadline+1.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_grid_wait")
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7])
        self.assertEqual(r._ordinary_lower.previous, cap)
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts,
            deadline+31.7)
        self.now = deadline+3.
        self.grid(11.2)
        await self.tick()
        self.assertIsNone(r._ordinary_lower)
        self.now = deadline+4.
        self.grid(11.2)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertEqual(r.last_target[0], 7)  # Existing action spacing still applies.
        self.now = deadline+6.
        self.grid(11.2)
        await self.tick()
        self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_acknowledged_ready_start_uses_same_bounded_expiry_handoff(self):
        r, cap, deadline = await self.late_partial(prearmed=True)
        await self.expire(deadline)
        self.assert_lower_ack(r, cap)

    async def test_post_dispatch_event_during_http_can_qualify_only_after_actual_ack(self):
        r, cap, deadline = await self.late_partial()
        self.adapter.gate = asyncio.Event()
        self.adapter.entered.clear()
        self.now = deadline+.1
        task = asyncio.create_task(self.tick())
        await asyncio.wait_for(self.adapter.entered.wait(), 2.)
        try:
            self.assertEqual(self.adapter.calls[-1].current_p1, 7)
            self.publish(deadline+.4, "charging", 11., 6.8,
                receipt=deadline+.2, grid_at=deadline-.5)
            self.assertEqual(r.last_target[0], cap)
        finally:
            self.adapter.gate.set()
            await task
        self.assert_lower_ack(r, cap)
        self.now = deadline+1.
        self.grid(11.)
        await self.tick()
        self.assertEqual(r.last_target[0], 7)
        # The pre-ACK tick above had no accepted7 A permission. Once HTTP
        # succeeds, the genuine post-dispatch frame plus later grid can retire
        # that observation without asking for another changed current event.
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._minimum_confirmation)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7])

    async def test_failed_or_blocked_lower_never_creates_an_accepted_seven_offer(self):
        for problem in ("failure", "blocked"):
            with self.subTest(problem=problem):
                if problem == "blocked":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r, cap, deadline = await self.late_partial()
                accepted = dict(self.controller.last_accepted_limit)
                if problem == "failure":
                    self.adapter.failure = OSError("synthetic adapter failure")
                else:
                    self.adapter.control_block_reason = "synthetic unsupported control"
                await self.expire(deadline)
                self.assertEqual(r.last_target[0], cap)
                self.assertEqual(self.controller.last_accepted_limit, accepted)
                self.assertEqual(self.controller.last_service_result,
                    "error" if problem == "failure" else "blocked")
                if r._ordinary_lower is not None:
                    self.assertIsNone(r._ordinary_lower.accepted_at)
                    self.assertIsNone(r._ordinary_lower.first_response)
                self.assertIsNone(r._minimum_confirmation)

    async def test_unsafe_raw_expired_lease_source_fault_and_rebind_cannot_use_handoff(self):
        for problem in ("raw", "lease", "source", "binding"):
            with self.subTest(problem=problem):
                if problem != "raw":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r, cap, deadline = await self.late_partial()
                self.now = deadline+.1
                if problem == "raw":
                    self.grid(20.)
                elif problem == "lease":
                    self.now = r.last_set_limit_ts+r.last_target[3]+.1
                    self.grid(11.)
                elif problem == "source":
                    self.native.error = "native_sdk_current_invalid"
                else:
                    self.controller._configured_control_identity.return_value = "other-source"
                await self.tick()
                self.assertEqual(self.adapter.calls[-1].current_p1, 0,
                    self.controller.last_decision)
                self.assertFalse(any(d.current_p1 == 7 for d in self.adapter.calls))
                self.assertIsNone(r._ordinary_lower)


if __name__ == "__main__":
    unittest.main()
