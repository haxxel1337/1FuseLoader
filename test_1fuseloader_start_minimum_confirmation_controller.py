"""A timely response to an already accepted startup minimum is not a new ACK.

All sources, clocks and storage are synthetic. Actual controller startup and
fixture adapter acknowledgements establish authority; no owner/proof seeds or
household time-series are used. Tests never connect to Home Assistant.
"""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_expired_start_handoff_controller as lower
import test_1fuseloader_runtime as fixtures


class StartMinimumConfirmationControllerTests(unittest.IsolatedAsyncioTestCase):
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

    async def accepted_minimum(self, first_grid=165., first_status="charging"):
        self.configure(enabled=False)
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 16., .02,
                receipt=None, changed=90.)
            await self.tick()
        r = self.controller.regulator
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 7])
        self.assertEqual(r.last_set_limit_ts, 160.)
        self.assertEqual(r.last_target, (7, 0, 0, 60))
        self.adapter.calls.clear()
        if first_grid is None:
            return r
        self.publish(first_grid, first_status, 22., .02, receipt=None,
            grid_at=first_grid, changed=first_grid-.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "positive_ramp_hold")
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts,
            min(first_grid+25., 220.))
        self.assertEqual(self.adapter.calls, [])
        return r

    async def minimum_wait(self):
        r = await self.accepted_minimum()
        self.publish(190., "charging", 22., 6.4,
            receipt=189.8, grid_at=189.5)
        await self.tick()
        self.assert_wait(r)
        return r

    def assert_wait(self, r, deadline=219.8):
        decision = self.controller.last_decision
        self.assertEqual(decision.current_p1, 7, decision)
        self.assertEqual((decision.command, decision.service, decision.should_call_service),
            (None, None, False), decision)
        self.assertIsNotNone(decision.grid_confirmation_deadline_ts, decision)
        self.assertAlmostEqual(decision.grid_confirmation_deadline_ts, deadline, places=6)
        self.assertIsNone(decision.nominal_response_deadline_ts)
        self.assertEqual(r.last_set_limit_ts, 160.)
        self.assertEqual(r.last_target, (7, 0, 0, 60))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.export_overload_state()["used_seconds"], 0)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_used_seconds"], 0)

    async def test_timely_minimum_uses_existing_ack_and_later_grid_completes(self):
        r = await self.minimum_wait()
        self.now = 191.
        self.grid(22., 191.)
        await self.tick()
        self.assertGreater(self.controller.last_decision.current_p1, 0)
        self.assertIsNone(r._minimum_confirmation)
        # After genuine completion, ordinary same-cap TTL renewal is allowed.
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls))

    async def test_genuine_minimum_before_charging_status_keeps_existing_fixed_phase(self):
        r = await self.accepted_minimum(first_status="awaiting_start")
        # Current and status are independently published. A real post-ACK
        # current event need not wait for the charging label to catch up.
        self.publish(190., "awaiting_start", 22., 6.4,
            receipt=189.8, grid_at=189.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "startup_grid_wait")
        self.assert_wait(r)
        self.publish(195., "charging", 22., 6.4,
            receipt=189.8, grid_at=189.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "startup_grid_wait")
        self.assert_wait(r)

    async def test_expired_high_offer_handoff_survives_the_full_delayed_high_then_lower_wave(self):
        r, cap, deadline = await lower.ExpiredStartHandoffControllerTests.late_partial(self)
        await lower.ExpiredStartHandoffControllerTests.expire(self, deadline)
        lower.ExpiredStartHandoffControllerTests.assert_lower_ack(self, r, cap)
        accepted = r.last_set_limit_ts
        # The grid sees the tail of the previously accepted high ramp before
        # its conductor event arrives. Neither frame proves the new7 A yet.
        self.now = deadline+1.
        self.grid(23.2, deadline+.9)
        await self.tick()
        self.assertEqual(r.last_target[0], 7, self.controller.last_decision)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7])
        self.publish(deadline+1.4, "charging", 23.2, 15.2,
            receipt=deadline+1.1, grid_at=deadline+.9)
        await self.tick()
        self.assertEqual(r.last_target[0], 7, self.controller.last_decision)
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap, 7])
        self.publish(deadline+3., "charging", 23.2, 7.1,
            receipt=deadline+2.7, grid_at=deadline+2.)
        await self.tick()
        self.assertEqual(r.last_target[0], 7, self.controller.last_decision)
        self.now = deadline+4.
        self.grid(15.2)
        await self.tick()
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))
        self.assertIsNone(r._ordinary_lower)

    async def test_identical_native_events_cannot_move_first_response_deadline(self):
        r = await self.minimum_wait()
        for at in (195., 205., 219.7):
            self.publish(at, "charging", 22., 6.4,
                receipt=at-.3, grid_at=189.5)
            await self.tick()
            self.assert_wait(r)

    async def test_absolute_watchdog_expires_without_new_report_or_renewal(self):
        r = await self.minimum_wait()
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        timer = next(item for item in self.active_timers() if abs(item.due-deadline) < 1e-6)
        self.now = deadline
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once_with(None)
        self.assertEqual(self.adapter.calls, [])
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_late_coherent_grid_cannot_retire_expired_phase_or_renew_same_cap(self):
        r = await self.minimum_wait()
        self.now = 219.9
        self.grid(22.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0],
            self.controller.last_decision)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertEqual(r.last_target[0], 0)

    async def test_higher_configured_startup_cap_cannot_get_minimum_observation_permission(self):
        for startup in (10, 16):
            with self.subTest(startup=startup):
                if startup == 16:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.configure(enabled=False)
                options = dict(automatic_overload_cycles=False, startup_charge_amps=startup)
                self.entry.options.update(options)
                self.controller.regulator.update_settings(replace(self.controller.settings, **options))
                raw = 23.-startup
                for at in (100., 160.):
                    self.publish(at, "awaiting_start", raw, .02, receipt=None, changed=90.)
                    await self.tick()
                self.assertEqual(self.controller.regulator.last_target[0], startup)
                self.adapter.calls.clear()
                self.publish(165., "charging", 22., .02, receipt=None,
                    grid_at=165., changed=164.5)
                await self.tick()
                self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)
                await self.tick()
                self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        with self.subTest(startup=6, accepted=7):
            await self.asyncTearDown()
            await self.asyncSetUp()
            self.configure()
            options = dict(automatic_overload_cycles=False, startup_charge_amps=6)
            self.entry.options.update(options)
            self.controller.regulator.update_settings(replace(self.controller.settings, **options))
            self.publish(100., "disconnected", 16., .02, receipt=90.)
            await self.tick()
            self.assertEqual(self.controller.regulator.last_target[0], 7)
            self.publish(110., "awaiting_start", 16., .02, receipt=109.7, changed=109.5)
            await self.tick()
            self.publish(112., "charging", 22., .02, receipt=109.7, grid_at=112.)
            await self.tick()
            self.publish(137., "charging", 22., 6.4, receipt=136.8, grid_at=136.5)
            await self.tick()
            self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
            self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_shared_phase_is_clipped_by_original_accepted_lease(self):
        r = await self.accepted_minimum(first_grid=195.)
        self.publish(219.9, "charging", 22., 6.4,
            receipt=219.8, grid_at=219.5)
        await self.tick()
        self.assert_wait(r, deadline=220.)
        self.now = 220.
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_same_cap_http_renewal_does_not_move_first_mismatch_or_response_clock(self):
        r = await ready.DisconnectedReadinessRuntimeTests.ready(self, raw=16., current=0.)
        self.publish(110., "awaiting_start", 16., .02, receipt=109.7, changed=109.5)
        await self.tick()
        self.adapter.calls.clear()
        self.adapter.gate = asyncio.Event()
        self.adapter.entered.clear()
        self.publish(135., "awaiting_start", 16., .02, receipt=109.7, grid_at=134.)
        task = asyncio.create_task(self.tick())
        await asyncio.wait_for(self.adapter.entered.wait(), 2.)
        try:
            self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
            self.publish(135.4, "charging", 22., .02, receipt=109.7,
                grid_at=135.3, changed=135.2)
            fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.sleep(0)
            self.assertFalse(self.controller._dispatch_superseded)
        finally:
            self.adapter.gate.set()
            await task
            if self.controller._refresh_task is not None:
                await self.controller._refresh_task
        self.assertAlmostEqual(self.controller.last_decision.positive_response_deadline_ts, 160.3, places=6)
        self.assertEqual(r.last_set_limit_ts, 135.4)
        self.publish(160.3, "charging", 22., 6.4,
            receipt=160.1, grid_at=159.8)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts, 190.1, places=6)
        self.assertEqual(r.last_set_limit_ts, 135.4)

    async def test_runtime_never_executes_forged_renewal_or_resume_in_confirmation(self):
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                if command == "resume":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.minimum_wait()
                evaluate = r.evaluate

                def forged(inp):
                    decision = evaluate(inp)
                    if decision.grid_confirmation_deadline_ts is not None:
                        return replace(decision, command=command, should_call_service=True,
                            service="easee.synthetic_forbidden_positive")
                    return decision

                r.evaluate = forged
                self.now = 191.
                await self.tick()
                self.assert_wait(r)

    async def test_late_get_or_invalid_native_cannot_create_first_confirmation(self):
        for problem in ("late", "get", "native", "source", "peak"):
            with self.subTest(problem=problem):
                if problem != "late":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.accepted_minimum()
                self.publish(190.3, "charging", 22., 6.4,
                    receipt=190.1 if problem == "late" else 189.8, grid_at=189.5)
                if problem == "get":
                    self.publish(190.3, "charging", 22., 6.4,
                        receipt=None, grid_at=189.5)
                    self.install(self.snapshot(current=6.4, observed=189.8))
                elif problem == "native":
                    self.native.committed = False
                elif problem == "source":
                    self.native.error = "native_sdk_current_invalid"
                elif problem == "peak":
                    self.grid(38.01)
                await self.tick()
                self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
                self.assertEqual(self.adapter.calls[-1].current_p1, 0,
                    self.controller.last_decision)

    async def test_pending_real_checkpoint_can_only_retain_same_live_confirmation(self):
        r = await self.accepted_minimum()
        self.slow_first_checkpoint()
        self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.controller._budget_error, "overload_budget_save_pending")
        self.assert_wait(r)
        self.now = 191.
        await self.tick()
        self.assert_wait(r)
        await self.controller._budget_store.async_wait_pending()

    async def test_pending_checkpoint_rechecks_latest_source_after_await(self):
        r = await self.accepted_minimum()
        self.slow_first_checkpoint()
        self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)

        async def change_clock():
            await asyncio.sleep(.05)
            self.native.received_at = self.wall(196.)
            self.native.observation_report_times = ((182, self.wall(196.)), (183, self.wall(196.)))

        changed = asyncio.create_task(change_clock())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertFalse(self.controller._pending_checkpoint_allows_normal(
            self.controller.last_decision, self.controller._read_input()))
        await self.controller._budget_store.async_wait_pending()

    async def test_pending_checkpoint_later_normal_grid_does_not_create_zero(self):
        r = await self.accepted_minimum()
        self.slow_first_checkpoint()
        self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)

        async def later_grid():
            await asyncio.sleep(.05)
            self.now = 190.1
            self.grid(11.)

        changed = asyncio.create_task(later_grid())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r.last_set_limit_ts, 160.)
        await self.controller._budget_store.async_wait_pending()
        self.now = 191.
        await self.tick()
        self.assertIsNone(r._minimum_confirmation)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls))

    async def test_pending_checkpoint_late_normal_grid_cannot_extend_original_phase(self):
        await self.accepted_minimum()
        self.slow_first_checkpoint()
        self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)

        async def late_grid():
            await asyncio.sleep(.05)
            self.now = 219.9
            self.grid(22.)

        changed = asyncio.create_task(late_grid())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        await self.controller._budget_store.async_wait_pending()

    async def test_permanent_checkpoint_failure_still_withdraws_existing_minimum(self):
        r = await self.accepted_minimum()
        self.slow_first_checkpoint(fail=True)
        self.publish(190., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assert_wait(r)
        await self.controller._budget_store.async_wait_pending()
        self.now = 191.
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")

    async def test_restoring_pending_confirmation_is_interrupted_not_another_thirty_seconds(self):
        r = await self.minimum_wait()
        saved = r.export_overload_state()
        self.assertTrue(saved["normal_control"]["response_pending"])
        await self.controller.async_stop()
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        self.adapter.calls.clear()
        self.publish(191., "charging", 22., 6.4, receipt=189.8, grid_at=189.5)
        await self.tick()
        restored = self.controller.regulator
        self.assertIsNone(restored._minimum_confirmation)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertEqual(restored.export_overload_state()["normal_control"]["response_failure"],
            "interrupted_normal_response")
        self.assertTrue(restored._restart_required)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)


if __name__ == "__main__":
    unittest.main()
