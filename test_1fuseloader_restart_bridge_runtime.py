"""Graceful restart crosses real controller/Store/dispatch boundaries offline.

Synthetic source packets and public fake Store files model a separate HA boot.
No live Home Assistant, vehicle, network or household history is accessed.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_meter_envelope_controller as meter
import test_1fuseloader_runtime as fixtures
import test_1fuseloader_b40_fixture_evidence as phase_fixture


class ShutdownJob:
    """The public HassJob wrapper exposes its target to the fake Core loop."""
    def __init__(self, target, *args, **kwargs):
        self.target = target


class RestartBridgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
    sample = meter.MeterEnvelopeControllerTests.sample
    boot = meter.MeterEnvelopeControllerTests.boot
    settled_sixteen = meter.MeterEnvelopeControllerTests.settled_sixteen

    async def asyncSetUp(self):
        await meter.MeterEnvelopeControllerTests.asyncSetUp(self)
        self.shutdown_jobs = []
        self.shutdown_removals = 0
        self.old_controllers = []
        self.hass.state = SimpleNamespace(name='running')
        self.hass.async_add_shutdown_job = self.add_shutdown_job
        core = ModuleType('homeassistant.core')
        core.HassJob = ShutdownJob
        core.callback = lambda target: target
        self.core_patch = patch.dict(sys.modules, {**fixtures.stubs,
            'homeassistant.core': core})
        self.core_patch.start()
        self.configure(graceful_restart_bridge=True)
        self.mqtt_initial_wait = False
        self.bridge_reply = True
        self.bridge_response_tasks = []
        self.install_mqtt_bootstrap()

    async def asyncTearDown(self):
        try:
            await meter.MeterEnvelopeControllerTests.asyncTearDown(self)
            for controller in self.old_controllers:
                await controller.async_stop()
        finally:
            for task in self.bridge_response_tasks:
                if not task.done():
                    task.cancel()
            if self.bridge_response_tasks:
                await asyncio.gather(*self.bridge_response_tasks, return_exceptions=True)
            self.core_patch.stop()

    def new_mqtt_source(self):
        config = fixtures.runtime.MqttL1Config('sensor.grid',
            'currently/one/001122aabbcc/state', 'I1', 'sec')
        source = fixtures.runtime.MqttL1Source(config,
            monotonic=lambda: self.now, utc_now=lambda: self.wall(),
            age_limit=lambda: 120., on_change=lambda: None)
        source._active = source._connected = True
        return source

    def receive_grid(self, value, *, retained=False):
        source = self.controller._l1_source
        if source is not None:
            source.receive(SimpleNamespace(topic=source.config.topic, retain=retained,
                payload=json.dumps({'I1': value, 'sec': self.wall().timestamp()})))

    def grid(self, value, reported=None):
        meter.MeterEnvelopeControllerTests.grid(self, value, reported)
        self.receive_grid(value)

    def install_mqtt_bootstrap(self):
        async def configure_source():
            if self.controller._l1_source is None:
                source = self.new_mqtt_source()
                self.controller._l1_source = source
                self.controller._l1_source_config = source.config
                if not self.mqtt_initial_wait:
                    self.receive_grid(float(self.states['sensor.grid'].state))
            return True
        self.controller._async_configure_l1_source_locked = configure_source

    async def execute(self, decision, on_dispatch):
        receipt = await meter.MeterEnvelopeControllerTests.execute(self, decision, on_dispatch)
        if decision.current_p1 == 7 and decision.ttl_seconds == 300 and self.bridge_reply:
            async def physical_response():
                # Real fixture native events follow actual SDK dispatch; a
                # later independent grid packet supplies coherent feedback.
                await asyncio.sleep(0)
                at = self.now + .2
                self.report(at, 'charging', 11., 7., at-.1)
                await asyncio.sleep(0)
                self.now += .2
                self.grid(11.)
            task = asyncio.create_task(physical_response())
            self.bridge_response_tasks.append(task)
        return receipt

    def add_shutdown_job(self, job, *args):
        item = SimpleNamespace(job=job, args=args, removed=False)
        self.shutdown_jobs.append(item)
        def remove():
            if not item.removed:
                item.removed = True
                self.shutdown_removals += 1
        return remove

    async def stage_one_shutdown(self):
        """Core stage one is before integration stop/MQTT disconnection."""
        self.assertEqual(self.hass.state.name, 'running')
        active = [item for item in self.shutdown_jobs if not item.removed]
        self.assertEqual(len(active), 1)
        target = getattr(active[0].job, 'target', active[0].job)
        await target(*active[0].args)

    async def healthy_session(self):
        regulator = await self.settled_sixteen()
        # Let the original cold-start possible-authority horizon retire; a
        # merely NORMAL label before that horizon is not shutdown permission.
        await self.sample(170., 20., 16., 169.)
        self.assertEqual(regulator.last_target[0], 16)
        self.assertEqual(regulator._get_meter_envelope_policy().state.value, 'NORMAL')
        self.adapter.calls.clear()
        self.dispatches.clear()
        return regulator

    def new_boot(self, downtime=20., *, native_wait=False):
        """New Core object, native receiver, monotonic epoch and Store gate."""
        old = self.controller
        disk = deepcopy(self.hass._test_store_disk)
        self.old_controllers.append(old)
        self.epoch += self.now + downtime
        self.now = 0.
        self.timers = []
        self.shutdown_jobs = []
        self.hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: self.now),
            states=SimpleNamespace(get=self.states.get), async_create_task=asyncio.create_task,
            config_entries=SimpleNamespace(async_update_entry=self.update_entry),
            data={}, _test_store_disk=disk, state=SimpleNamespace(name='running'),
            async_add_shutdown_job=self.add_shutdown_job)
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
        self.install_mqtt_bootstrap()
        self.mqtt_initial_wait = True
        self.sequence = 0
        self.dispatch_failure = None
        self.dispatches = []
        # An actual new native report is separate from the absent first grid
        # message. It must not manufacture either a lease or a grid receipt.
        self.report(0., 'charging', 11., 7., -.1)
        if native_wait:
            self.adapter.telemetry_block_reason = 'easee_stream_disconnected'
            self.adapter.transport_block_reason = 'easee_stream_disconnected'
            self.adapter._product_data = SimpleNamespace(state={
                'isOnline': False, 'signalRConnected': False})
            self.native_vector(7., (182, 183), {}, {}, None)

    async def prepared_restart(self, *, downtime=20., native_wait=False):
        regulator = await self.healthy_session()
        before = deepcopy(regulator.export_overload_state())
        await self.stage_one_shutdown()
        _, marker = self.persisted_bridge()
        self.new_boot(downtime=downtime, native_wait=native_wait)
        await self.controller.async_start()
        return before, marker

    def persisted_bridge(self):
        values = [(key, deepcopy(value)) for key, value in self.hass._test_store_disk.items()
                  if 'restart' in key]
        self.assertEqual(len(values), 1, values)
        return values[0]

    async def test_stage_one_prepares_verified_seven_amp_finite_handoff(self):
        regulator = await self.healthy_session()
        before = deepcopy(regulator.export_overload_state())
        await self.stage_one_shutdown()
        self.assertTrue(self.controller._stopped, self.controller._restart_bridge.diagnostics())
        self.assertEqual([(d.command, d.current_p1, d.current_p2, d.current_p3, d.ttl_seconds)
                          for d in self.adapter.calls], [('set_limit', 7, 0, 0, 300)])
        _, marker = self.persisted_bridge()
        self.assertIn('acknowledged', str(marker))
        after = regulator.export_overload_state()
        self.assertEqual(after['used_seconds'], before['used_seconds'])
        self.assertEqual(after['failure_reason'], before['failure_reason'])
        self.assertEqual(after['automatic_cycle']['cycle_id'], before['automatic_cycle']['cycle_id'])
        self.assertEqual(after['automatic_cycle']['used_seconds'], before['automatic_cycle']['used_seconds'])

    async def test_unchanged_old_ev_event_does_not_block_observed_shutdown_reduction(self):
        regulator = await self.healthy_session()
        original_receipt = self.native.received_at
        for at in range(175, 351, 5):
            # Fresh meter and transport/renewal records accompany an unchanged
            # original vehicle event. They never retime that EV observation.
            await self.sample(float(at), 20.)
        self.assertEqual(self.native.received_at, original_receipt)
        self.assertIsNone(regulator._controlled_l1_projection(self.controller._read_input()))
        self.assertIsNotNone(regulator._controlled_l1_boundary(self.controller._read_input()))
        self.assertEqual(regulator._get_meter_envelope_policy().state.value, 'NORMAL')
        self.adapter.calls.clear()
        await self.stage_one_shutdown()
        self.assertTrue(self.controller._stopped, self.controller._restart_bridge.diagnostics())
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 300)])
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertGreater(marker['physical_response_wall'], marker['dispatch_wall'])

    async def test_generic_entry_unload_does_not_create_restart_permission(self):
        await self.healthy_session()
        await self.controller.async_stop()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(any('restart' in key for key in self.hass._test_store_disk))
        self.assertFalse(any(not item.removed for item in self.shutdown_jobs))

    async def test_disabled_feature_leaves_shutdown_without_charger_command(self):
        await self.healthy_session()
        self.configure(graceful_restart_bridge=False)
        await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(any('restart' in key for key in self.hass._test_store_disk))

    async def test_enabling_future_bridge_does_not_cancel_live_normal_submission(self):
        regulator = await self.healthy_session()
        self.configure(graceful_restart_bridge=False)
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.adapter.async_execute_with_dispatch
        async def ordinary_sdk(decision, on_dispatch):
            receipt = await original(decision, on_dispatch)
            if decision.current_p1 == 16 and decision.ttl_seconds == 60:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return receipt
        self.adapter.async_execute_with_dispatch = ordinary_sdk
        self.report(205., 'charging', 20., 16., 204.8)
        sending = asyncio.create_task(self.controller.async_tick())
        selecting = None
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            generation = self.controller._generation
            cooling = (regulator._automatic_cooling_gt, regulator._automatic_cooling_since)
            self.assertIsNotNone(cooling[0])
            before = deepcopy(regulator.export_overload_state())
            selecting = asyncio.create_task(self.controller.async_set_option('graceful_restart_bridge', True))
            await asyncio.sleep(0)
            self.assertFalse(cancelled.is_set())
            self.assertFalse(selecting.done(), 'Preference must wait for the current normal request')
            release.set()
            await asyncio.wait_for(asyncio.gather(sending, selecting), 1.)
        finally:
            release.set()
            await asyncio.gather(sending, *([selecting] if selecting else []), return_exceptions=True)
        self.assertFalse(cancelled.is_set())
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(16, 60)])
        self.assertEqual(self.controller._generation, generation)
        self.assertEqual(regulator.last_target, (16, 0, 0, 60))
        self.assertAlmostEqual(regulator.last_set_limit_ts, 205.)
        self.assertTrue(self.controller.settings.graceful_restart_bridge)
        self.assertTrue(self.entry.options['graceful_restart_bridge'])
        self.assertEqual((regulator._automatic_cooling_gt, regulator._automatic_cooling_since), cooling)
        after = regulator.export_overload_state()
        for key in ('used_seconds', 'failure_reason'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['automatic_cycle']['cycle_id'], before['automatic_cycle']['cycle_id'])
        self.assertEqual(after['automatic_cycle']['used_seconds'], before['automatic_cycle']['used_seconds'])

    async def test_actual_high_load_prevents_graceful_blind_permission(self):
        await self.healthy_session()
        self.now += 1.
        self.grid(35.)
        await self.controller._async_prepare_shutdown_bridge()
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))
        self.assertFalse(any('acknowledged' in str(value)
                             for key, value in self.hass._test_store_disk.items() if 'restart' in key))

    async def test_preexisting_hard_limit_is_protected_before_shutdown_continues(self):
        await self.healthy_session()
        self.now += 1.
        self.grid(39.)
        # No callback is needed: the early shutdown job itself observes the
        # critical condition before any bridge packet could be submitted.
        await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNotNone(self.controller.regulator._get_meter_envelope_policy().operational_fault)

    async def test_preexisting_actuator_fault_is_not_hidden_by_handoff_ineligibility(self):
        await self.healthy_session()
        self.adapter.telemetry_block_reason = 'easee_stream_disconnected'
        self.adapter.transport_block_reason = 'easee_stream_disconnected'
        await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertIsNotNone(self.controller.regulator._get_meter_envelope_policy().operational_fault)

    async def test_prepared_checkpoint_failure_cannot_reach_sdk(self):
        await self.healthy_session()
        original = fixtures.FakeStore.async_save
        async def fail_bridge(store, payload):
            if 'restart' in store.key:
                raise OSError('offline synthetic checkpoint unavailable')
            return await original(store, payload)
        with patch.object(fixtures.FakeStore, 'async_save', fail_bridge):
            await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual(self.adapter.calls, [])

    async def test_http_acceptance_without_native_response_is_not_armed(self):
        await self.healthy_session()
        self.bridge_reply = False
        # Missing response must fail under a fixed local deadline, never wait
        # for an entire process shutdown in this deterministic offline test.
        task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
        await asyncio.sleep(.05)
        self.now += 20.
        await asyncio.wait_for(task, 1.)
        self.assertFalse(any('acknowledged' in str(value)
                             for key, value in self.hass._test_store_disk.items() if 'restart' in key))

    async def test_bootstrap_missing_grid_and_ordinary_recovery_have_no_second_zero(self):
        before, marker = await self.prepared_restart()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.controller._read_input().meter_grid_health.valid)
        self.assertIsNone(self.controller.regulator.last_target)
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertFalse(self.controller.last_decision.should_call_service)
        for at in range(5, 201, 5):
            self.report(float(at), 'charging', 11., 7., at-.2)
            await self.controller.async_tick()
            self.assertTrue(all(d.current_p1 > 0 for d in self.adapter.calls),
                            [(d.mode, d.current_p1, d.reason) for d in self.adapter.calls])
            if self.adapter.calls:
                break
            self.assertEqual(self.controller.last_decision.current_p1, 7)
            self.assertFalse(self.controller.last_decision.should_call_service)
        self.assertTrue(self.adapter.calls, 'Ordinary control never took over before fixed expiry')
        self.assertTrue(all(d.ttl_seconds == 60 for d in self.adapter.calls))
        self.assertGreater(self.controller.regulator.last_target[0], 0)
        self.assertFalse(self.controller._restart_bridge.armed)
        after = self.controller.regulator.export_overload_state()
        for key in ('used_seconds', 'failure_reason'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['automatic_cycle']['cycle_id'], before['automatic_cycle']['cycle_id'])
        self.assertEqual(after['automatic_cycle']['used_seconds'], before['automatic_cycle']['used_seconds'])

    async def test_actual_ordinary_takeover_responds_and_renews_beyond_old_bridge_expiry(self):
        before, marker = await self.prepared_restart()
        original_expiry = marker['expires_wall']
        for at in range(5, 426, 5):
            actual = self.adapter.calls[-1].current_p1 if self.adapter.calls else 7
            self.report(float(at), 'charging', actual+4., actual, at-.2)
            await self.controller.async_tick()
            self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls),
                            [(d.mode, d.current_p1, d.reason) for d in self.adapter.calls])
        self.assertGreater(self.wall().timestamp(), original_expiry)
        self.assertGreaterEqual(len(self.adapter.calls), 5)
        self.assertTrue(all(d.ttl_seconds == 60 for d in self.adapter.calls))
        self.assertEqual(self.controller.regulator.last_target[0], 16)
        self.assertEqual(self.controller.regulator._get_meter_envelope_policy().state.value, 'NORMAL')
        after = self.controller.regulator.export_overload_state()
        self.assertEqual(after['used_seconds'], before['used_seconds'])
        self.assertEqual(after['failure_reason'], before['failure_reason'])

    async def test_new_real_grid_peak_preempts_bootstrap_hold(self):
        await self.prepared_restart()
        self.now = 1.
        self.grid(39.)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertFalse(self.controller._restart_bridge.armed)

    async def test_actual_actuator_fault_preempts_bootstrap_hold(self):
        await self.prepared_restart()
        self.adapter.telemetry_block_reason = 'easee_stream_disconnected'
        self.adapter.transport_block_reason = 'easee_stream_disconnected'
        self.now = 1.
        await self.controller.async_tick()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_claimed_handoff_cannot_be_replayed_by_second_unprepared_boot(self):
        await self.prepared_restart()
        self.assertEqual(self.adapter.calls, [])
        await self.controller.async_stop()
        self.new_boot(downtime=5.)
        await self.controller.async_start()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_expired_handoff_does_not_suppress_first_missing_grid_zero(self):
        await self.prepared_restart(downtime=301.)
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_waiting_messages_and_ticks_cannot_refill_five_minute_window(self):
        _, original_marker = await self.prepared_restart(downtime=20.)
        claimed = self.persisted_bridge()[1]
        self.assertEqual(claimed['expires_wall'], original_marker['expires_wall'])
        original_deadline = self.controller.last_decision.normal_continuity_deadline_ts
        self.assertIsNotNone(original_deadline)
        for at in (10., 30., 60., 120., 240.):
            self.now = at
            await self.controller.async_tick()
            self.assertEqual(self.adapter.calls, [])
            self.assertEqual(self.controller.last_decision.current_p1, 7)
            self.assertEqual(self.controller.last_decision.normal_continuity_deadline_ts, original_deadline)
            self.assertEqual(self.persisted_bridge()[1]['expires_wall'], original_marker['expires_wall'])
            self.assertTrue(any(abs(timer.due-original_deadline) < 1e-6
                                for timer in self.active_timers()))
        self.now = 301.
        await self.controller.async_tick()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_claim_readback_mismatch_does_not_suppress_startup_protection(self):
        await self.healthy_session()
        await self.stage_one_shutdown()
        self.new_boot()
        original = fixtures.FakeStore.async_save
        async def swallowed_claim(store, payload):
            if 'restart' in store.key and payload.get('state') == 'claimed':
                return
            await original(store, payload)
        with patch.object(fixtures.FakeStore, 'async_save', swallowed_claim):
            await self.controller.async_start()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertEqual(self.controller.regulator.last_target[0], 0)
        self.assertNotEqual(self.persisted_bridge()[1]['state'], 'claimed')

    async def test_local_pre_sdk_refusal_does_not_send_an_unnecessary_zero(self):
        await self.healthy_session()
        async def locally_refused(decision, on_dispatch):
            raise fixtures.runtime.CommandNotSent('positive_rate_capacity_exhausted')
        self.adapter.async_execute_with_dispatch = locally_refused
        await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.controller._protective_zero_pending)

    async def test_ack_checkpoint_failure_withdraws_an_actually_sent_bridge(self):
        await self.healthy_session()
        original = fixtures.FakeStore.async_save
        async def fail_ack(store, payload):
            if 'restart' in store.key and payload.get('state') == 'acknowledged':
                raise OSError('synthetic acknowledgement checkpoint unavailable')
            return await original(store, payload)
        with patch.object(fixtures.FakeStore, 'async_save', fail_ack):
            await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertNotEqual(self.persisted_bridge()[1]['state'], 'acknowledged')

    async def test_prepare_waiting_for_lock_does_not_hide_ordinary_dispatch_cancellation(self):
        await self.healthy_session()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.adapter.async_execute_with_dispatch
        async def pending_ordinary(decision, on_dispatch):
            receipt = await original(decision, on_dispatch)
            if decision.current_p1 == 16 and decision.ttl_seconds == 60:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return receipt
        self.adapter.async_execute_with_dispatch = pending_ordinary
        self.report(205., 'charging', 20., 16., 204.8)
        ordinary = asyncio.create_task(self.controller.async_tick())
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            shutdown = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
            await asyncio.sleep(0)
            self.assertTrue(self.controller._restart_bridge.preparing)
            self.now += .2
            self.grid(39.)
            fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.wait_for(cancelled.wait(), 1.)
        finally:
            release.set()
            await asyncio.gather(ordinary, *([shutdown] if shutdown else []), return_exceptions=True)
        self.assertFalse(any(d.current_p1 == 7 and d.ttl_seconds == 300 for d in self.adapter.calls))
        self.assertFalse(self.controller._restart_bridge.armed)

    async def test_first_native_connection_wait_keeps_only_existing_finite_permission(self):
        await self.prepared_restart(native_wait=True)
        self.assertEqual(self.adapter.calls, [])
        inp = self.controller._read_input()
        self.assertFalse(inp.meter_grid_health.valid)
        self.assertFalse(inp.meter_actuator_health.valid)
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertFalse(self.controller.last_decision.should_call_service)
        self.assertIsNone(self.controller.regulator.last_target)
        # The meter can come online before the native Easee receiver. This
        # must not feed an actuator-invalid frame into ordinary cold recovery.
        self.now = .5
        self.grid(11.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(self.controller._read_input().meter_grid_health.valid)
        self.assertFalse(self.controller._read_input().meter_actuator_health.valid)
        # A genuine first native update and grid report release their own
        # bootstrap gaps; neither grants a new five-minute allowance.
        self.now = 1.
        self.adapter._product_data.state.update(isOnline=True, signalRConnected=True,
                                               latestPulse=self.wall())
        self.adapter.telemetry_block_reason = self.adapter.transport_block_reason = None
        for at in range(5, 201, 5):
            self.report(float(at), 'charging', 11., 7., at-.2)
            await self.controller.async_tick()
            self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))
            if self.adapter.calls:
                break
        self.assertTrue(self.adapter.calls)
        self.assertTrue(all(d.ttl_seconds == 60 for d in self.adapter.calls))

    async def test_fresh_offline_native_event_is_not_initial_connection_wait(self):
        await self.prepared_restart(native_wait=True)
        self.assertEqual(self.adapter.calls, [])
        self.now = 1.
        self.adapter._product_data.state.update(isOnline=False, signalRConnected=True,
                                               latestPulse=self.wall())
        self.adapter.telemetry_block_reason = 'easee_charger_offline'
        self.adapter.transport_block_reason = 'easee_charger_offline'
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_native_health_loss_after_first_healthy_update_is_not_bootstrap(self):
        await self.prepared_restart(native_wait=True)
        self.now = 1.
        self.adapter._product_data.state.update(isOnline=True, signalRConnected=True,
                                               latestPulse=self.wall())
        self.adapter.telemetry_block_reason = self.adapter.transport_block_reason = None
        self.report(1., 'charging', 11., 7., .8)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.now = 2.
        self.adapter.telemetry_block_reason = self.adapter.transport_block_reason = 'easee_stream_disconnected'
        # Even deleting the pulse cannot re-enter the one-time bootstrap lane.
        self.adapter._product_data.state.pop('latestPulse')
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_registry_binding_change_during_prepare_checkpoint_cannot_send(self):
        await self.healthy_session()
        original = fixtures.FakeStore.async_save
        async def rebound(store, payload):
            await original(store, payload)
            if 'restart' in store.key and payload.get('state') == 'prepared':
                self.controller._configured_control_identity.return_value = 'different-meter-unique-id'
        with patch.object(fixtures.FakeStore, 'async_save', rebound):
            await self.controller._async_prepare_shutdown_bridge()
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.controller._restart_bridge.armed)

    async def test_same_entity_rebound_to_new_meter_rejects_active_handoff(self):
        await self.prepared_restart()
        self.now = 1.
        self.controller._l1_source_config = replace(self.controller._l1_source_config,
            topic='currently/one/001122aabbdd/state')
        await self.controller.async_tick()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_actual_invalid_mqtt_frame_is_not_initial_grid_wait(self):
        await self.prepared_restart()
        self.now = 1.
        source = self.controller._l1_source
        source.receive(SimpleNamespace(topic=source.config.topic, retain=False,
            payload=json.dumps({'sec': self.wall().timestamp(), 'I2': 3})))
        await self.controller.async_tick()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_future_marker_cannot_supply_a_new_boot_permission(self):
        await self.prepared_restart(downtime=-1.)
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_clock_rollback_cannot_extend_an_active_handoff(self):
        await self.prepared_restart()
        self.now = 2.
        self.epoch -= 10.
        await self.controller.async_tick()
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_buffer_use_below_nominal_fuse_does_not_create_an_immediate_zero(self):
        await self.prepared_restart()
        self.report(5., 'charging', 24., 7., 4.8)
        await self.controller.async_tick()
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))
        self.assertLessEqual(self.controller.last_decision.current_p1, 7)
        self.assertGreater(self.controller.last_decision.current_p1, 0)
        # The normal buffer is not an overload period. A later genuine return
        # to whole-offer headroom may take over without refilling bridge time.
        for at in range(10, 201, 5):
            self.report(float(at), 'charging', 11., 7., at-.2)
            await self.controller.async_tick()
            self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))
            if self.adapter.calls:
                break
        self.assertTrue(self.adapter.calls)
        self.assertFalse(any(d.meter_overload_authority for d in self.adapter.calls))

    async def test_critical_source_event_cancels_inflight_prepare_before_zero(self):
        await self.healthy_session()
        self.bridge_reply = False
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.async_execute_with_dispatch
        async def held_sdk(decision, on_dispatch):
            receipt = await original(decision, on_dispatch)
            if decision.current_p1 == 7 and decision.ttl_seconds == 300:
                entered.set()
                await release.wait()
            return receipt
        self.adapter.async_execute_with_dispatch = held_sdk
        task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            self.now += .2
            self.grid(39.)
            fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
            await asyncio.wait_for(asyncio.shield(task), 1.)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertFalse(self.controller._restart_bridge.armed)
        self.assertIsNotNone(self.controller.regulator._get_meter_envelope_policy().operational_fault,
            'A real critical event must survive shutdown in the normal fault ledger')


if __name__ == '__main__':
    unittest.main()
