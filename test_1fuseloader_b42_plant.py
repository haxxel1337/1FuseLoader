"""Independent physical/event plant for standalone fault recovery and bounds.

Physical draw, command delivery generations, sparse reports, source clocks,
HTTP acknowledgement, Store latency and TTL are independent simulated events.
The actual controller/Store/dispatch path decides offers. No household data,
live HA, source patch or private selected-state proof injection is involved.
"""
from copy import deepcopy
from itertools import product
import unittest
from unittest.mock import patch

import test_1fuseloader_meter_envelope_model as selected


class RestoredFaultPlant(selected.MeterEnvelopePlant):
    def __init__(self,*,cycle_state='blocked',cycle_used=35.001,
                 cycle_reason='peak_response_timeout',source_fault_at=None,**options):
        self.saved_cycle=dict(version=1,cycle_id=9,state=cycle_state,
            used_seconds=cycle_used,source_id=None,failure_reason=cycle_reason)
        self.last_control_signature=None
        self.source_fault_at=source_fault_at
        super().__init__(**options)

    async def pump(self):
        await super().pump()
        if hasattr(self,'r'):
            diagnostics=self.r.meter_control_diagnostics()
            signature=(diagnostics['state'],diagnostics['operational_fault'],
                diagnostics['recovery_stage'],diagnostics['cycle_id'])
            if signature!=self.last_control_signature:
                self.trace.append(dict(event='STANDALONE',at=self.now,**diagnostics))
                self.last_control_signature=signature

    async def setup(self):
        controller_type=selected.original.fixtures.runtime.OneFuseLoaderController
        original=controller_type.async_start
        cycle=deepcopy(self.saved_cycle)
        async def restore(controller):
            # Establish the predecessor's existing ledger through the real
            # public Store wrapper BEFORE the actual startup load. No active
            # lease, response floor or private recovery flag is injected.
            state=controller.regulator.export_overload_state()
            state.pop('meter_control',None)
            state['automatic_cycle']=deepcopy(cycle)
            # The old ledger already exists before the event simulation. Only
            # later operational writes incur virtual Store delay; allowing a
            # setup write to consume simulated time would race the initial
            # watchdog against startup and manufacture a different ledger.
            backend=controller._budget_store.store
            async def initial_save(payload):
                await type(backend).async_save(backend,payload)
            with patch.object(backend,'async_save',initial_save):
                await controller._budget_store.async_save(state)
            return await original(controller)
        with patch.object(controller_type,'async_start',restore):
            await super().setup()
        if self.source_fault_at is not None:
            self.add(self.source_fault_at,'fault','source')


