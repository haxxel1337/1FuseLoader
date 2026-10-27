"""Provider and native receiver clocks are independent causal domains.

Actual adapter callbacks, runtime, SDK boundary and ACK run against synthetic
sources. A lagging but advancing provider clock cannot veto a genuine fast183
receipt after dispatch; a provider regression still withdraws response credit.
"""
import unittest

import test_1fuseloader_b40_controller as controller_fixtures


class B41ProviderClockControllerTests(unittest.IsolatedAsyncioTestCase):
    _base=controller_fixtures.B40ControlledPhaseControllerTests
    asyncSetUp=_base.asyncSetUp
    asyncTearDown=_base.asyncTearDown
    update_entry=_base.update_entry
    wall=_base.wall
    schedule=_base.schedule
    active_timers=_base.active_timers
    grid=_base.grid
    snapshot=_base.snapshot
    set_native=_base.set_native
    configure=_base.configure
    execute=_base.execute
    project_sdk=_base.project_sdk
    packet=_base.packet
    pump=_base.pump
    tick=_base.tick
    sdk_set=_base.sdk_set
    boot=_base.boot

    async def dispatched_peak(self):
        r=await self.boot()
        # The dispatch captures provider195 as its nonregression baseline.
        # Native receipt time and event sequence are separate actual fields.
        await self.packet(195.1,{182:16.2,183:16.1},source=195.)
        await self.tick(200.771116,31.,reported=200.)
        self.assertAlmostEqual(self.sdk_calls[-1]['at'],200.771116)
        self.assertEqual(self.sdk_calls[-1]['cap'],7)
        self.assertAlmostEqual(r.last_set_limit_ts,200.771116)
        self.assertAlmostEqual(self.controller.last_decision.nominal_response_deadline_ts,210.)
        return r

    async def test_lagging_advancing_provider_does_not_veto_fast_native_response(self):
        for l1_first in (False,True):
            with self.subTest(l1_first=l1_first):
                if l1_first:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r=await self.dispatched_peak()
                packets=((201.5,{183:7.093999863}),(201.502,{182:8.34})) if l1_first else (
                    (201.498788,{182:8.34}),(201.5,{183:7.093999863}))
                for at,values in packets:
                    await self.packet(at,values,source=199.5)
                response=self.controller.last_decision
                # Drive through the ORIGINAL electrical deadline before the
                # assertions so b40 fails on its real SDK0 behaviour, not on a
                # missing new API or an absent optional diagnostic field.
                deadline=await self.tick(210.,31.,reported=200.)
                self.assertIsNone(deadline.meter_zero_cause)
                self.assertEqual(r.last_target[0],7)
                self.assertAlmostEqual(response.grid_confirmation_deadline_ts,231.5)
                self.assertAlmostEqual(deadline.grid_confirmation_deadline_ts,231.5)
                self.assertTrue(all(row['cap']==7 for row in self.sdk_calls))
                self.assertEqual(self.adapter.native_current_observation.current,8.34)
                later=await self.tick(210.694,20.,reported=210.)
                self.assertIsNone(later.meter_zero_cause)
                self.assertIsNone(later.grid_confirmation_deadline_ts)
                self.assertEqual(later.current_p1,10)
                self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
                self.assertFalse(self.physical_interruptions)

    async def test_true_provider_regression_below_dispatch_baseline_still_cannot_qualify(self):
        await self.dispatched_peak()
        await self.packet(201.5,{183:7.093999863,182:8.34},source=194.5)
        await self.packet(202.,{114:7.},source=200.)
        before=self.controller.last_decision
        self.assertIsNone(before.grid_confirmation_deadline_ts)
        stopped=await self.tick(210.,31.,reported=200.)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))


if __name__=='__main__':
    unittest.main()
