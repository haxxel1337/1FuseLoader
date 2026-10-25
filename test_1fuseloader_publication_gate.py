"""A publication fence spends no authority, recovery attempt or response time."""
from dataclasses import replace
import unittest

import test_1fuseloader_clock_provenance as clocks
import test_1fuseloader_continuity as continuity
import test_1fuseloader_normal_shedding as shedding


class PublicationGateTests(unittest.TestCase):
    def bootstrap_owner(self):
        fixture = clocks.ClockProvenanceTests()
        r = fixture.regulator()
        zeros = tuple((key, 0) for key in clocks.IDS)
        for now in (0, 60):
            fixture.step(r, now, 4., 0., et=None, vector=(), sequences=zeros, status="awaiting_start")
        self.assertEqual(r.last_target[0], 16)
        return fixture, r, zeros

    def test_pending_bootstrap_retains_same_owner_without_any_proof_or_time_change(self):
        fixture, r, zeros = self.bootstrap_owner()
        before = (r._positive_house, r._positive_grid_ts, r._positive_ev_ts,
            r._positive_skew_since, r._positive_alignment_used_seconds, r.last_set_limit_ts)
        state = r.export_overload_state()
        for now, grid in ((61, 20.1), (61.5, 23.)):
            d = fixture.step(r, now, grid, 0., et=None, vector=(), sequences=zeros,
                charger_publication_pending=True, accept=False)
            self.assertEqual((d.current_p1, d.command), (16, None))
            self.assertFalse(d.should_call_service)
            self.assertEqual((r._positive_house, r._positive_grid_ts, r._positive_ev_ts,
                r._positive_skew_since, r._positive_alignment_used_seconds, r.last_set_limit_ts), before)
            self.assertEqual(r.export_overload_state(), state)

    def test_ordinary_qualified_owner_with_import_fallback_off_is_preserved(self):
        fixture = clocks.ClockProvenanceTests()
        r = fixture.regulator()
        r.update_settings(replace(r.settings, nominal_import_only_fallback=False))
        # Fresh ordered zero observations qualify an ordinary start without
        # relying on the optional import-only unknown-clock bootstrap path.
        for now in (0, 60):
            fixture.step(r, now, 4., 0., status="awaiting_start")
        self.assertEqual(r.last_target[0], 16)
        d = fixture.step(r, 61, 20.1, 0., et=60, charger_publication_pending=True, accept=False)
        self.assertEqual((d.current_p1, d.command), (16, None))
        self.assertFalse(r.settings.nominal_import_only_fallback)

    def test_pending_owner_source_status_raw_and_lease_bounds_are_not_bypassed(self):
        cases = [
            dict(grid=23.001), dict(grid=39.), dict(current=17.),
            dict(status="paused"), dict(status="completed"), dict(status="disconnected"),
            dict(now=120.), dict(data_valid=False, data_error="source_fault"),
            dict(charger_current_trusted=False), dict(control_source_id="different_selected_source"),
            dict(gt=-1000.),
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                fixture, r, zeros = self.bootstrap_owner()
                metadata = metadata.copy()
                now = metadata.pop("now", 61.)
                grid = metadata.pop("grid", 20.1)
                current = metadata.pop("current", 0.)
                d = fixture.step(r, now, grid, current, et=None, vector=(), sequences=zeros,
                    charger_publication_pending=True, accept=False, **metadata)
                self.assertNotEqual(d.house_load_basis, "accepted_positive_owner_during_raw_publication")

    def test_prior_owner_failure_authority_and_response_deadlines_are_not_bypassed(self):
        changes = [
            ("_positive_cap", None),
            ("_positive_house", 8.), ("_positive_alignment_used_seconds", 25.),
            ("_positive_skew_since", 36.), ("_normal_shed_failure", "response_timeout"),
            ("_low_grid_failure", "alignment_timeout"), ("_restart_required", True),
            ("paused_by_us", True), ("_uncertainty_retry_submission_pending", True),
            ("_normal_shed_deadline", 60.5), ("_overload_transition_started", 60.),
            ("_awaiting_reduction_target", 7), ("last_target", (15, 0, 0, 60)),
        ]
        for name, value in changes:
            with self.subTest(name=name):
                fixture, r, zeros = self.bootstrap_owner()
                # Establish the same ordinary pending frame, then invalidate
                # exactly one already owned bound without fabricating proof.
                fixture.step(r, 61, 20.1, 0., et=None, vector=(), sequences=zeros,
                    charger_publication_pending=True, accept=False)
                setattr(r, name, value)
                self.assertIsNone(r._publication_pending_positive_owner(61, 20.1, 0., "charging"))

    def test_pending_does_not_refund_already_running_positive_alignment_time(self):
        fixture, r, zeros = self.bootstrap_owner()
        r._positive_skew_since = 60.
        for now in (61., 61.5):
            d = fixture.step(r, now, 20.1, 0., et=None, vector=(), sequences=zeros,
                charger_publication_pending=True, accept=False)
            self.assertEqual(d.command, None)
            self.assertEqual(r._positive_skew_since, 60.)
            self.assertEqual(r._positive_alignment_used_seconds, 0.)
        expired = fixture.step(r, 85., 20.1, 0., et=None, vector=(), sequences=zeros,
            charger_publication_pending=True, accept=False)
        self.assertEqual(expired.current_p1, 0)
        self.assertTrue(expired.should_call_service)

    def test_pending_recovery_is_gated_before_attempt_reservation(self):
        fixture = clocks.ClockProvenanceTests()
        r = fixture.regulator()
        state = r.export_overload_state()
        state["normal_control"].update(cap=7, previous_offer=16, stops=1,
            stop_recorded=True, restart=True, retry_wait_seconds=0.)
        self.assertTrue(r.import_overload_state(state))
        fixture.step(r, 0, 2., .02, status="awaiting_start", family="legacy", charger_report_provenance=None)
        pending = fixture.step(r, 60, 2., .02, status="awaiting_start", family="legacy",
            charger_report_provenance=None, charger_publication_pending=True, accept=False)
        self.assertEqual(pending.mode, "waiting_for_current_publication")
        self.assertFalse(pending.should_call_service)
        self.assertFalse(r._uncertainty_retry_submission_pending)
        self.assertIsNone(pending.recovery_submission_ts)
        self.assertEqual(r._uncertainty_stops, 1)
        clear = fixture.step(r, 60.02, 2., .02, status="awaiting_start", family="legacy",
            charger_report_provenance=None, charger_publication_pending=False, accept=False)
        self.assertTrue(clear.should_call_service)
        self.assertTrue(r._uncertainty_retry_submission_pending)
        self.assertEqual(r._uncertainty_stops, 1)

    def test_pending_cannot_extend_original_nominal_response_deadline(self):
        fixture = shedding.NormalSheddingTests()
        r = fixture.pending()
        deadline = r._normal_shed_deadline
        pending = fixture.sample(r, 49.5, 26.1, 16.2, et=-10,
            charger_publication_pending=True, accept=False)
        self.assertFalse(pending.should_call_service)
        self.assertEqual(r._normal_shed_deadline, deadline)
        expired = fixture.sample(r, 50, 26.1, 16.2, et=-10,
            charger_publication_pending=True, accept=False)
        self.assertEqual((expired.mode, expired.current_p1), ("normal_shedding_stop", 0))
        self.assertTrue(expired.should_call_service)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_pending_also_defers_owned_resume_without_erasing_pause(self):
        fixture = continuity.ContinuityTests()
        r = fixture.regulator()
        fixture.sample(r, 0, house=23, ev=6)
        paused = fixture.sample(r, 121, house=23, ev=6)
        self.assertEqual(paused.command, "pause")
        for now in (125, 185, 246):
            fixture.sample(r, now, house=5, ev=0, status="awaiting_start")
        pending = fixture.sample(r, 251, house=5, ev=0, status="awaiting_start",
            charger_publication_pending=True, accept=False)
        self.assertEqual(pending.command, "resume")
        self.assertFalse(pending.should_call_service)
        self.assertTrue(r.paused_by_us)


if __name__ == "__main__":
    unittest.main()
