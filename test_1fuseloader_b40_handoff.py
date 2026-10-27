"""A policy handoff cannot discard already qualified mapped-L1 evidence."""
from dataclasses import replace
import unittest

import test_1fuseloader_b40_controller as controller_fixtures


class B40MappedHandoffTests(unittest.IsolatedAsyncioTestCase):
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
    peak=_base.peak
    response=_base.response

    async def test_reenable_keeps_actual_mapped_response_and_original_grid_phase(self):
        r=await self.peak()
        response=await self.response()
        original_deadline=response.grid_confirmation_deadline_ts
        original_lease=r.last_set_limit_ts
        calls=list(self.sdk_calls)
        # Public settings handoff, without an intervening legacy evaluation or
        # a different SDK command, source, mapping epoch or current event.
        r.update_settings(replace(r.settings,meter_envelope_control=False))
        r.update_settings(replace(r.settings,meter_envelope_control=True))
        waiting=await self.tick(209.,31.,reported=205.)
        at_old_deadline=await self.tick(210.,31.,reported=205.)
        self.assertIsNone(at_old_deadline.meter_zero_cause)
        self.assertEqual(waiting.grid_confirmation_deadline_ts,original_deadline)
        self.assertEqual(at_old_deadline.grid_confirmation_deadline_ts,original_deadline)
        self.assertEqual(r.last_set_limit_ts,original_lease)
        self.assertEqual(self.sdk_calls,calls)
        later=await self.tick(210.694,20.,reported=210.)
        self.assertIsNone(later.meter_zero_cause)
        self.assertIsNone(later.grid_confirmation_deadline_ts)
        self.assertEqual(later.current_p1,10)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))

    async def test_old_off_cannot_retire_a_later_unknown_positive_sixteen(self):
        self.entry.options['restart_stable_seconds']=15
        self.controller._reload_settings_locked()
        await self.peak()
        await self.tick(210.,31.,reported=205.)
        await self.packet(214.9,{182:0.,183:0.},source=214.9)
        failed=False
        async def unknown_once(row):
            nonlocal failed
            if row['cap']==16 and not failed:
                failed=True
                self.sdk_failure=RuntimeError('synthetic_unknown_positive16')
            elif row['cap']==0:
                self.sdk_failure=None
        self.sdk_hook=unknown_once
        for at in range(215,291,5):
            await self.tick(float(at),3. if at<=230 else 13.3,status='awaiting_start')
        unknown=[row for row in self.sdk_calls if row['cap']==16]
        self.assertEqual(len(unknown),1)
        self.assertEqual(unknown[0]['at'],230.)
        horizon=unknown[0]['at']+unknown[0]['ttl']
        later_positive=[row for row in self.sdk_calls if row['cap']>0 and row['at']>230.]
        self.assertTrue(later_positive,self.controller.last_decision)
        self.assertTrue(all(row['at']>=horizon for row in later_positive),later_positive)
        self.assertEqual(later_positive[0]['cap'],9)

    async def test_audited_legacy_sdk_floor_and_genuine_phase_survive_public_opt_in(self):
        await self.boot()
        await self.controller.async_set_switch('meter_envelope_control',False)
        await self.tick(200.771116,31.,reported=200.)
        await self.tick(205.7,31.,reported=205.)
        legacy=await self.response(neutral=7.0,l1=7.094)
        original_deadline=legacy.grid_confirmation_deadline_ts
        self.assertIsNotNone(original_deadline)
        calls=list(self.sdk_calls)
        await self.controller.async_set_switch('meter_envelope_control',True)
        self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts,
                         original_deadline)
        self.assertEqual(self.sdk_calls,calls)
        waiting=await self.tick(210.,31.,reported=205.)
        self.assertIsNone(waiting.meter_zero_cause)
        self.assertEqual(waiting.grid_confirmation_deadline_ts,original_deadline)
        later=await self.tick(210.694,20.,reported=210.)
        self.assertIsNone(later.meter_zero_cause)
        self.assertIsNone(later.grid_confirmation_deadline_ts)
        self.assertEqual(later.current_p1,10)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))


if __name__=='__main__':
    unittest.main()
