"""Configured steady ceilings agree at real policy and adapter boundaries.

All meter packets, native telemetry and HTTP responses are synthetic. These
tests establish a real accepted ordinary cap before permitting a decrease;
neither a marker nor a callable alone supplies operating authority.
"""
from dataclasses import replace
import unittest

import test_1fuseloader_b44_quiet_safety as actual


class FeedbackCeilingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = actual.B44QuietSafetyTests(methodName='runTest')
        await self.fixture.asyncSetUp()
        self.p = self.fixture.plant

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def prepared(self, margin=4):
        # This is an explicit profile, not installation migration behavior.
        self.p.entry.options['overload_margin_amps'] = margin
        pulse, native = await self.fixture.prepare_quiet(normal_raw=18.1)
        self.assertEqual(self.p.controller.regulator.settings.overload_margin_amps, margin)
        self.assertEqual(self.p.controller.regulator.last_target[0], 16)
        return pulse, native

    def proposal(self, raw, at=315.):
        self.p.now = at
        self.p.grid(raw, at)
        inp = self.p.controller._read_input()
        r = self.p.controller.regulator
        proposal = r.evaluate(inp)
        self.assertTrue(proposal.meter_feedback_curtailment, proposal)
        return inp, proposal

    async def test_explicit_29_profile_quiet_28_5_requests_real_normal_decrease(self):
        pulse, native = await self.prepared()
        d = await self.fixture.tick(315., 28.5, status='charging')
        self.assertIsNone(d.meter_zero_cause, d)
        self.assertTrue(d.meter_feedback_curtailment)
        self.assertEqual(d.meter_feedback_ceiling_amps, 29.)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [10])
        self.assertEqual(self.p.controller.regulator.last_target[0], 10)
        self.assertFalse(d.meter_overload_authority)
        self.assertEqual(self.p.product.state['latestPulse'], pulse)
        observed = self.p.adapter.native_current_observation
        self.assertEqual(observed.received_at, native.received_at)
        self.assertEqual(observed.observation_sequences, native.observation_sequences)
        self.assertFalse(self.p.physical_interruptions)
        self.p.sdk_charger.pause.assert_not_awaited()
        self.p.sdk_charger.resume.assert_not_awaited()

    async def test_explicit_29_ceiling_boundary_is_inclusive(self):
        await self.prepared()
        d = await self.fixture.tick(315., 29., status='charging')
        self.assertIsNone(d.meter_zero_cause, d)
        self.assertTrue(d.meter_feedback_curtailment)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [10])
        self.assertFalse(d.meter_overload_authority)

    async def test_28_profile_still_uses_original_peak_clock_above_28(self):
        await self.prepared(margin=3)
        d = await self.fixture.tick(315., 28.5, status='charging')
        self.assertIsNone(d.meter_zero_cause, d)
        self.assertFalse(d.meter_feedback_curtailment)
        self.assertEqual(d.current_p1, 7)
        self.assertEqual(d.nominal_response_deadline_ts, 325.)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [7])
        self.assertTrue(d.meter_quiet_peak_reduction)
        self.assertEqual(d.meter_peak_reduction_ceiling_amps, 28.)

    async def test_quiet_peak_uses_original_sample_age_and_never_refills(self):
        await self.prepared(margin=3)
        d = await self.fixture.tick(315., 28.5, reported=314., status='charging')
        self.assertEqual(d.nominal_response_deadline_ts, 324.)
        for at, reported in ((317.,314.), (320.,320.), (323.9,323.9)):
            d = await self.fixture.tick(at, 28.5, reported=reported)
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(d.nominal_response_deadline_ts, 324.)
        d = await self.fixture.tick(324., 28.5)
        self.assertEqual(d.meter_zero_cause, 'peak_response_timeout')
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [7,0])

    async def test_genuine_post_sdk_minimum_and_later_grid_complete_quiet_reduction(self):
        await self.prepared(margin=3)
        d = await self.fixture.tick(315., 35., status='charging')
        self.assertEqual(d.current_p1, 7)
        await self.p.packet(317., {182:7.128,183:7.128}, source=317.)
        d = self.p.controller.last_decision
        self.assertIsNone(d.meter_zero_cause, d)
        self.assertEqual(d.mode, 'meter_grid_wait')
        self.assertEqual(d.grid_confirmation_deadline_ts, 347.)
        d = await self.fixture.tick(318., 27.)
        self.assertIsNone(d.meter_zero_cause, d)
        self.assertEqual(d.current_p1, 7)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertIsNone(d.grid_confirmation_deadline_ts)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [7])
        self.assertFalse(self.p.physical_interruptions)

    async def test_normal_meter_can_retain_exact_seven_without_faking_vehicle_response(self):
        await self.prepared(margin=3)
        await self.fixture.tick(315., 35., status='charging')
        before = self.p.adapter.native_current_observation
        for at in (320.,325.,346.):
            d = await self.fixture.tick(at, 22.)
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(d.current_p1, 7)
        self.assertEqual([row['cap'] for row in self.p.sdk_calls], [7,7])
        self.assertEqual(self.p.adapter.native_current_observation.received_at, before.received_at)
        p = self.p.controller.regulator._get_meter_envelope_policy()
        self.assertIsNone(p.response_ts)
        self.assertIsNone(p.minimum_certificate)
        self.assertEqual(p.episode.deadline, 325.)

    async def test_quiet_ordinary_total_cannot_raise_or_subtract_stale_native_current(self):
        await self.prepared(margin=3)
        d = await self.fixture.tick(315., 24.9, status='charging')
        self.assertEqual(d.current_p1, 14)
        for at in (320.,350.):
            d = await self.fixture.tick(at, 5.)
            self.assertIsNone(d.meter_zero_cause, d)
            self.assertEqual(d.current_p1, 14)
            self.assertFalse(d.controlled_l1_used)
        self.assertTrue(all(row['cap']==14 for row in self.p.sdk_calls))

    async def test_quiet_peak_flag_cannot_replace_actual_dispatch_callback(self):
        await self.prepared(margin=3)
        self.p.now = 315.
        self.p.grid(35., 315.)
        inp = self.p.controller._read_input()
        d = self.p.controller.regulator.evaluate(inp)
        self.assertTrue(d.meter_quiet_peak_reduction)
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(d)
        self.assertFalse(self.p.sdk_calls)

    async def test_forged_quiet_peak_ceiling_or_lease_origin_is_rejected_by_actual_policy(self):
        await self.prepared(margin=3)
        self.p.now = 315.
        self.p.grid(35., 315.)
        inp = self.p.controller._read_input()
        r = self.p.controller.regulator
        d = r.evaluate(inp)
        for forged in (replace(d,meter_peak_reduction_ceiling_amps=29.),
                       replace(d,meter_peak_reduction_lease_origin_ts=d.meter_peak_reduction_lease_origin_ts-1)):
            with self.subTest(forged=forged):
                self.assertFalse(r.can_continue_meter_dispatch(inp,inp,forged))
                with self.assertRaises(self.p.adapter_module.CommandNotSent):
                    await self.p.adapter.async_execute(forged,
                        on_dispatch=lambda: r.can_continue_meter_dispatch(inp,inp,forged))
        self.assertFalse(self.p.sdk_calls)

    async def test_expired_quiet_peak_proposal_cannot_enter_sdk(self):
        await self.prepared(margin=3)
        self.p.now = 315.
        self.p.grid(35.,315.)
        inp = self.p.controller._read_input()
        r = self.p.controller.regulator
        d = r.evaluate(inp)
        self.p.now = 325.
        self.p.grid(35.,325.)
        latest = self.p.controller._read_input()
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(d,
                on_dispatch=lambda: r.can_continue_meter_dispatch(inp,latest,d))
        self.assertFalse(self.p.sdk_calls)

    async def test_quiet_hard_peak_still_stops_immediately(self):
        await self.prepared(margin=3)
        d = await self.fixture.tick(315.,38.1,status='charging')
        self.assertEqual(d.meter_zero_cause,'hard_limit')
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[0])

    async def test_post_minimum_high_grid_uses_original_raw_clock(self):
        await self.prepared(margin=3)
        await self.fixture.tick(315.,35.,status='charging')
        await self.p.packet(317.,{182:7.128,183:7.128},source=317.)
        d=await self.fixture.tick(320.,31.9)
        self.assertEqual(d.mode,'meter_raw_peak_hold')
        self.assertEqual(d.meter_raw_excursion_deadline_ts,325.)
        d=await self.fixture.tick(325.,31.9)
        self.assertEqual(d.meter_zero_cause,'grid_ceiling')
        self.assertEqual([row['cap'] for row in self.p.sdk_calls],[7,0])

    async def test_quiet_mapping_withdrawal_cannot_borrow_old_actual_boundary(self):
        await self.prepared(margin=3)
        self.p.product.state['outputPhase']=20
        d=await self.fixture.tick(315.,35.,status='charging')
        self.assertFalse(d.meter_quiet_peak_reduction)
        self.assertIsNotNone(d.meter_zero_cause)
        self.assertFalse(any(row['cap']>0 for row in self.p.sdk_calls))

    async def test_quiet_transport_failure_cannot_use_peak_reduction_lane(self):
        await self.prepared(margin=3)
        self.p.product.state['signalRConnected']=False
        d=await self.fixture.tick(315.,35.,status='charging')
        self.assertEqual(d.meter_zero_cause,'actuator_invalid')
        self.assertFalse(d.meter_quiet_peak_reduction)
        self.assertFalse(any(row['cap']>0 for row in self.p.sdk_calls))

    async def test_expired_actual_lease_cannot_manufacture_quiet_peak_authority(self):
        await self.prepared(margin=3)
        d=await self.fixture.tick(371.,35.,status='charging')
        self.assertEqual(d.meter_zero_cause,'lease_expired')
        self.assertFalse(d.meter_quiet_peak_reduction)
        self.assertFalse(any(row['cap']>0 for row in self.p.sdk_calls))

    async def test_disabled_profile_cannot_borrow_quiet_peak_reduction(self):
        await self.prepared(margin=3)
        self.p.entry.options['overload_enabled']=False
        self.p.controller._reload_settings_locked()
        d=await self.fixture.tick(315.,35.,status='charging')
        self.assertFalse(d.meter_quiet_peak_reduction)
        self.assertIsNotNone(d.meter_zero_cause)
        self.assertFalse(any(row['cap']>0 for row in self.p.sdk_calls))

    async def test_forged_29_marker_cannot_expand_real_28_profile(self):
        await self.prepared(margin=3)
        inp, d = self.proposal(27.9)
        forged = replace(d, meter_feedback_ceiling_amps=29.)
        r = self.p.controller.regulator
        self.assertFalse(r.can_continue_meter_dispatch(inp, inp, forged))
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(forged,
                on_dispatch=lambda: r.can_continue_meter_dispatch(inp, inp, forged))
        self.assertFalse(self.p.sdk_calls)

    async def test_new_raw_above_configured_ceiling_rejects_old_ordinary_proposal(self):
        await self.prepared()
        inp, d = self.proposal(28.5)
        self.p.now = 316.
        self.p.grid(29.1, 316.)
        latest = self.p.controller._read_input()
        r = self.p.controller.regulator
        self.assertFalse(r.can_continue_meter_dispatch(inp, latest, d))
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(d,
                on_dispatch=lambda: r.can_continue_meter_dispatch(inp, latest, d))
        self.assertFalse(self.p.sdk_calls)

    async def test_marker_without_actual_callback_still_cannot_reach_sdk(self):
        await self.prepared()
        _, d = self.proposal(28.5)
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(d)
        self.assertFalse(self.p.sdk_calls)

    async def test_missing_nonfinite_and_out_of_range_markers_are_rejected(self):
        await self.prepared()
        _, d = self.proposal(28.5)
        for marker in (None, True, float('nan'), float('inf'), 25., 29.1, 38.):
            with self.subTest(marker=marker):
                forged = replace(d, meter_feedback_ceiling_amps=marker)
                with self.assertRaises(self.p.adapter_module.CommandNotSent):
                    await self.p.adapter.async_execute(forged, on_dispatch=lambda: True)
        self.assertFalse(self.p.sdk_calls)

    async def test_non_decrease_cannot_borrow_valid_ceiling_marker(self):
        await self.prepared()
        _, d = self.proposal(28.5)
        forged = replace(d, current_p1=16)
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(forged, on_dispatch=lambda: True)
        self.assertFalse(self.p.sdk_calls)

    async def test_hard_transport_loss_preempts_configured_feedback_ceiling(self):
        await self.prepared()
        _, d = self.proposal(28.5)
        self.p.product.state['signalRConnected'] = False
        with self.assertRaises(self.p.adapter_module.CommandNotSent):
            await self.p.adapter.async_execute(d, on_dispatch=lambda: True)
        self.assertFalse(self.p.sdk_calls)

    async def test_no_write_retention_clears_feedback_authority_marker(self):
        await self.prepared()
        inp, d = self.proposal(28.5)
        held = self.p.controller.regulator.meter_envelope_retention(inp, d)
        self.assertIsNotNone(held)
        self.assertIsNone(held.command)
        self.assertFalse(held.meter_feedback_curtailment)
        self.assertIsNone(held.meter_feedback_ceiling_amps)


if __name__ == '__main__':
    unittest.main()
