"""A full waiting car can prove normal headroom without drawing current.

Synthetic source frames and public persisted state drive actual submission
and ACK boundaries. The native zero event never becomes a new measurement.
"""
import unittest

import test_1fuseloader_positive_ramp_owner as fixtures

logic = fixtures.logic


class IdleRawRecoveryTests(unittest.TestCase):
    frame = fixtures.PositiveRampOwnerTests.frame

    def regulator(self, *, maximum=16, stops=0, retry_wait=0., **options):
        values = dict(active_control=True, dry_run=False, overload_enabled=True,
            peak_only_response=True, automatic_overload_cycles=True,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            enable_soft_guard=False, enable_pause_resume=False,
            max_charge_amps=maximum, measurement_settle_seconds=25)
        values.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**values))
        state = r.export_overload_state()
        state.update(used_seconds=3601., failure_reason="response_timeout")
        state["normal_control"] = dict(version=1, response_pending=not stops,
            alignment_pending=False, cap=7 if stops else maximum,
            previous_offer=maximum, stops=stops, stop_recorded=False,
            restart=bool(stops), retry_wait_seconds=retry_wait,
            response_failure="positive_retry_submission_failed" if stops >= 3 else None,
            retry_submission_pending=False)
        self.assertTrue(r.import_overload_state(state))
        return r

    def sample(self, r, at, *, accept=True, status="awaiting_start", raw=3.,
               grid_at=None, **metadata):
        inp = self.frame(at, status=status, grid=raw, current=0.,
            receipt=0. if at == 0 else 4.9, sequence=1 if at == 0 else 2,
            grid_at=grid_at, **metadata)
        d = r.evaluate(inp)
        if accept and d.should_call_service:
            self.assertTrue(r.begin_submission(d, at, inp=inp))
            r.acknowledge(d, at+.1)
        return d

    def recovered_seven(self, **options):
        r = self.regulator(**options)
        for at in range(0, 61, 5):
            self.sample(r, float(at))
        self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 1)
        return r

    def assert_history(self, r):
        state = r.export_overload_state()
        self.assertEqual((state["used_seconds"], state["failure_reason"]),
                         (3601., "response_timeout"))
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0.)

    def test_both_connected_idle_statuses_recover_after_thirty_seconds(self):
        for status in ("awaiting_start", "ready_to_charge"):
            for maximum in (12, 16):
                with self.subTest(status=status, maximum=maximum):
                    r = self.recovered_seven(maximum=maximum)
                    native = r._last_input_charger_ts
                    for at in (65., 70., 80., 90., 94.9):
                        self.sample(r, at, status=status)
                        self.assertEqual(r.last_target[0], 7)
                    d = self.sample(r, 95., status=status, accept=False)
                    self.assertEqual((d.command, d.current_p1), ("set_limit", maximum))
                    # Planning the independent offer is not an acceptance.
                    self.assertEqual(r.last_target[0], 7)
                    self.assertEqual(r._last_input_charger_ts, native)
                    self.assertIsNone(r._positive_response_ts)
                    self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 0)
                    self.assert_history(r)

    def test_elapsed_time_without_advancing_meter_epochs_does_not_recover(self):
        r = self.recovered_seven()
        for at in (65., 75., 85., 95.):
            self.sample(r, at, grid_at=65.)
            self.assertEqual(r.last_target[0], 7)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 1)
        self.assertEqual(self.sample(r, 96.).current_p1, 16)

    def test_full_prospective_offer_must_fit_beside_raw_import(self):
        r = self.recovered_seven()
        for at in range(65, 111, 5):
            self.sample(r, float(at), raw=10.)
            self.assertLessEqual(r.last_target[0], 7)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"], 1)
        self.assert_history(r)

    def test_incomplete_native_frame_restarts_the_recovery_interval(self):
        r = self.recovered_seven()
        for at in (65., 70.):
            self.sample(r, at)
        self.sample(r, 75., charger_native_committed=False)
        for at in (80., 90., 100., 109.9):
            self.sample(r, at)
            self.assertEqual(r.last_target[0], 7)
        self.assertEqual(self.sample(r, 110.).current_p1, 16)

    def test_source_fault_preempts_and_does_not_spend_invalid_time_as_recovery(self):
        r = self.recovered_seven()
        for at in (65., 70.):
            self.sample(r, at)
        failed = self.sample(r, 75., data_valid=False, data_error="synthetic_source_fault")
        self.assertEqual(failed.current_p1, 0)
        self.assertIsNone(r._uncertainty_recovery_since)
        for at in range(80, 111, 5):
            self.assertLessEqual(self.sample(r, float(at)).current_p1, 7)
        self.assert_history(r)

    def test_terminal_retry_backoff_and_missing_ack_do_not_release(self):
        for problem in ("terminal", "backoff", "no_ack"):
            with self.subTest(problem=problem):
                if problem == "terminal":
                    r = self.regulator(stops=3)
                elif problem == "backoff":
                    r = self.regulator(stops=1, retry_wait=300.)
                else:
                    r = self.regulator()
                for at in range(0, 111, 5):
                    d = self.sample(r, float(at), accept=problem != "no_ack")
                    self.assertLessEqual(d.current_p1, 7)
                if problem == "no_ack":
                    self.assertIsNone(r.last_target)
                elif problem == "terminal":
                    self.assertGreaterEqual(r.export_overload_state()["normal_control"]["stops"], 3)
                self.assert_history(r)

    def test_unaccepted_recovery_lease_and_expired_lease_are_not_headroom_proof(self):
        r = self.recovered_seven()
        self.sample(r, 65.)
        # Do not send or ACK renewals. Fresh RAW cannot repair the old lease.
        for at in (75., 85., 94.):
            self.sample(r, at, accept=False)
        d = self.sample(r, 121., accept=False)
        self.assertEqual(d.current_p1, 0)
        self.assertNotEqual(r._uncertainty_ceiling, None)

    def test_long_idle_renews_finite_maximum_without_ev_or_response_clock(self):
        r = self.recovered_seven()
        previous = None
        renewals = 0
        for at in range(65, 336, 5):
            d = self.sample(r, float(at))
            if at >= 95:
                self.assertEqual(d.current_p1, 16)
                self.assertIsNone(d.positive_response_deadline_ts)
                self.assertIsNone(d.grid_confirmation_deadline_ts)
                if d.should_call_service:
                    if previous is not None:
                        self.assertLess(at, previous+d.ttl_seconds)
                        self.assertLessEqual(at-previous, d.ttl_seconds/2+5)
                    previous = at
                    renewals += 1
        self.assertGreater(renewals, 3)
        self.assertEqual(r._last_input_charger_ts, fixtures.EPOCH+4.9)
        self.assertIsNone(r._positive_response_ts)
        self.assert_history(r)


if __name__ == "__main__":
    unittest.main()
