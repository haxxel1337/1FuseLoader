"""A timely accepted startup minimum uses the shared fixed grid phase."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_expired_start_handoff as fixtures
from test_1fuseloader_trial_epochs import EPOCH

logic = fixtures.logic


class StartupMinimumConfirmationTests(unittest.TestCase):
    frame = fixtures.ExpiredStartHandoffTests.frame

    def prepared(self, *, startup=7, maximum=16):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False,
            overload_enabled=True, nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=startup == 7,
            enable_soft_guard=False, enable_pause_resume=False,
            startup_charge_amps=startup, max_charge_amps=maximum,
            measurement_settle_seconds=25, grid_confirmation_seconds=30))
        r.import_overload_state(dict(version=1, used_seconds=3601., exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))
        for now in (0., 60.):
            d = r.evaluate(self.frame(now, grid=23.-startup, current=.012,
                receipt=None, sequence=0, status="awaiting_start"))
            if d.should_call_service:
                r.acknowledge(d, now)
        self.assertEqual(r.last_target[0], startup)
        d = r.evaluate(self.frame(65., grid=22., current=.012,
            receipt=None, grid_at=65., sequence=0, status="awaiting_start"))
        self.assertEqual(d.mode, "positive_ramp_hold")
        self.assertEqual(d.positive_response_deadline_ts, 90.)
        return r

    def response(self, r, now=90., **extra):
        fields = dict(current=6.4, receipt=89.8, grid_at=89.5, sequence=1)
        fields.update(extra)
        return r.evaluate(self.frame(now, grid=22., **fields))

    def test_timely_minimum_gets_fixed_thirty_without_new_command_or_lease(self):
        r = self.prepared()
        d = self.response(r)
        self.assertEqual((d.mode, d.current_p1, d.command), ("startup_grid_wait", 7, None))
        self.assertFalse(d.should_call_service)
        self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 119.8)
        self.assertEqual(r._positive_ramp_deadline, 90.)
        self.assertIsNone(d.positive_response_deadline_ts)
        self.assertEqual(r.last_set_limit_ts, 60.)
        done = self.response(r, 91., grid_at=91.)
        self.assertGreaterEqual(done.current_p1, 7)
        self.assertIsNone(r._minimum_confirmation)
        self.assertIsNone(r._positive_cap)

    def test_native_response_preceding_allowed_status_delivery_does_not_send_zero(self):
        for status in ("awaiting_start", "ready_to_charge", "start_charging"):
            with self.subTest(status=status):
                r = self.prepared()
                d = self.response(r, status=status)
                self.assertEqual((d.mode, d.current_p1, d.command), ("startup_grid_wait", 7, None))
                self.assertFalse(d.should_call_service)
                self.assertAlmostEqual(d.grid_confirmation_deadline_ts, 119.8)
                self.assertEqual(r.last_set_limit_ts, 60.)
        for status in ("disconnected", "completed", "paused", "awaiting_scheduled_start", "awaiting_authorization"):
            with self.subTest(excluded=status):
                r = self.prepared()
                d = self.response(r, status=status)
                self.assertNotEqual(d.mode, "startup_grid_wait")
                self.assertIsNone(r._minimum_confirmation)

    def test_new_events_and_ttl_receipt_cannot_extend_first_phase_or_original_lease_cap(self):
        r = self.prepared()
        first = self.response(r)
        for now, ev in ((95., 6.6), (105., 6.3)):
            d = self.response(r, now, current=ev, receipt=now-.2,
                grid_at=now-.5, sequence=int(now))
            self.assertEqual(d.mode, "startup_grid_wait")
            self.assertEqual(d.grid_confirmation_deadline_ts, first.grid_confirmation_deadline_ts)
        # Even an erroneously arriving identical lease receipt cannot move
        # the phase's already clipped original lease/deadline.
        renewal = replace(first, mode="normal", command="set_limit", should_call_service=True)
        r.acknowledge(renewal, 110.)
        self.assertEqual(r._minimum_confirmation.deadline, first.grid_confirmation_deadline_ts)
        late = self.response(r, first.grid_confirmation_deadline_ts,
            current=6.3, receipt=104.8, grid_at=119.8, sequence=105)
        self.assertEqual(late.current_p1, 0)

    def test_late_coherent_grid_cannot_complete_expired_phase_but_whole_raw_may_exit(self):
        r = self.prepared()
        first = self.response(r)
        d = self.response(r, first.grid_confirmation_deadline_ts, grid_at=119.8)
        self.assertEqual(d.current_p1, 0)
        self.assertIn("timeout", d.reason)
        r = self.prepared()
        first = self.response(r)
        d = r.evaluate(self.frame(first.grid_confirmation_deadline_ts, grid=10.,
            current=6.4, receipt=89.8, grid_at=119.8, sequence=1))
        self.assertGreaterEqual(d.current_p1, 7)
        self.assertIsNone(r._minimum_confirmation)
        self.assertIsNone(d.grid_confirmation_deadline_ts)

    def test_late_get_uncommitted_or_unadvanced_values_cannot_create_phase(self):
        for extra in ({"receipt": 90.05}, {"charger_snapshot_verified": True},
                {"charger_native_committed": False}, {"sequence": 0},
                {"charger_event_source_known": True, "charger_event_source_ts": EPOCH+59.}):
            with self.subTest(extra=extra):
                r = self.prepared()
                d = self.response(r, 90.1, **extra)
                self.assertNotEqual(d.mode, "startup_grid_wait")
                self.assertIsNone(r._minimum_confirmation)

    def test_configured_higher_startup_cap_gets_no_minimum_phase(self):
        for cap in (10, 16):
            r = self.prepared(startup=cap)
            d = self.response(r, current=cap-.2)
            self.assertNotEqual(d.mode, "startup_grid_wait")
            self.assertFalse(r._minimum_confirmation and r._minimum_confirmation.owner == "startup")

    def test_export_restore_reset_and_reconfigure_interrupt_instead_of_restarting_phase(self):
        for interruption in ("restore", "reset", "settings"):
            with self.subTest(interruption=interruption):
                r = self.prepared()
                self.response(r)
                before = deepcopy(r.export_overload_state())
                self.assertTrue(before["normal_control"]["response_pending"])
                self.assertEqual(before["normal_control"]["cap"], 7)
                self.assertEqual(before["normal_control"]["previous_offer"], 7)
                if interruption == "restore":
                    r = logic.OneFuseRegulator(r.settings)
                    self.assertTrue(r.import_overload_state(before))
                elif interruption == "reset":
                    r.reset()
                else:
                    r.update_settings(replace(r.settings, resend_limit_every_seconds=31))
                self.assertIsNone(r._minimum_confirmation)
                self.assertTrue(r._restart_required)
                self.assertIsNotNone(r._normal_shed_failure)
                after = r.export_overload_state()
                self.assertEqual(after["used_seconds"], before["used_seconds"])
                self.assertEqual(after["failure_reason"], before["failure_reason"])


if __name__ == "__main__":
    unittest.main()
