"""Synthetic changed-conductor cohorts while a real decrease is pending."""
import unittest

import test_1fuseloader_clock_provenance as clocks

logic = clocks.logic
EPOCH = clocks.EPOCH
SOURCE = clocks.SOURCE


class ReductionCohortTests(unittest.TestCase):
    def frame(self, r, now, grid, current, *, winners=(182,), reports=None, sequences=None,
              gt=None, ready=True, accept=True, known=True, source_stamp="selected", **metadata):
        reports, sequences = reports or {}, sequences or {}
        selected = min(reports[key] for key in winners) if all(key in reports for key in winners) else None
        source = selected if source_stamp == "selected" else source_stamp
        values = dict(now_ts=now, wall_now_ts=EPOCH+now, status="charging", grid_l1=grid,
            grid_report_ts=EPOCH+(now if gt is None else gt), charger_current=current,
            charger_report_ts=None if selected is None else EPOCH+selected,
            charger_current_trusted=True, control_source_id=SOURCE,
            charger_report_family="native_terminal_receipt",
            charger_report_provenance=("receiver", "max_terminals", *winners),
            charger_observation_report_times=tuple((key, EPOCH+stamp) for key, stamp in reports.items()),
            charger_observation_sequences=tuple((key, sequences.get(key, 0)) for key in (182, 183, 184, 185)),
            charger_event_source_known=known,
            charger_event_source_ts=None if source is None else EPOCH+source,
            charger_native_committed=True, charger_increase_ready=ready)
        values.update(metadata)
        d = r.evaluate(logic.RegulatorInput(**values))
        if accept and d.should_call_service:
            r.acknowledge(d, now)
        return d

    def reduced(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False,
            overload_enabled=True, nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            measurement_settle_seconds=25, enable_soft_guard=False, enable_pause_resume=False))
        self.assertTrue(r.import_overload_state(dict(version=1, used_seconds=3601.,
            exposure_active=False, transition_pending=False, minimum_held=False, failure_reason="response_timeout")))
        for now in (900., 960.):
            self.frame(r, now, 4., 0., winners=(182, 183, 184, 185),
                known=False, status="awaiting_start")
        self.assertEqual(r.last_target[0], 16)
        reports, sequences = {182: 1001., 183: 1001.2}, {182: 1, 183: 1}
        self.frame(r, 1002.5, 20.1, 16.141, reports=reports, sequences=sequences)
        self.assertIsNone(r._positive_cap)
        self.assertEqual(self.frame(r, 1029., 24., 16.141, reports=reports, sequences=sequences).current_p1, 15)
        self.frame(r, 1063., 23., 16.141, reports=reports, sequences=sequences)
        self.assertEqual(r.last_target[0], 15)
        self.assertEqual(r._awaiting_reduction_target, 15)
        return r

    def cohort(self, r, now=1067.44, *, accept=False, **metadata):
        return self.frame(r, now, 23., 16.139, winners=(183,),
            reports={182: now-.08, 183: 1001.2}, sequences={182: 2, 183: 1},
            gt=now-.4, ready=False, accept=accept, **metadata)

    def test_known_old_maximum_is_held_without_clock_or_authority_credit(self):
        r = self.reduced()
        before = r.export_overload_state()
        first = self.cohort(r)
        self.assertEqual((first.mode, first.current_p1, first.command), ("waiting_for_current_cohort", 15, None))
        self.assertAlmostEqual(first.source_processing_deadline_ts, 1068.44)
        self.assertAlmostEqual(first.charger_report_age_seconds, 1067.44-1001.2)
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual(r.export_overload_state(), before)
        reports, sequences = {182: 1067.36, 183: 1067.53}, {182: 2, 183: 2}
        fresh = self.frame(r, 1067.53, 23., 15.137, winners=(182, 183),
            reports=reports, sequences=sequences, gt=1067.04, accept=False)
        self.assertEqual(fresh.current_p1, 15)
        self.assertIsNone(fresh.source_processing_deadline_ts)
        self.assertAlmostEqual(r._partial_reduction_used_seconds, .09)
        later = self.frame(r, 1069.2, 21., 15.137, winners=(182, 183), reports=reports,
            sequences=sequences, accept=False)
        self.assertEqual(later.current_p1, 16)
        self.assertAlmostEqual(r._partial_reduction_used_seconds, .09)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_processing_storm_does_not_move_first_deadline(self):
        r = self.reduced()
        deadline = self.cohort(r).source_processing_deadline_ts
        for at, sequence in ((1067.7, 3), (1068.2, 4)):
            d = self.frame(r, at, 23., 16.139, winners=(183,), reports={182: at-.01, 183: 1001.2},
                sequences={182: sequence, 183: 1}, ready=False, accept=False)
            self.assertEqual(d.command, None)
            self.assertEqual(d.source_processing_deadline_ts, deadline)
        expired = self.frame(r, 1068.45, 23., 16.139, winners=(183,),
            reports={182: 1068.44, 183: 1001.2}, sequences={182: 5, 183: 1}, ready=False, accept=False)
        self.assertEqual(expired.current_p1, 0)
        self.assertTrue(expired.should_call_service)
        self.assertIsNone(expired.source_processing_deadline_ts)

    def test_opt_out_malformed_provider_and_true_replay_get_no_processing_hold(self):
        for metadata in (dict(charger_native_committed=False), dict(charger_native_committed="yes"),
                         dict(charger_current_trusted=False), dict(source_stamp=None),
                         dict(source_stamp=1100.), dict(data_valid=False, data_error="native_fault")):
            with self.subTest(metadata=metadata):
                r = self.reduced()
                self.assertIsNone(self.cohort(r, **metadata).source_processing_deadline_ts)
        r = self.reduced()
        r.settings.nominal_import_only_fallback = False
        self.assertIsNone(self.cohort(r).source_processing_deadline_ts)
        r = self.reduced()
        replay = self.frame(r, 1067.44, 23., 16.139, winners=(183,), reports={182: 1067.36, 183: 1000.},
            sequences={182: 2, 183: 1}, ready=False, accept=False)
        self.assertEqual(replay.current_p1, 0)
        self.assertIn("regressed", replay.reason)

    def test_earlier_reduction_deadline_and_lease_expiry_remain_terminal(self):
        r = self.reduced()
        r._reduction_guard_deadline = 1067.4
        self.assertIsNone(self.cohort(r).source_processing_deadline_ts)
        r = self.reduced()
        held = self.cohort(r, now=1122.8)
        self.assertEqual(held.source_processing_deadline_ts, 1123.)
        stopped = self.cohort(r, now=1123.01)
        self.assertEqual(stopped.current_p1, 0)
        self.assertIsNone(stopped.source_processing_deadline_ts)


if __name__ == "__main__":
    unittest.main()
