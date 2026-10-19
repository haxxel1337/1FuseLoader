"""Virtual-clock snapshot deadlines and lifecycle races; no network or HA."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_runtime as fixtures


class RefreshDeadlineTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.now = 0.0
        self.epoch = 1_700_000_000.0
        self.hass.loop = SimpleNamespace(time=lambda: self.now)
        self.hass.data = {}
        self.entry.options.update(measurement_settle_seconds=25,
                                  enable_soft_guard=False, enable_pause_resume=False)
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._handle_source_change = Mock()
        self.adapter.async_read_current_snapshot = AsyncMock(side_effect=self.snapshot)
        self.adapter.snapshot_is_current = Mock(return_value=True)
        owner = self
        real_datetime = datetime

        class ClockMeta(type):
            def __instancecheck__(cls, value):
                return isinstance(value, real_datetime)

        class ClockDateTime(real_datetime, metaclass=ClockMeta):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.fromtimestamp(owner.epoch+owner.now, tz or timezone.utc)

        self.clock_patch = patch.object(fixtures.runtime, "datetime", ClockDateTime)
        self.clock_patch.start()
        self.timers = []
        self.timer_patch = patch.object(fixtures.runtime, "async_call_later", self.schedule)
        self.timer_patch.start()
        for entity_id, state in self.states.items():
            state.last_reported = self.wall()
            if entity_id == "sensor.car":
                state.state = "16.227"
            if entity_id == "sensor.grid":
                state.state = "21.7"

    async def asyncTearDown(self):
        await self.controller.async_stop()
        self.timer_patch.stop()
        self.clock_patch.stop()

    def wall(self, then=None):
        return datetime.fromtimestamp(self.epoch+(self.now if then is None else then), timezone.utc)

    def snapshot(self, current=16.227, status="charging"):
        return SimpleNamespace(current=current, status=status, received_at=self.wall())

    def schedule(self, hass, delay, callback):
        item = SimpleNamespace(due=self.now+delay, callback=callback, cancelled=False)
        self.timers.append(item)
        def cancel():
            item.cancelled = True
        return cancel

    def active_timers(self):
        return [item for item in self.timers if not item.cancelled]

    async def fire(self, item=None, *, advance=True):
        item = item or min(self.active_timers(), key=lambda candidate: candidate.due)
        if advance:
            self.now = max(self.now, item.due)
        item.cancelled = True
        self.states["sensor.grid"].last_reported = self.wall()
        await item.callback(self.wall())
        await asyncio.sleep(0)
        task = self.controller._charger_snapshot_task
        if task is not None:
            await task
        return item

    async def fetch(self):
        self.controller._request_current_snapshot()
        task = self.controller._charger_snapshot_task
        self.assertIsNotNone(task)
        await task

    def maintain(self):
        inp = self.controller._read_input()
        self.controller._schedule_current_snapshot(inp)
        return inp

    def seed_latched_cap(self):
        r = self.controller.regulator
        r.import_overload_state(dict(version=1, used_seconds=2000.0, exposure_active=False,
                                    transition_pending=False, minimum_held=False,
                                    failure_reason="response_timeout"))
        r.last_target = (16, 0, 0, 60)
        r.last_set_limit_ts = r.last_action_ts = r._last_limit_change_ts = -120

    async def test_prefetch_fires_at_half_window_without_watchdog_poll(self):
        self.maintain()
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 12.5)
        self.adapter.async_read_current_snapshot.assert_not_awaited()
        await self.fire()
        self.adapter.async_read_current_snapshot.assert_awaited_once()
        self.assertEqual(self.controller._read_input().charger_report_ts, self.epoch+12.5)
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 25)
        self.assertEqual(self.adapter.calls, [])

    async def test_repeated_prefetch_prevents_old_receipt_gap_and_allows_nominal_reduction(self):
        self.controller.settings.overload_enabled = True
        self.seed_latched_cap()
        self.maintain()
        await self.fire()
        await self.fire()
        self.now = 29.244660
        self.states["sensor.grid"].state = "25.1"
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        d = self.controller.last_decision
        self.assertEqual((d.mode, d.current_p1), ("normal", 14))
        self.assertIsNone(d.nominal_rejection_code)
        self.assertAlmostEqual(d.source_report_skew_seconds, 4.244660, places=5)
        state = self.controller.regulator.export_overload_state()
        self.assertEqual((state["used_seconds"], state["failure_reason"]), (2000, "response_timeout"))
        self.assertEqual(self.adapter.calls[-1].current_p1, 14)

    async def test_two_second_get_latency_is_charged_before_new_receipt_and_next_deadline(self):
        async def delayed():
            self.now += 2
            self.states["sensor.grid"].last_reported = self.wall()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = delayed
        self.maintain()
        await self.fire()
        self.assertEqual(self.now, 14.5)
        self.assertEqual(self.controller._read_input().charger_report_ts, self.epoch+14.5)
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 27)
        await self.fire()
        self.assertEqual(self.now, 29)
        self.assertEqual(self.controller._read_input().charger_report_ts, self.epoch+29)
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 41.5)

    async def test_slow_or_missing_refresh_still_stops_and_preserves_first_rejection(self):
        self.controller.settings.overload_enabled = True
        self.seed_latched_cap()
        entered = asyncio.Event()
        async def slow():
            entered.set()
            await asyncio.Event().wait()
        self.adapter.async_read_current_snapshot.side_effect = slow
        self.now = 29.244660
        self.states["sensor.grid"].state = "25.1"
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        details = self.controller.diagnostic_attributes()["last_stop_nominal_diagnostics"]
        self.assertEqual(details["nominal_rejection_code"], "report_skew_exceeds_alignment_window")
        self.assertAlmostEqual(details["source_report_skew_seconds"], 29.244660, places=5)
        self.assertEqual(details["source_alignment_window_seconds"], 25)
        self.assertEqual(details["source_max_age_seconds"], 120)
        details["nominal_rejection_code"] = "external mutation"
        self.assertNotEqual(self.controller.last_stop_nominal_diagnostics["nominal_rejection_code"], "external mutation")
        self.assertEqual(self.controller.diagnostic_attributes()["overload_failure_reason"], "response_timeout")
        self.assertIsNone(self.controller._charger_snapshot)

    async def test_native_numeric_reports_postpone_prefetch_but_product_pulse_does_not(self):
        self.maintain()
        old = self.active_timers()[0]
        self.now = 10
        self.states["sensor.car"].last_reported = self.wall()
        self.maintain()
        self.assertTrue(old.cancelled)
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 22.5)
        for then in (11, 12, 15, 20):
            self.now = then
            self.states["sensor.grid"].last_reported = self.wall()
            self.maintain()
            self.assertEqual(self.controller._charger_snapshot_refresh_due, 22.5)
        self.adapter.async_read_current_snapshot.assert_not_awaited()
        await self.fire()
        self.adapter.async_read_current_snapshot.assert_awaited_once()

    async def test_short_window_keeps_five_second_request_floor_and_does_not_fake_freshness(self):
        self.controller.settings.measurement_settle_seconds = 1
        self.maintain()
        self.assertEqual(self.controller._charger_snapshot_refresh_due, .5)
        await self.fire()
        self.assertEqual(self.controller._charger_snapshot_next_request, 5.5)
        self.now = 2
        self.states["sensor.grid"].last_reported = self.wall()
        self.maintain()
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 5.5)
        self.controller._request_current_snapshot()
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 1)
        inp = self.controller._read_input()
        r = self.controller.regulator
        r._last_input_wall_now_ts = inp.wall_now_ts
        r._last_input_grid_ts = inp.grid_report_ts
        r._last_input_charger_ts = inp.charger_report_ts
        self.assertFalse(r._fast_sources_fresh())

    async def test_max_age_and_alignment_both_determine_deadline(self):
        for alignment, max_age, expected in ((30, 120, 15), (10, 120, 5), (30, 8, 4)):
            with self.subTest(alignment=alignment, max_age=max_age):
                self.controller.settings.measurement_settle_seconds = alignment
                self.controller.settings.max_sensor_age_seconds = max_age
                self.maintain()
                self.assertEqual(self.controller._charger_snapshot_refresh_due, expected)

    async def test_stale_cancelled_timer_token_cannot_issue_or_schedule_get(self):
        self.maintain()
        old = self.active_timers()[0]
        self.now = 10
        self.states["sensor.car"].last_reported = self.wall()
        self.maintain()
        await old.callback(self.wall())
        self.adapter.async_read_current_snapshot.assert_not_awaited()
        self.assertEqual(self.controller._charger_snapshot_refresh_due, 22.5)

    async def test_health_loss_cancels_timer_and_get_without_publishing(self):
        self.maintain()
        old = self.active_timers()[0]
        self.adapter.telemetry_block_reason = "easee_product_update_stale"
        self.maintain()
        self.assertTrue(old.cancelled)
        await old.callback(self.wall())
        self.adapter.async_read_current_snapshot.assert_not_awaited()
        self.assertIsNone(self.controller._charger_snapshot_refresh_due)

    async def test_prefetch_is_not_started_for_idle_disabled_or_dry_run(self):
        for mode in ("idle", "disabled", "dry"):
            with self.subTest(mode=mode):
                self.controller.settings.active_control = mode != "disabled"
                self.controller.settings.dry_run = mode == "dry"
                self.states["sensor.status"].state = "awaiting_start" if mode == "idle" else "charging"
                self.maintain()
                self.assertIsNone(self.controller._charger_snapshot_refresh_due)
        self.adapter.async_read_current_snapshot.assert_not_awaited()

    async def test_failure_backoff_is_exponential_and_shared_with_demand_requests(self):
        self.adapter.async_read_current_snapshot.side_effect = RuntimeError("private response")
        for delay in (30, 60, 120, 240, 300):
            start = self.now
            with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING") as logs:
                await self.fetch()
            self.assertNotIn("private response", str(logs.output))
            self.assertEqual(self.controller._charger_snapshot_next_request, start+delay)
            count = self.adapter.async_read_current_snapshot.await_count
            self.now = start+delay-.01
            self.controller._request_current_snapshot()
            self.assertEqual(self.adapter.async_read_current_snapshot.await_count, count)
            self.now = start+delay
            self.states["sensor.grid"].last_reported = self.wall()
        self.assertIsNone(self.controller._charger_snapshot)

    async def test_reconfigure_preserves_rate_limit_and_cleans_old_timer(self):
        self.now = 1
        await self.fetch()
        old = self.active_timers()[0]
        quota = self.controller._charger_snapshot_next_request
        self.entry.options["measurement_settle_seconds"] = 10
        original_tick = self.controller._async_tick_locked
        self.controller._async_tick_locked = AsyncMock()
        await self.controller.async_reconfigure()
        self.controller._async_tick_locked = original_tick
        self.assertTrue(old.cancelled)
        self.assertEqual(self.controller._charger_snapshot_next_request, quota)
        self.maintain()
        self.assertEqual(self.controller._charger_snapshot_refresh_due, quota)
        await old.callback(self.wall())
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 1)

    async def test_same_ha_entry_reload_does_not_refund_request_floor_or_backoff(self):
        self.adapter.async_read_current_snapshot.side_effect = RuntimeError("failed")
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.fetch()
        replacement = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.assertEqual(replacement._charger_snapshot_next_request, 30)
        self.assertEqual(replacement._charger_snapshot_failures, 1)
        replacement._request_current_snapshot()
        self.assertIsNone(replacement._charger_snapshot_task)

    async def test_inflight_prefetch_and_demand_share_one_request_and_do_not_lock_control(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = slow
        self.controller._request_current_snapshot()
        await entered.wait()
        task = self.controller._charger_snapshot_task
        for _ in range(10):
            self.maintain()
            self.controller._request_current_snapshot()
        self.assertIs(self.controller._charger_snapshot_task, task)
        self.assertFalse(self.controller._lock.locked())
        self.assertEqual(self.adapter.async_read_current_snapshot.await_count, 1)
        release.set()
        await task

    async def test_stop_cancels_current_timer_and_pending_get(self):
        self.maintain()
        old = self.active_timers()[0]
        entered = asyncio.Event()
        async def slow():
            entered.set()
            await asyncio.Event().wait()
        self.adapter.async_read_current_snapshot.side_effect = slow
        self.controller._request_current_snapshot()
        task = self.controller._charger_snapshot_task
        await entered.wait()
        await self.controller.async_stop()
        await old.callback(self.wall())
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertIsNone(self.controller._charger_snapshot_refresh_due)
        self.controller._handle_source_change.assert_not_called()

    async def test_new_native_schedule_during_get_discards_whole_old_pair(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = slow
        self.controller._request_current_snapshot()
        task = self.controller._charger_snapshot_task
        await entered.wait()
        self.now = 1
        self.states["sensor.status"].state = "awaiting_scheduled_start"
        self.states["sensor.status"].last_reported = self.wall()
        release.set()
        await task
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertEqual(self.controller._read_input().status, "awaiting_scheduled_start")
        self.controller._handle_source_change.assert_not_called()

    async def test_outer_get_timeout_is_bounded_and_does_not_renew_numeric_evidence(self):
        async def blocked():
            await asyncio.Event().wait()
        self.adapter.async_read_current_snapshot.side_effect = blocked
        with patch.object(fixtures.runtime, "CURRENT_SNAPSHOT_REQUEST_TIMEOUT_SECONDS", .01):
            with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
                await self.fetch()
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertEqual(self.controller._charger_snapshot_error, "TimeoutError")
        self.assertEqual(self.controller._charger_snapshot_next_request, 30)
        self.assertEqual(self.controller._read_input().charger_report_ts, self.epoch)

    async def test_health_loss_during_get_never_publishes_response_or_refresh_loop(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked():
            entered.set()
            await release.wait()
            return self.snapshot()
        self.adapter.async_read_current_snapshot.side_effect = blocked
        self.now = 1
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        self.adapter.telemetry_block_reason = "easee_product_update_stale"
        release.set()
        await pending
        self.assertIsNone(self.controller._charger_snapshot)
        self.assertIsNone(self.controller._charger_snapshot_refresh_due)
        self.controller._handle_source_change.assert_not_called()

    async def test_obsolete_failed_request_cannot_clear_new_generation_evidence_or_errors(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def old_request():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise RuntimeError("obsolete request detail")
        self.adapter.async_read_current_snapshot.side_effect = old_request
        self.now = 1
        self.controller._request_current_snapshot()
        pending = self.controller._charger_snapshot_task
        await entered.wait()
        self.controller._generation += 1
        self.controller._cancel_snapshot_refresh(cancel_request=True)
        await cancelled.wait()
        sentinel = self.snapshot(6, "awaiting_start")
        self.controller._charger_snapshot = sentinel
        self.controller._charger_snapshot_error = "new_generation_evidence"
        release.set()
        await pending
        self.assertIs(self.controller._charger_snapshot, sentinel)
        self.assertEqual(self.controller._charger_snapshot_error, "new_generation_evidence")
        self.assertIsNone(self.controller._charger_snapshot_task)
        self.assertIsNone(self.controller._charger_snapshot_refresh_due)
        self.controller._handle_source_change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
