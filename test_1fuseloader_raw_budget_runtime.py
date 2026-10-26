"""Offline controller contracts for source-bound raw-grid recovery authority."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_clock_provenance_runtime as clocks
from test_1fuseloader_ha_current_representation import float32, ha_state


class RawBudgetRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    asyncSetUp = clocks.ClockProvenanceRuntimeTests.asyncSetUp
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    install = clocks.ClockProvenanceRuntimeTests.install

    def committed(self, value, *, received, sequence=1):
        self.set_native(value, received=received, changed_at=received-1,
            sequences=((182, sequence), (183, 0), (184, 0), (185, 0)))
        self.native.committed = True
        self.native.sdk_current = value
        self.native.native_publication_pending = False
        self.native.publication_generation = sequence
        self.states["sensor.car"].state = ha_state(value)

    async def test_committed_authority_requires_selected_native_tuple(self):
        self.committed(6., received=98)
        inp = self.controller._read_input()
        self.assertTrue(inp.charger_native_committed)
        self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())
        for flag in (False, None, 0, 1, "true"):
            with self.subTest(flag=flag):
                self.native.committed = flag
                self.assertFalse(self.controller._read_input().charger_native_committed)
        self.native.committed = True
        self.native.source_identity = (99, 98, 97, 96)
        self.assertFalse(self.controller._read_input().charger_native_committed)

    async def test_matching_get_never_supplies_atomic_authority_or_fresh_clock(self):
        self.committed(6., received=98)
        first = self.controller._read_input()
        self.install(self.snapshot(current=6., observed=99))
        later = self.controller._read_input()
        self.assertTrue(later.charger_native_committed)
        self.assertFalse(later.charger_snapshot_verified)
        self.assertEqual(later.charger_report_ts, first.charger_report_ts)
        self.assertEqual(later.charger_observation_sequences, first.charger_observation_sequences)
        self.set_native(6., received=None)
        self.install(self.snapshot(current=6., observed=99))
        fallback = self.controller._read_input()
        self.assertTrue(fallback.charger_snapshot_verified)
        self.assertFalse(fallback.charger_native_committed)

    async def test_authority_change_invalidates_prepared_dispatch_revision(self):
        self.committed(6., received=98)
        before = self.controller._read_input()
        self.assertNotEqual(self.controller._input_revision(before),
            self.controller._input_revision(replace(before, charger_native_committed=False)))

    async def test_invalid_tuple_clock_is_not_a_recent_publication_capability(self):
        for stamp in (self.now+10, -1., float("inf"), float("nan"), True, "99"):
            with self.subTest(stamp=stamp):
                self.committed(6., received=98)
                self.native.last_tuple_change_monotonic = stamp
                inp = self.controller._read_input()
                self.assertFalse(inp.data_valid)
                self.assertFalse(inp.charger_current_trusted)
                self.assertFalse(inp.charger_native_committed)
                self.assertFalse(inp.charger_increase_ready)

    async def test_pending_tuple_retains_old_number_without_new_raw_rise_authority(self):
        self.committed(6., received=98)
        self.native.native_publication_pending = True
        self.native.sdk_current = 12.
        self.native.divergence_started_monotonic = self.now
        self.states["sensor.car"].state = ha_state(12.)
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_native_committed)
        self.assertTrue(inp.charger_publication_pending)
        self.assertEqual(inp.charger_current, 6.)
        self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())

    async def prepare_raw_rise(self):
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            enable_soft_guard=False, enable_pause_resume=False))
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "charging"
        self.states["sensor.grid"].state = "17"
        self.committed(float32(5.985), received=self.now)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 6)
        self.now += 1
        self.committed(float32(5.985), received=self.now, sequence=2)
        self.now += .3
        self.states["sensor.grid"].state = "22.9"
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        # Keep the real accepted lease alive without inventing further EV
        # changes: native telemetry is allowed to be event-driven.
        for at in (120., 150., 180., 210., 240., 270.):
            self.now = at
            self.states["sensor.grid"].last_reported = self.wall()
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 6)
        self.now = 300.
        self.states["sensor.grid"].state = "10.5"
        self.states["sensor.grid"].last_reported = self.wall()

    async def test_full_controller_raw_rise_grid_first_response_then_fresh_fast_rise(self):
        await self.prepare_raw_rise()
        old_ev_clock = self.controller._read_input().charger_report_ts
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        self.assertEqual(self.controller._read_input().charger_report_ts, old_ev_clock)
        deadline = self.controller.last_decision.raw_budget_response_deadline_ts
        self.assertEqual(deadline, self.controller.regulator._positive_raw_deadline)
        self.assertTrue(any(abs(timer.due-deadline) < 1e-6 for timer in self.active_timers()))
        self.now += 1
        self.states["sensor.grid"].state = "16.5"
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 12)
        self.now += 1
        self.committed(12., received=self.now, sequence=3)
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 12)
        self.assertIsNone(self.controller.last_decision.command)
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))

    async def test_restart_bootstrap_keeps_native_zero_sequence_through_matching_get(self):
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            enable_soft_guard=False, enable_pause_resume=False))
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "charging"
        self.states["sensor.grid"].state = "17"
        self.set_native(6., received=None, changed_at=90,
            sequences=((182, 0), (183, 0), (184, 0), (185, 0)))
        self.native.committed = True
        self.native.sdk_current = 6.
        self.install(self.snapshot(current=6., observed=90))
        boot = self.controller._read_input()
        self.assertTrue(boot.charger_native_committed)
        self.assertFalse(boot.charger_snapshot_verified)
        self.assertIsNone(boot.charger_report_ts)
        self.assertEqual(boot.charger_report_family, "native_terminal_receipt")
        self.assertEqual(dict(boot.charger_observation_sequences)[182], 0)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 6)
        self.now += 6
        self.states["sensor.grid"].state = "10.5"
        self.states["sensor.grid"].last_reported = self.wall()
        self.install(self.snapshot(current=6., observed=90))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        r = self.controller.regulator
        deadline = r._positive_raw_deadline
        self.now += 1
        self.install(self.snapshot(current=6., observed=90))
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(r._positive_cap, 12)
        self.assertEqual(r._positive_raw_deadline, deadline)
        self.assertIsNone(self.controller._read_input().charger_report_ts)
        self.now += 1
        self.states["sensor.grid"].state = "16.5"
        self.states["sensor.grid"].last_reported = self.wall()
        self.committed(12., received=self.now, sequence=1)
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 12)
        self.assertIsNone(self.controller.last_decision.command)
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))

    async def dispatch_change(self, change):
        self.assertTrue(self.controller.regulator.import_overload_state(dict(version=1,
            used_seconds=1., exposure_active=False, transition_pending=False,
            minimum_held=False, failure_reason=None)))
        await self.prepare_raw_rise()
        self.adapter.calls.clear()
        self.controller._handle_source_change = Mock()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller._budget_store.async_save

        async def save(state):
            entered.set()
            await release.wait()
            return await original(state)

        self.controller._budget_store.async_save = save
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.adapter.calls, [])
            change()
            release.set()
            await tick
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertFalse(self.controller.last_decision.should_call_service)

    async def test_loss_of_committed_authority_during_checkpoint_discards_raw_rise(self):
        await self.dispatch_change(lambda: setattr(self.native, "committed", False))

    async def test_hard_grid_during_checkpoint_never_dispatches_old_raw_rise(self):
        def peak():
            self.states["sensor.grid"].state = "40"
            self.states["sensor.grid"].last_reported = self.wall()

        await self.dispatch_change(peak)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])

    async def test_expired_unsent_raw_proposal_does_not_stop_existing_live_cap(self):
        await self.dispatch_change(lambda: setattr(self, "now", 326.))
        self.assertEqual(self.controller.regulator.last_target[0], 6)
        self.assertFalse(self.controller.regulator._restart_required)

    async def test_raw_reply_after_original_deadline_is_recorded_without_a_new_owner(self):
        await self.prepare_raw_rise()
        self.adapter.calls.clear()
        self.controller._handle_source_change = Mock()
        original = self.adapter.async_execute

        async def late_reply(decision):
            result = await original(decision)
            self.now += 26
            return result

        self.adapter.async_execute = late_reply
        await self.controller.async_tick()
        r = self.controller.regulator
        self.assertEqual(self.adapter.calls[-1].current_p1, 12)
        self.assertEqual(r.last_target[0], 12)
        self.assertTrue(r._restart_required)
        self.assertIsNone(r._positive_cap)
        self.controller._handle_source_change.assert_called()

    async def test_raw_api_wait_spends_only_original_sample_remaining_time(self):
        await self.prepare_raw_rise()
        self.states["sensor.grid"].last_reported = self.wall(276.)
        self.adapter.calls.clear()
        actual_wait_for = asyncio.wait_for
        dispatch_timeouts = []

        async def slow_execute(decision):
            self.adapter.calls.append(decision)
            await asyncio.sleep(10)

        async def inspect_wait(awaitable, timeout):
            if awaitable is self.controller._dispatch_task:
                dispatch_timeouts.append(timeout)
                # Exercise cancellation without sleeping the proposed seconds.
                return await actual_wait_for(awaitable, .001)
            return await actual_wait_for(awaitable, timeout)

        self.adapter.async_execute = slow_execute
        with patch.object(asyncio, "wait_for", inspect_wait):
            await self.controller.async_tick()
        self.assertEqual(len(dispatch_timeouts), 1)
        self.assertGreater(dispatch_timeouts[0], 0)
        self.assertLessEqual(dispatch_timeouts[0], 1.)
        self.assertEqual(self.controller.regulator.last_target[0], 6)
        self.assertTrue(self.controller._protective_zero_pending)


if __name__ == "__main__":
    unittest.main()
