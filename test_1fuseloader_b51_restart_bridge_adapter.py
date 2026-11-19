"""Restart bridge through real controller, admission and adapter callbacks.

Only the selected charger, clock, packets and SDK transport are synthetic.
The earlier offers go through ordinary planning, SDK dispatch and ACK rather
than inserting prequalified leases into the controller.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

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
        self.bridge_globals = self.bridge.async_restart_with_charging.__func__.__globals__
        self.refusal = self.bridge_globals['BridgeRefused']
        self.command_not_sent = patch.dict(
            self.bridge_globals,
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
        self.assertFalse(self.bridge.armed)
        at = p.now + 1.
        await p.packet(at, {182: 7.08, 183: 7.08, 114: 7.}, drain=False)
        self.assertFalse(self.bridge.armed)
        p.now = at + 1.
        p.grid(16.5, p.now)

    async def wait_for_phase(self, phase):
        async with asyncio.timeout(2):
            while self.bridge.diagnostics()['restart_bridge_prepare_phase'] != phase:
                await asyncio.sleep(.01)

    async def begin_managed(self, restart):
        p = self.p
        request = asyncio.create_task(p.controller.async_restart_with_charging(restart))
        self.packet_tasks.append(request)
        await self.wait_for_phase('wait_normal_minimum')
        await p.pump()
        return request

    async def commit_shutdown(self):
        self.assertTrue(await self.p.controller._async_prepare_shutdown_bridge(), self.bridge.diagnostics())

    async def verify_real_minimum(self):
        p = self.p
        self.assertEqual([(row['phases'], row['ttl']) for row in p.sdk_calls], [((7, 0, 0), 60)])
        await self.publish_response()

    async def test_recent_14_to_13_can_bridge_at_ordinary_rate_capacity(self):
        r = await self.prepare_recent_normal_decrease()
        p = self.p
        prior = r._get_meter_envelope_policy().possible_positive.copy()
        self.saturate(p.adapter_module.NONZERO_ADMISSION_LIMIT - 2)
        restart = AsyncMock(side_effect=self.commit_shutdown)
        request = await self.begin_managed(restart)
        await self.verify_real_minimum()
        await asyncio.wait_for(request, 3)
        restart.assert_awaited_once()
        self.assertEqual([(row['phases'], row['ttl']) for row in p.sdk_calls],
                         [((7, 0, 0), 60), ((7, 0, 0), 300)])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.NONZERO_ADMISSION_LIMIT)
        marker = self.bridge._marker
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual(marker['expires_wall'], marker['dispatch_wall'] + 300)
        self.assertGreater(marker['grid_response_wall'], marker['physical_response_wall'])
        self.assertLessEqual(marker['grid_response_wall'], marker['dispatch_wall'])
        outstanding = r._get_meter_envelope_policy().possible_positive
        self.assertTrue(all(outstanding.get(key) == value for key, value in prior.items()))
        self.assertFalse(p.physical_interruptions)
        p.sdk_charger.pause.assert_not_awaited()
        p.sdk_charger.resume.assert_not_awaited()

    async def test_reserved_reduction_capacity_cannot_be_exceeded(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        self.saturate(p.adapter_module.PROTECTIVE_NONZERO_ADMISSION_LIMIT)

        restart = AsyncMock()
        with patch.dict(self.bridge_globals, {'MANAGED_PREFLIGHT_TIMEOUT_SECONDS': .35}):
            request = await self.begin_managed(restart)
            with self.assertRaisesRegex(self.refusal, 'bridge_prepare_timeout_wait_normal_minimum'):
                await asyncio.wait_for(request, 3)

        self.assertEqual(p.sdk_calls, [])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.PROTECTIVE_NONZERO_ADMISSION_LIMIT)
        self.assertTrue(any(event['kind'] == 'known_not_sent'
                            and event['code'] == 'positive_rate_capacity_exhausted'
                            and event['target_p1'] == 7 for event in p.controller._transport_events))
        restart.assert_not_awaited()
        self.assertFalse(p.controller._stopped)
        self.assertFalse(self.bridge.armed)

    async def test_same_cap_handoff_cannot_use_reserve_that_allowed_real_reduction(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        self.saturate(p.adapter_module.NONZERO_ADMISSION_LIMIT)
        restart = AsyncMock()
        request = await self.begin_managed(restart)
        await self.verify_real_minimum()
        with self.assertRaises(self.refusal) as caught:
            await asyncio.wait_for(request, 3)
        self.assertEqual(caught.exception.__cause__.code, 'positive_rate_capacity_exhausted')
        self.assertEqual([(row['cap'], row['ttl']) for row in p.sdk_calls], [(7, 60)])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.NONZERO_ADMISSION_LIMIT + 1)
        restart.assert_not_awaited()
        self.assertFalse(p.controller._stopped)
        self.assertFalse(self.bridge.armed)

    async def test_queued_bridge_refuses_grid_change_before_sdk(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        self.saturate(p.adapter_module.NONZERO_ADMISSION_LIMIT - 2)
        restart = AsyncMock()
        request = await self.begin_managed(restart)
        async with p.adapter._request_lock:
            await self.verify_real_minimum()
            await self.wait_for_phase('dispatch_verified_minimum')
            await asyncio.sleep(0)
            p.now += 1.
            p.grid(23.1, p.now)

        with self.assertRaisesRegex(self.refusal, 'bridge_(qualification_grid_ceiling|preflight_proof_lost)'):
            await asyncio.wait_for(request, 3)
        self.assertEqual([(row['cap'], row['ttl']) for row in p.sdk_calls], [(7, 60)])
        self.assertEqual(len(p.adapter._request_starts), p.adapter_module.NONZERO_ADMISSION_LIMIT - 1)
        restart.assert_not_awaited()
        self.assertFalse(p.controller._stopped)
        self.assertFalse(self.bridge.armed)

    async def test_cached_seven_is_not_native_readback_after_dispatch(self):
        await self.prepare_recent_normal_decrease()
        p = self.p
        restart = AsyncMock()

        async def cached_only(p1, p2, p3, minutes):
            # The SDK cache can show 7 after an HTTP ACK without an observed
            # native 111 packet for this attempt. Physical current reporting
            # and the later independent grid reading still arrive normally.
            response = await self.fixture.quiet_sdk(p1, p2, p3, minutes)
            if p1 == 7:
                p.product.state['dynamicCircuitCurrentP1'] = 7
            return response

        p.sdk_circuit.set_dynamic_current.side_effect = cached_only
        with patch.dict(self.bridge_globals, {'MANAGED_PREFLIGHT_TIMEOUT_SECONDS': .35}):
            request = await self.begin_managed(restart)
            await self.verify_real_minimum()
            self.assertEqual(p.product.state['dynamicCircuitCurrentP1'], 7)
            self.assertIsNone(p.adapter.reported_limit_receipts[0])
            inp = p.controller._read_input()
            physical = p.controller.regulator._controlled_l1_projection(inp)
            self.assertAlmostEqual(physical.current, 7.08)
            self.assertGreater(inp.grid_report_ts, physical.received_at_ts)
            with self.assertRaisesRegex(self.refusal, 'bridge_prepare_timeout_wait_normal_minimum'):
                await asyncio.wait_for(request, 3)

        self.assertEqual([(row['cap'], row['ttl']) for row in p.sdk_calls], [(7, 60)])
        restart.assert_not_awaited()
        self.assertFalse(self.bridge.armed)
        self.assertFalse(p.controller._stopped)


if __name__ == '__main__':
    unittest.main()
