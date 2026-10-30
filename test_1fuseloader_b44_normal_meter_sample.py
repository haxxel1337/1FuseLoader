"""Actual controller oracles for ordinary decreases awaiting meter feedback.

Native callbacks, SDK admission, HTTP acceptance, readback and finite leases
use the real adapter/controller fixture. All currents, identities and clocks
are synthetic; no installation history or private proof state is injected.
"""
from copy import deepcopy
import unittest

import test_1fuseloader_b40_controller as actual


class B44NormalMeterSampleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plant = actual.B40ControlledPhaseControllerTests(methodName='runTest')
        await self.plant.asyncSetUp()

    async def asyncTearDown(self):
        await self.plant.asyncTearDown()

    def observed_authority(self):
        regulator = self.plant.controller.regulator
        return dict(target=regulator.last_target,
                    accepted_origin=regulator.last_set_limit_ts,
                    lease_expiry=regulator._get_meter_envelope_policy().lease.expires,
                    sdk=deepcopy(self.plant.sdk_calls))

    async def first_normal_decrease(self):
        plant = self.plant
        await plant.boot()
        await plant.packet(199.8, {182:16.2, 183:16.0}, source=199.6)
        decision = await plant.tick(200., 24.5, reported=200., status='charging')
        self.assertIsNone(decision.meter_zero_cause)
        self.assertFalse(decision.meter_overload_authority)
        first = self.observed_authority()
        self.assertEqual(first['target'][0], 14)
        self.assertEqual(first['sdk'][-1]['cap'], 14)
        self.assertEqual(first['sdk'][-1]['phases'], (14, 0, 0))
        self.assertEqual(first['sdk'][-1]['ttl'], 60)
        return first

    def assert_original_authority_retained(self, first):
        retained = self.observed_authority()
        self.assertEqual(retained['target'][0], 14)
        self.assertEqual(retained['sdk'], first['sdk'],
                         'An old meter observation must not send another SDK command')
        self.assertEqual(retained['accepted_origin'], first['accepted_origin'])
        self.assertEqual(retained['lease_expiry'], first['lease_expiry'])
        self.plant.sdk_charger.resume.assert_not_awaited()

    async def test_new_ev_without_new_meter_cannot_shed_same_sample_twice(self):
        first = await self.first_normal_decrease()
        # A newer phase observation, including the same current value, is not
        # a later meter response to the already accepted normal14 decrease.
        await self.plant.packet(204.8, {182:16.2, 183:16.0}, source=204.6)
        decision = await self.plant.tick(205., 24.5, reported=200., status='charging')
        self.assertIsNone(decision.meter_zero_cause)
        self.assert_original_authority_retained(first)

    async def test_unchanged_ev_with_same_meter_retains_original_finite_lease(self):
        first = await self.first_normal_decrease()
        decision = await self.plant.tick(205., 24.5, reported=200., status='charging')
        self.assertIsNone(decision.meter_zero_cause)
        self.assert_original_authority_retained(first)

    async def test_genuinely_new_high_meter_preempts_to_original_peak_deadline(self):
        await self.first_normal_decrease()
        await self.plant.packet(204.8, {182:16.2, 183:16.0}, source=204.6)
        decision = await self.plant.tick(205., 31., reported=205., status='charging')
        authority = self.observed_authority()
        self.assertEqual(authority['target'][0], 7)
        self.assertEqual(authority['sdk'][-1]['cap'], 7)
        self.assertIsNone(decision.meter_zero_cause)
        self.assertEqual(decision.nominal_response_deadline_ts, 215.)
        later = await self.plant.tick(210., 31., reported=210., status='charging')
        self.assertEqual(self.observed_authority()['target'][0], 7)
        self.assertIsNone(later.meter_zero_cause)
        self.assertEqual(later.nominal_response_deadline_ts, 215.)
        self.plant.sdk_charger.resume.assert_not_awaited()

    async def test_new_normal_meter_permits_a_new_measured_adjustment(self):
        first = await self.first_normal_decrease()
        # The plant now physically takes the accepted14A. Its later fresh
        # meter24A leaves house10A, so the normal23A budget permits13A.
        # Queue the paired phase/meter feedback before draining refreshes.
        await self.plant.packet(209.8, {182:14.2, 183:14.0}, source=209.6, drain=False)
        decision = await self.plant.tick(210., 24., reported=210., status='charging')
        authority = self.observed_authority()
        self.assertIsNone(decision.meter_zero_cause)
        self.assertFalse(decision.meter_overload_authority)
        self.assertEqual(authority['target'][0], 13)
        self.assertEqual(authority['sdk'][-1]['cap'], 13)
        self.assertGreater(authority['accepted_origin'], first['accepted_origin'])
        self.plant.sdk_charger.resume.assert_not_awaited()

    async def test_stronger_raw_with_same_timestamp_preempts_normal_hold(self):
        await self.first_normal_decrease()
        await self.plant.packet(204.8, {182:16.2, 183:16.0}, source=204.6)
        # A stronger current bound cannot be hidden merely because its source
        # timestamp has not advanced. Existing overload admission owns it.
        decision = await self.plant.tick(205., 26., reported=200., status='charging')
        self.assertEqual(self.observed_authority()['target'][0], 7)
        self.assertEqual(self.observed_authority()['sdk'][-1]['cap'], 7)
        self.assertIsNone(decision.meter_zero_cause)
        self.assertTrue(decision.meter_overload_authority)
        self.plant.sdk_charger.resume.assert_not_awaited()

if __name__ == '__main__':
    unittest.main()
