"""Source-bound cached MAX changes can only continue the accepted normal cap."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_positive_owner_priority as fixtures
from test_1fuseloader_trial_epochs import EPOCH

logic = fixtures.logic


class CachedNativeNormalTests(unittest.TestCase):
    frame = fixtures.PositiveOwnerPriorityTests.frame

    def native(self, now, current=14.2, *, winner=182, grid=18.5,
               prefix="receiver", known=True, source=None, **extra):
        cached = winner == 183
        receipt = 65.8 if cached else 65.7
        fields = dict(charger_report_provenance=(prefix, "max_terminals", winner),
            charger_observation_report_times=((182, EPOCH+(972.7 if cached else 65.7)), (183, EPOCH+65.8)),
            charger_observation_sequences=((182, 2 if cached else 1), (183, 1), (184, 0), (185, 0)),
            charger_event_source_known=known,
            charger_event_source_ts=EPOCH+(receipt if source is None else source))
        fields.update(extra)
        return self.frame(now, grid=grid, current=current, receipt=receipt,
            grid_at=now, sequence=1, **fields)

    def prepared(self):
        r = logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False,
            overload_enabled=True, nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            peak_only_response=True, automatic_overload_cycles=True,
            enable_soft_guard=False, enable_pause_resume=False, max_charge_amps=16))
        r.import_overload_state(dict(version=1, used_seconds=3601., exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))
        for now in (0., 60.):
            d = r.evaluate(self.frame(now, grid=8.8, current=.012, receipt=None,
                sequence=0, status="awaiting_start"))
            if d.should_call_service:
                r.acknowledge(d, now)
        self.assertEqual(r.last_target[0], 14)
        r.evaluate(self.native(66., grid=22.5))
        self.assertIsNone(r._positive_cap)
        self.assertEqual(r._ev_established_value, 14.2)
        for now in range(95, 966, 30):
            d = r.evaluate(self.native(float(now)))
            self.assertEqual(d.current_p1, 14)
            if d.should_call_service:
                r.acknowledge(d, float(now))
        self.assertEqual(r.last_set_limit_ts, 965.)
        return r

    def test_cached_winner_change_and_real_renewal_keep_cap_without_reestablishing_value(self):
        r = self.prepared()
        established = (r._ev_established_value, r._ev_established_binding,
            getattr(r, "_ev_established_receiver_scope", None),
            getattr(r, "_ev_established_observation_keys", None))
        for now in (973., 995., 1025.):
            d = r.evaluate(self.native(now, 14.1, winner=183))
            self.assertEqual((d.mode, d.current_p1), ("normal", 14))
            self.assertEqual(d.house_load_basis, "native_cached_winner_normal_continuity")
            self.assertAlmostEqual(d.house_load_l1, 4.4)
            self.assertEqual(d.available_l1, 14)
            if now >= 995.:
                self.assertTrue(d.should_call_service)
                r.acknowledge(d, now)
            self.assertEqual((r._ev_established_value, r._ev_established_binding,
                getattr(r, "_ev_established_receiver_scope", None),
                getattr(r, "_ev_established_observation_keys", None)), established)
            self.assertEqual(r._uncertainty_stops, 0)
        self.assertEqual(r.export_overload_state()["used_seconds"], 3601.)
        self.assertEqual(r.export_overload_state()["failure_reason"], "response_timeout")

    def test_receiver_source_vector_and_budget_rejections_do_not_get_this_continuity(self):
        for change in ({"prefix": "new-receiver"}, {"charger_snapshot_verified": True},
                {"charger_native_committed": False}, {"current": .1}, {"grid": 24.},
                {"control_source_id": ("other-source",)}, {"data_valid": False},
                {"charger_observation_report_times": ()}, {"charger_observation_sequences": ()},
                {"source": 980.}, {"known": False}, {"charger_current_trusted": False}):
            with self.subTest(change=change):
                r = self.prepared()
                fields = dict(current=14.1, winner=183)
                fields.update(change)
                d = r.evaluate(self.native(973., **fields))
                self.assertNotEqual(d.house_load_basis, "native_cached_winner_normal_continuity")

    def test_source_replay_and_expired_lease_cannot_renew_from_cached_winner(self):
        r = self.prepared()
        d = r.evaluate(self.native(973., 14.1, winner=183,
            charger_observation_report_times=((182, EPOCH+972.7), (183, EPOCH+65.6))))
        self.assertNotEqual(d.house_load_basis, "native_cached_winner_normal_continuity")
        r = self.prepared()
        d = r.evaluate(self.native(1025., 14.1, winner=183))
        self.assertNotEqual(d.house_load_basis, "native_cached_winner_normal_continuity")

    def test_pending_checkpoint_proof_is_detached_and_exact_same_cap_only(self):
        r = self.prepared()
        inp = self.native(973., 14.1, winner=183)
        d = r.evaluate(inp)
        before = deepcopy(r.__dict__)
        self.assertTrue(r.can_retain_normal_offer(inp, d))
        self.assertEqual(r.__dict__, before)
        self.assertFalse(r.can_retain_normal_offer(inp, replace(d, current_p1=16)))
        self.assertFalse(r.can_retain_normal_offer(self.native(974., 14.1, winner=183, source=981.), d))
        self.assertEqual(r.__dict__, before)

    def test_stronger_custom_guard_wins_but_nonbinding_soft_cap_does_not_stop(self):
        for limit, expected in ((14, True), (6, False)):
            r = self.prepared()
            r.update_settings(replace(r.settings, enable_soft_guard=True,
                soft_limit_amps=10, soft_for_seconds=1, soft_current_amps=limit))
            r.evaluate(self.native(973., 14.1, winner=183))
            d = r.evaluate(self.native(975., 14.1, winner=183))
            self.assertEqual(d.house_load_basis == "native_cached_winner_normal_continuity", expected)


if __name__ == "__main__":
    unittest.main()
