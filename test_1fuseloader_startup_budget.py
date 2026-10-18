"""Initial ledger load latency without HA, network calls or real storage.

Shortened deadlines preserve the production 10:2 load/write relationship.
The delayed-load regression crosses the old write deadline before completing;
only the controller's initial-load deadline may allow it to finish.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_runtime as fixtures


class StartupBudgetTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry
    fresh_control_reports = fixtures.ControllerTests.fresh_control_reports

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.entry.options["overload_enabled"] = True
        self.fresh_control_reports()
        self.controller = fixtures.runtime.OneFuseLoaderController(
            self.hass, self.entry, self.adapter)
        self.controller._budget_store.clock = lambda: 1030.0
        self.controller._budget_store.store.data = self.envelope()
        self.initial_timeout = patch.object(
            fixtures.runtime, "INITIAL_EXPOSURE_LOAD_TIMEOUT_SECONDS", 0.25)
        self.write_timeout = patch.object(
            fixtures.runtime, "EXPOSURE_STORE_TIMEOUT_SECONDS", 0.05)
        self.initial_timeout.start()
        self.write_timeout.start()
        self.addCleanup(self.initial_timeout.stop)
        self.addCleanup(self.write_timeout.stop)

    async def asyncTearDown(self):
        await self.controller.async_stop()

    @staticmethod
    def envelope(**state_changes):
        return {
            "version": 1, "saved_at": 1000.0,
            "regulator": {
                "version": 1, "used_seconds": 120.0,
                "exposure_active": False, "transition_pending": False,
                "minimum_held": False, "failure_reason": None,
                **state_changes,
            },
        }

    def blocked_read(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller._budget_store._read_back

        async def read():
            entered.set()
            await release.wait()
            return await original()

        reader = AsyncMock(side_effect=read)
        self.controller._budget_store._read_back = reader
        return entered, release, reader

    def test_production_deadlines_keep_initial_load_separate_from_writes(self):
        self.initial_timeout.stop()
        self.write_timeout.stop()
        self.assertEqual(fixtures.runtime.INITIAL_EXPOSURE_LOAD_TIMEOUT_SECONDS, 10)
        self.assertEqual(fixtures.runtime.EXPOSURE_STORE_TIMEOUT_SECONDS, 2)

    async def test_initial_load_can_cross_write_deadline_without_fallback_or_early_command(self):
        original = deepcopy(self.controller._budget_store.store.data)
        entered, release, reader = self.blocked_read()
        starting = asyncio.create_task(self.controller.async_start())
        try:
            await entered.wait()
            # A real event-loop delay beyond the old 2-second-equivalent limit.
            await asyncio.sleep(fixtures.runtime.EXPOSURE_STORE_TIMEOUT_SECONDS * 1.5)
            self.assertFalse(starting.done())
            self.assertFalse(self.controller._budget_loaded)
            self.assertEqual(self.adapter.calls, [])
            release.set()
            await starting
            self.assertTrue(self.controller._budget_loaded)
            self.assertIsNone(self.controller._budget_error)
            self.assertEqual(self.controller.regulator.export_overload_state()["used_seconds"], 120)
            self.assertTrue(self.controller._budget_store.state_is_durable(
                self.controller.regulator.export_overload_state()))
            self.assertEqual(self.controller._budget_store.store.data, original)
            reader.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()

    async def test_initial_deadline_still_fails_closed_and_late_read_cannot_retry_or_write(self):
        original = deepcopy(self.controller._budget_store.store.data)
        entered, release, reader = self.blocked_read()
        starting = asyncio.create_task(self.controller.async_start())
        try:
            await entered.wait()
            await starting  # The scaled initial deadline, not the write one.
            self.assertTrue(self.controller._budget_loaded)
            self.assertEqual(self.controller._budget_error, "overload_budget_restore_failed")
            self.assertEqual(self.controller._budget_error_type, "TimeoutError")
            self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))
            self.assertGreaterEqual(
                self.controller.regulator.export_overload_state()["used_seconds"], 1800)
            pending = self.controller._budget_store._gate.pending
            self.assertIsNotNone(pending)
            self.assertFalse(pending.cancelled())
            self.assertEqual(self.controller._budget_store.store.data, original)
            release.set()
            await self.controller._budget_store.async_wait_pending()
            await self.controller.async_tick()
            self.assertEqual(self.controller._budget_error, "overload_budget_restore_failed")
            self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))
            self.assertEqual(self.controller._budget_store.store.data, original)
            reader.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()

    async def test_runtime_write_uses_short_deadline_and_blocks_before_positive_submission(self):
        self.states["sensor.grid"].state = "28"
        self.states["sensor.car"].state = "6"
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller._budget_store.store.async_save

        async def write(payload):
            entered.set()
            await release.wait()
            await original(payload)

        self.controller._budget_store.store.async_save = write
        starting = asyncio.create_task(self.controller.async_start())
        release_later = None
        try:
            await entered.wait()
            # Finish before the longer initial deadline, but after the writer's
            # deadline. Accidentally lengthening writes would make this succeed.
            release_later = self.hass.loop.call_later(
                fixtures.runtime.EXPOSURE_STORE_TIMEOUT_SECONDS * 2, release.set)
            await starting
            self.assertTrue(self.controller._budget_loaded)
            self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")
            self.assertEqual(self.controller._budget_error_type, "TimeoutError")
            self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
            pending = self.controller._budget_store._gate.pending
            self.assertIsNotNone(pending)
            self.assertFalse(pending.cancelled())
        finally:
            if release_later is not None:
                release_later.cancel()
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()

    async def test_longer_initial_window_preserves_active_downtime_debit(self):
        self.controller._budget_store.store.data = self.envelope(exposure_active=True)
        entered, release, _ = self.blocked_read()
        starting = asyncio.create_task(self.controller.async_start())
        try:
            await entered.wait()
            await asyncio.sleep(fixtures.runtime.EXPOSURE_STORE_TIMEOUT_SECONDS * 1.5)
            release.set()
            await starting
            self.assertIsNone(self.controller._budget_error)
            self.assertEqual(self.controller.regulator.export_overload_state()["used_seconds"], 150)
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()

    async def test_pending_transition_after_long_read_keeps_failure_without_new_grace(self):
        self.states["sensor.grid"].state = "28"
        self.states["sensor.car"].state = "6"
        self.controller._budget_store.store.data = self.envelope(
            exposure_active=True, transition_pending=True)
        entered, release, _ = self.blocked_read()
        starting = asyncio.create_task(self.controller.async_start())
        try:
            await entered.wait()
            await asyncio.sleep(fixtures.runtime.EXPOSURE_STORE_TIMEOUT_SECONDS * 1.5)
            release.set()
            await starting
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await self.controller._budget_store.async_wait_pending()
        state = self.controller.regulator.export_overload_state()
        self.assertEqual(state["used_seconds"], 150)
        self.assertEqual(state["failure_reason"], "interrupted_transition")
        self.assertFalse(state["transition_pending"])
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_invalid_restore_remains_latched_without_overwriting_journal(self):
        original = self.envelope(version=999)
        self.controller._budget_store.store.data = deepcopy(original)
        await self.controller.async_start()
        self.assertEqual(self.controller._budget_error, "overload_budget_restore_failed")
        self.assertEqual(self.controller._budget_error_type, "ValueError")
        self.assertEqual(self.controller._budget_store.store.data, original)
        self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_unreadable_existing_or_corrupt_marked_ledger_cannot_be_new_budget(self):
        self.controller._budget_store.store.data = None
        self.controller._budget_store._ledger_exists = AsyncMock(return_value=True)
        await self.controller.async_start()
        self.assertEqual(self.controller._budget_error, "overload_budget_restore_failed")
        self.assertEqual(self.controller._budget_error_type, "CheckpointVerificationError")
        self.assertIsNone(self.controller._budget_store.store.data)
        self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_stop_during_load_cannot_install_listeners_or_submit_late_commands(self):
        entered, release, _ = self.blocked_read()
        self.adapter.async_initialize = AsyncMock()
        with patch.object(fixtures.runtime, "async_track_time_interval") as timer, \
                patch.object(fixtures.runtime, "async_track_state_change_event", Mock()) as states:
            starting = asyncio.create_task(self.controller.async_start())
            stopping = None
            try:
                await entered.wait()
                stopping = asyncio.create_task(self.controller.async_stop())
                await asyncio.sleep(0)
                self.assertTrue(self.controller._stopped)
                release.set()
                await asyncio.gather(starting, stopping)
                timer.assert_not_called()
                states.assert_not_called()
                self.adapter.async_initialize.assert_not_awaited()
                self.assertEqual(self.adapter.calls, [])
            finally:
                release.set()
                await asyncio.gather(starting, *([stopping] if stopping else []), return_exceptions=True)
                await self.controller._budget_store.async_wait_pending()


if __name__ == "__main__":
    unittest.main()
