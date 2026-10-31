"""Quiet data dispatch boundaries use real adapter/controller, synthetic I/O."""
from dataclasses import replace
import unittest

import test_1fuseloader_b44_quiet_safety as fixture


class B44QuietDispatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.B44QuietSafetyTests.asyncSetUp
    asyncTearDown=fixture.B44QuietSafetyTests.asyncTearDown
    bind_real_exception_identity=fixture.B44QuietSafetyTests.bind_real_exception_identity
    tick=fixture.B44QuietSafetyTests.tick
    quiet_sdk=fixture.B44QuietSafetyTests.quiet_sdk
    prepare_quiet=fixture.B44QuietSafetyTests.prepare_quiet

    async def test_quiet_permission_requires_the_real_controller_dispatch_boundary(self):
        await self.prepare_quiet()
        d=await self.tick(315.,5.9)
        self.assertTrue(d.meter_quiet_normal_retention)
        with self.assertRaises(self.plant.adapter_module.CommandNotSent):
            await self.plant.adapter.async_execute(d)
        self.assertFalse(self.plant.sdk_calls)

    async def test_forged_quiet_flag_cannot_allow_a_different_cap_or_resume(self):
        await self.prepare_quiet()
        d=await self.tick(315.,5.9)
        for forged in (replace(d,current_p1=7),replace(d,current_p1=17),replace(d,command='resume')):
            with self.subTest(cap=forged.current_p1,command=forged.command):
                with self.assertRaises(self.plant.adapter_module.CommandNotSent):
                    await self.plant.adapter.async_execute_with_dispatch(forged,lambda:True)
        self.assertFalse(self.plant.sdk_calls)
        self.plant.sdk_charger.resume.assert_not_awaited()

    async def test_quiet_data_does_not_destroy_phase_scope_or_invent_new_current(self):
        pulse,clock=await self.prepare_quiet()
        p=self.plant
        epoch=p.adapter._controlled_mapping_epoch
        await self.tick(315.,5.9)
        self.assertIsNone(p.adapter.native_controlled_l1_observation)
        self.assertEqual(p.adapter._controlled_mapping_epoch,epoch)
        self.assertEqual(p.product.state['latestPulse'],pulse)
        self.assertEqual(p.adapter.native_current_observation.received_at,clock.received_at)
        self.assertEqual(p.controller.source_diagnostics['source_health'],'degraded')
        self.assertEqual(p.controller.source_diagnostics['easee_source_health'],'easee_product_data_quiet')
        self.assertTrue(p.controller.source_diagnostics['meter_actuator_valid'])
        self.assertFalse(p.controller.source_diagnostics['meter_ev_valid'])

    async def test_fresh_actual_feedback_during_quiet_http_is_not_unknown_delivery(self):
        await self.prepare_quiet()
        p=self.plant
        async def feedback(row):
            if row['cap']!=16:return
            at=p.now
            await p.packet(at+.05,{109:3,182:16.2,183:16.1},source=at+.04,drain=False)
            p.now=at+.07
            p.grid(19.1,at+.06)
        p.sdk_hook=feedback
        d=await self.tick(340.,5.9)
        self.assertIsNone(d.meter_zero_cause,d)
        self.assertTrue(p.sdk_calls)
        self.assertTrue(all(row['cap']==16 for row in p.sdk_calls),p.sdk_calls)
        self.assertIsNone(p.controller.regulator.meter_control_diagnostics()['operational_fault'])
        self.assertFalse(p.controller._protective_zero_pending)


if __name__=='__main__':unittest.main()
