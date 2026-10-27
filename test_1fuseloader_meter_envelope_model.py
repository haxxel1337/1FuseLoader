"""Independent physical plant oracle for the opt-in meter envelope.

The established synthetic event plant owns draw, delivery, generations and TTL;
the actual controller owns decisions. This separate oracle asserts operations,
not mode names. No private installation trace is included.
"""
import asyncio
from itertools import product
import logging
import unittest
from unittest.mock import patch

import test_1fuseloader_independent_operating_model as original
import test_1fuseloader_b40_fixture_evidence as phase_fixture


class MeterEnvelopePlant(original.ControllerPlant):
    async def setup(self):
        self.meter_zeros=[]
        self.cycle_trace=[]
        self.last_cycle_signature=None
        self.meter_samples=[]
        self.last_meter_signature=None
        configure=original.ready.DisconnectedReadinessRuntimeTests.configure
        def selected(case,enabled=True,**kwargs):
            kwargs.update(meter_envelope_control=True,measurement_settle_seconds=25)
            result=configure(case,enabled,**kwargs)
            def delivered_l1():
                if self.opts['secondary_delay']:
                    component=self.native_components.get(183)
                    return component[0] if component is not None else None
                return case.native.current if 183 in case.native.observation_ids else None
            phase_fixture.install(case,adopted=90.,value_reader=delivered_l1)
            return result
        with patch.object(original.ready.DisconnectedReadinessRuntimeTests,'configure',selected):
            await super().setup()

    async def pump(self):
        await super().pump()
        if hasattr(self,'r'):
            inp=self.c._read_input()
            meter_signature=(inp.grid_l1,inp.grid_report_ts)
            if self.c.last_decision is not None and meter_signature!=self.last_meter_signature:
                self.meter_samples.append(dict(at=self.now,raw=inp.grid_l1,
                    grid_report_ts=inp.grid_report_ts,
                    electrical_response_deadline=self.r.peak_response_deadline(self.now),
                    peak_previous_authority=self.r.export_overload_state().get('peak_response_previous_authority')))
                self.last_meter_signature=meter_signature
            if self.trace and self.trace[-1]['event']=='PLAN' and self.trace[-1]['at']==self.now:
                self.trace[-1]['grid_report_ts']=self.c._read_input().grid_report_ts
                self.trace[-1]['electrical_response_deadline']=self.c.last_decision.nominal_response_deadline_ts
                self.trace[-1]['peak_previous_authority']=self.r.export_overload_state().get('peak_response_previous_authority')
            cycle=self.r.export_overload_state()['automatic_cycle']
            signature=(cycle['cycle_id'],cycle['state'],cycle['failure_reason'])
            if signature!=self.last_cycle_signature:
                self.cycle_trace.append(dict(at=self.now,**cycle))
                self.last_cycle_signature=signature

    async def execute(self,decision):
        if decision.command=='set_limit' and decision.current_p1==0:
            inp=self.c._read_input()
            self.meter_zeros.append(dict(at=self.now,cause=getattr(decision,'meter_zero_cause',None),
                state=getattr(decision,'meter_state',None),reason=decision.reason,
                true_grid=self.house+self.draw,raw=inp.grid_l1,ev=inp.charger_current,
                grid_health=getattr(inp,'meter_grid_health',None),
                actuator_health=getattr(inp,'meter_actuator_health',None)))
        return await super().execute(decision)

    async def run(self):
        result=await super().run()
        result['meter_zero_events']=self.meter_zeros
        result['final_exposure']=self.r.export_overload_state()
        result['automatic_cycle_transitions']=self.cycle_trace
        result['meter_samples']=self.meter_samples
        return result


class MeterEnvelopePlantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop=asyncio.get_running_loop()
        self.debug=self.loop.get_debug()
        self.loop.set_debug(False)
        self.logger=logging.getLogger('onefuse_runtime_tests.runtime')
        self.disabled=self.logger.disabled
        self.logger.disabled=True

    async def asyncTearDown(self):
        self.logger.disabled=self.disabled
        self.loop.set_debug(self.debug)

    def assert_healthy(self,result,max_grid=25.):
        self.assertIsNotNone(result['first_positive_ack'])
        self.assertLessEqual(result['physical_max_grid'],max_grid)
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
        self.assertIsNone(result['first_zero'],result['first_zero'])
        self.assertFalse(result['meter_zero_events'],result['meter_zero_events'])

    async def test_healthy_normal_plant_never_interrupts_across_delivery_orders(self):
        # Long native lag changes confidence, not validity of the true meter.
        for ready,ev,http,partial in product((False,True),(0.,8.,30.),(.2,2.),(0.,.002)):
            options=dict(ready=ready,grid_delay=0.,ev_delay=ev,status_delay=8.,
                http_delay=http,secondary_delay=partial,apply_delay=.2,
                provider_quantum=1.,direct_full=True,house_steps=((163.,8.2),),horizon=360.2)
            result=await MeterEnvelopePlant(**options).run()
            with self.subTest(options=options):
                self.assert_healthy(result,24.2)
                self.assertEqual(result['final_exposure']['used_seconds'],4000.)
                self.assertEqual(result['final_exposure']['normal_control']['stops'],0)

    async def test_real_store_latency_does_not_trade_an_old_lease_for_zero(self):
        for ready,http in product((False,True),(.2,2.)):
            result=await MeterEnvelopePlant(ready=ready,store_delay=2.5,http_delay=http,
                ev_delay=8.,status_delay=8.,apply_delay=.2,direct_full=True,
                house_steps=((200.,8.2),),secondary_delay=.002,native_offset=.264,
                fresh_verification_at=213.1,horizon=380.2).run()
            with self.subTest(ready=ready,http=http):
                self.assert_healthy(result,24.2)
                self.assertGreater(result['counters'].get('store_writes_completed',0),1)

    async def test_one_generation_cannot_expire_three_renewed_ttls(self):
        result=await MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=8.,
            http_delay=2.,store_delay=2.5,horizon=480.2).run()
        self.assert_healthy(result,21.)
        positives=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['cap']>0]
        self.assertGreaterEqual(len(positives),7)
        for previous,following in zip(positives,positives[1:]):
            self.assertLess(following['at'],previous['at']+60.)

    async def test_critical_source_and_actual_hard_peak_have_typed_stop(self):
        for fault in ('source','hard_peak'):
            result=await MeterEnvelopePlant(fault=fault,ready=True,direct_full=True,
                ev_delay=8.,http_delay=.2,apply_delay=2.).run()
            with self.subTest(fault=fault):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNotNone(result['first_zero'])
                zeros=[row for row in result['meter_zero_events'] if row['at']>=200.]
                self.assertTrue(zeros)
                self.assertTrue(all(row['cause'] for row in zeros),zeros)
                if fault=='hard_peak':
                    self.assertLessEqual(zeros[0]['at'],200.1)

    async def test_user_pause_is_not_resumed_or_overridden(self):
        result=await MeterEnvelopePlant(fault='manual_pause',ready=True,direct_full=True,
            ev_delay=8.,http_delay=.2,apply_delay=2.).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        after=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=200.]
        self.assertFalse(any(row['cap']>0 or row['command']=='resume' for row in after),after)

    async def test_real_peak_minimum_response_within_ten_seconds_is_not_observation_zero(self):
        result=await MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=8.,
            http_delay=.2,apply_delay=.2,house_steps=((200.,22.),),horizon=250.2).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertLessEqual(result['physical_max_grid'],38.)
        after=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=200.]
        minimum=[row for row in after if row['cap']==7]
        self.assertTrue(minimum,result['trace'][-15:])
        self.assertLess(minimum[0]['at'],210.)
        self.assertIsNone(result['first_zero'],result['first_zero'])

    async def test_nonresponding_real_peak_stops_at_original_ten_second_deadline(self):
        result=await MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=0.,responds=False,
            http_delay=.2,apply_delay=.2,house_steps=((200.,22.),),horizon=235.2).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        zero=result['first_zero']
        self.assertIsNotNone(zero,result['trace'][-15:])
        self.assertGreaterEqual(zero['at'],210.)
        high=[row for row in result['trace'] if row['event']=='PLAN' and float(row['raw'])>28.]
        self.assertTrue(high)
        source_origin=high[0]['grid_report_ts']-original.EPOCH
        self.assertGreaterEqual(zero['at'],source_origin+10.)
        self.assertLessEqual(zero['at'],source_origin+10.2)
        typed=[row for row in result['meter_zero_events'] if row['at']>=200.]
        self.assertTrue(typed and typed[0]['cause'],typed)

    async def test_sustained_minimum_at_28_point_5_or_29_does_not_rearm_peak_timer(self):
        for house in (21.5,22.):
            plant=MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=8.,
                http_delay=.2,apply_delay=.2,house_steps=((200.,house),),horizon=500.2)
            result=await plant.run()
            with self.subTest(sustained_total=house+7.):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNone(result['first_zero'],result['first_zero'])
                later=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=200.]
                self.assertGreaterEqual(len(later),7)
                self.assertTrue(all(row['cap']==7 for row in later),later)
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                cycle=result['final_exposure']['automatic_cycle']
                self.assertEqual(cycle['state'],'active')
                self.assertGreater(cycle['used_seconds'],250.)

    async def test_bounded_peak_inside_two_second_sdk_preserves_first_meter_deadline(self):
        class CollisionPlant(MeterEnvelopePlant):
            async def setup(self):
                await super().setup()
                # Renewal enters SDK190→192. Physical house rises190.1;
                # this extra actual meter capture arrives during that await.
                self.add(190.2,'capture_grid')
        for native_delay in (0.,2.,12.):
            result=await CollisionPlant(ready=True,direct_full=True,ev_delay=native_delay,
                http_delay=2.,apply_delay=.2,house_steps=((190.1,22.),),horizon=240.2).run()
            with self.subTest(native_delay=native_delay):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                requests=[row for row in result['trace'] if row['event']=='HTTP_BEGIN']
                self.assertTrue(any(row['at']==190. and row['cap']==16 for row in requests),requests)
                minimum=[row for row in requests if row['at']>=190.2 and row['cap']==7]
                self.assertTrue(minimum,result['trace'][-15:])
                self.assertGreaterEqual(minimum[0]['at'],192.)
                self.assertLess(minimum[0]['at'],200.2)
                deadlines=[row['electrical_response_deadline'] for row in result['trace']
                    if row['event']=='PLAN' and row.get('electrical_response_deadline') is not None
                    and 190.2<=row['at']<=200.2]
                self.assertTrue(deadlines,result['trace'][-15:])
                self.assertTrue(all(abs(deadline-200.2)<1e-6 for deadline in deadlines),deadlines)
                if native_delay<=2.:
                    self.assertIsNone(result['first_zero'],result['first_zero'])
                    self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                else:
                    zero=result['first_zero']
                    self.assertIsNotNone(zero,result['trace'][-15:])
                    self.assertGreaterEqual(zero['at'],200.2)
                    self.assertLessEqual(zero['at'],200.4)
                    typed=[row for row in result['meter_zero_events'] if row['at']>=190.2]
                    self.assertEqual(typed[0]['cause'],'peak_response_timeout')

    async def test_cumulative_budget_preserves_original_90_second_lease_reserve(self):
        result=await MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=8.,
            http_delay=.2,apply_delay=.2,house_steps=((200.,22.),),budget=1800,
            horizon=2010.2).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertIsNotNone(result['first_zero'],result['trace'][-20:])
        # Actual finite leases retain their existing90s PRE reserve. The
        # operational maximum is1800s, not a promise to consume every second.
        self.assertGreaterEqual(result['first_zero']['at'],1910.)
        self.assertLessEqual(result['first_zero']['at'],1915.2)
        typed=[row for row in result['meter_zero_events'] if row['at']>=200.]
        self.assertTrue(typed)
        self.assertEqual(typed[0]['cause'],'overload_lease_reserve')
        used=result['final_exposure']['automatic_cycle']['used_seconds']
        self.assertGreaterEqual(used,1710.)
        self.assertLessEqual(used,1715.2)
        self.assertLess(1800.-used,90.)
        self.assertAlmostEqual(result['final_exposure']['used_seconds'],4000.+used)

    async def test_new_normal_meter_can_continue_without_erasing_original_electrical_clock(self):
        class TransientPlant(MeterEnvelopePlant):
            async def setup(self):
                await super().setup()
                self.add(190.2,'capture_grid')
                self.add(194.2,'capture_grid')
                self.add(210.2,'capture_grid')
        for returns_high in (False,True):
            steps=((190.1,22.),(194.1,11.)) + (((210.1,22.),) if returns_high else ())
            result=await TransientPlant(ready=True,direct_full=True,ev_delay=30.,http_delay=2.,
                apply_delay=.2,house_steps=steps,horizon=215.2).run()
            with self.subTest(returns_high=returns_high):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                normal=[row for row in result['meter_samples']
                        if 194.2<=row['at']<210.1 and float(row['raw'])==18.]
                self.assertTrue(normal,result['trace'][-20:])
                self.assertTrue(any(row['at']>200.2 for row in normal),normal)
                # Normal meter permission neither grants an EV response nor
                # refunds the original real peak's10s or known16 A upper.
                self.assertTrue(all(row['electrical_response_deadline']==200.2 for row in normal),normal)
                self.assertTrue(all(row['peak_previous_authority']==16 for row in normal),normal)
                later=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=194.2]
                self.assertFalse(any(row['cap']>7 for row in later),later)
                if not returns_high:
                    self.assertIsNone(result['first_zero'],result['first_zero'])
                    self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                else:
                    zero=result['first_zero']
                    self.assertIsNotNone(zero,result['trace'][-20:])
                    self.assertEqual(zero['raw'],'29.0')
                    self.assertGreaterEqual(zero['at'],210.2)
                    self.assertLessEqual(zero['at'],210.4)
                    typed=[row for row in result['meter_zero_events'] if row['at']>=190.2]
                    self.assertEqual(typed[0]['cause'],'peak_response_timeout')

    async def test_rearm_requires_continuous_strictly_below_23_for_1800_seconds(self):
        # After expiry the car draws zero. Exactly23 A interrupts cooling;
        # only the later22.9 A segment may create a new automatic period.
        result=await MeterEnvelopePlant(ready=True,direct_full=True,ev_delay=8.,
            http_delay=.2,apply_delay=.2,house_steps=((200.,22.),(2050.,23.),(3850.,22.9)),
            budget=1800,horizon=5660.2).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertIsNotNone(result['first_zero'])
        self.assertEqual(result['final_exposure']['automatic_cycle']['state'],'ready')
        self.assertEqual(result['final_exposure']['automatic_cycle']['used_seconds'],0.)
        self.assertGreaterEqual(result['final_exposure']['used_seconds'],5710.)
        self.assertLessEqual(result['final_exposure']['used_seconds'],5715.2)
        ready=[row for row in result['automatic_cycle_transitions']
               if row['at']>2000. and row['state']=='ready']
        self.assertTrue(ready,result['automatic_cycle_transitions'])
        self.assertGreaterEqual(ready[0]['at'],5650.)
        self.assertLessEqual(ready[0]['at'],5655.2)
        # No positive session starts from a22.9 A house with no whole-RAW
        # headroom, even though its electrical period has rearmed.
        late=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=2000.]
        self.assertFalse(any(row['cap']>0 for row in late),late)

    async def test_transient_normal_meter_renews_three_ttls_without_new_ev_or_peak_clock(self):
        class NoNewCurrentPlant(MeterEnvelopePlant):
            async def setup(self):
                await super().setup()
                self.add(190.2,'capture_grid')
                self.add(194.2,'capture_grid')
        result=await NoNewCurrentPlant(ready=True,direct_full=True,ev_delay=500.,
            http_delay=2.,apply_delay=.2,house_steps=((190.1,22.),(194.1,11.)),
            horizon=445.2).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertIsNone(result['first_zero'],result['first_zero'])
        self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
        after=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=192.]
        self.assertGreaterEqual(len(after),8)
        self.assertTrue(all(row['cap']==7 for row in after),after)
        for previous,following in zip(after,after[1:]):
            self.assertLess(following['at'],previous['at']+60.)
        normal=[row for row in result['meter_samples'] if row['at']>=194.2 and float(row['raw'])==18.]
        self.assertGreater(normal[-1]['at']-normal[0]['at'],180.)
        self.assertTrue(all(row['electrical_response_deadline']==200.2 for row in normal),normal[-5:])
        self.assertTrue(all(row['peak_previous_authority']==16 for row in normal),normal[-5:])
        exposure=result['final_exposure']
        self.assertTrue(exposure['peak_response_pending'])
        self.assertEqual(exposure['peak_response_previous_authority'],16)
        self.assertEqual(exposure['normal_control']['stops'],0)


if __name__=='__main__':
    unittest.main()
