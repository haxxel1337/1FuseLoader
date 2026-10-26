"""Raw-peak ownership is separate from ordinary measurement uncertainty."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_1fuseloader_clock_provenance as fixtures

logic, EPOCH, SOURCE = fixtures.logic, fixtures.EPOCH, fixtures.SOURCE


class PeakResponsePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ClockProvenanceTests()

    def running(self):
        r = self.fixture.explicit_running_sixteen()
        r.update_settings(replace(r.settings, peak_only_response=True))
        self.assertEqual(r.last_target[0], 16)
        return r

    def observe(self, r, now, grid, *, gt=None, current=16.2, **metadata):
        fields = dict(now_ts=now, wall_now_ts=EPOCH+now, status="charging",
            grid_l1=grid, charger_current=current,
            grid_report_ts=EPOCH+(now if gt is None else gt),
            charger_report_ts=EPOCH+102., control_source_id=SOURCE)
        fields.update(metadata)
        r.observe_peak_response(logic.RegulatorInput(**fields))

    def publish(self, r, now, grid, current, *, et, gt=None, accept=False, **metadata):
        # Defer automatic route completion so this test exercises the public
        # completion API against real evaluated input/ACK evidence itself.
        with patch.object(r, "complete_peak_response", return_value=False):
            return self.fixture.step(r, now, grid, current, et=et, gt=gt,
                accept=accept, **metadata)

    def test_only_valid_raw_above_twenty_eight_to_hard_limit_owns_ten_seconds(self):
        self.assertEqual(logic.PEAK_RESPONSE_TRIGGER_AMPS, 28.)
        for grid, expected in ((2., False), (23., False), (25., False),
                (27.999, False), (28., False), (28.001, True),
                (35., True), (38., True), (38.001, False)):
            with self.subTest(grid=grid):
                r = self.running()
                self.observe(r, 110., grid, gt=108.5)
                self.assertEqual(r.peak_response_active, expected)
                self.assertEqual(r.peak_response_deadline(999.), 118.5 if expected else None)

    def test_legacy_default_does_not_implicitly_enable_the_new_peak_policy(self):
        r = self.fixture.explicit_running_sixteen()
        self.assertFalse(r.settings.peak_only_response)
        self.observe(r, 110., 31.)
        self.assertFalse(r.peak_response_active)
        self.assertIsNone(r.peak_response_deadline())
        self.assertNotIn("peak_response_pending", r.export_overload_state())

    def legacy_profile(self, enabled):
        return logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True,
            dry_run=False, overload_enabled=True, peak_only_response=enabled,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            measurement_settle_seconds=25, enable_soft_guard=False, enable_pause_resume=False))

    def legacy_step(self, r, now, grid, current, *, et=None, accept=True):
        d = r.evaluate(logic.RegulatorInput(now_ts=now, wall_now_ts=EPOCH+now,
            status="charging", grid_l1=grid, charger_current=current,
            grid_report_ts=EPOCH+now, charger_report_ts=EPOCH+(now if et is None else et),
            control_source_id=SOURCE))
        if accept and d.should_call_service:
            self.assertTrue(r.begin_submission(d, now))
            r.acknowledge(d, now)
        return d

    def test_new_mode_reduces_legacy_higher_offer_to_seven_false_stays_six(self):
        for enabled, expected in ((True, 7), (False, 6)):
            with self.subTest(enabled=enabled):
                r = self.legacy_profile(enabled)
                self.assertEqual(self.legacy_step(r, 0., 22., 16.).current_p1, 16)
                peak = self.legacy_step(r, 1., 31., 16., et=0., accept=False)
                self.assertEqual(r.overload_minimum_current, expected)
                self.assertEqual(peak.current_p1, expected)

    def test_existing_stricter_six_is_not_raised_and_its_real_proof_completes_peak(self):
        r = self.legacy_profile(True)
        self.assertEqual(self.legacy_step(r, 0., 23., 6.).current_p1, 6)
        self.legacy_step(r, 1., 23., 6.)  # Genuine reported6 after its changed-cap ACK.
        peak = self.legacy_step(r, 2., 31., 6., et=1., accept=False)
        self.assertEqual(r.overload_minimum_current, 6)
        self.assertLessEqual(peak.current_p1, 6)
        with patch.object(r, "complete_peak_response", return_value=False):
            self.legacy_step(r, 3., 27., 6., et=1., accept=False)
        self.assertTrue(r.complete_peak_response(3., minimum_response_ts=EPOCH+1., ceiling=29.))
        self.assertFalse(r.peak_response_active)
        self.assertEqual(r.last_target[0], 6)

    def test_first_source_age_is_spent_even_before_ack_or_api_response(self):
        r = self.running()
        self.observe(r, 115., 31., gt=103.)
        self.assertEqual(r.peak_response_deadline(), 113.)
        self.observe(r, 116., 31., gt=116.)
        self.assertEqual(r.peak_response_deadline(), 113.)

    def test_oscillation_and_identical_samples_do_not_rearm_unresolved_owner(self):
        r = self.running()
        before = deepcopy(r.export_overload_state())
        self.observe(r, 110., 31., gt=109.)
        owner = r._peak_response
        for now, grid in ((111., 27.), (112., 28.), (113., 31.), (125., 30.), (126., 2.)):
            self.observe(r, now, grid)
            self.assertIs(r._peak_response, owner)
            self.assertEqual(r.peak_response_deadline(), 119.)
        after = r.export_overload_state()
        self.assertEqual(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], before["failure_reason"])
        self.assertNotIn("trial_epoch", after)

    def test_house_only_idle_expired_lease_and_bad_source_cannot_arm(self):
        cases = (dict(status="disconnected"), dict(status="paused"), dict(current=.01),
            dict(data_valid=False), dict(charger_current_trusted=False),
            dict(control_source_id=("different",)), dict(gt=-100.),
            dict(grid_report_ts=None), dict(grid_l1=True))
        for metadata in cases:
            with self.subTest(metadata=metadata):
                r = self.running()
                self.observe(r, 110., 31., **metadata)
                self.assertFalse(r.peak_response_active)
        r = self.running()
        self.observe(r, 200., 31.)
        self.assertFalse(r.peak_response_active)

    def test_changed_binding_cannot_replace_a_live_peak_clock(self):
        r = self.running()
        self.observe(r, 110., 31.)
        owner = r._peak_response
        self.observe(r, 111., 35., control_source_id=("new", "charger"))
        self.assertIs(r._peak_response, owner)

    def test_whole_raw_completion_keeps_all_unresolved_higher_authority(self):
        r = self.running()
        self.observe(r, 110., 31.)
        self.publish(r, 111., 7.001, 16.2, et=102.)
        self.assertFalse(r.complete_peak_response(111.))
        self.assertEqual(r.peak_response_deadline(), 120.)
        self.publish(r, 112., 7., 16.2, et=102.)
        self.assertTrue(r.complete_peak_response(112.))
        self.assertFalse(r.peak_response_active)
        # Reusing an older high packet is neither a new crossing nor a refund.
        self.observe(r, 113., 31., gt=110.)
        self.assertFalse(r.peak_response_active)
        self.observe(r, 114., 31., gt=114.)
        self.assertEqual(r.peak_response_deadline(), 124.)

    def proven_minimum(self):
        r = self.running()
        lower = self.fixture.step(r, 104., 22., 6., et=103.)
        self.assertEqual(lower.current_p1, 7)
        self.assertEqual(r.last_target[0], 7)
        self.publish(r, 106., 13., 7.1, et=105.)
        self.assertIsNone(r._awaiting_reduction_target)
        self.assertEqual(r.last_target[0], 7)
        return r, lower

    def test_previously_proven_seven_uses_changed_cap_ack_not_later_ttl_ack(self):
        r = self.running()
        self.observe(r, 110., 31.)
        lower = self.fixture.step(r, 111., 22., 6., et=110.5)
        self.assertEqual(lower.current_p1, 7)
        self.publish(r, 113., 13., 7.1, et=112.)
        self.assertEqual(r.last_target[0], 7)
        deadline = r.peak_response_deadline()
        r.acknowledge(replace(lower, should_call_service=True), 140.)
        self.publish(r, 150., 15.5, 7.1, et=112.)
        self.assertEqual(r.peak_response_deadline(), deadline)
        self.assertTrue(r.complete_peak_response(150., minimum_response_ts=EPOCH+112., ceiling=23.))
        self.assertFalse(r.peak_response_active)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_unqualified_minimum_or_wrong_ceiling_cannot_complete_a_peak(self):
        r, _ = self.proven_minimum()
        self.observe(r, 110., 31., current=7.1)
        self.publish(r, 111., 27., 7.1, et=105.)
        self.assertFalse(r.complete_peak_response(111., minimum_response_ts=EPOCH+105., ceiling=29.))
        self.assertTrue(r.peak_response_active)
        self.assertFalse(r.complete_peak_response(111., minimum_response_ts=EPOCH+104., ceiling=23.))

    def test_late_report_of_previous_higher_offer_restores_whole_authority_bound(self):
        for current, expected in ((7.1, 7), (16.2, 16)):
            with self.subTest(current=current):
                r, _ = self.proven_minimum()
                self.observe(r, 110., 31., current=current)
                self.assertEqual(r._peak_response.previous_authority, expected)
                self.assertEqual(r._peak_whole_authority(), expected)

    def vector_minimum(self, *, sequence=3, provider=105.):
        r = self.running()
        counters = lambda value: ((182, 0), (183, value), (184, 0), (185, 0))
        self.fixture.step(r, 103., 21.7, 16.2, et=103.,
            vector=((183, 103.),), sequences=counters(1), accept=False)
        lower = self.fixture.step(r, 104., 22., 6., et=103.5,
            vector=((183, 103.5),), sequences=counters(2))
        self.assertEqual(lower.current_p1, 7)
        values = dict(et=105., vector=((183, 105.),), sequences=counters(sequence),
            source_known=True, source_ts=provider)
        self.publish(r, 106., 13., 7.1, **values)
        self.observe(r, 107., 31., current=7.1)
        self.publish(r, 108., 20., 7.1, **values)
        return r

    def test_native_per_conductor_post_ack_minimum_proof_can_complete(self):
        r = self.vector_minimum()
        self.assertTrue(r._peak_minimum_proof(EPOCH+105.))
        self.assertTrue(r.complete_peak_response(108., minimum_response_ts=EPOCH+105., ceiling=23.))
        self.assertFalse(r.peak_response_active)

    def test_same_native_counter_or_pre_ack_provider_cannot_prove_minimum(self):
        for values in (dict(sequence=2), dict(provider=103.)):
            with self.subTest(values=values):
                r = self.vector_minimum(**values)
                self.assertFalse(r._peak_minimum_proof(EPOCH+105.))
                self.assertFalse(r.complete_peak_response(108., minimum_response_ts=EPOCH+105., ceiling=23.))
                self.assertTrue(r.peak_response_active)

    def test_reset_and_restore_interrupt_without_a_fresh_electrical_clock(self):
        r = self.running()
        self.observe(r, 110., 31., gt=109.)
        saved = deepcopy(r.export_overload_state())
        self.assertTrue(saved["peak_response_pending"])
        r.reset()
        self.assertTrue(r.peak_response_active)
        self.assertIsNone(r.peak_response_deadline())
        self.assertTrue(r._restart_required)
        self.assertGreaterEqual(r.export_overload_state()["normal_control"]["stops"], 1)
        restored = self.fixture.regulator()
        self.assertTrue(restored.import_overload_state(saved))
        self.assertFalse(restored.settings.peak_only_response)
        self.assertFalse(restored.peak_response_active)
        self.assertTrue(restored.export_overload_state()["peak_response_pending"])
        self.assertTrue(restored._restart_required)
        restored.update_settings(replace(restored.settings, peak_only_response=True))
        self.assertTrue(restored.peak_response_active)
        self.assertIsNone(restored.peak_response_deadline())
        self.assertTrue(restored._restart_required)
        self.assertEqual(restored.export_overload_state()["used_seconds"], saved["used_seconds"])
        self.assertEqual(restored.export_overload_state()["failure_reason"], saved["failure_reason"])
        self.observe(restored, 200., 31.)
        self.assertIsNone(restored.peak_response_deadline())

    def test_invalid_additive_peak_state_does_not_erase_valid_historical_exposure(self):
        r = self.running()
        state = deepcopy(r.export_overload_state())
        state["peak_response_pending"] = "yes"
        restored = self.fixture.regulator()
        self.assertFalse(restored.import_overload_state(state))
        self.assertTrue(restored._restart_required)
        self.assertIsNone(restored.peak_response_deadline())
        self.assertEqual(restored.export_overload_state()["used_seconds"], state["used_seconds"])
        self.assertEqual(restored.export_overload_state()["failure_reason"], state["failure_reason"])

    def test_actual_idle_policy_can_clear_peak_without_resetting_thermal_budget(self):
        r = self.running()
        self.observe(r, 110., 31.)
        before = r.export_overload_state()
        self.publish(r, 111., 2., 0., et=111., status="disconnected")
        self.assertTrue(r.clear_peak_response_session())
        self.assertFalse(r.peak_response_active)
        self.assertEqual(r.export_overload_state()["used_seconds"], before["used_seconds"])
        self.assertEqual(r.export_overload_state()["failure_reason"], before["failure_reason"])


if __name__ == "__main__":
    unittest.main()
