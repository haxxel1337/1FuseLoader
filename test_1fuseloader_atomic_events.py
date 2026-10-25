"""Atomic raw observations across the real SDK's native-before-observer order.

The native publication projection models ProductData.async_update_stream_data,
async_update_observation and set_state/notify from audited native Easee commit
397eaedd1a56a95e81bfa37d45130349ba3de7a8, including its later lifetimeEnergy
cost-refresh await. HA's display formatter is modelled here as .15g; separate
real-HA tests exercise the actual Entity implementation. This is not the full
native integration. SDK conversion and SignalR dispatch are real;
no sockets, credentials or network requests are used.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from pyeasee import __version__ as REAL_SDK_VERSION
    from pyeasee.easee import Easee
    from pyeasee.const import ChargerStreamData
    from pysignalr.client import SignalRClient
    from pysignalr.messages import InvocationMessage
except ModuleNotFoundError as error:
    if os.environ.get('ONEFUSE_REQUIRE_PYEASEE_CONTRACT') == '1':
        raise
    Easee = SignalRClient = InvocationMessage = None

import test_1fuseloader_clock_provenance_adapter as raw
import test_1fuseloader_runtime as runtime_fixtures

adapter = raw.adapter
runtime = runtime_fixtures.runtime


class AtomicConductorTests(unittest.IsolatedAsyncioTestCase):
    setUp = raw.ConductorClockTests.setUp
    health = staticmethod(raw.ConductorClockTests.health)
    make_charger = raw.ConductorClockTests.make_charger
    assert_no_commands = raw.ConductorClockTests.assert_no_commands
    advance = raw.ConductorClockTests.advance
    emit = raw.ConductorClockTests.emit

    async def test_sdk_publication_does_not_mix_new_number_with_old_clock(self):
        before = await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.advance(.1)
        self.product.state['inCurrentT2'] = 11.
        pending = self.client.native_current_observation
        self.assertEqual((pending.current, pending.sdk_current), (8., 11.))
        self.assertTrue(pending.committed)
        self.assertTrue(pending.native_publication_pending)
        self.assertEqual(pending.received_at, before.received_at)
        self.assertEqual(pending.observation_sequences, before.observation_sequences)
        after = await self.emit({182: 11.})
        self.assertEqual(after.current, 11.)
        self.assertFalse(after.native_publication_pending)
        self.assertGreater(after.received_at, before.received_at)

    async def test_partial_sdk_tuple_does_not_publish_partial_raw_maximum(self):
        before = await self.emit({182: 10., 183: 9., 184: 0., 185: 0.})
        self.product.state['inCurrentT2'] = 0.
        pending = self.client.native_current_observation
        self.assertEqual((pending.current, pending.observation_ids), (10., (182,)))
        self.assertEqual(pending.received_at, before.received_at)
        after = await self.emit({182: 0., 183: 0.})
        self.assertEqual(after.current, 0.)
        self.assertFalse(after.native_publication_pending)

    async def test_watchdog_is_fixed_at_first_mismatch_despite_churn_or_poll(self):
        await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.product.state['inCurrentT2'] = 9.
        first = self.client.native_current_observation
        generation = first.publication_generation
        self.advance(.4)
        self.product.state['inCurrentT2'] = 8.  # Poll of old tuple is no raw commit.
        polled = self.client.native_current_observation
        self.assertTrue(polled.native_publication_pending)
        self.product.state['inCurrentT2'] = 12.
        self.advance(.599)
        just_before = self.client.native_current_observation
        self.assertEqual(just_before.publication_generation, generation)
        self.assertEqual(just_before.divergence_started_monotonic, first.divergence_started_monotonic)
        self.assertIsNone(just_before.error)
        self.advance(.002)
        expired = self.client.native_current_observation
        self.assertEqual(expired.error, 'native_publication_diverged')
        self.assertEqual(expired.divergence_started_monotonic, first.divergence_started_monotonic)

    async def test_committed_coherence_then_new_gap_has_new_episode(self):
        await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.product.state['inCurrentT2'] = 9.
        first = self.client.native_current_observation
        self.advance(.7)
        coherent = await self.emit({182: 9.})
        self.assertFalse(coherent.native_publication_pending)
        self.advance(.1)
        self.product.state['inCurrentT2'] = 10.
        second = self.client.native_current_observation
        self.assertGreater(second.publication_generation, first.publication_generation)
        self.assertGreater(second.divergence_started_monotonic, first.divergence_started_monotonic)
        self.advance(.3)
        self.assertIsNone(self.client.native_current_observation.error)

    async def test_connection_rebind_cannot_renew_divergence(self):
        await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.product.state['inCurrentT2'] = 9.
        first = self.client.native_current_observation
        self.advance(.8)
        self.charger.easee.sr_connection = raw.Connection()
        rebound = self.client.native_current_observation
        self.assertEqual(rebound.publication_generation, first.publication_generation)
        self.assertEqual(rebound.divergence_started_monotonic, first.divergence_started_monotonic)
        self.advance(.201)
        self.assertEqual(self.client.native_current_observation.error, 'native_publication_diverged')

    async def test_bad_nonwinner_invalidates_entire_tuple_until_valid_recovery(self):
        first = await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.client._receive_current_observations([
            {'mid': self.charger.id, 'id': 183, 'dataType': 3, 'value': 'invalid'}], self.charger.id)
        invalid = self.client.native_current_observation
        self.assertEqual(invalid.error, 'invalid_terminal_current')
        self.assertIsNone(invalid.received_at)
        recovered = await self.emit({183: 0.})
        self.assertIsNone(recovered.error)
        self.assertEqual(recovered.received_at, first.received_at)

    async def test_conflicting_nonwinner_is_not_hidden_by_old_larger_max(self):
        await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.client._receive_current_observations([
            {'mid': self.charger.id, 'id': 183, 'dataType': 3, 'value': '0'},
            {'mid': self.charger.id, 'id': 183, 'dataType': 3, 'value': '12'}], self.charger.id)
        self.assertEqual(self.client.native_current_observation.error, 'conflicting_terminal_observations')

    async def test_invalid_sdk_tuple_is_immediate_even_when_raw_was_valid(self):
        await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        for bad in (None, True, float('nan'), float('inf')):
            with self.subTest(bad=bad):
                self.product.state['inCurrentT3'] = bad
                self.assertEqual(self.client.native_current_observation.error, 'native_sdk_current_invalid')

    async def test_source_object_replacement_bootstraps_without_old_event_proof(self):
        first = await self.emit({182: 8., 183: 0., 184: 0., 185: 0.})
        self.controller.chargers_data = [SimpleNamespace(product=self.charger, state=dict(self.product.state))]
        replaced = self.client.native_current_observation
        self.assertNotEqual(replaced.source_identity, first.source_identity)
        self.assertIsNone(replaced.received_at)
        self.assertEqual(dict(replaced.observation_sequences), {182: 0, 183: 0, 184: 0, 185: 0})


@unittest.skipUnless(Easee is not None, 'Optional local real SDK contract; required by CI.')
class SDKPublicationOrderTests(unittest.IsolatedAsyncioTestCase):
    """Real SDK dispatch plus narrow native-HA publication projection."""
    setUp = raw.ConductorClockTests.setUp
    health = staticmethod(raw.ConductorClockTests.health)
    make_charger = raw.ConductorClockTests.make_charger
    assert_no_commands = raw.ConductorClockTests.assert_no_commands
    advance = raw.ConductorClockTests.advance

    async def asyncSetUp(self):
        self.signal = SignalRClient('https://offline.invalid/hub')
        self.sdk = object.__new__(Easee)
        self.sdk.sr_connection = self.signal
        self.sdk.sr_subscriptions = {self.charger.id: self.native_publication_projection}
        self.signal.on('ProductUpdate', self.sdk._sr_product_update_cb)
        self.charger.easee = self.sdk
        self.hass.loop = SimpleNamespace(time=lambda: self.mono)
        self.hass.async_create_task = asyncio.create_task
        self.states = {
            'sensor.grid': SimpleNamespace(state='23', last_reported=self.now, attributes={'unit_of_measurement': 'A'}),
            'sensor.car': SimpleNamespace(state='0', last_reported=self.now, attributes={'unit_of_measurement': 'A'}),
            'sensor.status': SimpleNamespace(state='charging', last_reported=self.now, last_changed=self.now, attributes={}),
        }
        self.hass.states = SimpleNamespace(get=self.states.get)
        self.hass.config_entries = SimpleNamespace(async_update_entry=lambda *a, **k: None)
        entry = SimpleNamespace(title='Synthetic source', entry_id='atomic-test',
            data={'device_id': 'ha-device-id', 'controlled_phase': 'L1', 'l1_entity': 'sensor.grid',
                  'status_entity': 'sensor.status', 'charger_current_entity': 'sensor.car'},
            options={'active_control': True, 'dry_run': False, 'measurement_settle_seconds': 25,
                     'enable_soft_guard': False, 'enable_pause_resume': False,
                     'nominal_import_only_fallback': True})
        self.runtime_clock = patch.object(runtime, 'datetime', adapter.datetime)
        self.runtime_clock.start()
        self.addCleanup(self.runtime_clock.stop)
        self.planner = runtime.OneFuseLoaderController(self.hass, entry, self.client)
        self.planner._configured_control_identity = Mock(return_value='synthetic-selected-source')
        self.planner._handle_source_change = Mock()
        self.client.async_read_current_snapshot = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.energy_id = ChargerStreamData.state_lifetimeEnergy.value
        self.client.native_current_observation  # Bind additive observer AFTER SDK.

    async def asyncTearDown(self):
        await self.planner.async_stop()

    async def native_publication_projection(self, mid, data_type, observation_id, value):
        # Native Easee0.9.74 sets each state and notifies HA before processing
        # the next observation. A later lifetimeEnergy awaits cost refresh.
        self.product.state['latestPulse'] = self.now
        if observation_id in (182, 183, 184, 185):
            self.product.state[f'inCurrentT{observation_id-180}'] = value
            number = max(self.product.state[f'inCurrentT{x}'] for x in (2, 3, 4, 5))
            self.states['sensor.car'].state = format(number, '.15g')
            self.states['sensor.car'].last_reported = self.now
        elif observation_id == self.energy_id:
            self.entered.set()
            await self.release.wait()

    async def dispatch(self, values, *, energy_barrier=False):
        records = [{'mid': self.charger.id, 'id': key, 'dataType': 3,
                    'value': str(value), 'timestamp': self.now.isoformat()} for key, value in values.items()]
        if energy_barrier:
            records.append({'mid': self.charger.id, 'id': self.energy_id, 'dataType': 3, 'value': '1'})
        await self.signal._on_invocation_message(InvocationMessage(
            invocation_id=None, target='ProductUpdate', arguments=records))

    async def prepare_accepted_eight(self):
        await self.dispatch({182: 10., 183: 0., 184: 0., 185: 0.})
        self.advance(.3)
        self.states['sensor.grid'].last_reported = self.now
        r = self.planner.regulator
        inp = self.planner._read_input()
        legacy = replace(inp, charger_report_family='legacy', charger_report_provenance=None,
                         charger_event_source_known=False, charger_event_source_ts=None,
                         charger_observation_report_times=None, charger_observation_sequences=None)
        initial = r.evaluate(legacy)
        self.assertEqual(initial.current_p1, 10)
        self.assertTrue(initial.should_call_service)
        r.acknowledge(initial, self.mono)
        for _ in range(2):
            self.advance(.5)
            await self.dispatch({182: 10.})
            self.states['sensor.grid'].last_reported = self.now
            r.evaluate(self.planner._read_input())
        self.advance(.3)
        self.states['sensor.grid'].last_reported = self.now
        r.evaluate(self.planner._read_input())
        self.advance(1)
        self.states['sensor.grid'].state = '18.9'
        self.states['sensor.grid'].last_reported = self.now
        value = struct.unpack('<f', struct.pack('<f', 3.998))[0]
        await self.dispatch({182: value})
        self.advance(.3)
        self.states['sensor.grid'].last_reported = self.now
        lower = r.evaluate(self.planner._read_input())
        self.assertEqual(lower.current_p1, 8, lower)
        r.acknowledge(lower, self.mono)
        self.circuit.set_dynamic_current.reset_mock()

    async def test_real_sdk_native_publish_before_observer_never_erases_committed_clock(self):
        await self.prepare_accepted_eight()
        before = self.client.native_current_observation
        self.advance(.5)
        value = struct.unpack('<f', struct.pack('<f', 9.965))[0]
        task = asyncio.create_task(self.dispatch({182: value, 183: value}, energy_barrier=True))
        await asyncio.wait_for(self.entered.wait(), 1)
        try:
            pending = self.client.native_current_observation
            self.assertEqual(pending.sdk_current, value)
            self.assertEqual(pending.current, before.current)
            self.assertEqual(pending.received_at, before.received_at)
            self.assertEqual(self.states['sensor.car'].state, format(value, '.15g'))
            await self.planner.async_tick()
            self.circuit.set_dynamic_current.assert_not_awaited()
            self.assertGreater(self.planner.last_decision.current_p1, 0, self.planner.last_decision)
        finally:
            self.release.set()
            await task
        after = self.client.native_current_observation
        self.assertFalse(after.native_publication_pending)
        self.assertEqual(after.current, value)
        self.assertGreater(after.received_at, before.received_at)
        self.states['sensor.grid'].last_reported = self.now
        await self.planner.async_tick()
        self.assertGreater(self.planner.last_decision.current_p1, 0, self.planner.last_decision)

    async def test_supported_real_sdk_matches_matrix_pin(self):
        self.assertIn(REAL_SDK_VERSION, {'0.8.17', '0.8.19'})
        expected = os.environ.get('ONEFUSE_EXPECT_PYEASEE_VERSION')
        if expected:
            self.assertEqual(REAL_SDK_VERSION, expected)


if __name__ == '__main__':
    unittest.main()
