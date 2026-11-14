"""Independent RAW-excursion contracts through the real controller, offline.

Clocks, Store files, native callbacks, MQTT packets and SDK acknowledgements
are synthetic. Public controller/submission paths create accepted authority;
no household data or live Home Assistant/charger I/O is used.
"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_meter_envelope_controller as meter
import test_1fuseloader_runtime as fixtures
import test_1fuseloader_b40_fixture_evidence as phase_fixture


class B49RawExcursionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = meter.MeterEnvelopeControllerTests.update_entry
    wall = meter.MeterEnvelopeControllerTests.wall
    schedule = meter.MeterEnvelopeControllerTests.schedule
    active_timers = meter.MeterEnvelopeControllerTests.active_timers
    set_native = meter.MeterEnvelopeControllerTests.set_native
    snapshot = meter.MeterEnvelopeControllerTests.snapshot
    install = meter.MeterEnvelopeControllerTests.install
    native_vector = meter.MeterEnvelopeControllerTests.native_vector
    configure = meter.MeterEnvelopeControllerTests.configure
    publish = meter.MeterEnvelopeControllerTests.publish
    tick = meter.MeterEnvelopeControllerTests.tick
    report = meter.MeterEnvelopeControllerTests.report
    execute = meter.MeterEnvelopeControllerTests.execute
    boot = meter.MeterEnvelopeControllerTests.boot
    settled_sixteen = meter.MeterEnvelopeControllerTests.settled_sixteen
    assert_typed_zero = meter.MeterEnvelopeControllerTests.assert_typed_zero
    assert_positive_only = meter.MeterEnvelopeControllerTests.assert_positive_only
    asyncTearDown = meter.MeterEnvelopeControllerTests.asyncTearDown

    async def asyncSetUp(self):
        await meter.MeterEnvelopeControllerTests.asyncSetUp(self)
        self.configure(overload_margin_amps=3)
        self.install_mqtt()

    def install_mqtt(self):
        async def configure_source():
            if self.controller._l1_source is None:
                config = fixtures.runtime.MqttL1Config('sensor.grid',
                    'synthetic/currently/one/state', 'I1', 'sec')
                source = fixtures.runtime.MqttL1Source(config,
                    monotonic=lambda: self.now, utc_now=lambda: self.wall(),
                    age_limit=lambda: 120., on_change=lambda: None)
                # Model the MQTT integration's connected callback. Actual
                # payload validation/clock handling still runs in receive().
                source._active = source._connected = True
                self.controller._l1_source = source
                self.controller._l1_source_config = config
                self.receive_grid(float(self.states['sensor.grid'].state))
            return True
        self.controller._async_configure_l1_source_locked = configure_source

    def receive_grid(self, value, reported=None):
        source = self.controller._l1_source
        if source is not None:
            source.receive(SimpleNamespace(topic=source.config.topic, retain=False,
                payload=json.dumps({'I1': value,
                    'sec': self.wall(reported).timestamp()})))

    def grid(self, value, reported=None):
        meter.MeterEnvelopeControllerTests.grid(self, value, reported)
        self.receive_grid(value, reported)

    async def sample(self, at, raw, current=None, receipt=None, *, grid_at=None,
                     status='charging'):
        if current is None:
            self.now = at
            self.grid(raw, grid_at)
            self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        else:
            self.publish(at, status, raw, current,
                receipt=at-.2 if receipt is None else receipt, grid_at=grid_at)
            self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        await self.tick()
        return self.controller.last_decision

    async def accepted_minimum(self):
        """Ordinary whole-RAW startup creates 7 A with configured max16."""
        r = self.controller.regulator
        for at in range(100, 166, 5):
            self.report(float(at), 'awaiting_start', 16., .02, 99.)
            await self.tick()
            if r.last_target and r.last_target[0] == 7:
                break
        self.assertEqual(r.settings.max_charge_amps, 16)
        self.assertEqual(r.last_target, (7, 0, 0, 60), self.controller.last_decision)
        self.assertEqual(r.last_set_limit_ts, 160.)
        await self.sample(165., 26.3, 7.128, 164.8)
        self.assertTrue(self.controller._read_input().controlled_l1.evidence_credit)
        self.assertEqual(r.last_target[0], 7)
        self.adapter.calls.clear()
        self.dispatches.clear()
        return r

    def deadline(self, expected):
        d = self.controller.last_decision
        self.assertAlmostEqual(d.meter_raw_excursion_deadline_ts, expected, places=5)
        self.assertTrue(any(abs(timer.due-expected) < 1e-5
                            for timer in self.active_timers()),
                        [(timer.due, timer.cancelled) for timer in self.active_timers()])

    async def test_already_verified_seven_keeps_fixed_ten_second_raw_tolerance(self):
        r = await self.accepted_minimum()
        for at in (170., 172., 175., 179.8):
            d = await self.sample(at, 31.9, 7.128, at-.2)
            self.assertEqual(d.current_p1, 7, d)
            self.assertIsNone(d.meter_zero_cause, d)
            self.deadline(180.)
            self.assertEqual(r.last_target[0], 7)
        self.assert_positive_only()
        await self.sample(180., 31.9, 7.128, 179.8)
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.last_decision.meter_zero_cause, 'grid_ceiling')
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assert_typed_zero()

    async def test_first_high_source_timestamp_not_later_evaluation_owns_deadline(self):
        await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 168.8, grid_at=169.)
        self.deadline(179.)
        await self.sample(178.9, 31.9, 7.128, 178.8)
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.deadline(179.)
        await self.sample(179., 31.9)
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_actual_deadline_callback_stops_without_an_extra_meter_packet(self):
        await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        await self.sample(175., 31.9, 7.128, 174.8)
        self.deadline(180.)
        timer = next(item for item in self.active_timers() if abs(item.due-180.) < 1e-5)
        self.controller._handle_source_change = (
            fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(
                self.controller, fixtures.runtime.OneFuseLoaderController))
        self.now = timer.due
        timer.cancelled = True
        await timer.callback(self.wall())
        task = self.controller._refresh_task
        self.assertIsNotNone(task)
        await task
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assert_typed_zero()

    async def test_old_high_packet_and_same_cap_sdk_renewal_cannot_move_deadline(self):
        r = await self.accepted_minimum()
        for at in (175., 185.):
            await self.sample(at, 26.3, 7.128, at-.2)
        self.assertEqual(r.last_set_limit_ts, 160.)
        await self.sample(188., 31.9, 7.128, 187.8)
        self.deadline(198.)
        await self.sample(190., 31.9, 7.128, 187.8)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.assertEqual(r.last_target[0], 7)
        self.assertGreaterEqual(r.last_set_limit_ts, 190.)
        self.deadline(198.)
        self.publish(194., 'charging', 31.9, 7.128, receipt=191., grid_at=190.)
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        await self.tick()
        self.deadline(198.)
        await self.sample(198., 31.9, 7.128, 197.8)
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def minimum_peak_near_renewal(self):
        r = await self.accepted_minimum()
        for at in (175., 185.):
            await self.sample(at, 26.3, 7.128, at-.2)
        await self.sample(188., 31.9, 7.128, 187.8)
        self.deadline(198.)
        self.assertEqual(r.last_set_limit_ts, 160.)
        self.adapter.calls.clear()
        return r

    async def test_queued_sdk_preflight_rejects_expired_raw_plan_without_source_revision_change(self):
        r = await self.minimum_peak_near_renewal()
        entered, release = asyncio.Event(), asyncio.Event()
        boundary_results = []
        accepted = r.last_target, r.last_set_limit_ts
        execute = self.adapter.async_execute_with_dispatch

        async def queued(decision, on_dispatch):
            entered.set()
            await release.wait()
            admitted = on_dispatch()
            boundary_results.append(admitted)
            if not admitted:
                raise fixtures.runtime.CommandNotSent('synthetic_queue_deadline')
            # A failing implementation would incorrectly enter the SDK here.
            self.adapter.calls.append(decision)
            return SimpleNamespace(http_status=200, correlation_ids=())

        self.adapter.async_execute_with_dispatch = queued
        self.report(190., 'charging', 31.9, 7.128, 187.8)
        running = asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            revision = self.controller._input_revision(self.controller._read_input())
            self.now = 198.001
            self.assertEqual(self.controller._input_revision(self.controller._read_input()), revision)
            release.set()
            await asyncio.wait_for(running, 1.)
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            self.adapter.async_execute_with_dispatch = execute
        self.assertEqual(boundary_results, [False])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual((r.last_target, r.last_set_limit_ts), accepted)
        # The unsent old plan is discarded; the owner now records protection,
        # without either a positive ACK or a replacement ten-second window.
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.last_decision.meter_zero_cause, 'grid_ceiling')
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_verified_store_await_cannot_dispatch_old_raw_plan_after_deadline(self):
        r = await self.minimum_peak_near_renewal()
        writer = self.controller._budget_store.store
        save = writer.async_save
        entered, release = asyncio.Event(), asyncio.Event()
        first = True

        async def gated(payload):
            nonlocal first
            if first:
                first = False
                entered.set()
                await release.wait()
            return await save(payload)

        writer.async_save = gated
        accepted = r.last_target, r.last_set_limit_ts
        self.report(190., 'charging', 31.9, 7.128, 187.8)
        running = asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            revision = self.controller._input_revision(self.controller._read_input())
            self.now = 198.001
            self.assertEqual(self.controller._input_revision(self.controller._read_input()), revision)
            release.set()
            await asyncio.wait_for(running, 1.)
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual((r.last_target, r.last_set_limit_ts), accepted)
        self.assertFalse(self.controller.last_decision.should_call_service)
        self.assertEqual(self.controller.last_service_result, 'not_sent')
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_pending_store_retains_only_old_seven_until_fixed_raw_deadline(self):
        r = await self.minimum_peak_near_renewal()
        writer = self.controller._budget_store.store
        save = writer.async_save
        entered, release = asyncio.Event(), asyncio.Event()
        first = True

        async def gated(payload):
            nonlocal first
            if first:
                first = False
                entered.set()
                await release.wait()
            return await save(payload)

        writer.async_save = gated
        accepted = r.last_target, r.last_set_limit_ts
        try:
            with patch.object(fixtures.runtime, 'EXPOSURE_STORE_TIMEOUT_SECONDS', .03):
                d = await self.sample(190., 31.9, 7.128, 187.8)
                self.assertTrue(entered.is_set())
                self.assertTrue(self.controller._budget_store.verification_pending)
                self.assertEqual(d.current_p1, 7, d)
                self.assertIsNone(d.meter_zero_cause)
                self.assertFalse(d.should_call_service)
                self.deadline(198.)
                self.assertEqual(self.adapter.calls, [])
                self.assertEqual((r.last_target, r.last_set_limit_ts), accepted)
                self.now = 194.
                await self.tick()
                self.assertEqual(self.controller.last_decision.current_p1, 7)
                self.assertFalse(self.controller.last_decision.should_call_service)
                self.deadline(198.)
                self.assertEqual((r.last_target, r.last_set_limit_ts), accepted)
                self.now = 198.
                await self.tick()
                self.assertEqual(self.controller.last_decision.current_p1, 0)
                self.assertEqual(self.controller.last_decision.meter_zero_cause, 'grid_ceiling')
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        finally:
            release.set()
            await self.controller._budget_store.async_wait_pending()

    async def test_dispatched_renewal_can_finish_after_resolved_raw_deadline(self):
        r = await self.minimum_peak_near_renewal()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execute = self.adapter.async_execute_with_dispatch

        async def awaiting_ack(decision, on_dispatch):
            receipt = await execute(decision, on_dispatch)
            if decision.current_p1 > 0:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return receipt

        self.adapter.async_execute_with_dispatch = awaiting_ack
        self.controller._handle_source_change = (
            fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(
                self.controller, fixtures.runtime.OneFuseLoaderController))
        self.report(197.8, 'charging', 31.9, 7.128, 187.8)
        running = asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            self.assertTrue(self.controller._dispatch_started)
            timer = next(item for item in self.active_timers() if abs(item.due-198.) < 1e-5)
            self.report(197.9, 'charging', 27.5, 7.128, 197.7)
            self.controller._handle_source_change(None)
            self.assertTrue(timer.cancelled)
            self.assertFalse(any(abs(item.due-198.) < 1e-5 for item in self.active_timers()))
            self.now = 198.1
            # More real time passes than the OLD 0.2s action timeout. The
            # fresh lower RAW retired that electrical deadline; it must not
            # manufacture an unknown-delivery failure for an admitted SDK call.
            await asyncio.sleep(.3)
            self.assertFalse(cancelled.is_set())
            self.assertFalse(running.done())
            release.set()
            await asyncio.wait_for(running, 1.)
            refresh = self.controller._refresh_task
            if refresh is not None:
                await asyncio.wait_for(refresh, 1.)
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            self.adapter.async_execute_with_dispatch = execute
        self.assertFalse(cancelled.is_set())
        self.assertTrue(self.adapter.calls)
        self.assertTrue(all(d.current_p1 == 7 for d in self.adapter.calls))
        self.assertEqual(r.last_target[0], 7)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.assertIsNone(self.controller.last_decision.meter_raw_excursion_deadline_ts)

    async def test_high_raw_deadline_still_preempts_already_dispatched_renewal(self):
        r = await self.minimum_peak_near_renewal()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execute = self.adapter.async_execute_with_dispatch

        async def awaiting_ack(decision, on_dispatch):
            receipt = await execute(decision, on_dispatch)
            if decision.current_p1 > 0:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return receipt

        self.adapter.async_execute_with_dispatch = awaiting_ack
        self.controller._handle_source_change = (
            fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(
                self.controller, fixtures.runtime.OneFuseLoaderController))
        self.report(197.8, 'charging', 31.9, 7.128, 187.8)
        running = asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            self.assertTrue(self.controller._dispatch_started)
            timer = next(item for item in self.active_timers() if abs(item.due-198.) < 1e-5)
            self.now = 198.
            timer.cancelled = True
            await timer.callback(self.wall())
            await asyncio.wait_for(running, 1.)
            refresh = self.controller._refresh_task
            if refresh is not None:
                await asyncio.wait_for(refresh, 1.)
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            self.adapter.async_execute_with_dispatch = execute
        self.assertTrue(cancelled.is_set())
        self.assertEqual(r.last_target[0], 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertTrue(self.controller.last_decision.meter_zero_cause)
        self.assert_typed_zero()

    async def test_first_high_raw_while_sdk_renewal_waits_schedules_and_enforces_its_deadline(self):
        r = await self.accepted_minimum()
        for at in (175., 185.):
            await self.sample(at, 26.3, 7.128, at-.2)
        self.assertIsNone(self.controller.last_decision.meter_raw_excursion_deadline_ts)
        self.adapter.calls.clear()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execute = self.adapter.async_execute_with_dispatch

        async def awaiting_ack(decision, on_dispatch):
            receipt = await execute(decision, on_dispatch)
            if decision.current_p1 > 0:
                self.assertIsNone(decision.meter_raw_excursion_deadline_ts)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return receipt

        self.adapter.async_execute_with_dispatch = awaiting_ack
        self.controller._handle_source_change = (
            fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(
                self.controller, fixtures.runtime.OneFuseLoaderController))
        self.report(190., 'charging', 26.3, 7.128, 187.8)
        running = asyncio.create_task(self.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            self.assertTrue(self.controller._dispatch_started)
            self.assertFalse(any(abs(item.due-205.) < 1e-5 for item in self.active_timers()))
            # The real source callback observes the FIRST new high RAW while
            # the SDK holds the controller lock. No locked tick can run yet.
            self.report(195., 'charging', 31.9, 7.128, 187.8)
            self.controller._handle_source_change(None)
            timer = next(item for item in self.active_timers() if abs(item.due-205.) < 1e-5)
            self.assertFalse(cancelled.is_set())
            self.assertFalse(running.done())
            self.assertEqual(r.last_set_limit_ts, 160.)
            # No second high packet or completed ACK is needed for protection.
            self.now = timer.due
            timer.cancelled = True
            await timer.callback(self.wall())
            await asyncio.wait_for(running, 1.)
            refresh = self.controller._refresh_task
            if refresh is not None:
                await asyncio.wait_for(refresh, 1.)
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
            self.adapter.async_execute_with_dispatch = execute
        self.assertTrue(cancelled.is_set())
        self.assertEqual(r.last_target[0], 0)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertTrue(self.controller.last_decision.meter_zero_cause)
        self.assert_typed_zero()

    async def test_later_below_ceiling_packet_ends_only_excursion_not_exposure_budget(self):
        r = await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        self.deadline(180.)
        before = r.export_overload_state()['automatic_cycle']['used_seconds']
        d = await self.sample(174., 27.5, 7.128, 173.8)
        self.assertIsNone(d.meter_zero_cause)
        self.assertIsNone(d.meter_raw_excursion_deadline_ts)
        self.assertGreaterEqual(r.export_overload_state()['automatic_cycle']['used_seconds'], before)
        await self.sample(176., 31.9, 7.128, 175.8)
        self.deadline(186.)
        await self.sample(180.1, 27.5, 7.128, 179.8)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.assertIsNone(self.controller.last_decision.meter_raw_excursion_deadline_ts)
        self.assert_positive_only()

    async def test_newly_achieved_minimum_does_not_immediately_zero_next_high_grid(self):
        r = await self.settled_sixteen()
        d = await self.sample(150., 35., 16.2, 149.8)
        self.assertEqual(r.last_target[0], 7, d)
        self.assertAlmostEqual(d.nominal_response_deadline_ts, 160.)
        self.deadline(160.)
        d = await self.sample(152., 35., 7.128, 151.8)
        self.assertEqual(d.current_p1, 7, d)
        self.assertIsNone(d.meter_zero_cause)
        self.deadline(160.)
        await self.sample(155., 29.1, 7.128, 154.8)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.deadline(160.)
        await self.sample(159.5, 27.8, 7.128, 159.3)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.assertIsNone(self.controller.last_decision.meter_raw_excursion_deadline_ts)
        self.assert_positive_only()

    async def test_above_hard_limit_or_critical_actuator_fault_preempts_raw_tolerance(self):
        for fault in ('hard', 'actuator'):
            with self.subTest(fault=fault):
                if fault != 'hard':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.accepted_minimum()
                await self.sample(170., 31.9, 7.128, 169.8)
                self.adapter.calls.clear()
                if fault == 'actuator':
                    self.adapter.telemetry_block_reason = 'synthetic_transport_down'
                await self.sample(172., 38.01 if fault == 'hard' else 31.9,
                                  7.128, 171.8)
                self.assertEqual(self.controller.last_decision.current_p1, 0)
                self.assertTrue(self.controller.last_decision.meter_zero_cause)
                self.assertEqual(self.adapter.calls[-1].current_p1, 0)
                self.assert_typed_zero()

    async def test_invalid_old_mqtt_sample_cannot_buy_new_raw_tolerance(self):
        await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        self.report(175., 'charging', 27.5, 7.128, 174.8)
        self.grid(27.5, 169.)
        await self.tick()
        d = self.controller.last_decision
        self.assertEqual(d.current_p1, 0)
        self.assertTrue(d.meter_zero_cause)
        self.assert_typed_zero()

    async def test_expired_actual_minimum_lease_cannot_gain_raw_excursion_permission(self):
        r = await self.accepted_minimum()
        expiry = r.last_set_limit_ts+r.last_target[3]
        await self.sample(expiry+.1, 31.9, 7.128, expiry-.1)
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.last_decision.meter_zero_cause, 'lease_expired')
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))
        self.assert_typed_zero()

    async def test_grid_fault_with_live_minimum_preempts_unexpired_raw_window(self):
        await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        self.adapter.calls.clear()
        self.now = 172.
        self.grid('unavailable')
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.last_decision.meter_zero_cause, 'grid_invalid')
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assert_typed_zero()

    async def test_permanent_store_failure_cannot_retain_raw_excursion_authority(self):
        await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        old = self.controller._budget_store
        old.store.failure = True
        self.controller._budget_store = type(old)(old.store, clock=old.clock,
                                                read_back=old.store.async_load)
        self.adapter.calls.clear()
        await self.sample(172., 31.9, 7.128, 171.8)
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertEqual(self.controller.last_decision.meter_zero_cause,
                         fixtures.logic.ElectricalStopCause.PERSISTENCE.value)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assert_typed_zero()

    async def test_active_excursion_store_and_new_process_cannot_refill_ten_seconds(self):
        r = await self.accepted_minimum()
        await self.sample(170., 31.9, 7.128, 169.8)
        before = deepcopy(r.export_overload_state())
        self.assertTrue(before['meter_control']['raw_excursion_active'])
        await self.controller.async_stop()
        disk = deepcopy(self.hass._test_store_disk)
        self.epoch += self.now + .5
        self.now = 0.
        self.timers = []
        self.hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: self.now),
            states=SimpleNamespace(get=self.states.get), async_create_task=asyncio.create_task,
            config_entries=SimpleNamespace(async_update_entry=self.update_entry),
            data={}, _test_store_disk=disk)
        self.adapter = fixtures.FakeAdapter()
        self.adapter.async_read_current_snapshot = None
        self.adapter.offline_limits_verified_zero = True
        self.adapter.async_execute_with_dispatch = self.execute
        self.adapter.reported_limit_receipts = (None, None, None)
        self.owner = tuple(value+100 for value in self.owner)
        self.adapter.observation_clock_identity = self.owner
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: self.epoch+self.now
        self.controller._configured_control_identity = Mock(return_value='synthetic-selected-source')
        self.controller._handle_source_change = Mock()
        phase_fixture.install(self, adopted=-1.)
        self.install_mqtt()
        self.sequence = 0
        self.dispatch_failure = None
        self.dispatches = []
        await self.sample(0., 31.9, 7.128, -.1)
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertEqual(self.controller.last_decision.current_p1, 0)
        self.assertTrue(self.controller.last_decision.meter_zero_cause)
        after = self.controller.regulator.export_overload_state()
        self.assertGreaterEqual(after['automatic_cycle']['used_seconds'],
                                before['automatic_cycle']['used_seconds'])


if __name__ == '__main__':
    unittest.main()
