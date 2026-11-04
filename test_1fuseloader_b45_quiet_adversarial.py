"""Independent normal-continuation oracles with real controller/SDK boundaries.

Only MQTT/native transport, time, HTTP and the vehicle plant are synthetic.
Normal leases and fault recovery are acquired through real dispatch/Store
paths; no new provenance flag is seeded by a test. Application silence must
not create EV proof, restart permission or a fresh electrical deadline.
"""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_b44_quiet_safety as fixture


class B45QuietAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture.B44QuietSafetyTests(methodName='runTest')
        await self.fixture.asyncSetUp()
        self.p = self.fixture.plant

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def reset_plant(self):
        await self.asyncTearDown()
        await self.asyncSetUp()

    async def quiet(self, raw=18.1):
        return await self.fixture.prepare_quiet(normal_raw=raw)

    async def tick(self, at, raw, **kw):
        return await self.fixture.tick(at, raw, **kw)

    def assert_no_positive(self):
        self.fixture.assert_no_positive()

    async def test_normal_total_not_raw_plus_draw_owns_same_cap_continuity(self):
        pulse, native = await self.quiet()
        p = self.p
        r = p.controller.regulator
        for at, raw in ((315.,18.1),(340.,23.),(370.,18.1),(400.,21.9)):
            d = await self.tick(at, raw)
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(r.last_target[0],16)
            self.assertFalse(d.meter_overload_authority)
        self.assertTrue(p.sdk_calls)
        self.assertTrue(all(c['phases']==(16,0,0) and c['ttl']==60 for c in p.sdk_calls))
        self.assertEqual(p.product.state['latestPulse'],pulse)
        current = p.adapter.native_current_observation
        self.assertEqual(current.received_at,native.received_at)
        self.assertEqual(current.source_timestamps,native.source_timestamps)
        self.assertEqual(current.observation_sequences,native.observation_sequences)
        self.assertFalse(p.physical_interruptions)
        p.sdk_charger.pause.assert_not_awaited()
        p.sdk_charger.resume.assert_not_awaited()

    async def test_actual_post_dispatch_fresher_total_above_buffer_requires_strict_decrease(self):
        pulse,native=await self.quiet()
        d = await self.tick(315.,23.001)
        # B46 withdraws one ampere of existing authority. The old same-cap
        # quiet exception does not turn the buffer boundary into a stop.
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[15])
        self.assertTrue(all(row['ttl']==60 for row in self.p.sdk_calls))
        self.assertFalse(d.meter_overload_authority)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertEqual(self.p.product.state['latestPulse'],pulse)
        self.assertEqual(self.p.adapter.native_current_observation.received_at,native.received_at)
        self.p.sdk_charger.resume.assert_not_awaited()

    async def test_earlier_larger_possible_lease_is_not_erased_by_lower_ack(self):
        p = self.p
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        for at in (220.,250.,280.):
            await self.tick(at,5.9)
        lower = await self.tick(309.,24.5)
        self.assertIsNone(lower.meter_zero_cause,lower)
        self.assertEqual(p.controller.regulator.last_target[0],14)
        policy=p.controller.regulator._get_meter_envelope_policy()
        parent={key:value for key,value in policy.possible_positive.items()
                if value[0]>14 and value[1]>p.now}
        self.assertTrue(parent)
        original_expiry=policy.lease.expires
        before=deepcopy(p.controller.regulator.export_overload_state())
        native=deepcopy(p.adapter.native_current_observation)
        p.sdk_calls.clear()
        d = await self.tick(312.,8.)
        # B46 preserves the earlier actual own ACK as possible authority,
        # while a later normal total retains only the actual lower cap.
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(p.controller.regulator.last_target[0],14)
        self.assertEqual(p.sdk_calls,[])
        self.assertEqual(policy.lease.expires,original_expiry)
        for key,value in parent.items():
            if value[1]>p.now:
                self.assertEqual(policy.possible_positive.get(key),value)
            else:
                self.assertNotIn(key,policy.possible_positive)
        self.assertEqual(p.adapter.native_current_observation.received_at,native.received_at)
        self.assertEqual(p.controller.regulator.export_overload_state()['used_seconds'],
                         before['used_seconds'])
        self.assertFalse(d.meter_overload_authority)

    async def test_missing_or_future_grid_clock_does_not_acquire_quiet_permission(self):
        for reported in (100.,316.):
            with self.subTest(reported=reported):
                if reported!=100.: await self.reset_plant()
                await self.quiet()
                d = await self.tick(315.,18.1,reported=reported)
                self.assertEqual(d.meter_zero_cause,'grid_invalid')
                self.assert_no_positive()

    async def test_quiet_product_does_not_turn_transport_flags_into_a_new_clock(self):
        for key in ('signalRConnected','isOnline'):
            with self.subTest(key=key):
                if key!='signalRConnected': await self.reset_plant()
                await self.quiet()
                self.p.product.state[key] = False
                d = await self.tick(315.,18.1)
                self.assertEqual(d.meter_zero_cause,'actuator_invalid')
                self.assert_no_positive()

    async def test_same_live_cap_never_becomes_a_quiet_cold_start(self):
        p = self.p
        await p.packet(99.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.},drain=False)
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        d = await self.tick(225.,3.,status='awaiting_start')
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_same_live_cap_cannot_be_revived_after_original_expiry(self):
        await self.quiet()
        p = self.p
        expiry = p.controller.regulator.last_set_limit_ts + 60.
        d = await self.tick(expiry+.01,18.1)
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_independent_meter_age_boundary_remains_fifteen_seconds(self):
        for report in (325.,324.999):
            with self.subTest(report=report):
                if report!=325.: await self.reset_plant()
                await self.quiet()
                d = await self.tick(340.,18.1,reported=report)
                if report==325.:
                    self.assertIsNone(d.meter_zero_cause,d)
                    self.assertTrue(self.p.sdk_calls)
                    self.assertTrue(all(c['cap']==16 for c in self.p.sdk_calls))
                else:
                    self.assertIsNotNone(d.meter_zero_cause,d)
                    self.assert_no_positive()

    async def normal_recovery_without_vehicle_response(self, reason):
        p = self.p
        await p.packet(99.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.})
        p.grid(3.,99.)
        saved = p.controller.regulator.export_overload_state()
        saved.pop('meter_control',None)
        saved.update(used_seconds=4000.,failure_reason='response_timeout',
            exposure_active=False,transition_pending=False,minimum_held=False)
        saved['automatic_cycle'] = dict(version=1,cycle_id=9,state='blocked',
            used_seconds=35.001,source_id=p.controller._read_input().control_source_id,
            failure_reason=reason)
        await p.controller._budget_store.async_save(saved)
        await self.tick(100.,3.,status='awaiting_start')
        for at in range(105,171,5):
            await p.packet(float(at)-.2,{182:0.,183:0.},source=float(at)-.2)
            await self.tick(float(at),3.,status='awaiting_start')
        self.assertEqual(p.controller.regulator.last_target[0],16)
        before = deepcopy(p.controller.regulator.export_overload_state())
        diag = p.controller.regulator.meter_control_diagnostics()
        self.assertTrue(diag['recovery_required'])
        self.assertEqual(diag['recovery_stage'],'await_normal_response')
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        for at in (200.,230.,260.,290.):
            await self.tick(at,3.,status='awaiting_start')
        p.sdk_calls.clear()
        return before

    async def test_normally_admitted_recovery_cap_can_continue_without_faking_response(self):
        before = await self.normal_recovery_without_vehicle_response('peak_response_timeout')
        p = self.p
        for at in (295.,320.,350.):
            d = await self.tick(at,18.1,status='awaiting_start')
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(p.controller.regulator.last_target[0],16)
            self.assertFalse(d.meter_overload_authority)
        self.assertTrue(p.sdk_calls)
        self.assertTrue(all(c['cap']==16 and c['ttl']==60 for c in p.sdk_calls))
        diag = p.controller.regulator.meter_control_diagnostics()
        self.assertTrue(diag['recovery_required'])
        self.assertEqual(diag['recovery_stage'],'await_normal_response')
        self.assertEqual(diag['operational_fault'],'peak_response_timeout')
        after = p.controller.regulator.export_overload_state()
        self.assertEqual(after['automatic_cycle']['cycle_id'],before['automatic_cycle']['cycle_id'])
        self.assertEqual(after['automatic_cycle']['used_seconds'],before['automatic_cycle']['used_seconds'])
        self.assertEqual(after['used_seconds'],before['used_seconds'])
        self.assertEqual(after['failure_reason'],before['failure_reason'])
        self.assertEqual(after['meter_control']['history'],before['meter_control']['history'])
        p.sdk_charger.resume.assert_not_awaited()

    async def test_old_fault_name_is_not_a_new_active_failure_after_normal_readmission(self):
        for reason in ('unknown_delivery','persistence'):
            with self.subTest(reason=reason):
                if reason!='unknown_delivery': await self.reset_plant()
                before = await self.normal_recovery_without_vehicle_response(reason)
                d = await self.tick(295.,3.,status='awaiting_start')
                self.assertIsNone(d.meter_zero_cause,d)
                self.assertEqual(self.p.controller.regulator.last_target[0],16)
                diag = self.p.controller.regulator.meter_control_diagnostics()
                self.assertTrue(diag['recovery_required'])
                self.assertEqual(diag['operational_fault'],reason)
                self.assertEqual(diag['recovery_stage'],'await_normal_response')
                after = self.p.controller.regulator.export_overload_state()
                self.assertEqual(after['meter_control']['history'],before['meter_control']['history'])
                self.assertEqual(after['automatic_cycle']['used_seconds'],before['automatic_cycle']['used_seconds'])
                self.assertFalse(d.meter_overload_authority)

    async def test_new_actual_sdk_failure_is_not_hidden_by_the_old_normal_lease(self):
        await self.quiet()
        p = self.p
        p.sdk_failure = RuntimeError('Synthetic transport failure after SDK entry')
        await self.tick(340.,18.1)
        self.assertTrue(p.sdk_calls)
        p.sdk_failure = None
        p.sdk_calls.clear()
        for at in (345.,350.,355.):
            await self.tick(at,18.1)
        self.assert_no_positive()
        self.assertTrue(p.controller.regulator.meter_control_diagnostics()['recovery_required'])

    async def test_actual_checkpoint_failure_preempts_a_quiet_normal_renewal(self):
        await self.quiet()
        p = self.p
        # Fail the public checkpoint boundary, rather than injecting a
        # historical fault name or editing normal-admission proof state.
        with patch.object(p.controller._budget_store,'async_save',
                          AsyncMock(side_effect=OSError('Synthetic checkpoint unavailable'))):
            await self.tick(340.,18.1)
        self.assert_no_positive()
        self.assertEqual(p.controller._budget_error,'overload_budget_save_failed')

    async def test_new_high_load_during_recovery_does_not_gain_overload_permission(self):
        before=await self.normal_recovery_without_vehicle_response('peak_response_timeout')
        native=deepcopy(self.p.adapter.native_current_observation)
        d = await self.tick(295.,28.,status='charging')
        # B46 can withdraw5A of an actually readmitted16A offer without
        # granting an overload period or clearing historical recovery.
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[11])
        self.assertFalse(d.meter_overload_authority)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertEqual(self.p.adapter.native_current_observation.received_at,native.received_at)
        after=self.p.controller.regulator.export_overload_state()
        self.assertEqual(after['automatic_cycle']['cycle_id'],before['automatic_cycle']['cycle_id'])
        self.assertGreaterEqual(after['automatic_cycle']['used_seconds'],
                                before['automatic_cycle']['used_seconds'])
        self.assertEqual(after['failure_reason'],before['failure_reason'])
        self.assertEqual(after['meter_control']['history'],before['meter_control']['history'])
        self.p.sdk_charger.resume.assert_not_awaited()

    async def confirmed_minimum_hold(self):
        p = self.p
        r = await p.peak()
        await p.response()
        d = await self.tick(210.694,28.5,reported=210.)
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.meter_control_diagnostics()['episode_phase'],'MINIMUM_HOLD')
        self.assertEqual(r.last_target[0],7)
        return r

    async def test_resolved_minimum_hold_does_not_poison_a_later_actual_normal_seven(self):
        p = self.p
        r = await self.confirmed_minimum_hold()
        for at in (215.,220.,225.,230.,235.):
            await self.tick(at,28.5)
        # The old EV report no longer pairs with the new normal meter. Its
        # resolved episode remains for history, but ordinary measured RAW
        # still leads to an actual normal7A ACK before application silence.
        for at in (240.,245.,250.,255.,260.):
            d = await self.tick(at,21.9)
            self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.meter_control_diagnostics()['episode_phase'],'MINIMUM_HOLD')
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        before = deepcopy(r.export_overload_state())
        for at in (290.,320.,350.,380.):
            d = await self.tick(at,21.9)
            self.assertIsNone(d.meter_zero_cause,d)
        p.sdk_calls.clear()
        for at in (385.,410.):
            d = await self.tick(at,21.9)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertFalse(d.meter_overload_authority)
        self.assertEqual(r.last_target[0],7)
        self.assertTrue(p.sdk_calls)
        self.assertFalse(any(c['cap']!=7 for c in p.sdk_calls))
        after = r.export_overload_state()
        self.assertEqual(after['automatic_cycle']['used_seconds'],before['automatic_cycle']['used_seconds'])
        self.assertEqual(after['used_seconds'],before['used_seconds'])
        self.assertEqual(r.meter_control_diagnostics()['episode_phase'],'MINIMUM_HOLD')
        self.assertFalse(p.physical_interruptions)

    async def test_resolved_hold_without_a_later_normal_ack_cannot_be_relabelled_quiet_normal(self):
        p = self.p
        r = await self.confirmed_minimum_hold()
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        for at in (215.,230.,260.,290.,320.):
            d = await self.tick(at,28.5)
            self.assertIsNone(d.meter_zero_cause,d)
        p.sdk_calls.clear()
        d = await self.tick(329.,21.9)
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_unresolved_peak_does_not_acquire_the_resolved_hold_exception(self):
        p = self.p
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect = self.fixture.quiet_sdk
        for at in (220.,250.,280.):
            await self.tick(at,18.1)
        peak = await self.tick(309.,31.)
        self.assertIsNone(peak.meter_zero_cause,peak)
        deadline = peak.nominal_response_deadline_ts
        p.sdk_calls.clear()
        d = await self.tick(312.,3.)
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()
        self.assertEqual(p.controller.regulator.meter_control_diagnostics()['response_deadline'],deadline)

    async def queued_change(self, kind):
        await self.quiet()
        p = self.p
        await p.adapter._request_lock.acquire()
        task = asyncio.create_task(self.tick(340.,18.1))
        try:
            for _ in range(60):
                await asyncio.sleep(0)
                if p.controller._dispatch_task is not None: break
            self.assertIsNotNone(p.controller._dispatch_task)
            self.assertEqual(p.controller._dispatch_decision.current_p1,16)
            self.assertFalse(p.sdk_calls)
            p.now = 339. if kind=='monotonic' else 371. if kind=='lease_expired' else 341.
            raw = 23.001 if kind=='raw' else 18.1
            report = 335. if kind=='grid_regressed' else 325. if kind=='stale' else p.now
            p.grid(raw,report)
            if kind=='transport': p.product.state['signalRConnected']=False
            if kind=='future_pulse': p.product.state['latestPulse']=p.wall(400.)
            if kind=='source': p.controller._configured_control_identity=Mock(return_value='synthetic-b45-other-source')
            if kind=='pause': p.states['sensor.status'].state='paused'
            if kind=='max':
                p.entry.options['max_charge_amps']=12
                p.controller._reload_settings_locked()
            if kind=='inactive':
                p.entry.options['active_control']=False
                p.controller._reload_settings_locked()
            if kind=='dry_run':
                p.entry.options['dry_run']=True
                p.controller._reload_settings_locked()
        finally:
            p.adapter._request_lock.release()
        await task
        self.assert_no_positive()
        self.assertNotEqual(p.controller.regulator.meter_control_diagnostics()['operational_fault'],
                            'unknown_delivery')

    async def test_queue_cannot_spend_old_normal_total_permission(self):
        for kind in ('raw','grid_regressed','monotonic','stale','transport',
                     'future_pulse','source','pause','max','lease_expired',
                     'inactive','dry_run'):
            with self.subTest(kind=kind):
                if kind!='raw': await self.reset_plant()
                await self.queued_change(kind)


if __name__=='__main__':
    unittest.main()
