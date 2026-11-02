"""Actual adapter/controller regressions for asynchronous normal feedback.

The synthetic plant supplies ordered native receipts and advancing RAW meter
samples, without assuming their physical acquisitions were simultaneous. Its
accepted normal cap must not chatter during a delayed startup/ramp report.
Electrical, source, finite-authority and explicit configuration guards remain
independent. These tests do not explain an uncommanded native pilot zero.
"""
import unittest

import test_1fuseloader_b40_controller as actual


class B45NormalFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plant = actual.B40ControlledPhaseControllerTests(methodName='runTest')
        await self.plant.asyncSetUp()
        # All paired receipts below fit the production default settle window.
        self.plant.entry.options['measurement_settle_seconds'] = 10
        self.plant.controller._reload_settings_locked()

    async def asyncTearDown(self):
        await self.plant.asyncTearDown()

    async def frame(self, at, raw, ev):
        await self.plant.packet(at-.2, {182:ev, 183:ev},
                                source=at-.4, drain=False)
        return await self.plant.tick(at, raw, reported=at, status='charging')

    def assert_no_session_control(self):
        self.plant.sdk_charger.pause.assert_not_awaited()
        self.plant.sdk_charger.resume.assert_not_awaited()
        self.assertFalse(self.plant.physical_interruptions)

    async def test_advancing_normal_meter_does_not_chatter_on_delayed_ramp_receipts(self):
        r = await self.plant.boot()
        origin = r.last_set_limit_ts
        expiry = r._get_meter_envelope_policy().lease.expires
        # RAW is already observing the established full draw. Received EV
        # packets describe different points in the preceding ramp/response.
        # Every grid timestamp advances, so an old-meter hold cannot hide it.
        for at, ev in ((200.,5.6), (205.,16.), (210.,10.), (215.,16.)):
            with self.subTest(at=at, ev=ev):
                d = await self.frame(at, 18.1, ev)
                self.assertIsNone(d.meter_zero_cause)
                self.assertFalse(d.meter_overload_authority)
                self.assertEqual(r.last_target[0], 16)
                self.assertEqual(self.plant.sdk_calls, [],
                    'Normal delayed EV must not add down/up SDK commands')
                self.assertEqual(r.last_set_limit_ts, origin)
                self.assertEqual(r._get_meter_envelope_policy().lease.expires, expiry)
        # Retention did not invent a renewal. Once genuinely due, one normal
        # 16A SDK renewal still carries the original finite60-second contract.
        d = await self.frame(221., 18.1, 16.)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(len(self.plant.sdk_calls), 1)
        self.assertEqual(self.plant.sdk_calls[0]['phases'], (16,0,0))
        self.assertEqual(self.plant.sdk_calls[0]['ttl'], 60)
        self.assert_no_session_control()

    async def test_buffered_boundary_retains_the_live_normal_cap(self):
        r = await self.plant.boot()
        d = await self.frame(200., 23., 5.6)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual(self.plant.sdk_calls, [])
        self.assert_no_session_control()

    async def test_new_meter_above_buffer_still_sheds_measured_house_load(self):
        r = await self.plant.boot()
        d = await self.frame(200., 23.1, 16.)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(r.last_target[0], 15)
        self.assertEqual(self.plant.sdk_calls[-1]['cap'], 15)
        self.assert_no_session_control()

    async def test_nominal_fuse_boundary_still_permits_an_ordinary_lower_offer(self):
        r = await self.plant.boot()
        d = await self.frame(200., 25., 16.)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(self.plant.sdk_calls[-1]['cap'], 14)
        self.assert_no_session_control()

    async def test_original_peak_response_deadline_still_preempts_normal_retention(self):
        r = await self.plant.boot()
        d = await self.frame(200., 31., 16.)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(d.nominal_response_deadline_ts, 210.)
        stopped = await self.plant.tick(211., 31., reported=211., status='charging')
        self.assertEqual(stopped.meter_zero_cause, 'peak_response_timeout')
        self.assertEqual(self.plant.sdk_calls[-1]['cap'], 0)

    async def test_hard_total_limit_keeps_its_immediate_stop_authority(self):
        await self.plant.boot()
        d = await self.frame(200., 38.1, 5.6)
        self.assertEqual(d.meter_zero_cause, 'hard_limit')
        self.assertEqual(self.plant.sdk_calls[-1]['cap'], 0)

    async def test_unavailable_grid_is_not_treated_as_normal_headroom(self):
        await self.plant.boot()
        self.plant.now = 200.
        grid = self.plant.states['sensor.grid']
        grid.state = 'unavailable'
        grid.last_reported = self.plant.wall()
        await self.plant.controller.async_tick()
        await self.plant.pump()
        d = self.plant.controller.last_decision
        self.assertEqual(d.meter_zero_cause, 'grid_invalid')
        self.assertEqual(self.plant.sdk_calls[-1]['cap'], 0)

    async def test_expired_authority_cannot_be_revived_by_a_normal_meter(self):
        r = await self.plant.boot()
        expiry = r._get_meter_envelope_policy().lease.expires
        await self.frame(expiry+1., 18.1, 16.)
        # A following callback may already publish recovery-wait. Preserve
        # the actual protective dispatch and its separate current fault.
        self.assertTrue(any(row['cap'] == 0 for row in self.plant.sdk_calls))
        self.assertEqual(r._get_meter_envelope_policy().operational_fault,
                         'lease_expired')

    async def test_explicit_lower_configured_maximum_is_not_overridden(self):
        r = await self.plant.boot()
        d = await self.frame(200., 18.1, 16.)
        self.assertIsNone(d.meter_zero_cause)
        await self.plant.controller.async_set_option('max_charge_amps', 12)
        await self.plant.pump()
        self.assertLessEqual(r.last_target[0], 12)
        self.assertTrue(self.plant.sdk_calls)
        self.assertLessEqual(self.plant.sdk_calls[-1]['cap'], 12)
        self.assertEqual(self.plant.controller.settings.max_charge_amps, 12)


if __name__ == '__main__':
    unittest.main()
