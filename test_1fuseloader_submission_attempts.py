"""Only actual adapter dispatches spend transition retry throttles, offline."""
from dataclasses import replace
import unittest

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class SubmissionAttemptTests(unittest.IsolatedAsyncioTestCase):
    update_entry = cohorts.ZeroCohortRuntimeTests.update_entry
    wall = cohorts.ZeroCohortRuntimeTests.wall
    schedule = cohorts.ZeroCohortRuntimeTests.schedule
    active_timers = cohorts.ZeroCohortRuntimeTests.active_timers
    asyncSetUp = cohorts.ZeroCohortRuntimeTests.asyncSetUp
    asyncTearDown = cohorts.ZeroCohortRuntimeTests.asyncTearDown
    set_native = cohorts.ZeroCohortRuntimeTests.set_native
    snapshot = cohorts.ZeroCohortRuntimeTests.snapshot
    install = cohorts.ZeroCohortRuntimeTests.install
    native_vector = cohorts.ZeroCohortRuntimeTests.native_vector
    grid = cohorts.ZeroCohortRuntimeTests.grid

    async def establish(self, kind="low_grid"):
        self.kind = kind
        self.attempt_field = {"low_grid": "_low_grid_attempt_at",
            "normal": "_normal_shed_attempt_at",
            "overload": "_overload_minimum_attempted_at"}[kind]
        r = self.controller.regulator
        options = dict(nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            overload_enabled=True, measurement_settle_seconds=25,
            grid_confirmation_seconds=30, enable_soft_guard=False,
            enable_pause_resume=False)
        self.entry.options.update(options)
        r.update_settings(replace(self.controller.settings, **options))
        self.assertTrue(r.import_overload_state(dict(version=1,
            used_seconds=3601., exposure_active=False, transition_pending=False,
            minimum_held=False, failure_reason="response_timeout")))
        self.controller._budget_loaded = True
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.adapter.offline_limits_verified_zero = True
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "awaiting_start"
        for at in (100., 160.):
            self.now = at
            self.grid(4.)
            self.native_vector(0., (182, 183, 184, 185), {}, {}, 90.)
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.now = 162.5
        self.states["sensor.status"].state = "charging"
        self.states["sensor.status"].last_changed = self.wall()
        self.native_vector(float32(16.1), (182, 183),
            {182: 162., 183: 162.}, {182: 1, 183: 1}, 162.)
        self.grid(20.1)
        await self.controller.async_tick()
        self.assertIsNone(r._positive_cap)
        if kind == "overload":
            await self.controller.async_start_overload_trial()
        self.now = 194.5
        self.native_vector(float32(16.1), (182, 183),
            {182: 194., 183: 194.}, {182: 2, 183: 2}, 194.)
        self.grid(20.1)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 16)
        self.adapter.calls.clear()
        self.now = 200.
        self.grid({"low_grid": 5.7, "normal": 26.6, "overload": 30.4}[kind], 194.5)
        return r

    async def planning_only(self, kind):
        r = await self.establish(kind)
        original = r.export_overload_state()["normal_control"]["stops"]
        first = r.evaluate(self.controller._read_input())
        self.assertEqual(first.current_p1, 7)
        self.assertTrue(first.should_call_service)
        self.assertIsNone(getattr(r, self.attempt_field))
        deadline = first.nominal_response_deadline_ts or (
            r._overload_transition_started+r.settings.overload_response_seconds)
        self.now += .1
        second = r.evaluate(self.controller._read_input())
        self.assertEqual(second.current_p1, 7)
        self.assertTrue(second.should_call_service)
        self.assertIsNone(getattr(r, self.attempt_field))
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], original)
        if second.nominal_response_deadline_ts is not None:
            self.assertEqual(second.nominal_response_deadline_ts, deadline)
        self.assertEqual(self.adapter.calls, [])

    async def test_low_grid_planning_does_not_spend_an_actual_attempt(self):
        await self.planning_only("low_grid")

    async def test_nominal_planning_does_not_spend_an_actual_attempt(self):
        await self.planning_only("normal")

    async def test_overload_planning_does_not_spend_an_actual_attempt(self):
        await self.planning_only("overload")

    async def checkpoint_abort(self, kind):
        r = await self.establish(kind)
        save = self.controller._budget_store.async_save
        changed = False

        async def newer_valid_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 200.05
                self.grid(13.7 if kind == "low_grid" else 26.6, 200.)
            return result

        self.controller._budget_store.async_save = newer_valid_grid
        await self.controller.async_tick()
        self.assertTrue(changed)
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(getattr(r, self.attempt_field))
        deadline = self.controller.last_decision.nominal_response_deadline_ts
        self.assertAlmostEqual(deadline, 204.5)
        self.controller._budget_store.async_save = save
        self.now = 200.1
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [7])
        self.assertEqual(getattr(r, self.attempt_field), 200.1)
        self.assertEqual(self.controller.last_decision.nominal_response_deadline_ts, deadline)
        self.now = 201.3
        self.native_vector(float32(7.1), (182, 183),
            {182: 200.8, 183: 200.8}, {182: 3, 183: 3}, 200.8)
        self.grid(13.7 if kind == "low_grid" else 17.7, 201.)
        await self.controller.async_tick()
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))
        self.assertGreater(self.controller.last_decision.current_p1, 0)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 0)

    async def test_low_grid_checkpoint_abort_can_recalculate_and_send_before_original_deadline(self):
        await self.checkpoint_abort("low_grid")

    async def test_nominal_checkpoint_abort_can_recalculate_and_send_before_original_deadline(self):
        await self.checkpoint_abort("normal")

    async def failed_attempt(self, kind, failure):
        r = await self.establish(kind)
        self.adapter.failure = failure
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [7])
        self.assertEqual(getattr(r, self.attempt_field), 200.)
        self.assertEqual(r.last_target[0], 16)  # A failed API is not acceptance.
        self.assertTrue(self.controller._protective_zero_pending)
        self.now = 200.1
        retry = r.evaluate(self.controller._read_input())
        self.assertEqual(retry.current_p1, 7)
        self.assertFalse(retry.should_call_service)
        self.assertEqual(getattr(r, self.attempt_field), 200.)

    async def test_real_low_grid_failure_keeps_its_actual_attempt_throttle(self):
        await self.failed_attempt("low_grid", OSError("synthetic unavailable"))

    async def test_ambiguous_low_grid_timeout_keeps_its_actual_attempt_throttle(self):
        await self.failed_attempt("low_grid", TimeoutError())

    async def test_real_nominal_failure_keeps_its_actual_attempt_throttle(self):
        await self.failed_attempt("normal", OSError("synthetic unavailable"))

    async def test_real_overload_failure_keeps_its_actual_attempt_throttle(self):
        await self.failed_attempt("overload", OSError("synthetic unavailable"))

    async def test_invalid_or_non_dispatch_begin_does_not_create_an_attempt(self):
        r = await self.establish()
        planned = r.evaluate(self.controller._read_input())
        for invalid in (None, True, -1., float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                self.assertFalse(r.begin_submission(planned, invalid))
                self.assertIsNone(r._low_grid_attempt_at)
        for no_dispatch in (replace(planned, should_call_service=False),
                            replace(planned, dry_run=True), replace(planned, command=None)):
            self.assertFalse(r.begin_submission(no_dispatch, self.now))
            self.assertIsNone(r._low_grid_attempt_at)
        self.assertEqual(self.adapter.calls, [])

    async def test_checkpoint_latency_is_debited_at_real_dispatch_not_planning(self):
        r = await self.establish()
        save = self.controller._budget_store.async_save
        original = self.adapter.async_execute
        observed = []

        async def delayed_checkpoint(state):
            result = await save(state)
            self.now += .25
            return result

        async def observe_dispatch(decision):
            observed.append((self.now, r._low_grid_attempt_at))
            return await original(decision)

        self.controller._budget_store.async_save = delayed_checkpoint
        self.adapter.async_execute = observe_dispatch
        await self.controller.async_tick()
        self.assertEqual(observed, [(200.25, 200.25)])
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [7])
        self.assertAlmostEqual(self.controller.last_decision.nominal_response_deadline_ts, 204.5)


if __name__ == "__main__":
    unittest.main()
