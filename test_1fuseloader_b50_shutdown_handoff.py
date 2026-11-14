"""B50 shutdown handoff regressions through actual controller code, offline.

The runtime, finite SDK dispatch boundary, receipt validation and public Store
path are real integration code. Native packets, clocks and device replies are
synthetic. Only tests defined here are loaded; inherited older suites run in
their own files. No household data or live device/network operation is used.
"""
import asyncio
from copy import deepcopy
import json
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_1fuseloader_restart_bridge_runtime as bridge_fixture
import test_1fuseloader_runtime as fixtures


class B50ShutdownHandoffTests(bridge_fixture.RestartBridgeRuntimeTests):
    def short_prepare_timeout(self, seconds):
        # The standalone loader deliberately removes its temporary module
        # registrations; patch the loaded function's own global constant.
        function = self.controller._restart_bridge.async_prepare_shutdown.__func__
        return patch.dict(function.__globals__, {'PREPARE_TIMEOUT_SECONDS': seconds})

    def native_only(self, at, current):
        """A genuine conductor frame is independent of an MQTT grid packet."""
        self.now = at
        self.sequence += 1
        self.native_vector(current, (182, 183), {182: at, 183: at},
                           {182: self.sequence, 183: self.sequence}, at)
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()

    def independent_grid(self, at, value, sampled_at):
        self.now = at
        self.states['sensor.grid'].state = str(value)
        self.states['sensor.grid'].last_reported = self.wall(sampled_at)
        source = self.controller._l1_source
        source.receive(SimpleNamespace(topic=source.config.topic, retain=False,
            payload=json.dumps({'I1': value, 'sec': self.wall(sampled_at).timestamp()})))
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()

    async def begin_without_auto_response(self):
        self.bridge_reply = False
        entered = asyncio.Event()
        original = self.adapter.async_execute_with_dispatch
        async def execute(decision, on_dispatch):
            result = await original(decision, on_dispatch)
            if decision.current_p1 == 7 and decision.ttl_seconds == 300:
                entered.set()
            return result
        self.adapter.async_execute_with_dispatch = execute
        task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
        await asyncio.wait_for(entered.wait(), 1.)
        return task

    async def finish_or_cancel(self, task, timeout=1.):
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def assert_quiescent(self):
        self.assertTrue(self.controller._stopped)
        self.assertIsNone(self.controller.unsub_states)
        self.assertIsNone(self.controller.unsub_timer)
        self.assertIsNone(self.controller._delivery_unsub)
        self.assertIsNone(self.controller._current_observation_unsub)
        self.assertIsNone(self.controller._telemetry_unsub)
        self.assertFalse(any(not item.removed for item in self.shutdown_jobs))
        calls = list(self.adapter.calls)
        self.now += 2.
        self.states['sensor.grid'].state = '39'
        fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller, None)
        await self.controller.async_tick()
        await asyncio.sleep(0)
        self.assertEqual(self.adapter.calls, calls,
            'Teardown callbacks must not produce any later positive or zero command')

    def assert_failure_phase(self, phase):
        diagnostics = self.controller._restart_bridge.diagnostics()
        self.assertEqual(diagnostics.get('restart_bridge_failed_phase'), phase, diagnostics)
        self.assertNotEqual(diagnostics['restart_bridge_last_error'],
                            'bridge_prepare_timeout_or_failure', diagnostics)
        self.assertFalse(self.controller._restart_bridge.armed)

    async def test_immutable_config_entry_data_can_prepare_finite_bridge(self):
        regulator = await self.healthy_session()
        self.entry.data = MappingProxyType(dict(self.entry.data))
        before = deepcopy(regulator.export_overload_state())
        await self.stage_one_shutdown()
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual((marker['cap'], marker['ttl_seconds']), (7, 300))
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 300)])
        after = regulator.export_overload_state()
        for key in ('used_seconds', 'failure_reason', 'automatic_cycle'):
            self.assertEqual(after[key], before[key])
        await self.assert_quiescent()

    async def test_repeated_minimum_frames_pin_first_response_for_later_grid(self):
        await self.healthy_session()
        task = await self.begin_without_auto_response()
        self.native_only(171., 7.1)
        await asyncio.sleep(.07)
        self.native_only(172., 7.1)
        await asyncio.sleep(.07)
        self.native_only(173., 7.1)
        # This independent measurement follows the FIRST physical response,
        # but predates the newest EV frame. New frames cannot move its target.
        self.independent_grid(173.1, 11., 171.5)
        self.assertTrue(await self.finish_or_cancel(task))
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual(marker['physical_response_wall'], self.wall(171.).timestamp())
        self.assertEqual(marker['grid_response_wall'], self.wall(171.5).timestamp())
        self.assertEqual(marker['expires_wall'], marker['dispatch_wall'] + 300.)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        await self.assert_quiescent()

    async def test_later_above_minimum_frame_revokes_pinned_response(self):
        await self.healthy_session()
        with self.short_prepare_timeout(.5):
            task = await self.begin_without_auto_response()
            self.native_only(171., 7.1)
            await asyncio.sleep(.07)
            self.native_only(172., 8.5)
            self.independent_grid(172.1, 12., 171.5)
            self.assertFalse(await self.finish_or_cancel(task))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertNotEqual(self.persisted_bridge()[1]['state'], 'acknowledged')
        self.assertIn('response', self.controller._restart_bridge.diagnostics()['restart_bridge_last_error'])
        await self.assert_quiescent()

    async def test_later_partial_higher_conductor_cannot_arm_blind_minimum(self):
        await self.healthy_session()
        with self.short_prepare_timeout(.5):
            task = await self.begin_without_auto_response()
            self.native_only(171., 7.1)
            await asyncio.sleep(.07)
            self.native_only(172., 8.5)
            # A newer partial native publication is not response credit. Its
            # higher observed conductor still contradicts an unobserved7A
            # shutdown grant; the first proof must not hide that new input.
            self.native.observation_evidence_credit = ((182, False), (183, False))
            self.independent_grid(172.1, 12., 171.5)
            self.assertFalse(await self.finish_or_cancel(task))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertNotEqual(self.persisted_bridge()[1]['state'], 'acknowledged')
        await self.assert_quiescent()

    async def test_partial_minimum_frame_does_not_retime_first_genuine_response(self):
        await self.healthy_session()
        task = await self.begin_without_auto_response()
        self.native_only(171., 7.1)
        await asyncio.sleep(.07)
        self.native_only(172., 7.1)
        self.native.observation_evidence_credit = ((182, False), (183, False))
        self.independent_grid(172.1, 11., 171.5)
        self.assertTrue(await self.finish_or_cancel(task))
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['physical_response_wall'], self.wall(171.).timestamp())
        self.assertEqual(marker['grid_response_wall'], self.wall(171.5).timestamp())
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        await self.assert_quiescent()

    async def test_partial_higher_during_ack_checkpoint_withdraws_before_arming(self):
        await self.healthy_session()
        entered, release = asyncio.Event(), asyncio.Event()
        original = fixtures.FakeStore.async_save
        async def held_ack(store, payload):
            if 'restart' in store.key and payload.get('state') == 'acknowledged':
                entered.set()
                await release.wait()
            await original(store, payload)
        with patch.object(fixtures.FakeStore, 'async_save', held_ack):
            task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
            try:
                await asyncio.wait_for(entered.wait(), 1.)
                self.native_only(172., 8.5)
                self.native.observation_evidence_credit = ((182, False), (183, False))
            finally:
                release.set()
            self.assertFalse(await self.finish_or_cancel(task))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assertNotEqual(self.persisted_bridge()[1]['state'], 'acknowledged')
        self.assert_failure_phase('final_check')
        await self.assert_quiescent()

    async def test_old_out_of_order_higher_frame_cannot_replace_pinned_response(self):
        await self.healthy_session()
        task = await self.begin_without_auto_response()
        self.native_only(171., 7.1)
        await asyncio.sleep(.07)
        self.now = 172.
        self.sequence += 1
        self.native_vector(8.5, (182, 183), {182: 170.5, 183: 170.5},
                           {182: self.sequence, 183: self.sequence}, 170.5)
        self.native.observation_evidence_credit = ((182, False), (183, False))
        self.independent_grid(172.1, 11., 171.5)
        self.assertTrue(await self.finish_or_cancel(task))
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['physical_response_wall'], self.wall(171.).timestamp())
        self.assertEqual(marker['grid_response_wall'], self.wall(171.5).timestamp())
        self.assertEqual(marker['expires_wall'], marker['dispatch_wall'] + 300.)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        await self.assert_quiescent()

    async def test_prepare_diagnostics_are_finite_bounded_and_contain_no_source_identity(self):
        await self.healthy_session()
        await self.stage_one_shutdown()
        diagnostics = self.controller._restart_bridge.diagnostics()
        encoded = json.dumps(diagnostics, allow_nan=False, sort_keys=True)
        self.assertLess(len(encoded), 2500)
        for private_value in ('synthetic-selected-source', 'test-entry',
                              'sensor.grid', 'currently/one/'):
            self.assertNotIn(private_value, encoded)
        self.assertEqual(diagnostics['restart_bridge_prepare_phase'], 'armed')
        self.assertIsNone(diagnostics['restart_bridge_failed_phase'])
        self.assertIsNone(diagnostics['restart_bridge_error_type'])
        self.assertGreaterEqual(diagnostics['restart_bridge_prepare_elapsed_seconds'], 0.)
        for name in ('restart_bridge_prepare_deadline_utc',
                     'restart_bridge_physical_response_at_utc',
                     'restart_bridge_grid_response_at_utc'):
            self.assertIsInstance(diagnostics[name], str, name)
        await self.assert_quiescent()

    async def test_disabled_bridge_still_quiesces_without_new_command(self):
        await self.healthy_session()
        self.configure(graceful_restart_bridge=False)
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual(self.adapter.calls, [])
        await self.assert_quiescent()

    async def test_ineligible_bridge_still_quiesces_without_new_permission(self):
        await self.healthy_session()
        self.now += 1.
        self.grid(35.)
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual(self.adapter.calls, [])
        await self.assert_quiescent()

    async def test_unexpected_prepare_exception_cannot_leave_ordinary_controller_live(self):
        await self.healthy_session()
        with patch.object(self.controller._restart_bridge, 'async_prepare_shutdown',
                          new_callable=AsyncMock, side_effect=RuntimeError('synthetic local failure')):
            self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual(self.adapter.calls, [])
        await self.assert_quiescent()

    async def test_cancelled_prepare_still_removes_listeners_and_prevents_ghost_command(self):
        await self.healthy_session()
        entered = asyncio.Event()
        async def waiting():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.controller._restart_bridge, 'async_prepare_shutdown', waiting):
            task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
            await asyncio.wait_for(entered.wait(), 1.)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.adapter.calls, [])
        await self.assert_quiescent()

    async def test_teardown_unsubscribes_before_first_bridge_close_await(self):
        await self.healthy_session()
        self.configure(graceful_restart_bridge=False)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller._restart_bridge.async_close
        async def slow_close():
            entered.set()
            await release.wait()
            await original()
        with patch.object(self.controller._restart_bridge, 'async_close', slow_close):
            task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
            try:
                await asyncio.wait_for(entered.wait(), 1.)
                self.assertTrue(self.controller._stopped)
                for name in ('unsub_states', 'unsub_timer', '_delivery_unsub',
                             '_current_observation_unsub', '_telemetry_unsub'):
                    self.assertIsNone(getattr(self.controller, name), name)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        await self.assert_quiescent()

    async def test_lock_wait_failure_is_identifiable_and_does_not_send(self):
        await self.healthy_session()
        await self.controller._lock.acquire()
        task = None
        with self.short_prepare_timeout(.08):
            try:
                task = asyncio.create_task(self.controller._async_prepare_shutdown_bridge())
                await asyncio.sleep(.14)
            finally:
                self.controller._lock.release()
            self.assertFalse(await self.finish_or_cancel(task))
        self.assertEqual(self.adapter.calls, [])
        self.assert_failure_phase('wait_lock')
        await self.assert_quiescent()

    async def test_prepared_store_failure_is_identifiable_and_never_reaches_sdk(self):
        await self.healthy_session()
        original = fixtures.FakeStore.async_save
        async def fail_marker(store, payload):
            if 'restart' in store.key:
                raise OSError('synthetic marker write failure')
            await original(store, payload)
        with patch.object(fixtures.FakeStore, 'async_save', fail_marker):
            self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual(self.adapter.calls, [])
        self.assert_failure_phase('save_prepared')
        await self.assert_quiescent()

    async def test_sdk_failure_is_identifiable_and_withdraws_possible_bridge(self):
        await self.healthy_session()
        self.dispatch_failure = TimeoutError('synthetic SDK acceptance unavailable')
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assert_failure_phase('dispatch_minimum')
        await self.assert_quiescent()

    async def test_missing_physical_response_is_distinct_from_missing_grid(self):
        await self.healthy_session()
        self.bridge_reply = False
        with self.short_prepare_timeout(.1):
            self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assert_failure_phase('wait_physical')
        await self.assert_quiescent()

    async def test_missing_grid_confirmation_records_its_own_failure_phase(self):
        await self.healthy_session()
        with self.short_prepare_timeout(.2):
            task = await self.begin_without_auto_response()
            self.native_only(171., 7.1)
            self.assertFalse(await self.finish_or_cancel(task))
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7, 0])
        self.assert_failure_phase('wait_grid')
        await self.assert_quiescent()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(B50ShutdownHandoffTests(name)
        for name, method in B50ShutdownHandoffTests.__dict__.items()
        if name.startswith('test_') and callable(method))


if __name__ == '__main__':
    unittest.main()
