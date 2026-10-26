"""Partial startup/rise response ownership through the real controller.

Synthetic source events, clocks and adapter replies reproduce the ordering
contract without importing household history or seeding a positive owner.
Every positive authority in this file is accepted at an actual controller
adapter boundary. No Home Assistant or charger connection is made.
"""
from dataclasses import replace
import unittest

import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class StartupOwnerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = cohorts.ZeroCohortRuntimeTests.update_entry
    wall = cohorts.ZeroCohortRuntimeTests.wall
    schedule = cohorts.ZeroCohortRuntimeTests.schedule
    active_timers = cohorts.ZeroCohortRuntimeTests.active_timers
    asyncTearDown = cohorts.ZeroCohortRuntimeTests.asyncTearDown
    set_native = cohorts.ZeroCohortRuntimeTests.set_native
    snapshot = cohorts.ZeroCohortRuntimeTests.snapshot
    install = cohorts.ZeroCohortRuntimeTests.install
    native_vector = cohorts.ZeroCohortRuntimeTests.native_vector
    grid = cohorts.ZeroCohortRuntimeTests.grid

    async def asyncSetUp(self):
        await cohorts.ZeroCohortRuntimeTests.asyncSetUp(self)
        options = dict(nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=True,
            overload_enabled=True, grid_confirmation_seconds=30,
            enable_soft_guard=False, enable_pause_resume=False)
        self.entry.options.update(options)
        self.controller.regulator.update_settings(replace(self.controller.settings, **options))
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.adapter.async_read_current_snapshot = None
        self.adapter.offline_limits_verified_zero = True
        self.sequence = 0

    def publish(self, at, grid, current, *, receipt=None, grid_at=None, status="charging"):
        self.now = at
        self.states["sensor.status"].state = status
        self.states["sensor.status"].last_changed = self.wall(min(at-1., receipt or at-1.))
        if receipt is None:
            self.native_vector(float32(current), (182, 183), {}, {}, at-1.)
        else:
            self.sequence += 1
            self.native_vector(float32(current), (182, 183),
                {182: receipt, 183: receipt},
                {182: self.sequence, 183: self.sequence}, receipt)
        self.grid(grid, at if grid_at is None else grid_at)

    async def bootstrap(self, *, clear_calls=True):
        for at in (100., 160.):
            self.publish(at, 8.4, .012, status="awaiting_start")
            await self.controller.async_tick()
        r = self.controller.regulator
        self.assertEqual(self.adapter.calls[-1].current_p1, 14)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(r._positive_cap, 14)
        self.assertAlmostEqual(r._positive_house, 8.4, places=6)
        self.assertEqual(r.last_set_limit_ts, 160.)
        if clear_calls:
            self.adapter.calls.clear()
        return r

    async def partial(self):
        r = await self.bootstrap()
        self.publish(170., 22.6, 1.8, receipt=167., grid_at=169.)
        await self.controller.async_tick()
        self.assert_retained(r)
        return r

    async def seven_then_sixteen_offer(self):
        for at in (100., 160.):
            self.publish(at, 16., .012, status="awaiting_start")
            await self.controller.async_tick()
        r = self.controller.regulator
        self.assertEqual(r.last_target[0], 7)
        self.publish(170., 12., 7., receipt=167., grid_at=169.)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.assertAlmostEqual(r._positive_house, 5., places=6)
        self.assertEqual(r.last_set_limit_ts, 170.)
        self.adapter.calls.clear()
        return r

    def assert_retained(self, r):
        decision = self.controller.last_decision
        self.assertEqual(decision.current_p1, 14, decision)
        self.assertEqual((decision.command, decision.service, decision.should_call_service),
            (None, None, False), decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(r.last_set_limit_ts, 160.)
        self.assertFalse(r._restart_required)
        cycle = r.automatic_cycle_diagnostics()
        self.assertEqual(cycle["automatic_cycle_state"], "ready")
        self.assertIsNone(cycle["automatic_cycle_failure_reason"])
        self.assertEqual(cycle["automatic_cycle_used_seconds"], 0)
        self.assertNotIn("trial_epoch", r.export_overload_state())

    async def test_later_grid_after_genuine_partial_response_retains_same_accepted_owner(self):
        r = await self.partial()
        self.assertEqual(r._positive_cap, 14)
        self.assertAlmostEqual(self.controller.last_decision.house_load_l1, 8.4, places=6)
        self.assertIsNotNone(self.controller.last_decision.measurement_alignment_remaining_seconds)

    async def test_first_grid_before_any_ev_event_keeps_only_accepted_start_and_first_deadline(self):
        for status in ("awaiting_start", "charging"):
            with self.subTest(status=status):
                if status == "charging":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.bootstrap(clear_calls=False)
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 14])
                # The meter sees the ramp first. Cached bootstrap .012 A has
                # no native event, response credit or fabricated first receipt.
                self.publish(162.5, 22.6, .012, grid_at=162., status=status)
                await self.controller.async_tick()
                self.assertEqual(self.controller.last_decision.current_p1, 14)
                self.assertEqual((self.controller.last_decision.command,
                    self.controller.last_decision.should_call_service), (None, False))
                self.assertIsNone(r._positive_response_ts)
                self.assertIsNone(r._positive_anchor_response_ts)
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 14])
                deadline = self.controller.last_decision.positive_response_deadline_ts
                self.assertAlmostEqual(deadline, 187., places=6)
                for at, receipt, current, sample in ((170., 167., 1.8, 169.),
                        (175., 172., 6., 174.)):
                    self.publish(at, 22.6, current, receipt=receipt, grid_at=sample)
                    await self.controller.async_tick()
                    self.assertEqual(self.controller.last_decision.current_p1, 14)
                    self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, deadline)
                    self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 14])
                self.publish(180., 22.6, 14.1, receipt=178., grid_at=179.)
                await self.controller.async_tick()
                self.assertIsNone(r._positive_cap)
                self.assertEqual(self.controller.last_decision.current_p1, 14)
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 14])
                self.assertEqual(r.last_set_limit_ts, 160.)
                self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
                self.assertIsNone(r.automatic_cycle_diagnostics()["automatic_cycle_failure_reason"])

    async def test_first_grid_window_expires_without_inventing_ev_response_or_renewal(self):
        r = await self.bootstrap()
        self.publish(162.5, 22.6, .012, grid_at=162., status="awaiting_start")
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 14)
        deadline = self.controller.last_decision.positive_response_deadline_ts
        self.assertAlmostEqual(deadline, 187., places=6)
        self.publish(deadline+.1, 22.6, .012, grid_at=deadline,
            status="awaiting_start")
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertIsNone(r._positive_response_ts)

    async def test_unchanged_ev_demand_with_coherent_house_does_not_open_ramp_timer(self):
        r = await self.seven_then_sixteen_offer()
        # A16 A offer is an upper bound. The car may continue drawing7 A;
        # new12 A meter samples still agree with the already verified5 A house.
        for at in (175., 185., 205.):
            self.now = at
            self.grid(12., at-1.)
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 16,
                (at, self.controller.last_decision))
            self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)
            self.assertFalse(r._restart_required)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls))

    async def test_coherent_partial_demand_below_offered_cap_is_not_a_failed_response(self):
        r = await self.seven_then_sixteen_offer()
        self.publish(176., 13., 8., receipt=174., grid_at=175.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        for at in (185., 200., 210.):
            self.now = at
            self.grid(13., at-1.)
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 16,
                (at, self.controller.last_decision))
            self.assertFalse(r._restart_required)
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls))

    async def test_partial_steps_share_original_clock_without_renewal_or_rise(self):
        r = await self.partial()
        deadline = self.now+self.controller.last_decision.measurement_alignment_remaining_seconds
        for at, receipt, current in ((175., 172., 6.), (180., 177., 10.)):
            self.publish(at, 22.6, current, receipt=receipt, grid_at=at-1.)
            await self.controller.async_tick()
            self.assert_retained(r)
            self.assertAlmostEqual(self.now+self.controller.last_decision.measurement_alignment_remaining_seconds,
                deadline, places=6)

    async def test_runtime_registers_same_absolute_deadline_then_requests_expiry_evaluation(self):
        r = await self.partial()
        deadline = self.controller.last_decision.positive_response_deadline_ts
        self.assertIsNotNone(deadline)
        self.assertAlmostEqual(deadline, 192., places=6)
        self.assertTrue(any(abs(item.due-deadline) < 1e-6 for item in self.active_timers()))
        self.publish(180., 22.6, 6., receipt=177., grid_at=179.)
        await self.controller.async_tick()
        self.assert_retained(r)
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, deadline)
        timer = next(item for item in self.active_timers() if abs(item.due-deadline) < 1e-6)
        self.now = deadline
        self.grid(22.6, deadline-1.)
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once_with(None)
        self.assertEqual(self.adapter.calls, [])
        # The fixture replaces the refresh queue with a spy; execute the
        # evaluation it requested to verify expiry without a new EV event.
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertIsNone(self.controller.last_decision.positive_response_deadline_ts)

    async def test_runtime_hard_no_write_rejects_forged_set_limit_and_resume_in_hold(self):
        for command in ("set_limit", "resume"):
            with self.subTest(command=command):
                if command == "resume":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.bootstrap()
                evaluate = r.evaluate

                def forged(inp):
                    decision = evaluate(inp)
                    if decision.mode == "positive_ramp_hold":
                        return replace(decision, command=command, should_call_service=True,
                            service="easee.synthetic_forbidden_positive")
                    return decision

                r.evaluate = forged
                self.publish(170., 22.6, 1.8, receipt=167., grid_at=169.)
                await self.controller.async_tick()
                self.assert_retained(r)
                self.assertEqual(self.controller.last_decision.mode, "positive_ramp_hold")
                self.assertFalse(r._uncertainty_retry_submission_pending)

    async def test_full_cap_and_later_coherent_grid_retire_original_owner(self):
        r = await self.partial()
        self.publish(175., 22.6, 6., receipt=172., grid_at=174.)
        await self.controller.async_tick()
        self.assert_retained(r)
        self.publish(179., 22.6, 14.1, receipt=177., grid_at=178.)
        await self.controller.async_tick()
        self.assertIsNone(r._positive_cap)
        self.assertEqual(self.controller.last_decision.current_p1, 14)
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.assertIsNone(r.automatic_cycle_diagnostics()["automatic_cycle_failure_reason"])

    async def test_full_cap_without_later_grid_keeps_only_same_live_owner(self):
        r = await self.partial()
        self.publish(178., 22.6, 14.1, receipt=177., grid_at=176.)
        await self.controller.async_tick()
        self.assert_retained(r)
        self.assertEqual(r._positive_cap, 14)
        self.now = 179.
        self.grid(22.6, 178.5)
        await self.controller.async_tick()
        self.assertIsNone(r._positive_cap)
        self.assertEqual(self.controller.last_decision.current_p1, 14)
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_identical_partial_event_cannot_move_observation_deadline(self):
        r = await self.partial()
        deadline = self.now+self.controller.last_decision.measurement_alignment_remaining_seconds
        self.publish(181., 22.6, 1.8, receipt=178., grid_at=180.)
        await self.controller.async_tick()
        self.assert_retained(r)
        self.assertAlmostEqual(self.now+self.controller.last_decision.measurement_alignment_remaining_seconds,
            deadline, places=6)

    async def test_expired_original_owner_is_not_recreated_by_another_partial_event(self):
        r = await self.partial()
        deadline = self.now+self.controller.last_decision.measurement_alignment_remaining_seconds
        self.publish(deadline+.1, 22.6, 6., receipt=deadline-1., grid_at=deadline)
        await self.controller.async_tick()
        self.assertLess(self.controller.last_decision.current_p1, 14)
        self.assertTrue(self.adapter.calls)
        self.assertFalse(any(d.current_p1 == 14 for d in self.adapter.calls))

    async def test_first_partial_after_actual_lease_expiry_cannot_renew_old_offer(self):
        r = await self.bootstrap()
        expiry = r.last_set_limit_ts+r.last_target[3]
        self.publish(expiry+.3, 22.6, 1.8, receipt=expiry+.01, grid_at=expiry+.1)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertFalse(any(d.current_p1 == 14 for d in self.adapter.calls))

    async def test_actual_accepted_decrease_cannot_be_replaced_by_old_startup_owner(self):
        r = await self.bootstrap()
        self.publish(170., 30.4, 14.1, receipt=167., grid_at=169.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.assertEqual(r.last_target[0], 7)
        self.adapter.calls.clear()
        self.publish(172., 22.6, 1.8, receipt=170.5, grid_at=171.5)
        await self.controller.async_tick()
        self.assertLessEqual(self.controller.last_decision.current_p1, 7)
        self.assertLessEqual(r.last_target[0], 7)
        self.assertFalse(any(d.current_p1 == 14 for d in self.adapter.calls))

    async def test_missing_invalid_future_and_rebound_sources_do_not_retain_start_owner(self):
        for fault in ("missing", "invalid", "future", "binding"):
            with self.subTest(fault=fault):
                if fault != "missing":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.bootstrap()
                self.publish(170., 22.6, 1.8, receipt=167., grid_at=169.)
                if fault == "missing":
                    self.states["sensor.grid"].state = "unavailable"
                elif fault == "invalid":
                    self.native.error = "native_sdk_current_invalid"
                elif fault == "future":
                    self.native.received_at = self.wall(180.)
                    self.native.observation_report_times = ((182, self.wall(180.)), (183, self.wall(180.)))
                else:
                    self.controller._configured_control_identity.return_value = "different-source"
                await self.controller.async_tick()
                self.assertEqual(self.controller.last_decision.current_p1, 0)
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_paused_status_never_uses_start_owner_to_resume(self):
        r = await self.bootstrap()
        self.publish(170., 22.6, 1.8, receipt=167., grid_at=169., status="paused")
        await self.controller.async_tick()
        self.assertLess(self.controller.last_decision.current_p1, 14)
        self.assertFalse(any(d.command == "resume" for d in self.adapter.calls))
        self.assertFalse(any(d.current_p1 == 14 for d in self.adapter.calls))

    async def test_true_raw_peak_and_unbuffered_house_override_start_owner(self):
        for raw in (24., 30.4, 38.01):
            with self.subTest(raw=raw):
                if raw != 24.:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.bootstrap()
                self.publish(170., raw, 1.8, receipt=167., grid_at=169.)
                await self.controller.async_tick()
                self.assertLess(self.controller.last_decision.current_p1, 14)
                self.assertTrue(self.adapter.calls)
                if raw > 28:
                    self.assertLessEqual(self.controller.last_decision.current_p1, 7)
                if raw > 38:
                    self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_discarded_pre_api_start_proposal_cannot_create_positive_owner(self):
        self.publish(100., 8.4, .012, status="awaiting_start")
        await self.controller.async_tick()
        self.adapter.calls.clear()
        save = self.controller._budget_store.async_save
        changed = False

        async def changed_before_dispatch(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.publish(160.05, 22.6, 1.8, receipt=160.01, grid_at=160.02)
            return result

        self.controller._budget_store.async_save = changed_before_dispatch
        self.publish(160., 8.4, .012, status="awaiting_start")
        await self.controller.async_tick()
        r = self.controller.regulator
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 0)
        self.assertIsNone(r._positive_cap)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.controller._budget_store.async_save = save
        self.now = 160.4
        await self.controller.async_tick()
        self.assertFalse(any(d.current_p1 == 14 for d in self.adapter.calls))
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.assertIsNone(r.automatic_cycle_diagnostics()["automatic_cycle_failure_reason"])


if __name__ == "__main__":
    unittest.main()
