"""Offline races for on-demand GET telemetry; never connects to HA or Easee."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_1fuseloader_runtime as fixtures


class SnapshotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.controller._handle_source_change = Mock()
        self.adapter.async_read_current_snapshot = AsyncMock(side_effect=self.snapshot)
        self.adapter.snapshot_is_current = Mock(return_value=True)

    def snapshot(self, current=8.0, status="charging", received=None):
        received = received or datetime.now(timezone.utc)
        return SimpleNamespace(current=current, status=status,
            received_at=received, current_source_timestamp=received,
            status_source_timestamp=received, source_timestamp_error=None)

    async def fetch(self):
        self.controller._request_current_snapshot()
        task = self.controller._charger_snapshot_task
        self.assertIsNotNone(task)
        await task

    def install(self, snapshot):
        self.controller._charger_snapshot = snapshot
        self.controller._charger_snapshot_selection = self.controller._charger_source_revision()

    async def test_get_is_read_only_and_requests_a_new_evaluation(self):
        await self.fetch()
        self.adapter.async_read_current_snapshot.assert_awaited_once()
        self.assertEqual(self.adapter.calls, [])
        self.controller._handle_source_change.assert_called_once_with(None)

    async def test_verified_snapshot_supplies_a_coherent_current_status_pair(self):
        original_current = self.states["sensor.car"].last_reported
        original_status = self.states["sensor.status"].last_reported
        self.adapter.async_read_current_snapshot.side_effect = lambda: self.snapshot(0.01, "ready_to_charge")
        await self.fetch()
        inp = self.controller._read_input()
        self.assertEqual((inp.charger_current, inp.status), (0.01, "ready_to_charge"))
        self.assertTrue(inp.charger_snapshot_verified)
        self.assertGreater(inp.charger_report_ts, original_current.timestamp())
        self.assertEqual(self.states["sensor.car"].last_reported, original_current)
        self.assertEqual(self.states["sensor.status"].last_reported, original_status)
        self.assertEqual(self.controller.source_diagnostics["charger_current_entity_reported_at_utc"], original_current.isoformat())
        self.assertEqual(self.controller.source_diagnostics["charger_current_source"], "selected_charger_rest_snapshot")

    async def test_new_current_event_invalidates_entire_cached_pair(self):
        await self.fetch()
        self.states["sensor.car"].state = "9"
        self.states["sensor.car"].last_reported = datetime.now(timezone.utc)
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_current, "9")
        self.assertIsNone(self.controller._charger_snapshot)

    async def test_even_unchanged_value_new_report_invalidates_cached_pair(self):
        await self.fetch()
        self.states["sensor.car"].last_reported = datetime.now(timezone.utc)
        self.assertFalse(self.controller._read_input().charger_snapshot_verified)

    async def test_new_schedule_status_wins_over_older_snapshot(self):
        await self.fetch()
        self.states["sensor.status"].state = "awaiting_scheduled_start"
        self.states["sensor.status"].last_reported = datetime.now(timezone.utc)
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertEqual(inp.status, "awaiting_scheduled_start")

    async def test_old_and_future_receipts_are_never_used(self):
        for seconds in (-31, 1):
            with self.subTest(seconds=seconds):
                self.install(self.snapshot(received=datetime.now(timezone.utc) + timedelta(seconds=seconds)))
                self.assertFalse(self.controller._read_input().charger_snapshot_verified)

    async def test_snapshot_must_be_later_than_original_entity_reports(self):
        self.states["sensor.car"].last_reported = datetime.now(timezone.utc) + timedelta(seconds=1)
        await self.fetch()
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertEqual(self.controller._charger_snapshot_error, "snapshot_not_newer_than_entity_reports")

    async def test_cached_pair_is_invalid_after_easee_runtime_replacement(self):
        await self.fetch()
        self.adapter.snapshot_is_current.return_value = False
        self.assertFalse(self.controller._read_input().charger_snapshot_verified)

    async def test_source_change_during_get_discards_later_arriving_old_response(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read():
            entered.set()
            await release.wait()
            return self.snapshot(16, "charging")
        self.adapter.async_read_current_snapshot.side_effect = read
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        self.states["sensor.status"].state = "paused"
        self.states["sensor.status"].last_reported = datetime.now(timezone.utc)
        release.set()
        await pending
        self.assertIsNone(self.controller._charger_snapshot)
        self.controller._handle_source_change.assert_not_called()

    async def test_reconfiguration_during_get_discards_response(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read():
            entered.set()
            await release.wait()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = read
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        self.controller._generation += 1
        release.set()
        await pending
        self.assertIsNone(self.controller._charger_snapshot)

    async def test_health_loss_invalidates_cached_pair_and_prevents_request(self):
        await self.fetch()
        self.adapter.telemetry_block_reason = "easee_stream_disconnected"
        self.assertFalse(self.controller._read_input().charger_snapshot_verified)
        self.controller._charger_snapshot_next_request = 0
        self.controller._request_current_snapshot()
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 1)

    async def test_failure_is_sanitized_and_never_renews_a_snapshot(self):
        self.adapter.async_read_current_snapshot.side_effect = RuntimeError("private-token-and-url")
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING") as logs:
            await self.fetch()
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertEqual(self.controller._charger_snapshot_error, "RuntimeError")
        self.assertIsNone(self.controller._charger_snapshot_error_reason)
        self.assertNotIn("private-token-and-url", str(logs.output))

    async def test_only_fixed_snapshot_error_code_is_exposed_then_cleared_on_success(self):
        self.adapter.async_read_current_snapshot.side_effect = RuntimeError(
            "easee_snapshot_invalid_current:private-token-and-url")
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING") as logs:
            await self.fetch()
        self.controller._read_input()
        self.assertEqual(self.controller.source_diagnostics["charger_snapshot_error_reason"],
                         "easee_snapshot_invalid_current")
        self.assertNotIn("private-token-and-url", str(logs.output))
        self.assertNotIn("private-token-and-url", str(self.controller.source_diagnostics))
        self.controller._charger_snapshot_next_request = 0
        self.adapter.async_read_current_snapshot.side_effect = self.snapshot
        await self.fetch()
        self.assertIsNone(self.controller._charger_snapshot_error_reason)

    async def test_requests_are_throttled_and_single_flight(self):
        await self.fetch()
        self.controller._request_current_snapshot()
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 1)
        self.controller._charger_snapshot_next_request = 0
        await self.fetch()
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 2)

    async def test_safe_underlying_failure_types_are_visible_and_clear_after_success(self):
        for failure in ("KeyError", "ForbiddenServiceException", "TimeoutError"):
            with self.subTest(failure=failure):
                self.controller._charger_snapshot_next_request = 0
                self.adapter.async_read_current_snapshot.side_effect = RuntimeError(
                    "easee_snapshot_failed:" + failure)
                with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING") as logs:
                    await self.fetch()
                self.controller._read_input()
                self.assertEqual(self.controller.source_diagnostics["charger_snapshot_failure_type"], failure)
                self.assertIn(failure, str(logs.output))
        self.controller._charger_snapshot_next_request = 0
        self.adapter.async_read_current_snapshot.side_effect = self.snapshot
        await self.fetch()
        self.assertIsNone(self.controller._charger_snapshot_failure_type)

    async def test_unrecognized_or_extended_failure_types_never_expose_private_text(self):
        for text in ("easee_snapshot_failed:private-token-and-url",
                     "easee_snapshot_failed:KeyError:private-token-and-url",
                     "private-token-and-url:KeyError"):
            with self.subTest(text=text):
                self.controller._charger_snapshot_next_request = 0
                self.adapter.async_read_current_snapshot.side_effect = RuntimeError(text)
                with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING") as logs:
                    await self.fetch()
                self.controller._read_input()
                self.assertIsNone(self.controller._charger_snapshot_failure_type)
                self.assertNotIn("private-token-and-url", str(logs.output))
                self.assertNotIn("private-token-and-url", str(self.controller.source_diagnostics))

    async def test_slow_get_does_not_block_immediate_protective_tick(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read():
            entered.set()
            await release.wait()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = read
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        now = datetime.now(timezone.utc)
        self.states["sensor.grid"].state = "42"
        self.states["sensor.car"].state = "16"
        for state in self.states.values():
            state.last_reported = now
        self.controller.regulator.last_target = (16, 0, 0, 60)
        await asyncio.wait_for(self.controller.async_tick(), .5)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertFalse(pending.done())
        release.set()
        await pending

    async def test_stop_cancels_pending_get_without_late_publication(self):
        entered = asyncio.Event()
        async def read():
            entered.set()
            await asyncio.Event().wait()
        self.adapter.async_read_current_snapshot.side_effect = read
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        await self.controller.async_stop()
        self.assertTrue(pending.cancelled())
        self.assertIsNone(self.controller._charger_snapshot)
        self.controller._handle_source_change.assert_not_called()

    async def test_disable_during_checkpoint_write_cannot_execute_old_command(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = self.controller._budget_store.async_save
        async def save(state):
            entered.set()
            await release.wait()
            await original_save(state)
        self.controller._budget_store.async_save = save
        self.controller.regulator._overload_used_seconds = 1
        tick = asyncio.create_task(self.controller.async_tick())
        await entered.wait()
        disable = asyncio.create_task(self.controller.async_set_switch("active_control", False))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(tick, disable)
        self.assertFalse(self.controller.settings.active_control)
        self.assertEqual(self.adapter.calls, [])

    async def test_reconfigure_during_checkpoint_write_cannot_execute_old_command(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = self.controller._budget_store.async_save
        async def save(state):
            entered.set()
            await release.wait()
            await original_save(state)
        self.controller._budget_store.async_save = save
        self.controller.regulator._overload_used_seconds = 1
        tick = asyncio.create_task(self.controller.async_tick())
        await entered.wait()
        reconfigure = asyncio.create_task(self.controller.async_reconfigure())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(tick, reconfigure)
        self.assertEqual(self.adapter.calls, [])

    async def test_new_grid_during_checkpoint_write_cannot_execute_old_increase(self):
        now = datetime.now(timezone.utc)
        for state in self.states.values():
            state.last_reported = now
        self.states["sensor.grid"].state = "12"
        self.controller.regulator._overload_used_seconds = 1
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = self.controller._budget_store.async_save
        async def save(state):
            entered.set()
            await release.wait()
            await original_save(state)
        self.controller._budget_store.async_save = save
        tick = asyncio.create_task(self.controller.async_tick())
        await entered.wait()
        self.states["sensor.grid"].state = "38"
        self.states["sensor.grid"].last_reported = datetime.now(timezone.utc)
        release.set()
        await tick
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.controller._handle_source_change.assert_called()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
