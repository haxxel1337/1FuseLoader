"""Finite normal offers for a connected idle car; synthetic and offline.

A public Store checkpoint is restored, actual adapter requests reach their
dispatch boundary, and readback belongs to those requests. No private command
owner or uncertainty fields are seeded. Native zero remains an unchanged
event while fresh meter reports independently establish whole-offer headroom.
"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_1fuseloader_disconnected_readiness_runtime as fixtures


class ConnectedFullReadinessControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.DisconnectedReadinessRuntimeTests.update_entry
    wall = fixtures.DisconnectedReadinessRuntimeTests.wall
    schedule = fixtures.DisconnectedReadinessRuntimeTests.schedule
    active_timers = fixtures.DisconnectedReadinessRuntimeTests.active_timers
    set_native = fixtures.DisconnectedReadinessRuntimeTests.set_native
    snapshot = fixtures.DisconnectedReadinessRuntimeTests.snapshot
    install = fixtures.DisconnectedReadinessRuntimeTests.install
    native_vector = fixtures.DisconnectedReadinessRuntimeTests.native_vector
    grid = fixtures.DisconnectedReadinessRuntimeTests.grid
    configure = fixtures.DisconnectedReadinessRuntimeTests.configure
    publish = fixtures.DisconnectedReadinessRuntimeTests.publish
    tick = fixtures.DisconnectedReadinessRuntimeTests.tick

    async def asyncSetUp(self):
        await fixtures.DisconnectedReadinessRuntimeTests.asyncSetUp(self)
        self.maximum = 16
        self.configure()
        self.dispatch_failure = None
        self.dispatches = []
        self.circuit = SimpleNamespace(state="0", last_reported=self.wall(), attributes={
            "unit_of_measurement": "A", "state_dynamicCircuitCurrentP1": 0,
            "state_dynamicCircuitCurrentP2": 0, "state_dynamicCircuitCurrentP3": 0,
            "state_latestPulse": self.wall().isoformat()})
        self.states["sensor.circuit"] = self.circuit
        registry = SimpleNamespace(async_get=lambda _: None)
        self.registry_patch = patch.object(fixtures.fixtures.runtime, "er", SimpleNamespace(
            async_get=lambda _: registry,
            async_entries_for_device=lambda *_: [SimpleNamespace(
                platform="easee", disabled_by=None, entity_id="sensor.circuit")]))
        self.registry_patch.start()
        self.logger = fixtures.fixtures.runtime._LOGGER
        self.logger_was_disabled = self.logger.disabled
        self.logger.disabled = True
        self.adapter.async_execute_with_dispatch = self.execute
        self.adapter.reported_limit_receipts = (None, None, None)

    async def asyncTearDown(self):
        try:
            await fixtures.DisconnectedReadinessRuntimeTests.asyncTearDown(self)
        finally:
            self.logger.disabled = self.logger_was_disabled
            self.registry_patch.stop()

    async def execute(self, decision, on_dispatch):
        if on_dispatch() is not True:
            raise fixtures.fixtures.runtime.CommandNotSent("synthetic_dispatch_rejected")
        self.adapter.calls.append(decision)
        if self.dispatch_failure is not None:
            # The request has entered SDK; refusal is deliberately ambiguous.
            raise self.dispatch_failure
        self.dispatches.append(dict(at=self.now, cap=decision.current_p1,
            command=decision.command, ttl=decision.ttl_seconds))
        self.now += .01
        if decision.command == "set_limit":
            self.circuit.state = str(decision.current_p1)
            self.circuit.attributes.update(
                state_dynamicCircuitCurrentP1=decision.current_p1,
                state_dynamicCircuitCurrentP2=decision.current_p2,
                state_dynamicCircuitCurrentP3=decision.current_p3)
            self.circuit.last_reported = self.wall()
            self.adapter.reported_limit_receipts = (self.now,) * 3
        return SimpleNamespace(http_status=200, correlation_ids=())

    def report(self, at, status="awaiting_start", raw=3., current=0., receipt=99.):
        self.publish(at, status, raw, current, receipt=receipt)
        self.circuit.attributes["state_latestPulse"] = self.wall().isoformat()

    async def start(self, *, interrupted=True, stops=0, retry_wait=0., maximum=16):
        self.maximum = maximum
        if maximum != 16:
            self.configure(max_charge_amps=maximum)
        r = self.controller.regulator
        saved = r.export_overload_state()
        saved.update(used_seconds=3210., failure_reason="response_timeout")
        if interrupted or stops:
            saved["normal_control"] = dict(version=1,
                response_pending=interrupted, alignment_pending=False,
                cap=maximum if interrupted else 7, previous_offer=maximum,
                stops=stops, stop_recorded=False, restart=bool(stops),
                retry_wait_seconds=retry_wait,
                response_failure="positive_retry_submission_failed" if stops >= 3 else None,
                retry_submission_pending=False)
        await self.controller._budget_store.async_save(saved)
        self.report(100.)
        await self.tick()
        if stops >= 3:
            return r
        if interrupted:
            self.assertEqual(self.adapter.calls[0].current_p1, 0)
            for at in range(105, 186, 5):
                self.report(float(at))
                await self.tick()
                if r.last_target and r.last_target[0] == 7:
                    self.assertEqual([d.current_p1 for d in self.adapter.calls if d.current_p1 > 0], [7])
                    self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 1)
                    self.recovered_at = r.last_set_limit_ts
                    self.adapter.calls.clear()
                    return r
                self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))
            self.fail("The actual capped recovery was never acknowledged")
        self.recovered_at = self.now
        self.adapter.calls.clear()
        return r

    def assert_history(self):
        state = self.controller.regulator.export_overload_state()
        self.assertEqual(state["used_seconds"], 3210.)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0.)
        self.assertEqual(state["automatic_cycle"]["state"], "ready")

    def assert_finite_maximum_continuity(self):
        accepted = [row for row in self.dispatches
            if row["command"] == "set_limit" and row["cap"] == self.maximum]
        self.assertTrue(accepted)
        for previous, renewed in zip(accepted, accepted[1:]):
            self.assertLess(renewed["at"], previous["at"]+previous["ttl"],
                "An expired offer cannot be repaired retroactively by an equal new cap")
            self.assertLessEqual(renewed["at"]-previous["at"], previous["ttl"]/2+5.02)
        self.assertLess(self.now, accepted[-1]["at"]+accepted[-1]["ttl"])

    async def maximum_after_recovery(self, *, interrupted=True):
        r = await self.start(interrupted=interrupted, maximum=self.maximum)
        if interrupted:
            for offset in range(5, 46, 5):
                self.report(self.recovered_at+offset)
                await self.tick()
                if offset < 30:
                    self.assertEqual(r.last_target[0], 7)
            self.assertEqual(r.last_target[0], self.maximum, self.controller.last_decision)
        else:
            for at in range(105, 186, 5):
                if r.last_target and r.last_target[0] == self.maximum:
                    break
                self.report(float(at))
                await self.tick()
            self.assertEqual(r.last_target[0], self.maximum, self.controller.last_decision)
        self.assertEqual(self.circuit.attributes["state_dynamicCircuitCurrentP1"], self.maximum)
        self.assertTrue(all(d.command == "set_limit" and d.current_p2 == d.current_p3 == 0
            for d in self.adapter.calls))
        self.assert_history()
        return r

    async def drive_idle(self, end, *, raw=3., current=0., receipt=99., status="awaiting_start"):
        """Deliver each due watchdog/deadline as well as advancing meter frames."""
        sample = self.now+5.
        while True:
            timers = [t for t in self.active_timers() if t.due <= end]
            timer = min(timers, key=lambda t: t.due) if timers else None
            at = min(sample, timer.due if timer else float("inf"))
            if at > end:
                return
            self.now = max(self.now, at)
            if sample <= at:
                self.report(self.now, status, raw, current, receipt)
                sample += 5.
            if timer is not None and timer.due <= at:
                timer.cancelled = True
                await timer.callback(self.wall())
            await self.tick()
            if status == "awaiting_start" and current <= .5:
                self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)
                self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_recovered_full_car_prepares_configured_max_only_after_whole_raw_proof(self):
        for maximum in (12, 16):
            with self.subTest(maximum=maximum):
                if maximum != 12:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.maximum = maximum
                self.configure(max_charge_amps=maximum)
                await self.maximum_after_recovery()

    async def test_idle_offer_renews_without_ev_draw_or_an_idle_response_deadline(self):
        r = await self.maximum_after_recovery()
        native_clock = self.controller._read_input().charger_report_ts
        self.adapter.calls.clear()
        await self.drive_idle(self.now+220.)
        self.assertEqual(r.last_target[0], 16)
        self.assertTrue(self.adapter.calls)
        self.assertTrue(all(d.command == "set_limit" and d.current_p1 == 16 for d in self.adapter.calls))
        self.assertEqual(self.controller._read_input().charger_report_ts, native_clock)
        self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assert_finite_maximum_continuity()
        self.assert_history()

    async def test_long_idle_then_real_draw_preserves_max_in_both_report_orders(self):
        for interrupted, grid_first in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(interrupted=interrupted, grid_first=grid_first):
                if (interrupted, grid_first) != (True, True):
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.maximum_after_recovery(interrupted=interrupted)
                await self.drive_idle(self.now+190.)
                self.adapter.calls.clear()
                wake = self.now+1.
                self.report(wake, "charging", 19. if grid_first else 3.,
                    0. if grid_first else 16., 99. if grid_first else wake)
                await self.tick()
                if grid_first:
                    self.report(wake+8., "charging", 19., 16., wake+8.)
                else:
                    self.report(wake+2., "charging", 19., 16., wake)
                # Provider seconds are coarse and never replace local receipts.
                self.native.source_timestamps = tuple((key, self.wall(float(int(wake))))
                    for key in (182, 183, 184, 185))
                await self.tick()
                self.report(wake+10., "charging", 19., 16., wake+8. if grid_first else wake)
                await self.tick()
                await self.drive_idle(wake+40., raw=19., current=16.,
                    receipt=wake+8. if grid_first else wake, status="charging")
                self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
                self.assertTrue(all(d.command == "set_limit" and d.current_p1 == 16 for d in self.adapter.calls),
                    [(d.command, d.current_p1, d.reason) for d in self.adapter.calls])
                self.assert_history()
                self.assert_finite_maximum_continuity()

    async def test_insufficient_whole_prior_headroom_and_unadvanced_grid_do_not_recover(self):
        for problem in ("whole_offer", "unchanged_grid"):
            with self.subTest(problem=problem):
                if problem != "whole_offer":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.start()
                for offset in range(5, 46, 5):
                    self.report(self.recovered_at+offset, raw=10. if problem == "whole_offer" else 3.)
                    if problem == "unchanged_grid":
                        self.states["sensor.grid"].last_reported = self.wall(self.recovered_at+5.)
                    await self.tick()
                self.assertFalse(any(d.current_p1 > 7 for d in self.adapter.calls))
                self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 1)
                self.assert_history()

    async def test_terminal_recovery_and_manual_status_policy_are_not_bypassed(self):
        for problem in ("attempts", "paused", "awaiting_scheduled_start", "completed", "source"):
            with self.subTest(problem=problem):
                if problem != "attempts":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.start(stops=3, interrupted=False) if problem == "attempts" else await self.start()
                for step in range(1, 10):
                    self.report(self.now+5., problem if problem in {"paused", "awaiting_scheduled_start", "completed"} else "awaiting_start")
                    if problem == "source":
                        self.native.error = "native_sdk_current_invalid"
                    await self.tick()
                self.assertFalse(any(d.current_p1 > 7 or d.command == "resume" for d in self.adapter.calls))
                if problem == "attempts":
                    self.assertGreaterEqual(r.export_overload_state()["normal_control"]["stops"], 3)
                self.assert_history()

    async def test_permanent_store_error_and_unverified_zero_block_new_maximum(self):
        for problem in ("storage", "zero"):
            with self.subTest(problem=problem):
                if problem != "storage":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.start()
                if problem == "storage":
                    previous = self.controller._budget_store
                    previous.store.failure = True
                    self.controller._budget_store = type(previous)(previous.store,
                        clock=previous.clock, read_back=previous.store.async_load)
                else:
                    self.dispatch_failure = RuntimeError("synthetic_unknown_delivery")
                    self.report(self.now+1.)
                    self.native.error = "native_sdk_current_invalid"
                    await self.tick()
                for _ in range(9):
                    self.report(self.now+5.)
                    await self.tick()
                self.assertFalse(any(d.current_p1 > 7 for d in self.adapter.calls))
                self.assertTrue(any(d.current_p1 == 0 for d in self.adapter.calls))
                self.assert_history()

    async def test_real_pending_checkpoint_keeps_old_seven_until_verification(self):
        r = await self.start()
        for offset in (5., 10., 15., 20., 25.):
            self.report(self.recovered_at+offset)
            await self.tick()
        accepted = r.last_set_limit_ts
        previous = self.controller._budget_store
        self.controller._budget_store = type(previous)(previous.store,
            clock=previous.clock, read_back=previous.store.async_load)
        save = previous.store.async_save
        delayed = False

        async def slow(payload):
            nonlocal delayed
            if not delayed:
                delayed = True
                await asyncio.sleep(.01)
                self.now += 2.1  # The original live lease pays for this await.
                await asyncio.sleep(2.49)
            await save(payload)

        previous.store.async_save = slow
        self.adapter.calls.clear()
        self.report(self.recovered_at+35.)
        await self.tick()
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r.last_set_limit_ts, accepted)
        await self.controller._budget_store.async_wait_pending()
        self.report(self.now+5.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assert_history()


if __name__ == "__main__":
    unittest.main()
