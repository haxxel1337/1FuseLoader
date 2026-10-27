"""Independent b40 controller/plant checks using actual adapter callbacks.

The native ProductUpdate stream, HA projections, SDK transport, meter and time
are synthetic. Adapter, runtime, Store, admission, dispatch and ACK code are
real. No private core proof state or installation history is seeded.
"""
import asyncio
from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_connected_full_readiness_controller as connected
import test_1fuseloader_clock_provenance_adapter as native
import test_1fuseloader_runtime as fixtures


class B40ControlledPhaseControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = connected.ConnectedFullReadinessControllerTests.update_entry
    wall = connected.ConnectedFullReadinessControllerTests.wall
    schedule = connected.ConnectedFullReadinessControllerTests.schedule
    active_timers = connected.ConnectedFullReadinessControllerTests.active_timers
    grid = connected.ConnectedFullReadinessControllerTests.grid
    snapshot = connected.ConnectedFullReadinessControllerTests.snapshot
    set_native = connected.ConnectedFullReadinessControllerTests.set_native
    configure = connected.ConnectedFullReadinessControllerTests.configure
    execute = connected.ConnectedFullReadinessControllerTests.execute

    async def asyncSetUp(self):
        await connected.ConnectedFullReadinessControllerTests.asyncSetUp(self)
        self.entry.options.update(meter_envelope_control=True,
            measurement_settle_seconds=25)
        self.sdk_calls = []
        self.sdk_hook = None
        self.sdk_failure = None
        self.sdk_delay = .122415
        self.connection = native.Connection()
        self.product = SimpleNamespace(state={
            'signalRConnected': True, 'isOnline': True, 'latestPulse': self.wall(),
            **{f'inCurrentT{n}': 0. for n in (2, 3, 4, 5)},
            **{f'dynamicCircuitCurrentP{n}': 0 for n in (1, 2, 3)},
            **{f'offlineMaxCircuitCurrentP{n}': 0 for n in (1, 2, 3)}})
        self.sdk_circuit = SimpleNamespace(set_dynamic_current=AsyncMock(side_effect=self.sdk_set))
        self.sdk_charger = SimpleNamespace(id='synthetic-b40-charger',
            circuit=self.sdk_circuit, easee=SimpleNamespace(sr_connection=self.connection),
            pause=AsyncMock(), resume=AsyncMock(),
            get_observations=AsyncMock(side_effect=AssertionError('No polling in this plant')))
        self.product.product = self.sdk_charger
        self.native_controller = SimpleNamespace(
            get_chargers=lambda: [self.sdk_charger], chargers_data=[self.product])
        self.hass.data['easee'] = {'controller': self.native_controller}
        helpers = ModuleType('homeassistant.helpers')
        device = SimpleNamespace(identifiers={('easee', self.sdk_charger.id)})
        helpers.device_registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(async_get=lambda _: device))
        easee_const = ModuleType('custom_components.easee.const')
        easee_const.VERSION = '0.9.74'
        sdk = ModuleType('pyeasee')
        sdk.__version__ = '0.8.17'
        self.module_patch = patch.dict(sys.modules, {
            'homeassistant': ModuleType('homeassistant'),
            'homeassistant.helpers': helpers,
            'custom_components.easee.const': easee_const, 'pyeasee': sdk})
        self.module_patch.start()
        self.adapter_module = sys.modules[fixtures.PACKAGE + '.easee_adapter']
        self.adapter_clock_patch = patch.object(self.adapter_module, 'datetime', fixtures.runtime.datetime)
        self.adapter_clock_patch.start()
        self.adapter_mono_patch = patch.object(self.adapter_module, '_monotonic', lambda: self.now)
        self.adapter_mono_patch.start()
        self.connection.on('ProductUpdate', self.project_sdk)
        self.adapter = self.adapter_module.EaseeCommandAdapter(self.hass, self.entry.data['device_id'])
        self.adapter.async_read_current_snapshot = None
        self.background_errors = []
        self.background_tasks = set()
        def create_task(coroutine):
            name = getattr(getattr(coroutine,'cr_code',None),'co_name',None)
            task = asyncio.create_task(coroutine)
            self.background_tasks.add(task)
            def finished(done):
                self.background_tasks.discard(done)
                if not done.cancelled():
                    error=done.exception()
                    # Transport-task failures are awaited and classified by
                    # the controller; refresh failures otherwise disappear
                    # when its finally block clears _refresh_task.
                    if error is not None and name=='_async_process_refreshes':
                        self.background_errors.append(error)
            task.add_done_callback(finished)
            return task
        self.hass.async_create_task = create_task
        self.controller = fixtures.runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        self.controller._configured_control_identity = Mock(return_value='synthetic-b40-selected-source')
        self.controller._budget_store.clock = lambda: self.epoch + self.now
        self.callback_count = 0
        handler = self.controller._handle_source_change
        def observed(event):
            self.callback_count += 1
            return handler(event)
        self.controller._handle_source_change = observed
        self.physical_draw = 0.
        self.physical_interruptions = []

    async def asyncTearDown(self):
        try:
            await connected.ConnectedFullReadinessControllerTests.asyncTearDown(self)
            await asyncio.sleep(0)
            self.assertFalse(self.background_errors,self.background_errors)
        finally:
            self.adapter_mono_patch.stop()
            self.adapter_clock_patch.stop()
            self.module_patch.stop()

    async def project_sdk(self, records):
        keys = {21: 'detectedPowerGridType', 38: 'phaseMode', 109: 'chargerOpMode',
                110: 'outputPhase', 114: 'outputCurrent', 120: 'totalPower'}
        for record in records:
            key = record['id']
            name = (f'inCurrentT{key-180}' if key in (182, 183, 184, 185)
                    else f'dynamicCircuitCurrentP{key-110}' if key in (111, 112, 113)
                    else keys.get(key))
            if name:
                self.product.state[name] = float(record['value']) if record['dataType'] == 3 else record['value']
        self.product.state['latestPulse'] = self.wall()
        maximum = max(self.product.state[f'inCurrentT{n}'] for n in (2, 3, 4, 5))
        self.states['sensor.car'].state = str(maximum)
        self.states['sensor.car'].last_reported = self.wall()
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()

    async def packet(self, at, values, *, source=None, drain=True):
        self.now = at
        records = [dict(mid=self.sdk_charger.id, id=key,
            dataType=4 if key in (21, 38, 109, 110) else 3, value=value,
            timestamp=self.wall(at if source is None else source).isoformat())
            for key, value in values.items()]
        # Resolving the real adapter installs its additive listener after the
        # synthetic SDK's own registered projection listener.
        self.adapter.observation_clock_identity
        for handler in tuple(self.connection.handlers['ProductUpdate']):
            await handler(records)
        if drain:
            await self.pump()

    async def pump(self):
        for _ in range(40):
            await asyncio.sleep(0)
            task = self.controller._refresh_task
            if task is None or task.done():
                if task is not None:
                    await task
                self.assertFalse(self.background_errors,self.background_errors)
                return
        raise AssertionError('Synthetic controller refresh did not settle')

    async def tick(self, at, raw, *, reported=None, status=None):
        self.now = at
        if status is not None:
            previous = self.states['sensor.status'].state
            self.states['sensor.status'].state = status
            if previous != status:
                self.states['sensor.status'].last_changed = self.wall()
        self.grid(raw, at if reported is None else reported)
        self.product.state['latestPulse'] = self.wall()
        self.circuit.attributes['state_latestPulse'] = self.wall().isoformat()
        if not self.controller._budget_loaded:
            await self.controller.async_start()
        else:
            await self.controller.async_tick()
        await self.pump()
        return self.controller.last_decision

    async def sdk_set(self, p1, p2, p3, minutes):
        row = dict(at=self.now, cap=p1, phases=(p1, p2, p3), ttl=minutes*60)
        self.sdk_calls.append(row)
        if self.sdk_hook is not None:
            await self.sdk_hook(row)
        if self.sdk_failure is not None:
            raise self.sdk_failure
        if self.physical_draw > .5 and p1 == 0:
            self.physical_interruptions.append(self.now)
        self.physical_draw = min(self.physical_draw, float(p1))
        self.now += self.sdk_delay
        self.circuit.state = str(p1)
        self.circuit.attributes.update(state_dynamicCircuitCurrentP1=p1,
            state_dynamicCircuitCurrentP2=p2, state_dynamicCircuitCurrentP3=p3)
        self.circuit.last_reported = self.wall()
        await self.packet(self.now, {111:p1, 112:p2, 113:p3}, drain=False)
        return SimpleNamespace(status=200, release=Mock())

    async def boot(self, mode=10, maximum=16):
        if maximum != 16:
            self.entry.options['max_charge_amps'] = maximum
            self.controller._reload_settings_locked()
        await self.packet(99., {21:1, 38:2, 110:mode, 109:1,
            182:0., 183:0., 184:0., 185:0.})
        await self.tick(100., 3., status='disconnected')
        self.assertEqual(self.controller.regulator.last_target[0], maximum,
                         self.controller.last_decision)
        await self.packet(129., {109:3, 182:16.2, 183:16.1})
        self.physical_draw = 16.1
        await self.tick(130., 21.2, status='charging')
        await self.tick(160., 21.2)
        await self.tick(190., 21.2)
        self.sdk_calls.clear()
        self.physical_interruptions.clear()
        return self.controller.regulator

    async def peak(self, mode=10):
        r = await self.boot(mode)
        await self.tick(200.771116, 31., reported=200.)
        self.assertEqual(self.sdk_calls[-1]['cap'], 7, self.controller.last_decision)
        self.assertAlmostEqual(self.sdk_calls[-1]['at'], 200.771116)
        self.assertAlmostEqual(r.last_set_limit_ts, 200.771116)
        self.assertAlmostEqual(self.controller.last_decision.nominal_response_deadline_ts, 210.)
        await self.tick(205.7, 31., reported=205.)
        return r

    async def response(self, *, neutral=8.34, l1=7.093999863, reverse=False):
        values = ((183,l1,208.151245),(182,neutral,208.156)) if reverse else (
            (182,neutral,208.148788),(183,l1,208.151245))
        for key, value, at in values:
            await self.packet(at, {key:value}, source=207.)
        await self.packet(208.156850, {114:7.}, source=207.)
        await self.pump()
        return self.controller.last_decision

    async def test_real_callbacks_qualify_l1_before_ten_seconds_then_delayed_grid_confirms(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                if reverse:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.peak()
                before = self.callback_count
                response = await self.response(reverse=reverse)
                self.assertGreater(self.callback_count, before)
                self.assertIsNone(response.meter_zero_cause)
                self.assertEqual(r.last_target[0], 7)
                self.assertEqual(self.adapter.native_current_observation.current, 8.34)
                at_deadline = await self.tick(210., 31., reported=205.)
                self.assertIsNone(at_deadline.meter_zero_cause)
                self.assertTrue(all(row['cap']<=7 for row in self.sdk_calls))
                self.assertIsNotNone(response.grid_confirmation_deadline_ts)
                self.assertAlmostEqual(response.grid_confirmation_deadline_ts, 238.151245, places=4)
                self.assertAlmostEqual(at_deadline.grid_confirmation_deadline_ts, 238.151245, places=4)
                later = await self.tick(210.694, 20., reported=210.)
                self.assertIsNone(later.meter_zero_cause)
                self.assertIsNone(later.grid_confirmation_deadline_ts)
                self.assertEqual(later.current_p1,10)
                self.assertAlmostEqual(later.house_load_l1,20.-7.093999863,places=5)
                self.assertTrue(any(row['cap']==10 for row in self.sdk_calls))
                self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
                self.assertFalse(self.physical_interruptions)

    async def test_reversed_neutral_and_l1_never_use_the_smaller_terminal(self):
        await self.peak()
        await self.response(neutral=7.094, l1=8.34)
        stopped = await self.tick(210., 31., reported=205.)
        self.assertEqual(stopped.meter_zero_cause, 'peak_response_timeout')
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))

    async def test_independent_house_and_draw_plant_stays_normal_after_genuine_peak_response(self):
        await self.peak()
        await self.response()
        house=14.9
        # This independent plant defines actual L1 draw and an unrelated
        # larger calculated-neutral channel. Its meter is house+L1, never MAX.
        draw=7.093999863
        first=await self.tick(210.694,house+draw,reported=210.)
        self.assertIsNone(first.meter_zero_cause)
        normal_max=0.
        for at in range(215,436,5):
            applied=self.sdk_calls[-1]['cap']
            self.assertGreater(applied,0)
            draw=applied+.094
            self.physical_draw=draw
            actual_total=house+draw
            normal_max=max(normal_max,actual_total)
            self.assertLessEqual(actual_total,23.)
            await self.packet(float(at)-.2,{182:draw+1.246,183:draw},source=float(at)-1.)
            decision=await self.tick(float(at),actual_total)
            self.assertIsNone(decision.meter_zero_cause)
            self.assertLessEqual(house+decision.current_p1+.094,23.)
        self.assertGreater(normal_max,22.)
        self.assertFalse(self.physical_interruptions)
        self.assertFalse(any(row['cap']==0 for row in self.sdk_calls))
        self.assertGreaterEqual(len(self.sdk_calls),7)

    async def test_unknown_other_phase_it_and_multiphase_cannot_grant_l1_response(self):
        for mode in (0, 11, 12, 14, 20, 21, 22, 30):
            with self.subTest(mode=mode):
                if mode != 0:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.peak(mode)
                await self.response()
                stopped = await self.tick(210., 31., reported=205.)
                self.assertEqual(stopped.meter_zero_cause, 'peak_response_timeout')

    async def test_phase_change_after_good_minimum_does_not_reuse_old_response(self):
        await self.peak()
        response = await self.response()
        original = response.grid_confirmation_deadline_ts
        await self.packet(209., {110:12}, source=209.)
        decision = await self.tick(210., 31., reported=205.)
        self.assertIsNotNone(decision.meter_zero_cause)
        self.assertFalse(decision.grid_confirmation_deadline_ts and
                         decision.grid_confirmation_deadline_ts > original)

    async def test_old_183_plus_new_neutral_pilot_and_phase_packets_do_not_make_response(self):
        await self.boot()
        await self.packet(199., {183:7.094, 182:8.34}, source=199.)
        await self.tick(200.771116,31.,reported=200.)
        await self.packet(208., {110:10,182:8.35,114:7.}, source=208.)
        stopped = await self.tick(210.,31.,reported=205.)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')

    async def test_matching_get_snapshot_cannot_relabel_old_183_as_postdispatch_response(self):
        await self.boot()
        await self.packet(199.,{183:7.094,182:7.0},source=199.)
        await self.tick(200.771116,31.,reported=200.)
        self.now=208.
        self.controller._charger_snapshot=SimpleNamespace(current=7.094,status='charging',
            received_at=self.wall(),source_identity=self.adapter.observation_clock_identity,
            current_source_timestamp=self.wall(),status_source_timestamp=self.wall(),
            current_source_timestamps=((183,self.wall()),),source_timestamp_error=None,
            current_source_observation_ids=(183,))
        self.controller._charger_snapshot_selection=self.controller._charger_source_revision()
        stopped=await self.tick(210.,31.,reported=205.)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')
        self.sdk_charger.get_observations.assert_not_awaited()

    async def test_phase_switch_during_sdk_does_not_manufacture_a_later_mapping_floor(self):
        await self.boot()
        async def changed(row):
            if row['cap']==7:
                await self.packet(201., {110:12}, source=201., drain=False)
        self.sdk_hook=changed
        await self.tick(200.771116,31.,reported=200.)
        self.sdk_hook=None
        await self.response()
        stopped=await self.tick(210.,31.,reported=205.)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')

    async def test_mapping_current_packet_orders_and_replayed_mode_cannot_rebind_old_dispatch(self):
        for current_first in (False, True):
            with self.subTest(current_first=current_first):
                if current_first:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.peak()
                await self.packet(206., {110:12}, source=206.)
                if current_first:
                    await self.packet(207., {183:7.094,182:8.34}, source=207.)
                    await self.packet(208., {110:10}, source=208.)
                else:
                    await self.packet(207., {110:10}, source=207.)
                    await self.packet(208., {183:7.094,182:8.34}, source=208.)
                # An older mode10 replay cannot grant the original dispatch's
                # retired mapping epoch, even with fresh lower183 afterwards.
                await self.packet(208.5, {110:10}, source=99.)
                stopped=await self.tick(210.,31.,reported=205.)
                self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')

    async def test_maximum_neutral_cannot_supply_unsafe_house_subtraction_or_rise(self):
        for mode in (10,0):
            with self.subTest(mode=mode):
                if mode != 10:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r=await self.boot(mode,maximum=7)
                self.entry.options['max_charge_amps']=16
                self.controller._reload_settings_locked()
                await self.packet(195.,{182:16.2},source=195.)
                await self.packet(195.01,{183:7.094},source=195.)
                self.sdk_calls.clear()
                decision=await self.tick(196.,22.5)
                self.assertIsNone(decision.meter_zero_cause)
                self.assertLessEqual(decision.current_p1,7)
                self.assertFalse(any(row['cap']>7 for row in self.sdk_calls))
                self.assertEqual(self.adapter.native_current_observation.current,16.2)
                self.assertGreaterEqual(r._get_meter_envelope_policy().upper,r.last_target[0])

    async def test_independently_mapped_l1_with_later_raw_can_grant_normal_rise(self):
        await self.boot(maximum=7)
        self.entry.options['max_charge_amps']=16
        self.controller._reload_settings_locked()
        await self.packet(195.,{182:8.34,183:7.094},source=195.)
        self.sdk_calls.clear()
        decision=await self.tick(196.,12.1)
        self.assertIsNone(decision.meter_zero_cause)
        self.assertEqual(decision.current_p1,16)
        self.assertTrue(any(row['cap']==16 for row in self.sdk_calls))

    async def test_mapped_minimum_does_not_override_hard_peak_or_real_lease_expiry(self):
        await self.peak()
        await self.response()
        hard=await self.tick(209.,38.01)
        self.assertEqual(hard.meter_zero_cause,'hard_limit')
        await self.asyncTearDown()
        await self.asyncSetUp()
        r=await self.boot()
        expiry=r.last_set_limit_ts+60.
        expired=await self.tick(expiry+1.,18.)
        self.assertEqual(expired.meter_zero_cause,'lease_expired')

    async def test_normal_uncertainty_holds_real_seven_across_ttls_without_refunding_old_peak(self):
        r=await self.peak()
        original=r._peak_response
        for at in range(210,431,5):
            await self.tick(float(at),18.)
            self.assertIsNone(self.controller.last_decision.meter_zero_cause)
            self.assertEqual(r.last_target[0],7)
            self.assertEqual(r._peak_response.deadline,original.deadline)
        returned=await self.tick(435.,28.5)
        self.assertEqual(returned.meter_zero_cause,'peak_response_timeout')
        self.assertAlmostEqual(r._peak_response.deadline,210.)

    async def fault_zero(self):
        r=await self.peak()
        stopped=await self.tick(210.,31.,reported=205.)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')
        self.assertEqual(r.last_target[0],0)
        self.sdk_calls.clear()
        return r,deepcopy(r.export_overload_state())

    async def test_repeated_zero_acks_recover_normal_nine_without_clearing_history(self):
        r,before=await self.fault_zero()
        cooled=None
        for at in range(215,611,5):
            await self.packet(float(at)-.1,{182:0.,183:0.},source=float(at)-.1)
            await self.tick(float(at),13.3,status='awaiting_start')
            if cooled is None:
                cooled=deepcopy(r.export_overload_state())
        positives=[row for row in self.sdk_calls if row['cap']>0]
        self.assertTrue(positives,self.controller.last_decision)
        self.assertEqual(positives[0]['cap'],9)
        self.assertLessEqual(positives[0]['at'],400.)
        after=r.export_overload_state()
        self.assertEqual(after['failure_reason'],before['failure_reason'])
        self.assertEqual(after['automatic_cycle']['state'],'blocked')
        self.assertEqual(after['automatic_cycle']['failure_reason'],
                         before['automatic_cycle']['failure_reason'])
        self.assertGreaterEqual(after['used_seconds'],before['used_seconds'])
        self.assertEqual(after['used_seconds'],cooled['used_seconds'])
        self.assertEqual(after['automatic_cycle']['used_seconds'],
                         cooled['automatic_cycle']['used_seconds'])
        self.assertGreaterEqual(len(positives),6)
        self.assertFalse(any(row['phases'][1:]!=(0,0) or row['ttl']!=60 for row in self.sdk_calls))

    async def test_postzero_pause_bad_headroom_and_source_fault_never_restart(self):
        for problem in ('paused','scheduled','headroom','source'):
            with self.subTest(problem=problem):
                if problem != 'paused':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                await self.fault_zero()
                for at in range(215,611,5):
                    if problem=='source':
                        self.product.state['signalRConnected']=False
                    await self.tick(float(at),20. if problem=='headroom' else 13.3,
                        status='paused' if problem=='paused' else 'awaiting_scheduled_start'
                        if problem=='scheduled' else 'awaiting_start')
                self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
                self.sdk_charger.resume.assert_not_awaited()

    async def test_unknown_positive_delivery_is_not_a_new_live_or_recovered_lease(self):
        r=await self.boot()
        previous=(r.last_target,r.last_set_limit_ts)
        self.sdk_failure=RuntimeError('synthetic_unknown_sdk_outcome')
        await self.tick(200.771116,31.,reported=200.)
        self.assertEqual((r.last_target,r.last_set_limit_ts),previous)
        self.sdk_failure=None
        self.sdk_calls.clear()
        for at in range(205,401,5):
            await self.tick(float(at),13.3,status='awaiting_start')
        self.assertFalse(any(row['cap']>0 for row in self.sdk_calls))
        self.assertTrue(any(row['cap']==0 for row in self.sdk_calls))


if __name__=='__main__':
    unittest.main()
