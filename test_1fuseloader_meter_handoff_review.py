"""Independent review regressions for genuine electrical peak handoff.

Only synthetic public evaluate/submission/ACK inputs are used. No HA or SDK.
"""
from dataclasses import replace
import unittest

import test_1fuseloader_automatic_cycles as automatic
import test_1fuseloader_grid_confirmation as nominal
import test_1fuseloader_meter_envelope_policy as meter


def mapped_l1(inp,logic=meter.logic):
    """Explicit synthetic mode-10 installation, independent of legacy MAX."""
    return replace(inp,controlled_l1=logic.ControlledL1Evidence(
        current=inp.charger_current,received_at_ts=inp.charger_report_ts,
        sequence=int((inp.charger_report_ts-meter.EPOCH)*1000)+1,
        evidence_credit=True,source_id=inp.control_source_id,
        receiver_identity=(100,101),mapping_epoch=1,output_phase=10,
        mapping_adopted_at_ts=meter.EPOCH-1.,mapping_sequence=1))


class ExplicitNominalFixture:
    """Keep the nominal oracle, adding actual dispatch and phase witnesses."""
    regulator=nominal.GridConfirmationTests.regulator
    running=nominal.GridConfirmationTests.running
    assertEqual=unittest.TestCase().assertEqual
    assertTrue=unittest.TestCase().assertTrue

    def sample(self,r,now,grid,ev,*,status='charging',gt=None,et=None,accept=True,**metadata):
        identity=metadata.pop('identity',nominal.fixtures.IDENTITY)
        inp=nominal.logic.RegulatorInput(now_ts=now,status=status,grid_l1=grid,
            charger_current=ev,wall_now_ts=nominal.EPOCH+now,
            grid_report_ts=nominal.EPOCH+(now if gt is None else gt),
            charger_report_ts=nominal.EPOCH+(now if et is None else et),
            control_source_id=identity,**metadata)
        inp=mapped_l1(inp,nominal.logic)
        self.last_input=inp
        d=r.evaluate(inp)
        if accept and d.should_call_service:
            self.assertTrue(r.begin_submission(d,now,inp=inp))
            r.acknowledge(d,now)
        return d

    def pending(self,**options):
        r=nominal.GridConfirmationTests.pending(self,accept=False,**options)
        self.assertTrue(r.begin_submission(r._last_decision,40.2,inp=self.last_input))
        r.acknowledge(r._last_decision,40.2)
        return r


class MeterElectricalHandoffReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automatic.AutomaticCycleTests()
        self.fixture.setUp()
        self.r = self.fixture.ready()
        self.helper = self.fixture.helper

    def step(self, at, raw, ev, receipt, grid_at, *, accept=True):
        inp = self.helper.frame(at, raw, ev, et=receipt, gt=grid_at,
                                charger_native_committed=True)
        inp = mapped_l1(inp)
        decision = self.r.evaluate(inp)
        if accept and decision.should_call_service:
            self.assertTrue(self.r.begin_submission(decision, at, inp=inp))
            self.r.acknowledge(decision, at + .1)
        return decision

    def select_meter(self):
        self.r.update_settings(replace(self.r.settings, meter_envelope_control=True))

    def complete_handoff_from_unsent_legacy_minimum(self):
        planned = self.step(3., 35., 16.1, 2., 3., accept=False)
        self.assertEqual(planned.current_p1, 7)
        self.assertEqual(self.r.last_target[0], 16)
        self.select_meter()
        self.step(4., 35., 16.1, 2., 4.)
        self.step(5., 35., 7.1, 4.8, 4.)
        completed = self.step(6., 28.5, 7.1, 4.8, 6.)
        self.assertEqual(completed.current_p1, 7)
        self.assertIsNone(completed.meter_zero_cause)
        self.assertIsNone(self.r._peak_response)

    def test_original_genuine_minimum_and_grid_deadline_survive_handoff(self):
        self.step(3., 35., 16.1, 2., 3.)
        response = self.step(4., 35., 7.1, 3.8, 3.)
        original_deadline = response.grid_confirmation_deadline_ts
        self.assertAlmostEqual(original_deadline, 33.8, places=5)
        original_lease = self.r.last_set_limit_ts
        self.select_meter()
        waiting = self.step(5., 35., 7.1, 3.8, 3.)
        self.assertFalse(waiting.should_call_service)
        self.assertEqual(waiting.grid_confirmation_deadline_ts, original_deadline)
        self.assertEqual(self.r.last_set_limit_ts, original_lease)
        completed = self.step(6., 28.5, 7.1, 3.8, 6.)
        self.assertIsNone(completed.meter_zero_cause)
        self.assertIsNone(self.r._peak_response)
        after_old_response_deadline = self.step(13., 28.5, 7.1, 3.8, 13.)
        self.assertEqual(after_old_response_deadline.current_p1, 7)
        self.assertIsNone(after_old_response_deadline.meter_zero_cause)

    def test_resolved_handoff_does_not_force_premature_identical_minimum_writes(self):
        self.complete_handoff_from_unsent_legacy_minimum()
        lease = self.r.last_set_limit_ts
        for at in (14., 24.):
            decision = self.step(at, 28.5, 7.1, 4.8, at)
            self.assertFalse(decision.should_call_service)
            self.assertEqual(self.r.last_set_limit_ts, lease)

    def test_resolved_handoff_does_not_persist_a_false_interrupted_transition(self):
        self.complete_handoff_from_unsent_legacy_minimum()
        self.step(10., 24., 7.1, 4.8, 10.)
        state = self.r.export_overload_state()
        self.assertEqual(state['automatic_cycle']['state'], 'ready')
        self.assertIsNone(state['automatic_cycle']['failure_reason'])
        self.assertFalse(state['transition_pending'])
        restored = automatic.logic.OneFuseRegulator(self.r.settings)
        self.assertTrue(restored.import_overload_state(state))
        self.assertEqual(restored._automatic_cycle['state'], 'ready')
        self.assertIsNone(restored._automatic_cycle['failure_reason'])

    def test_genuine_peak_nominal_minimum_keeps_original_confirmation_phase(self):
        fixture = ExplicitNominalFixture()
        r = fixture.pending(peak_only_response=True, automatic_overload_cycles=True)
        response = fixture.sample(r, 46.5, 30.2, 7.08, gt=44.)
        self.assertEqual(r._minimum_confirmation.owner, 'nominal')
        self.assertIsNotNone(r._peak_response)
        original_deadline = response.grid_confirmation_deadline_ts
        original_lease = r.last_set_limit_ts
        r.update_settings(replace(r.settings, meter_envelope_control=True))

        def tick(at, raw, grid_at):
            inp = nominal.logic.RegulatorInput(now_ts=at, status='charging',
                grid_l1=raw, charger_current=7.08, wall_now_ts=nominal.EPOCH + at,
                grid_report_ts=nominal.EPOCH + grid_at,
                charger_report_ts=nominal.EPOCH + 46.5,
                control_source_id=nominal.fixtures.IDENTITY,
                charger_native_committed=True)
            inp = mapped_l1(inp,nominal.logic)
            decision = r.evaluate(inp)
            if decision.should_call_service:
                self.assertTrue(r.begin_submission(decision, at, inp=inp))
                r.acknowledge(decision, at + .1)
            return decision

        waiting = tick(47., 30.2, 44.)
        self.assertFalse(waiting.should_call_service)
        self.assertEqual(waiting.grid_confirmation_deadline_ts, original_deadline)
        self.assertEqual(r.last_set_limit_ts, original_lease)
        completed = tick(48., 21.3, 48.)
        self.assertIsNone(completed.meter_zero_cause)
        self.assertIsNone(r._peak_response)
        later = tick(50., 21.3, 50.)
        self.assertIsNone(later.meter_zero_cause)

    def test_reenabling_previously_started_meter_engine_adopts_genuine_legacy_phase(self):
        fixture = meter.MeterEnvelopePolicyTests()
        r = fixture.live()
        r.update_settings(replace(r.settings, meter_envelope_control=False))
        fixture.sample(r, 5., 20.1, 16.1, receipt=4.8, sequence=2)
        fixture.sample(r, 10., 35., 16.1, receipt=4.8, sequence=2)
        _, response = fixture.sample(r, 12., 35., 7.1,
            receipt=11.8, sequence=3, grid_at=10.)
        original_deadline = response.grid_confirmation_deadline_ts
        self.assertAlmostEqual(original_deadline, 41.8, places=5)
        r.update_settings(replace(r.settings, meter_envelope_control=True))
        _, waiting = fixture.sample(r, 13., 35., 7.1,
            receipt=11.8, sequence=3, grid_at=10.)
        self.assertFalse(waiting.should_call_service)
        self.assertEqual(waiting.grid_confirmation_deadline_ts, original_deadline)
        _, completed = fixture.sample(r, 14., 28.5, 7.1,
            receipt=11.8, sequence=3)
        self.assertIsNone(completed.meter_zero_cause)
        self.assertIsNone(r._peak_response)
        _, later = fixture.sample(r, 20., 28.5, 7.1,
            receipt=11.8, sequence=3)
        self.assertEqual(later.current_p1, 7)
        self.assertIsNone(later.meter_zero_cause)

    def test_changed_genuine_minimum_requires_later_grid_inside_original_deadline(self):
        for final_grid in (28.8, 29.01):
            with self.subTest(final_grid=final_grid):
                fixture = meter.MeterEnvelopePolicyTests()
                r = fixture.peak()
                _, response = fixture.sample(r, 12., 35., 7.1,
                    receipt=11.8, sequence=3, grid_at=10.)
                original_deadline = response.grid_confirmation_deadline_ts
                _, changed = fixture.sample(r, 13., 28.8, 7.4,
                    receipt=12.9, sequence=4, grid_at=12., accept=False)
                self.assertIsNone(changed.meter_zero_cause)
                self.assertEqual(changed.grid_confirmation_deadline_ts, original_deadline)
                self.assertIsNotNone(r._peak_response)
                self.assertFalse(changed.should_call_service)
                _, later_grid = fixture.sample(r, 20., final_grid, 7.4,
                    receipt=12.9, sequence=4, grid_at=13.5, accept=False)
                if final_grid <= 29.:
                    self.assertIsNone(later_grid.meter_zero_cause)
                    self.assertIsNone(r._peak_response)
                    self.assertEqual(later_grid.current_p1, 7)
                else:
                    self.assertEqual(later_grid.meter_zero_cause,
                        meter.logic.ElectricalStopCause.GRID_CEILING.value)

    def test_fresh_normal_meter_can_renew_real_seven_without_erasing_original_peak(self):
        fixture = meter.MeterEnvelopePolicyTests()
        r = fixture.peak()
        peak = r._peak_response
        renewals = 0
        for at in (12., *map(float, range(15, 225, 5))):
            _, decision = fixture.sample(r, at, 18., 16.1,
                receipt=4.8, sequence=2,
                meter_ev_health=meter.logic.SourceHealth(False, 'late_native'))
            self.assertIsNone(decision.meter_zero_cause)
            self.assertEqual(decision.current_p1, 7)
            self.assertEqual(r.last_target[0], 7)
            self.assertIsNone(decision.nominal_response_deadline_ts)
            self.assertIs(r._peak_response, peak)
            self.assertEqual((peak.origin, peak.deadline), (10., 20.))
            self.assertGreaterEqual(r._get_meter_envelope_policy().upper, 16.)
            renewals += decision.should_call_service
        self.assertGreaterEqual(renewals, 7)
        _, returned_peak = fixture.sample(r, 225., 28.5, 16.1,
            receipt=4.8, sequence=2, accept=False,
            meter_ev_health=meter.logic.SourceHealth(False, 'late_native'))
        self.assertEqual(returned_peak.meter_zero_cause,
            meter.logic.ElectricalStopCause.PEAK_RESPONSE_TIMEOUT.value)
        self.assertIs(r._peak_response, peak)
        self.assertEqual(peak.deadline, 20.)

    def test_normal_meter_exception_requires_an_actual_accepted_minimum(self):
        fixture = meter.MeterEnvelopePolicyTests()
        r = fixture.live()
        fixture.sample(r, 5., 20.1, 16.1, receipt=4.8, sequence=2)
        fixture.sample(r, 10., 35., 16.1, receipt=4.8,
                       sequence=2, accept=False)
        self.assertEqual(r.last_target[0], 16)
        fixture.sample(r, 12., 18., 16.1, receipt=4.8, sequence=2,
            accept=False, meter_ev_health=meter.logic.SourceHealth(False, 'late_native'))
        _, unaccepted = fixture.sample(r, 20., 18., 16.1,
            receipt=4.8, sequence=2, accept=False,
            meter_ev_health=meter.logic.SourceHealth(False, 'late_native'))
        self.assertEqual(unaccepted.meter_zero_cause,
            meter.logic.ElectricalStopCause.PEAK_RESPONSE_TIMEOUT.value)
        self.assertEqual(r.last_target[0], 16)


if __name__ == '__main__':
    unittest.main()
