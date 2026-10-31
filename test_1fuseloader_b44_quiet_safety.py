"""Independent negative oracles for quiet-product normal continuity.

Clocks, RAW, native packets and HTTP replies are synthetic. The real
controller, adapter, finite leases, Store and dispatch boundaries run here.
Quiet HTTP receipts deliberately publish no ProductUpdate: neither an ACK
nor these tests manufacture a new current measurement or liveness pulse.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_b40_controller as actual


class B44QuietSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plant = actual.B40ControlledPhaseControllerTests(methodName='runTest')
        await self.plant.asyncSetUp()
        self.bind_real_exception_identity()

    def bind_real_exception_identity(self):
        # The predecessor fixture loads __init__.py and the adapter through
        # two synthetic package names. Real HA imports one adapter module.
        # Preserve that production typed-unsent contract for queued refusals.
        self.command_type_patch=patch.object(actual.fixtures.runtime,'CommandNotSent',
            self.plant.adapter_module.CommandNotSent)
        self.command_type_patch.start()

    async def asyncTearDown(self):
        try:
            await self.plant.asyncTearDown()
        finally:
            self.command_type_patch.stop()

    async def reset_plant(self):
        await self.asyncTearDown()
        self.plant = actual.B40ControlledPhaseControllerTests(methodName='runTest')
        await self.plant.asyncSetUp()
        self.bind_real_exception_identity()

    async def quiet_sdk(self, p1, p2, p3, minutes):
        p = self.plant
        row = dict(at=p.now, cap=p1, phases=(p1,p2,p3), ttl=minutes*60)
        p.sdk_calls.append(row)
        if p.sdk_hook is not None:
            await p.sdk_hook(row)
        if p.sdk_failure is not None:
            raise p.sdk_failure
        if p.physical_draw > .5 and p1 == 0:
            p.physical_interruptions.append(p.now)
        p.physical_draw = min(p.physical_draw, float(p1))
        p.now += p.sdk_delay
        # HTTP acceptance has no native event or readback side effect.
        return SimpleNamespace(status=200, release=Mock())

    async def tick(self, at, raw, *, reported=None, status=None):
        p = self.plant
        p.now = at
        if status is not None:
            p.states['sensor.status'].state = status
        p.grid(raw, at if reported is None else reported)
        if not p.controller._budget_loaded:
            await p.controller.async_start()
        else:
            await p.controller.async_tick()
        await p.pump()
        return p.controller.last_decision

    async def prepare_quiet(self, *, normal_raw=5.9):
        p = self.plant
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect = self.quiet_sdk
        pulse = p.product.state['latestPulse']
        clock = deepcopy(p.adapter.native_current_observation)
        for at in (220.,250.,280.,310.):
            d = await self.tick(at, normal_raw)
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(p.controller.regulator.last_target[0],16)
        self.assertEqual(p.product.state['latestPulse'], pulse)
        self.assertEqual(p.adapter.native_current_observation.received_at, clock.received_at)
        self.assertEqual(p.adapter.native_current_observation.source_timestamps, clock.source_timestamps)
        p.sdk_calls.clear()
        return pulse, clock

    def assert_no_positive(self):
        self.assertFalse(any(row['cap']>0 for row in self.plant.sdk_calls),
                         self.plant.sdk_calls)
        self.plant.sdk_charger.resume.assert_not_awaited()

    async def test_quiet_normal_renewal_keeps_same_cap_finite_and_does_not_refresh_ev(self):
        pulse, clock = await self.prepare_quiet()
        p = self.plant
        for at in (315.,340.,370.,400.):
            d = await self.tick(at,5.9)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(p.controller.regulator.last_target[0],16)
            self.assertFalse(d.meter_overload_authority)
        self.assertTrue(p.sdk_calls)
        self.assertTrue(all(row['cap']==16 and row['ttl']==60 for row in p.sdk_calls))
        self.assertTrue(all(b['at']<a['at']+a['ttl'] for a,b in zip(p.sdk_calls,p.sdk_calls[1:])))
        self.assertEqual(p.product.state['latestPulse'],pulse)
        observed=p.adapter.native_current_observation
        self.assertEqual(observed.received_at,clock.received_at)
        self.assertEqual(observed.source_timestamps,clock.source_timestamps)
        self.assertEqual(observed.observation_sequences,clock.observation_sequences)
        self.assertEqual(observed.observation_evidence_credit,clock.observation_evidence_credit)
        self.assertFalse(p.physical_interruptions)
        self.assertEqual(p.controller.regulator.export_overload_state()['automatic_cycle']['used_seconds'],0.)
        p.sdk_charger.resume.assert_not_awaited()

    async def test_quiet_exception_never_bypasses_online_or_stream_fault(self):
        for key in ('isOnline','signalRConnected'):
            with self.subTest(key=key):
                if key!='isOnline': await self.reset_plant()
                await self.prepare_quiet()
                self.plant.product.state[key]=False
                d=await self.tick(315.,3.)
                self.assertEqual(d.meter_zero_cause,'actuator_invalid')
                self.assert_no_positive()

    async def test_missing_or_future_product_clock_is_not_quiet(self):
        for clock_kind in ('missing','future'):
            with self.subTest(clock=clock_kind):
                if clock_kind!='missing': await self.reset_plant()
                await self.prepare_quiet()
                p=self.plant
                p.product.state['latestPulse']=None if clock_kind=='missing' else p.wall(400.)
                d=await self.tick(315.,3.)
                self.assertEqual(d.meter_zero_cause,'actuator_invalid')
                self.assert_no_positive()

    async def test_quiet_product_does_not_make_old_raw_fresh(self):
        await self.prepare_quiet()
        d=await self.tick(315.,3.,reported=100.)
        self.assertEqual(d.meter_zero_cause,'grid_invalid')
        self.assert_no_positive()

    async def test_expired_original_lease_cannot_be_resurrected_by_quiet_headroom(self):
        await self.prepare_quiet()
        p=self.plant
        expiry=p.controller.regulator.last_set_limit_ts+p.controller.regulator.last_target[3]
        d=await self.tick(expiry+1.,3.)
        # Either the expired authority or the unqualified quiet-source guard
        # may own the protective diagnosis. Neither may renew the old offer.
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_normal_whole_offer_bound_does_not_use_old_ev_subtraction(self):
        await self.prepare_quiet()
        d=await self.tick(315.,8.)
        # RAW8 + accepted16 exceeds normal23. Old EV16 is not house credit.
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_old_larger_possible_offer_counts_after_a_lower_normal_ack(self):
        p=self.plant
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect=self.quiet_sdk
        for at in (220.,250.,280.):
            await self.tick(at,5.9)
        # With unknown EV, fresh normal RAW24.5 permits an actual14A normal
        # decrease while the earlier16A request remains inside its own TTL.
        lower=await self.tick(309.,24.5)
        self.assertIsNone(lower.meter_zero_cause,lower)
        self.assertEqual(p.controller.regulator.last_target[0],14)
        p.sdk_calls.clear()
        d=await self.tick(312.,8.)
        # 8+14 fits23, but a still possible16 does not. Do not erase that
        # earlier authority merely because a lower HTTP request was accepted.
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_quiet_product_cannot_create_a_cold_positive_offer(self):
        p=self.plant
        await p.packet(99.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.},drain=False)
        p.sdk_circuit.set_dynamic_current.side_effect=self.quiet_sdk
        d=await self.tick(225.,3.,status='awaiting_start')
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()

    async def test_quiet_headroom_cannot_raise_a_live_seven_amp_offer(self):
        p=self.plant
        await p.boot()
        # Genuine independent7A input current and a later23.5A meter require
        # a normal7A offer. This is not an artificial proof-state injection.
        await p.packet(199.8,{182:7.2,183:7.},source=199.6)
        lower=await p.tick(200.,23.5,status='charging')
        self.assertIsNone(lower.meter_zero_cause,lower)
        self.assertEqual(p.controller.regulator.last_target[0],7)
        p.sdk_circuit.set_dynamic_current.side_effect=self.quiet_sdk
        for at in (230.,260.,290.,320.):
            d=await self.tick(at,18.)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(p.controller.regulator.last_target[0],7)
        p.sdk_calls.clear()
        d=await self.tick(325.,2.)
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertEqual(p.controller.regulator.last_target[0],7)
        self.assertTrue(all(row['cap']==7 for row in p.sdk_calls))
        self.assertFalse(d.meter_overload_authority)

    async def test_quiet_normal_bound_cannot_erase_an_unresolved_peak(self):
        p=self.plant
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect=self.quiet_sdk
        for at in (220.,250.,280.):
            await self.tick(at,5.9)
        peak=await self.tick(309.,31.)
        self.assertIsNone(peak.meter_zero_cause,peak)
        self.assertEqual(peak.current_p1,7)
        self.assertEqual(peak.nominal_response_deadline_ts,319.)
        original=p.controller.regulator.meter_control_diagnostics()
        p.sdk_calls.clear()
        d=await self.tick(312.,3.)
        self.assertIsNotNone(d.meter_zero_cause,d)
        self.assert_no_positive()
        retained=p.controller.regulator.meter_control_diagnostics()
        self.assertEqual(retained['episode_id'],original['episode_id'])
        self.assertEqual(retained['response_deadline'],original['response_deadline'])

    async def test_quiet_product_cannot_override_manual_pause_or_schedule(self):
        for status in ('paused','awaiting_scheduled_start','awaiting_authorization'):
            with self.subTest(status=status):
                if status!='paused': await self.reset_plant()
                await self.prepare_quiet()
                await self.tick(315.,3.,status=status)
                self.assert_no_positive()

    async def test_actual_unknown_delivery_is_not_hidden_by_later_quiet_raw(self):
        p=self.plant
        await p.boot()
        p.sdk_circuit.set_dynamic_current.side_effect=self.quiet_sdk
        p.sdk_failure=RuntimeError('Synthetic command failed after SDK entry')
        await self.tick(220.,3.)
        p.sdk_failure=None
        p.sdk_calls.clear()
        # The interrupted request is an actual ambiguity, not ProductUpdate
        # silence. Low RAW cannot turn it into accepted healthy authority.
        for at in (225.,255.,285.,315.):
            await self.tick(at,3.)
        self.assert_no_positive()

    async def queued_quiet_change(self, kind):
        await self.prepare_quiet()
        p=self.plant
        await p.adapter._request_lock.acquire()
        task=asyncio.create_task(self.tick(340.,5.9))
        try:
            for _ in range(60):
                await asyncio.sleep(0)
                if p.controller._dispatch_task is not None:
                    break
            self.assertIsNotNone(p.controller._dispatch_task,
                'The positive renewal must actually wait before SDK entry')
            self.assertEqual(p.controller._dispatch_decision.current_p1,16,
                'This oracle must exercise a queued positive normal renewal')
            self.assertFalse(p.sdk_calls)
            p.now=339. if kind=='monotonic' else 341.
            p.grid(8. if kind=='raw' else 3.,
                335. if kind=='grid_clock' else p.now)
            if kind=='transport': p.product.state['signalRConnected']=False
        finally:
            p.adapter._request_lock.release()
        await task
        self.assert_no_positive()
        self.assertNotEqual(p.controller.regulator.meter_control_diagnostics()['operational_fault'],
                            'unknown_delivery')

    async def test_whole_raw_bound_is_rechecked_after_waiting_for_adapter_lock(self):
        await self.queued_quiet_change('raw')

    async def test_real_transport_is_rechecked_after_waiting_for_adapter_lock(self):
        await self.queued_quiet_change('transport')

    async def test_newly_received_regressed_raw_clock_cannot_enter_sdk(self):
        await self.queued_quiet_change('grid_clock')

    async def test_regressed_runtime_clock_cannot_enter_sdk(self):
        await self.queued_quiet_change('monotonic')


if __name__=='__main__':
    unittest.main()
