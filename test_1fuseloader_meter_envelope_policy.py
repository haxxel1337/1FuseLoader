"""Independent pure contract checks for the explicit measured-meter policy."""
from copy import deepcopy
from dataclasses import replace
import unittest
import test_1fuseloader_positive_ramp_owner as native

logic=native.logic
EPOCH=native.EPOCH

class MeterEnvelopePolicyTests(unittest.TestCase):
    def frame(self,now,grid=22.6,current=1.8,*,receipt=67.,sequence=1,**metadata):
        """Synthetic N+L1 installation with an independently explicit witness.

        The old aggregate fixture alone no longer certifies the controlled
        phase. Missing/invalid native quality deliberately supplies no witness.
        Production does not default a missing mapping to this test setup.
        """
        inp=native.PositiveRampOwnerTests.frame(self,now,grid,current,
            receipt=receipt,sequence=sequence,**metadata)
        quality=inp.meter_ev_health
        if (receipt is None or current is None or not inp.charger_native_committed
                or inp.charger_snapshot_verified or inp.charger_publication_pending
                or not inp.charger_current_trusted or quality is not None and not quality.valid):
            return inp
        return replace(inp,controlled_l1=logic.ControlledL1Evidence(
            current=current,received_at_ts=EPOCH+receipt,sequence=sequence,
            evidence_credit=True,source_id=inp.control_source_id,
            receiver_identity=(100,101),mapping_epoch=1,output_phase=10,
            mapping_adopted_at_ts=EPOCH-1.,mapping_sequence=1))

    def regulator(self, **changes):
        options=dict(active_control=True,dry_run=False,meter_envelope_control=True,
            nominal_import_only_fallback=True,peak_only_response=True,automatic_overload_cycles=True,
            overload_enabled=True,disconnected_readiness=True,enable_soft_guard=False,
            enable_pause_resume=False,grid_confirmation_seconds=30,measurement_settle_seconds=25)
        options.update(changes)
        return logic.OneFuseRegulator(logic.RegulatorSettings(**options))

    def sample(self,r,at,raw,current=.012,*,status='charging',receipt=None,sequence=1,accept=True,**kw):
        values=dict(meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        values.update(kw)
        inp=self.frame(at,grid=raw,current=current,status=status,
            receipt=at-.2 if receipt is None else receipt,sequence=sequence,**values)
        d=r.evaluate(inp)
        if accept and d.should_call_service:
            self.assertTrue(r.begin_submission(d,at,inp=inp),d)
            r.acknowledge(d,at+.1)
        return inp,d

    def live(self,cap=16):
        r=self.regulator(max_charge_amps=cap)
        _,d=self.sample(r,0.,4.,status='disconnected',receipt=0.)
        self.assertEqual(r.last_target[0],cap,d)
        return r

    def peak(self):
        r=self.live()
        self.sample(r,5.,20.1,16.1,receipt=4.8,sequence=2)
        _,first=self.sample(r,10.,35.,16.1,receipt=4.8,sequence=2)
        self.assertEqual((r.last_target[0],first.nominal_response_deadline_ts),(7,20.))
        return r

    def verified_minimum(self):
        r=self.peak()
        _,d=self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts,41.8,places=4)
        self.sample(r,15.,28.5,7.1,receipt=11.8,sequence=3)
        self.assertIsNone(r._peak_response)
        return r

    def test_expired_ordinary_owners_and_fractional_partial_current_have_no_stop_authority(self):
        r=self.live()
        # Public ordinary offers, followed by packet order EV-before-meter.
        self.sample(r,5.,24.2,16.1,receipt=4.8,sequence=2)
        self.sample(r,35.,22.6,14.146,receipt=34.8,sequence=3)
        for fraction in (12.49,12.50,12.51,12.61):
            at=40.+fraction
            inp,d=self.sample(r,at,22.6,fraction,receipt=at-.1,sequence=4,grid_at=at-.5,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'))
            self.assertGreater(d.current_p1,0,d)
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(r.export_overload_state()['normal_control']['stops'],0)

    def test_policy_handoff_preserves_actual_lease_and_budget(self):
        r=self.live()
        r.settings=replace(r.settings,meter_envelope_control=False)
        old=(r.last_target,r.last_set_limit_ts,r._accepted_control_source_id)
        state=r.export_overload_state();state['used_seconds']=4000.
        state['failure_reason']='response_timeout'
        # Import on a separate object demonstrates history; handoff itself
        # uses the real in-process accepted authority without a new ACK.
        r._overload_used_seconds=4000.;r._overload_failure_reason='response_timeout'
        r.update_settings(replace(r.settings,meter_envelope_control=True))
        _,d=self.sample(r,5.,22.6,12.61,receipt=4.9,sequence=2,grid_at=4.,accept=False)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual((r.last_target,r.last_set_limit_ts,r._accepted_control_source_id),old)
        self.assertEqual(r.export_overload_state()['used_seconds'],4000.)
        self.assertEqual(r.export_overload_state()['failure_reason'],'response_timeout')

    def test_cold_restore_uses_maximum_unknown_upper_and_never_restores_lease(self):
        old=self.live()
        state=old.export_overload_state()
        restored=self.regulator(disconnected_readiness=False)
        self.assertTrue(restored.import_overload_state(state))
        self.assertIsNone(restored.last_target)
        _,first=self.sample(restored,0.,10.,status='awaiting_start',accept=False)
        self.assertFalse(first.should_call_service)
        self.assertEqual(restored._get_meter_envelope_policy().upper,16)
        for at in (10.,30.,59.9):
            _,d=self.sample(restored,at,10.,status='awaiting_start',accept=False)
            self.assertFalse(d.should_call_service)
        _,d=self.sample(restored,60.,10.,status='awaiting_start')
        self.assertEqual(d.current_p1,13)
        self.assertEqual(restored.last_set_limit_ts,60.)

    def test_unsent_ack_and_untyped_zero_cannot_create_authority(self):
        r=self.regulator()
        inp,d=self.sample(r,0.,4.,status='disconnected',accept=False)
        self.assertTrue(d.should_call_service)
        r.acknowledge(d,.1)
        self.assertIsNone(r.last_target)
        forged=replace(d,current_p1=0,meter_zero_cause=None)
        self.assertFalse(r.begin_submission(forged,0.,inp=inp))
        forged=replace(d,current_p1=0,meter_zero_cause=logic.ElectricalStopCause.HARD_LIMIT.value)
        self.assertFalse(r.begin_submission(forged,0.,inp=inp))

    def test_critical_source_fault_is_distinct_from_ev_quality(self):
        r=self.live()
        inp=self.frame(5.,grid=22.6,current=None,receipt=4.8,sequence=2,
            data_valid=False,data_error='ev_missing',charger_current_trusted=False,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(False,'missing'))
        d=r.evaluate(inp)
        self.assertIsNone(d.meter_zero_cause);self.assertEqual(d.current_p1,16)
        bad=replace(inp,meter_grid_health=logic.SourceHealth(False,'missing_grid'))
        stop=r.evaluate(bad)
        self.assertEqual(stop.meter_zero_cause,logic.ElectricalStopCause.GRID_INVALID.value)
        self.assertTrue(r.begin_submission(stop,5.,inp=bad))

    def test_partial_classification_cannot_hide_unclassified_fault(self):
        r=self.live()
        inp=self.frame(5.,grid=20.,current=7.,data_valid=False,data_error='unclassified',
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True))
        self.assertIsNotNone(r.evaluate(inp).meter_zero_cause)

    def test_invalid_clock_and_expired_lease_are_typed(self):
        r=self.live()
        inp=self.frame(float('nan'),grid=10.,current=7.)
        self.assertEqual(r.evaluate(inp).meter_zero_cause,logic.ElectricalStopCause.CLOCK_INVALID.value)
        r=self.live()
        _,d=self.sample(r,60.,10.,7.,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.LEASE_EXPIRED.value)

    def test_invalid_numeric_settings_cannot_bypass_shared_profile_validation(self):
        for key,value in (('buffer_amps',float('nan')),('minimum_reserve_amps',-1),
                          ('max_charge_amps',float('inf')),('resend_limit_every_seconds',0),
                          ('overload_budget_seconds',None),('max_charge_amps',None),
                          ('active_control','yes'),('dry_run',None)):
            r=self.live()
            r.settings=replace(r.settings,**{key:value})
            inp=self.frame(5.,grid=10.,current=7.)
            self.assertEqual(r.evaluate(inp).meter_zero_cause,logic.ElectricalStopCause.CONFIGURATION.value)

    def test_disconnected_off_and_manual_pause_never_start_or_resume(self):
        for status in ('disconnected','paused','awaiting_scheduled_start'):
            r=self.regulator(disconnected_readiness=False)
            for at in (0.,60.,120.):
                _,d=self.sample(r,at,4.,status=status,accept=False)
                self.assertFalse(d.should_call_service)
            self.assertIsNone(r.last_target)

    def test_peak_keeps_original_sample_aged_ten_seconds(self):
        r=self.peak()
        for at in (12.,15.,19.9):
            _,d=self.sample(r,at,35.,16.1,receipt=4.8,sequence=2,grid_at=10.)
            self.assertEqual(d.nominal_response_deadline_ts,20.)
        _,d=self.sample(r,20.,35.,16.1,receipt=4.8,sequence=2,grid_at=10.,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.PEAK_RESPONSE_TIMEOUT.value)

    def test_separate_grid_confirmation_is_thirty_after_timely_minimum(self):
        r=self.peak()
        _,first=self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        for at in (20.,30.,41.7):
            _,d=self.sample(r,at,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
            self.assertEqual(d.grid_confirmation_deadline_ts,first.grid_confirmation_deadline_ts)
            self.assertIsNone(d.meter_zero_cause)
        _,d=self.sample(r,41.8,35.,7.1,receipt=11.8,sequence=3,grid_at=10.,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.GRID_CONFIRMATION_TIMEOUT.value)

    def test_same_accepted_seven_gets_one_real_peak_dispatch_not_ttl_wait(self):
        r=self.live(cap=7)
        _,d=self.sample(r,5.,28.5,7.1,receipt=0.,sequence=1)
        self.assertEqual((d.current_p1,d.should_call_service),(7,True))
        self.assertEqual(r._get_meter_envelope_policy().minimum_dispatch,5.)
        _,again=self.sample(r,6.,28.5,7.1,receipt=0.,sequence=1)
        self.assertFalse(again.should_call_service)

    def test_sustained_minimum_does_not_rearm_peak_and_counts_full_budget(self):
        r=self.verified_minimum()
        for at in range(20,1810,5):
            _,d=self.sample(r,float(at),28.5,7.1,receipt=11.8,sequence=3)
            self.assertIsNone(d.meter_zero_cause,d)
            self.assertEqual(d.current_p1,7)
            self.assertIsNone(r._peak_response)
        self.assertEqual(r._automatic_cycle['state'],'active')
        _,d=self.sample(r,1810.,28.5,7.1,receipt=11.8,sequence=3,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.OVERLOAD_BUDGET.value)
        self.assertGreaterEqual(r.effective_overload_used_seconds,1800.)

    def test_hard_and_postresponse_ceilings_remain(self):
        r=self.live()
        _,d=self.sample(r,5.,38.01,16.1,receipt=4.8,sequence=2,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.HARD_LIMIT.value)
        r=self.peak()
        self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        _,d=self.sample(r,15.,29.01,7.1,receipt=11.8,sequence=3,accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.GRID_CEILING.value)

    def test_rearm_requires_strict_under23_continuously1800(self):
        r=self.live()
        cycle=r._automatic_cycle;cycle.update(state='blocked',used_seconds=100.,failure_reason='response_timeout')
        for at in range(5,1801,5): self.sample(r,float(at),23.,7.,receipt=4.8,sequence=2)
        self.assertEqual(cycle['used_seconds'],100.)
        for at in range(1805,3601,5): self.sample(r,float(at),22.,7.,receipt=4.8,sequence=2)
        self.assertEqual(cycle['used_seconds'],100.)
        self.sample(r,3605.,22.,7.,receipt=4.8,sequence=2)
        self.assertEqual(cycle['used_seconds'],0.)
        self.assertIsNone(cycle['failure_reason'])
        self.assertGreaterEqual(r._overload_used_seconds,0.)

    def test_b38_frozen_trial_snapshot_without_new_flag_keeps_history(self):
        import test_1fuseloader_trial_epochs as trials
        fixture=trials.ExplicitTrialTests()
        old=fixture.active()
        state=old.export_overload_state()
        state['trial_epoch']['policy']['settings'].pop('meter_envelope_control')
        frozen=deepcopy(state['trial_epoch']['policy'])
        restored=logic.OneFuseRegulator(deepcopy(old.settings))
        self.assertTrue(restored.import_overload_state(state))
        self.assertEqual(restored.export_overload_state()['used_seconds'],state['used_seconds'])
        self.assertEqual(restored.export_overload_state()['failure_reason'],state['failure_reason'])
        self.assertEqual(restored.export_overload_state()['trial_epoch']['policy'],frozen)

    def test_genuine_normal_decrease_precedes_blocked_overload_admission(self):
        r=self.live()
        r._ensure_automatic_cycle().update(state='blocked',failure_reason='response_timeout')
        _,d=self.sample(r,5.,25.2,16.1,receipt=4.8,sequence=2)
        self.assertEqual(d.current_p1,13)
        self.assertTrue(d.meter_ordinary_decrease)
        self.assertFalse(d.meter_overload_authority)
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual(r._automatic_cycle['failure_reason'],'response_timeout')
        # Reusing this one pre-response meter packet cannot synthesize either
        # another decrement or an immediate zero. It cannot renew the lease.
        _,same=self.sample(r,6.,25.2,16.1,receipt=4.8,sequence=2,grid_at=5.,accept=False)
        self.assertEqual(same.current_p1,13)
        self.assertFalse(same.should_call_service)
        self.assertIsNone(same.meter_zero_cause)
        # A genuinely later still-high meter now needs actual overload
        # admission unless a genuinely newer coherent current allows another
        # strictly normal decrease. Cached old EV is no such authority.
        _,later=self.sample(r,7.,25.2,16.1,receipt=4.8,sequence=2,accept=False)
        self.assertEqual(later.meter_zero_cause,logic.ElectricalStopCause.OVERLOAD_NOT_ADMITTED.value)

    def test_normal_decrease_does_not_bypass_minimum_or_ineligible_ev(self):
        for cap,current,error in ((7,7.1,False),(16,16.1,True)):
            with self.subTest(cap=cap,error=error):
                r=self.live(cap)
                r._ensure_automatic_cycle().update(state='blocked',failure_reason='response_timeout')
                _,d=self.sample(r,5.,25.2,current,receipt=4.8,sequence=2,accept=False,
                    meter_ev_health=logic.SourceHealth(not error,'estimate_error' if error else None))
                self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.OVERLOAD_NOT_ADMITTED.value)

    def test_selected_profile_requires_exact_seven_and_cold_keeps_unknown_sixteen(self):
        with self.assertRaises(ValueError):
            self.regulator(startup_charge_amps=16)
        r=self.regulator()
        r.settings=replace(r.settings,startup_charge_amps=16)
        _,d=self.sample(r,0.,4.,status='disconnected',accept=False)
        self.assertEqual(d.meter_zero_cause,logic.ElectricalStopCause.CONFIGURATION.value)
        cold=self.regulator(max_charge_amps=7,disconnected_readiness=False)
        _,d=self.sample(cold,0.,10.,status='awaiting_start',accept=False)
        self.assertEqual(cold._get_meter_envelope_policy().upper,16)
        self.assertFalse(d.should_call_service)

if __name__=='__main__': unittest.main()
