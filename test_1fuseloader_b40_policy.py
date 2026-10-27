"""Pure mapped-L1/positive-authority contracts; no network or HA access."""
from dataclasses import replace
from copy import deepcopy
import unittest
import test_1fuseloader_meter_envelope_policy as base

logic,EPOCH=base.logic,base.EPOCH


class MappedInstallation:
    """Explicit synthetic N+L1 installation; production never infers this."""
    regulator=base.MeterEnvelopePolicyTests.regulator
    sample=base.MeterEnvelopePolicyTests.sample
    live=base.MeterEnvelopePolicyTests.live
    peak=base.MeterEnvelopePolicyTests.peak
    assertEqual=unittest.TestCase().assertEqual
    assertTrue=unittest.TestCase().assertTrue
    def frame(self,now,grid=22.6,current=1.8,*,receipt=67.,sequence=1,
              mapped_current=None,mapping=10,mapping_epoch=1,adopted=-1.,
              phase_receipt=None,phase_sequence=None,phase_credit=True,
              provider=None,phase_error=None,controlled_missing=False,**kw):
        inp=base.MeterEnvelopePolicyTests.frame(self,now,grid,current,receipt=receipt,sequence=sequence,**kw)
        if controlled_missing:
            return replace(inp,controlled_l1=None)
        e=logic.ControlledL1Evidence(current=current if mapped_current is None else mapped_current,
            received_at_ts=EPOCH+(receipt if phase_receipt is None else phase_receipt),
            sequence=sequence if phase_sequence is None else phase_sequence,
            evidence_credit=phase_credit,source_id=inp.control_source_id,
            receiver_identity=(100,101),mapping_epoch=mapping_epoch,output_phase=mapping,
            mapping_adopted_at_ts=EPOCH+adopted,mapping_sequence=1,
            source_ts=None if provider is None else EPOCH+provider,
            source_highwater_ts=None if provider is None else EPOCH+provider,
            error=phase_error)
        return replace(inp,controlled_l1=e)


