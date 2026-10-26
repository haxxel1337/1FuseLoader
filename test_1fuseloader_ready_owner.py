"""Memory-only unplugged authority and late source/ACK edges, all synthetic."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_trial_epochs as fixtures

logic, EPOCH, SOURCE = fixtures.logic, fixtures.EPOCH, fixtures.SOURCE


class ReadyOwnerTests(unittest.TestCase):
    def regulator(self, **options):
        values = dict(active_control=True, dry_run=False, disconnected_readiness=True,
            enable_soft_guard=False, enable_pause_resume=False, measurement_settle_seconds=25)
        values.update(options)
        return logic.OneFuseRegulator(logic.RegulatorSettings(**values))

    def frame(self, now, grid=5., status="disconnected", **metadata):
        fields = dict(now_ts=now, wall_now_ts=EPOCH+now, status=status,
            grid_l1=grid, charger_current=16., grid_report_ts=EPOCH+now,
            charger_report_ts=EPOCH, control_source_id=SOURCE,
            charger_native_committed=True, charger_report_family="native_terminal_receipt",
            charger_report_provenance=("receiver", "max_terminals", 182, 183),
            charger_observation_report_times=((182, EPOCH), (183, EPOCH)),
            charger_observation_sequences=((182, 1), (183, 1), (184, 0), (185, 0)))
        fields.update(metadata)
        return logic.RegulatorInput(**fields)

    def ready(self, **options):
        r = self.regulator(**options)
        proposal = r.evaluate(self.frame(0.))
        self.assertEqual((proposal.mode, proposal.current_p1), ("disconnected_ready", 16))
        self.assertIsNone(r._ready_owner)
        r.acknowledge(proposal, 1.)
        self.assertIsNotNone(r._ready_owner)
        return r, proposal

    def test_cold_limit_uses_entire_raw_offer_and_requires_startup_minimum(self):
        r = self.regulator()
        d = r.evaluate(self.frame(0., grid="10"))
        self.assertEqual((d.current_p1, d.current_p2, d.current_p3), (13, 0, 0))
        self.assertEqual(d.house_load_l1, 10.)
        r = self.regulator()
        self.assertEqual(r.evaluate(self.frame(0., grid=17.)).current_p1, 0)

    def test_only_actual_ack_creates_owner_and_http_latency_cannot_extend_lease(self):
        r, _ = self.ready()
        self.assertEqual(r._ready_owner.accepted_at, 1.)
        self.assertEqual(r._ready_owner.lease_expires, 60.)
        self.assertEqual(r.last_set_limit_ts, 0.)
        self.assertNotIn("ready_owner", r.export_overload_state())

    def test_obsolete_reply_records_acceptance_without_new_ready_authority(self):
        r = self.regulator()
        d = r.evaluate(self.frame(0.))
        r.interrupt_positive_control("protective_zero")
        r.acknowledge(d, 1.)
        self.assertEqual(r.last_target[0], 16)
        self.assertIsNone(r._ready_owner)
        self.assertTrue(r._restart_required)
        r = self.regulator()
        d = r.evaluate(self.frame(0.))
        r.acknowledge(d, 26.)
        self.assertIsNone(r._ready_owner)
        self.assertTrue(r._restart_required)

    def test_disconnected_raw_change_cannot_reuse_old_house_or_ev_cache(self):
        r, d = self.ready()
        original = self.frame(0.)
        self.assertTrue(r.can_continue_readiness_dispatch(original, self.frame(2.), d))
        self.assertFalse(r.can_continue_readiness_dispatch(original, self.frame(2., grid=10.), d))
        self.assertFalse(r.can_dispatch_pending_readiness(self.frame(2., grid=10.), d))
        self.assertEqual(r.evaluate(self.frame(2., grid=21.)).current_p1, 0)

    def test_connected_wait_has_fixed_source_aged_clock_inherited_by_charging(self):
        r, _ = self.ready()
        waiting = r.evaluate(self.frame(10., grid=21., status="awaiting_start",
            grid_report_ts=EPOCH+9.))
        self.assertEqual((waiting.mode, waiting.command, waiting.current_p1), ("readiness_wait", None, 16))
        self.assertEqual(waiting.readiness_deadline_ts, 34.)
        charging = r.evaluate(self.frame(11., grid=21., status="charging"))
        self.assertEqual(charging.current_p1, 16)
        self.assertEqual(charging.positive_response_deadline_ts, 34.)
        self.assertIsNone(r._positive_response_ts)

    def test_wait_pending_proof_checks_latest_future_replay_provider_and_custom_guard(self):
        r, _ = self.ready(enable_soft_guard=True, soft_limit_amps=20., soft_for_seconds=2,
            soft_current_amps=6)
        waiting = r.evaluate(self.frame(10., grid=21., status="awaiting_start"))
        before = deepcopy(r.__dict__)
        self.assertTrue(r.can_retain_readiness_wait(self.frame(11., grid=21., status="awaiting_start"), waiting))
        variants = (
            dict(charger_report_ts=EPOCH+30., charger_observation_report_times=((182, EPOCH+30.), (183, EPOCH+30.))),
            dict(charger_report_ts=EPOCH-1., charger_observation_report_times=((182, EPOCH-1.), (183, EPOCH-1.))),
            dict(charger_event_source_known=True, charger_event_source_ts=EPOCH+30.),
            dict(data_valid=False), dict(control_source_id=("other",)),
        )
        for change in variants:
            self.assertFalse(r.can_retain_readiness_wait(self.frame(11., grid=21., status="awaiting_start", **change), waiting))
        self.assertFalse(r.can_retain_readiness_wait(self.frame(12.5, grid=21., status="awaiting_start"), waiting))
        self.assertEqual(r.__dict__, before)

    def test_renewal_never_claims_expired_owner_and_import_does_not_restore_it(self):
        r, d = self.ready()
        self.assertTrue(r.can_retain_readiness_offer(self.frame(2.), d))
        self.assertFalse(r.can_retain_readiness_offer(self.frame(60.), d))
        state = r.export_overload_state()
        self.assertTrue(r.import_overload_state(state))
        self.assertIsNone(r._ready_owner)

    def test_default_off_and_completed_or_pause_never_prepare(self):
        self.assertFalse(logic.RegulatorSettings().disconnected_readiness)
        for status in ("completed", "paused", "awaiting_scheduled_start", "awaiting_authorization"):
            r = self.regulator()
            d = r.evaluate(self.frame(0., status=status))
            self.assertNotEqual(d.mode, "disconnected_ready")
            self.assertIsNone(r._ready_owner)
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                self.regulator(disconnected_readiness=value)


if __name__ == "__main__":
    unittest.main()
