"""Restart bridge through real controller, admission and adapter callbacks.

Only the selected charger, clock, packets and SDK transport are synthetic.
The earlier offers go through ordinary planning, SDK dispatch and ACK rather
than inserting prequalified leases into the controller.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_1fuseloader_b44_quiet_safety as fixture
import test_1fuseloader_runtime as runtime_fixture


class RestartBridgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.B44QuietSafetyTests(methodName='runTest')
        await self.fixture.asyncSetUp()
        self.p = self.fixture.plant
        self.install_mqtt_source()
        self.packet_tasks = []
        self.bridge = self.p.controller._restart_bridge
        self.bridge._store = runtime_fixture.FakeStore(
            self.p.hass, 1, self.bridge.storage_key)
        self.bridge._read_back = runtime_fixture.FakeStore(
            self.p.hass, 1, self.bridge.storage_key).async_load
        # The historical controller and adapter fixtures load the underlying
        # modules with different names. Keep typed admission refusal identical
        # to the single production import at the bridge boundary.
        self.command_not_sent = patch.dict(
            self.bridge.async_prepare_shutdown.__func__.__globals__,
            {'CommandNotSent': self.p.adapter_module.CommandNotSent},
        )
        self.command_not_sent.start()

    def install_mqtt_source(self):
        p = self.p
        runtime = runtime_fixture.runtime
        original_grid = p.grid

        def receive(value, reported):
            source = p.controller._l1_source
            if source is not None:
                source.receive(SimpleNamespace(topic=source.config.topic, retain=False,
                    payload=json.dumps({'I1': value, 'sec': p.wall(reported).timestamp()})))

        async def configure_source():
            if p.controller._l1_source is None:
                config = runtime.MqttL1Config('sensor.grid', 'synthetic/grid/state', 'I1', 'sec')
                source = runtime.MqttL1Source(config, monotonic=lambda: p.now,
                    utc_now=p.wall, age_limit=lambda: 120., on_change=lambda: None)
                # Only broker setup is synthetic. Actual MQTT parsing and
                # freshness checks consume the independent grid packet.
                source._active = source._connected = True
                p.controller._l1_source = source
                p.controller._l1_source_config = config
                receive(float(p.states['sensor.grid'].state), p.now)
            return True

        def grid(value, reported=None):
            original_grid(value, reported)
            receive(value, p.now if reported is None else reported)

        p.controller._async_configure_l1_source_locked = configure_source
        p.grid = grid

    async def asyncTearDown(self):
        for task in self.packet_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.packet_tasks, return_exceptions=True)
        try:
            await self.fixture.asyncTearDown()
        finally:
            self.command_not_sent.stop()

    async def prepare_recent_normal_decrease(self):
        p = self.p
        p.entry.options.update(graceful_restart_bridge=True)
        r = await p.boot()
        # RAW rises after the last qualifying mapped EV observation. The real
        # policy issues a normal feedback decrease on each new meter sample.
        for at, raw, expected in ((200., 24.5, 14), (210., 23.4, 13)):
            await p.tick(at, raw)
            self.assertEqual(r.last_target[0], expected, p.controller.last_decision)
        await p.tick(217., 22.5)
        self.assertEqual(r.last_target[0], 13)
        policy = r._get_meter_envelope_policy()
        parents = policy.possible_positive
        self.assertTrue(any(cap == 14 and expiry > p.now
                            for cap, expiry in parents.values()), parents)
        self.assertEqual(policy.state.value, 'NORMAL')
        p.sdk_calls.clear()
        return r

    def saturate(self, count):
        # Model local write history independently of the actual earlier
        # commands above. This changes no regulator authority or SDK result.
        self.p.adapter._request_starts.clear()
        self.p.adapter._request_starts.extend([self.p.now - 1.] * count)

    async def publish_response(self):
        p = self.p
        # A successful API ACK or its native limit readback alone cannot arm
        # the handoff. Actual current and a later grid sample are separate.
        await asyncio.sleep(.02)
        self.assertFalse(self.bridge.armed)
        at = p.now + 1.
        await p.packet(at, {182: 7.08, 183: 7.08, 114: 7.}, drain=False)
        self.assertFalse(self.bridge.armed)
        p.now = at + 1.
        p.grid(16.5, p.now)

    async def schedule_response(self, row):
        if row['cap'] == 7 and row['ttl'] == 300:
            self.packet_tasks.append(asyncio.create_task(self.publish_response()))

    async def test_recent_14_to_13_can_bridge_at_ordinary_rate_capacity(self):
        r = await self.prepare_recent_normal_decrease()
        p = self.p
        prior = r._get_meter_envelope_policy().possible_positive.copy()
        self.saturate(p.adapter_module.NONZERO_ADMISSION_LIMIT)
        p.sdk_hook = self.schedule_response

        self.assertTrue(await self.bridge.async_prepare_shutdown(), self.bridge.diagnostics())
        await asyncio.gather(*self.packet_tasks)

        self.assertEqual([(row['phases'], row['ttl']) for row in p.sdk_calls], [((7, 0, 0), 300)])
        marker = self.bridge._marker
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual(marker['expires_wall'], marker['dispatch_wall'] + 300)
        self.assertGreater(marker['physical_response_wall'], marker['dispatch_wall'])
        self.assertGreater(marker['grid_response_wall'], marker['physical_response_wall'])
        self.assertEqual(r._get_meter_envelope_policy().possible_positive, prior)
        self.assertFalse(p.physical_interruptions)
        p.sdk_charger.pause.assert_not_awaited()
        p.sdk_charger.resume.assert_not_awaited()

    async def test_reserved_reduction_capacity_cannot_be_exceeded(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        self.saturate(p.adapter_module.PROTECTIVE_NONZERO_ADMISSION_LIMIT)

        self.assertFalse(await self.bridge.async_prepare_shutdown())

        self.assertEqual(p.sdk_calls, [])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.PROTECTIVE_NONZERO_ADMISSION_LIMIT)
        self.assertEqual(self.bridge.diagnostics()['restart_bridge_last_error'],
                         'bridge_command_not_sent_positive_rate_capacity_exhausted')
        self.assertFalse(self.bridge.armed)

    async def test_queued_bridge_refuses_grid_change_before_sdk(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        self.saturate(p.adapter_module.NONZERO_ADMISSION_LIMIT)
        async with p.adapter._request_lock:
            task = asyncio.create_task(self.bridge.async_prepare_shutdown())
            self.packet_tasks.append(task)
            for _ in range(100):
                if self.bridge.diagnostics()['restart_bridge_prepare_phase'] == 'dispatch_minimum':
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.bridge.diagnostics()['restart_bridge_prepare_phase'], 'dispatch_minimum')
            await asyncio.sleep(0)
            p.now += 1.
            p.grid(23.1, p.now)

        self.assertFalse(await task)
        self.assertEqual(p.sdk_calls, [])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.NONZERO_ADMISSION_LIMIT)
        self.assertEqual(self.bridge.diagnostics()['restart_bridge_last_error'],
                         'bridge_qualification_grid_ceiling')
        self.assertFalse(self.bridge.armed)

    async def test_cached_seven_is_not_native_readback_after_dispatch(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        p.sdk_hook = self.schedule_response

        async def cached_only(p1, p2, p3, minutes):
            # The SDK cache can show 7 after an HTTP ACK without an observed
            # native 111 packet for this attempt. Physical current reporting
            # and the later independent grid reading still arrive normally.
            response = await self.fixture.quiet_sdk(p1, p2, p3, minutes)
            if p1 == 7:
                p.product.state['dynamicCircuitCurrentP1'] = 7
            return response

        p.sdk_circuit.set_dynamic_current.side_effect = cached_only
        module = self.bridge.async_prepare_shutdown.__func__.__globals__
        with patch.dict(module, {'PREPARE_TIMEOUT_SECONDS': .35}):
            self.assertFalse(await self.bridge.async_prepare_shutdown())
        await asyncio.gather(*self.packet_tasks)

        diagnostics = self.bridge.diagnostics()
        self.assertEqual(diagnostics['restart_bridge_failed_phase'], 'wait_readback')
        self.assertIsNotNone(diagnostics['restart_bridge_physical_response_at_utc'])
        self.assertIsNotNone(diagnostics['restart_bridge_grid_response_at_utc'])
        self.assertEqual([row['cap'] for row in p.sdk_calls], [7, 0])
        self.assertEqual(self.bridge._marker['state'], 'aborted')


if __name__ == '__main__':
    unittest.main()
