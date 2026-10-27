"""Independent public synthetic meter-policy controller regressions.

No household records or running HA are used. Actual runtime, Store, dispatch,
SDK-result and conductor publication paths run against synthetic sources.
Accepted authority is created by real fixture acknowledgements, never seeding
private response owners or repairing their deadlines.
"""
import asyncio
from copy import deepcopy
from dataclasses import fields, replace
import unittest
from types import SimpleNamespace

import test_1fuseloader_connected_full_readiness_controller as connected
import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_runtime as fixtures
from test_1fuseloader_ha_current_representation import float32


class MeterEnvelopeControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = connected.ConnectedFullReadinessControllerTests.update_entry
    wall = connected.ConnectedFullReadinessControllerTests.wall
    schedule = connected.ConnectedFullReadinessControllerTests.schedule
    active_timers = connected.ConnectedFullReadinessControllerTests.active_timers
    set_native = connected.ConnectedFullReadinessControllerTests.set_native
    snapshot = connected.ConnectedFullReadinessControllerTests.snapshot
    install = connected.ConnectedFullReadinessControllerTests.install
    native_vector = connected.ConnectedFullReadinessControllerTests.native_vector
    grid = connected.ConnectedFullReadinessControllerTests.grid
    configure = connected.ConnectedFullReadinessControllerTests.configure
    publish = connected.ConnectedFullReadinessControllerTests.publish
    tick = connected.ConnectedFullReadinessControllerTests.tick
    asyncTearDown = connected.ConnectedFullReadinessControllerTests.asyncTearDown
    execute = connected.ConnectedFullReadinessControllerTests.execute
    report = connected.ConnectedFullReadinessControllerTests.report
    slow_first_checkpoint = ready.DisconnectedReadinessRuntimeTests.slow_first_checkpoint

    async def asyncSetUp(self):
        await connected.ConnectedFullReadinessControllerTests.asyncSetUp(self)
        self.configure(measurement_settle_seconds=25)
        # The same physical vectors can run RED on the predecessor rather than
        # failing only because its Settings constructor lacks the new field.
        if 'meter_envelope_control' in {f.name for f in fields(fixtures.logic.RegulatorSettings)}:
            self.configure(meter_envelope_control=True)

    async def sample(self, at, raw, current=None, receipt=None, status='charging'):
        if current is None:
            self.now = at
            self.grid(raw)
            self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        else:
            self.report(at, status, raw, current, receipt)
        await self.tick()
        return self.controller.last_decision

    def assert_typed_zero(self):
        for decision in self.adapter.calls:
            if decision.command == 'set_limit' and decision.current_p1 == 0:
                self.assertIn(getattr(decision, 'meter_zero_cause', None),
                              {cause.value for cause in fixtures.logic.ElectricalStopCause},decision)

    def assert_positive_only(self):
        self.assertTrue(all(d.command == 'set_limit' and d.current_p1 > 0
                            for d in self.adapter.calls),
                        [(d.command, d.current_p1, d.mode, d.reason) for d in self.adapter.calls])

    async def boot(self):
        r = self.controller.regulator
        saved = r.export_overload_state()
        saved.update(used_seconds=3210., failure_reason='response_timeout')
        await self.controller._budget_store.async_save(saved)
        for at in range(100, 186, 5):
            self.report(float(at), 'disconnected', 3., .02, 99.)
            await self.tick()
            if r.last_target and r.last_target[0] == 16:
                break
        self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
        self.adapter.calls.clear()
        return r

    async def settled_sixteen(self):
        r = await self.boot()
        await self.sample(130., 21.2, 16.2, 129.)
        await self.sample(135., 21.2, 16.2, 129.)
        self.assertEqual(r.last_target[0], 16)
        self.adapter.calls.clear()
        return r

    async def partial_chain(self):
        r = await self.settled_sixteen()
        await self.sample(150., 24.8, 16.23, 140.)
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        fourteen = r.last_set_limit_ts
        self.report(155.2,'charging',24.8,15.2,155.1)
        self.grid(24.8,155.)
        await self.tick()
        await self.sample(174., 24.7, 14.146, 173.7)
        self.assertEqual(r.last_target[0], 12, self.controller.last_decision)
        twelve = r.last_set_limit_ts
        self.assertLess(twelve-fourteen, 25.1)
        self.adapter.calls.clear()
        return r, fourteen, twelve

    async def test_explicit_opt_in_reaches_actual_controller(self):
        await self.boot()
        self.assertTrue(self.controller.settings.meter_envelope_control)
        self.assertTrue(self.controller.last_decision.meter_envelope)
        inp = self.controller._read_input()
        for name in ('meter_grid_health', 'meter_actuator_health', 'meter_ev_health'):
            self.assertIsNotNone(getattr(inp, name))

    async def test_startup_delayed_low_ev_never_creates_overload_zero(self):
        for raw, low, later in ((22.3, 1.102, 14.067), (24.3, 6.42, 16.093),
                                (24.2, 3.041, 16.095)):
            with self.subTest(raw=raw, low_ev=low):
                if raw != 22.3:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.boot()
                await self.sample(130., raw, low, 126.)
                await self.sample(130.8, raw, later, 130.7)
                await self.sample(136., raw, later, 130.7)
                self.assert_positive_only()
                self.assertGreater(r.last_target[0], 0)
                state = r.export_overload_state()
                self.assertEqual(state['normal_control']['stops'], 0)
                self.assertFalse(state['normal_control']['restart'])

    async def test_fractional_partial_observation_expiry_does_not_own_zero(self):
        for fractional in (12.49, 12.50, 12.51, 12.61):
            for grid_first in (True, False):
                with self.subTest(current=fractional, grid_first=grid_first):
                    if (fractional,grid_first) != (12.49,True):
                        await self.asyncTearDown()
                        await self.asyncSetUp()
                    r, old, accepted = await self.partial_chain()
                    if grid_first:
                        await self.sample(180., 22.6)
                        self.report(180.2,'charging',22.6,fractional,180.1)
                        self.grid(22.6,180.)
                        await self.tick()
                    else:
                        await self.sample(180., 24.7, fractional, 179.9)
                        await self.sample(180.2, 22.6)
                    # Genuine receipt ordering can keep the latest EV just
                    # ahead of the5s meter clock for longer than the old25s
                    # observation budget. RAW remains independently healthy.
                    for at in (185.2,190.2,195.2,200.2,205.2,206.2):
                        self.report(at,'charging',22.6,fractional,at-.1)
                        self.grid(22.6,at-.5)
                        await self.tick()
                    await self.sample(206.200618, 22.6, 12.128, 206.2005)
                    self.assert_positive_only()
                    self.assertGreater(r.last_target[0],0)
                    self.assertGreater(self.now-old,25.)
                    self.assertEqual(r.export_overload_state()['normal_control']['stops'],0)

    async def test_native_partial_publication_never_relabels_estimate_as_grid_fault(self):
        r, _, _ = await self.partial_chain()
        accepted = r.last_set_limit_ts
        self.now = 180.
        self.grid(22.6)
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        self.native.native_publication_pending = True
        self.native.sdk_current = float32(12.61)
        self.native.divergence_started_monotonic = 179.9
        self.native.publication_generation = 2
        await self.tick()
        self.assert_positive_only()
        self.assertGreater(r.last_target[0],0)
        await self.sample(181.2,22.6,12.128,181.1)
        self.assert_positive_only()
        self.assertGreaterEqual(r.last_set_limit_ts,accepted)

    async def test_valid_sources_with_older_partial_debt_do_not_gain_electrical_zero_authority(self):
        r,_,accepted=await self.partial_chain()
        for at in (180.,185.,190.,195.,200.,205.,206.):
            self.report(at,'charging',22.6,12.61,179.9)
            self.grid(22.6,179.5)
            await self.tick()
            # Source validity is separately bounded by120s; crossing the
            # ordinary25s observation clock is not a critical source expiry.
            self.assertLess(self.now-179.5,self.controller.settings.max_sensor_age_seconds)
        self.assert_positive_only()
        self.assertGreater(r.last_target[0],0)
        self.assertEqual(r.export_overload_state()['normal_control']['stops'],0)
        self.assertLess(self.now,accepted+60.)

    async def test_stable_old_ev_renews_three_ttls_without_fake_measurement_credit(self):
        r = await self.settled_sixteen()
        native = deepcopy(vars(self.native))
        receipt = self.controller._read_input().charger_report_ts
        for at in range(145,366,5):
            await self.sample(float(at),21.2)
            self.assertEqual(self.controller._read_input().charger_report_ts,receipt)
            self.assertEqual(vars(self.native),native)
            self.assertGreater(r.last_target[0],0)
        self.assert_positive_only()
        actual = [row for row in self.dispatches if row['at'] >= 130.]
        self.assertGreaterEqual(len(actual),5)
        for first,second in zip(actual,actual[1:]):
            self.assertLess(second['at'], first['at']+first['ttl'])
        self.assertLess(self.now,actual[-1]['at']+actual[-1]['ttl'])
        self.assertEqual(r.export_overload_state()['normal_control']['stops'],0)

    async def test_one_raw_sample_cannot_compound_the_same_normal_feedback_reduction(self):
        r=await self.settled_sixteen()
        self.native.error='native_sdk_current_invalid'
        for at in (150.,152.,154.):
            self.now=at
            self.grid(24.2,150.)
            self.circuit.attributes['state_latestPulse']=self.wall().isoformat()
            await self.tick()
        self.assert_positive_only()
        self.assertEqual(r.last_target[0],14,self.controller.last_decision)
        self.assertEqual([d.current_p1 for d in self.adapter.calls],[14])

    async def test_real_coherent_capacity_raises_directly_to_sixteen(self):
        r, _, _ = await self.partial_chain()
        await self.sample(190.,15.2,12.2,189.5)
        await self.sample(190.2,15.2)
        self.assertEqual(r.last_target[0],16,self.controller.last_decision)
        self.assertEqual(self.adapter.calls[-1].current_p1,16)
        self.assert_positive_only()

    async def test_pending_real_store_keeps_only_old_actual_lease_then_renews(self):
        r, _, _ = await self.partial_chain()
        prior = self.controller._budget_store
        self.controller._budget_store = type(prior)(prior.store,
            clock=prior.clock,read_back=prior.store.async_load)
        self.slow_first_checkpoint()
        cap, accepted = r.last_target[0],r.last_set_limit_ts
        await self.sample(180.,22.6,12.61,179.7)
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual((r.last_target[0],r.last_set_limit_ts),(cap,accepted))
        await self.controller._budget_store.async_wait_pending()
        await self.sample(self.now+5.,22.6)
        self.assert_positive_only()
        self.assertGreater(r.last_target[0],0)

    async def test_active_electrical_period_can_retain_normal_seven_during_store_wait(self):
        r=await self.settled_sixteen()
        await self.sample(150.,38.,16.2,149.9)
        self.assertEqual(r.last_target[0],7,self.controller.last_decision)
        await self.sample(150.2,29.,7.,150.1)
        for at in (155.,160.,165.,170.,175.,180.):
            await self.sample(at,29.)
        self.assertEqual(r.export_overload_state()['automatic_cycle']['state'],'active')
        prior=self.controller._budget_store
        self.controller._budget_store=type(prior)(prior.store,
            clock=prior.clock,read_back=prior.store.async_load)
        self.slow_first_checkpoint()
        self.adapter.calls.clear()
        actual,accepted=r.last_target,r.last_set_limit_ts
        await self.sample(185.,22.6)
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual((r.last_target,r.last_set_limit_ts),(actual,accepted))
        await self.controller._budget_store.async_wait_pending()
        await self.sample(accepted+35.,22.6)
        self.assert_positive_only()
        self.assertEqual(r.last_target[0],7)

    async def test_completed_during_http_does_not_become_unknown_delivery_zero(self):
        r = await self.settled_sixteen()
        execute = self.execute
        async def complete_after_boundary(decision,on_dispatch):
            if decision.current_p1==14:
                self.assertTrue(on_dispatch())
                self.adapter.calls.append(decision)
                self.now += .01
                self.states['sensor.status'].state='completed'
                self.states['sensor.status'].last_changed=self.wall()
                fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller,None)
                await asyncio.sleep(0)
                return SimpleNamespace(http_status=200,correlation_ids=())
            return await execute(decision,on_dispatch)
        self.adapter.async_execute_with_dispatch=complete_after_boundary
        await self.sample(150.,24.8,16.23,149.)
        await self.sample(150.2,5.,status='completed')
        self.assertFalse(any(d.command=='resume' or d.current_p1==0 for d in self.adapter.calls),
                         [(d.current_p1,d.mode,d.reason) for d in self.adapter.calls])

    async def test_known_not_sent_renewal_preserves_actual_authority_and_original_lease(self):
        r=await self.settled_sixteen()
        actual,lease=r.last_target,r.last_set_limit_ts
        execute=self.execute
        async def known_unsent(decision,on_dispatch):
            # Deliberately BEFORE SDK admission: no on_dispatch and no call.
            raise fixtures.runtime.CommandNotSent('synthetic_not_sent',retry_after_seconds=1.)
        self.adapter.async_execute_with_dispatch=known_unsent
        await self.sample(lease+35.,21.2)
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual((r.last_target,r.last_set_limit_ts),(actual,lease))
        self.assertLess(self.now,lease+actual[3])
        self.adapter.async_execute_with_dispatch=execute
        await self.sample(self.now+2.,21.2)
        self.assert_positive_only()
        self.assertEqual(r.last_target[0],16)
        self.assertGreater(r.last_set_limit_ts,lease)

    async def test_real_hard_peak_during_http_still_preempts_the_inflight_offer(self):
        await self.settled_sixteen()
        execute=self.execute
        async def critical_after_boundary(decision,on_dispatch):
            if decision.current_p1==14:
                self.assertTrue(on_dispatch())
                self.adapter.calls.append(decision)
                self.now+=.01
                self.grid(39.)
                self.circuit.attributes['state_latestPulse']=self.wall().isoformat()
                fixtures.runtime.OneFuseLoaderController._handle_source_change(self.controller,None)
                await asyncio.sleep(0)
                return SimpleNamespace(http_status=200,correlation_ids=())
            return await execute(decision,on_dispatch)
        self.adapter.async_execute_with_dispatch=critical_after_boundary
        await self.sample(150.,24.8,16.23,149.)
        await self.sample(150.2,39.)
        zeros=[d for d in self.adapter.calls if d.current_p1==0]
        self.assertTrue(zeros,self.controller.last_decision)
        self.assert_typed_zero()

    async def test_unknown_sdk_outcome_is_not_accepted_or_a_new_positive_lease(self):
        r=await self.settled_sixteen()
        actual,lease=r.last_target,r.last_set_limit_ts
        self.dispatch_failure=RuntimeError('synthetic_ambiguous_delivery')
        await self.sample(lease+35.,21.2)
        self.assertEqual((r.last_target,r.last_set_limit_ts),(actual,lease))
        self.assertTrue(self.adapter.calls)
        self.dispatch_failure=None
        await self.sample(self.now+1.,21.2)
        after=self.adapter.calls[1:]
        self.assertTrue(any(d.current_p1==0 for d in after),self.controller.last_decision)
        self.assertTrue(all(d.current_p1==0 for d in after),after)
        self.assert_typed_zero()

    async def test_expired_actual_lease_cannot_be_retained_as_an_accepted_live_offer(self):
        r=await self.settled_sixteen()
        lease=r.last_set_limit_ts
        await self.sample(lease+61.,21.2)
        self.assertFalse(any(d.current_p1>0 for d in self.adapter.calls))
        self.assertTrue(any(d.current_p1==0 for d in self.adapter.calls),self.controller.last_decision)
        self.assert_typed_zero()

    async def test_manual_pause_never_sends_positive_or_resume(self):
        await self.settled_sixteen()
        for at in (150.,180.,210.):
            await self.sample(at,5.,.02,149.,status='paused')
        self.assertFalse(any(d.command=='resume' or d.current_p1>0 for d in self.adapter.calls))

    async def test_manual_pause_during_high_house_load_does_not_demand_a_vehicle_response(self):
        await self.settled_sixteen()
        for at in (150.,155.,160.1):
            await self.sample(at,29.,.02,149.9,status='paused')
        self.assertFalse(any(d.command=='resume' or d.current_p1>0 for d in self.adapter.calls),
                         [(d.current_p1,d.mode,d.reason) for d in self.adapter.calls])
        self.assertFalse(any(getattr(d,'meter_zero_cause',None)=='peak_response_timeout'
                             for d in self.adapter.calls))

    async def test_real_grid_fault_and_hard_peak_have_typed_zero(self):
        for fault in ('invalid','hard_peak','actuator'):
            with self.subTest(fault=fault):
                if fault != 'invalid':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.settled_sixteen()
                self.now=150.
                self.grid('unavailable' if fault=='invalid' else 38.1 if fault=='hard_peak' else 5.)
                self.circuit.attributes['state_latestPulse']=self.wall().isoformat()
                if fault=='actuator':
                    self.adapter.telemetry_block_reason='synthetic_transport_down'
                await self.tick()
                zeros=[d for d in self.adapter.calls if d.command=='set_limit' and d.current_p1==0]
                self.assertTrue(zeros,self.controller.last_decision)
                self.assertTrue(all(getattr(d,'meter_zero_cause',None) for d in zeros))

    async def test_bad_ev_numeric_is_estimate_only_not_critical_grid_failure(self):
        r = await self.settled_sixteen()
        self.native.error='native_sdk_current_invalid'
        self.now=150.
        self.grid(21.2)
        self.circuit.attributes['state_latestPulse']=self.wall().isoformat()
        await self.tick()
        inp=self.controller._read_input()
        self.assertTrue(inp.meter_grid_health.valid)
        self.assertTrue(inp.meter_actuator_health.valid)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assert_positive_only()
        self.assertGreater(r.last_target[0],0)

    async def test_permanent_store_failure_is_a_real_typed_critical_stop(self):
        await self.settled_sixteen()
        old=self.controller._budget_store
        old.store.failure=True
        self.controller._budget_store=type(old)(old.store,clock=old.clock,
                                                read_back=old.store.async_load)
        await self.sample(150.,21.2)
        zeros=[d for d in self.adapter.calls if d.command=='set_limit' and d.current_p1==0]
        self.assertTrue(zeros,self.controller.last_decision)
        self.assertTrue(all(getattr(d,'meter_zero_cause',None) for d in zeros))

    async def test_changed_control_binding_cannot_retain_old_accepted_authority(self):
        await self.settled_sixteen()
        self.controller._configured_control_identity.return_value='different-synthetic-source'
        await self.sample(150.,21.2)
        self.assertFalse(any(d.current_p1>0 for d in self.adapter.calls))
        self.assertTrue(any(d.current_p1==0 for d in self.adapter.calls),self.controller.last_decision)
        self.assert_typed_zero()

    async def test_actual_sender_rejects_untyped_zero_from_an_ordinary_plan(self):
        r=await self.settled_sixteen()
        evaluate=r.evaluate
        def accidentally_zero(inp):
            d=evaluate(inp)
            return replace(d,current_p1=0,current_p2=0,current_p3=0,
                command='set_limit',service='easee.set_circuit_dynamic_limit',
                should_call_service=True,meter_zero_cause=None)
        r.evaluate=accidentally_zero
        await self.sample(150.,21.2)
        self.assertFalse(any(d.current_p1==0 for d in self.adapter.calls),
                         [(d.current_p1,d.meter_zero_cause,d.reason) for d in self.adapter.calls])


if __name__ == '__main__':
    unittest.main()
