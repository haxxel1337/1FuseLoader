"""Bounded passive incident context, with no physical or control authority.

All clocks, values and IDs below are synthetic. Source timestamps describe
provider metadata; a later callback or GET is never a new physical sample.
"""
from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_passive_native_journal as previous
import test_1fuseloader_runtime as fixtures


class DiagnosticOriginAdapterTests(unittest.IsolatedAsyncioTestCase):
    health = staticmethod(previous.PassiveAdapterTests.health)
    make_charger = previous.PassiveAdapterTests.make_charger
    assert_no_commands = previous.PassiveAdapterTests.assert_no_commands
    setUp = previous.PassiveAdapterTests.setUp

    async def test_actual_callback_keeps_old_provider_metadata_without_physical_credit(self):
        seen = []
        self.client.set_telemetry_callback(seen.append)
        old = self.now-timedelta(minutes=15)
        records = [{"mid":self.charger.id, "id":96, "dataType":4,
                    "value":3, "timestamp":old.isoformat()}]
        for handler in tuple(self.connection.handlers["ProductUpdate"]):
            await handler(records)
        item = seen[0][0].public()
        self.assertEqual(item['origin'], 'signalr_product_update_callback')
        self.assertEqual(item['source_at_utc'], old.isoformat())
        self.assertEqual(item['received_at_utc'], self.now.isoformat())
        self.assertFalse(item['control_evidence'])
        self.assertIn('cached_provider_data', item['scope'])
        self.assert_no_commands()

    async def test_get_and_unspecified_capture_do_not_claim_native_callback_origin(self):
        seen = []
        self.client.set_telemetry_callback(seen.append)
        await self.client.async_read_current_snapshot()
        self.assertEqual(seen, [], 'GET replies must not enter the passive callback journal')
        self.client._receive_telemetry_observations(
            {'mid':self.charger.id, 'id':48, 'value':16,
             'timestamp':self.now.isoformat()}, self.charger.id)
        self.assertEqual(seen[0][0].public()['origin'], 'unknown')
        self.client._receive_telemetry_observations(
            {'mid':self.charger.id, 'id':48, 'value':16}, self.charger.id,
            origin='PRIVATE_SENTINEL')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(seen[-1][0].public()))
        self.assert_no_commands()


class LatestDiagnosticControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = previous.PassiveControllerTests.update_entry
    wall = previous.PassiveControllerTests.wall
    schedule = previous.PassiveControllerTests.schedule
    active_timers = previous.PassiveControllerTests.active_timers
    asyncSetUp = previous.PassiveControllerTests.asyncSetUp
    asyncTearDown = previous.PassiveControllerTests.asyncTearDown
    set_native = previous.PassiveControllerTests.set_native
    snapshot = previous.PassiveControllerTests.snapshot
    install = previous.PassiveControllerTests.install

    def event(self, key, value, *, source=100., receipt=100., generation=1, batch=1, error=None,
              origin='signalr_product_update_callback'):
        return fixtures.runtime.NativeTelemetryObservation(key, value,
            self.wall(source).isoformat() if source is not None else None,
            self.wall(receipt).isoformat(), receipt, batch, generation, error,
            origin)

    async def test_rare_reason_and_limit_survive_fifo_noise_without_control_or_disk_effect(self):
        c = self.controller
        c._queue_incident_journal = Mock()
        before = deepcopy(c.regulator.export_overload_state())
        with patch.object(c, '_read_input', side_effect=AssertionError('diagnostic input')), \
                patch.object(c.regulator, 'evaluate', side_effect=AssertionError('diagnostic evaluation')), \
                patch.object(c, 'notify') as notify:
            c._handle_passive_telemetry((self.event(96, 3), self.event(48, 16.)))
            for at in range(101, 250):
                c._handle_passive_telemetry((self.event(183, 7., source=float(at), receipt=float(at)),))
            c._handle_passive_telemetry((self.event(999, 1.),))
        self.assertEqual(len(c._native_telemetry_events), 128)
        self.assertFalse(any(item['observation_id'] in (48, 96) for item in c._native_telemetry_events))
        context = c._passive_diagnostic_context()
        self.assertEqual(context['observations']['96']['last_received']['value'], 3)
        self.assertEqual(context['observations']['48']['last_received']['value'], 16.)
        self.assertNotIn('999', context['observations'])
        self.assertLessEqual(len(context['observations']), 16)
        self.assertFalse(context['observations']['96']['control_evidence'])
        self.assertEqual(c.regulator.export_overload_state(), before)
        self.assertEqual(self.adapter.calls, [])
        c._queue_incident_journal.assert_not_called()
        c._handle_source_change.assert_not_called()
        notify.assert_not_called()
        self.assertIsNone(c._passive_journal_unsub)

    async def test_duplicate_and_regressed_provider_events_do_not_refresh_highwater_receipt(self):
        c = self.controller
        c._handle_passive_telemetry((self.event(96, 3, source=10., receipt=20.),))
        original = deepcopy(c._passive_diagnostic_context()['observations']['96']['provider_highwater'])
        c._handle_passive_telemetry((self.event(96, 3, source=10., receipt=100., batch=2),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'duplicate_provider_timestamp')
        self.assertFalse(current['highwater_receipt_advanced'])
        self.assertEqual(current['provider_highwater'], original)
        self.assertEqual(current['last_received']['received_at_utc'], self.wall(100.).isoformat())
        c._handle_passive_telemetry((self.event(96, 1, source=9., receipt=110., batch=3),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'regressed_provider_timestamp')
        self.assertEqual(current['provider_highwater'], original)
        self.assertEqual(current['provider_age_at_receipt_seconds'], 101.)
        self.assertFalse(current['control_evidence'])
        c._handle_passive_telemetry((self.event(96, 4, source=111., receipt=112., batch=4,
                                             origin='unknown'),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'unknown_event_origin')
        self.assertEqual(current['last_received']['origin'], 'unknown')
        self.assertEqual(current['provider_highwater'], original)

    async def test_same_provider_bucket_change_and_missing_metadata_remain_explicit(self):
        c = self.controller
        c._handle_passive_telemetry((self.event(96, 3),))
        original = deepcopy(c._passive_diagnostic_context()['observations']['96']['provider_highwater'])
        c._handle_passive_telemetry((self.event(96, 1, receipt=101., batch=2),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'same_provider_timestamp_changed_value')
        self.assertEqual(current['provider_highwater'], original)
        c._handle_passive_telemetry((self.event(96, 0, source=None, receipt=102., batch=3),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'missing_provider_timestamp')
        self.assertEqual(current['provider_highwater'], original)
        c._handle_passive_telemetry((self.event(96, None, source=None, receipt=103., batch=4,
                                             error='easee_observation_timestamp_future'),))
        current = c._passive_diagnostic_context()['observations']['96']
        self.assertEqual(current['source_relation'], 'invalid_observation')
        self.assertEqual(current['provider_highwater'], original)
        self.assertFalse(current['highwater_receipt_advanced'])

    async def test_receiver_generation_discards_old_context_and_retired_callbacks(self):
        c = self.controller
        c._queue_incident_journal = Mock()
        c._handle_passive_telemetry((self.event(96, 3), self.event(114, 16.)))
        c._handle_passive_telemetry((self.event(48, 12., generation=2),
                                     self.event(114, 16., generation=2)))
        c._handle_passive_telemetry((self.event(96, 0, receipt=101., generation=1),
                                     self.event(114, 0., receipt=101., generation=1)))
        self.assertEqual(len(c._pilot_zero_observations), 0)
        context = c._passive_diagnostic_context()
        self.assertEqual(context['receiver_generation'], 2)
        self.assertNotIn('96', context['observations'])
        c._handle_passive_telemetry((self.event(114, 0., source=102., receipt=102., generation=2),))
        marker = c._pilot_zero_observations[-1]
        self.assertEqual(marker['diagnostic_context']['receiver_generation'], 2)
        self.assertNotIn('96', marker['diagnostic_context']['observations'])
        self.assertEqual(marker['diagnostic_context']['observations']['48']['last_received']['value'], 12.)

    async def test_pilot_context_includes_later_same_batch_reason_and_is_frozen(self):
        c = self.controller
        c._queue_incident_journal = Mock()
        c._handle_passive_telemetry((self.event(114, 16.),))
        c._handle_passive_telemetry((self.event(114, 0., batch=2),
                                     self.event(96, 3, batch=2), self.event(48, 12., batch=2)))
        marker = c._pilot_zero_observations[-1]
        context = marker['diagnostic_context']
        self.assertEqual(context['observations']['96']['last_received']['value'], 3)
        self.assertEqual(context['observations']['48']['last_received']['value'], 12.)
        self.assertIn('batch_end', context['scope'])
        self.assertIn('not_synchronized_or_control_proof', context['scope'])
        c._handle_passive_telemetry((self.event(96, 4, source=101., receipt=101., batch=3),))
        self.assertEqual(context['observations']['96']['last_received']['value'], 3)
        self.assertEqual(c._passive_diagnostic_context()['observations']['96']['last_received']['value'], 4)
        self.assertEqual(c._queue_incident_journal.call_count, 1)
        self.assertEqual(len(self.active_timers()), 1)

    async def test_latest_context_persists_only_in_private_incident_store(self):
        c = self.controller
        queue = c._queue_incident_journal
        c._queue_incident_journal = Mock()
        values = {21:1, 38:2, 48:32., 96:3, 100:'C', 109:3, 110:10,
                  111:16., 112:0., 113:0., 114:16., 120:3.7,
                  182:16.2, 183:16., 184:0., 185:0.}
        for index in range(8):
            at = 100.+index*2
            c._handle_passive_telemetry(tuple(self.event(key, value, source=at,
                receipt=at, batch=index*2+1) for key, value in values.items()))
            c._handle_passive_telemetry((self.event(114, 0., source=at+1,
                receipt=at+1, batch=index*2+2),))
        c._queue_incident_journal = queue
        c._queue_incident_journal()
        await c._incident_journal_task
        key = f'1fuseloader.{self.entry.entry_id}.incident_journal'
        payload = self.hass._test_store_disk[key]
        encoded = json.dumps(payload, allow_nan=False).encode('utf-8')
        self.persisted_payload_bytes = len(encoded)
        decoded = json.loads(encoded)
        self.assertIsNone(c._incident_journal_error)
        self.assertEqual(len(decoded['native_events']), 128)
        self.assertEqual(len(decoded['pilot_zero_observations']), 8)
        context = payload['native_latest_diagnostics']
        self.assertEqual(context['observations']['96']['last_received']['value'], 3)
        self.assertEqual(context['observations']['48']['last_received']['value'], 32.)
        self.assertEqual(set(context['observations']), {str(key) for key in values})
        self.assertEqual(decoded['native_latest_diagnostics'], context)
        for marker in decoded['pilot_zero_observations']:
            self.assertEqual(set(marker['diagnostic_context']['observations']), {str(key) for key in values})
        self.assertTrue({'commands', 'stops', 'native_events', 'transport_events',
                         'pilot_zero_observations', 'native_latest_diagnostics'} <= set(decoded))
        self.assertFalse(context['observations']['96']['control_evidence'])
        self.assertNotIn('native_latest_diagnostics', c.diagnostic_attributes())
        c._handle_passive_telemetry((self.event(96, 4, source=120., receipt=120., batch=17),))
        self.assertEqual(context['observations']['96']['last_received']['value'], 3)
        self.assertEqual(self.adapter.calls, [])


if __name__ == '__main__':
    unittest.main()
