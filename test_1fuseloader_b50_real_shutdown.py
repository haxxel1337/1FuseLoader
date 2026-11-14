"""Actual HA ConfigEntry, disk Store and Core shutdown regression contracts.

All current/grid observations and charger responses are synthetic. A temporary
HomeAssistant owns the real ConfigEntry, Store files, HassJobs and Core stop.
No live HA, broker, charger or household history is accessed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest


HAS_HA = importlib.util.find_spec('homeassistant') is not None
if os.environ.get('ONEFUSE_REQUIRE_HOMEASSISTANT') and not HAS_HA:
    raise RuntimeError('CI must install real Home Assistant for B50 shutdown contracts')


class ImmutableEntryBindingContractTests(unittest.TestCase):
    def test_binding_accepts_read_only_entry_data_without_mutating_it(self):
        # This small pure serializer check runs locally too. The separate
        # real-HA class below exercises the actual ConfigEntry, Store and Core.
        import test_1fuseloader_runtime as fixture
        data = MappingProxyType({'device_id': 'synthetic-device', 'controlled_phase': 'L1'})
        controller = SimpleNamespace(
            hass=SimpleNamespace(data={}),
            entry=SimpleNamespace(entry_id='offline-immutable-binding', data=data),
            settings=fixture.logic.RegulatorSettings(graceful_restart_bridge=True),
            _adapter=SimpleNamespace(_circuit=SimpleNamespace(id='offline-circuit')),
            _configured_control_identity=lambda: 'offline-source',
            _electrical_selection=lambda: ('offline-source',), _l1_source_config=None)
        digest = fixture.runtime.GracefulRestartBridge(controller)._binding()
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertIs(controller.entry.data, data)
        self.assertEqual(dict(data), {'device_id': 'synthetic-device', 'controlled_phase': 'L1'})


class SyntheticShutdownAdapter:
    """Only the device boundary is synthetic; HA lifecycle and I/O are real."""
    control_block_reason = None
    telemetry_block_reason = None
    transport_block_reason = None
    overload_block_reason = None
    offline_limits_verified_zero = True
    async_read_current_snapshot = None
    reported_limit_receipts = (None, None, None)
    control_identity = 'offline-real-core-adapter'

    def __init__(self, owner):
        self.owner = owner
        self._circuit = SimpleNamespace(id='offline-real-core-circuit')
        self.calls = []
        self.close_count = 0
        self.callbacks = {}

    async def async_initialize(self):
        return None

    def subscribe(self, key, action):
        self.callbacks[key] = action
        def unsubscribe():
            self.callbacks.pop(key, None)
        return unsubscribe

    def set_current_observation_callback(self, action):
        return self.subscribe('current', action)

    def set_delivery_callback(self, action):
        return self.subscribe('delivery', action)

    def set_telemetry_callback(self, action):
        return self.subscribe('telemetry', action)

    async def async_execute_with_dispatch(self, decision, on_dispatch):
        if not on_dispatch():
            raise self.owner.component.CommandNotSent('offline_dispatch_refused')
        self.calls.append(decision)
        if decision.current_p1 == 7 and decision.ttl_seconds == 300:
            self.owner.packet_tasks.append(asyncio.create_task(self.owner.response_packets()))
        return SimpleNamespace(http_status=200, correlation_ids=())

    async def async_close(self):
        self.close_count += 1
        self.owner.packet_stop.set()


@unittest.skipUnless(HAS_HA, 'Real Home Assistant is installed in CI')
class RealCoreRestartBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.config_entries import ConfigEntry
        from homeassistant.core import CoreState, HomeAssistant

        self.CoreState = CoreState
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.set_state(CoreState.running)
        self.component = importlib.import_module('custom_components.1fuseloader')
        self.logic = importlib.import_module('custom_components.1fuseloader.logic')
        self.bridge_module = importlib.import_module('custom_components.1fuseloader.restart_bridge')
        const = importlib.import_module('custom_components.1fuseloader.const')
        self.entry = ConfigEntry(version=1, minor_version=1, domain='1fuseloader',
            title='Offline real Core charger', data={
                'device_id': 'offline-real-core-device', 'controlled_phase': 'L1',
                'status_entity': 'sensor.offline_status', 'l1_entity': 'sensor.offline_l1',
                'l2_entity': 'sensor.offline_l2', 'l3_entity': 'sensor.offline_l3',
                'charger_current_entity': 'sensor.offline_current'},
            options={**const.DEFAULT_OPTIONS, 'active_control': True, 'dry_run': False,
                'nominal_import_only_fallback': True, 'meter_envelope_control': True,
                'graceful_restart_bridge': True, 'disconnected_readiness': True,
                'peak_only_response': True, 'automatic_overload_cycles': True,
                'overload_enabled': True, 'overload_margin_amps': 3,
                'enable_soft_guard': False, 'enable_pause_resume': False,
                'restart_stable_seconds': 15,
                'grid_confirmation_seconds': 30, 'measurement_settle_seconds': 25},
            source='user', unique_id='offline-real-core-device',
            discovery_keys=MappingProxyType({}), subentries_data=None)
        self.source = 'offline-real-core-source'
        self.status = 'charging'
        self.current = 16.2
        self.raw = 20.
        self.sequence = 100
        wall = datetime.now(timezone.utc).timestamp()
        self.current_wall = wall-.2
        self.grid_wall = wall-.1
        self.mapping_wall = wall-1000.
        self.response_delay = .02
        self.repeated_current = False
        self.packet_stop = asyncio.Event()
        self.packet_tasks = []
        self.controllers = []
        self.extra_hass = []
        self.adapter = SyntheticShutdownAdapter(self)
        self.controller = self.make_controller(self.hass, self.adapter)
        for entity_id, value in (('sensor.offline_status', 'charging'),
                                 ('sensor.offline_l1', '20'), ('sensor.offline_l2', '4'),
                                 ('sensor.offline_l3', '5'), ('sensor.offline_current', '16.2')):
            self.hass.states.async_set(entity_id, value)

    async def asyncTearDown(self):
        self.packet_stop.set()
        for task in self.packet_tasks:
            if not task.done():
                task.cancel()
        if self.packet_tasks:
            await asyncio.gather(*self.packet_tasks, return_exceptions=True)
        try:
            for controller in reversed(self.controllers):
                await controller.async_stop()
            for hass in [*reversed(self.extra_hass), self.hass]:
                if hass.state is not self.CoreState.stopped:
                    await asyncio.wait_for(hass.async_stop(force=True), 10)
                await hass.async_block_till_done()
        finally:
            self.temp.cleanup()

    def make_controller(self, hass, adapter):
        controller = self.component.OneFuseLoaderController(hass, self.entry, adapter)
        controller._configured_control_identity = lambda: self.source
        # Synthetic device acquisition only. The production Store, controller
        # lifecycle, public policy, shutdown job, dispatch and marker are real.
        controller._read_input = lambda: self.input(hass)
        self.controllers.append(controller)
        return controller

    def frame(self, mono, wall, raw, current, status, receipt, grid, sequence):
        return self.logic.RegulatorInput(now_ts=mono, status=status, grid_l1=raw,
            charger_current=current, wall_now_ts=wall, grid_report_ts=grid,
            charger_report_ts=receipt, control_source_id=self.source,
            charger_current_trusted=True, charger_native_committed=True,
            charger_report_family='native_terminal_receipt',
            meter_grid_health=self.logic.SourceHealth(True),
            meter_actuator_health=self.logic.SourceHealth(True),
            meter_ev_health=self.logic.SourceHealth(True),
            controlled_l1=self.logic.ControlledL1Evidence(current=current,
                received_at_ts=receipt, sequence=sequence, evidence_credit=True,
                source_id=self.source, receiver_identity=(100, 101), mapping_epoch=1,
                output_phase=10, mapping_adopted_at_ts=self.mapping_wall, mapping_sequence=1))

    def input(self, hass=None):
        hass = hass or self.hass
        wall = datetime.now(timezone.utc).timestamp()
        return self.frame(hass.loop.time(), wall, self.raw, self.current, self.status,
                          self.current_wall, self.grid_wall, self.sequence)

    async def start_healthy(self):
        """Public synthetic prehistory supplies an already-running normal car.

        Historical evaluate/begin_submission/acknowledge model actual earlier
        finite offers; no private lease/response fields are seeded. This avoids
        spending 60 real seconds bootstrapping each real-Core contract test.
        """
        r = self.controller.regulator
        mono = self.hass.loop.time()
        wall = datetime.now(timezone.utc).timestamp()
        for index, offset in enumerate((-75., -40., -10., -1.), 1):
            charging = offset == -1.
            inp = self.frame(mono+offset, wall+offset,
                20. if charging else 4., 16.2 if charging else .02,
                'charging' if charging else 'disconnected',
                wall+offset-.2, wall+offset-.1, index)
            decision = r.evaluate(inp)
            if decision.should_call_service:
                self.assertEqual(decision.current_p1, 16, decision)
                self.assertTrue(r.begin_submission(decision, mono+offset, inp=inp))
                r.acknowledge(decision, mono+offset+.001)
        self.assertEqual(r.last_target, (16, 0, 0, 60))
        await self.controller._budget_store.async_save(r.export_overload_state())
        await self.controller.async_start()
        self.assertTrue(self.controller._budget_loaded)
        self.assertTrue(self.controller._budget_store.state_is_durable(r.export_overload_state()))
        self.assertTrue(self.controller._restart_bridge._producer_eligible(self.input()),
                        self.controller._restart_bridge.diagnostics())
        self.adapter.calls.clear()
        return r

    async def response_packets(self):
        start = self.hass.loop.time()
        while self.hass.loop.time()-start < self.response_delay:
            self.grid_wall = datetime.now(timezone.utc).timestamp()
            await asyncio.sleep(.02)
        self.current = 7.1
        self.sequence += 1
        self.current_wall = datetime.now(timezone.utc).timestamp()
        # There is a real later meter event. Repeated mode then publishes a
        # new identical-current event just AFTER each meter event, so a reader
        # that keeps moving its physical floor can never catch that meter.
        await asyncio.sleep(.08)
        while not self.packet_stop.is_set():
            self.raw = 11.
            self.grid_wall = datetime.now(timezone.utc).timestamp()
            if self.repeated_current:
                self.sequence += 1
                self.current_wall = datetime.now(timezone.utc).timestamp()
            await asyncio.sleep(.03)

    async def marker(self, hass=None):
        from homeassistant.helpers.storage import Store
        return await Store(hass or self.hass, 1,
            self.controller._restart_bridge.storage_key, read_only=True).async_load()

    async def core_stop(self):
        await asyncio.wait_for(self.hass.async_stop(), 24)
        self.assertIs(self.hass.state, self.CoreState.stopped)

    async def assert_quiescent(self, calls):
        self.assertTrue(self.controller._stopped)
        self.assertIsNone(self.controller.unsub_timer)
        self.assertIsNone(self.controller.unsub_states)
        self.assertEqual(self.adapter.callbacks, {})
        self.assertEqual(self.adapter.close_count, 1)
        self.controller._handle_source_change(None)
        await self.controller.async_tick()
        await self.hass.async_block_till_done()
        self.assertEqual(self.adapter.calls, calls)
        await self.controller.async_stop()
        self.assertEqual(self.adapter.close_count, 1)

    async def test_real_config_entry_binding_has_stable_serializable_digest(self):
        self.assertIsInstance(self.entry.data, MappingProxyType)
        binding = self.controller._restart_bridge._binding()
        self.assertRegex(binding, r'^[0-9a-f]{64}$')
        self.assertEqual(self.controller._restart_bridge._binding(), binding)
        self.assertEqual(self.entry.data['device_id'], 'offline-real-core-device')

    async def test_actual_core_stop_arms_real_marker_and_next_runtime_claims_it_once(self):
        from homeassistant.core import HomeAssistant
        r = await self.start_healthy()
        before = deepcopy(r.export_overload_state())
        await self.core_stop()
        self.assertEqual([(d.command, d.current_p1, d.ttl_seconds) for d in self.adapter.calls],
                         [('set_limit', 7, 300)])
        marker = await self.marker()
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual(marker['expires_wall'], marker['dispatch_wall']+300)
        self.assertGreater(marker['physical_response_wall'], marker['dispatch_wall'])
        self.assertGreater(marker['grid_response_wall'], marker['physical_response_wall'])
        self.assertTrue(Path(self.hass.config.path('.storage',
            self.controller._restart_bridge.storage_key)).is_file())
        await self.assert_quiescent(list(self.adapter.calls))
        self.assertEqual(r.export_overload_state()['used_seconds'], before['used_seconds'])
        next_hass = HomeAssistant(self.temp.name)
        next_hass.set_state(self.CoreState.running)
        self.extra_hass.append(next_hass)
        next_adapter = SyntheticShutdownAdapter(self)
        next_controller = self.make_controller(next_hass, next_adapter)
        await next_controller.async_start()
        claimed = await self.marker(next_hass)
        self.assertEqual(claimed['state'], 'claimed')
        self.assertEqual(claimed['nonce'], marker['nonce'])
        self.assertEqual(claimed['expires_wall'], marker['expires_wall'])
        self.assertEqual(next_adapter.calls, [])
        replay = self.bridge_module.GracefulRestartBridge(next_controller)
        await replay.async_restore()
        self.assertEqual(replay.diagnostics()['restart_bridge_state'], 'not_reusable')
        self.assertFalse(replay._active)
        await replay.async_close()
        # Use a valid 15s test setting, not a forged policy clock. All Core,
        # Store and asyncio timers below are real; fresh independent samples
        # continue until an ordinary request actually reaches the SDK boundary.
        until = next_hass.loop.time()+18.
        while not next_adapter.calls and next_hass.loop.time() < until:
            self.sequence += 1
            self.current_wall = datetime.now(timezone.utc).timestamp()
            self.grid_wall = datetime.now(timezone.utc).timestamp()
            await next_controller.async_tick()
            await asyncio.sleep(.1)
        self.assertTrue(next_adapter.calls, next_controller._restart_bridge.diagnostics())
        self.assertTrue(all(0 < d.current_p1 <= 16 and d.ttl_seconds == 60
                            for d in next_adapter.calls))
        offered = next_adapter.calls[-1].current_p1
        self.current = offered+.1
        self.sequence += 1
        self.current_wall = datetime.now(timezone.utc).timestamp()
        await asyncio.sleep(.01)
        self.raw = self.current+4.
        self.grid_wall = datetime.now(timezone.utc).timestamp()
        await next_controller.async_tick()
        await next_hass.async_block_till_done()
        completed = await self.marker(next_hass)
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['expires_wall'], marker['expires_wall'])
        self.assertEqual(next_controller.regulator.last_target[0], offered)
        self.assertIsNone(next_controller.last_decision.meter_zero_cause)
        self.assertTrue(all(d.current_p1 > 0 for d in next_adapter.calls))

    async def test_repeated_new_minimum_frames_do_not_move_grid_confirmation_floor(self):
        await self.start_healthy()
        self.repeated_current = True
        await self.core_stop()
        marker = await self.marker()
        self.assertIsNotNone(marker)
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertGreater(marker['grid_response_wall'], marker['physical_response_wall'])
        await self.assert_quiescent(list(self.adapter.calls))

    async def test_fourteen_second_real_response_can_arm_within_core_early_stage(self):
        await self.start_healthy()
        self.response_delay = 14.1
        await self.core_stop()
        marker = await self.marker()
        self.assertIsNotNone(marker)
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertGreaterEqual(marker['physical_response_wall']-marker['dispatch_wall'], 14.)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        await self.assert_quiescent(list(self.adapter.calls))

    async def test_refused_early_job_still_quiesces_before_core_finishes(self):
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import callback
        await self.start_healthy()
        self.status = 'completed'
        @callback
        def during_stop(event):
            self.hass.states.async_set('sensor.offline_l1', '19.9')
            self.controller._handle_source_change(None)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, during_stop)
        await self.core_stop()
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(await self.marker())
        await self.assert_quiescent([])

    async def test_disabled_bridge_still_quiesces_real_core_lifecycle(self):
        from dataclasses import replace
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import callback
        await self.start_healthy()
        self.controller.regulator.update_settings(replace(self.controller.settings,
                                                         graceful_restart_bridge=False))
        @callback
        def during_stop(event):
            self.hass.states.async_set('sensor.offline_l1', '19.8')
            self.controller._handle_source_change(None)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, during_stop)
        await self.core_stop()
        self.assertEqual(self.adapter.calls, [])
        self.assertIsNone(await self.marker())
        await self.assert_quiescent([])

    async def test_fast_shutdown_paths_keep_the_following_real_ha_job(self):
        from dataclasses import replace
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import HassJob, callback

        for mode in ('disabled', 'refused', 'already_stopped'):
            with self.subTest(mode=mode):
                if mode != 'disabled':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.start_healthy()
                if mode == 'disabled':
                    self.controller.regulator.update_settings(replace(self.controller.settings,
                        graceful_restart_bridge=False))
                elif mode == 'refused':
                    self.status = 'completed'
                else:
                    await self.controller.async_stop()
                    # Model a stale queued callback whose controller was
                    # stopped before Core began executing its HassJob.
                    self.controller._register_shutdown_bridge_job()
                events = []
                async def next_job():
                    events.append('following_job')
                @callback
                def stopped(event):
                    events.append('stop_event')
                self.hass.async_add_shutdown_job(HassJob(next_job))
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopped)
                await self.core_stop()
                self.assertEqual(events, ['following_job', 'stop_event'])
                self.assertEqual(self.adapter.calls, [])
                await self.assert_quiescent([])

    async def test_cancelled_stop_waiter_does_not_cancel_shared_real_cleanup(self):
        await self.start_healthy()
        entered, release = asyncio.Event(), asyncio.Event()
        close = self.controller._restart_bridge.async_close
        async def gated_bridge_close():
            entered.set()
            await release.wait()
            await close()
        self.controller._restart_bridge.async_close = gated_bridge_close
        first = asyncio.create_task(self.controller.async_stop())
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1.)
            self.assertTrue(self.controller._stopped)
            self.assertIsNone(self.controller.unsub_timer)
            self.assertIsNone(self.controller.unsub_states)
            self.assertEqual(self.adapter.callbacks, {})
            first.cancel()
            result = await asyncio.gather(first, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertEqual(self.adapter.close_count, 0)
            second = asyncio.create_task(self.controller.async_stop())
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            await asyncio.wait_for(second, 1.)
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second is not None else []),
                                 return_exceptions=True)
            self.controller._restart_bridge.async_close = close
        self.assertEqual(self.adapter.close_count, 1)
        await self.controller.async_stop()
        self.assertEqual(self.adapter.close_count, 1)
        self.assertEqual(self.adapter.calls, [])
        await self.core_stop()
        self.assertEqual(self.adapter.close_count, 1)


if __name__ == '__main__':
    unittest.main()
