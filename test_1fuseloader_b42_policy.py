"""Standalone state/history/recovery contracts through public SDK boundaries."""
from copy import deepcopy
from dataclasses import replace
import unittest
from test_1fuseloader_b40_policy import MappedInstallation,logic


class B42StandalonePolicyTests(unittest.TestCase):
    def setUp(self): self.f=MappedInstallation()

    def legacy(self,*,used=35.001,state='blocked',reason='peak_response_timeout'):
        source=self.f.frame(0.,grid=3.,current=.012,receipt=0.).control_source_id
        return dict(version=1,used_seconds=4567.384568,exposure_active=False,transition_pending=False,
            minimum_held=False,failure_reason='response_timeout',automatic_cycle=dict(version=1,cycle_id=9,
                state=state,used_seconds=used,source_id=source,failure_reason=reason))

    def started(self,used=35.001,state='blocked'):
        r=self.f.regulator();self.assertTrue(r.import_overload_state(self.legacy(used=used,state=state)))
        for at in range(0,61,5):
            self.f.sample(r,float(at),3.,.012,receipt=max(0.,at-.2),sequence=at+1,status='awaiting_start')
        self.assertEqual(r.last_target[0],16)
        return r

    def proved(self,used=35.001,state='blocked'):
        r=self.started(used,state)
        self.f.sample(r,65.,19.1,16.1,receipt=64.8,sequence=100)
        self.assertIsNone(r.meter_control_diagnostics()['operational_fault'])
        return r

    def test_unspent_blocked_cycle_recovers_after_full_normal_receipt_without_refund(self):
        r=self.started();before=r.export_overload_state()
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(before['automatic_cycle']['cycle_id'],9)
        self.f.sample(r,65.,19.1,16.1,receipt=64.8,sequence=100)
        recovered=r.export_overload_state()
        self.assertEqual(recovered['automatic_cycle']['used_seconds'],35.001)
        self.assertEqual(recovered['automatic_cycle']['cycle_id'],9)
        self.assertEqual(recovered['automatic_cycle']['failure_reason'],'peak_response_timeout')
        self.assertEqual(recovered['used_seconds'],before['used_seconds'])
        self.assertEqual(recovered['failure_reason'],'response_timeout')
        self.assertFalse(recovered['meter_control']['recovery_required'])
        self.assertTrue(all(event['resolved'] for event in recovered['meter_control']['history']))
        self.f.sample(r,70.,31.,16.1,receipt=64.8,sequence=100)
        _,response=self.f.sample(r,72.,31.,8.34,mapped_current=7.094,receipt=71.8,sequence=101,grid_at=70.)
        self.assertEqual(response.meter_control_state,'GRID_WAIT')
        _,hold=self.f.sample(r,75.,28.5,8.34,mapped_current=7.094,receipt=71.8,sequence=101)
        self.assertIsNone(hold.meter_zero_cause)
        self.assertEqual(hold.meter_control_state,'MINIMUM_HOLD')
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],9)
        self.assertGreater(r.export_overload_state()['automatic_cycle']['used_seconds'],35.001)

    def test_acceptance_or_cached_phase_alone_cannot_recover_a_saved_fault(self):
        r=self.started()
        for at in (65.,70.,75.):
            self.f.sample(r,at,19.1,.012,receipt=59.8,sequence=61)
        self.assertTrue(r.meter_control_diagnostics()['recovery_required'])
        _,high=self.f.sample(r,80.,31.,.012,receipt=59.8,sequence=61)
        self.assertEqual(high.meter_zero_cause,'overload_not_admitted')
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],9)

    def test_delayed_ev_after_normal_sdk_does_not_erase_completed_startup_stability(self):
        r=self.started()
        self.f.sample(r,63.,21.,.012,receipt=59.8,sequence=61)
        self.assertEqual(r.meter_control_diagnostics()['recovery_stage'],'await_normal_response')
        self.f.sample(r,70.,21.,16.,receipt=69.8,sequence=100)
        self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(r.export_overload_state()['automatic_cycle']['used_seconds'],35.001)

    def test_exhausted_budget_stays_exhausted_after_operational_recovery(self):
        r=self.proved(1800.,'exhausted')
        self.assertEqual(r.export_overload_state()['automatic_cycle']['used_seconds'],1800.)
        _,high=self.f.sample(r,70.,28.5,16.1,receipt=64.8,sequence=100)
        self.assertEqual(high.meter_zero_cause,'overload_budget')
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],9)

    def test_ready_history_does_not_create_a_current_operational_fault(self):
        r=self.f.regulator();saved=self.legacy(used=0.,state='ready',reason=None)
        self.assertTrue(r.import_overload_state(saved))
        self.assertIsNone(r.meter_control_diagnostics()['operational_fault'])
        self.assertFalse(r.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(r.export_overload_state()['failure_reason'],'response_timeout')

    def test_normal_mode_is_independent_of_legacy_extra_flags(self):
        r=self.f.regulator(overload_enabled=False,automatic_overload_cycles=False,peak_only_response=False,
            nominal_import_only_fallback=False,bounded_nominal_shedding=False)
        self.f.sample(r,0.,3.,status='disconnected',receipt=0.)
        for at in range(5,206,5):
            _,d=self.f.sample(r,float(at),19.2,16.2,receipt=at-.2,sequence=at)
            self.assertIsNone(d.meter_zero_cause);self.assertEqual(d.current_p1,16)
        self.assertEqual(r.effective_overload_used_seconds,0.)
        _,bad=self.f.sample(r,210.,37.4,16.2,receipt=209.8,sequence=210)
        self.assertEqual(bad.meter_zero_cause,'overload_not_admitted')

    def test_standalone_reset_does_not_reset_fault_cycle_or_active_episode_clock(self):
        r=self.proved();self.f.sample(r,70.,31.,16.1,receipt=64.8,sequence=100)
        diag=deepcopy(r.meter_control_diagnostics());saved=r.export_overload_state()
        r.reset();r.reset_overload_budget()
        self.assertEqual(r.meter_control_diagnostics()['episode_id'],diag['episode_id'])
        self.assertEqual(r.peak_response_deadline(),diag['response_deadline'])
        self.assertEqual(r.export_overload_state()['automatic_cycle']['used_seconds'],saved['automatic_cycle']['used_seconds'])
        self.assertEqual(r.export_overload_state()['automatic_cycle']['cycle_id'],9)

    def test_recovered_checkpoint_restores_history_without_a_lease(self):
        r=self.proved();saved=r.export_overload_state();new=self.f.regulator()
        self.assertTrue(new.import_overload_state(saved));self.assertIsNone(new.last_target)
        self.assertFalse(new.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(new.export_overload_state()['meter_control']['history'],saved['meter_control']['history'])
        self.assertEqual(new.effective_overload_used_seconds,35.001)

    def test_malformed_additive_schema_fails_closed_without_lifetime_refund(self):
        r=self.proved();good=r.export_overload_state()
        mutations=[('version',True),('version',2),('next_episode_id',True),('next_episode_id',0),
            ('state','SOMETHING'),('recovery_stage','unknown'),('operational_fault',True),
            ('history',[dict(reason='x',resolved=True,cycle_id=9,episode_id=None,used_seconds='35')]),
            ('history',[dict(reason='x',resolved=True,cycle_id=9,episode_id=None,used_seconds=float('nan'))]),
            ('history',[dict(reason='x',resolved=True,cycle_id=9,episode_id=None,used_seconds=-1)]),
            ('history',[dict(reason='x'*161,resolved=True,cycle_id=9,episode_id=None,used_seconds=35.)])]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                bad=deepcopy(good);bad['meter_control'][key]=value;new=self.f.regulator()
                self.assertFalse(new.import_overload_state(bad));self.assertIsNone(new.last_target)
                self.assertGreaterEqual(new.export_overload_state()['used_seconds'],good['used_seconds'])
                self.assertGreaterEqual(new.effective_overload_used_seconds,1800.)

    def test_active_minimum_checkpoint_requires_new_normal_recovery(self):
        r=self.proved();self.f.sample(r,70.,31.,16.1,receipt=64.8,sequence=100)
        self.f.sample(r,72.,31.,7.1,receipt=71.8,sequence=101,grid_at=70.)
        self.f.sample(r,75.,28.5,7.1,receipt=71.8,sequence=101)
        saved=r.export_overload_state();self.assertEqual(saved['meter_control']['state'],'MINIMUM_HOLD')
        new=self.f.regulator();self.assertTrue(new.import_overload_state(saved))
        self.assertIsNone(new.last_target);self.assertTrue(new.meter_control_diagnostics()['recovery_required'])
        self.assertEqual(new.export_overload_state()['automatic_cycle']['cycle_id'],9)
        self.assertEqual(new.effective_overload_used_seconds,saved['automatic_cycle']['used_seconds'])

    def test_invalid_configuration_after_an_accepted_zero_remains_a_typed_fault(self):
        for changes in (dict(max_sensor_age_seconds=None),dict(overload_budget_seconds=None),
                        dict(max_charge_amps=None),dict(resend_limit_every_seconds=None),dict(buffer_amps=float('nan'))):
            with self.subTest(changes=changes):
                r=self.f.live();self.f.sample(r,5.,39.,16.,receipt=4.8,sequence=2)
                self.assertEqual(r.last_target[0],0)
                r.settings=replace(r.settings,**changes)
                _,fault=self.f.sample(r,6.,3.,0.,receipt=5.8,sequence=3,accept=False)
                self.assertEqual(fault.meter_zero_cause,'configuration')


if __name__=='__main__': unittest.main()
