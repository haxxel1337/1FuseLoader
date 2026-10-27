"""Independent operating oracles for the standalone electrical controller.

The existing real adapter fixture owns native callbacks, SDK admission, HTTP,
readback and public Store persistence. These tests specify charging, exposure
and immutable deadlines rather than reproducing the selected policy's code.
All data and clocks are synthetic; no Home Assistant/network actions occur.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_b40_controller as actual
import test_1fuseloader_b40_handoff as authority
import test_1fuseloader_runtime as runtime_fixture


class B42StandaloneControllerTests(unittest.IsolatedAsyncioTestCase):
    _base=actual.B40ControlledPhaseControllerTests
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
    fault_zero=_base.fault_zero

    async def saved_fault(self,*,used=35.001,state='blocked',reason='peak_response_timeout'):
        await self.packet(99.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.})
        self.grid(3.,99.)
        saved=self.controller.regulator.export_overload_state()
        # This is a predecessor ledger, written before the new additive
        # electrical checkpoint existed. The old cycle is the migration input.
        saved.pop('meter_control',None)
        saved.update(used_seconds=4000.,failure_reason='response_timeout',
            exposure_active=False,transition_pending=False,minimum_held=False)
        saved['automatic_cycle']=dict(version=1,cycle_id=9,state=state,
            used_seconds=used,source_id=self.controller._read_input().control_source_id,
            failure_reason=reason)
        await self.controller._budget_store.async_save(saved)
        await self.tick(100.,3.,status='awaiting_start')
        return deepcopy(saved)

    async def recover_normal(self,*,used=35.001,state='blocked',reason='peak_response_timeout'):
        saved=await self.saved_fault(used=used,state=state,reason=reason)
        r=self.controller.regulator
        for at in range(105,171,5):
            await self.packet(float(at)-.2,{182:0.,183:0.},source=float(at)-.2)
            await self.tick(float(at),3.,status='awaiting_start')
        self.assertTrue(any(row['cap']==16 for row in self.sdk_calls),self.controller.last_decision)
        normal_dispatch=max(row['at'] for row in self.sdk_calls if row['cap']==16)
        # Actual normal positive acceptance alone is not physical recovery.
        # The independent plant next draws the real accepted offer and then
        # sends a later meter report of house3 + line16.1.
        await self.packet(171.,{109:3,182:16.2,183:16.1},source=171.)
        self.physical_draw=16.1
        await self.tick(172.,19.1,status='charging')
        await self.tick(180.,19.1)
        await self.tick(185.,19.1)
        await self.tick(190.,19.1)
        self.assertLess(normal_dispatch,171.)
        return r,saved

    async def high_load(self,*,ceiling=28.5):
        await self.packet(195.,{182:16.2,183:16.1},source=195.)
        await self.tick(200.771116,31.,reported=200.)
        await self.packet(201.5,{182:8.34,183:7.094,114:7.},source=199.5)
        self.physical_draw=7.094
        response=self.controller.last_decision
        await self.tick(210.,31.,reported=200.)
        confirmed=await self.tick(210.694,ceiling,reported=210.)
        return response,confirmed

    def assert_history_preserved(self,saved,*,same_cycle=True):
        state=self.controller.regulator.export_overload_state()
        self.assertGreaterEqual(state['used_seconds'],saved['used_seconds'])
        self.assertEqual(state['failure_reason'],saved['failure_reason'])
        if same_cycle:
            self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],saved['automatic_cycle']['used_seconds'])
        return state

    async def test_resolved_lightly_used_fault_allows_remaining_overload_without_refill(self):
        r,saved=await self.recover_normal()
        spent_before=r.export_overload_state()['automatic_cycle']['used_seconds']
        self.sdk_calls.clear()
        response,confirmed=await self.high_load()
        # On b41 this reaches an actual overload_not_admitted SDK zero. The
        # same oracle must fail behaviorally, not on an absent new API.
        self.assertIsNone(confirmed.meter_zero_cause,confirmed)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls),self.sdk_calls)
        self.assertEqual(r.last_target[0],7)
        for at in range(215,251,5):
            await self.tick(float(at),28.5)
            self.assertIsNone(self.controller.last_decision.meter_zero_cause)
            self.assertEqual(r.last_target[0],7)
        state=self.assert_history_preserved(saved)
        self.assertGreater(state['automatic_cycle']['used_seconds'],spent_before)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],saved['automatic_cycle']['used_seconds']+40.)
        self.assertAlmostEqual(response.grid_confirmation_deadline_ts,231.5,places=4)
        self.assertIsNone(r.meter_control_diagnostics()['operational_fault'])
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)

    async def test_exhausted_budget_allows_normal_but_not_new_overload(self):
        r,saved=await self.recover_normal(used=1800.,state='exhausted',reason='overload_budget')
        self.sdk_calls.clear()
        _,confirmed=await self.high_load()
        self.assertIsNotNone(confirmed.meter_zero_cause)
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))
        state=self.assert_history_preserved(saved)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],1800.)

    async def test_cached_current_and_ack_do_not_recover_saved_fault(self):
        saved=await self.saved_fault()
        r=self.controller.regulator
        for at in range(105,196,5):
            await self.tick(float(at),3.,status='awaiting_start')
        self.assertTrue(any(row['cap']>0 for row in self.sdk_calls))
        self.sdk_calls.clear()
        # This minimum report is before the actual peak SDK boundary. A new
        # neutral or matching readback does not invent a new phase183 event.
        await self.packet(199.,{109:3,182:8.34,183:7.094},source=199.)
        await self.tick(200.771116,31.,reported=200.,status='charging')
        await self.packet(208.,{182:8.35,114:7.},source=208.)
        stopped=await self.tick(210.,31.,reported=205.)
        self.assertIsNotNone(stopped.meter_zero_cause)
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))
        self.assert_history_preserved(saved)

    async def test_cold_restore_of_real_minimum_hold_requires_new_physical_recovery(self):
        r,saved=await self.recover_normal()
        await self.high_load()
        for at in range(215,251,5):
            await self.tick(float(at),28.5)
        checkpoint=deepcopy(r.export_overload_state())
        self.assertEqual(checkpoint['automatic_cycle']['state'],'active')
        self.assertEqual(checkpoint['meter_control']['state'],'MINIMUM_HOLD')
        old_used=checkpoint['automatic_cycle']['used_seconds']
        await self.asyncTearDown()
        await self.asyncSetUp()
        # The actual exported public checkpoint is the input to a NEW cold
        # controller. Neither the accepted lease nor native proof survives.
        await self.packet(299.,{21:1,38:2,110:10,109:2,
            182:0.,183:0.,184:0.,185:0.})
        self.grid(3.,299.)
        await self.controller._budget_store.async_save(checkpoint)
        await self.tick(300.,3.,status='awaiting_start')
        r=self.controller.regulator
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
        self.assertTrue(r.last_target is None or r.last_target[0]==0)
        for at in range(305,371,5):
            await self.tick(float(at),3.,status='awaiting_start')
        self.assertTrue(any(row['cap']==16 for row in self.sdk_calls))
        # A fresh normal SDK ACK with the same old zero cache is incomplete.
        # It cannot silently restore extra-overload permission after a reboot.
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        previous=len(self.sdk_calls)
        d=await self.tick(375.,31.,status='charging')
        self.assertIsNotNone(d.meter_zero_cause)
        self.assertFalse(any(row['cap']==7 for row in self.sdk_calls[previous:]))
        final=self.assert_history_preserved(saved)
        self.assertGreaterEqual(final['automatic_cycle']['used_seconds'],old_used)

    async def test_ready_cycle_with_global_historical_error_is_not_an_active_fault(self):
        r,saved=await self.recover_normal(used=0.,state='ready',reason=None)
        self.sdk_calls.clear()
        _,d=await self.high_load()
        self.assertIsNone(d.meter_zero_cause)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
        state=self.assert_history_preserved(saved)
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertEqual(state['failure_reason'],'response_timeout')

    async def test_malformed_additive_checkpoint_fails_closed_preserving_old_ledger(self):
        for index,bad in enumerate(({},'broken',{'version':2},{'version':-1})):
            if index:
                await self.asyncTearDown()
                await self.asyncSetUp()
            with self.subTest(checkpoint=bad):
                await self.packet(99.,{21:1,38:2,110:10,109:2,
                    182:0.,183:0.,184:0.,185:0.})
                self.grid(3.,99.)
                saved=self.controller.regulator.export_overload_state()
                saved.update(used_seconds=4000.,failure_reason='response_timeout',
                    exposure_active=False,transition_pending=False,minimum_held=False)
                saved['automatic_cycle']=dict(version=1,cycle_id=9,state='exhausted',
                    used_seconds=1800.,source_id=self.controller._read_input().control_source_id,
                    failure_reason='overload_budget')
                saved['meter_control']=deepcopy(bad)
                await self.controller._budget_store.async_save(saved)
                stored=deepcopy(self.controller._budget_store.store.data)
                for at in (100.,105.,110.):
                    await self.tick(at,3.,status='awaiting_start')
                # Corrupt NEW metadata must neither acquire a lease nor erase
                # the valid prior lifetime and spent-cycle records on disk.
                self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
                state=self.controller.regulator.export_overload_state()
                self.assertGreaterEqual(state['used_seconds'],4000.)
                self.assertEqual(state['failure_reason'],'response_timeout')
                self.assertEqual(state['automatic_cycle']['cycle_id'],9)
                self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],1800.)
                self.assertEqual(self.controller._budget_store.store.data,stored)
                self.assertEqual(self.controller._budget_error,'overload_budget_restore_failed')

    async def test_out_of_range_additive_fields_cannot_create_recovery_authority(self):
        base=deepcopy(self.controller.regulator.export_overload_state()['meter_control'])
        variants=[]
        for value in (0,-1,True,2**53):
            value_state=deepcopy(base)
            value_state['next_episode_id']=value
            variants.append(value_state)
        for value in (-1.,):
            value_state=deepcopy(base)
            value_state['history']=[dict(reason='historic',cycle_id=9,
                episode_id=None,used_seconds=value,resolved=False)]
            variants.append(value_state)
        value_state=deepcopy(base)
        value_state['version']=True
        variants.append(value_state)
        value_state=deepcopy(base)
        value_state['version']=1.0
        variants.append(value_state)
        value_state=deepcopy(base)
        value_state.update(episode_active=True,episode_id=None)
        variants.append(value_state)
        value_state=deepcopy(base)
        value_state.update(operational_fault='peak_response_timeout',recovery_required=False)
        variants.append(value_state)
        for index,bad in enumerate(variants):
            if index:
                await self.asyncTearDown()
                await self.asyncSetUp()
            with self.subTest(checkpoint=bad):
                await self.packet(99.,{21:1,38:2,110:10,109:2,
                    182:0.,183:0.,184:0.,185:0.})
                self.grid(3.,99.)
                saved=self.controller.regulator.export_overload_state()
                saved.update(used_seconds=4000.,failure_reason='response_timeout',
                    exposure_active=False,transition_pending=False,minimum_held=False)
                saved['automatic_cycle']=dict(version=1,cycle_id=9,state='exhausted',
                    used_seconds=1800.,source_id=self.controller._read_input().control_source_id,
                    failure_reason='overload_budget')
                saved['meter_control']=deepcopy(bad)
                await self.controller._budget_store.async_save(saved)
                stored=deepcopy(self.controller._budget_store.store.data)
                for at in (100.,105.,110.):
                    await self.tick(at,3.,status='awaiting_start')
                self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
                state=self.assert_history_preserved(saved)
                self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],1800.)
                self.assertEqual(self.controller._budget_store.store.data,stored)
                self.assertEqual(self.controller._budget_error,'overload_budget_restore_failed')

    async def test_normal_control_does_not_require_experimental_or_legacy_flags(self):
        self.entry.options.update(overload_enabled=False,nominal_import_only_fallback=False,
            bounded_nominal_shedding=False,peak_only_response=False,automatic_overload_cycles=False)
        self.controller._reload_settings_locked()
        r=await self.boot()
        for at in range(200,401,5):
            await self.tick(float(at),21.2)
            self.assertEqual(r.last_target[0],16)
            self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
        self.assertGreaterEqual(len(self.sdk_calls),6)

    async def test_stale_ev_estimate_cannot_interrupt_separately_normal_live_offer(self):
        r=await self.boot()
        # No current event after129. The meter and actual native product
        # heartbeat advance, so EV confidence ages without a critical fault.
        for at in range(200,431,5):
            d=await self.tick(float(at),21.2)
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(r.last_target[0],16)
        self.assertGreaterEqual(len(self.sdk_calls),7)
        self.assertTrue(all(row['cap']==16 and row['ttl']==60 for row in self.sdk_calls))

    async def test_normal_decrease_between_twenty_five_and_twenty_eight_uses_no_overload(self):
        r=await self.boot()
        await self.packet(195.,{182:16.2,183:16.1},source=195.)
        d=await self.tick(200.,26.2)
        self.assertEqual(r.last_target[0],12,d)
        self.assertFalse(d.meter_overload_authority)
        self.assertIsNone(d.meter_zero_cause)
        await self.packet(201.5,{182:12.2,183:12.1},source=201.)
        self.physical_draw=12.1
        d=await self.tick(202.,22.2)
        self.assertIsNone(d.meter_zero_cause)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))

    async def test_genuine_peak_and_minimum_have_one_fixed_grid_deadline(self):
        r=await self.peak()
        response=await self.response()
        deadline=response.grid_confirmation_deadline_ts
        self.assertAlmostEqual(deadline,238.151245,places=4)
        for at in (210.,215.,225.,235.):
            d=await self.tick(at,31.,reported=205.)
            self.assertIsNone(d.meter_zero_cause)
            self.assertAlmostEqual(d.grid_confirmation_deadline_ts,deadline,places=4)
            self.assertEqual(r.last_target[0],7)
        timed_out=await self.tick(deadline+.001,31.,reported=205.)
        self.assertIsNotNone(timed_out.meter_zero_cause)
        self.assertEqual(self.sdk_calls[-1]['cap'],0)

    async def test_hard_peak_and_expired_real_lease_remain_typed_stops(self):
        r=await self.boot()
        hard=await self.tick(200.,38.01)
        self.assertEqual(hard.meter_zero_cause,'hard_limit')
        self.assertEqual(self.sdk_calls[-1]['cap'],0)
        await self.asyncTearDown()
        await self.asyncSetUp()
        r=await self.boot()
        expired=await self.tick(r.last_set_limit_ts+60.01,18.)
        self.assertEqual(expired.meter_zero_cause,'lease_expired')
        self.assertEqual(self.sdk_calls[-1]['cap'],0)

    async def test_late_normal_ack_cannot_move_the_original_positive_ttl(self):
        await self.packet(99.,{21:1,38:2,110:10,109:1,
            182:0.,183:0.,184:0.,185:0.})
        self.sdk_delay=70.
        response=await self.tick(100.,3.,status='disconnected')
        first=next(row for row in self.sdk_calls if row['cap']>0)
        original_expiry=first['at']+first['ttl']
        r=self.controller.regulator
        if r.last_target and r.last_target[0]>0:
            self.assertEqual(r.last_set_limit_ts,first['at'])
            self.assertLessEqual(r.last_set_limit_ts+r.last_target[3],original_expiry)
        # A late HTTP acknowledgement is not a renewal at completion time.
        self.sdk_delay=.122415
        self.assertGreater(self.now,original_expiry)
        self.assertEqual(response.meter_zero_cause,'lease_expired')
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))
        await self.tick(self.now+.1,3.,status='disconnected')
        self.assertFalse(any(row['cap']>0 and row['at']>first['at'] for row in self.sdk_calls))

    async def test_new_peak_at_already_verified_minimum_has_no_second_ten_seconds(self):
        r=await self.peak()
        await self.response()
        await self.tick(210.694,28.5,reported=210.)
        for at in range(215,246,5):
            await self.tick(float(at),28.5)
        await self.packet(248.,{182:8.35,183:7.1},source=248.)
        previous=len(self.sdk_calls)
        d=await self.tick(250.,31.)
        self.assertIsNotNone(d.meter_zero_cause)
        self.assertEqual(self.sdk_calls[-1]['cap'],0)
        self.assertEqual(len(self.sdk_calls),previous+1)
        self.assertFalse(d.nominal_response_deadline_ts and d.nominal_response_deadline_ts>250.)

    async def test_spent_budget_does_not_refill_on_short_normal_dip(self):
        r,saved=await self.recover_normal(used=1800.,state='exhausted',reason='overload_budget')
        for at in range(195,701,5):
            d=await self.tick(float(at),19.1)
            self.assertIsNone(d.meter_zero_cause)
        state=self.assert_history_preserved(saved)
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],1800.)

    async def test_spent_cycle_requires_full_strict_cooling_before_automatic_rearm(self):
        r,saved=await self.recover_normal(used=1800.,state='exhausted',reason='overload_budget')
        # The first qualified low RAW is at100. New meter observations remain
        # strictly below23 and continuous; elapsed wall time alone is not the
        # oracle. Every interval is covered by a new report and native pulse.
        for at in range(195,1896,5):
            await self.tick(float(at),19.1)
        await self.tick(1899.,22.9)
        before=self.assert_history_preserved(saved)
        self.assertEqual(before['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(before['automatic_cycle']['used_seconds'],1800.)
        await self.tick(1900.,22.9)
        after=r.export_overload_state()
        self.assertEqual(after['automatic_cycle']['cycle_id'],10)
        self.assertEqual(after['automatic_cycle']['used_seconds'],0.)
        self.assertEqual(after['used_seconds'],before['used_seconds'])
        self.assertEqual(after['failure_reason'],'response_timeout')
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))

    async def test_exact_twenty_three_breaks_cooling_and_does_not_refill_spent_cycle(self):
        r,saved=await self.recover_normal(used=1800.,state='exhausted',reason='overload_budget')
        for at in range(195,1896,5):
            await self.tick(float(at),19.1)
        await self.tick(1899.,23.)
        # A new low streak starts after this boundary; another minute cannot
        # stand in for the missing continuous1800 seconds.
        for at in range(1900,1961,5):
            await self.tick(float(at),19.1)
        state=self.assert_history_preserved(saved)
        self.assertEqual(state['automatic_cycle']['cycle_id'],9)
        self.assertGreaterEqual(state['automatic_cycle']['used_seconds'],1800.)

    async def test_fresh_source_fault_is_not_optional_ev_estimate_uncertainty(self):
        await self.boot()
        self.product.state['signalRConnected']=False
        fault=await self.tick(200.,3.)
        self.assertIsNotNone(fault.meter_zero_cause)
        self.assertEqual(self.sdk_calls[-1]['cap'],0)
        self.sdk_calls.clear()
        for at in (205.,235.,265.):
            await self.tick(at,3.)
        self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))

    async def test_grid_source_gap_rejects_old_raw_even_with_healthy_stream(self):
        await self.boot()
        d=await self.tick(205.,3.,reported=70.)
        self.assertEqual(d.meter_zero_cause,'grid_invalid')
        self.assertEqual(self.sdk_calls[-1]['cap'],0)

    async def test_manual_and_scheduled_intent_never_get_resume_or_positive_writes(self):
        for status in ('paused','awaiting_scheduled_start','awaiting_authorization'):
            with self.subTest(status=status):
                if status!='paused':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.boot()
                for at in range(200,311,5):
                    await self.tick(float(at),3.,status=status)
                self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
                self.sdk_charger.resume.assert_not_awaited()

    async def test_disconnected_readiness_allocates_only_normal_offer_without_starting_car(self):
        await self.packet(99.,{21:1,38:2,110:10,109:1,182:0.,183:0.,184:0.,185:0.})
        d=await self.tick(100.,3.,status='disconnected')
        self.assertEqual(self.controller.regulator.last_target[0],16,d)
        self.assertEqual(self.physical_draw,0.)
        self.sdk_charger.resume.assert_not_awaited()
        self.assertFalse(d.meter_overload_authority)

    async def test_vehicle_stops_with_positive_pilot_do_not_invent_a_software_zero(self):
        r=await self.boot()
        await self.packet(200.,{109:2,114:16.,120:0.,182:0.,183:0.},source=200.)
        self.physical_draw=0.
        for at in range(205,401,5):
            d=await self.tick(float(at),3.,status='awaiting_start')
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(r.last_target[0],16)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
        self.sdk_charger.resume.assert_not_awaited()

    async def test_legacy_owner_poison_cannot_veto_or_become_selected_authority(self):
        r=await self.boot()
        logic=runtime_fixture.logic
        poison=dict(_normal_shed_failure='legacy_timeout',_normal_shed_deadline=150.,
            _low_grid_pending=True,_low_grid_failure='legacy_timeout',
            _uncertainty_stops=3,_uncertainty_retry_submission_pending=True)
        window=logic.MinimumGridConfirmation(owner='nominal',source_id='old-legacy-source',
            cap=7,deadline=150.,first_ev_ts=self.epoch+140.,floor=self.epoch+140.,
            value=7.1,last_ev_ts=self.epoch+140.,last_grid_ts=self.epoch+139.)
        poison['_minimum_confirmation']=window
        for name,value in poison.items():
            setattr(r,name,value)
        for at in (200.,230.,260.):
            d=await self.tick(at,21.2)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(r.last_target[0],16)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
        for name,value in poison.items():
            self.assertEqual(getattr(r,name),value,name)

    # Preserve independent predecessor oracles for actual adapter phase/replay,
    # unknown-delivery/OFF causality, and >3TTL zero recovery. These use real
    # callbacks and commands rather than private selected-policy fields.
    test_mapping_switch_withdraws_response_credit=_base.test_phase_change_after_good_minimum_does_not_reuse_old_response
    test_unsupported_phase_cannot_supply_l1_response=_base.test_unknown_other_phase_it_and_multiphase_cannot_grant_l1_response
    test_cached_get_cannot_supply_new_l1_response=_base.test_matching_get_snapshot_cannot_relabel_old_183_as_postdispatch_response
    test_old_off_cannot_retire_new_unknown_positive=authority.B40MappedHandoffTests.test_old_off_cannot_retire_a_later_unknown_positive_sixteen
    test_zero_renewals_do_not_slide_normal_recovery=_base.test_repeated_zero_acks_recover_normal_nine_without_clearing_history


if __name__=='__main__':
    unittest.main()
