"""Offline restart-marker parsing, one-shot ownership and I/O race contracts."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_1fuseloader_runtime as fixture

bridge_module = fixture.load("restart_bridge_test", "restart_bridge.py")
Bridge = bridge_module.GracefulRestartBridge
logic = fixture.logic


class MemoryStore:
    def __init__(self):
        self.disk = None
        self.write_gate = None
        self.write_entered = asyncio.Event()
        self.skip_write = False
        self.writes = []

    async def async_save(self, value):
        self.write_entered.set()
        gate, self.write_gate = self.write_gate, None
        if gate is not None:
            await gate.wait()
        self.writes.append(deepcopy(value))
        if not self.skip_write:
            self.disk = deepcopy(value)

    async def async_load(self):
        return deepcopy(self.disk)


class RestartBridgeStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mono, self.wall = 20., 1020.
        self.store = MemoryStore()
        self.settings = logic.RegulatorSettings(active_control=True, dry_run=False,
            graceful_restart_bridge=True, meter_envelope_control=True,
            nominal_import_only_fallback=True)
        self.input = logic.RegulatorInput(self.mono, 'charging', 11., charger_current=7.,
            wall_now_ts=self.wall, grid_report_ts=self.wall,
            meter_grid_health=logic.SourceHealth(True),
            meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True), control_source_id='source-A')
        self.owner = SimpleNamespace(settings=self.settings,
            entry=SimpleNamespace(entry_id='offline-entry', data={'device_id':'offline-device'}),
            hass=SimpleNamespace(data={}, loop=SimpleNamespace(time=lambda:self.mono),
                async_create_task=asyncio.create_task),
            _adapter=SimpleNamespace(transport_block_reason=None),
            _configured_control_identity=lambda:'source-A', _electrical_selection=lambda:('L1','sensor.raw'),
            _l1_source_config=None, _l1_source=None, _l1_source_error=None,
            _protective_zero_pending=False, _budget_error=None,
            _budget_store=SimpleNamespace(verification_pending=False),
            regulator=logic.OneFuseRegulator(self.settings),
            _read_input=lambda:self.input)
        self.bridge = Bridge(self.owner, store=self.store)

    async def asyncTearDown(self):
        await self.bridge.async_close()

    def marker(self, **changes):
        data = dict(version=1, state='acknowledged', nonce='a'*32,
            binding=self.bridge._binding(), cap=7, ttl_seconds=300,
            prepared_wall=1000., dispatch_wall=1000.25, expires_wall=1300.25,
            possible_until_wall=1316., physical_response_wall=1000.8,
            grid_response_wall=1001., claimed_wall=None, ended_wall=None,
            unobserved_seconds=0., revision='b'*32)
        data.update(changes)
        return data

    async def restore(self, **changes):
        self.store.disk = self.marker(**changes)
        await self.bridge.async_restore()

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds
        self.input = replace(self.input, now_ts=self.mono, wall_now_ts=self.wall,
            grid_report_ts=self.wall)

    async def test_marker_is_claimed_exactly_once_with_original_expiry(self):
        await self.restore()
        self.assertTrue(self.bridge._active)
        self.assertEqual(self.store.disk['state'], 'claimed')
        self.assertEqual(self.store.disk['expires_wall'], 1300.25)
        other = Bridge(self.owner, store=self.store)
        await other.async_restore()
        self.assertFalse(other._active)
        self.assertEqual(other.diagnostics()['restart_bridge_state'], 'not_reusable')

    async def test_expired_marker_cannot_acquire_new_grace(self):
        self.advance(400)
        await self.restore()
        self.assertFalse(self.bridge._active)
        self.assertFalse(self.store.writes)

    async def test_unknown_pending_dispatch_never_becomes_eligible_boot_owner(self):
        await self.restore(state='prepared', dispatch_wall=None,
            expires_wall=1300., physical_response_wall=None, grid_response_wall=None)
        self.assertFalse(self.bridge._active)
        self.assertEqual(self.bridge._unknown_until, 1316.)
        self.assertTrue(self.owner._protective_zero_pending)

    async def test_binding_change_refuses_even_otherwise_valid_marker(self):
        data = self.marker()
        self.owner._configured_control_identity = lambda:'source-B'
        self.store.disk = data
        await self.bridge.async_restore()
        self.assertFalse(self.bridge._active)
        self.assertFalse(self.store.writes)

    async def test_setting_change_refuses_marker_without_mutating_ledger(self):
        data = self.marker()
        prior = deepcopy(self.owner.regulator.export_overload_state())
        self.owner.settings = replace(self.settings, max_charge_amps=15)
        self.store.disk = data
        await self.bridge.async_restore()
        self.assertFalse(self.bridge._active)
        self.assertEqual(self.owner.regulator.export_overload_state(), prior)

    async def test_claim_requires_independent_readback(self):
        self.store.skip_write = True
        await self.restore()
        self.assertFalse(self.bridge._active)
        self.assertEqual(self.store.disk['state'], 'acknowledged')

    async def test_disabled_option_never_reads_or_claims_store(self):
        self.owner.settings = replace(self.settings, graceful_restart_bridge=False)
        self.owner._read_input = lambda: (_ for _ in ()).throw(AssertionError('No input read'))
        await self.bridge.async_restore()
        self.assertIsNone(await self.bridge.async_tick_override(self.input))
        self.assertFalse(self.store.writes)

    async def test_restored_current_fault_cannot_be_hidden_by_valid_bridge_marker(self):
        self.owner.regulator.meter_envelope_fault(self.input,logic.ElectricalStopCause.GRID_INVALID)
        prior = deepcopy(self.owner.regulator.export_overload_state())
        await self.restore()
        self.assertFalse(self.bridge._active)
        self.assertTrue(self.owner._protective_zero_pending)
        self.assertEqual(self.store.disk['state'],'acknowledged')
        self.assertFalse(self.store.writes)
        self.assertEqual(self.owner.regulator.export_overload_state(),prior)

    async def test_restored_storage_failure_cannot_claim_valid_marker(self):
        self.owner._budget_error = 'overload_budget_restore_failed'
        await self.restore()
        self.assertFalse(self.bridge._active)
        self.assertFalse(self.store.writes)
        self.assertEqual(self.bridge.diagnostics()['restart_bridge_last_error'],
                         'bridge_restored_operational_fault')

    async def test_diagnostics_never_recurse_into_input_publication(self):
        self.owner._read_input = lambda: (_ for _ in ()).throw(AssertionError('recursive input read'))
        self.assertEqual(self.bridge.diagnostics()['restart_bridge_remaining_seconds'], 0.)

    async def test_clock_rollback_withdraws_without_fabricated_lease(self):
        await self.restore()
        self.input = replace(self.input, wall_now_ts=self.wall-1)
        decision = await self.bridge.async_tick_override(self.input)
        self.assertEqual(decision.meter_zero_cause, 'clock_invalid')
        self.assertIsNone(self.owner.regulator.last_target)

    async def test_monotonic_expiry_cannot_be_extended_by_wall_clock_change(self):
        await self.restore()
        self.advance(281)
        decision = await self.bridge.async_tick_override(self.input)
        self.assertEqual(decision.meter_zero_cause, 'lease_expired')
        self.assertFalse(self.bridge._active)

    async def test_real_high_grid_preempts_bridge_and_no_overload_is_granted(self):
        await self.restore()
        self.input = replace(self.input, grid_l1=29.)
        decision = await self.bridge.async_tick_override(self.input)
        self.assertEqual(decision.meter_zero_cause, 'grid_ceiling')
        self.assertFalse(self.bridge._active)

    async def test_new_actuator_fault_preempts_bridge(self):
        await self.restore()
        self.input = replace(self.input, meter_actuator_health=logic.SourceHealth(False,'offline'))
        decision = await self.bridge.async_tick_override(self.input)
        self.assertEqual(decision.meter_zero_cause, 'actuator_invalid')

    async def test_http_acceptance_alone_cannot_validate_physical_marker(self):
        await self.restore(physical_response_wall=None, grid_response_wall=None)
        self.assertFalse(self.bridge._active)

    async def test_zero_or_negative_ttl_and_bool_cap_are_rejected(self):
        for values in ({'ttl_seconds':0}, {'ttl_seconds':-1}, {'cap':True}, {'cap':16}):
            with self.subTest(values=values):
                self.assertFalse(self.bridge._valid_marker(self.marker(**values),self.wall))

    async def test_extra_private_fields_and_malformed_values_are_rejected(self):
        for values in ({'unexpected':'PRIVATE_SENTINEL'}, {'nonce':'PRIVATE_SENTINEL'},
                       {'expires_wall':float('nan')}, {'prepared_wall':self.wall+1}):
            with self.subTest(values=values):
                self.assertFalse(self.bridge._valid_marker(self.marker(**values),self.wall))

    async def test_late_ack_transaction_is_tombstoned_before_next_controller_reads(self):
        self.store.disk = self.marker(state='prepared', dispatch_wall=None,
            expires_wall=1300., physical_response_wall=None, grid_response_wall=None)
        self.store.write_gate = asyncio.Event()
        gate = self.store.write_gate
        with patch.object(bridge_module,'STORE_TIMEOUT_SECONDS',.02):
            with self.assertRaises(TimeoutError):
                await self.bridge._save(self.marker())
        self.assertTrue(self.bridge._abort)
        # A failed unload can restart the same controller. Its new mutable
        # flags must not revive the previous transaction's one-way grant.
        self.bridge._abort = False
        self.bridge._closed = False
        self.bridge._grant_guard = {'valid': True}
        consumer = Bridge(self.owner, store=self.store)
        loading = asyncio.create_task(consumer.async_restore())
        await asyncio.sleep(0)
        self.assertFalse(loading.done())
        gate.set()
        await loading
        self.assertEqual(self.store.disk['state'],'aborted')
        self.assertFalse(consumer._active)

    async def test_old_queued_completion_cannot_overwrite_new_prepared_nonce(self):
        await self.restore()
        wait = asyncio.Event()
        original = self.bridge._save
        async def delayed(marker, **kwargs):
            await wait.wait()
            return await original(marker, **kwargs)
        self.bridge._save = delayed
        decision = self.bridge._decision(self.input,mode='meter_normal',cap=12,command='set_limit',ttl=60)
        self.bridge.note_submission(decision,True)
        await asyncio.sleep(0)
        next_marker = self.marker(state='prepared',nonce='c'*32,dispatch_wall=None,
            expires_wall=1300.,physical_response_wall=None,grid_response_wall=None)
        next_writer = Bridge(self.owner,store=self.store)
        await next_writer._save(next_marker)
        wait.set()
        await self.bridge._background_save
        self.assertEqual(self.store.disk['nonce'],'c'*32)
        self.assertEqual(self.store.disk['state'],'prepared')

    async def test_ack_cannot_replace_different_prepared_owner(self):
        self.store.disk = self.marker(state='prepared',nonce='c'*32,dispatch_wall=None,
            expires_wall=1300.,physical_response_wall=None,grid_response_wall=None)
        with self.assertRaises(bridge_module.BridgeRefused):
            await self.bridge._save(self.marker())
        self.assertEqual(self.store.disk['nonce'],'c'*32)

    async def test_old_completion_cannot_clear_same_owner_abort(self):
        await self.restore()
        aborted = {**self.store.disk,'state':'aborted','ended_wall':self.wall}
        await self.bridge._save(aborted,expected_nonce='a'*32)
        completed = {**self.store.disk,'state':'completed','ended_wall':self.wall}
        result = await self.bridge._save(completed,expected_nonce='a'*32)
        self.assertIsNone(result)
        self.assertEqual(self.store.disk['state'],'aborted')

    async def test_repeated_shutdown_request_cannot_refill_an_armed_bridge(self):
        self.bridge._armed = True
        self.owner._read_input = lambda: (_ for _ in ()).throw(AssertionError('must not inspect or renew'))
        self.assertFalse(await self.bridge.async_prepare_shutdown())
        self.assertFalse(self.store.writes)

    async def test_unhashable_marker_state_is_rejected(self):
        self.assertFalse(self.bridge._valid_marker(self.marker(state=[]),self.wall))

    async def test_missing_clock_is_protective_not_a_grace_or_exception(self):
        await self.restore()
        self.input = replace(self.input,wall_now_ts=None)
        decision = await self.bridge.async_tick_override(self.input)
        self.assertEqual(decision.meter_zero_cause,'clock_invalid')
        self.assertFalse(self.bridge._active)

    async def test_actual_positive_acceptance_ends_without_resetting_history(self):
        await self.restore()
        prior = deepcopy(self.owner.regulator.export_overload_state())
        decision = self.bridge._decision(self.input,mode='meter_normal',cap=12,command='set_limit',ttl=60)
        self.bridge.note_submission(decision,True)
        await self.bridge._background_save
        self.assertEqual(self.store.disk['state'],'completed')
        self.assertFalse(self.bridge._active)
        self.assertEqual(self.owner.regulator.export_overload_state(),prior)


if __name__ == '__main__':
    unittest.main()
