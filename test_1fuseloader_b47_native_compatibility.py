"""Exact runtime pairs and real pinned native Easee/SDK offline contracts.

CI checks out the exact native commit for its SDK row. Only device-registry
selection and network transport are simulated; native Controller/ProductData,
SDK factories, observation conversion and SignalR dispatch are real. No live
HA, credentials, sockets or charger are used.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_adapter as fixture

PAIRS = {('0.9.74', '0.8.17'), ('0.9.74', '0.8.19'), ('2026.11.0', '0.8.22')}
PINS = {
    '0.8.17': ('0.9.74', '0a95d0dcc1fb90d182ee625e7bcc5f795460c95a'),
    '0.8.19': ('0.9.74', '397eaedd1a56a95e81bfa37d45130349ba3de7a8'),
    '0.8.22': ('2026.11.0', '57e6881a0c705a9d8b5df6e004f43a701947b58f'),
}


class RuntimePairTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.AdapterTests.setUp

    def versions(self, native, sdk):
        self.version_module.VERSION = native
        self.pyeasee_module.__version__ = sdk

    async def test_only_the_three_audited_runtime_pairs_are_admitted(self):
        self.assertEqual(fixture.adapter.SUPPORTED_RUNTIME_PAIRS, frozenset(PAIRS))
        for native, sdk in sorted(PAIRS):
            with self.subTest(native=native, sdk=sdk):
                self.versions(native, sdk)
                self.assertIsNone(self.client.control_block_reason)
                self.assertEqual(self.client.runtime_contract_diagnostics(), {
                    'easee_loaded_version': native, 'pyeasee_loaded_version': sdk,
                    'easee_runtime_pair_supported': True})
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_known_but_unaudited_cross_pairs_cannot_submit_even_zero(self):
        for native, sdk in (('0.9.74', '0.8.22'), ('2026.11.0', '0.8.17'),
                            ('2026.11.0', '0.8.19')):
            with self.subTest(native=native, sdk=sdk):
                self.versions(native, sdk)
                self.assertEqual(self.client.control_block_reason,
                                 'unsupported_easee_pyeasee_combination')
                with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError,
                                            'unsupported_easee_pyeasee_combination'):
                    await self.client.async_execute(fixture.decision(amps=0))
                self.assertFalse(self.client.runtime_contract_diagnostics()['easee_runtime_pair_supported'])
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_new_pair_preserves_explicit_phases_and_finite_renewal(self):
        self.versions('2026.11.0', '0.8.22')
        await self.client.async_execute(fixture.decision(amps=16, ttl=60))
        await self.client.async_execute(fixture.decision(amps=16, ttl=60))
        self.assertEqual(self.circuit.set_dynamic_current.await_count, 2)
        for call in self.circuit.set_dynamic_current.await_args_list:
            self.assertEqual(call.args, (16, 0, 0, 1))
        self.charger.pause.assert_not_awaited()
        self.charger.resume.assert_not_awaited()

    async def test_missing_native_or_sdk_module_is_not_imported_or_started(self):
        with patch.dict(sys.modules):
            sys.modules.pop('custom_components.easee.const', None)
            self.assertEqual(self.client.control_block_reason, 'unsupported_or_unloaded_easee_version')
            self.assertNotIn('custom_components.easee.const', sys.modules)
        with patch.dict(sys.modules):
            sys.modules.pop('pyeasee', None)
            self.assertEqual(self.client.control_block_reason, 'unsupported_or_unloaded_pyeasee_version')
            self.assertNotIn('pyeasee', sys.modules)

    async def test_future_and_prior_unaudited_native_versions_stay_blocked(self):
        for version in ('0.9.75', '2026.11.1', '2026.12.0', '2026.11.0b1'):
            with self.subTest(version=version):
                self.versions(version, '0.8.22')
                self.assertEqual(self.client.control_block_reason, 'unsupported_or_unloaded_easee_version')
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_malformed_version_values_are_bounded_and_fail_closed(self):
        for value in (None, [], {}, 22, True, object(), 'private-token-not-a-version', '1.' * 100):
            with self.subTest(kind=type(value).__name__):
                self.versions(value, '0.8.22')
                self.assertEqual(self.client.control_block_reason, 'unsupported_or_unloaded_easee_version')
                self.assertIsNone(self.client.runtime_contract_diagnostics()['easee_loaded_version'])
                self.versions('2026.11.0', value)
                self.assertEqual(self.client.control_block_reason, 'unsupported_or_unloaded_pyeasee_version')
                diag = self.client.runtime_contract_diagnostics()
                self.assertIsNone(diag['pyeasee_loaded_version'])
                self.assertFalse(diag['easee_runtime_pair_supported'])

    async def test_new_pair_does_not_bypass_online_transport_or_offline_fallback(self):
        self.versions('2026.11.0', '0.8.22')
        self.product.state['isOnline'] = False
        with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError, 'easee_charger_offline'):
            await self.client.async_execute(fixture.decision())
        self.product.state['isOnline'] = True
        self.product.state['signalRConnected'] = False
        with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError, 'easee_stream_disconnected'):
            await self.client.async_execute(fixture.decision())
        self.product.state['signalRConnected'] = True
        self.assertEqual(self.client.overload_block_reason, 'offline_fallback_not_zero_or_unverified')
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_runtime_replacement_rechecks_pair_before_using_prior_objects(self):
        self.assertIsNone(self.client.control_block_reason)
        self.versions('2026.11.0', '0.8.19')
        with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError, 'unsupported_easee_pyeasee_combination'):
            await self.client.async_execute(fixture.decision())
        self.versions('2026.11.0', '0.8.22')
        await self.client.async_execute(fixture.decision())
        self.circuit.set_dynamic_current.assert_awaited_once_with(12, 0, 0, 1)


NATIVE_ROOT = os.environ.get('ONEFUSE_NATIVE_EASEE_ROOT')
REQUIRE_NATIVE = os.environ.get('ONEFUSE_REQUIRE_NATIVE_EASEE') == '1'
NATIVE_AVAILABLE = bool(NATIVE_ROOT and importlib.util.find_spec('homeassistant')
                        and importlib.util.find_spec('pyeasee'))
if REQUIRE_NATIVE and not NATIVE_AVAILABLE:
    raise RuntimeError('CI requires the pinned real native Easee checkout, real HA and real SDK')


@unittest.skipUnless(NATIVE_AVAILABLE, 'Optional local native contract; all CI matrix rows require it')
class RealNativeEaseeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import pyeasee
        cls.sdk_module = pyeasee
        cls.sdk_version = pyeasee.__version__
        cls.expected_version, cls.expected_sha = PINS[cls.sdk_version]
        cls.source_root = Path(NATIVE_ROOT).resolve()
        cls.native_path = cls.source_root / 'custom_components' / 'easee'
        actual_sha = subprocess.check_output(['git', '-C', str(cls.source_root),
                                              'rev-parse', 'HEAD'], text=True).strip()
        if actual_sha != cls.expected_sha:
            raise AssertionError((actual_sha, cls.expected_sha))
        subprocess.run(['git', '-C', str(cls.source_root), 'diff', '--exit-code',
                        'HEAD', '--', 'custom_components/easee'], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for name, expected in (('ONEFUSE_EXPECT_NATIVE_EASEE_SHA', cls.expected_sha),
                               ('ONEFUSE_EXPECT_NATIVE_EASEE_VERSION', cls.expected_version),
                               ('ONEFUSE_EXPECT_PYEASEE_VERSION', cls.sdk_version)):
            if os.environ.get(name) and os.environ[name] != expected:
                raise AssertionError((name, os.environ[name], expected))
        importlib.import_module('custom_components')
        spec = importlib.util.spec_from_file_location('custom_components.easee',
            cls.native_path / '__init__.py', submodule_search_locations=[str(cls.native_path)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        cls.native = importlib.import_module('custom_components.easee.controller')
        cls.native_const = importlib.import_module('custom_components.easee.const')

    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from pyeasee.const import ChargerStreamData
        from pyeasee.site import Circuit, Site
        from pysignalr.client import SignalRClient
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.response = SimpleNamespace(status=202, release=Mock())
        self.json_response = SimpleNamespace(json=AsyncMock(return_value={'observations': []}))
        self.transport = SimpleNamespace(post=AsyncMock(return_value=self.response),
                                         get=AsyncMock(return_value=self.json_response))
        self.site = Site({'id': 123, 'name': 'Offline native contract'}, self.transport)
        self.circuit = Circuit({'id': 456, 'chargers': [{'id': 'OFFLINE_NATIVE',
            'name': 'Offline native contract', 'productCode': 1, 'levelOfAccess': 1}]},
            self.site, self.transport)
        self.charger = self.circuit.get_chargers()[0]
        self.native_controller = self.native.Controller('offline-fixture', 'offline-fixture',
                                                        self.hass, SimpleNamespace())
        self.native_controller.chargers = [self.charger]
        kwargs = dict(product=self.charger, site=self.site, streamdata=ChargerStreamData,
                      poll_observations=(109, 182, 183, 184, 185), circuit=self.circuit)
        if self.expected_version == '2026.11.0':
            kwargs['user_data'] = self.native_controller.user_data
        self.product = self.native.ProductData(**kwargs)
        self.product.observers = {'state': {}, 'config': {}, 'schedule': {}, 'weekly_schedule': {}, 'site': {}}
        self.native_controller.chargers_data = [self.product]
        self.hass.data['easee'] = {'controller': self.native_controller}
        self.signal = SignalRClient('https://offline.invalid/hub')
        self.sdk = object.__new__(self.sdk_module.Easee)
        self.sdk.sr_connection = self.signal
        self.sdk.sr_subscriptions = {self.charger.id: self.native_controller.async_stream_callback}
        # Keep the real SDK SignalR dispatcher, but terminate every HTTP call
        # at the offline transport even after binding the charger to this SDK.
        self.sdk.get = self.transport.get
        self.sdk.post = self.transport.post
        self.charger.easee = self.sdk
        self.signal.on('ProductUpdate', self.sdk._sr_product_update_cb)
        device = SimpleNamespace(identifiers={('easee', self.charger.id)})
        self.registry_patch = patch('homeassistant.helpers.device_registry.async_get',
            return_value=SimpleNamespace(async_get=lambda _: device))
        self.registry_patch.start()
        self.client = fixture.adapter.EaseeCommandAdapter(self.hass, 'native-device')

    async def asyncTearDown(self):
        await self.client.async_close()
        self.registry_patch.stop()
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    def seed(self):
        self.product.state = dict(signalRConnected=True, isOnline=True,
            latestPulse=datetime.now(timezone.utc),
            **{f'inCurrentT{n}': 0. for n in (2, 3, 4, 5)},
            **{f'dynamicCircuitCurrentP{n}': 16 if n == 1 else 0 for n in (1, 2, 3)},
            **{f'offlineMaxCircuitCurrentP{n}': 0 for n in (1, 2, 3)})
        self.product.config = {}

    async def dispatch(self, values):
        from pysignalr.messages import InvocationMessage
        records = [dict(mid=self.charger.id, id=key, dataType=3, value=str(value),
                        timestamp=datetime.now(timezone.utc).isoformat())
                   for key, value in values.items()]
        await self.signal._on_invocation_message(InvocationMessage(
            invocation_id=None, target='ProductUpdate', arguments=records))

    async def test_exact_native_source_and_manifest_are_the_matrix_contract(self):
        manifest = json.loads((self.native_path / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], self.expected_version)
        self.assertEqual(manifest['requirements'], ['pyeasee==' + self.sdk_version])
        self.assertEqual(self.native_const.VERSION, self.expected_version)
        self.assertEqual(Path(self.native.__file__).resolve(), self.native_path / 'controller.py')
        self.assertIsNone(self.client.control_block_reason)
        self.assertIs(self.client._controller, self.native_controller)
        self.assertIs(self.client._product_data, self.product)
        self.assertIs(self.client._charger, self.charger)
        self.assertIs(self.client._circuit, self.circuit)
        self.transport.post.assert_not_awaited()

    async def test_real_native_initial_poll_does_not_grant_positive_authority(self):
        self.assertEqual(self.client.telemetry_block_reason, 'easee_telemetry_missing')
        await self.product.async_refresh()
        self.transport.get.assert_awaited_once_with(
            '/state/OFFLINE_NATIVE/observations?ids=109,182,183,184,185')
        if self.sdk_version in {'0.8.19', '0.8.22'}:
            self.assertIs(self.product.state['isOnline'], False)
        self.assertIsNotNone(self.client.telemetry_block_reason)
        with self.assertRaises(fixture.adapter.EaseeAdapterError):
            await self.client.async_execute(fixture.decision())
        self.transport.post.assert_not_awaited()

    async def test_native_stream_and_raw_observer_commit_same_current_after_dispatch(self):
        self.seed()
        self.assertIsNone(self.client.control_block_reason)
        before = self.client.native_current_observation
        await self.dispatch({182: 8.25, 183: 8.1, 184: 0., 185: 0.})
        after = self.client.native_current_observation
        self.assertEqual(self.product.state['inCurrentT3'], 8.1)
        self.assertEqual(after.current, 8.25)
        self.assertTrue(after.committed)
        self.assertFalse(after.native_publication_pending)
        self.assertIsNone(after.error)
        self.assertIsNotNone(after.received_at)
        self.assertGreater(dict(after.observation_sequences)[183], dict(before.observation_sequences)[183])
        self.assertIsNone(self.client.telemetry_block_reason)
        self.transport.post.assert_not_awaited()

    async def test_native_online_notifier_observes_completed_observation_on_new_sources(self):
        from pyeasee.const import ChargerStreamData
        self.seed()
        observed = []
        observer = SimpleNamespace(enabled=True,
            async_schedule_update_ha_state=lambda _: observed.append(self.product.state['inCurrentT3']))
        self.product.register_for_update('state.isOnline', observer)
        await self.product.async_update_stream_data(3, ChargerStreamData.state_inCurrentT3.value, 12.5)
        self.assertTrue(observed)
        self.assertEqual(observed[-1], 0. if self.sdk_version == '0.8.17' else 12.5)
        self.assertEqual(self.product.state['inCurrentT3'], 12.5)
        self.transport.post.assert_not_awaited()

    async def test_native_explicit_cloud_disconnect_and_transport_disconnect_remain_vetoes(self):
        from pyeasee.const import ChargerStreamData
        self.seed()
        await self.product.async_update_stream_data(1,
            ChargerStreamData.state_connectedToCloud.value, False)
        if self.sdk_version in {'0.8.19', '0.8.22'}:
            self.assertIs(self.product.state['isOnline'], False)
            self.assertEqual(self.client.transport_block_reason, 'easee_charger_offline')
            with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError, 'easee_charger_offline'):
                await self.client.async_execute(fixture.decision())
        self.product.set_signalr_state(False)
        self.assertEqual(self.client.transport_block_reason, 'easee_stream_disconnected')
        with self.assertRaisesRegex(fixture.adapter.EaseeAdapterError, 'easee_stream_disconnected'):
            await self.client.async_execute(fixture.decision())
        self.transport.post.assert_not_awaited()

    async def test_real_native_adapter_and_sdk_preserve_identical_finite_submissions(self):
        self.seed()
        first = await self.client.async_execute(fixture.decision(amps=16, ttl=60))
        second = await self.client.async_execute(fixture.decision(amps=16, ttl=60))
        self.assertEqual((first.http_status, second.http_status), (202, 202))
        self.assertEqual(self.transport.post.await_count, 2)
        expected = {'phase1': 16, 'phase2': 0, 'phase3': 0, 'timeToLive': 1}
        for call in self.transport.post.await_args_list:
            self.assertEqual(call.args, ('/api/sites/123/circuits/456/dynamicCurrent',))
            self.assertEqual(call.kwargs, {'json': expected})

    async def test_native_auxiliary_lookup_wait_does_not_stop_actual_normal_meter_allocation(self):
        # The selected meter path must tolerate native publication waiting on
        # unrelated auxiliary HTTP without inventing a raw receipt or response.
        # The normal allocation is earned through the actual controller/ACK
        # harness, never inserted into private normal-admission proof fields.
        import test_1fuseloader_b44_quiet_safety as meter_fixture
        from pyeasee.const import ChargerStreamData
        from pysignalr.client import SignalRClient
        from pysignalr.messages import InvocationMessage
        harness = meter_fixture.B44QuietSafetyTests(methodName='runTest')
        await harness.asyncSetUp()
        p = harness.plant
        native_product = self.native.ProductData(**dict(
            product=p.sdk_charger, site=self.site, streamdata=ChargerStreamData,
            poll_observations=(), circuit=p.sdk_circuit,
            **({'user_data': self.native.UserData()} if self.expected_version == '2026.11.0' else {})))
        native_product.state = dict(p.product.state)
        native_product.config = {}
        native_product.observers = {'state': {}, 'config': {}, 'schedule': {}, 'weekly_schedule': {}, 'site': {}}
        native_controller = self.native.Controller('offline-fixture', 'offline-fixture', p.hass, p.entry)
        native_controller.chargers = [p.sdk_charger]
        native_controller.chargers_data = [native_product]
        sdk = object.__new__(self.sdk_module.Easee)
        signal = SignalRClient('https://offline.invalid/hub')
        sdk.sr_connection = signal
        sdk.sr_subscriptions = {p.sdk_charger.id: native_controller.async_stream_callback}
        p.sdk_charger.easee = sdk
        p.product = native_product
        p.native_controller = native_controller
        p.hass.data['easee'] = {'controller': native_controller}
        signal.on('ProductUpdate', sdk._sr_product_update_cb)

        def publish(_):
            maximum = max(native_product.state[f'inCurrentT{n}'] for n in (2, 3, 4, 5))
            p.states['sensor.car'].state = str(maximum)
            p.states['sensor.car'].last_reported = p.wall()
            p.circuit.attributes['state_latestPulse'] = p.wall().isoformat()

        observer = SimpleNamespace(enabled=True, async_schedule_update_ha_state=publish)
        for name in ('isOnline', 'inCurrentT2', 'inCurrentT3', 'inCurrentT4', 'inCurrentT5'):
            native_product.register_for_update('state.' + name, observer)

        async def packet(at, values, *, source=None, drain=True):
            p.now = at
            records = [dict(mid=p.sdk_charger.id, id=key,
                dataType=4 if key in (21, 38, 109, 110) else 3, value=str(value),
                timestamp=p.wall(at if source is None else source).isoformat())
                for key, value in values.items()]
            p.adapter.observation_clock_identity
            await signal._on_invocation_message(InvocationMessage(
                invocation_id=None, target='ProductUpdate', arguments=records))
            if drain:
                await p.pump()

        p.packet = packet
        entered, release = asyncio.Event(), asyncio.Event()

        async def lookup(*args):
            entered.set()
            await release.wait()
            return []

        native_clock = patch.object(self.native.dt_util, 'utcnow', p.wall)
        native_clock.start()
        modules = patch.dict(sys.modules, {'custom_components.easee.const': self.native_const,
                                          'pyeasee': self.sdk_module})
        modules.start()
        task = None
        try:
            r = await p.boot()
            before = deepcopy(p.adapter.native_current_observation)
            before_l1 = deepcopy(p.adapter.native_controlled_l1_observation)
            original_expiry = r._get_meter_envelope_policy().lease.expires
            original_ack = r.last_set_limit_ts
            p.now = 200.
            if self.expected_version == '2026.11.0':
                self.transport.get_keys = AsyncMock(side_effect=lookup)
                auxiliary_id = ChargerStreamData.state_chargingSessionStart.value
                auxiliary_value = json.dumps({'Id': 'offline-session', 'Auth': 'offline-key',
                                             'Start': '2030-01-01T00:00:00Z'})
            else:
                native_product.async_cost_refresh = lookup
                auxiliary_id = ChargerStreamData.state_lifetimeEnergy.value
                auxiliary_value = '1'
            records = [dict(mid=p.sdk_charger.id, id=183, dataType=3, value='13',
                            timestamp=p.wall().isoformat()),
                       dict(mid=p.sdk_charger.id, id=auxiliary_id,
                            dataType=7 if self.expected_version == '2026.11.0' else 3,
                            value=auxiliary_value)]
            task = asyncio.create_task(signal._on_invocation_message(InvocationMessage(
                invocation_id=None, target='ProductUpdate', arguments=records)))
            await asyncio.wait_for(entered.wait(), 2.)
            pending = p.adapter.native_current_observation
            self.assertTrue(pending.native_publication_pending)
            self.assertEqual(pending.received_at, before.received_at)
            self.assertEqual(pending.observation_sequences, before.observation_sequences)
            native_pulse = native_product.state['latestPulse']
            d = await harness.tick(201.2, 21.2, status='charging')
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(r.last_target[0], 16)
            self.assertEqual(r.last_set_limit_ts, original_ack)
            self.assertEqual(r._get_meter_envelope_policy().lease.expires, original_expiry)
            self.assertFalse(p.sdk_calls)
            self.assertFalse(p.physical_interruptions)
            held = p.adapter.native_current_observation
            held_l1 = p.adapter.native_controlled_l1_observation
            self.assertEqual(held.error, 'native_publication_diverged')
            self.assertIsNone(held.received_at)
            self.assertEqual(held.observation_sequences, before.observation_sequences)
            self.assertEqual(held_l1.sequence, before_l1.sequence)
            self.assertEqual(held_l1.received_at, before_l1.received_at)
            self.assertFalse(held_l1.committed)
            self.assertEqual(held_l1.current_error, 'controlled_current_publication_pending')
            self.assertEqual(held.current, before.current)
            self.assertEqual(native_product.state['latestPulse'], native_pulse)
            release.set()
            await task
            task = None
            after = p.adapter.native_current_observation
            self.assertFalse(after.native_publication_pending)
            self.assertIsNone(after.error)
            self.assertGreater(dict(after.observation_sequences)[183], dict(before.observation_sequences)[183])
            self.assertGreater(dict(after.observation_report_times)[183],
                               dict(before.observation_report_times)[183])
        finally:
            release.set()
            if task is not None:
                await task
            modules.stop()
            native_clock.stop()
            await harness.asyncTearDown()


if __name__ == '__main__':
    unittest.main()
