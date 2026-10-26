"""Opt-in disconnected readiness through actual controller boundaries.

Sources, clocks, storage and cloud replies are synthetic. Ready authority is
created only by a real fixture adapter acknowledgement, never private proof
seeding. These tests perform no Home Assistant or charger network actions.
"""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_1fuseloader_runtime as fixtures
import test_1fuseloader_zero_cohort_runtime as cohorts
from test_1fuseloader_ha_current_representation import float32


class DisconnectedReadinessRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
        self.adapter.async_read_current_snapshot = None
        self.adapter.offline_limits_verified_zero = True
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.sequence = 0

    def configure(self, enabled=True, **extra):
        options = dict(nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=True,
            overload_enabled=True, disconnected_readiness=enabled,
            grid_confirmation_seconds=30, enable_soft_guard=False,
            enable_pause_resume=False, **extra)
        self.entry.options.update(options)
        self.controller.regulator.update_settings(replace(self.controller.settings, **options))

    def publish(self, at, status, grid, current, *, receipt=90., grid_at=None, changed=None):
        self.now = at
        previous = self.states["sensor.status"].state
        self.states["sensor.status"].state = status
        if previous != status or changed is not None:
            self.states["sensor.status"].last_changed = self.wall(at-.4 if changed is None else changed)
        value = float32(current)
        cached = getattr(self, "native", None)
        same_cache = (cached is not None and cached.current == value
            and cached.received_at == (None if receipt is None else self.wall(receipt)))
        if same_cache:
            # Status/grid fan-out is not a new current observation. Keep the
            # actual conductor sequence and native receiver clock unchanged.
            cached.error = None
            self.states["sensor.car"].last_reported = self.wall()
        elif receipt is None:
            self.native_vector(value, (182, 183), {}, {}, at-1.)
        else:
            self.sequence += 1
            self.native_vector(value, (182, 183),
                {182: receipt, 183: receipt},
                {182: self.sequence, 183: self.sequence}, receipt)
        self.grid(grid, at if grid_at is None else grid_at)

    async def tick(self):
        if not self.controller._budget_loaded:
            # Exercise the actual public ledger-load/startup boundary on the
            # first tick, including the pending-verification permission gate.
            await self.controller.async_start()
        else:
            await self.controller.async_tick()

    async def ready(self, raw=5., current=16.):
        self.configure()
        self.publish(100., "disconnected", raw, current)
        await self.tick()
        cap = min(16, int(23.-raw))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [cap])
        self.assertEqual(self.adapter.calls[-1].mode, "disconnected_ready")
        self.assertEqual(self.controller.regulator.last_target[0], cap)
        return self.controller.regulator

    def assert_no_overload_action(self):
        for decision in self.adapter.calls:
            self.assertEqual((decision.command, decision.current_p2, decision.current_p3),
                ("set_limit", 0, 0))
            self.assertGreater(decision.ttl_seconds, 0)
            self.assertLessEqual(decision.ttl_seconds, 60)
        state = self.controller.regulator.export_overload_state()
        self.assertNotIn("trial_epoch", state)
        self.assertEqual(state["used_seconds"], 0)
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0)

    async def test_disconnected_real_ack_uses_whole_raw_bound_not_stale_ev_subtraction(self):
        await self.ready(raw=10., current=16.)
        self.assertEqual(self.adapter.calls[-1].current_p1, 13)
        self.assertLessEqual(10.+self.adapter.calls[-1].current_p1, 23.)
        self.assert_no_overload_action()

    async def test_disconnected_readiness_renews_finite_offer_beyond_one_minute(self):
        r = await self.ready()
        for at in (135., 170., 205.):
            self.now = at
            self.grid(5.)
            await self.tick()
            self.assertEqual(r.last_target[0], 16)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16, 16, 16, 16])
        self.assertEqual(r.last_set_limit_ts, 205.)
        self.assert_no_overload_action()

    async def test_configured_maximum_still_limits_ready_offer(self):
        self.configure(max_charge_amps=7)
        self.publish(100., "disconnected", 5., 16.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assert_no_overload_action()

    async def test_cold_capacity_below_configured_startup_current_is_not_prearmed(self):
        self.configure()
        self.publish(100., "disconnected", 17., 16.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))

    async def test_ready_owner_is_memory_only_and_does_not_reset_historical_exposure(self):
        self.configure()
        r = self.controller.regulator
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=4000.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        await self.controller._budget_store.async_save(r.export_overload_state())
        self.publish(100., "disconnected", 5., 16.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertIsNotNone(r._ready_owner)
        state = r.export_overload_state()
        self.assertEqual(state["used_seconds"], 4000.)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertNotIn("ready_owner", state)
        self.assertNotIn("disconnected_ready_owner", state)
        self.assertNotIn("trial_epoch", state)
        await self.controller.async_stop()
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.controller._configured_control_identity = Mock(return_value="synthetic-selected-source")
        self.controller._handle_source_change = Mock()
        loaded = await self.controller._budget_store.async_load()
        self.assertTrue(self.controller.regulator.import_overload_state(loaded))
        self.assertIsNone(self.controller.regulator._ready_owner)
        self.assertEqual(self.controller.regulator.export_overload_state()["used_seconds"], 4000.)

    async def test_ready_connect_zero_partial_full_avoids_zero_and_old_ack_response_clock(self):
        r = await self.ready()
        for at in (135., 170., 205.):
            self.now = at
            self.grid(5.)
            await self.tick()
        self.adapter.calls.clear()
        self.publish(210., "awaiting_start", 5., .012, receipt=209.8, changed=209.6)
        await self.tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, 205.)
        self.assertAlmostEqual(r._positive_ev_ts, self.wall(209.8).timestamp(), places=6)
        self.assertGreaterEqual(r._ev_owner_clocks["_positive_ev_ts"]["wall_floor"],
            self.wall(209.6).timestamp())
        self.publish(220., "charging", 21., 1.8, receipt=217., grid_at=219., changed=216.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertEqual(self.adapter.calls, [])
        self.assertAlmostEqual(self.controller.last_decision.positive_response_deadline_ts, 242., places=6)
        self.publish(226., "charging", 21.1, 16.1, receipt=224., grid_at=225.)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertIsNone(r._positive_cap)

    async def test_quick_unplug_and_replug_with_cached_charging_draw_never_sends_zero(self):
        self.configure()
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 5., .012, receipt=None)
            await self.tick()
        self.assertEqual(self.controller.regulator.last_target[0], 16)
        self.publish(162.5, "charging", 21.1, 16.1, receipt=162., changed=161.5)
        await self.tick()
        self.adapter.calls.clear()
        self.publish(195., "disconnected", 5., 16.1, receipt=162., changed=194.5)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertEqual(self.adapter.calls[0].mode, "disconnected_ready")
        self.adapter.calls.clear()
        self.publish(196., "charging", 21., 16.1, receipt=162., changed=195.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.controller.regulator._restart_required)

    async def test_live_ready_owner_survives_awaiting_then_charging_with_cached_old_draw(self):
        r = await self.ready()
        self.adapter.calls.clear()
        self.publish(105., "awaiting_start", 21., 16., receipt=90., changed=104.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16,
            self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(r._positive_response_ts)
        deadline = self.controller.last_decision.readiness_deadline_ts
        self.assertEqual(self.controller.last_decision.mode, "readiness_wait")
        self.assertAlmostEqual(deadline, 130., places=6)
        self.assertTrue(any(abs(item.due-deadline) < 1e-6 for item in self.active_timers()))
        self.publish(106., "charging", 21., 16., receipt=90., changed=105.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16,
            self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(r._positive_response_ts)
        self.assertFalse(r._restart_required)
        inherited = (self.controller.last_decision.positive_response_deadline_ts
            or self.controller.last_decision.readiness_deadline_ts)
        self.assertEqual(inherited, deadline)
        self.assertEqual(r.last_set_limit_ts, 100.)
        self.assertEqual((self.controller.last_decision.command,
            self.controller.last_decision.should_call_service), (None, False))

    async def test_waiting_then_charging_cannot_extend_original_connected_mismatch_window(self):
        r = await self.ready()
        self.adapter.calls.clear()
        self.publish(105., "awaiting_start", 21., 16., receipt=90., changed=104.5)
        await self.tick()
        deadline = self.controller.last_decision.readiness_deadline_ts
        self.assertAlmostEqual(deadline, 130., places=6)
        self.publish(106., "charging", 21., 16., receipt=90., changed=105.5)
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.publish(deadline, "charging", 21., 16., receipt=90.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(r._ready_owner)

    async def test_runtime_hard_no_write_blocks_forged_positive_wait_action(self):
        r = await self.ready()
        self.adapter.calls.clear()
        evaluate = r.evaluate

        def forged(inp):
            decision = evaluate(inp)
            if decision.mode == "readiness_wait":
                return replace(decision, command="resume", service="easee.synthetic_forbidden",
                    should_call_service=True)
            return decision

        r.evaluate = forged
        self.publish(105., "awaiting_start", 21., 16., receipt=90., changed=104.5)
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "readiness_wait")
        self.assertEqual((self.controller.last_decision.command,
            self.controller.last_decision.service, self.controller.last_decision.should_call_service),
            (None, None, False))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, 100.)

    async def test_disconnected_high_raw_cannot_use_old_or_fresh_ev_as_house_subtraction(self):
        for receipt in (90., 104.):
            with self.subTest(native_receipt=receipt):
                if receipt == 104.:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.ready()
                self.adapter.calls.clear()
                # Even an actual post-ACK numeric receipt cannot prove which
                # physical connection episode the DISCONNECTED status denotes.
                # Without a status source clock,21+16 cannot be an idle normal
                # whole-house offer. This protective limitation is intentional.
                self.publish(105., "disconnected", 21., 16., receipt=receipt)
                await self.tick()
                self.assertEqual(self.controller.last_decision.current_p1, 0)
                self.assertNotEqual(self.controller.last_decision.mode, "readiness_wait")
                self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))
                self.assertIsNone(r._ready_owner)

    async def test_default_off_dry_run_and_inactive_control_create_no_ready_ack(self):
        for mode in ("disabled", "dry", "inactive"):
            with self.subTest(mode=mode):
                if mode != "disabled":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.configure(mode != "disabled", **({"dry_run": True} if mode == "dry"
                    else {"active_control": False} if mode == "inactive" else {}))
                self.publish(100., "disconnected", 5., 16.)
                await self.tick()
                self.assertEqual(self.adapter.calls, [])
                self.assertIsNone(self.controller.regulator.last_target)

    async def test_completed_and_external_hold_statuses_never_publish_or_resume(self):
        for status in ("completed", "paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(status=status):
                if status != "completed":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.configure()
                self.publish(100., status, 5., 16.)
                await self.tick()
                self.assertFalse(any(d.current_p1 > 0 or d.command == "resume" for d in self.adapter.calls))

    async def test_expired_ready_lease_or_changed_binding_cannot_be_adopted(self):
        for invalid in ("expired", "binding"):
            with self.subTest(invalid=invalid):
                if invalid == "binding":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.ready()
                self.adapter.calls.clear()
                at = 100.+r.last_target[3]+.3 if invalid == "expired" else 101.
                self.publish(at, "charging", 21., 16., receipt=90., changed=at-.2)
                if invalid == "binding":
                    self.controller._configured_control_identity.return_value = "other-source"
                await self.tick()
                self.assertFalse(any(d.current_p1 == 16 for d in self.adapter.calls))
                self.assertLess(self.controller.last_decision.current_p1, 16)

    async def test_source_fault_and_stale_grid_never_publish_positive_ready_limit(self):
        for invalid in ("native", "grid", "stale", "peak"):
            with self.subTest(invalid=invalid):
                if invalid != "native":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.configure()
                self.publish(100., "disconnected", 5., 16.,
                    grid_at=-1. if invalid == "stale" else 100.)
                if invalid == "native":
                    self.native.error = "native_sdk_current_invalid"
                elif invalid == "grid":
                    self.states["sensor.grid"].state = "unavailable"
                elif invalid == "peak":
                    self.grid(38.01)
                await self.tick()
                self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))

    async def test_recent_real_source_fault_is_not_bypassed_by_disconnected_readiness(self):
        self.configure()
        self.publish(100., "disconnected", 5., 16.)
        self.native.error = "native_sdk_current_invalid"
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.adapter.calls.clear()
        self.publish(101., "disconnected", 5., 16., receipt=90.)
        await self.tick()
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))

    async def test_discarded_pre_api_readiness_is_not_adopted_as_an_ack(self):
        self.configure()
        save = self.controller._budget_store.async_save
        changed = False

        async def new_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 100.05
                self.grid(5.1)
            return result

        self.controller._budget_store.async_save = new_grid
        self.publish(100., "disconnected", 5., 16.)
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(self.controller.regulator.last_target)
        self.controller._budget_store.async_save = save
        self.publish(101., "charging", 21., 16., receipt=90., changed=100.5)
        await self.tick()
        self.assertFalse(any(d.current_p1 == 16 for d in self.adapter.calls))

    async def test_observed_fresh_connect_zero_without_ready_ack_can_submit_real_safe_offer(self):
        self.configure()
        save = self.controller._budget_store.async_save
        changed = False

        async def new_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 100.05
                self.grid(5.1)
            return result

        self.controller._budget_store.async_save = new_grid
        self.publish(100., "disconnected", 5., 16.)
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.controller._budget_store.async_save = save
        self.publish(101., "awaiting_start", 5., .012, receipt=100.7, changed=100.5)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertEqual(self.controller.regulator.last_set_limit_ts, 101.)

    async def test_fresh_connect_without_ack_still_honors_current_tuple_quiet_gate(self):
        self.configure()
        save = self.controller._budget_store.async_save
        changed = False

        async def new_grid(state):
            nonlocal changed
            result = await save(state)
            if not changed:
                changed = True
                self.now = 100.05
                self.grid(5.1)
            return result

        self.controller._budget_store.async_save = new_grid
        self.publish(100., "disconnected", 5., 16.)
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.controller._budget_store.async_save = save
        self.publish(101., "awaiting_start", 5., .012, receipt=100.8, changed=100.5)
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertIsNone(self.controller.regulator.last_target)
        self.now = 101.1
        self.grid(5.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertEqual(self.controller.regulator.last_set_limit_ts, 101.1)

    async def test_generation_change_before_api_cannot_create_ready_owner(self):
        self.configure()
        entered, release = asyncio.Event(), asyncio.Event()
        save = self.controller._budget_store.async_save

        async def wait(state):
            entered.set()
            await release.wait()
            return await save(state)

        self.controller._budget_store.async_save = wait
        self.publish(100., "disconnected", 5., 16.)
        tick = asyncio.create_task(self.tick())
        await asyncio.wait_for(entered.wait(), 2.)
        toggle = asyncio.create_task(self.controller.async_set_switch("disconnected_readiness", False))
        await asyncio.sleep(0)
        release.set()
        await tick
        await toggle
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.controller.settings.disconnected_readiness)
        self.assertIsNone(self.controller.regulator.last_target)

    def slow_first_checkpoint(self, *, fail=False):
        writer = self.controller._budget_store.store
        save = writer.async_save
        first = True

        async def slow(payload):
            nonlocal first
            if first:
                first = False
                # An actual unfinished public Store transaction, not a fake
                # pending flag. Control/source readings use the virtual clock.
                await asyncio.sleep(2.5)
                if fail:
                    raise OSError("synthetic permanent Store failure")
            return await save(payload)

        writer.async_save = slow

    async def test_real_slow_checkpoint_allows_only_loaded_raw_bounded_cold_prearm(self):
        self.configure()
        self.slow_first_checkpoint()
        self.publish(100., "disconnected", 5., 16.)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._budget_loaded)
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.controller._budget_error, "overload_budget_save_pending")
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.now = 100.1
        self.grid(5.)
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assertEqual(self.controller.regulator.last_set_limit_ts, 100.)
        await self.controller._budget_store.async_wait_pending()
        self.now = 100.2
        self.grid(5.)
        await self.tick()
        self.assertIsNone(self.controller._budget_error)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.assert_no_overload_action()

    async def test_permanent_checkpoint_failure_after_pending_prearm_withdraws_ready_owner(self):
        self.configure()
        self.slow_first_checkpoint(fail=True)
        self.publish(100., "disconnected", 5., 16.)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        await self.controller._budget_store.async_wait_pending()
        self.now = 100.1
        self.grid(5.)
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertIsNone(self.controller.regulator._ready_owner)
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")

    async def test_source_fault_still_withdraws_ready_offer_while_checkpoint_is_pending(self):
        self.configure()
        self.slow_first_checkpoint()
        self.publish(100., "disconnected", 5., 16.)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [16])
        self.now = 100.1
        self.grid(5.)
        self.native.error = "native_sdk_current_invalid"
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertIsNone(self.controller.regulator._ready_owner)
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls[1:]))
        await self.controller._budget_store.async_wait_pending()

    async def test_pending_connected_wait_validates_latest_native_clock_after_store_await(self):
        r = await self.ready()
        self.adapter.calls.clear()
        self.slow_first_checkpoint()
        self.publish(105., "awaiting_start", 21., 16., receipt=90., changed=104.5)

        async def future_receiver_clock():
            await asyncio.sleep(.05)
            self.native.received_at = self.wall(111.)
            self.native.observation_report_times = ((182, self.wall(111.)), (183, self.wall(111.)))
            # The monotonic tuple-change clock remains valid. This isolates
            # core receiver/vector validation, not the earlier UI watchdog.

        changed = asyncio.create_task(future_receiver_clock())
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        await changed
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNone(r._ready_owner)
        await self.controller._budget_store.async_wait_pending()

    async def test_inflight_repeated_disconnected_and_safe_connect_do_not_cancel_ready_dispatch(self):
        self.configure()
        self.adapter.gate = asyncio.Event()
        self.publish(100., "disconnected", 5., 16.)
        tick = asyncio.create_task(self.tick())
        await asyncio.wait_for(self.adapter.entered.wait(), 2.)
        try:
            self.now = 100.1
            self.grid(5.)
            fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.sleep(0)
            repeated_cancelled = self.controller._dispatch_superseded
            self.publish(100.3, "awaiting_start", 5., .012, receipt=100.2, changed=100.15)
            fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.sleep(0)
            connected_cancelled = self.controller._dispatch_superseded
        finally:
            self.adapter.gate.set()
            await tick
            if self.controller._refresh_task is not None:
                await self.controller._refresh_task
        self.assertFalse(repeated_cancelled)
        self.assertFalse(connected_cancelled)
        self.assertTrue(all(d.current_p1 == 16 for d in self.adapter.calls))
        self.assertEqual(self.controller.regulator.last_target[0], 16)

    async def test_inflight_fault_peak_manual_hold_completion_and_rebind_still_cancel(self):
        for invalid in ("fault", "peak", "paused", "completed", "binding", "disconnected_house", "future_clock"):
            with self.subTest(invalid=invalid):
                if invalid != "fault":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.configure()
                self.adapter.gate = asyncio.Event()
                self.publish(100., "disconnected", 5., 16.)
                tick = asyncio.create_task(self.tick())
                await asyncio.wait_for(self.adapter.entered.wait(), 2.)
                try:
                    self.now = 100.1
                    self.grid(5.)
                    if invalid == "fault":
                        self.native.error = "native_sdk_current_invalid"
                    elif invalid == "peak":
                        self.grid(38.01)
                    elif invalid == "binding":
                        self.controller._configured_control_identity.return_value = "different-source"
                    elif invalid == "disconnected_house":
                        self.grid(10.)
                    elif invalid == "future_clock":
                        self.native.received_at = self.wall(106.1)
                        self.native.observation_report_times = ((182, self.wall(106.1)), (183, self.wall(106.1)))
                    else:
                        self.states["sensor.status"].state = invalid
                        self.states["sensor.status"].last_changed = self.wall()
                    fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
                    await asyncio.sleep(0)
                    superseded = self.controller._dispatch_superseded
                finally:
                    self.adapter.gate.set()
                    await tick
                    if self.controller._refresh_task is not None:
                        await self.controller._refresh_task
                self.assertTrue(superseded)
                self.assertIsNone(self.controller.regulator._ready_owner)
                self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls[1:]))


if __name__ == "__main__":
    unittest.main()
