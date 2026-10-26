"""Synthetic native conductor/GET clocks through the real controller; offline."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_1fuseloader_refresh_deadlines as clocks
import test_1fuseloader_runtime as fixtures


class ClockProvenanceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.RefreshDeadlineTests.update_entry
    wall = clocks.RefreshDeadlineTests.wall
    schedule = clocks.RefreshDeadlineTests.schedule
    active_timers = clocks.RefreshDeadlineTests.active_timers

    async def asyncSetUp(self):
        await clocks.RefreshDeadlineTests.asyncSetUp(self)
        self.now = 100.0
        self.owner = (11, 12, 13, 14)
        self.adapter.observation_clock_identity = self.owner
        self.controller._configured_control_identity = Mock(return_value='synthetic-selected-source')
        self.states['sensor.status'].last_changed = self.wall(90)
        self.set_native(8, received=98, ids=(182,), changed_at=90)

    asyncTearDown = clocks.RefreshDeadlineTests.asyncTearDown

    def set_native(self, value, *, received=None, ids=(182,), changed_at=None,
                   source=None, report_times=None, sequences=None):
        self.states['sensor.car'].state = str(value)
        self.states['sensor.car'].last_reported = self.wall()
        self.states['sensor.grid'].last_reported = self.wall()
        self.native = SimpleNamespace(current=value, observation_ids=ids,
            received_at=self.wall(received) if received is not None else None,
            source_identity=self.owner, error=None,
            source_timestamps=tuple((key, self.wall(source) if source is not None else None)
                                    for key in (182, 183, 184, 185)),
            observation_report_times=(tuple((key, self.wall(stamp)) for key, stamp in report_times)
                if report_times is not None else tuple((key, self.wall(received)) for key in ids)
                if received is not None else ()),
            observation_sequences=sequences or tuple((key, 1 if key in ids and received is not None else 0)
                                                    for key in (182, 183, 184, 185)),
            last_tuple_change_monotonic=changed_at)
        self.adapter.native_current_observation = self.native

    def snapshot(self, current=8, status='charging', *, observed=None, ids=(182,)):
        stamp = self.now-1 if observed is None else observed
        return SimpleNamespace(current=current, status=status, received_at=self.wall(),
            source_identity=self.owner, current_source_timestamp=self.wall(stamp),
            status_source_timestamp=self.wall(stamp), source_timestamp_error=None,
            current_source_observation_ids=ids,
            current_source_timestamps=tuple((key, self.wall(stamp)) for key in (182, 183, 184, 185)))

    def install(self, snapshot):
        self.controller._charger_snapshot = snapshot
        self.controller._charger_snapshot_selection = self.controller._charger_source_revision()

    async def test_matching_get_cannot_retime_known_native_event_or_change_family(self):
        before = self.controller._read_input()
        self.install(self.snapshot(observed=96.5))
        after = self.controller._read_input()
        self.assertEqual(before.charger_report_family, 'native_terminal_receipt')
        self.assertEqual(after.charger_report_family, before.charger_report_family)
        self.assertEqual(after.charger_report_provenance, before.charger_report_provenance)
        self.assertEqual(after.charger_report_ts, self.wall(98).timestamp())
        self.assertFalse(after.charger_event_source_known)
        self.assertFalse(after.charger_snapshot_verified)
        self.assertEqual(self.controller.source_diagnostics['charger_snapshot_current_source_timestamp_utc'],
                         self.wall(96.5).isoformat())
        self.assertEqual(self.states['sensor.car'].last_reported, self.wall())

    async def test_old_a_metadata_cannot_label_new_a_after_intervening_b(self):
        self.install(self.snapshot(current=8, observed=90))
        self.controller._read_input()
        self.now += 1
        self.set_native(5, received=self.now, changed_at=self.now)
        self.controller._read_input()
        self.now += 1
        self.set_native(8, received=self.now, changed_at=self.now)
        before = self.controller._read_input()
        self.install(self.snapshot(current=8, observed=90))
        after = self.controller._read_input()
        self.assertEqual(after.charger_report_ts, before.charger_report_ts)
        self.assertEqual(after.charger_report_family, 'native_terminal_receipt')
        self.assertFalse(after.charger_event_source_known)
        self.assertTrue(after.charger_current_trusted)

    async def test_old_direct_raw_provider_time_is_a_separate_proof_constraint(self):
        self.set_native(8, received=99, source=-500, changed_at=90)
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_report_ts, self.wall(99).timestamp())
        self.assertTrue(inp.charger_event_source_known)
        self.assertEqual(inp.charger_event_source_ts, self.wall(-500).timestamp())
        self.assertTrue(inp.data_valid)
        self.assertEqual(inp.charger_report_family, 'native_terminal_receipt')

    async def test_bootstrap_has_no_fake_ha_receipt_and_can_use_real_vendor_observation(self):
        self.set_native(8, received=None, changed_at=None)
        before = self.controller._read_input()
        self.assertIsNone(before.charger_report_ts)
        self.assertEqual(before.charger_observation_report_times, ())
        self.assertEqual(dict(before.charger_observation_sequences), {182: 0, 183: 0, 184: 0, 185: 0})
        self.assertTrue(before.data_valid)
        self.install(self.snapshot(observed=99))
        after = self.controller._read_input()
        self.assertEqual(after.charger_report_family, 'vendor_event')
        self.assertEqual(after.charger_report_ts, self.wall(99).timestamp())
        self.assertTrue(after.charger_snapshot_verified)
        self.assertIsNone(after.charger_observation_sequences)

    async def test_absent_legacy_api_is_distinct_from_unavailable_real_clock_evidence(self):
        del self.adapter.native_current_observation
        legacy = self.controller._read_input()
        self.assertEqual(legacy.charger_report_family, 'legacy')
        self.assertIsNone(legacy.charger_report_provenance)
        self.assertIsNone(legacy.charger_observation_sequences)
        self.adapter.native_current_observation = None
        unavailable = self.controller._read_input()
        self.assertEqual(unavailable.charger_report_family, 'native_terminal_receipt')
        self.assertIsNone(unavailable.charger_report_ts)
        self.assertTrue(unavailable.data_valid)

    async def test_same_value_ha_attributes_do_not_discard_only_useful_get(self):
        self.set_native(8, received=None)
        snapshot = self.snapshot(observed=99)
        self.install(snapshot)
        revision = self.controller._charger_source_revision()
        self.now += .1
        self.states['sensor.car'].last_reported = self.wall()
        self.states['sensor.status'].last_reported = self.wall()
        self.states['sensor.status'].attributes['unrelated_led'] = True
        self.assertEqual(self.controller._charger_source_revision(), revision)
        inp = self.controller._read_input()
        self.assertTrue(inp.charger_snapshot_verified)
        self.assertEqual(inp.charger_report_ts, snapshot.current_source_timestamp.timestamp())

    async def test_unrelated_tick_while_get_waits_does_not_discard_reply(self):
        self.set_native(8, received=None)

        async def reply():
            self.now += .1
            self.states['sensor.car'].last_reported = self.wall()
            self.states['sensor.status'].last_reported = self.wall()
            return self.snapshot(observed=self.now-.05)

        self.adapter.async_read_current_snapshot = AsyncMock(side_effect=reply)
        self.controller._request_current_snapshot()
        await self.controller._charger_snapshot_task
        self.assertIsNotNone(self.controller._charger_snapshot)
        self.assertTrue(self.controller._read_input().charger_snapshot_verified)

    async def test_true_current_or_pause_change_invalidates_cached_get_pair(self):
        for field, value in (('sensor.car', '6'), ('sensor.status', 'paused')):
            with self.subTest(field=field):
                self.states['sensor.status'].state = 'charging'
                self.set_native(8, received=None)
                self.install(self.snapshot(observed=99))
                self.states[field].state = value
                self.states[field].last_reported = self.wall()
                self.states[field].last_changed = self.wall()
                inp = self.controller._read_input()
                self.assertFalse(inp.charger_snapshot_verified)
                self.assertEqual(inp.status, self.states['sensor.status'].state)

    async def test_winner_change_retains_all_individual_clock_and_sequence_evidence(self):
        self.set_native(9, received=98, ids=(182,),
            report_times=((182, 98), (183, 90)), sequences=((182, 3), (183, 2), (184, 0), (185, 0)))
        first = self.controller._read_input()
        self.set_native(8, received=90, ids=(183,),
            report_times=((182, 99), (183, 90)), sequences=((182, 4), (183, 2), (184, 0), (185, 0)))
        second = self.controller._read_input()
        self.assertLess(second.charger_report_ts, first.charger_report_ts)
        self.assertNotEqual(second.charger_report_provenance, first.charger_report_provenance)
        self.assertEqual(dict(second.charger_observation_report_times)[182], self.wall(99).timestamp())
        self.assertEqual(dict(second.charger_observation_sequences)[183], 2)

    async def test_tuple_quiet_timer_changes_no_clock_and_old_generation_is_inert(self):
        self.set_native(8, received=100, changed_at=100)
        before = self.controller._read_input()
        self.assertFalse(before.charger_increase_ready)
        timer = next(item for item in self.active_timers() if item.due == 100.25)
        self.controller._generation += 1
        self.now = 100.25
        self.controller._handle_source_change.reset_mock()
        await timer.callback(self.wall())
        self.controller._handle_source_change.assert_not_called()
        after = self.controller._read_input()
        self.assertTrue(after.charger_increase_ready)
        self.assertEqual(after.charger_report_ts, before.charger_report_ts)

    async def test_provenance_change_during_checkpoint_prevents_old_positive_dispatch(self):
        regulator = self.controller.regulator
        regulator.import_overload_state(dict(version=1, used_seconds=2000.0, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason='response_timeout'))
        await self.bootstrap_response()
        self.now += 1
        self.states['sensor.grid'].state = '23'
        self.set_native(7, received=self.now, source=self.now, changed_at=self.now,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.adapter.calls.clear()
        self.now += 10
        self.states['sensor.grid'].state = '20'
        self.set_native(7.2, received=self.now, source=self.now, changed_at=self.now-1,
            sequences=((182, 3), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.now += .3
        self.states['sensor.grid'].last_reported = self.wall()
        self.controller._handle_source_change.reset_mock()
        save = self.controller._budget_store.async_save

        async def rebind(state):
            result = await save(state)
            self.adapter.observation_clock_identity = (21, 22, 23, 24)
            return result

        self.controller._budget_store.async_save = rebind
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.mode, 'waiting_for_latest_sources')
        self.assertGreater(self.controller.last_decision.current_p1, 7)
        self.assertTrue(self.controller._handle_source_change.called, self.controller.last_decision)

    async def test_urgent_zero_is_not_delayed_by_tuple_quiescence(self):
        self.set_native(16, received=100, changed_at=100)
        self.states['sensor.grid'].state = '45'
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])

    async def bootstrap_response(self, *, source_offset=None):
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, enable_soft_guard=False, enable_pause_resume=False))
        self.adapter.async_read_current_snapshot = None
        self.states['sensor.status'].state = 'awaiting_start'
        self.states['sensor.grid'].state = '4'
        self.set_native(0, received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.now += 60
        self.set_native(0, received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        accepted_at = self.now
        self.now += 1
        self.states['sensor.status'].state = 'charging'
        self.states['sensor.status'].last_changed = self.wall()
        self.states['sensor.grid'].state = '20'
        self.set_native(16, received=self.now, report_times=((182, self.now),),
            sequences=((182, 1), (183, 0), (184, 0), (185, 0)),
            source=None if source_offset is None else accepted_at+source_offset)
        self.now += .3
        self.states['sensor.grid'].last_reported = self.wall()
        await self.controller.async_tick()
        return accepted_at

    async def steady_cache(self):
        baseline = len(self.adapter.calls)
        for _ in range(96):
            self.now += 5
            self.states['sensor.grid'].last_reported = self.wall()
            await self.controller.async_tick()
        self.assertTrue(all(call.current_p1 == 16 for call in self.adapter.calls[baseline:]))
        self.assertEqual(self.controller.last_decision.current_p1, 16)

    async def test_bootstrap_one_post_ack_response_then_steady_cache_does_not_stop(self):
        await self.bootstrap_response()
        await self.steady_cache()

    async def test_bootstrap_one_source_bound_response_keeps_receiver_sequence_identity(self):
        accepted_at = await self.bootstrap_response(source_offset=1)
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_report_family, 'native_terminal_receipt')
        self.assertEqual(dict(inp.charger_observation_sequences)[182], 1)
        self.assertTrue(inp.charger_event_source_known)
        self.assertGreater(inp.charger_event_source_ts, self.wall(accepted_at).timestamp())
        await self.steady_cache()

    async def test_provider_before_ack_does_not_hide_actual_post_dispatch_native_response(self):
        accepted_at = await self.bootstrap_response(source_offset=-1)
        inp = self.controller._read_input()
        self.assertGreater(inp.charger_report_ts, self.wall(accepted_at).timestamp())
        self.assertLess(inp.charger_event_source_ts, self.wall(accepted_at).timestamp())
        regulator = self.controller.regulator
        # Provider UTC is a coarse source clock, not the local dispatch clock.
        # This actual seq0->seq1 observation arrived after real SDK entry.
        self.assertTrue(regulator._ev_advanced_since(regulator._positive_ev_ts, '_positive_ev_ts'))
        self.assertIsNone(regulator._positive_cap)
        self.assertEqual(regulator._ev_established_value, 16.)
        self.assertEqual(regulator.last_target[0], 16)

    async def test_new_zero_proof_requires_source_event_after_command_when_available(self):
        self.controller.last_accepted_limit = dict(started_at_utc=self.wall(99).isoformat())
        self.set_native(0, received=100, source=98)
        self.assertIsNone(self.controller._zero_readback_evidence(self.controller._read_input())['ev_current'])
        self.set_native(0, received=100, source=99.5)
        self.assertEqual(float(self.controller._zero_readback_evidence(self.controller._read_input())['ev_current']), 0)
        self.set_native(0, received=100)
        self.assertEqual(float(self.controller._zero_readback_evidence(self.controller._read_input())['ev_current']), 0)

    async def test_post_restart_overload_cannot_use_pre_restart_source_event(self):
        self.controller.settings.overload_enabled = True
        self.controller._overload_restore_receipt_floor = self.wall(99).timestamp()
        self.set_native(8, received=100, source=98)
        # Isolate the runtime's second line of defence: even a stale positive
        # plan must pass the original restore wall floor after its checkpoint.
        decision = self.controller.regulator.evaluate(self.controller._read_input())
        decision = replace(decision, mode='overload_hold', command='set_limit',
            current_p1=7, current_p2=0, current_p3=0, should_call_service=True,
            overload_remaining_seconds=1000)
        self.controller.regulator.evaluate = Mock(return_value=decision)
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertEqual(self.controller.last_decision.mode, 'overload_control_blocked')

    async def test_tuple_quiescence_does_not_delay_identical_live_renewal(self):
        await self.bootstrap_response()
        self.now += 1
        self.states['sensor.grid'].state = '23'
        self.set_native(8, received=self.now, source=self.now, changed_at=self.now,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 8)
        self.now += 1
        self.set_native(8, received=self.now, source=self.now, changed_at=self.now,
            sequences=((182, 3), (183, 0), (184, 0), (185, 0)))
        self.now += .3
        self.states['sensor.grid'].last_reported = self.wall()
        await self.controller.async_tick()
        self.adapter.calls.clear()
        self.now += 30
        self.set_native(8, received=self.now, source=self.now, changed_at=self.now,
            sequences=((182, 4), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertFalse(self.controller._read_input().charger_increase_ready)
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [8])

    async def test_new_same_value_source_event_waits_for_adjacent_conductor_frame_before_increase(self):
        r = self.controller.regulator
        self.controller.settings.nominal_import_only_fallback = True
        r.last_target = (7, 0, 0, 60)
        r.last_set_limit_ts = r.last_action_ts = 95
        r._accepted_control_source_id = 'synthetic-selected-source'
        self.states['sensor.grid'].state = '14'
        self.set_native(7, received=98, source=98, changed_at=90)
        r.evaluate(self.controller._read_input())
        self.set_native(7, received=100, source=100, changed_at=100,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.mode, 'waiting_for_stable_current_frame')
        self.assertEqual(self.adapter.calls, [])
        self.now += .02
        self.set_native(0, received=self.now, source=self.now, changed_at=self.now,
            ids=(182, 183, 184, 185),
            sequences=((182, 3), (183, 1), (184, 1), (185, 1)))
        await self.controller.async_tick()
        self.assertFalse(any(call.current_p1 > 7 for call in self.adapter.calls))
        self.assertEqual(r.last_target[0], 7)

    async def test_tuple_quiescence_does_not_delay_positive_reduction(self):
        await self.bootstrap_response()
        self.adapter.calls.clear()
        self.now += 1
        self.states['sensor.grid'].state = '32'
        self.set_native(16, received=self.now, source=self.now, changed_at=self.now,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.assertFalse(self.controller._read_input().charger_increase_ready)
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [7])


if __name__ == '__main__':
    unittest.main()
