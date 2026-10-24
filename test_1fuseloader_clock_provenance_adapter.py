"""Synthetic per-conductor event clocks; no HA, network or device capture."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_snapshot_adapter as fixtures
import test_1fuseloader_observation_timestamps as timestamps

adapter = fixtures.adapter


class Connection:
    def __init__(self):
        self.handlers = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)


class ConductorClockTests(unittest.IsolatedAsyncioTestCase):
    health = staticmethod(fixtures.SnapshotAdapterTests.health)
    make_charger = fixtures.SnapshotAdapterTests.make_charger
    assert_no_commands = fixtures.SnapshotAdapterTests.assert_no_commands

    def setUp(self):
        fixtures.SnapshotAdapterTests.setUp(self)
        self.now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
        self.mono = 100.0
        owner = self
        real_datetime = datetime

        class ClockMeta(type):
            def __instancecheck__(cls, value):
                return isinstance(value, real_datetime)

        class Clock(real_datetime, metaclass=ClockMeta):
            @classmethod
            def now(cls, tz=None):
                return owner.now.astimezone(tz or timezone.utc)

        self.addCleanup(patch.stopall)
        patch.object(adapter, "datetime", Clock).start()
        patch.object(adapter, "_monotonic", lambda: owner.mono).start()
        self.connection = Connection()
        self.native_listener = AsyncMock()
        self.connection.on("ProductUpdate", self.native_listener)
        self.charger.easee = SimpleNamespace(sr_connection=self.connection)
        self.product.state.update({f"inCurrentT{phase}": 0.0 for phase in (2, 3, 4, 5)})
        self.product.state["latestPulse"] = self.now
        self.observer = Mock()
        self.unsubscribe = self.client.set_current_observation_callback(self.observer)

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds
        self.product.state["latestPulse"] = self.now

    async def emit(self, values, *, stamp=None, extra=()):
        records = []
        for key, value in values.items():
            self.product.state[f"inCurrentT{key-180}"] = value
            record = {"mid": self.charger.id, "id": key, "dataType": 3, "value": str(value)}
            if stamp is not None:
                record["timestamp"] = stamp.isoformat() if isinstance(stamp, datetime) else stamp
            records.append(record)
        records.extend(extra)
        for handler in tuple(self.connection.handlers["ProductUpdate"]):
            await handler(records)
        return self.client.native_current_observation

    async def test_hook_bootstrap_has_zero_sequences_without_invented_times(self):
        evidence = self.client.native_current_observation
        self.assertEqual(evidence.observation_ids, (182, 183, 184, 185))
        self.assertEqual(evidence.observation_sequences, ((182, 0), (183, 0), (184, 0), (185, 0)))
        self.assertEqual(evidence.observation_report_times, ())
        self.assertIsNone(evidence.received_at)
        self.assert_no_commands()

    async def test_nonwinner_attribute_fanout_never_renews_old_maximum(self):
        first = await self.emit({182: 4.0, 183: 7.25, 184: 0.0, 185: 0.0})
        self.advance(400)
        second = await self.emit({182: 0.0})
        self.assertEqual(second.current, 7.25)
        self.assertEqual(second.received_at, first.received_at)
        self.assertEqual(dict(second.observation_sequences)[183], 1)
        self.assertEqual(dict(second.observation_sequences)[182], 2)
        self.assertEqual(second.observation_ids, (183,))
        self.native_listener.assert_awaited()
        self.assert_no_commands()

    async def test_same_value_duplicate_has_no_new_receipt_or_sequence(self):
        first = await self.emit({182: 8.0, 183: 0.0, 184: 0.0, 185: 0.0})
        self.advance(20)
        second = await self.emit({182: 8.0})
        self.assertEqual(second.received_at, first.received_at)
        self.assertEqual(second.observation_sequences, first.observation_sequences)
        self.assertEqual(second.last_tuple_change_monotonic, first.last_tuple_change_monotonic)

    async def test_cached_winner_change_and_fresh_winner_change_are_distinct(self):
        first = await self.emit({182: 12.0, 183: 11.0, 184: 0.0, 185: 0.0})
        self.advance(2)
        cached = await self.emit({182: 10.0})
        self.assertEqual(cached.observation_ids, (183,))
        self.assertEqual(cached.received_at, first.received_at)
        self.assertEqual(dict(cached.observation_sequences)[183], 1)
        self.advance(1)
        fresh = await self.emit({183: 13.0})
        self.assertGreater(fresh.received_at, cached.received_at)
        self.assertEqual(dict(fresh.observation_sequences)[183], 2)

    async def test_tied_maximum_uses_oldest_real_receipt(self):
        first = await self.emit({182: 9.0, 183: 0.0, 184: 0.0, 185: 0.0})
        self.advance(2)
        second = await self.emit({183: 9.0})
        self.assertEqual(second.observation_ids, (182, 183))
        self.assertEqual(second.received_at, first.received_at)
        self.assertEqual(len(second.observation_report_times), 4)

    async def test_value_cycle_cannot_reuse_old_provider_metadata(self):
        old = self.now-timedelta(seconds=20)
        await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0}, stamp=old)
        self.advance()
        second = await self.emit({182: 5.0})
        self.assertIsNone(dict(second.source_timestamps)[182])
        self.advance()
        third = await self.emit({182: 7.0})
        receipt = third.received_at
        self.advance()
        fourth = await self.emit({182: 7.0}, stamp=old)
        self.assertEqual(fourth.received_at, receipt)
        self.assertIsNone(dict(fourth.source_timestamps)[182])
        self.assertEqual(dict(fourth.source_metadata_highwaters)[182], old)

    async def test_new_raw_source_event_can_recover_unbound_value_epoch_without_retiming_it(self):
        await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0},
                        stamp=self.now-timedelta(seconds=20))
        self.advance(5)
        await self.emit({182: 0.0})
        self.advance(5)
        before = await self.emit({182: 7.0})
        old_receipt = before.received_at
        old_sequence = dict(before.observation_sequences)[182]
        old_change = before.last_tuple_change_monotonic
        self.advance(10)
        source = old_receipt+timedelta(seconds=5)
        after = await self.emit({182: 7.0}, stamp=source)
        self.assertEqual(before.received_at, old_receipt)
        self.assertGreater(after.received_at, old_receipt)
        self.assertEqual(dict(after.observation_sequences)[182], old_sequence+1)
        self.assertEqual(dict(after.source_timestamps)[182], source)
        self.assertGreater(after.last_tuple_change_monotonic, old_change)
        self.assert_no_commands()

    async def test_new_same_value_source_frame_and_adjacent_conductor_frame_extend_quiet_only(self):
        await self.emit({182: 7.0, 183: 6.0, 184: 0.0, 185: 0.0}, stamp=self.now)
        self.advance(1)
        recovered = await self.emit({182: 7.0}, stamp=self.now)
        self.assertEqual(recovered.last_tuple_change_monotonic, self.mono)
        self.advance(.02)
        adjacent = await self.emit({183: 0.0})
        self.assertEqual(adjacent.current, recovered.current)
        self.assertEqual(adjacent.received_at, recovered.received_at)
        self.assertGreater(adjacent.last_tuple_change_monotonic, recovered.last_tuple_change_monotonic)
        self.advance(.02)
        duplicate = await self.emit({182: 7.0}, stamp=self.now-timedelta(seconds=.04))
        self.assertEqual(duplicate.received_at, recovered.received_at)
        self.assertEqual(duplicate.last_tuple_change_monotonic, adjacent.last_tuple_change_monotonic)

    async def test_old_or_equal_raw_metadata_cannot_recover_unknown_epoch(self):
        source = self.now-timedelta(seconds=20)
        await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0}, stamp=source)
        self.advance(5)
        await self.emit({182: 0.0})
        self.advance(5)
        before = await self.emit({182: 7.0})
        self.advance(10)
        for stamp in (source, before.received_at-timedelta(seconds=1), before.received_at):
            with self.subTest(stamp=stamp):
                after = await self.emit({182: 7.0}, stamp=stamp)
                self.assertEqual(after.received_at, before.received_at)
                self.assertEqual(after.observation_sequences, before.observation_sequences)
                self.assertIsNone(dict(after.source_timestamps)[182])

    async def test_true_per_terminal_provider_replay_stays_invalid_after_value_cycle(self):
        old = self.now-timedelta(seconds=20)
        await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0}, stamp=old)
        self.advance()
        await self.emit({182: 5.0})
        result = await self.emit({182: 7.0}, stamp=old-timedelta(seconds=1))
        self.assertEqual(result.error, "easee_observation_timestamp_regressed")
        self.assertIsNone(result.received_at)

    async def test_invalid_winning_metadata_and_conflicting_duplicate_fail_closed(self):
        await self.emit({182: 8.0, 183: 0.0, 184: 0.0, 185: 0.0})
        for invalid in (None, True, "NaN", "Infinity"):
            with self.subTest(invalid=invalid):
                self.client._receive_current_observations(
                    [{"mid": self.charger.id, "id": 182, "dataType": 3, "value": invalid}], self.charger.id)
                self.assertIsNotNone(self.client.native_current_observation.error)
        result = await self.emit({182: 8.0}, extra=[
            {"mid": self.charger.id, "id": 182, "dataType": 3, "value": "9.0"}])
        self.assertEqual(result.error, "conflicting_terminal_observations")

    async def test_callback_is_additive_once_per_batch_and_unsubscribe_or_close_is_inert(self):
        await self.emit({182: 7.0, 183: 6.0, 184: 0.0, 185: 0.0})
        self.observer.assert_called_once_with()
        self.native_listener.assert_awaited_once()
        self.unsubscribe()
        await self.emit({182: 8.0})
        self.observer.assert_called_once_with()
        old_handler = self.connection.handlers["ProductUpdate"][-1]
        await self.client.async_close()
        await old_handler([{"mid": self.charger.id, "id": 182, "dataType": 3, "value": "15"}])
        self.observer.assert_called_once_with()
        self.assertEqual(self.client._native_current_receipts, {})

    async def test_runtime_replacement_clears_receipts_but_stream_reconnect_does_not(self):
        first = await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0})
        self.charger.easee.sr_connection = Connection()
        self.assertEqual(self.client.native_current_observation.received_at, first.received_at)
        replacement = SimpleNamespace(product=self.charger, state=dict(self.product.state))
        self.controller.chargers_data = [replacement]
        self.assertIsNone(self.client.native_current_observation.received_at)
        self.assertEqual(dict(self.client.native_current_observation.observation_sequences)[182], 0)

    async def test_callback_from_previous_connection_is_inert_after_rebind(self):
        await self.emit({182: 7.0, 183: 0.0, 184: 0.0, 185: 0.0})
        previous_handler = self.connection.handlers['ProductUpdate'][-1]
        self.connection = Connection()
        self.charger.easee.sr_connection = self.connection
        before = self.client.native_current_observation
        self.observer.reset_mock()
        await previous_handler([{'mid': self.charger.id, 'id': 182, 'dataType': 3, 'value': '16'}])
        after = self.client.native_current_observation
        self.assertEqual(after.received_at, before.received_at)
        self.assertEqual(after.observation_sequences, before.observation_sequences)
        self.observer.assert_not_called()


class VendorMaximumClockTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SnapshotAdapterTests.setUp
    health = staticmethod(fixtures.SnapshotAdapterTests.health)
    make_charger = fixtures.SnapshotAdapterTests.make_charger
    assert_no_commands = fixtures.SnapshotAdapterTests.assert_no_commands
    state = timestamps.ObservationTimestampTests.state
    read = timestamps.ObservationTimestampTests.read

    async def test_maximum_ids_are_immutable_and_include_all_ties_or_zeros(self):
        state = self.state(10)
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current_source_observation_ids, (182,))
        zeros = await self.read(self.state(0, at=datetime.now(timezone.utc)))
        self.assertEqual(zeros.current_source_observation_ids, (182, 183, 184, 185))

    async def test_maximum_clock_can_regress_on_cached_winner_without_per_id_replay(self):
        now = datetime.now(timezone.utc)
        state = self.state(12, at=now-timedelta(seconds=5))
        for row in state['observations']:
            if row['id'] == 183:
                row.update(value='11.0', timestamp=(now-timedelta(seconds=10)).isoformat())
        first = await self.read(state)
        for row in state['observations']:
            if row['id'] == 182:
                row.update(value='10.0', timestamp=(now-timedelta(seconds=1)).isoformat())
        second = await self.read(state)
        self.assertEqual(first.current_source_observation_ids, (182,))
        self.assertEqual(second.current_source_observation_ids, (183,))
        self.assertLess(second.current_source_timestamp, first.current_source_timestamp)
        self.assertIsNone(second.source_timestamp_error)


if __name__ == '__main__':
    unittest.main()
