"""Observation provenance and bounded local incident evidence; no network."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_1fuseloader_snapshot_runtime as snapshots
import test_1fuseloader_runtime as fixtures


class EvidenceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = snapshots.SnapshotRuntimeTests.asyncSetUp
    update_entry = snapshots.SnapshotRuntimeTests.update_entry
    snapshot = snapshots.SnapshotRuntimeTests.snapshot
    install = snapshots.SnapshotRuntimeTests.install

    async def test_http_receipt_never_replaces_observation_clock(self):
        received = datetime.now(timezone.utc)
        observed = received - timedelta(seconds=5)
        for state in self.states.values():
            state.last_reported = received - timedelta(seconds=10)
        item = self.snapshot(received=received)
        item.current_source_timestamp = observed
        self.install(item)
        inp = self.controller._read_input()
        self.assertTrue(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_report_ts, observed.timestamp())
        self.assertNotEqual(inp.charger_report_ts, received.timestamp())

    async def test_get_of_old_observation_cannot_replace_newer_native_value(self):
        received = datetime.now(timezone.utc)
        self.states['sensor.car'].state = '6'
        self.states['sensor.car'].last_reported = received - timedelta(seconds=2)
        item = self.snapshot(current=16, received=received)
        item.current_source_timestamp = received - timedelta(seconds=30)
        self.install(item)
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_current, '6')
        self.assertEqual(self.controller.source_diagnostics['charger_snapshot_source_rejection'],
                         'observation_precedes_native_report')

    async def test_legacy_snapshot_has_no_clock_credit(self):
        self.install(SimpleNamespace(current=16, status='charging', received_at=datetime.now(timezone.utc)))
        before = self.states['sensor.car'].last_reported.timestamp()
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_report_ts, before)
        self.assertEqual(self.controller.source_diagnostics['charger_snapshot_source_rejection'],
                         'observation_timestamp_missing')

    async def test_bad_or_future_source_metadata_does_not_poison_independent_native_input(self):
        for error, offset in [('regressed', 0), (None, 10), (None, -121)]:
            with self.subTest(error=error, offset=offset):
                item = self.snapshot()
                item.source_timestamp_error = error
                item.current_source_timestamp = datetime.now(timezone.utc)+timedelta(seconds=offset)
                self.install(item)
                inp = self.controller._read_input()
                self.assertFalse(inp.charger_snapshot_verified)
                self.assertTrue(inp.data_valid)
                self.assertEqual(inp.charger_current, self.states['sensor.car'].state)

    async def test_old_event_driven_status_timestamp_does_not_require_new_status_change(self):
        item = self.snapshot()
        item.status_source_timestamp = datetime.now(timezone.utc)-timedelta(days=1)
        self.install(item)
        self.assertTrue(self.controller._read_input().charger_snapshot_verified)

    async def test_fresh_current_cannot_replace_native_pause_with_unverified_get_status(self):
        for status in ('paused', 'awaiting_scheduled_start', 'awaiting_authorization', 'disconnected'):
            for status_time in (None, datetime.now(timezone.utc)-timedelta(days=1), datetime.now(timezone.utc)):
                with self.subTest(status=status, status_time=status_time):
                    self.states['sensor.status'].state = status
                    item = self.snapshot(current=16, status='charging')
                    item.status_source_timestamp = status_time
                    self.install(item)
                    inp = self.controller._read_input()
                    self.assertFalse(inp.charger_snapshot_verified)
                    self.assertEqual(inp.status, status)

    async def test_stop_journal_is_durable_bounded_and_cannot_change_budget(self):
        for index in range(50):
            self.controller._recent_commands.append({'target_p1': index, 'result': 'submitted_unverified'})
        before = self.controller.regulator.export_overload_state()
        self.controller._recent_stop_contexts.append({'reason': 'synthetic incident'})
        self.controller._queue_incident_journal()
        await self.controller._incident_journal_task
        key = f'1fuseloader.{self.entry.entry_id}.incident_journal'
        saved = self.hass._test_store_disk[key]
        self.assertEqual(len(saved['commands']), 32)
        self.assertEqual(saved['commands'][0]['target_p1'], 18)
        self.assertEqual(saved['stops'][0]['reason'], 'synthetic incident')
        self.assertEqual(self.controller.regulator.export_overload_state(), before)

    async def test_diagnostic_write_failure_is_sanitized_and_not_a_control_failure(self):
        class FailingStore:
            def __init__(self, *args, **kwargs):
                pass
            async def async_save(self, data):
                raise OSError('secret-must-not-be-logged')
        before = self.controller.regulator.export_overload_state()
        with patch.object(fixtures.runtime, 'Store', FailingStore):
            with self.assertLogs(fixtures.runtime._LOGGER, level='WARNING') as logs:
                self.controller._queue_incident_journal()
                await self.controller._incident_journal_task
        self.assertNotIn('secret-must-not-be-logged', str(logs.output))
        self.assertEqual(self.controller._incident_journal_error, 'OSError')
        self.assertEqual(self.controller.regulator.export_overload_state(), before)

    async def test_matching_old_event_retains_clock_without_fault_or_false_receipt_credit(self):
        now = datetime.now(timezone.utc)
        old = now-timedelta(hours=2)
        self.states['sensor.car'].state = '8'
        self.states['sensor.car'].last_reported = now-timedelta(seconds=1)
        item = self.snapshot(current=8, received=now)
        item.current_source_timestamp = old
        self.install(item)
        for _ in range(2):
            inp = self.controller._read_input()
            self.assertTrue(inp.data_valid)
            self.assertTrue(inp.charger_current_trusted)
            self.assertEqual(inp.charger_report_ts, old.timestamp())
            self.assertFalse(inp.charger_snapshot_verified)
        self.states['sensor.car'].last_reported = datetime.now(timezone.utc)
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_report_ts, old.timestamp())
        self.assertIn('retained_vendor_event', self.controller.source_diagnostics['effective_current_clock_scope'])

    async def test_matching_invalid_metadata_is_untrusted_until_numeric_native_value_changes(self):
        cases = [('easee_observation_timestamp_regressed', None),
                 ('easee_observation_timestamp_value_changed', None),
                 (None, 'not-an-observation-time'),
                 (None, datetime.now(timezone.utc)+timedelta(minutes=1))]
        for error, stamp in cases:
            with self.subTest(error=error, stamp_type=type(stamp).__name__):
                self.controller._charger_vendor_evidence = None
                self.states['sensor.car'].state = '8'
                item = self.snapshot(current=8)
                item.current_source_timestamp = stamp
                item.source_timestamp_error = error
                self.install(item)
                for _ in range(2):
                    inp = self.controller._read_input()
                    self.assertTrue(inp.data_valid)
                    self.assertFalse(inp.charger_current_trusted)
                self.states['sensor.car'].state = '6'
                self.states['sensor.car'].last_reported = datetime.now(timezone.utc)
                inp = self.controller._read_input()
                self.assertTrue(inp.charger_current_trusted)
                self.assertEqual(inp.charger_current, '6')

    async def test_missing_metadata_alone_does_not_poison_matching_independent_native_value(self):
        for error in (None, 'easee_observation_timestamp_missing'):
            item = self.snapshot(current=8)
            item.current_source_timestamp = None
            item.source_timestamp_error = error
            self.install(item)
            before = self.states['sensor.car'].last_reported.timestamp()
            inp = self.controller._read_input()
            self.assertTrue(inp.charger_current_trusted)
            self.assertEqual(inp.charger_report_ts, before)
            self.assertIsNone(self.controller._charger_vendor_evidence)

    async def test_vendor_evidence_is_discarded_on_generation_adapter_or_source_identity_change(self):
        for mutation in ('generation', 'adapter', 'source'):
            with self.subTest(mutation=mutation):
                self.adapter.snapshot_is_current.return_value = True
                self.controller._adapter = self.adapter
                item = self.snapshot(current=8)
                item.current_source_timestamp = datetime.now(timezone.utc)-timedelta(hours=2)
                self.install(item)
                self.controller._read_input()
                self.assertIsNotNone(self.controller._charger_vendor_evidence)
                if mutation == 'generation':
                    self.controller._generation += 1
                elif mutation == 'adapter':
                    self.controller._adapter = SimpleNamespace(telemetry_block_reason=None,
                        snapshot_is_current=lambda item: True)
                else:
                    self.adapter.snapshot_is_current.return_value = False
                inp = self.controller._read_input()
                self.assertIsNone(self.controller._charger_vendor_evidence)
                self.assertEqual(inp.charger_report_ts, self.states['sensor.car'].last_reported.timestamp())

    async def test_matching_event_clock_does_not_allow_get_to_override_native_manual_pause(self):
        self.states['sensor.status'].state = 'paused'
        item = self.snapshot(current=8, status='charging')
        item.current_source_timestamp = datetime.now(timezone.utc)-timedelta(hours=2)
        self.install(item)
        inp = self.controller._read_input()
        self.assertEqual(inp.status, 'paused')
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertTrue(inp.charger_current_trusted)
        self.assertEqual(inp.charger_report_ts, item.current_source_timestamp.timestamp())

    async def test_journal_reload_serializes_actual_old_io_even_after_diagnostic_timeout(self):
        self.hass.data = {}
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        class SlowStore:
            def __init__(self, *args, **kwargs):
                pass
            async def async_save(self, payload):
                tag = payload['commands'][0]['tag']
                if tag == 'old':
                    entered.set()
                    await release.wait()
                writes.append(tag)
        with patch.object(fixtures.runtime, 'Store', SlowStore), patch.object(
                fixtures.runtime, 'EXPOSURE_STORE_TIMEOUT_SECONDS', .01):
            self.controller._recent_commands.append({'tag': 'old'})
            self.controller._queue_incident_journal()
            old_task = self.controller._incident_journal_task
            await entered.wait()
            await self.controller.async_stop()
            replacement = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
            replacement._recent_commands.append({'tag': 'new'})
            replacement._queue_incident_journal()
            new_task = replacement._incident_journal_task
            await asyncio.sleep(.03)
            self.assertEqual(self.controller._incident_journal_error, 'TimeoutError')
            self.assertFalse(old_task.done())
            self.assertFalse(new_task.done())
            self.assertEqual(writes, [])
            release.set()
            await asyncio.gather(old_task, new_task)
            self.assertEqual(writes, ['old', 'new'])

    async def test_journal_skips_obsolete_payload_waiting_for_the_entry_writer(self):
        self.hass.data = {}
        first = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        second = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        writes = []
        class RecordingStore:
            def __init__(self, *args, **kwargs):
                pass
            async def async_save(self, payload):
                writes.append(payload['commands'][0]['tag'])
        lock = first._incident_journal_writer['lock']
        await lock.acquire()
        with patch.object(fixtures.runtime, 'Store', RecordingStore):
            first._recent_commands.append({'tag': 'obsolete'})
            first._queue_incident_journal()
            second._recent_commands.append({'tag': 'latest'})
            second._queue_incident_journal()
            await asyncio.sleep(0)
            lock.release()
            await asyncio.gather(first._incident_journal_task, second._incident_journal_task)
        self.assertEqual(writes, ['latest'])

    async def test_cancelled_journal_retains_lock_until_actual_old_write_finishes(self):
        self.hass.data = {}
        first = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        second = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        class SlowStore:
            def __init__(self, *args, **kwargs):
                pass
            async def async_save(self, payload):
                tag = payload['commands'][0]['tag']
                if tag == 'old':
                    entered.set()
                    await release.wait()
                writes.append(tag)
        with patch.object(fixtures.runtime, 'Store', SlowStore):
            first._recent_commands.append({'tag': 'old'})
            first._queue_incident_journal()
            old_task = first._incident_journal_task
            await entered.wait()
            for _ in range(2):
                old_task.cancel()
                await asyncio.sleep(0)
            second._recent_commands.append({'tag': 'new'})
            second._queue_incident_journal()
            new_task = second._incident_journal_task
            await asyncio.sleep(0)
            self.assertTrue(first._incident_journal_writer['lock'].locked())
            self.assertFalse(old_task.done())
            self.assertFalse(new_task.done())
            release.set()
            results = await asyncio.gather(old_task, new_task, return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertEqual(writes, ['old', 'new'])

    async def test_blocked_journal_does_not_block_protective_control_or_change_budget(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowStore:
            def __init__(self, *args, **kwargs):
                pass
            async def async_save(self, payload):
                entered.set()
                await release.wait()
        before = self.controller.regulator.export_overload_state()
        with patch.object(fixtures.runtime, 'Store', SlowStore):
            self.controller._queue_incident_journal()
            task = self.controller._incident_journal_task
            await entered.wait()
            self.states['sensor.grid'].state = '42'
            for state in self.states.values():
                state.last_reported = datetime.now(timezone.utc)
            await asyncio.wait_for(self.controller.async_tick(), .5)
            self.assertEqual(self.adapter.calls[-1].current_p1, 0)
            self.assertEqual(self.controller.regulator.export_overload_state(), before)
            release.set()
            await task

    async def test_old_event_get_cadence_uses_receipts_without_renewing_the_source_clock(self):
        import test_1fuseloader_refresh_deadlines as deadlines
        case = deadlines.RefreshDeadlineTests('test_prefetch_fires_at_half_window_without_watchdog_poll')
        await case.asyncSetUp()
        try:
            def unchanged():
                item = case.snapshot()
                item.current_source_timestamp = item.status_source_timestamp = case.wall(0)
                return item
            case.adapter.async_read_current_snapshot.side_effect = unchanged
            case.maintain()
            starts = []
            for _ in range(3):
                await case.fire()
                starts.append(case.now)
                count = case.adapter.async_read_current_snapshot.await_count
                inp = case.controller._read_input()
                self.assertEqual(inp.charger_report_ts, case.epoch)
                self.assertTrue(inp.charger_current_trusted)
                case.now += 1
                case.controller._request_current_snapshot()
                self.assertEqual(case.adapter.async_read_current_snapshot.await_count, count)
            self.assertEqual(starts, [12.5, 25, 37.5])
            case.now = 43
            case.controller.regulator.last_target = (8, 0, 0, 60)
            case.controller._request_current_snapshot()
            await case.controller._charger_snapshot_task
            self.assertEqual(case.adapter.async_read_current_snapshot.await_count, 4)
        finally:
            await case.asyncTearDown()


if __name__ == '__main__':
    unittest.main()
