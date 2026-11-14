"""Public synthetic transition checks for bounded measured-RAW excursions.

No household history, HA connection or private-owner setup is used. Real
evaluate/dispatch/ACK transitions establish every accepted current boundary.
"""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_meter_envelope_policy as fixtures

logic=fixtures.logic


class RawExcursionPolicyTests(unittest.TestCase):
    frame=fixtures.MeterEnvelopePolicyTests.frame
    sample=fixtures.MeterEnvelopePolicyTests.sample
    live=fixtures.MeterEnvelopePolicyTests.live
    peak=fixtures.MeterEnvelopePolicyTests.peak

    def regulator(self,**changes):
        changes.setdefault('overload_margin_amps',3)
        return fixtures.MeterEnvelopePolicyTests.regulator(self,**changes)

    def accepted_minimum(self):
        r=self.regulator()
        for at,raw,ev,status,sequence in (
            (0.,16.,.012,'awaiting_start',1),
            (30.,16.,.012,'awaiting_start',2),
            (60.,16.,.012,'awaiting_start',3),
            (65.,26.3,7.128,'charging',4),
        ):
            self.sample(r,at,raw,ev,status=status,receipt=at-.2,sequence=sequence)
        self.assertEqual(r.last_target[0],7)
        self.assertTrue(r._get_meter_envelope_policy().minimum_verified)
        self.assertIsNone(r._get_meter_envelope_policy().episode)
        return r

    def high(self,r,at=70.,*,raw=31.9,receipt=None,sequence=5,accept=False,**kw):
        return self.sample(r,at,raw,7.128,receipt=at-.2 if receipt is None else receipt,
            sequence=sequence,accept=accept,**kw)

    def test_already_verified_minimum_gets_raw_window_without_new_dispatch(self):
        r=self.accepted_minimum()
        before=(r.last_target,r.last_set_limit_ts)
        _,d=self.high(r)
        self.assertEqual((d.current_p1,d.mode),(7,'meter_raw_peak_hold'))
        self.assertIsNone(d.meter_zero_cause)
        self.assertEqual(d.meter_raw_excursion_deadline_ts,80.)
        self.assertIsNone(d.nominal_response_deadline_ts)
        self.assertFalse(d.should_call_service)
        self.assertEqual((r.last_target,r.last_set_limit_ts),before)
        self.assertLess(r.effective_overload_used_seconds,1800.)

    def test_grid_sample_age_is_deducted_from_raw_window(self):
        r=self.accepted_minimum()
        _,d=self.high(r,receipt=67.8,grid_at=68.)
        self.assertEqual(d.meter_raw_excursion_deadline_ts,78.)

    def test_new_high_packets_and_same_packet_do_not_slide_deadline(self):
        r=self.accepted_minimum()
        self.high(r)
        for at,grid_at,receipt,sequence in ((71.,70.,69.8,5),(75.,75.,74.8,6),(79.9,79.9,79.7,7)):
            _,d=self.high(r,at,grid_at=grid_at,receipt=receipt,sequence=sequence)
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(d.meter_raw_excursion_deadline_ts,80.)
        _,d=self.high(r,80.,sequence=8)
        self.assertEqual((d.current_p1,d.meter_zero_cause),(0,'grid_ceiling'))
        self.assertIsNone(d.meter_raw_excursion_deadline_ts)

    def test_new_minimum_receipt_before_next_grid_cannot_create_another_reduction(self):
        r=self.accepted_minimum()
        self.high(r)
        _,new_ev=self.high(r,71.,grid_at=70.,receipt=70.8,sequence=6)
        self.assertEqual(new_ev.mode,'meter_raw_peak_hold')
        self.assertFalse(new_ev.should_call_service)
        self.assertIsNone(new_ev.nominal_response_deadline_ts)
        self.assertEqual(new_ev.meter_raw_excursion_deadline_ts,80.)
        for at,raw in ((75.,27.),(80.,27.)):
            _,low=self.high(r,at,raw=raw,receipt=70.8,sequence=6)
            self.assertIsNone(low.meter_zero_cause)
            self.assertEqual(low.current_p1,7)

    def test_partial_advisory_gap_retains_prior_genuine_minimum_without_fake_reduction(self):
        r=self.accepted_minimum()
        for at,raw in ((70.,31.9),(75.,27.),(80.,27.)):
            _,d=self.high(r,at,raw=raw,sequence=5,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'))
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(d.current_p1,7)
            self.assertIsNone(d.nominal_response_deadline_ts)
            if at==70.:
                self.assertEqual(d.meter_raw_excursion_deadline_ts,80.)
                self.assertFalse(d.should_call_service)

    def test_independent_conductor_minimum_can_survive_aggregate_increase_quality(self):
        r=self.accepted_minimum()
        inp=self.frame(70.,grid=31.9,current=7.128,receipt=69.8,sequence=5,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        d=r.evaluate(replace(inp,meter_ev_health=logic.SourceHealth(False,'increase_not_ready')))
        self.assertEqual(d.mode,'meter_raw_peak_hold')
        self.assertEqual(d.meter_raw_excursion_deadline_ts,80.)
        self.assertIsNone(d.nominal_response_deadline_ts)

    def test_uncommitted_minimum_publication_can_retain_but_not_extend_prior_certificate(self):
        r=self.accepted_minimum()
        certificate=r._get_meter_envelope_policy().minimum_certificate
        inp=self.frame(70.,grid=31.9,current=7.128,receipt=69.8,sequence=5,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        inp=replace(inp,controlled_l1=replace(inp.controlled_l1,committed=False),
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        d=r.evaluate(inp)
        self.assertEqual(d.mode,'meter_raw_peak_hold')
        self.assertEqual(r._get_meter_envelope_policy().minimum_certificate,certificate)
        self.assertEqual(d.meter_raw_excursion_deadline_ts,80.)

    def test_explicit_mapping_withdrawal_cannot_resurrect_from_later_partial_gap(self):
        r=self.accepted_minimum()
        inp=self.frame(70.,grid=31.9,current=7.128,receipt=69.8,sequence=5,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        r.evaluate(replace(inp,controlled_l1=None))
        _,later=self.high(r,71.,sequence=6,meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        self.assertNotEqual(later.mode,'meter_raw_peak_hold')
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)

    def test_advisory_missing_mapping_on_new_receiver_withdraws_prior_certificate(self):
        r=self.accepted_minimum()
        inp=self.frame(70.,grid=31.9,current=7.128,receipt=69.8,sequence=5,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        d=r.evaluate(replace(inp,charger_report_provenance=('different_receiver','max_terminals',182,183)))
        self.assertNotEqual(d.mode,'meter_raw_peak_hold')
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)

    def test_advancing_larger_uncommitted_conductor_withdraws_minimum_certificate(self):
        r=self.accepted_minimum()
        inp=self.frame(70.,grid=31.9,current=9.,receipt=69.8,sequence=5,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        inp=replace(inp,controlled_l1=replace(inp.controlled_l1,committed=False),
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        d=r.evaluate(inp)
        self.assertNotEqual(d.mode,'meter_raw_peak_hold')
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)

    def test_partial_gap_cannot_extend_minimum_certificate_past_original_lease(self):
        r=self.accepted_minimum()
        _,renew=self.high(r,115.,sequence=5,accept=True,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        self.assertEqual(renew.meter_raw_excursion_deadline_ts,120.)
        self.assertEqual(r.last_set_limit_ts,115.)
        _,expired=self.high(r,120.,sequence=6,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        self.assertEqual(expired.current_p1,0)
        self.assertIsNone(expired.meter_raw_excursion_deadline_ts)
        self.assertIsNone(expired.nominal_response_deadline_ts)

    def test_inflight_uses_original_raw_origin_and_clipped_deadline(self):
        r=self.accepted_minimum()
        inp,d=self.high(r,115.,sequence=5,accept=True)
        self.assertEqual(d.meter_raw_excursion_deadline_ts,120.)
        # A later genuine larger current withdraws the old physical minimum,
        # while the first actual RAW sample still owns the electrical limit.
        latest=self.frame(118.,grid=31.9,current=9.,receipt=117.8,sequence=6,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        r._get_meter_envelope_policy().observe_inflight(r,latest,d)
        episode=r._get_meter_envelope_policy().episode
        self.assertEqual((episode.origin,episode.deadline),(115.,120.))

    def test_long_quiet_same_cap_ack_chain_keeps_setting_not_fresh_ev_credit(self):
        r=self.accepted_minimum()
        first=r._get_meter_envelope_policy().minimum_certificate
        for index,at in enumerate(range(90,331,30),start=5):
            _,d=self.high(r,float(at),raw=24.,receipt=64.8,sequence=4,accept=True,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'),
                meter_product_data_quiet=True)
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(r.last_set_limit_ts,float(at))
            certificate=r._get_meter_envelope_policy().minimum_certificate
            self.assertEqual((certificate.received_at_ts,certificate.sequence),
                (first.received_at_ts,first.sequence))
            self.assertEqual(certificate.expires,float(at)+60.)
        _,peak=self.high(r,340.,receipt=64.8,sequence=4,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),
            meter_product_data_quiet=True)
        self.assertEqual((peak.mode,peak.meter_raw_excursion_deadline_ts),('meter_raw_peak_hold',350.))
        self.assertFalse(peak.should_call_service)
        self.assertIsNone(peak.nominal_response_deadline_ts)
        for at in (345.,350.,360.):
            _,low=self.high(r,at,raw=27.,receipt=64.8,sequence=4,accept=True,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'),
                meter_product_data_quiet=True)
            self.assertIsNone(low.meter_zero_cause)
            self.assertEqual(low.current_p1,7)

    def test_advisory_callbacks_without_actual_ack_cannot_extend_certificate(self):
        r=self.accepted_minimum()
        first=r._get_meter_envelope_policy().minimum_certificate
        for at in (90.,110.,119.):
            _,d=self.high(r,at,raw=24.,receipt=64.8,sequence=4,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'),
                meter_product_data_quiet=True)
            self.assertIsNone(d.meter_zero_cause)
            self.assertEqual(r._get_meter_envelope_policy().minimum_certificate.expires,first.expires)
            self.assertEqual(r.last_set_limit_ts,60.)
        _,expired=self.high(r,120.,raw=24.,receipt=64.8,sequence=4,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),
            meter_product_data_quiet=True)
        self.assertEqual(expired.meter_zero_cause,'lease_expired')
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)

    def test_same_cap_ack_after_original_chain_expiry_cannot_reuse_old_physical_proof(self):
        r=self.accepted_minimum()
        inp,d=self.high(r,90.,raw=24.,receipt=64.8,sequence=4,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),
            meter_product_data_quiet=True)
        self.assertTrue(r.begin_submission(d,90.,inp=inp))
        r.acknowledge(d,120.1)
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)
        _,peak=self.high(r,121.,sequence=5,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'))
        self.assertNotEqual(peak.mode,'meter_raw_peak_hold')

    def test_completed_disconnected_or_paused_session_cannot_start_auxiliary_overload(self):
        for status in ('completed','disconnected','paused','awaiting_scheduled_start'):
            r=self.accepted_minimum()
            _,d=self.high(r,70.,status=status,
                meter_ev_health=logic.SourceHealth(False,'partial_publication'))
            self.assertNotEqual(d.mode,'meter_raw_peak_hold')
            self.assertFalse(d.should_call_service)
            self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)

    def test_quiet_steady_minimum_retention_is_no_write_and_exact_finite_authority(self):
        r=self.accepted_minimum()
        inp,d=self.high(r,90.,raw=27.,receipt=64.8,sequence=4,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),meter_product_data_quiet=True)
        retained=r.meter_envelope_retention(inp,d)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.current_p1,7)
        self.assertFalse(retained.should_call_service)
        self.assertEqual(retained.normal_continuity_deadline_ts,120.)
        self.assertTrue(retained.meter_episode_retention)
        self.assertFalse(retained.meter_quiet_minimum_retention)

    def test_quiet_minimum_marker_is_exact_setting_continuation_without_ev_credit(self):
        r=self.accepted_minimum()
        self.high(r,90.,raw=27.,receipt=64.8,sequence=4,accept=True,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),meter_product_data_quiet=True)
        inp,d=self.high(r,95.,raw=24.,receipt=64.8,sequence=4,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),meter_product_data_quiet=True)
        self.assertTrue(d.meter_quiet_minimum_retention)
        self.assertEqual(d.meter_minimum_continuation_ceiling,28.)
        self.assertEqual((d.current_p1,d.committed_current_p1,d.ttl_seconds),(7,7,60))
        self.assertFalse(d.meter_overload_authority)
        self.assertFalse(d.controlled_l1_used)
        self.assertIsNone(d.controlled_l1_current)
        self.assertEqual(d.house_load_l1,24.)
        self.assertTrue(r.can_continue_meter_dispatch(inp,inp,d))

    def test_counterfeit_quiet_minimum_marker_without_physical_certificate_is_rejected(self):
        r=self.regulator()
        for at in (0.,30.,60.):
            self.sample(r,at,16.,.012,receipt=at-.2,sequence=int(at)+1,status='awaiting_start')
        inp,d=self.high(r,65.,raw=20.,receipt=64.8,sequence=100,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),meter_product_data_quiet=True)
        self.assertIsNone(r._get_meter_envelope_policy().minimum_certificate)
        self.assertFalse(d.meter_quiet_minimum_retention)
        forged=replace(d,mode='meter_normal',meter_quiet_minimum_retention=True,
            meter_minimum_continuation_ceiling=28.,meter_quiet_normal_retention=False)
        self.assertFalse(r.can_continue_meter_dispatch(inp,inp,forged))

    def test_quiet_old_preminimum_raw_preserves_original_grid_confirmation(self):
        r=self.peak()
        _,first=self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        _,quiet=self.sample(r,25.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.,accept=False,
            meter_ev_health=logic.SourceHealth(False,'partial_publication'),meter_product_data_quiet=True)
        self.assertIsNone(quiet.meter_zero_cause)
        self.assertEqual(quiet.grid_confirmation_deadline_ts,first.grid_confirmation_deadline_ts)
        self.assertIsNone(quiet.meter_raw_excursion_deadline_ts)

    def test_real_same_cap_renewal_keeps_original_excursion_deadline(self):
        r=self.accepted_minimum()
        _,first=self.high(r,89.,sequence=5)
        self.assertEqual(first.meter_raw_excursion_deadline_ts,99.)
        _,renew=self.high(r,90.,sequence=6,accept=True)
        self.assertTrue(renew.should_call_service)
        self.assertEqual(r.last_set_limit_ts,90.)
        _,later=self.high(r,95.,sequence=7)
        self.assertEqual(later.meter_raw_excursion_deadline_ts,99.)
        _,expired=self.high(r,99.,sequence=8)
        self.assertEqual(expired.meter_zero_cause,'grid_ceiling')

    def test_original_actual_lease_expiry_bounds_window_even_after_renewal(self):
        r=self.accepted_minimum()
        _,renew=self.high(r,115.,sequence=5,accept=True)
        self.assertEqual(renew.meter_raw_excursion_deadline_ts,120.)
        self.assertEqual(r.last_set_limit_ts,115.)
        _,expired=self.high(r,120.,sequence=6)
        self.assertEqual(expired.meter_zero_cause,'grid_ceiling')

    def test_new_low_grid_retires_excursion_but_not_exposure(self):
        r=self.accepted_minimum()
        self.high(r)
        used=r.effective_overload_used_seconds
        _,low=self.high(r,75.,raw=27.5,sequence=6)
        self.assertIsNone(low.meter_zero_cause)
        self.assertFalse(r.export_overload_state()['meter_control']['raw_excursion_active'])
        self.assertGreater(r.effective_overload_used_seconds,used)
        _,again=self.high(r,76.,sequence=7)
        self.assertEqual(again.meter_raw_excursion_deadline_ts,86.)
        self.assertGreater(r.effective_overload_used_seconds,used)

    def test_low_value_without_new_grid_timestamp_cannot_refill_window(self):
        r=self.accepted_minimum()
        self.high(r)
        self.high(r,75.,raw=27.5,grid_at=70.,receipt=69.8,sequence=5)
        self.assertTrue(r.export_overload_state()['meter_control']['raw_excursion_active'])
        _,again=self.high(r,76.,sequence=6)
        self.assertEqual(again.meter_raw_excursion_deadline_ts,80.)

    def test_new_minimum_response_and_new_high_grid_keep_first_raw_origin(self):
        r=self.peak()
        self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        _,high=self.sample(r,15.,29.1,7.1,receipt=11.8,sequence=3,accept=False)
        self.assertEqual((high.current_p1,high.meter_raw_excursion_deadline_ts),(7,20.))
        _,expired=self.sample(r,20.,29.1,7.1,receipt=11.8,sequence=3,accept=False)
        self.assertEqual(expired.meter_zero_cause,'grid_ceiling')

    def test_old_preminimum_raw_uses_separate_grid_confirmation(self):
        r=self.peak()
        _,first=self.sample(r,12.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.)
        _,old=self.sample(r,25.,35.,7.1,receipt=11.8,sequence=3,grid_at=10.,accept=False)
        self.assertIsNone(old.meter_zero_cause)
        self.assertIsNone(old.meter_raw_excursion_deadline_ts)
        self.assertEqual(old.grid_confirmation_deadline_ts,first.grid_confirmation_deadline_ts)
        # A later advancing still-high meter cannot start another ten seconds.
        _,late=self.sample(r,25.1,31.9,7.1,receipt=11.8,sequence=3,accept=False)
        self.assertEqual(late.meter_zero_cause,'grid_ceiling')

    def test_higher_current_still_needs_actual_minimum_in_original_ten_seconds(self):
        r=self.peak()
        _,d=self.sample(r,20.,31.9,16.1,receipt=4.8,sequence=2,accept=False)
        self.assertEqual(d.meter_zero_cause,'peak_response_timeout')

    def test_hard_transition_limit_preempts_raw_hold(self):
        r=self.accepted_minimum()
        self.high(r)
        _,d=self.high(r,71.,raw=38.01,sequence=6)
        self.assertEqual(d.meter_zero_cause,'hard_limit')

    def test_lost_mapping_cannot_use_sticky_minimum_or_restart_raw_clock(self):
        r=self.accepted_minimum()
        self.high(r)
        inp=self.frame(75.,grid=31.9,current=7.128,receipt=74.8,sequence=6,
            meter_grid_health=logic.SourceHealth(True),meter_actuator_health=logic.SourceHealth(True),
            meter_ev_health=logic.SourceHealth(True))
        d=r.evaluate(replace(inp,controlled_l1=None))
        self.assertNotEqual(d.mode,'meter_raw_peak_hold')
        self.assertEqual(d.nominal_response_deadline_ts,80.)

    def test_new_above_minimum_current_cannot_use_sticky_minimum(self):
        r=self.accepted_minimum()
        self.high(r)
        _,d=self.sample(r,75.,31.9,10.,receipt=74.8,sequence=6,accept=False)
        self.assertNotEqual(d.mode,'meter_raw_peak_hold')
        self.assertEqual(d.nominal_response_deadline_ts,80.)

    def test_current_grid_fault_preempts_without_resetting_exposure(self):
        r=self.accepted_minimum()
        self.high(r)
        used=r.effective_overload_used_seconds
        _,d=self.high(r,75.,sequence=6,meter_grid_health=logic.SourceHealth(False,'missing'))
        self.assertEqual(d.meter_zero_cause,'grid_invalid')
        self.assertGreaterEqual(r.effective_overload_used_seconds,used)

    def test_active_excursion_restore_is_recovery_not_a_new_raw_window(self):
        r=self.accepted_minimum()
        self.high(r)
        state=r.export_overload_state()
        self.assertTrue(state['meter_control']['raw_excursion_active'])
        restored=self.regulator()
        self.assertTrue(restored.import_overload_state(deepcopy(state)))
        self.assertEqual(restored.meter_control_diagnostics()['operational_fault'],
            'interrupted_raw_excursion_after_restore')
        self.assertFalse(restored.meter_control_diagnostics()['raw_excursion_active'])
        _,d=self.sample(restored,0.,31.9,7.128,receipt=-.2,sequence=1,accept=False)
        self.assertEqual(d.current_p1,0)
        self.assertIsNone(d.meter_raw_excursion_deadline_ts)
        self.assertGreaterEqual(restored.effective_overload_used_seconds,r.effective_overload_used_seconds)

    def test_legacy_v1_checkpoint_restores_without_raw_authority(self):
        r=self.accepted_minimum()
        state=r.export_overload_state()
        state['meter_control']['version']=1
        for key in tuple(state['meter_control']):
            if key.startswith('raw_excursion_'): state['meter_control'].pop(key)
        restored=self.regulator()
        self.assertTrue(restored.import_overload_state(state))
        self.assertFalse(restored.meter_control_diagnostics()['raw_excursion_active'])

    def test_no_write_io_retention_uses_same_raw_and_finite_lease_deadlines(self):
        r=self.accepted_minimum()
        inp,planned=self.high(r)
        retained=r.meter_envelope_retention(inp,planned)
        self.assertIsNotNone(retained)
        self.assertTrue(retained.meter_episode_retention)
        self.assertFalse(retained.should_call_service)
        self.assertEqual(retained.meter_raw_excursion_deadline_ts,80.)
        expired=replace(inp,now_ts=80.,wall_now_ts=fixtures.EPOCH+80.,grid_report_ts=fixtures.EPOCH+79.)
        self.assertIsNone(r.meter_envelope_retention(expired,planned))

    def test_both_28_and_existing_29_profiles_use_configured_ceiling(self):
        for margin,inside,outside in ((3,28.,28.01),(4,29.,29.01)):
            r=self.regulator(overload_margin_amps=margin)
            # Reuse the public minimum path with the selected settings.
            for at,raw,ev,status,sequence in ((0.,16.,.012,'awaiting_start',1),
                    (30.,16.,.012,'awaiting_start',2),(60.,16.,.012,'awaiting_start',3),
                    (65.,26.3,7.128,'charging',4)):
                self.sample(r,at,raw,ev,status=status,receipt=at-.2,sequence=sequence)
            _,normal=self.high(r,70.,raw=inside)
            self.assertIsNone(normal.meter_raw_excursion_deadline_ts)
            _,peak=self.high(r,71.,raw=outside,sequence=6)
            self.assertEqual(peak.meter_raw_excursion_deadline_ts,81.)


if __name__=='__main__': unittest.main()