class B40PolicyTests(unittest.TestCase):
    def setUp(self):
        self.f=MappedInstallation()

    def peak(self):
        return self.f.peak()

    def test_neutral_max_and_mapped_line_have_separate_response_authority(self):
        r=self.peak()
        _,response=self.f.sample(r,18.151245,31.,8.34,mapped_current=7.094,
            receipt=18.148788,phase_receipt=18.151245,sequence=3,grid_at=15.)
        self.assertEqual(response.mode,'meter_grid_wait')
        self.assertAlmostEqual(response.grid_confirmation_deadline_ts,48.151245,places=4)
        self.assertEqual(response.charger_current,8.34)
        self.assertEqual(response.controlled_l1_current,7.094)
        _,deadline=self.f.sample(r,20.,31.,8.34,mapped_current=7.094,
            receipt=18.148788,phase_receipt=18.151245,sequence=3,grid_at=15.)
        self.assertIsNone(deadline.meter_zero_cause)
        _,confirmed=self.f.sample(r,20.694,20.,8.34,mapped_current=7.094,
            receipt=18.148788,phase_receipt=18.151245,sequence=3,grid_at=20.)
        self.assertIsNone(confirmed.meter_zero_cause)
        self.assertIsNone(r._peak_response)
        self.assertEqual(confirmed.current_p1,10)
        self.assertEqual(confirmed.house_load_basis,'eligible_mapped_l1_then_meter')

    def test_reverse_values_never_choose_the_smallest_terminal(self):
        r=self.peak()
        self.f.sample(r,18.2,31.,7.094,mapped_current=8.34,receipt=18.1,sequence=3,grid_at=15.)
        _,d=self.f.sample(r,20.,31.,7.094,mapped_current=8.34,receipt=18.1,sequence=3,grid_at=15.)
        self.assertEqual(d.meter_zero_cause,'peak_response_timeout')

    def test_unknown_phase_scalar_and_mapping_epoch_cannot_supply_response(self):
        for changes in (dict(controlled_missing=True),dict(mapping=12),dict(mapping=14),
                        dict(mapping=0),dict(mapping=20),dict(mapping_epoch=2,adopted=17.),
                        dict(phase_credit=False),dict(phase_error='mapping_replay')):
            with self.subTest(changes=changes):
                r=self.peak()
                self.f.sample(r,18.,31.,7.094,receipt=17.8,sequence=3,grid_at=15.,**changes)
                _,d=self.f.sample(r,20.,31.,7.094,receipt=17.8,sequence=3,grid_at=15.,**changes)
                self.assertEqual(d.meter_zero_cause,'peak_response_timeout')

    def test_mapping_change_withdraws_fixed_confirmation_without_new_clock(self):
        r=self.peak()
        _,d=self.f.sample(r,18.,31.,8.34,mapped_current=7.094,receipt=17.8,sequence=3,grid_at=15.)
        original=d.grid_confirmation_deadline_ts
        _,lost=self.f.sample(r,19.,31.,8.34,mapped_current=7.094,receipt=18.8,
            sequence=4,grid_at=15.,mapping_epoch=2,adopted=18.5)
        self.assertIsNone(lost.grid_confirmation_deadline_ts)
        _,stopped=self.f.sample(r,20.,31.,8.34,mapped_current=7.094,receipt=19.8,
            sequence=5,grid_at=15.,mapping_epoch=2,adopted=18.5)
        self.assertEqual(stopped.meter_zero_cause,'peak_response_timeout')
        self.assertEqual(r._peak_response.deadline,20.)
        self.assertLess(r._peak_response.deadline,original)

    def test_same_provider_second_real_native_credit_advances_after_sdk(self):
        r=self.f.live()
        self.f.sample(r,5.,20.1,16.1,receipt=4.8,sequence=2,provider=4.)
        self.f.sample(r,10.6,31.,16.1,receipt=10.1,sequence=3,provider=10.,grid_at=10.)
        _,d=self.f.sample(r,11.,31.,8.34,mapped_current=7.094,receipt=10.8,
            sequence=4,provider=10.,grid_at=10.)
        self.assertEqual(d.mode,'meter_grid_wait')
        self.assertIsNone(d.meter_zero_cause)
        r2=self.f.live()
        self.f.sample(r2,5.,20.1,16.1,receipt=4.8,sequence=2,provider=4.)
        self.f.sample(r2,10.6,31.,16.1,receipt=10.1,sequence=3,provider=10.,grid_at=10.)
        _,uncredited=self.f.sample(r2,20.01,31.,8.34,mapped_current=7.094,receipt=10.8,
            sequence=4,provider=10.,grid_at=10.,phase_credit=False)
        self.assertEqual(uncredited.meter_zero_cause,'peak_response_timeout')

    def test_provider_regression_below_captured_floor_cannot_become_a_response(self):
        r=self.f.live()
        self.f.sample(r,5.,20.1,16.1,receipt=4.8,sequence=2,provider=4.)
        self.f.sample(r,10.6,31.,16.1,receipt=9.8,sequence=3,provider=9.,grid_at=10.)
        self.f.sample(r,18.,31.,8.34,mapped_current=7.094,receipt=17.8,
            sequence=4,provider=8.,grid_at=15.)
        _,d=self.f.sample(r,20.01,31.,8.34,mapped_current=7.094,receipt=17.8,
            sequence=4,provider=8.,grid_at=15.)
        self.assertEqual(d.meter_zero_cause,'peak_response_timeout')

    def test_fast_native_response_with_lagging_provider_clock_keeps_original_phases(self):
        r=self.f.live()
        self.f.sample(r,5.,20.1,16.1,receipt=4.8,sequence=2,provider=2.)
        _,first=self.f.sample(r,10.6,31.,16.1,receipt=10.1,sequence=3,
            provider=8.,grid_at=10.)
        original=first.nominal_response_deadline_ts
        # The source clock advances8→9 independently of HA wall10.6→11.2.
        # Native183 is new and credited after SDK; no cross-clock offset is
        # asserted, and neither electrical observation phase is extended.
        _,response=self.f.sample(r,11.2,31.,8.34,mapped_current=7.094,receipt=11.2,
            sequence=4,provider=9.,grid_at=10.)
        self.assertEqual(response.mode,'meter_grid_wait')
        self.assertAlmostEqual(response.grid_confirmation_deadline_ts,41.2,places=4)
        self.assertAlmostEqual(r._peak_response.deadline,original,places=7)
        _,after_ten=self.f.sample(r,20.01,31.,8.34,mapped_current=7.094,receipt=11.2,
            sequence=4,provider=9.,grid_at=10.)
        self.assertIsNone(after_ten.meter_zero_cause)
        self.assertAlmostEqual(after_ten.grid_confirmation_deadline_ts,41.2,places=4)
        _,normal=self.f.sample(r,20.694,20.,8.34,mapped_current=7.094,receipt=11.2,
            sequence=4,provider=9.,grid_at=20.)
        self.assertIsNone(normal.meter_zero_cause)
        self.assertEqual(normal.current_p1,10)
        self.assertIsNone(r._peak_response)

    def test_ack_uses_actual_dispatch_phase_floor_not_new_http_input(self):
        r=self.f.live()
        self.f.sample(r,5.,20.1,16.1,receipt=4.8,sequence=2)
        inp,d=self.f.sample(r,10.,31.,16.1,receipt=4.8,sequence=2,accept=False)
        self.assertTrue(r.begin_submission(d,10.,inp=inp))
        self.f.sample(r,18.,31.,8.34,mapped_current=7.094,receipt=17.8,sequence=3,
            grid_at=15.,accept=False)
        r.acknowledge(d,18.1)
        _,after=self.f.sample(r,18.2,31.,8.34,mapped_current=7.094,receipt=17.8,
            sequence=3,grid_at=15.,accept=False)
        self.assertEqual(after.mode,'meter_grid_wait')
        self.assertAlmostEqual(after.grid_confirmation_deadline_ts,47.8,places=4)

    def test_only_mapped_l1_and_later_grid_supply_headroom_credit(self):
        for raw,expected in ((22.5,7),(12.1,16)):
            r=self.f.live(7)
            r.update_settings(replace(r.settings,max_charge_amps=16))
            _,d=self.f.sample(r,5.,raw,16.2,mapped_current=7.094,receipt=4.8,sequence=2)
            self.assertEqual(d.current_p1,expected)
            self.assertEqual(d.house_load_basis,'eligible_mapped_l1_then_meter')
        r=self.f.live(7)
        r.update_settings(replace(r.settings,max_charge_amps=16))
        _,unknown=self.f.sample(r,5.,12.1,16.2,receipt=4.8,sequence=2,mapping=12)
        self.assertEqual(unknown.current_p1,7)
        self.assertEqual(unknown.house_load_basis,'meter_envelope_feedback')

    def test_zero_renewals_do_not_slide_positive_horizon_or_normal_timer(self):
        r=self.peak()
        _,fault=self.f.sample(r,20.,31.,8.34,receipt=18.,sequence=3,grid_at=15.)
        self.assertEqual(fault.meter_zero_cause,'peak_response_timeout')
        saved=deepcopy(r.export_overload_state())
        original_cold=r._get_meter_envelope_policy().cold_upper_until
        restart=None
        for at in range(25,251,5):
            inp,d=self.f.sample(r,float(at),13.3,0.,receipt=at-.2,sequence=at,
                controlled_missing=True)
            if at in (50,80) and r.last_target[0]==0:
                zero=r.meter_envelope_fault(inp,logic.ElectricalStopCause.PEAK_RESPONSE_TIMEOUT)
                self.assertTrue(zero.should_call_service)
                self.assertTrue(r.begin_submission(zero,float(at),inp=inp))
                r.acknowledge(zero,float(at)+.1)
            if restart is None and r.last_target[0]>0:
                restart=at
                self.assertEqual(r.last_target[0],9)
        self.assertIsNotNone(restart)
        self.assertLessEqual(restart,90)
        self.assertEqual(r._get_meter_envelope_policy().cold_upper_until,original_cold)
        current=r.export_overload_state()
        self.assertEqual(current['automatic_cycle']['state'],'blocked')
        self.assertEqual(current['automatic_cycle']['failure_reason'],saved['automatic_cycle']['failure_reason'])
        self.assertEqual(current['failure_reason'],saved['failure_reason'])

    def test_new_actual_positive_dispatch_expiry_survives_ambiguous_result(self):
        r=self.f.live(7)
        r.update_settings(replace(r.settings,max_charge_amps=16))
        inp,plan=self.f.sample(r,30.,5.,7.1,receipt=29.8,sequence=2,accept=False)
        self.assertEqual(plan.current_p1,16)
        self.assertTrue(r.begin_submission(plan,30.,inp=inp))
        r.reject_submission(plan,31.)
        entry=r._get_meter_envelope_policy().possible_positive[plan.plan_id]
        self.assertEqual(entry,(16,90.))
        normal=self.f.frame(35.,grid=13.3,current=0.,receipt=34.8,sequence=3)
        zero=r.meter_envelope_fault(normal,logic.ElectricalStopCause.UNKNOWN_DELIVERY)
        self.assertTrue(r.begin_submission(zero,35.,inp=normal))
        r.acknowledge(zero,35.1)
        self.assertEqual(r._get_meter_envelope_policy().possible_positive[plan.plan_id],(16,90.))

    def test_legacy_ambiguous_positive_keeps_its_actual_longer_ttl_at_handoff(self):
        r=self.f.regulator(meter_envelope_control=False,normal_ttl_seconds=120)
        inp,plan=self.f.sample(r,0.,4.,status='disconnected',receipt=0.,accept=False)
        self.assertEqual(plan.ttl_seconds,120)
        self.assertTrue(r.begin_submission(plan,0.,inp=inp))
        r.reject_submission(plan,.1)
        self.assertIsNone(r.last_target)
        r.update_settings(replace(r.settings,meter_envelope_control=True,normal_ttl_seconds=60))
        for at in range(10,120,5):
            _,wait=self.f.sample(r,float(at),13.3,0.,receipt=at-.2,sequence=at,
                status='awaiting_start',accept=False)
            self.assertFalse(wait.should_call_service,wait)
        _,ready=self.f.sample(r,120.,13.3,0.,receipt=119.8,sequence=120,status='awaiting_start')
        self.assertEqual(ready.current_p1,9)
        self.assertEqual(r.last_set_limit_ts,120.)

    def test_positive_sdk_entry_without_boundary_input_is_rejected(self):
        r=self.f.regulator()
        _,plan=self.f.sample(r,0.,4.,status='disconnected',receipt=0.,accept=False)
        self.assertTrue(plan.should_call_service)
        self.assertFalse(r.begin_submission(plan,0.))
        self.assertFalse(r._get_meter_envelope_policy().possible_positive)

    def test_old_off_witness_cannot_retire_a_later_unknown_positive(self):
        r=self.peak()
        r.update_settings(replace(r.settings,restart_stable_seconds=15))
        self.f.sample(r,20.,31.,8.34,receipt=18.,sequence=3,grid_at=15.)
        self.f.sample(r,25.,3.,0.,receipt=24.8,sequence=4,status='awaiting_start')
        inp,positive=self.f.sample(r,40.,3.,0.,receipt=24.8,sequence=4,
            status='awaiting_start',accept=False)
        self.assertEqual(positive.current_p1,16)
        self.assertTrue(r.begin_submission(positive,40.,inp=inp))
        r.reject_submission(positive,41.)
        # The prior OFF24.8 predates the possible positive40 (expiry100).
        # A new zero50 must pin that current state as its baseline rather
        # than reuse the old zero20/physical-OFF relationship.
        inp=self.f.frame(50.,grid=13.3,current=0.,receipt=24.8,sequence=4,status='awaiting_start')
        zero=r.meter_envelope_fault(inp,logic.ElectricalStopCause.UNKNOWN_DELIVERY)
        self.assertTrue(r.begin_submission(zero,50.,inp=inp))
        r.acknowledge(zero,50.1)
        for at in range(55,100,5):
            _,wait=self.f.sample(r,float(at),13.3,0.,receipt=24.8,sequence=4,
                status='awaiting_start',accept=False)
            self.assertFalse(wait.should_call_service,wait)
        _,ready=self.f.sample(r,100.,13.3,0.,receipt=24.8,sequence=4,
            status='awaiting_start',accept=False)
        self.assertTrue(ready.should_call_service,ready)
        self.assertEqual(ready.current_p1,9)


if __name__=='__main__':
    unittest.main()
