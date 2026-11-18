"""Recent accepted decreases and the finite restart handoff, offline."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

import test_1fuseloader_b50_shutdown_handoff as handoff


class RecentDecreaseRestartBridgeTests(handoff.B50ShutdownHandoffTests):
    async def recent_decrease(self):
        regulator = await self.healthy_session()
        await self.sample(172., 24.9)
        self.assertEqual(regulator.last_target, (14, 0, 0, 60))
        await self.sample(178., 22.5, 14., 177.)
        await self.sample(184., 23.4, 14., 183.)
        self.assertEqual(regulator.last_target, (13, 0, 0, 60))
        await self.sample(188., 22.5, 13., 187.)
        policy = regulator._get_meter_envelope_policy()
        self.assertEqual(policy.state.value, 'NORMAL')
        self.assertIn((14, 232.), policy.possible_positive.values())
        self.assertIn((13, 244.), policy.possible_positive.values())
        self.adapter.calls.clear()
        return policy

    async def physical_and_grid(self):
        self.native_only(189., 7.1)
        await asyncio.sleep(.06)
        self.independent_grid(190., 16.6, 189.5)
        await asyncio.sleep(.06)

    async def test_recent_normal_decrease_can_arm_after_new_readback(self):
        policy = await self.recent_decrease()
        original_horizons = deepcopy(policy.possible_positive)
        bridge = self.controller._restart_bridge
        task = await self.begin_without_auto_response()
        self.assertEqual(self.adapter.calls[0].committed_current_p1, 13)
        self.assertEqual(self.adapter.calls[0].command_urgency, 'protective_reduction')
        try:
            await self.physical_and_grid()
            self.assertEqual(bridge.diagnostics()['restart_bridge_prepare_phase'], 'wait_readback')
            self.assertFalse(bridge.armed)
            self.now = 190.2
            self.adapter.reported_limits = (7, 0, 0)
            self.adapter.reported_limit_receipts = (self.now, None, None)
            self.assertTrue(await self.finish_or_cancel(task))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 300)])
        self.assertEqual(policy.possible_positive, original_horizons)
        marker = self.persisted_bridge()[1]
        self.assertEqual(marker['state'], 'acknowledged')
        self.assertEqual(marker['expires_wall'] - marker['dispatch_wall'], 300.)

    async def test_cached_matching_readback_cannot_arm_with_older_higher_offer(self):
        await self.recent_decrease()
        self.adapter.reported_limits = (7, 0, 0)
        with self.short_prepare_timeout(.35):
            task = await self.begin_without_auto_response()
            # The SDK receipt is synthetic. Replace the fixture's auto-readback
            # receipt with this scenario's unchanged, earlier native 7 A.
            self.adapter.reported_limit_receipts = (187., None, None)
            await self.physical_and_grid()
            self.assertFalse(await self.finish_or_cancel(task))
        self.assert_failure_phase('wait_readback')
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 300), (0, 60)])
        self.assertEqual(self.persisted_bridge()[1]['state'], 'aborted')

    async def test_other_source_cannot_borrow_a_known_predecessors_ack(self):
        policy = await self.recent_decrease()
        previous_key = next(key for key, value in policy.possible_positive.items() if value == (14, 232.))
        policy.lease.normal_commands[previous_key]['source_id'] = 'other-source'
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assert_failure_phase('qualify')
        diagnostics = self.controller._restart_bridge.diagnostics()
        self.assertEqual(diagnostics['restart_bridge_last_error'], 'bridge_qualification_previous_authority_unknown')
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(any('restart' in key for key in self.hass._test_store_disk))

    async def test_new_unacknowledged_authority_between_plan_and_sdk_blocks_write(self):
        policy = await self.recent_decrease()
        original = self.adapter.async_execute_with_dispatch

        async def with_unknown_predecessor(decision, callback):
            # The protective bridge decision has been built and queued. Its
            # callback must requalify before the adapter sends the request.
            policy.possible_positive['unacknowledged-plan'] = (15, 240.)
            return await original(decision, callback)

        self.adapter.async_execute_with_dispatch = with_unknown_predecessor
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assert_failure_phase('dispatch_minimum')
        diagnostics = self.controller._restart_bridge.diagnostics()
        self.assertEqual(diagnostics['restart_bridge_last_error'], 'bridge_qualification_previous_authority_unknown')
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.persisted_bridge()[1]['state'], 'aborted')

    async def test_valid_entity_grid_text_retains_all_qualification_limits(self):
        await self.recent_decrease()
        bridge = self.controller._restart_bridge
        snapshot = self.controller._read_input()
        self.assertIsNone(bridge._producer_problem(replace(snapshot, grid_l1='22.5')))
        self.assertEqual(bridge._producer_problem(replace(snapshot, grid_l1='25.1')),
                         'bridge_qualification_grid_ceiling')
        for invalid in (True, float('nan'), 'nan', 'unavailable', '1,0'):
            with self.subTest(invalid=invalid):
                self.assertEqual(bridge._producer_problem(replace(snapshot, grid_l1=invalid)),
                                 'bridge_qualification_grid_invalid')

    async def test_early_correlated_vendor_rejection_vetoes_http_accepted_bridge(self):
        await self.healthy_session()
        original = self.adapter.async_execute_with_dispatch

        async def reject_before_http_acceptance(decision, callback):
            reply = await original(decision, callback)
            if decision.current_p1 == 7:
                self.controller._handle_delivery(SimpleNamespace(command_type=22, ticks='222',
                    was_accepted=False))
                return SimpleNamespace(http_status=200, correlation_ids=((22, '222'),))
            return reply

        self.adapter.async_execute_with_dispatch = reject_before_http_acceptance
        self.assertFalse(await self.controller._async_prepare_shutdown_bridge())
        self.assert_failure_phase('dispatch_minimum')
        diagnostic = self.controller._restart_bridge.diagnostics()
        self.assertEqual(diagnostic['restart_bridge_last_error'], 'bridge_vendor_rejected')
        self.assertEqual([(d.current_p1, d.ttl_seconds) for d in self.adapter.calls], [(7, 300), (0, 60)])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RecentDecreaseRestartBridgeTests(name)
        for name in RecentDecreaseRestartBridgeTests.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
