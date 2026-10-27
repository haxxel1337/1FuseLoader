"""Late phase provenance through actual native callbacks, Store and SDK.

These are synthetic offline operating oracles. No private floor, accepted
lease or recovery evidence is injected. Physical proof comes from actual
fixture SDK entry/ACK and later native183/meter events.
"""
from copy import deepcopy
import unittest

import test_1fuseloader_b42_controller as previous


class B43NormalPhaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    _fixture = previous.B42StandaloneControllerTests
    _base = _fixture._base
    asyncSetUp = _fixture.asyncSetUp
    asyncTearDown = _fixture.asyncTearDown
    update_entry = _fixture.update_entry
    wall = _fixture.wall
    schedule = _fixture.schedule
    active_timers = _fixture.active_timers
    grid = _fixture.grid
    snapshot = _fixture.snapshot
    set_native = _fixture.set_native
    configure = _fixture.configure
    execute = _fixture.execute
    project_sdk = _fixture.project_sdk
    packet = _fixture.packet
    pump = _fixture.pump
    tick = _fixture.tick
    sdk_set = _fixture.sdk_set
    boot = _fixture.boot
    peak = _fixture.peak
    response = _fixture.response
    fault_zero = _fixture.fault_zero

    async def recovery_start(self, *, phase=0, until=165, prior_used=None):
        await self.packet(98., {21:1, 38:2, 110:phase, 109:2,
            182:0., 183:0., 184:0., 185:0.}, source=97.)
        if prior_used is not None:
            saved = self.controller.regulator.export_overload_state()
            saved.pop('meter_control', None)
            saved.update(used_seconds=4000., failure_reason='response_timeout',
                exposure_active=False, transition_pending=False, minimum_held=False)
            saved['automatic_cycle'] = dict(version=1, cycle_id=9, state='blocked',
                used_seconds=prior_used, source_id=self.controller._read_input().control_source_id,
                failure_reason='response_timeout')
            await self.controller._budget_store.async_save(saved)
        self.product.state['signalRConnected'] = False
        await self.tick(99., 3., status='awaiting_start')
        self.assertEqual(self.controller.last_decision.meter_zero_cause, 'actuator_invalid')
        self.product.state['signalRConnected'] = True
        for at in range(100, until + 1, 5):
            await self.tick(float(at), 3., status='awaiting_start')
        r = self.controller.regulator
        if until >= 160:
            self.assertEqual(r.last_target[0], 16)
            self.assertTrue(any(row['cap']==16 and row['at']==160. for row in self.sdk_calls))
            self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        return r, deepcopy(r.export_overload_state())

    def assert_ledger_preserved(self, saved):
        r = self.controller.regulator
        current = r.export_overload_state()
        self.assertEqual(current['automatic_cycle']['cycle_id'], saved['automatic_cycle']['cycle_id'])
        self.assertEqual(current['automatic_cycle']['used_seconds'], saved['automatic_cycle']['used_seconds'])
        self.assertEqual(current['used_seconds'], saved['used_seconds'])
        self.assertEqual(current['failure_reason'], saved['failure_reason'])
        self.assertEqual(current['automatic_cycle']['failure_reason'], saved['automatic_cycle']['failure_reason'])
        self.assertGreaterEqual(len(current['meter_control']['history']), len(saved['meter_control']['history']))
        return current

    def assert_no_extra_zero_or_dither(self):
        writes = [row for row in self.sdk_calls if row['at'] >= 100.]
        self.assertTrue(writes)
        self.assertTrue(all(row['cap']==16 and row['ttl']==60 and row['phases']==(16,0,0)
                            for row in writes), writes)
        self.sdk_charger.resume.assert_not_awaited()

    async def test_missing_normal_floor_binds_on_later_actual_renewal_then_new_response(self):
        for index, current in enumerate((7., 16.1)):
            if index:
                await self.asyncTearDown()
                await self.asyncSetUp()
            with self.subTest(actual_current=current):
                r, saved = await self.recovery_start(prior_used=35.001 if index==0 else None)
                await self.packet(170., {110:10, 109:3, 182:current+.1, 183:current}, source=168.)
                await self.tick(175., current+3., status='charging')
                self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
                for at in (180., 185., 190.):
                    await self.packet(at-.2, {182:current+.1, 183:current}, source=at-.4)
                    await self.tick(at, current+3., status='charging')
                self.assertTrue(any(row['cap']==16 and row['at']==190. for row in self.sdk_calls))
                # Mapping adoption, cached reports and the new SDK ACK alone
                # cannot supply a physical response after that new boundary.
                self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
                self.assertIsNotNone(r.meter_control_diagnostics()['operational_fault'])
                await self.packet(194.8, {182:current+.1, 183:current}, source=194.6)
                await self.tick(195., current+3., status='charging')
                self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
                self.assertIsNone(r.meter_control_diagnostics()['operational_fault'])
                for at in range(200, 311, 5):
                    await self.packet(float(at)-.2, {182:current+.1, 183:current}, source=float(at)-.4)
                    await self.tick(float(at), current+3., status='charging')
                    self.assertIsNone(self.controller.last_decision.meter_zero_cause)
                    self.assertEqual(r.last_target[0], 16)
                final = self.assert_ledger_preserved(saved)
                self.assertTrue(any(event['resolved'] for event in final['meter_control']['history']))
                self.assert_no_extra_zero_or_dither()

    async def test_changed_mapping_epoch_rebinds_same_cap_without_losing_normal_decrease(self):
        r, saved = await self.recovery_start(phase=10)
        await self.packet(170., {110:0, 182:16.2, 183:16.1}, source=169.)
        await self.tick(171., 3., status='awaiting_start')
        await self.packet(175., {110:10, 109:3, 182:16.2, 183:16.1}, source=174.)
        await self.tick(180., 19.1, status='charging')
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        await self.packet(189.8, {182:16.2, 183:16.1}, source=189.6)
        await self.tick(190., 19.1, status='charging')
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        await self.packet(194.8, {182:16.2, 183:16.1}, source=194.6)
        await self.tick(195., 19.1, status='charging')
        self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
        self.assert_ledger_preserved(saved)
        self.assert_no_extra_zero_or_dither()
        # The accepted normal floor must also follow the actual new scope.
        # A valid subsequent normal12 decrease must not become an extra7 offer.
        await self.packet(199.8, {182:16.2, 183:16.1}, source=199.6)
        d = await self.tick(200., 26.2, status='charging')
        self.assertIsNone(d.meter_zero_cause)
        self.assertFalse(d.meter_overload_authority)
        self.assertEqual(r.last_target[0], 12)
        self.assertEqual(self.sdk_calls[-1]['cap'], 12)

    async def test_valid_same_epoch_renewal_does_not_repin_and_starve_real_response(self):
        r, saved = await self.recovery_start(phase=10)
        await self.tick(180., 3., status='awaiting_start')
        await self.packet(185., {109:3, 182:16.2, 183:16.1}, source=184.8)
        # This meter packet precedes the phase response. Its cached value is
        # not coherent completion, but the normal16 lease may renew at190.
        await self.tick(190., 3., reported=180., status='charging')
        self.assertTrue(any(row['cap']==16 and row['at']==190. for row in self.sdk_calls))
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        # No NEW phase report after the renewal. The real report at185 follows
        # the original valid SDK floor160 and the later RAW now confirms it.
        await self.tick(195., 19.1, status='charging')
        self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
        self.assert_ledger_preserved(saved)
        self.assert_no_extra_zero_or_dither()
        d = await self.tick(200., 26.2, status='charging')
        self.assertIsNone(d.meter_zero_cause)
        self.assertFalse(d.meter_overload_authority)
        self.assertEqual(r.last_target[0], 12)

    async def test_mapping_arriving_during_http_cannot_retroactively_bind_sdk_floor(self):
        r, saved = await self.recovery_start(until=155)
        installed = False
        async def during_sdk(row):
            nonlocal installed
            if row['cap']==16 and not installed:
                installed = True
                await self.packet(row['at']+.05, {110:10, 109:3, 182:7.1, 183:7.}, source=159.9)
        self.sdk_hook = during_sdk
        await self.tick(160., 3., status='awaiting_start')
        self.sdk_hook = None
        await self.tick(165., 10., status='charging')
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        self.assertTrue(any(row['cap']==16 and row['at']==160. for row in self.sdk_calls))
        # The mapping arrived AFTER actual SDK entry. Neither acceptance nor
        # an unknown outcome may retrospectively attach that newer context to
        # the SDK's missing floor. Read these fields; never seed proof state.
        policy = r._get_meter_envelope_policy()
        self.assertIsNone(policy.lease.accepted_floor)
        self.assertIsNone(policy.recovery.normal_floor)
        self.assertIsNotNone(r.meter_control_diagnostics()['operational_fault'])
        self.assert_ledger_preserved(saved)
        self.sdk_charger.resume.assert_not_awaited()

    # Independent existing electrical/lease oracles remain unchanged.
    test_peak_and_grid_deadlines_remain_fixed = _fixture.test_genuine_peak_and_minimum_have_one_fixed_grid_deadline
    test_hard_limit_and_original_lease_expiry_remain_typed = _fixture.test_hard_peak_and_expired_real_lease_remain_typed_stops
    test_get_cannot_supply_new_phase_response = _fixture.test_cached_get_cannot_supply_new_l1_response
    test_phase_change_keeps_original_electrical_deadline = _fixture.test_mapping_switch_withdraws_response_credit
    test_native_mapping_during_peak_http_cannot_create_credit = _base.test_phase_switch_during_sdk_does_not_manufacture_a_later_mapping_floor


if __name__ == '__main__':
    unittest.main()
