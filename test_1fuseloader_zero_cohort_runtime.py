"""Source-derived stop during a synthetic multi-conductor reduction; offline."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

import test_1fuseloader_clock_provenance_runtime as clocks
from test_1fuseloader_ha_current_representation import float32, ha_state


class ZeroCohortRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    asyncSetUp = clocks.ClockProvenanceRuntimeTests.asyncSetUp
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    install = clocks.ClockProvenanceRuntimeTests.install

    def native_vector(self, value, winners, reports, sequences, changed_at):
        received = min(reports[key] for key in winners) if all(key in reports for key in winners) else None
        self.native = SimpleNamespace(current=value, observation_ids=winners,
            received_at=self.wall(received) if received is not None else None,
            source_identity=self.owner, error=None,
            source_timestamps=tuple((key, self.wall(reports[key]) if key in reports else None)
                                    for key in (182, 183, 184, 185)),
            observation_report_times=tuple((key, self.wall(stamp)) for key, stamp in reports.items()),
            observation_sequences=tuple((key, sequences.get(key, 0)) for key in (182, 183, 184, 185)),
            last_tuple_change_monotonic=changed_at, committed=True,
            sdk_current=value, native_publication_pending=False)
        self.adapter.native_current_observation = self.native
        self.states["sensor.car"].state = ha_state(value)
        self.states["sensor.car"].last_reported = self.wall()

    def grid(self, value, reported=None):
        self.states["sensor.grid"].state = str(value)
        self.states["sensor.grid"].last_reported = self.wall(reported)

    async def reduced_offer(self):
        r = self.controller.regulator
        r.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            overload_enabled=True, measurement_settle_seconds=25,
            enable_soft_guard=False, enable_pause_resume=False))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "awaiting_start"
        for at in (100., 160.):
            self.now = at
            self.grid(4.)
            self.native_vector(0., (182, 183, 184, 185), {}, {}, 90.)
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.now = 162.5
        self.states["sensor.status"].state = "charging"
        self.states["sensor.status"].last_changed = self.wall()
        self.native_vector(float32(16.141), (182,), {182: 162., 183: 162.25},
            {182: 1, 183: 1}, 162.25)
        self.grid(20.1)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._positive_cap)
        self.now = 195.
        self.grid(24.)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 15)
        self.now = 228.
        self.grid(23.)
        await self.controller.async_tick()
        self.assertEqual(r.last_target[0], 15)
        self.assertEqual(r._awaiting_reduction_target, 15)
        self.adapter.calls.clear()

    async def test_reduction_cohort_old_winner_does_not_commit_a_terminal_zero(self):
        await self.reduced_offer()
        r = self.controller.regulator
        before = r.export_overload_state()
        self.now = 232.36
        self.grid(23., 229.)
        # One conductor has fallen. Another still wins MAX with its old clock;
        # the full old-winner tuple is committed, not an HA-only draft value.
        self.native_vector(float32(16.139), (183,), {182: 232.30, 183: 162.25},
            {182: 2, 183: 1}, 232.30)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [], self.controller.last_decision)
        self.assertEqual(r.last_target[0], 15)
        self.assertFalse(r._restart_required)
        deadline = self.controller.last_decision.source_processing_deadline_ts
        self.assertIsNotNone(deadline)
        self.assertLessEqual(deadline, self.now+1.)
        self.assertTrue(any(abs(timer.due-deadline) < 1e-6 for timer in self.active_timers()))
        self.assertEqual(r.last_set_limit_ts, 228.)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"],
            before.get("normal_control", {}).get("stops", 0))
        self.now = 232.45
        self.native_vector(float32(15.137), (182, 183), {182: 232.30, 183: 232.44},
            {182: 2, 183: 2}, 232.44)
        await self.controller.async_tick()
        self.now = 235.
        self.grid(21.)
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls))
        self.assertEqual(r.export_overload_state()["used_seconds"], 3601.)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    async def test_future_tuple_clock_cannot_open_a_processing_hold(self):
        await self.reduced_offer()
        self.now = 232.36
        self.grid(23., 229.)
        self.native_vector(float32(16.139), (183,), {182: 232.30, 183: 162.25},
            {182: 2, 183: 1}, self.now+10.)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertIsNone(self.controller.last_decision.source_processing_deadline_ts)
        self.assertEqual(self.controller.source_diagnostics["source_health"], "unhealthy")


if __name__ == "__main__":
    unittest.main()
