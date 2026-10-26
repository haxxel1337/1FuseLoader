"""An accepted normal startup owns its mixed EV-first publications, offline."""
import unittest

import test_1fuseloader_positive_ramp_owner as ramp_fixtures

logic = ramp_fixtures.logic


class PositiveOwnerPriorityTests(unittest.TestCase):
    frame = ramp_fixtures.PositiveRampOwnerTests.frame

    def bootstrap(self, **options):
        settings = dict(active_control=True, dry_run=False, overload_enabled=True,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=True,
            enable_soft_guard=False, enable_pause_resume=False,
            measurement_settle_seconds=25, grid_confirmation_seconds=30)
        settings.update(options)
        r = logic.OneFuseRegulator(logic.RegulatorSettings(**settings))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False,
            failure_reason="response_timeout")))
        for now in (0., 60.):
            d = r.evaluate(self.frame(now, grid=5.5, current=.012, receipt=None,
                sequence=0, status="awaiting_start"))
            if d.should_call_service:
                r.acknowledge(d, now)
        self.assertEqual(r.last_target[0], 16)
        self.assertEqual((r._positive_cap, r._positive_house), (16, 5.5))
        return r

    def test_partial_during_same_cap_renewal_stays_with_original_startup_owner(self):
        r = self.bootstrap()
        renewal = r.evaluate(self.frame(90., grid=2.5, current=.16,
            receipt=85., grid_at=85., sequence=1))
        self.assertEqual((renewal.mode, renewal.current_p1), ("waiting_for_vehicle", 16))
        self.assertTrue(renewal.should_call_service)
        # A numeric event occurs during the identical-cap API call. It is
        # after the real changed-cap ACK, not after this lease renewal ACK.
        r.acknowledge(renewal, 90.2)
        d = r.evaluate(self.frame(90.3, grid=2.5, current=5.5,
            receipt=90.1, grid_at=85., sequence=2))
        self.assertEqual((d.mode, d.current_p1, d.command), ("positive_ramp_hold", 16, None))
        self.assertFalse(d.should_call_service)
        self.assertAlmostEqual(d.positive_response_deadline_ts, 115.1)
        self.assertIsNone(r._ordinary_lower)
        for now, grid, current, receipt, gt, seq in (
                (91., 8.8, 9.2, 90.7, 91., 3),
                (95., 18.6, 16.1, 94.6, 94., 4)):
            d = r.evaluate(self.frame(now, grid=grid, current=current,
                receipt=receipt, grid_at=gt, sequence=seq))
            # A partial draw plus a genuinely later grid may already prove
            # the ENTIRE offer fits. Full-cap demand is not required.
            self.assertEqual(d.current_p1, 16)
            self.assertFalse(d.should_call_service)
            if d.positive_response_deadline_ts is not None:
                self.assertAlmostEqual(d.positive_response_deadline_ts, 115.1)
            self.assertEqual(r.last_set_limit_ts, 90.2)
        d = r.evaluate(self.frame(100., grid=18.6, current=16.1,
            receipt=94.6, grid_at=99., sequence=4))
        self.assertEqual(d.current_p1, 16)
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(r._ordinary_lower)
        self.assertEqual(r._uncertainty_stops, 0)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_existing_owner_first_partial_cannot_become_an_ordinary_lower_attempt(self):
        r = self.bootstrap()
        d = r.evaluate(self.frame(65.3, grid=2.5, current=5.5,
            receipt=65.1, grid_at=64., sequence=1))
        self.assertEqual((d.mode, d.current_p1, d.command), ("positive_ramp_hold", 16, None))
        self.assertFalse(d.should_call_service)
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._ordinary_lower)

    def test_repeated_partial_without_coherent_budget_keeps_first_clock_and_expires(self):
        r = self.bootstrap()
        first = r.evaluate(self.frame(65.3, grid=2.5, current=5.5,
            receipt=65.1, grid_at=64., sequence=1))
        self.assertAlmostEqual(first.positive_response_deadline_ts, 90.1)
        # A later partial report has a later grid, but its implied house plus
        # the complete16 offer does not fit; no new owner may reset the clock.
        for now, receipt, seq in ((70., 69., 2), (85., 84., 3)):
            d = r.evaluate(self.frame(now, grid=22.5, current=6., receipt=receipt,
                grid_at=now, sequence=seq))
            self.assertEqual((d.mode, d.current_p1, d.command), ("positive_ramp_hold", 16, None))
            self.assertAlmostEqual(d.positive_response_deadline_ts, 90.1)
            self.assertEqual(r.last_set_limit_ts, 60.)
        d = r.evaluate(self.frame(90.1, grid=22.5, current=6., receipt=89.,
            grid_at=90.1, sequence=4))
        self.assertEqual(d.current_p1, 0)
        self.assertIn("original measurement window", d.reason)
        self.assertIsNone(r._ordinary_lower)

    def test_actual_peak_and_source_binding_faults_preempt_priority(self):
        for grid, fields in ((29., {}), (39., {}),
                (2.5, {"data_valid": False}),
                (2.5, {"control_source_id": ("different-source",)}),
                (2.5, {"charger_current_trusted": False}),
                (2.5, {"status": "paused"})):
            with self.subTest(grid=grid, fields=fields):
                r = self.bootstrap()
                d = r.evaluate(self.frame(65.3, grid=grid, current=5.5,
                    receipt=65.1, grid_at=65., sequence=1, **fields))
                self.assertNotEqual(d.mode, "positive_ramp_hold")
                self.assertLess(d.current_p1, 16)
        r = self.bootstrap()
        d = r.evaluate(self.frame(120., grid=2.5, current=5.5,
            receipt=119., grid_at=120., sequence=1))
        self.assertNotEqual(d.mode, "positive_ramp_hold")

    def test_ordinary_fallback_remains_available_without_positive_owner(self):
        r = self.bootstrap()
        # A genuine partial draw + later grid first retires the startup owner
        # by proving that the whole16 offer fits; it is not a fabricated ACK.
        d = r.evaluate(self.frame(65., grid=13., current=8.,
            receipt=64., grid_at=65., sequence=1))
        self.assertEqual(d.current_p1, 16)
        self.assertIsNone(r._positive_cap)
        d = r.evaluate(self.frame(66., grid=2.5, current=8.,
            receipt=64., grid_at=66., sequence=1))
        self.assertEqual((d.mode, d.current_p1, d.command), ("ordinary_lower_offer", 7, "set_limit"))
        self.assertIsNotNone(r._ordinary_lower)

    def test_expired_rise_may_handoff_to_independently_raw_bounded_lower_offer(self):
        r = self.bootstrap()
        first = r.evaluate(self.frame(65.3, grid=2.5, current=5.5,
            receipt=65.1, grid_at=64., sequence=1))
        self.assertEqual(first.mode, "positive_ramp_hold")
        d = r.evaluate(self.frame(90.1, grid=13., current=14.5,
            receipt=89., grid_at=90.1, sequence=2))
        # The expired16 owner earns no further hold. A new lower7 request is
        # independently safe without EV subtraction; prior16 stays unresolved.
        self.assertEqual((d.mode, d.current_p1, d.command), ("ordinary_lower_offer", 7, "set_limit"))
        self.assertIsNone(r._positive_cap)
        self.assertEqual(r._ordinary_lower.previous, 16)


if __name__ == "__main__":
    unittest.main()
