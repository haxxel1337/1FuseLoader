"""Synthetic full-controller fencing while native raw publication is pending."""
from dataclasses import replace
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_1fuseloader_clock_provenance_runtime as clocks
import test_1fuseloader_normal_shedding_runtime as recovery
from test_1fuseloader_ha_current_representation import float32, ha_state


class CurrentPublicationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    asyncSetUp = clocks.ClockProvenanceRuntimeTests.asyncSetUp
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    install = clocks.ClockProvenanceRuntimeTests.install
    bootstrap_response = clocks.ClockProvenanceRuntimeTests.bootstrap_response

    def pending(self, committed, *, received, displayed, started=None, sequence=1):
        self.set_native(committed, received=received, changed_at=self.now-1,
            sequences=((182, sequence), (183, 0), (184, 0), (185, 0)))
        self.native.committed = True
        self.native.sdk_current = displayed
        self.native.native_publication_pending = True
        self.native.divergence_started_monotonic = self.now if started is None else started
        self.native.publication_generation = 1
        self.states["sensor.car"].state = ha_state(displayed)

    async def test_pending_uses_old_committed_number_and_actual_clock(self):
        self.pending(8., received=98, displayed=float32(9.965))
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_current, 8.)
        self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())
        self.assertTrue(inp.charger_publication_pending)
        self.assertTrue(inp.data_valid)
        self.assertEqual(dict(inp.charger_observation_sequences)[182], 1)
        self.assertFalse(self.controller._snapshot_prefetch_allowed(inp))

    async def test_get_cannot_replace_pending_atomic_tuple(self):
        self.pending(8., received=98, displayed=10.)
        self.install(self.snapshot(current=10., observed=99))
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_current, 8.)
        self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_report_family, "native_terminal_receipt")
        self.assertIsNone(self.controller._charger_vendor_evidence)

    async def test_positive_reduction_waits_without_call_or_retry_reservation(self):
        await self.bootstrap_response()
        self.adapter.calls.clear()
        self.now += 1
        self.states["sensor.grid"].state = "32"
        self.states["sensor.grid"].last_reported = self.wall()
        self.pending(16., received=self.now-1, displayed=7.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_current_publication")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.regulator.last_target[0], 16)
        self.assertFalse(self.controller.regulator._uncertainty_retry_submission_pending)

    async def test_first_bootstrap_response_publication_keeps_existing_response_owner(self):
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, enable_soft_guard=False, enable_pause_resume=False))
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "awaiting_start"
        self.states["sensor.grid"].state = "4"
        self.set_native(0., received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.now += 60
        self.set_native(0., received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.adapter.calls.clear()
        self.now += 1
        self.states["sensor.status"].state = "charging"
        self.states["sensor.status"].last_changed = self.wall()
        self.states["sensor.grid"].state = "20.1"
        self.pending(0., received=None, displayed=float32(16.059), sequence=0)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [], self.controller.last_decision)
        self.assertEqual(self.controller.regulator.last_target[0], 16)
        self.now += .02
        self.set_native(float32(16.059), received=self.now, changed_at=self.now,
            sequences=((182, 1), (183, 0), (184, 0), (185, 0)))
        self.native.committed = True
        self.native.native_publication_pending = False
        self.native.publication_generation = 1
        self.states["sensor.car"].state = ha_state(self.native.current)
        self.now += .3
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertIsNone(self.controller.regulator._positive_cap)
        # A single genuine value event must suffice for event-driven sources;
        # no second current change may be needed during eight steady minutes.
        for _ in range(96):
            self.now += 5
            self.states["sensor.grid"].last_reported = self.wall()
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertTrue(all(call.current_p1 == 16 for call in self.adapter.calls))

    async def test_identical_live_renewal_waits_without_spending_lease(self):
        await self.bootstrap_response()
        self.adapter.calls.clear()
        accepted = self.controller.regulator.last_set_limit_ts
        self.now += 30
        self.states["sensor.grid"].last_reported = self.wall()
        self.pending(16., received=self.now-30, displayed=7.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_current_publication")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.regulator.last_set_limit_ts, accepted)

    async def test_raw_peak_still_sends_urgent_zero_during_publication(self):
        self.pending(8., received=98, displayed=16.)
        self.states["sensor.grid"].state = "45"
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])

    async def test_fault_still_sends_zero_during_publication(self):
        self.pending(8., received=98, displayed=16.)
        self.states["sensor.status"].state = "unavailable"
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])

    async def test_fixed_watchdog_does_not_rearm_on_changed_sdk_or_get(self):
        self.pending(8., received=98, displayed=10.)
        first = self.controller._read_input()
        deadline = self.controller._current_publication_deadline
        timer = next(item for item in self.active_timers() if item.due == deadline)
        self.assertTrue(first.data_valid)
        self.assertEqual(deadline, 101.)
        self.now += .5
        self.native.sdk_current = 11.
        self.native.divergence_started_monotonic = self.now
        self.install(self.snapshot(current=11., observed=100))
        self.controller._read_input()
        self.assertEqual(self.controller._current_publication_deadline, deadline)
        self.now = deadline
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_called_once()
        expired = self.controller._read_input()
        self.assertFalse(expired.data_valid)
        self.assertIn("native_publication_diverged", expired.data_error)
        self.assertEqual(self.controller.source_diagnostics["source_health"], "unhealthy")
        self.assertEqual(expired.charger_report_ts, first.charger_report_ts)
        self.assertEqual(expired.charger_current, first.charger_current)

    async def test_smaller_alignment_window_caps_watchdog(self):
        self.controller.settings.measurement_settle_seconds = .5
        self.pending(8., received=98, displayed=10.)
        self.controller._read_input()
        self.assertEqual(self.controller._current_publication_deadline, 100.5)
        self.now = 100.5
        self.assertFalse(self.controller._read_input().data_valid)

    async def test_old_generation_watchdog_is_inert(self):
        self.pending(8., received=98, displayed=10.)
        self.controller._read_input()
        timer = next(item for item in self.active_timers() if item.due == 101)
        self.controller._generation += 1
        self.now = 101
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_not_called()

    async def test_proven_new_publication_episode_does_not_inherit_old_expiry(self):
        self.pending(8., received=98, displayed=10.)
        self.controller._read_input()
        self.assertEqual(self.controller._current_publication_deadline, 101.)
        self.now += .02
        self.set_native(10., received=self.now, changed_at=self.now)
        self.native.committed = True
        self.native.native_publication_pending = False
        self.native.publication_generation = 1
        # A source revision can observe a real committed coherent interval
        # without running _read_input; the adapter retains the episode proof.
        self.controller._charger_source_revision()
        self.now = 102.
        self.pending(10., received=100.02, displayed=11.)
        self.native.publication_generation = 2
        inp = self.controller._read_input()
        self.assertTrue(inp.data_valid)
        self.assertEqual(self.controller._current_publication_deadline, 103.)

    async def test_commit_cancels_watchdog_and_has_no_fabricated_receipt(self):
        self.pending(8., received=98, displayed=float32(9.965))
        before = self.controller._read_input()
        self.now += .02
        self.set_native(float32(9.965), received=self.now, changed_at=self.now,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        self.native.committed = True
        self.native.native_publication_pending = False
        self.native.divergence_started_monotonic = None
        self.states["sensor.car"].state = ha_state(self.native.current)
        after = self.controller._read_input()
        self.assertFalse(after.charger_publication_pending)
        self.assertIsNone(self.controller._current_publication_deadline)
        self.assertEqual(after.charger_report_ts, self.wall(self.now).timestamp())
        self.assertNotEqual(after.charger_report_ts, before.charger_report_ts)
        self.assertEqual(dict(after.charger_observation_sequences)[182], 2)

    async def test_pending_change_invalidates_input_revision_without_retiming(self):
        self.set_native(8., received=98, changed_at=90)
        self.native.committed = True
        before = self.controller._read_input()
        old_revision = self.controller._input_revision(before)
        self.native.native_publication_pending = True
        self.native.divergence_started_monotonic = self.now
        self.native.sdk_current = 10.
        after = self.controller._read_input()
        self.assertNotEqual(old_revision, self.controller._input_revision(after))
        self.assertEqual(before.charger_current, after.charger_current)
        self.assertEqual(before.charger_report_ts, after.charger_report_ts)


class CurrentPublicationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    update_entry = recovery.NormalSheddingRuntimeTests.update_entry
    wall = recovery.NormalSheddingRuntimeTests.wall
    snapshot = recovery.NormalSheddingRuntimeTests.snapshot
    schedule = recovery.NormalSheddingRuntimeTests.schedule
    active_timers = recovery.NormalSheddingRuntimeTests.active_timers
    asyncSetUp = recovery.NormalSheddingRuntimeTests.asyncSetUp
    asyncTearDown = recovery.NormalSheddingRuntimeTests.asyncTearDown
    enter_retry = recovery.NormalSheddingRuntimeTests.enter_retry

    async def test_ready_real_recovery_publication_wait_spends_no_attempt_or_checkpoint(self):
        await self.enter_retry()  # Failed reduction -> zero -> full stable restart wait.
        read = self.controller._read_input
        before = deepcopy(self.regulator.export_overload_state()["normal_control"])
        count = len(self.adapter.calls)
        checkpoints = []
        save = self.controller._budget_store.async_save

        async def inspect(state):
            checkpoints.append(deepcopy(state["normal_control"]))
            return await save(state)

        with patch.object(self.controller, "_read_input", side_effect=lambda: replace(
                read(), charger_publication_pending=True)), patch.object(
                self.controller._budget_store, "async_save", side_effect=inspect):
            await self.controller.async_tick()
        after = self.regulator.export_overload_state()["normal_control"]
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_current_publication")
        self.assertEqual(len(self.adapter.calls), count)
        self.assertFalse(after["retry_submission_pending"])
        self.assertEqual(after["stops"], before["stops"])
        self.assertEqual(self.regulator._uncertainty_last_attempt, None)
        self.assertTrue(all(not state["retry_submission_pending"] for state in checkpoints))
        self.now += .02
        for state in self.states.values():
            state.last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.assertEqual(len(self.adapter.calls), count+1)


if __name__ == "__main__":
    unittest.main()
