"""Independent operating-feedback checks across real controller/SDK boundaries.

The plant supplies synthetic grid, native receipt, transport and HTTP inputs.
It does not seed normal-admission or response proof. A lower offer is an
actuator request, not proof that the car has physically reduced its current.
Historical storage is a fixture input; no installation history appears here.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_b44_quiet_safety as actual


class B46OperatingFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = actual.B44QuietSafetyTests(methodName='runTest')
        await self.fixture.asyncSetUp()
        self.p = self.fixture.plant

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def reset_plant(self):
        await self.asyncTearDown()
        await self.asyncSetUp()

    async def stale_normal_offer(self):
        r = await self.p.boot()
        await self.p.packet(199.8, {182:16., 183:16.}, source=199.6,
                            drain=False)
        await self.p.tick(200., 21.2, status='charging')
        await self.p.tick(220., 21.2)
        self.assertEqual(r.last_target[0],16)
        self.assertTrue(r._get_meter_envelope_policy().lease.normal_admitted)
        self.p.sdk_calls.clear()
        return r

    async def quiet_normal_offer(self):
        return await self.fixture.prepare_quiet(normal_raw=18.1)

    def assert_no_session_commands(self):
        self.p.sdk_charger.pause.assert_not_awaited()
        self.p.sdk_charger.resume.assert_not_awaited()

    def assert_no_positive(self):
        self.assertFalse(any(row['cap']>0 for row in self.p.sdk_calls),
                         self.p.sdk_calls)
        self.assert_no_session_commands()

    async def test_buffer_excess_with_fifty_second_old_ev_requests_thirteen_not_zero(self):
        r = await self.stale_normal_offer()
        native = deepcopy(self.p.adapter.native_current_observation)
        before = deepcopy(r.export_overload_state())
        d = await self.p.tick(250.,25.4,status='charging')
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[13])
        self.assertFalse(d.meter_overload_authority)
        self.assertIsNone(d.nominal_response_deadline_ts,
                          'A sub-28A operating correction is not a peak episode')
        self.assertEqual(self.p.adapter.native_current_observation.received_at,
                         native.received_at)
        self.assertEqual(self.p.adapter.native_current_observation.observation_sequences,
                         native.observation_sequences)
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],
                         before['automatic_cycle']['cycle_id'])
        self.assertFalse(self.p.physical_interruptions)
        self.assert_no_session_commands()

    async def test_pure_quiet_buffer_crossing_uses_strictly_lower_finite_offer(self):
        for raw, expected in ((23.1,15),(24.9,14)):
            with self.subTest(raw=raw):
                if raw!=23.1:
                    await self.reset_plant()
                pulse, native = await self.quiet_normal_offer()
                r = self.p.controller.regulator
                d = await self.fixture.tick(315.,raw,status='charging')
                self.assertIsNone(d.meter_zero_cause,d)
                self.assertEqual(r.last_target[0],expected)
                self.assertEqual([row['cap'] for row in self.p.sdk_calls],[expected])
                self.assertTrue(all(row['phases']==(expected,0,0) and row['ttl']==60
                                    for row in self.p.sdk_calls))
                self.assertFalse(d.meter_overload_authority)
                self.assertEqual(self.p.product.state['latestPulse'],pulse)
                observed = self.p.adapter.native_current_observation
                self.assertEqual(observed.received_at,native.received_at)
                self.assertEqual(observed.observation_sequences,native.observation_sequences)
                self.assertFalse(self.p.physical_interruptions)
                self.assert_no_session_commands()

    async def test_same_meter_packet_cannot_shed_twice_or_renew_its_hold(self):
        await self.quiet_normal_offer()
        p = self.p
        r = p.controller.regulator
        first = await self.fixture.tick(315.,23.1,status='charging')
        self.assertIsNone(first.meter_zero_cause,first)
        self.assertEqual(r.last_target[0],15)
        origin = r.last_set_limit_ts
        expiry = r._get_meter_envelope_policy().lease.expires
        for at in (316.,320.,325.):
            d = await self.fixture.tick(at,23.1,reported=315.)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],15)
            self.assertEqual(r.last_set_limit_ts,origin)
            self.assertEqual(r._get_meter_envelope_policy().lease.expires,expiry)
            self.assertEqual([row['cap'] for row in p.sdk_calls],[15])
        # The newer normal sample permits retention; it supplies no EV credit
        # to raise the cap. The actual renewal still waits until genuinely due.
        d = await self.fixture.tick(327.,22.)
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.last_target[0],15)
        self.assertEqual(len(p.sdk_calls),1)
        d = await self.fixture.tick(346.,22.)
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual([row['cap'] for row in p.sdk_calls],[15,15])
        self.assertGreater(r.last_set_limit_ts,origin)
        self.assert_no_session_commands()

    async def restore_historical_recovery(self, *, pre_ack_current=False):
        p = self.p
        await p.packet(99.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.})
        p.grid(3.,99.)
        saved = p.controller.regulator.export_overload_state()
        saved.pop('meter_control',None)
        saved.update(used_seconds=4000.,failure_reason='response_timeout',
            exposure_active=False,transition_pending=False,minimum_held=False)
        saved['automatic_cycle'] = dict(version=1,cycle_id=9,state='blocked',
            used_seconds=35.,source_id=p.controller._read_input().control_source_id,
            failure_reason='lease_expired')
        await p.controller._budget_store.async_save(saved)
        await p.tick(100.,3.,status='awaiting_start')
        for at in range(105,171,5):
            if not pre_ack_current or at<=160:
                value=16. if pre_ack_current and at==160 else 0.
                await p.packet(float(at)-.2,{182:value,183:value},source=float(at)-.2)
            await p.tick(float(at),3.,status='awaiting_start')
        self.assertEqual(p.controller.regulator.last_target[0],16)
        return deepcopy(saved)

    async def test_historical_lease_recovery_and_stale_ev_do_not_turn_small_excess_into_zero(self):
        await self.restore_historical_recovery(pre_ack_current=True)
        p=self.p
        r=p.controller.regulator
        p.physical_draw=16.
        await p.tick(180.,21.2,status='charging')
        await p.tick(190.,21.2,status='charging')
        before=r.meter_control_diagnostics()
        self.assertTrue(before['recovery_required'])
        self.assertEqual(before['operational_fault'],'lease_expired')
        p.sdk_calls.clear()
        d=await p.tick(210.,25.4,status='charging')
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual([row['cap'] for row in p.sdk_calls],[13])
        self.assertFalse(d.meter_overload_authority)
        self.assertFalse(p.physical_interruptions)
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(r.export_overload_state()['automatic_cycle']['used_seconds'],35.)

    async def test_normal_meter_stability_retires_operational_recovery_without_new_ev(self):
        original = await self.restore_historical_recovery()
        p = self.p
        r = p.controller.regulator
        native = deepcopy(p.adapter.native_current_observation)
        p.physical_draw = 16.
        # The car started but its event sensor remains the older zero. These
        # advancing totals contain the draw; no old EV value is subtracted.
        for at in range(175,246,5):
            d = await p.tick(float(at),18.1,status='charging')
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],16)
            self.assertFalse(d.meter_overload_authority)
        diag = r.meter_control_diagnostics()
        self.assertFalse(diag['recovery_required'],diag)
        self.assertIsNone(diag['operational_fault'],diag)
        after = r.export_overload_state()
        self.assertEqual(after['automatic_cycle']['cycle_id'],9)
        self.assertEqual(after['automatic_cycle']['used_seconds'],35.)
        self.assertEqual(after['used_seconds'],original['used_seconds'])
        self.assertEqual(after['failure_reason'],'response_timeout')
        self.assertTrue(any(event['reason']=='lease_expired'
                            for event in after['meter_control']['history']))
        self.assertEqual(p.adapter.native_current_observation.received_at,native.received_at)
        self.assertEqual(p.adapter.native_current_observation.observation_sequences,
                         native.observation_sequences)
        self.assertFalse(p.physical_interruptions)
        self.assert_no_session_commands()

    async def test_buffer_crossing_does_not_destroy_an_actual_normal_admission(self):
        await self.restore_historical_recovery()
        p = self.p
        r = p.controller.regulator
        await p.packet(199.8,{182:16.,183:16.},source=199.6,drain=False)
        await p.tick(200.,21.2,status='charging')
        await p.tick(220.,21.2)
        p.sdk_calls.clear()
        d = await p.tick(250.,25.4,status='charging')
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual([row['cap'] for row in p.sdk_calls],[13])
        self.assertFalse(d.meter_overload_authority)
        state = r.export_overload_state()
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],35.)
        self.assertTrue(any(event['reason']=='lease_expired'
                            for event in state['meter_control']['history']))

    async def test_successive_feedback_commands_keep_known_ancestors_on_original_horizons(self):
        r=await self.stale_normal_offer()
        policy=r._get_meter_envelope_policy()
        earlier=deepcopy(policy.possible_positive)
        for at,expected in ((250.,13),(255.,10),(260.,7)):
            d=await self.p.tick(at,25.4,status='charging')
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],expected)
            self.assertFalse(d.meter_overload_authority)
            for key,value in earlier.items():
                if value[1]>self.p.now:
                    self.assertEqual(policy.possible_positive.get(key),value,
                        'A later ACK must not erase or extend older possible authority')
            earlier=deepcopy(policy.possible_positive)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[13,10,7])
        d=await self.p.tick(265.,25.4,status='charging')
        if r.last_target[0]>0:
            self.assertLessEqual(r.last_target[0],7)
            self.assertTrue(d.meter_overload_authority,
                            'Above nominal at minimum needs actual extra permission')
        else:
            self.assertIsNotNone(d.meter_zero_cause)
        self.assert_no_session_commands()

    async def test_quiet_minimum_at_twenty_four_is_nominal_not_new_overload(self):
        p=self.p
        r=await p.boot()
        await p.packet(199.8,{182:7.,183:7.},source=199.6,drain=False)
        await p.tick(200.,23.5,status='charging')
        self.assertEqual(r.last_target[0],7)
        p.sdk_circuit.set_dynamic_current.side_effect=self.fixture.quiet_sdk
        for at in (230.,260.,290.,320.):
            await self.fixture.tick(at,18.,status='charging')
        pulse=p.product.state['latestPulse']
        native=deepcopy(p.adapter.native_current_observation)
        p.sdk_calls.clear()
        for at,raw in ((325.,24.),(350.,24.),(380.,24.),(410.,25.)):
            d=await self.fixture.tick(at,raw,status='charging')
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],7)
            self.assertFalse(d.meter_overload_authority)
        self.assertTrue(p.sdk_calls)
        self.assertTrue(all(row['cap']==7 and row['ttl']==60 for row in p.sdk_calls))
        self.assertEqual(p.product.state['latestPulse'],pulse)
        self.assertEqual(p.adapter.native_current_observation.received_at,native.received_at)
        self.assertEqual(r.export_overload_state()['automatic_cycle']['used_seconds'],0.)
        cycle_id=r.export_overload_state()['automatic_cycle']['cycle_id']
        beyond=await self.fixture.tick(415.,25.01,status='charging')
        if r.last_target[0]>0:
            self.assertEqual(r.last_target[0],7)
            self.assertTrue(beyond.meter_overload_authority,
                            '25.01A is not nominal minimum permission')
        else:
            self.assertIsNotNone(beyond.meter_zero_cause)
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],cycle_id)
        self.assert_no_session_commands()

    async def test_known_larger_lease_near_expiry_cannot_block_same_lower_renewal(self):
        for short_gap in (False,True):
            with self.subTest(ack_origins_less_than_one_second=short_gap):
                if short_gap:
                    await self.reset_plant()
                p=self.p
                if short_gap:
                    await self.quiet_normal_offer()
                    lower_at,renew_at=310.5,340.5
                else:
                    await p.boot()
                    p.sdk_circuit.set_dynamic_current.side_effect=self.fixture.quiet_sdk
                    for at in (220.,250.,280.):
                        await self.fixture.tick(at,5.9)
                    lower_at,renew_at=309.,339.
                r=p.controller.regulator
                old_origin=r.last_set_limit_ts
                first=await self.fixture.tick(lower_at,24.9,status='charging')
                self.assertIsNone(first.meter_zero_cause,first)
                self.assertEqual(r.last_target[0],14)
                if short_gap:
                    self.assertLess(r.last_set_limit_ts-old_origin,1.)
                policy=r._get_meter_envelope_policy()
                parents={key:value for key,value in policy.possible_positive.items()
                         if value[0]>14 and value[1]>p.now}
                self.assertTrue(parents)
                native=deepcopy(p.adapter.native_current_observation)
                p.sdk_calls.clear()
                for at in range(int(lower_at)+6,int(renew_at),5):
                    await self.fixture.tick(float(at),8.,status='charging')
                renewed=await self.fixture.tick(renew_at,8.,status='charging')
                self.assertIsNone(renewed.meter_zero_cause,renewed)
                self.assertTrue(p.sdk_calls)
                self.assertTrue(all(row['cap']==14 and row['ttl']==60 for row in p.sdk_calls))
                self.assertEqual(r.last_set_limit_ts,renew_at)
                self.assertTrue(any(value[1]>renew_at for value in parents.values()))
                for key,value in parents.items():
                    if value[1]>p.now:
                        self.assertEqual(policy.possible_positive.get(key),value)
                self.assertEqual(p.adapter.native_current_observation.received_at,native.received_at)
                self.assertFalse(renewed.meter_overload_authority)
                self.assert_no_session_commands()

    async def test_above_nominal_feedback_lease_requires_full_ninety_second_reserve(self):
        for remaining in (89.,90.):
            with self.subTest(remaining=remaining):
                if remaining!=89.:
                    await self.reset_plant()
                r=await self.stale_normal_offer()
                cycle=r._ensure_automatic_cycle()
                cycle.update(state='active',used_seconds=1800.-remaining,failure_reason=None)
                cycle_id=cycle['cycle_id']
                before=r.export_overload_state()['used_seconds']
                await self.p.tick(250.,25.4,status='charging')
                positive=[row for row in self.p.sdk_calls if row['cap']>0]
                if remaining==89.:
                    self.assertEqual(positive,[])
                    self.assertEqual(self.p.sdk_calls[-1]['cap'],0)
                else:
                    self.assertTrue(positive)
                    self.assertEqual(positive[0]['cap'],13)
                    self.assertEqual(positive[0]['ttl'],60)
                after=r.export_overload_state()
                self.assertEqual(after['automatic_cycle']['cycle_id'],cycle_id)
                self.assertGreaterEqual(after['automatic_cycle']['used_seconds'],1800.-remaining)
                self.assertGreaterEqual(after['used_seconds'],before)

    async def test_pending_checkpoint_keeps_only_old_finite_offer_then_sends_lower(self):
        r=await self.stale_normal_offer()
        p=self.p
        writer=p.controller._budget_store.store
        save,gate,first=writer.async_save,asyncio.Event(),True
        async def gated(payload):
            nonlocal first
            if first:
                first=False
                await gate.wait()
            return await save(payload)
        writer.async_save=gated
        origin=r.last_set_limit_ts
        expiry=r._get_meter_envelope_policy().lease.expires
        try:
            with patch.object(actual.actual.fixtures.runtime,'EXPOSURE_STORE_TIMEOUT_SECONDS',.03):
                first_decision=await p.tick(250.,25.4,status='charging')
                self.assertTrue(p.controller._budget_store.verification_pending)
                self.assertIsNone(first_decision.meter_zero_cause,first_decision)
                self.assertEqual(r.last_target[0],16)
                self.assertEqual(p.sdk_calls,[])
                self.assertEqual(r.last_set_limit_ts,origin)
                self.assertEqual(r._get_meter_envelope_policy().lease.expires,expiry)
                later=await p.tick(255.,25.4,status='charging')
                self.assertIsNone(later.meter_zero_cause,later)
                self.assertEqual(r.last_target[0],16)
                self.assertEqual(p.sdk_calls,[])
                self.assertEqual(r._get_meter_envelope_policy().lease.expires,expiry)
        finally:
            gate.set()
            await p.controller._budget_store.async_wait_pending()
        await p.pump()
        if not p.sdk_calls:
            await p.tick(260.,25.4,status='charging')
        lower=p.controller.last_decision
        self.assertIsNone(lower.meter_zero_cause,lower)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual([row['cap'] for row in p.sdk_calls],[13])
        self.assertFalse(lower.meter_overload_authority)
        self.assert_no_session_commands()

    async def test_old_normal_owner_clock_does_not_slide_later_recovery_acceptance(self):
        original=await self.restore_historical_recovery()
        p=self.p
        r=p.controller.regulator
        policy=r._get_meter_envelope_policy()
        # An older original operating owner is not new authority or a new
        # ACK. Preserve that older clock across actual same-cap renewals.
        policy.lease.normal_origin=99.
        initial_native=deepcopy(p.adapter.native_current_observation)
        for at in range(175,246,5):
            d=await p.tick(float(at),18.1,status='charging')
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],16)
        self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(policy.lease.normal_origin,99.)
        self.assertEqual(p.adapter.native_current_observation.received_at,initial_native.received_at)
        state=r.export_overload_state()
        self.assertEqual(state['automatic_cycle']['used_seconds'],35.)
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertEqual(state['used_seconds'],original['used_seconds'])

    async def test_high_meter_frame_breaks_continuous_normal_recovery(self):
        await self.restore_historical_recovery()
        p=self.p
        r=p.controller.regulator
        for at in (175.,180.,185.):
            await p.tick(at,18.1,status='charging')
        await p.tick(190.,23.1,status='charging')
        for at in range(195,241,5):
            d=await p.tick(float(at),18.1,status='charging')
            self.assertIsNone(d.meter_zero_cause,d)
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'],
                        'Separate low intervals must not complete continuous recovery')

    async def test_latest_meter_change_before_sdk_defers_without_unknown_delivery(self):
        await self.quiet_normal_offer()
        p=self.p
        await p.adapter._request_lock.acquire()
        task=asyncio.create_task(self.fixture.tick(315.,23.1,status='charging'))
        try:
            for _ in range(80):
                await asyncio.sleep(0)
                if p.controller._dispatch_task is not None:
                    break
            self.assertIsNotNone(p.controller._dispatch_task)
            self.assertEqual(p.controller._dispatch_decision.current_p1,15)
            self.assertFalse(p.sdk_calls)
            p.now=316.
            p.grid(24.9,316.)
        finally:
            p.adapter._request_lock.release()
        await task
        await p.pump()
        self.assertFalse(any(row['cap']==0 for row in p.sdk_calls),p.sdk_calls)
        self.assertFalse(any(row['cap']==15 for row in p.sdk_calls),p.sdk_calls)
        self.assertTrue(all(row['cap']<=14 for row in p.sdk_calls))
        self.assertNotEqual(p.controller.regulator.meter_control_diagnostics()['operational_fault'],
                            'unknown_delivery')
        self.assert_no_session_commands()

    async def test_meter_movement_during_sdk_keeps_actual_lower_acceptance(self):
        await self.quiet_normal_offer()
        p=self.p
        moved=False
        async def move_meter(row):
            nonlocal moved
            if not moved:
                moved=True
                p.now+=.01
                p.grid(23.5,p.now)
        p.sdk_hook=move_meter
        d=await self.fixture.tick(315.,23.1,status='charging')
        self.assertTrue(moved)
        self.assertTrue(any(row['cap']==15 for row in p.sdk_calls))
        self.assertFalse(any(row['cap']==0 for row in p.sdk_calls),p.sdk_calls)
        self.assertTrue(all(0<row['cap']<=15 for row in p.sdk_calls))
        self.assertNotEqual(p.controller.regulator.meter_control_diagnostics()['operational_fault'],
                            'unknown_delivery')
        self.assertIsNone(d.meter_zero_cause,d)
        self.assert_no_session_commands()

    async def test_hard_peak_during_sdk_still_preempts_operating_curtailment(self):
        await self.quiet_normal_offer()
        p=self.p
        moved=False
        async def hard_peak(row):
            nonlocal moved
            if not moved:
                moved=True
                p.now+=.01
                p.grid(38.1,p.now)
        p.sdk_hook=hard_peak
        await self.fixture.tick(315.,23.1,status='charging')
        await p.pump()
        self.assertTrue(moved)
        self.assertEqual(p.sdk_calls[-1]['cap'],0)
        self.assertEqual(p.controller.regulator.meter_control_diagnostics()['operational_fault'],
                         'hard_limit')

    async def test_feedback_flag_without_dispatch_revalidation_cannot_reach_sdk(self):
        await self.stale_normal_offer()
        p=self.p
        p.now=250.
        p.product.state['latestPulse']=p.wall()
        proposal=SimpleNamespace(command='set_limit',mode='meter_normal_decrease',
            current_p1=13,current_p2=0,current_p3=0,committed_current_p1=16,
            ttl_seconds=60,grid_l1=25.4,meter_envelope=True,
            meter_feedback_curtailment=True,meter_feedback_budget_amps=23.,
            meter_ordinary_decrease=True,meter_overload_authority=False,
            meter_quiet_normal_retention=False,command_urgency='normal')
        with self.assertRaises(p.adapter_module.CommandNotSent):
            await p.adapter.async_execute(proposal)
        self.assertFalse(p.sdk_calls)
        # A callable alone cannot turn a nondecrease into that exception.
        proposal.current_p1=16
        with self.assertRaises(p.adapter_module.CommandNotSent):
            await p.adapter.async_execute(proposal,on_dispatch=lambda:True)
        self.assertFalse(p.sdk_calls)

    async def test_real_grid_failure_still_stops_instead_of_claiming_feedback(self):
        await self.stale_normal_offer()
        p = self.p
        p.now=250.
        p.states['sensor.grid'].state='unavailable'
        p.states['sensor.grid'].last_reported=p.wall()
        await p.controller.async_tick()
        await p.pump()
        self.assertEqual(p.controller.last_decision.meter_zero_cause,'grid_invalid')
        self.assertEqual(p.sdk_calls[-1]['cap'],0)
        self.assert_no_positive()

    async def test_rebinding_cannot_reuse_the_operating_cap(self):
        await self.quiet_normal_offer()
        p = self.p
        p.controller._configured_control_identity=Mock(return_value='synthetic-b46-rebound-source')
        d = await self.fixture.tick(315.,24.9,status='charging')
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_quiet_transport_loss_still_preempts_positive_curtailment(self):
        for key in ('isOnline','signalRConnected'):
            with self.subTest(key=key):
                if key!='isOnline':
                    await self.reset_plant()
                await self.quiet_normal_offer()
                self.p.product.state[key]=False
                d = await self.fixture.tick(315.,24.9,status='charging')
                self.assertEqual(d.meter_zero_cause,'actuator_invalid')
                self.assert_no_positive()

    async def test_unknown_sdk_outcome_does_not_become_a_confirmed_lower_offer(self):
        await self.stale_normal_offer()
        p = self.p
        p.sdk_failure=RuntimeError('Synthetic transport failure after entry')
        await p.tick(250.,25.4,status='charging')
        self.assertEqual(p.sdk_calls[0]['cap'],13)
        p.sdk_failure=None
        p.sdk_calls.clear()
        await p.tick(255.,20.,status='charging')
        self.assert_no_positive()
        self.assertTrue(p.controller.regulator.meter_control_diagnostics()['recovery_required'])

    async def test_failed_checkpoint_cannot_admit_a_feedback_decrease(self):
        await self.stale_normal_offer()
        p = self.p
        with patch.object(p.controller._budget_store,'async_save',
                          AsyncMock(side_effect=OSError('Synthetic checkpoint unavailable'))):
            await p.tick(250.,25.4,status='charging')
        self.assert_no_positive()
        self.assertEqual(p.controller._budget_error,'overload_budget_save_failed')
        self.assertEqual(p.controller.regulator.last_target[0],0)

    async def test_above_twenty_eight_keeps_original_ten_second_peak_deadline(self):
        r = await self.stale_normal_offer()
        d = await self.p.tick(250.,31.,reported=250.,status='charging')
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(r.last_target[0],7)
        self.assertEqual(d.nominal_response_deadline_ts,260.)
        stopped = await self.p.tick(261.,31.,reported=261.,status='charging')
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')
        self.assertEqual(self.p.sdk_calls[-1]['cap'],0)

    async def test_above_hard_limit_still_requests_zero_immediately(self):
        await self.stale_normal_offer()
        d = await self.p.tick(250.,38.01,status='charging')
        self.assertEqual(d.meter_zero_cause,'hard_limit')
        self.assertEqual(self.p.sdk_calls[-1]['cap'],0)
        self.assert_no_positive()

    async def test_minimum_offer_does_not_invent_spent_or_blocked_overload_budget(self):
        for state, used in (('exhausted',1800.),('blocked',35.)):
            with self.subTest(state=state):
                if state!='exhausted':
                    await self.reset_plant()
                p=self.p
                if state=='blocked':
                    await self.restore_historical_recovery()
                    r=p.controller.regulator
                else:
                    r=await p.boot()
                await p.packet(199.8,{182:7.,183:7.},source=199.6,drain=False)
                await p.tick(200.,23.5,status='charging')
                self.assertEqual(r.last_target[0],7)
                cycle=r._ensure_automatic_cycle()
                if state=='exhausted':
                    # A spent budget is an external historical input, never
                    # an adapter ACK, live lease or vehicle response.
                    cycle.update(state=state,used_seconds=used,
                                 failure_reason='synthetic_budget_block')
                else:
                    self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
                    self.assertEqual(cycle['used_seconds'],used)
                cycle_id=cycle['cycle_id']
                await p.tick(230.,22.,status='charging')
                p.sdk_calls.clear()
                d=await p.tick(255.,25.4,status='charging')
                self.assertFalse(d.meter_overload_authority)
                self.assertEqual(r.last_target[0],0)
                self.assert_no_positive()
                after=r.export_overload_state()['automatic_cycle']
                self.assertEqual(after['cycle_id'],cycle_id)
                self.assertGreaterEqual(after['used_seconds'],used)


if __name__=='__main__':
    unittest.main()
