"""A real lower ACK owns the delayed tail of the earlier normal offer.

Synthetic actual-controller traces; no household records or seeded authority.
Fresh checkpoint verification uses the public Store wrapper over the same
synthetic ledger, never a fabricated pending flag or accepted command.
"""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_expired_start_handoff_controller as lower
import test_1fuseloader_runtime as fixtures


class OrdinaryResponseOwnerControllerTests(unittest.IsolatedAsyncioTestCase):
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
    slow_first_checkpoint = ready.DisconnectedReadinessRuntimeTests.slow_first_checkpoint
    ready = ready.DisconnectedReadinessRuntimeTests.ready
    late_partial = lower.ExpiredStartHandoffControllerTests.late_partial
    expire = lower.ExpiredStartHandoffControllerTests.expire
    assert_lower_ack = lower.ExpiredStartHandoffControllerTests.assert_lower_ack

    async def hold(self):
        r, cap, ramp_deadline = await self.late_partial()
        await self.expire(ramp_deadline)
        self.assert_lower_ack(r, cap)
        accepted = r.last_set_limit_ts
        self.now = ramp_deadline+1.
        self.grid(23.2, ramp_deadline+.9)
        await self.tick()
        self.assert_hold(r, accepted)
        return r, cap, ramp_deadline, accepted

    def assert_hold(self, r, accepted, deadline=217.):
        decision = self.controller.last_decision
        self.assertEqual(decision.mode, "ordinary_response_hold", decision)
        self.assertEqual(decision.current_p1, 7)
        self.assertEqual((decision.command, decision.service, decision.should_call_service),
            (None, None, False))
        self.assertAlmostEqual(decision.ordinary_response_deadline_ts, deadline, places=6)
        self.assertIsNone(decision.nominal_response_deadline_ts)
        self.assertEqual(r.last_target, (7, 0, 0, 60))
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [15, 7])
        self.assertEqual(r._ordinary_lower.previous, 15)
        self.assertEqual(r.export_overload_state()["used_seconds"], 0)

    def fresh_checkpoint_verification(self):
        # The controller already loaded the real synthetic ledger and has a
        # real7 A ACK. A new public verification gate knows no durable hash;
        # it must perform actual write/readback before granting NEW authority.
        previous = self.controller._budget_store
        self.controller._budget_store = type(previous)(previous.store,
            clock=previous.clock, read_back=previous.store.async_load)
        self.assertTrue(self.controller._budget_loaded)
        self.assertFalse(self.controller._budget_store.state_is_durable(
            self.controller.regulator.export_overload_state()))

    async def test_event_churn_cannot_move_birth_clock_and_absolute_watchdog_expires(self):
        r, _, _, accepted = await self.hold()
        for at in (200., 210., 216.99):
            self.publish(at, "charging", 23.2, 15.2,
                receipt=at-.3, grid_at=at-.4)
            await self.tick()
            self.assert_hold(r, accepted)
        timer = next(item for item in self.active_timers() if abs(item.due-217.) < 1e-6)
        self.now = timer.due
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once_with(None)
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0, self.controller.last_decision)
        self.assertIsNone(self.controller.last_decision.ordinary_response_deadline_ts)

    async def test_genuine_post_ack_minimum_and_later_grid_retire_old_high_before_new_rises(self):
        r, _, ramp, accepted = await self.hold()
        self.publish(ramp+1.4, "charging", 23.2, 15.2,
            receipt=ramp+1.1, grid_at=ramp+.9)
        await self.tick()
        self.assert_hold(r, accepted)
        self.publish(ramp+3., "charging", 23.2, 7.1,
            receipt=ramp+2.7, grid_at=ramp+2.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "ordinary_grid_wait")
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [15, 7])
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts,
            ramp+32.7, places=6)
        self.now = ramp+4.
        self.grid(15.2)
        await self.tick()
        self.assertIsNone(r._ordinary_lower)
        self.now = ramp+6.
        self.grid(15.2)
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        # Another meaningful changed native draw and later grid release the
        # new14 A rise envelope under its existing response-proof contract.
        self.publish(ramp+12., "charging", 11., 8.,
            receipt=ramp+11.7, grid_at=ramp+12.)
        await self.tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [15, 7, 14, 16])
        self.assertEqual((self.controller.last_decision.command,
            self.controller.last_decision.should_call_service), ("set_limit", True))
        self.now = ramp+17.
        self.grid(11.)
        await self.tick()
        self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [15, 7, 14, 16])

    async def test_lower_observation_uses_actual_new_lower_lease_not_old_high_lease(self):
        self.configure(enabled=False)
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 7.5, .02, receipt=None, changed=90.)
            await self.tick()
        r = self.controller.regulator
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 15])
        self.adapter.calls.pop(0)
        old_expiry = r.last_set_limit_ts+r.last_target[3]
        self.publish(190., "charging", 11., .02, receipt=None, grid_at=190.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, 215.)
        self.publish(214.8, "charging", 11., 6.4, receipt=214.7, grid_at=214.5)
        await self.tick()
        self.now = 215.1
        await self.tick()
        self.assert_lower_ack(r, 15)
        self.assertEqual(old_expiry, 220.)
        accepted = r.last_set_limit_ts
        self.now = 230.
        self.grid(23.2, 229.9)
        await self.tick()
        self.assert_hold(r, accepted, deadline=239.5)

    async def test_runtime_blocks_forged_limit_or_resume_during_owned_wait(self):
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                if command == "resume":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r, _, _, accepted = await self.hold()
                evaluate = r.evaluate

                def forged(inp):
                    decision = evaluate(inp)
                    if decision.mode == "ordinary_response_hold":
                        return replace(decision, command=command, should_call_service=True,
                            service="easee.synthetic_forbidden_positive")
                    return decision

                r.evaluate = forged
                self.now = 201.
                self.grid(23.2)
                await self.tick()
                self.assert_hold(r, accepted)

    async def test_real_pending_reverification_retains_only_existing_bound_owner(self):
        r, _, _, accepted = await self.hold()
        self.fresh_checkpoint_verification()
        self.slow_first_checkpoint()
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assert_hold(r, accepted)
        await self.controller._budget_store.async_wait_pending()

    async def test_pending_reverification_rechecks_latest_future_clock(self):
        r, _, _, _ = await self.hold()
        self.fresh_checkpoint_verification()
        self.slow_first_checkpoint()

        async def changed_source():
            await asyncio.sleep(.05)
            self.native.received_at = self.wall(self.now+6.)
            self.native.observation_report_times = ((182, self.wall(self.now+6.)),
                (183, self.wall(self.now+6.)))

        changed = asyncio.create_task(changed_source())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        await self.controller._budget_store.async_wait_pending()

    async def test_pending_reverification_can_retain_old_cap_when_later_normal_proposes_more(self):
        r, _, ramp, accepted = await self.hold()
        self.fresh_checkpoint_verification()
        self.slow_first_checkpoint()

        async def coherent_pair():
            await asyncio.sleep(.05)
            self.publish(ramp+3., "charging", 15.2, 7.1,
                receipt=ramp+2.7, grid_at=ramp+3.)

        changed = asyncio.create_task(coherent_pair())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [15, 7])
        await self.controller._budget_store.async_wait_pending()

    async def test_source_binding_hard_peak_and_expired_lease_override_wait(self):
        for problem in ("source", "binding", "peak", "lease"):
            with self.subTest(problem=problem):
                if problem != "source":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r, _, _, _ = await self.hold()
                if problem == "source":
                    self.native.error = "native_sdk_current_invalid"
                elif problem == "binding":
                    self.controller._configured_control_identity.return_value = "other-source"
                elif problem == "peak":
                    self.grid(38.01)
                else:
                    self.now = r.last_set_limit_ts+r.last_target[3]
                    self.grid(23.2)
                await self.tick()
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)
                self.assertIsNone(self.controller.last_decision.ordinary_response_deadline_ts)

    async def test_actual_above_nominal_grid_yields_to_existing_profile(self):
        r, _, _, _ = await self.hold()
        self.now += .5
        self.grid(25.1)
        await self.tick()
        self.assertNotEqual(self.controller.last_decision.mode, "ordinary_response_hold")
        self.assertIsNone(self.controller.last_decision.ordinary_response_deadline_ts)

    async def test_restoring_response_wait_preserves_interruption_not_fresh_time(self):
        r, _, _, _ = await self.hold()
        self.assertTrue(r.export_overload_state()["normal_control"]["response_pending"])
        await self.controller.async_stop()
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        self.adapter.calls.clear()
        self.now += .5
        self.grid(23.2)
        await self.tick()
        self.assertIsNone(self.controller.last_decision.ordinary_response_deadline_ts)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertEqual(self.controller.regulator.export_overload_state()["normal_control"]["response_failure"],
            "interrupted_normal_response")
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)


if __name__ == "__main__":
    unittest.main()
