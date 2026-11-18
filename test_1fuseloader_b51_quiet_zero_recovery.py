"""Exercise cold zero recovery through the real controller, adapter and SDK boundary."""
import unittest

import test_1fuseloader_b44_quiet_safety as fixture


class B51QuietZeroRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.B44QuietSafetyTests.asyncSetUp
    asyncTearDown = fixture.B44QuietSafetyTests.asyncTearDown
    bind_real_exception_identity = fixture.B44QuietSafetyTests.bind_real_exception_identity
    tick = fixture.B44QuietSafetyTests.tick
    quiet_sdk = fixture.B44QuietSafetyTests.quiet_sdk

    async def boot_after_initial_zero(self, *, readback=True):
        plant = self.plant
        # The outgoing command is known by the charger, not this new controller.
        plant.product.state['dynamicCircuitCurrentP1'] = 13
        plant.circuit.attributes['state_dynamicCircuitCurrentP1'] = 13
        if not readback:
            plant.sdk_circuit.set_dynamic_current.side_effect = self.quiet_sdk
        await plant.packet(99., {21: 1, 38: 2, 109: 2, 110: 0,
                                 182: 0., 183: 0., 184: 0., 185: 0.}, drain=False)
        first = await self.tick(100., 12.9, reported=-200., status='awaiting_start')
        self.assertEqual(first.meter_zero_cause, 'grid_invalid')
        self.assertEqual(plant.controller.regulator.last_target[0], 0)
        self.assertEqual([call['cap'] for call in plant.sdk_calls], [0])
        self.assertEqual(plant.adapter.reported_limits,
                         (0., 0., 0.) if readback else (13., 0., 0.))
        # Current has been zero throughout. Avoid a synthetic post-command
        # positive or mapped-off frame, just like Easee's quiet startup.
        for at in range(105, 215, 5):
            await self.tick(float(at), 16.2)
        return plant

    async def test_quiet_zero_remains_waiting_then_fresh_data_restarts_normally(self):
        plant = await self.boot_after_initial_zero()
        await self.tick(215., 12.9)
        plant.now = 221.
        self.assertTrue(plant.adapter.product_data_quiet)
        first_quiet = await self.tick(221., 12.9)
        self.assertIsNone(first_quiet.meter_zero_cause, first_quiet)
        self.assertEqual(first_quiet.mode, 'meter_recovery_wait')
        self.assertEqual([call['cap'] for call in plant.sdk_calls], [0])

        # RAW 16.2 cannot fit the minimum under the normal 23 A envelope.
        await self.tick(230., 16.2)
        for at in range(245, 305, 5):
            waiting = await self.tick(float(at), 12.9)
            self.assertEqual(waiting.current_p1, 0)
            self.assertFalse(waiting.should_call_service)
        self.assertEqual([call['cap'] for call in plant.sdk_calls], [0])

        # A genuine product update opens the existing route only. The recent
        # high reading must have broken the earlier stability interval.
        await plant.packet(303., {109: 2, 114: 0., 120: 0.}, drain=False)
        too_soon = await self.tick(304., 12.9)
        self.assertFalse(too_soon.should_call_service)
        resumed = await self.tick(306., 12.9)
        self.assertEqual(resumed.current_p1, 10)
        self.assertEqual([call['cap'] for call in plant.sdk_calls], [0, 10])
        self.assertFalse(resumed.controlled_l1_used)
        plant.sdk_charger.resume.assert_not_awaited()

    async def test_http_only_zero_keeps_the_original_protection(self):
        plant = await self.boot_after_initial_zero(readback=False)
        decision = await self.tick(225., 12.9)
        self.assertEqual(decision.meter_zero_cause, 'actuator_invalid')
        self.assertGreaterEqual(len(plant.sdk_calls), 2)
        self.assertTrue(all(call['cap'] == 0 for call in plant.sdk_calls))

    async def test_verified_zero_does_not_hide_a_critical_transport_failure(self):
        plant = await self.boot_after_initial_zero()
        plant.product.state['signalRConnected'] = False
        decision = await self.tick(225., 12.9)
        self.assertEqual(decision.meter_zero_cause, 'actuator_invalid')
        self.assertTrue(all(call['cap'] == 0 for call in plant.sdk_calls))

    async def test_contradictory_limit_invalidates_old_zero_readback(self):
        plant = await self.boot_after_initial_zero()
        # A restored or replaced cached limit cannot inherit the old event's
        # receipt. The controller may still send a protective zero here.
        plant.product.state['dynamicCircuitCurrentP1'] = 8
        decision = await self.tick(225., 12.9)
        self.assertEqual(decision.meter_zero_cause, 'actuator_invalid')
        self.assertEqual([call['cap'] for call in plant.sdk_calls], [0, 0])

    async def test_verified_zero_does_not_hide_a_hard_meter_limit(self):
        plant = await self.boot_after_initial_zero()
        decision = await self.tick(225., 39.)
        self.assertEqual(decision.meter_zero_cause, 'hard_limit')
        self.assertTrue(all(call['cap'] == 0 for call in plant.sdk_calls))


if __name__ == '__main__':
    unittest.main()