class B42StandalonePlantTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=selected.MeterEnvelopePlantTests.asyncSetUp
    asyncTearDown=selected.MeterEnvelopePlantTests.asyncTearDown

    def assert_preserved(self,result,*,used=35.001):
        state=result['final_exposure']
        self.assertEqual(state['failure_reason'],'response_timeout')
        self.assertGreaterEqual(state['used_seconds'],4000.)
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],used)
        self.assertFalse(result['background_errors'],result['background_errors'][:1])

    async def test_resolved_fault_plant_keeps_remaining_overload_without_physical_stop(self):
        for ev,http,store in product((0.,6.),(.2,2.),(.2,2.5)):
            options=dict(ready=False,grid_delay=0.,ev_delay=ev,status_delay=0.,
                http_delay=http,store_delay=store,secondary_delay=.002,
                apply_delay=.2,provider_quantum=1.,direct_full=True,
                house_steps=((200.,21.4),),horizon=360.2)
            result=await RestoredFaultPlant(**options).run()
            with self.subTest(options=options):
                self.assertIsNotNone(result['first_positive_ack'])
                self.assertIsNone(result['first_zero'],result['first_zero'])
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertFalse(result['meter_zero_events'],result['meter_zero_events'])
                self.assertLessEqual(result['physical_max_grid'],38.)
                self.assertGreater(result['physical_max_grid'],28.)
                self.assert_preserved(result)
                self.assertGreater(result['final_exposure']['automatic_cycle']['used_seconds'],100.)
                positive=[r for r in result['trace'] if r['event']=='HTTP_BEGIN' and r['cap']>0]
                self.assertTrue(any(r['cap']==7 for r in positive))
                for row in positive:
                    self.assertLessEqual(row['cap'],16)

    async def test_old_fault_does_not_stop_normal_plant_when_ev_reporting_is_very_late(self):
        result=await RestoredFaultPlant(ready=False,direct_full=True,ev_delay=500.,
            http_delay=2.,store_delay=2.5,grid_delay=0.,apply_delay=.2,
            house_steps=((220.,8.2),),horizon=480.2).run()
        self.assertIsNotNone(result['first_positive_ack'])
        self.assertIsNone(result['first_zero'],result['first_zero'])
        self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
        self.assertLessEqual(result['physical_max_grid'],24.2)
        self.assert_preserved(result)
        self.assertEqual(result['final_exposure']['automatic_cycle']['used_seconds'],35.001)
        self.assertGreaterEqual(result['counters'].get('http_replies',0),7)

    async def test_spent_cycle_cannot_gain_overload_from_normal_recovery(self):
        result=await RestoredFaultPlant(cycle_state='exhausted',cycle_used=1800.,
            cycle_reason='overload_budget',ready=False,direct_full=True,ev_delay=.2,
            http_delay=.2,store_delay=.2,grid_delay=0.,apply_delay=.2,
            house_steps=((200.,21.4),),horizon=300.2).run()
        self.assertIsNotNone(result['first_positive_ack'])
        self.assertIsNotNone(result['first_zero'])
        self.assert_preserved(result,used=1800.)
        self.assertGreaterEqual(result['final_exposure']['automatic_cycle']['used_seconds'],1800.)

    async def test_real_hard_peak_and_source_fault_remain_electrical_stops(self):
        for fault,house in (('source',21.4),(None,24.)):
            result=await RestoredFaultPlant(ready=False,direct_full=True,
                ev_delay=.2,http_delay=.2,store_delay=.2,apply_delay=.2,
                house_steps=((200.,house),),fault=fault,horizon=260.2).run()
            with self.subTest(fault=fault,house=house):
                self.assertIsNotNone(result['first_positive_ack'])
                self.assertIsNotNone(result['first_zero'])
                self.assertTrue(result['meter_zero_events'])
                self.assertTrue(all(row['cause'] is not None for row in result['meter_zero_events']))
                self.assert_preserved(result)

    async def test_critical_source_fault_cancels_unsent_pending_reduction(self):
        result=await RestoredFaultPlant(ready=False,direct_full=True,ev_delay=0.,
            http_delay=.2,store_delay=2.5,grid_delay=0.,apply_delay=.2,
            house_steps=((200.,21.4),),source_fault_at=206.,horizon=260.2).run()
        self.assertIsNotNone(result['first_positive_ack'])
        # The peak was observed at205 while the real Store write is pending.
        # A critical source fault at206 invalidates that UNSENT7 A proposal.
        self.assertIsNotNone(result['first_zero'])
        self.assertFalse(any(row['event']=='HTTP_BEGIN' and row['cap']>0
            and row['at']>=206. for row in result['trace']))
        self.assertTrue(any(row['cause']=='actuator_invalid' for row in result['meter_zero_events']))
        self.assert_preserved(result)

    async def test_slow_unknown_minimum_http_does_not_buy_new_response_time(self):
        result=await RestoredFaultPlant(ready=False,direct_full=True,ev_delay=.2,
            http_delay=12.,store_delay=.2,grid_delay=0.,apply_delay=.2,
            house_steps=((220.,21.4),),horizon=300.2).run()
        self.assertIsNotNone(result['first_positive_ack'])
        self.assertIsNotNone(result['first_zero'])
        high=[row for row in result['meter_samples'] if float(row['raw'])>28.]
        self.assertTrue(high)
        original_deadline=min(row['grid_report_ts']-selected.original.EPOCH for row in high)+10.
        self.assertLessEqual(result['first_zero']['at'],original_deadline+.2)
        self.assert_preserved(result)


if __name__=='__main__':
    unittest.main()
