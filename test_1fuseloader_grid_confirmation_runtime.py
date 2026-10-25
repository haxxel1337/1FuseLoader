"""Fixed post-EV grid confirmation through the real controller, offline.

The synthetic receiver/provider clocks below are separate from API acceptance.
Every lower offer and response is established through ordinary controller ticks.
"""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_runtime as fixtures
from test_1fuseloader_ha_current_representation import float32
import test_1fuseloader_zero_cohort_runtime as cohorts


class GridConfirmationRuntimeTests(unittest.IsolatedAsyncioTestCase):
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

    async def accepted_lower(self, *, trial=False, ttl=60):
        r = self.controller.regulator
        options = dict(nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, overload_enabled=True,
            measurement_settle_seconds=25, grid_confirmation_seconds=30,
            enable_soft_guard=False, enable_pause_resume=False,
            normal_ttl_seconds=ttl)
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
        self.native_vector(float32(16.141), (182, 183),
            {182: 162., 183: 162.}, {182: 1, 183: 1}, 162.)
        self.grid(20.1)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._positive_cap)
        if trial:
            await self.controller.async_start_overload_trial()
            self.assertEqual(r.export_overload_state()["trial_epoch"]["state"], "active")
        self.now = 194.5
        self.grid(20.1)
        self.native_vector(float32(16.141), (182, 183),
            {182: 194., 183: 194.}, {182: 2, 183: 2}, 194.)
        await self.controller.async_tick()
        self.now = 195.
        self.grid(30.4, 194.8)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7,
            self.controller.last_decision)
        self.assertIn(self.controller.last_decision.mode,
            {"normal_shedding", "overload_shedding"})
        self.lower_plan = self.controller.last_decision
        self.lower_ack = r.last_set_limit_ts
        self.adapter.calls.clear()
        return r

    def timely_seven(self, *, at=201.7, received=201.4):
        self.now = at
        self.grid(30.4, 198.8)  # This grid predates the genuine lower EV event.
        self.native_vector(float32(7.086), (182, 183),
            {182: received, 183: received}, {182: 3, 183: 3}, received)

    async def grid_wait(self, *, trial=False, ttl=60):
        r = await self.accepted_lower(trial=trial, ttl=ttl)
        self.timely_seven()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode,
            "overload_grid_wait" if trial else "normal_shedding_grid_wait")
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(self.controller.last_decision.command)
        self.assertFalse(self.controller.last_decision.should_call_service)
        return r

    async def test_nominal_grid_wait_outlives_physical_deadline_without_a_ttl_renewal(self):
        r = await self.grid_wait()
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        self.assertAlmostEqual(deadline, 231.4, places=5)
        self.assertIsNone(self.controller.last_decision.nominal_response_deadline_ts)
        original = r.export_overload_state()
        for at in (204.8, 211., 225.1, 230.):
            self.now = at
            await self.controller.async_tick()
            self.assertEqual(self.adapter.calls, [], self.controller.last_decision)
            self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts, deadline)
            self.assertEqual(r.last_set_limit_ts, self.lower_ack)
        self.now = 230.4
        self.grid(21.5)
        await self.controller.async_tick()
        self.assertGreater(self.controller.last_decision.current_p1, 0)
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertEqual(r.export_overload_state()["failure_reason"], original["failure_reason"])
        self.assertNotIn("trial_epoch", r.export_overload_state())

    async def test_wait_modes_suppress_even_a_malformed_positive_dispatch_flag(self):
        r = await self.grid_wait()
        genuine = self.controller.last_decision
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                forged = replace(genuine, command=command,
                    should_call_service=True)
                with patch.object(r, "evaluate", return_value=forged):
                    await self.controller.async_tick()
                self.assertEqual(self.adapter.calls, [])
                self.assertIsNone(self.controller.last_decision.command)
                self.assertFalse(self.controller.last_decision.should_call_service)
                self.assertEqual(r.last_set_limit_ts, self.lower_ack)

    async def test_active_trial_wait_suppresses_a_malformed_resume_or_limit_flag(self):
        r = await self.grid_wait(trial=True)
        genuine = self.controller.last_decision
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                forged = replace(genuine, command=command, should_call_service=True)
                with patch.object(r, "evaluate", return_value=forged):
                    await self.controller.async_tick()
                self.assertEqual(self.adapter.calls, [])
                self.assertIsNone(self.controller.last_decision.command)
                self.assertFalse(self.controller.last_decision.should_call_service)
                self.assertEqual(r.last_set_limit_ts, self.lower_ack)

    async def test_deadline_timer_wakes_at_exact_first_response_deadline(self):
        await self.grid_wait()
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        timer = next(item for item in self.active_timers()
            if abs(item.due-deadline) < 1e-6)
        self.now = deadline-.001
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        timer = next(item for item in self.active_timers()
            if abs(item.due-deadline) < 1e-6)
        self.controller._handle_source_change.reset_mock()
        self.now = deadline
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once_with(None)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIn("grid_confirmation_timeout", self.controller.last_decision.reason)
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_expired_unsent_nominal_plan_rechecks_a_genuine_grid_wait(self):
        r = await self.accepted_lower()
        old = self.lower_plan
        self.timely_seven(at=205.)
        original = r.evaluate
        first = True

        def with_one_stale_plan(inp):
            nonlocal first
            if first:
                first = False
                return replace(old, should_call_service=True, command="set_limit")
            return original(inp)

        with patch.object(r, "evaluate", side_effect=with_one_stale_plan):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "normal_shedding_grid_wait")
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertIsNone(self.controller.last_decision.command)
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts, 231.4, places=5)

    async def test_source_fault_cannot_use_the_grid_confirmation_window(self):
        await self.grid_wait()
        self.now += .1
        self.adapter.telemetry_block_reason = "easee_stream_disconnected"
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_new_raw_peak_protects_immediately_during_grid_wait(self):
        await self.grid_wait()
        self.now += .1
        self.grid(39.)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_accepted_lease_expiry_shortens_grid_wait_and_never_renews(self):
        await self.grid_wait(ttl=20)
        deadline = self.lower_ack+20
        self.assertAlmostEqual(self.controller.last_decision.grid_confirmation_deadline_ts,
            deadline, places=5)
        self.now = deadline-.001
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.now = deadline
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_new_post_response_high_grid_stops_without_spending_the_rest_of_wait(self):
        await self.grid_wait()
        self.now = 202.
        self.grid(23.01)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIn("grid_confirmation_outside_nominal_budget", self.controller.last_decision.reason)

    async def test_pending_grid_wait_restores_as_interrupted_instead_of_a_fresh_thirty_seconds(self):
        r = await self.grid_wait()
        pending = deepcopy(self.controller._budget_store.store.data["regulator"])
        self.assertTrue(pending["normal_control"]["response_pending"])
        self.assertEqual(pending["failure_reason"], "response_timeout")
        await self.controller.async_stop()
        self.now += 1
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        await self.controller.async_start()
        restored = self.controller.regulator.export_overload_state()
        self.assertEqual(restored["normal_control"]["response_failure"], "interrupted_normal_response")
        self.assertGreaterEqual(restored["used_seconds"], pending["used_seconds"])
        self.assertEqual(restored["failure_reason"], pending["failure_reason"])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertTrue(all(call.current_p1 == 0 for call in self.adapter.calls))

    async def test_active_trial_grid_wait_keeps_exposure_and_transition_pending(self):
        r = await self.grid_wait(trial=True)
        before = r.export_overload_state()
        self.assertTrue(before["transition_pending"])
        self.assertTrue(before["exposure_active"])
        self.assertEqual(before["trial_epoch"]["state"], "active")
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        self.now = 225.1
        await self.controller.async_tick()
        after = r.export_overload_state()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts, deadline)
        self.assertTrue(after["transition_pending"])
        self.assertGreater(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], "response_timeout")
        self.assertEqual(after["trial_epoch"]["state"], "active")
        self.now = 225.2
        self.grid(28.)
        await self.controller.async_tick()
        self.assertEqual(r.export_overload_state()["trial_epoch"]["state"], "active")
        self.assertFalse(r.export_overload_state()["transition_pending"])
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))

    async def test_active_trial_new_post_response_high_grid_stops_immediately(self):
        r = await self.grid_wait(trial=True)
        self.now = 202.
        self.grid(29.01)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertEqual(r.export_overload_state()["trial_epoch"]["state"], "failed")
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_active_wait_survives_a_get_clock_baseline_without_replacing_its_response(self):
        r = await self.grid_wait(trial=True)
        deadline = self.controller.last_decision.grid_confirmation_deadline_ts
        first_response = r._overload_minimum_response_ts
        self.now = 202.
        # The actual native evidence API is temporarily unavailable. A selected
        # GET can describe the unchanged cached value, but supplies no new raw
        # receiver sequence or post-ACK event merely by changing clock family.
        self.adapter.native_current_observation = None
        self.install(self.snapshot(current=float(self.states["sensor.car"].state),
            observed=201.4, ids=(182, 183)))
        await self.controller.async_tick()
        self.assertEqual(self.controller._read_input().charger_report_family, "vendor_event")
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "overload_grid_wait")
        self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts, deadline)
        self.assertEqual(r._overload_minimum_response_ts, first_response)
        self.assertIsNone(self.controller.last_decision.command)
        self.assertEqual(r.last_set_limit_ts, self.lower_ack)

        self.now = 202.2
        self.states["sensor.car"].state = "6.8"
        self.states["sensor.car"].last_reported = self.wall(202.1)
        self.install(self.snapshot(current=6.8, observed=201.4, ids=(182, 183)))
        await self.controller.async_tick()
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIn("grid_confirmation_value_changed_without_event", self.controller.last_decision.reason)
        self.assertEqual(r.export_overload_state()["trial_epoch"]["state"], "failed")
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)

    async def test_active_trial_grid_wait_restore_does_not_resume_its_pending_transition(self):
        r = await self.grid_wait(trial=True)
        pending = deepcopy(self.controller._budget_store.store.data["regulator"])
        self.assertTrue(pending["transition_pending"])
        await self.controller.async_stop()
        self.now += 1
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        await self.controller.async_start()
        restored = self.controller.regulator.export_overload_state()
        self.assertEqual(restored["trial_epoch"]["state"], "interrupted")
        self.assertGreaterEqual(restored["used_seconds"], pending["used_seconds"])
        self.assertEqual(restored["failure_reason"], pending["failure_reason"])
        self.assertFalse(restored["transition_pending"])
        self.assertIsNone(self.controller.last_decision.grid_confirmation_deadline_ts)
        self.assertTrue(self.adapter.calls)
        self.assertTrue(all(call.current_p1 == 0 for call in self.adapter.calls))


if __name__ == "__main__":
    unittest.main()
