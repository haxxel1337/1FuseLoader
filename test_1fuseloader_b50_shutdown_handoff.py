"""B50 shutdown handoff regressions through actual controller code, offline.

The runtime, finite SDK dispatch boundary, receipt validation and public Store
path are real integration code. Native packets, clocks and device replies are
synthetic. Only tests defined here are loaded; inherited older suites run in
their own files. No household data or live device/network operation is used.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_1fuseloader_restart_bridge_runtime as bridge_fixture
import test_1fuseloader_runtime as fixtures


class B50ShutdownHandoffTests(bridge_fixture.RestartBridgeRuntimeTests):
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
        with patch.object(self.controller._restart_bridge, 'async_commit_managed_shutdown',
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
        with patch.object(self.controller._restart_bridge, 'async_commit_managed_shutdown', waiting):
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


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(B50ShutdownHandoffTests(name)
        for name, method in B50ShutdownHandoffTests.__dict__.items()
        if name.startswith('test_') and callable(method))


if __name__ == '__main__':
    unittest.main()
