"""Vendor observation metadata remains distinct from a fresh HTTP receipt.

Fixtures use the public GET Observations timestamp schema, not a physical
device capture. No network, charging commands or physical sampling is implied.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

import test_1fuseloader_snapshot_adapter as fixtures


class ObservationTimestampTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SnapshotAdapterTests.setUp
    health = staticmethod(fixtures.SnapshotAdapterTests.health)
    make_charger = fixtures.SnapshotAdapterTests.make_charger
    assert_no_commands = fixtures.SnapshotAdapterTests.assert_no_commands

    def state(self, current=10.0, *, at=None, status_at=None):
        at = at or datetime.now(timezone.utc) - timedelta(seconds=10)
        rows = [dict(row) for row in fixtures.snapshot_state(current, 3)["observations"]]
        for row in rows:
            row["timestamp"] = (status_at if row["id"] == 109 and status_at else at).isoformat()
        return {"observations": rows}

    async def read(self, state):
        self.charger.get_observations.return_value = state
        snapshot = await self.client.async_read_current_snapshot()
        self.assert_no_commands()
        return snapshot

    async def test_four_argument_snapshots_remain_compatible_and_immutable(self):
        now = datetime.now(timezone.utc)
        snapshot = fixtures.adapter.ChargerCurrentSnapshot(6, "charging", now, (1, 2, 3, 4))
        self.assertIsNone(snapshot.current_source_timestamp)
        self.assertIsNone(snapshot.status_source_timestamp)
        self.assertEqual(snapshot.current_source_timestamps, ())
        self.assertIsNone(snapshot.source_timestamp_error)
        with self.assertRaises(FrozenInstanceError):
            snapshot.current_source_timestamp = now

    async def test_old_source_timestamp_is_retained_with_a_new_local_receipt(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        snapshot = await self.read(self.state(at=old))
        self.assertEqual(snapshot.current_source_timestamp, old)
        self.assertEqual(snapshot.status_source_timestamp, old)
        self.assertGreater(snapshot.received_at, old + timedelta(hours=1))
        self.assertIsNone(snapshot.source_timestamp_error)
        self.assertEqual(dict(snapshot.current_source_timestamps)[182], old)

    async def test_repeated_get_does_not_advance_unchanged_source_metadata(self):
        state = self.state()
        first = await self.read(state)
        second = await self.read(state)
        self.assertEqual(second.current_source_timestamp, first.current_source_timestamp)
        self.assertGreaterEqual(second.received_at, first.received_at)
        self.assertIsNone(second.source_timestamp_error)

    async def test_missing_timestamp_keeps_values_but_explicitly_marks_unknown_current_metadata(self):
        snapshot = await self.read(fixtures.snapshot_state(10, 3))
        self.assertEqual((snapshot.current, snapshot.status), (10, "charging"))
        self.assertIsNone(snapshot.current_source_timestamp)
        self.assertIsNone(snapshot.status_source_timestamp)
        self.assertEqual(snapshot.source_timestamp_error, "easee_observation_timestamp_missing")
        self.assertEqual(snapshot.current_source_timestamps, ((182, None), (183, None), (184, None), (185, None)))

    async def test_old_event_driven_status_and_unused_zero_do_not_age_a_fresh_positive_maximum(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        old = at - timedelta(days=3)
        state = self.state(at=at, status_at=old)
        for row in state["observations"]:
            if row["id"] in (183, 184, 185):
                row["timestamp"] = old.isoformat()
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current_source_timestamp, at)
        self.assertEqual(snapshot.status_source_timestamp, old)
        self.assertIsNone(snapshot.source_timestamp_error)

    async def test_invalid_unused_zero_metadata_does_not_poison_positive_current(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        for value in (None, "private-url-or-token", True,
                      (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()):
            with self.subTest(value_type=type(value).__name__):
                state = self.state(at=at)
                state["observations"][2]["timestamp"] = value
                snapshot = await self.read(state)
                self.assertEqual(snapshot.current_source_timestamp, at)
                self.assertIsNone(dict(snapshot.current_source_timestamps)[183])
                self.assertIsNone(snapshot.source_timestamp_error)
                self.assertNotIn("private-url-or-token", repr(snapshot))

    async def test_equal_positive_maxima_choose_the_oldest_relevant_timestamp(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        older = at - timedelta(seconds=30)
        state = self.state(at=at)
        state["observations"][2].update(value=10.0, timestamp=older.isoformat())
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current_source_timestamp, older)
        self.assertIsNone(snapshot.source_timestamp_error)

    async def test_invalid_equal_maximum_cannot_be_hidden_by_another_valid_maximum(self):
        state = self.state()
        state["observations"][2].update(value=10.0, timestamp=None)
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current, 10)
        self.assertIsNone(snapshot.current_source_timestamp)
        self.assertEqual(snapshot.source_timestamp_error, "easee_observation_timestamp_missing")

    async def test_all_zero_uses_conservative_oldest_metadata_without_inventing_active_phase(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        old = at - timedelta(hours=2)
        state = self.state(0, at=at)
        state["observations"][4]["timestamp"] = old.isoformat()
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current, 0)
        self.assertEqual(snapshot.current_source_timestamp, old)

    async def test_backward_current_timestamp_is_unknown_and_does_not_lower_the_high_water(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await self.read(self.state(at=at))
        for old in (at - timedelta(seconds=2), at - timedelta(seconds=1)):
            snapshot = await self.read(self.state(at=old))
            self.assertIsNone(snapshot.current_source_timestamp)
            self.assertEqual(snapshot.source_timestamp_error, "easee_observation_timestamp_regressed")
        recovered = await self.read(self.state(8, at=at + timedelta(seconds=1)))
        self.assertEqual(recovered.current_source_timestamp, at + timedelta(seconds=1))
        self.assertIsNone(recovered.source_timestamp_error)

    async def test_changed_value_at_identical_source_time_is_not_new_measurement_evidence(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await self.read(self.state(at=at))
        snapshot = await self.read(self.state(8, at=at))
        self.assertEqual(snapshot.current, 8)
        self.assertIsNone(snapshot.current_source_timestamp)
        self.assertEqual(snapshot.source_timestamp_error, "easee_observation_timestamp_value_changed")

    async def test_regressed_unused_zero_or_status_does_not_invalidate_the_current_maximum(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await self.read(self.state(at=at))
        state = self.state(at=at)
        for row in state["observations"]:
            if row["id"] in (109, 183):
                row["timestamp"] = (at - timedelta(seconds=1)).isoformat()
        snapshot = await self.read(state)
        self.assertEqual(snapshot.current_source_timestamp, at)
        self.assertIsNone(snapshot.status_source_timestamp)
        self.assertIsNone(dict(snapshot.current_source_timestamps)[183])
        self.assertIsNone(snapshot.source_timestamp_error)

    async def test_timestamp_history_is_scoped_to_actual_selected_runtime(self):
        at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await self.read(self.state(at=at))
        replacement = self.make_charger(self.circuit)
        replacement.get_observations.return_value = self.state(at=at - timedelta(seconds=10))
        self.controller.get_chargers.return_value = [replacement]
        self.product.product = replacement
        snapshot = await self.client.async_read_current_snapshot()
        self.assertEqual(snapshot.current_source_timestamp, at - timedelta(seconds=10))
        self.assertIsNone(snapshot.source_timestamp_error)
        self.assert_no_commands()

    def test_parser_requires_explicit_rfc3339_timezone_and_normalizes_utc(self):
        received = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        for value in ("2026-10-20T16:00:00+02:00", "2026-10-20T14:00:00Z",
                      "2026-10-20t14:00:00z"):
            with self.subTest(value=value):
                parsed, error = fixtures.adapter.EaseeCommandAdapter._observation_timestamp(value, received)
                self.assertEqual(parsed, received)
                self.assertIs(parsed.tzinfo, timezone.utc)
                self.assertIsNone(error)

    def test_parser_rejects_malformed_naive_numeric_and_unknown_offset_without_echoing_input(self):
        received = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        invalid = (True, 1792504800, float("nan"), float("inf"), received, "", "private-token",
                   "2026-10-20", "2026-10-20T14:00:00", "2026-10-20 14:00:00Z",
                   "2026-10-20T14:00:00-00:00", "2026-02-30T14:00:00Z",
                   "2026-10-20T24:00:00Z", "2026-10-20T14:00:60Z",
                   "2026-10-20T14:00:00+25:00", "2026-10-20T14:00:00+00:60",
                   "2026-10-20T14:00:00+02:99", "2026-10-20T14:00:00-00:60",
                   "2026-10-20T14:00:00Z trailing")
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                parsed, error = fixtures.adapter.EaseeCommandAdapter._observation_timestamp(value, received)
                self.assertIsNone(parsed)
                self.assertEqual(error, "easee_observation_timestamp_invalid")

    def test_future_tolerance_boundary_is_separate_from_local_receipt(self):
        received = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        for delta, expected_error in ((2, None), (2.000001, "easee_observation_timestamp_future")):
            value = (received + timedelta(seconds=delta)).isoformat()
            parsed, error = fixtures.adapter.EaseeCommandAdapter._observation_timestamp(value, received)
            self.assertEqual(error, expected_error)
            self.assertEqual(parsed, received + timedelta(seconds=delta) if error is None else None)


if __name__ == "__main__":
    unittest.main()
