"""Normal recovery remains available beside an unadmitted automatic cycle.

Generic virtual-clock controller traces. All limits are created by actual
fixture adapter acknowledgements; no private household records or positive
proof states are imported. The historical exposure snapshot is synthetic.
"""
from dataclasses import replace
import unittest

import test_1fuseloader_zero_cohort_runtime as fixtures
from test_1fuseloader_ha_current_representation import float32


class AutomaticNormalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ZeroCohortRuntimeTests.update_entry
    wall = fixtures.ZeroCohortRuntimeTests.wall
    schedule = fixtures.ZeroCohortRuntimeTests.schedule
    active_timers = fixtures.ZeroCohortRuntimeTests.active_timers
    asyncSetUp = fixtures.ZeroCohortRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ZeroCohortRuntimeTests.asyncTearDown
    set_native = fixtures.ZeroCohortRuntimeTests.set_native
    snapshot = fixtures.ZeroCohortRuntimeTests.snapshot
    install = fixtures.ZeroCohortRuntimeTests.install
    native_vector = fixtures.ZeroCohortRuntimeTests.native_vector
    grid = fixtures.ZeroCohortRuntimeTests.grid

    def configure(self, automatic=True):
        options = dict(min_charge_amps=7, nominal_import_only_fallback=True,
            bounded_nominal_shedding=True, overload_enabled=True,
            peak_only_response=True, automatic_overload_cycles=automatic,
            measurement_settle_seconds=25, enable_soft_guard=False,
            enable_pause_resume=False)
        self.entry.options.update(options)
        r = self.controller.regulator
        r.update_settings(replace(self.controller.settings, **options))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.adapter.async_read_current_snapshot = None
        self.adapter.offline_limits_verified_zero = True
        return r

    def report_native(self, value, receipt, sequence):
        self.native_vector(float32(value), (182,), {182: receipt},
            {182: sequence}, receipt)

    def assert_history(self, r, automatic=True):
        state = r.export_overload_state()
        self.assertEqual(state["used_seconds"], 3601.)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertNotIn("trial_epoch", state)
        if automatic:
            self.assertEqual(state["automatic_cycle"]["state"], "ready")
            self.assertEqual(state["automatic_cycle"]["used_seconds"], 0)
            self.assertIsNone(state["automatic_cycle"]["failure_reason"])

    async def stale_seven(self, automatic=True):
        r = self.configure(automatic)
        self.states["sensor.status"].state = "charging"
        self.now = 100.3
        self.report_native(7., 100., 1)
        self.grid(16.)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 7)
        self.now = 101.3
        self.report_native(7., 101., 2)
        self.grid(23.)
        await self.controller.async_tick()
        for at in (120., 150., 180., 210., 240., 270.):
            self.now = at
            self.grid(23.)
            await self.controller.async_tick()
            self.assertEqual(r.last_target[0], 7)
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertIsNone(r._uncertainty_ceiling)
        self.adapter.calls.clear()
        self.now = 300.
        self.grid(11.)
        return r

    async def reduction_cohort(self, automatic=True):
        r = self.configure(automatic)
        self.states["sensor.status"].state = "awaiting_start"
        for at in (100., 160.):
            self.now = at
            self.grid(4.)
            self.native_vector(0., (182, 183, 184, 185), {}, {}, 90.)
            await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 16)
        self.now = 162.5
        self.states["sensor.status"].state = "charging"
        self.states["sensor.status"].last_changed = self.wall()
        self.native_vector(float32(16.14), (182,), {182: 162., 183: 161.},
            {182: 1, 183: 1}, 162.)
        self.grid(20.)
        await self.controller.async_tick()
        self.now = 190.3
        self.native_vector(float32(16.14), (182,), {182: 190., 183: 173.},
            {182: 2, 183: 2}, 190.)
        self.grid(20.)
        await self.controller.async_tick()
        self.now = 195.
        self.grid(24.)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 15)
        self.assertEqual(r._awaiting_reduction_target, 15)
        self.adapter.calls.clear()
        self.now = 200.36
        self.grid(21., 199.)
        self.native_vector(float32(16.139), (183,), {182: 200.30, 183: 173.},
            {182: 3, 183: 2}, 200.30)
        return r

    async def test_automatic_ready_allows_twelve_from_full_raw_eleven_without_ev_credit(self):
        r = await self.stale_seven()
        old_clock = self.controller._read_input().charger_report_ts
        await self.controller.async_tick()
        d = self.controller.last_decision
        self.assertEqual([v.current_p1 for v in self.adapter.calls], [12])
        self.assertEqual(d.house_load_l1, 11.)
        self.assertLessEqual(d.house_load_l1+d.current_p1, 23.)
        self.assertEqual(self.controller._read_input().charger_report_ts, old_clock)
        self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_not_admitted")
        self.assert_history(r)

    async def test_same_stale_raw_trace_with_automatic_off_preserves_twelve(self):
        r = await self.stale_seven(False)
        await self.controller.async_tick()
        self.assertEqual([v.current_p1 for v in self.adapter.calls], [12])
        self.assertEqual(self.controller.last_decision.house_load_l1, 11.)
        self.assert_history(r, False)

    async def test_raw_rise_response_and_later_grid_complete_normal_sixteen(self):
        r = await self.stale_seven()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        deadline = self.controller.last_decision.raw_budget_response_deadline_ts
        self.assertAlmostEqual(deadline, 325., places=6)
        self.adapter.calls.clear()
        self.now = 302.
        self.grid(16.)
        await self.controller.async_tick()
        self.assertEqual((self.controller.last_decision.current_p1,
            self.controller.last_decision.command), (12, None))
        self.assertEqual(self.adapter.calls, [])
        self.now = 303.3
        self.report_native(12.05, 303., 3)
        self.grid(16., 302.5)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 12)
        self.assertEqual(self.adapter.calls, [])
        self.now = 305.
        self.grid(16.)
        await self.controller.async_tick()
        # Qualifying the accepted12 A offer discharges measurement work;
        # it does not authorize a higher offer in that same evaluation.
        self.assertEqual((self.controller.last_decision.current_p1,
            self.controller.last_decision.command), (12, None))
        self.assertEqual(r.last_set_limit_ts, 300.)
        self.assertEqual(self.adapter.calls, [])
        self.now = 310.
        self.grid(16.)
        await self.controller.async_tick()
        self.assertEqual([v.current_p1 for v in self.adapter.calls], [16])
        self.assert_history(r)

    async def test_raw_budget_rounding_never_subtracts_cached_ev(self):
        for raw, expected in ((11.001, 11), (16., 7), (16.1, 7)):
            with self.subTest(raw=raw):
                if raw != 11.001:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.stale_seven()
                self.grid(raw)
                await self.controller.async_tick()
                self.assertEqual(self.controller.last_decision.current_p1, expected)
                self.assertFalse(any(v.current_p1 > expected for v in self.adapter.calls))

    async def test_raw_rise_still_rejects_expired_lease_invalid_future_and_binding_sources(self):
        for invalid in ("lease", "native", "future", "binding", "uncommitted"):
            with self.subTest(invalid=invalid):
                if invalid != "lease":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.stale_seven()
                if invalid == "lease":
                    self.now = r.last_set_limit_ts+r.last_target[3]+.1
                    self.grid(11.)
                elif invalid == "native":
                    self.native.error = "native_sdk_current_invalid"
                elif invalid == "future":
                    self.native.received_at = self.wall(self.now+6.)
                    self.native.observation_report_times = ((182, self.wall(self.now+6.)),)
                elif invalid == "binding":
                    self.controller._configured_control_identity.return_value = "other-source"
                else:
                    self.native.committed = False
                await self.controller.async_tick()
                self.assertLessEqual(self.controller.last_decision.current_p1, 7)
                self.assertFalse(any(v.current_p1 > 7 for v in self.adapter.calls))

    async def test_hard_peak_still_stops_instead_of_increasing_stale_seven(self):
        await self.stale_seven()
        self.grid(39.)
        await self.controller.async_tick()
        self.assertEqual([v.current_p1 for v in self.adapter.calls], [0])

    async def test_raw_response_churn_cannot_renew_original_deadline_without_response(self):
        r = await self.stale_seven()
        await self.controller.async_tick()
        deadline = self.controller.last_decision.raw_budget_response_deadline_ts
        accepted_at = r.last_set_limit_ts
        self.adapter.calls.clear()
        for at in (302., 310., 324.):
            self.now = at
            self.grid(20.)
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 12)
            self.assertEqual(r._positive_raw_deadline, deadline)
            self.assertEqual(r.last_set_limit_ts, accepted_at)
            self.assertEqual(self.adapter.calls, [])
        self.now = deadline
        self.grid(20.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertEqual(r._positive_raw_deadline, None)

    async def test_automatic_ready_retains_live_fifteen_inside_original_normal_bound(self):
        r = await self.reduction_cohort()
        await self.controller.async_tick()
        d = self.controller.last_decision
        self.assertEqual((d.current_p1, d.should_call_service), (15, False))
        self.assertEqual(d.mode, "normal")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, 195.)
        self.assertIsNone(d.source_processing_deadline_ts)
        self.assertEqual(r._committed_normal.observation_deadline, 220.)
        self.assertLessEqual(r._committed_normal.house + r._committed_normal.unresolved_cap, 23.)
        self.assert_history(r)

    async def test_same_cohort_with_automatic_off_preserves_existing_hold(self):
        r = await self.reduction_cohort(False)
        await self.controller.async_tick()
        self.assertEqual((self.controller.last_decision.current_p1,
            self.controller.last_decision.should_call_service), (15, False))
        self.assertEqual(r.last_set_limit_ts, 195.)
        self.assertEqual(self.adapter.calls, [])
        self.assert_history(r, False)

    async def test_cohort_raw_lease_future_and_binding_guards_remain_independent(self):
        for invalid in ("raw", "lease", "future", "binding"):
            with self.subTest(invalid=invalid):
                if invalid != "raw":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.reduction_cohort()
                if invalid == "raw":
                    # A partial ordinary batch below25 A can use its bounded
                    # cohort fence. Actual above-nominal load must leave it.
                    self.grid(25.01)
                elif invalid == "lease":
                    self.now = r.last_set_limit_ts+r.last_target[3]+.1
                    self.grid(21.)
                    self.native_vector(float32(16.139), (183,),
                        {182: self.now-.05, 183: 173.}, {182: 3, 183: 2}, self.now-.05)
                elif invalid == "future":
                    self.native.received_at = self.wall(self.now+6.)
                    self.native.observation_report_times = (
                        (182, self.wall(200.3)), (183, self.wall(self.now+6.)))
                else:
                    self.controller._configured_control_identity.return_value = "other-source"
                await self.controller.async_tick()
                self.assertNotEqual(self.controller.last_decision.mode, "waiting_for_current_cohort")
                self.assertFalse(any(v.current_p1 == 15 for v in self.adapter.calls))

    async def test_cohort_churn_cannot_refill_committed_observation_or_renew_lease(self):
        r = await self.reduction_cohort()
        await self.controller.async_tick()
        deadline = r._committed_normal.observation_deadline
        self.assertEqual(deadline, 220.)
        self.now = 200.75
        self.grid(21., 200.7)
        self.native_vector(float32(16.139), (183,), {182: 200.70, 183: 173.},
            {182: 4, 183: 2}, 200.70)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 15)
        self.assertEqual(r._committed_normal.observation_deadline, deadline)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, 195.)
        self.now = deadline
        self.grid(21., deadline-.1)
        self.native_vector(float32(16.139), (183,), {182: deadline-.05, 183: 173.},
            {182: 5, 183: 2}, deadline-.05)
        await self.controller.async_tick()
        # A partial native update cannot create a fresh processing interval
        # after the original accepted lower observation budget is exhausted.
        self.assertEqual([v.current_p1 for v in self.adapter.calls], [0])
        self.assertEqual(r.last_target[0], 0)
        self.assertIsNone(r._committed_normal)


if __name__ == "__main__":
    unittest.main()
