"""Only an already established normal cap survives an old MAX winner change.

Actual offline controller, native conductor vectors and HTTP200 fixture ACKs.
The winning native timestamp really stays old; no proof, established value or
response owner is seeded. This is a synthetic contract test, not home history.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_runtime as fixtures
from test_1fuseloader_ha_current_representation import float32


class CachedNativeNormalControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = ready.DisconnectedReadinessRuntimeTests.update_entry
    wall = ready.DisconnectedReadinessRuntimeTests.wall
    schedule = ready.DisconnectedReadinessRuntimeTests.schedule
    active_timers = ready.DisconnectedReadinessRuntimeTests.active_timers
    set_native = ready.DisconnectedReadinessRuntimeTests.set_native
    snapshot = ready.DisconnectedReadinessRuntimeTests.snapshot
    install = ready.DisconnectedReadinessRuntimeTests.install
    native_vector = ready.DisconnectedReadinessRuntimeTests.native_vector
    grid = ready.DisconnectedReadinessRuntimeTests.grid
    configure = ready.DisconnectedReadinessRuntimeTests.configure
    publish = ready.DisconnectedReadinessRuntimeTests.publish
    tick = ready.DisconnectedReadinessRuntimeTests.tick
    slow_first_checkpoint = ready.DisconnectedReadinessRuntimeTests.slow_first_checkpoint

    async def asyncSetUp(self):
        await ready.DisconnectedReadinessRuntimeTests.asyncSetUp(self)
        self.configure(enabled=False)
        self.circuit = SimpleNamespace(state="0", last_reported=self.wall(), attributes={
            "unit_of_measurement": "A", "state_dynamicCircuitCurrentP1": 0,
            "state_dynamicCircuitCurrentP2": 0, "state_dynamicCircuitCurrentP3": 0,
            "state_latestPulse": self.wall().isoformat()})
        self.states["sensor.circuit"] = self.circuit
        registry = SimpleNamespace(async_get=lambda _: None)
        self.registry_patch = patch.object(fixtures.runtime, "er", SimpleNamespace(
            async_get=lambda _: registry,
            async_entries_for_device=lambda *_: [SimpleNamespace(platform="easee",
                disabled_by=None, entity_id="sensor.circuit")]))
        self.registry_patch.start()
        execute = self.adapter.async_execute

        async def accepted(decision):
            await execute(decision)
            self.circuit.attributes.update(state_dynamicCircuitCurrentP1=decision.current_p1,
                state_dynamicCircuitCurrentP2=decision.current_p2,
                state_dynamicCircuitCurrentP3=decision.current_p3)
            self.circuit.state = str(decision.current_p1)
            self.circuit.last_reported = self.wall()
            self.adapter.reported_limit_receipts = (self.now, self.now, self.now)
            return SimpleNamespace(http_status=200, correlation_ids=())

        self.adapter.async_execute = accepted

    async def asyncTearDown(self):
        await ready.DisconnectedReadinessRuntimeTests.asyncTearDown(self)
        self.registry_patch.stop()

    def pulse(self):
        self.circuit.attributes["state_latestPulse"] = self.wall().isoformat()
        self.circuit.last_reported = self.wall()

    async def established(self, historical=False):
        if historical:
            self.assertTrue(self.controller.regulator.import_overload_state(dict(
                version=1, used_seconds=4000., exposure_active=False,
                transition_pending=False, minimum_held=False, failure_reason="response_timeout")))
            await self.controller._budget_store.async_save(
                self.controller.regulator.export_overload_state())
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 8.4, .012, receipt=None, changed=90.)
            self.pulse()
            await self.tick()
        r = self.controller.regulator
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0, 14])
        self.publish(163., "charging", 22.6, 14.2, receipt=161.7, changed=161.5)
        self.native_vector(float32(14.2), (182,), {182:161.7, 183:161.8},
            {182:1, 183:1}, 161.8)
        self.pulse()
        await self.tick()
        self.assertIsNone(r._positive_cap)
        self.assertEqual(r._ev_established_value, float32(14.2))
        self.assertEqual(r.last_target[0], 14)
        original = deepcopy(vars(self.native))
        for at in range(190, 1061, 30):
            self.now = float(at)
            self.grid(18.5)
            self.pulse()
            await self.tick()
            self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
            self.assertEqual(vars(self.native), original)
            self.assertEqual(self.controller._delivery.latest.http_status, 200)
        self.assertEqual(r.last_set_limit_ts, 1060.)
        self.adapter.calls.clear()
        return r

    def switch_to_old_winner(self, *, raw=18.5):
        self.now = 1068.
        self.grid(raw)
        self.native_vector(float32(14.1), (183,), {182:1067.7, 183:161.8},
            {182:2, 183:1}, 1067.7)
        self.pulse()

    def assert_no_new_ev_proof(self, r):
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_report_ts, self.wall(161.8).timestamp())
        self.assertTrue(inp.charger_event_source_known)
        self.assertEqual(r._ev_established_value, float32(14.2))
        self.assertFalse(r._ev_clock_credit)
        self.assertIsNone(r._positive_cap)
        self.assertIsNone(r._ordinary_lower)
        self.assertIsNone(r._minimum_confirmation)
        self.assertIsNone(r._normal_shed_deadline)
        self.assertIsNone(r._low_grid_deadline)
        self.assertEqual(r.export_overload_state()["used_seconds"], 0)
        self.assertEqual(r.automatic_cycle_diagnostics()["automatic_cycle_state"], "ready")

    async def test_old_committed_max_winner_keeps_and_renews_only_established_fourteen(self):
        r = await self.established()
        self.switch_to_old_winner()
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "normal", self.controller.last_decision)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.house_load_basis,
            "native_cached_winner_normal_continuity")
        self.assert_no_new_ev_proof(r)
        self.now = 1090.
        self.grid(18.5)
        self.pulse()
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [14])
        self.assertEqual(self.controller._delivery.latest.http_status, 200)
        self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])
        self.assertEqual(r.last_set_limit_ts, 1090.)
        self.assert_no_new_ev_proof(r)

    async def test_pending_checkpoint_retains_same_cap_but_never_renews_it(self):
        r = await self.established()
        self.switch_to_old_winner()
        await self.tick()
        self.assertEqual(r.last_target[0], 14)
        previous = self.controller._budget_store
        self.controller._budget_store = type(previous)(previous.store,
            clock=previous.clock, read_back=previous.store.async_load)
        self.slow_first_checkpoint()
        self.now = 1090.
        self.grid(18.5)
        self.pulse()
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(r.last_set_limit_ts, 1060.)
        self.assert_no_new_ev_proof(r)
        await self.controller._budget_store.async_wait_pending()

    async def test_same_normal_offer_does_not_refund_history_or_create_overload_permission(self):
        r = await self.established(historical=True)
        self.switch_to_old_winner()
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        state = r.export_overload_state()
        self.assertEqual(state["used_seconds"], 4000.)
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(state["automatic_cycle"]["state"], "ready")
        self.assertEqual(state["automatic_cycle"]["used_seconds"], 0)
        self.assertNotIn("trial_epoch", state)
        self.assertEqual(r.effective_overload_failure_reason, "automatic_cycle_not_admitted")

    async def test_inactive_real_reduction_envelope_keeps_only_same_cap_and_old_response_floors(self):
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 4., .012, receipt=None, changed=90.)
            self.pulse()
            await self.tick()
        self.assertEqual(self.controller.regulator.last_target[0], 16)
        self.publish(163., "charging", 20.2, 16.2, receipt=161.7, changed=161.5)
        self.pulse()
        await self.tick()
        self.publish(200., "charging", 25., 16.2, receipt=199.7, grid_at=200.)
        self.pulse()
        await self.tick()
        r = self.controller.regulator
        self.assertEqual(r.last_target[0], 14)
        # This trusted numeric value has a source bound BEFORE the14 A ACK.
        # It can establish the cached measurement, never response to that ACK.
        self.publish(201., "charging", 22.6, 14.2, receipt=200.7, grid_at=201.)
        self.native_vector(float32(14.2), (182,), {182:200.7, 183:200.8},
            {182:3, 183:3}, 200.8)
        self.native.source_timestamps = tuple((key,self.wall(199.9)) for key in (182,183,184,185))
        self.pulse()
        await self.tick()
        self.assertEqual(r._ev_established_value, float32(14.2))
        self.assertEqual((r._awaiting_reduction_target, r._reduction_previous_cap), (14,16))
        self.assertIsNone(r._normal_shed_deadline)
        self.assertIsNone(r._settle_started)
        for at in range(230, 1101, 30):
            self.now = float(at)
            self.grid(18.5)
            self.pulse()
            await self.tick()
            self.assertEqual(r.last_target[0], 14)
        prior = (r._awaiting_reduction_target, r._reduction_previous_cap,
            r._house_before_reduction, r._reduction_charger_ts)
        self.adapter.calls.clear()
        self.now = 1108.
        self.grid(18.5)
        self.native_vector(float32(14.1), (183,), {182:1107.7, 183:200.8},
            {182:4, 183:3}, 1107.7)
        self.native.source_timestamps = ((182,self.wall(1107.7)), (183,self.wall(199.9)),
            (184,None), (185,None))
        self.pulse()
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "normal", self.controller.last_decision)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual((r._awaiting_reduction_target, r._reduction_previous_cap,
            r._house_before_reduction, r._reduction_charger_ts), prior)
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual(r._ev_established_value, float32(14.2))
        self.assertEqual(self.controller._read_input().charger_report_ts, self.wall(200.8).timestamp())
        self.now = 1109.
        self.grid(11.)
        self.pulse()
        await self.tick()
        self.assertEqual(self.controller.last_decision.mode, "normal", self.controller.last_decision)
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual((r._awaiting_reduction_target, r._reduction_previous_cap,
            r._house_before_reduction, r._reduction_charger_ts), prior)
        self.assertFalse(r._ev_clock_credit)

    async def test_unsafe_whole_offer_cannot_be_retained_from_old_winner(self):
        await self.established()
        self.switch_to_old_winner(raw=24.5)
        await self.tick()
        self.assertLess(self.controller.last_decision.current_p1, 14,
            self.controller.last_decision)
        self.assertNotEqual(self.controller.last_decision.house_load_basis,
            "native_cached_winner_normal_continuity")
        self.assertFalse(any(d.current_p1 >= 14 for d in self.adapter.calls))

    async def test_get_bootstrap_cannot_replace_native_receiver_proof_for_continuity(self):
        r = await self.established()
        self.now = 1068.
        self.grid(18.5)
        self.native_vector(float32(14.1), (183,), {}, {}, 1067.7)
        self.native.committed = False
        self.now = 1068.1
        self.install(self.snapshot(current=float32(14.1), observed=1067.8))
        self.pulse()
        self.assertTrue(self.controller._read_input().charger_snapshot_verified)
        await self.tick()
        self.assertNotEqual(self.controller.last_decision.house_load_basis,
            "native_cached_winner_normal_continuity")
        self.assertFalse(any(d.current_p1 >= 14 for d in self.adapter.calls))
        self.assertEqual(r._ev_established_value, float32(14.2))

    async def test_native_source_vector_binding_lease_and_peak_guards_still_apply(self):
        for problem in ("uncommitted", "fault", "replay", "future", "provider_future",
                        "provider_missing", "winner_missing", "binding", "receiver", "family",
                        "lease", "grid", "peak"):
            with self.subTest(problem=problem):
                if problem != "uncommitted":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.established()
                self.switch_to_old_winner()
                if problem == "uncommitted":
                    self.native.committed = False
                elif problem == "fault":
                    self.native.error = "native_sdk_current_invalid"
                elif problem == "replay":
                    self.native.observation_sequences = ((182,2), (183,0), (184,0), (185,0))
                elif problem == "future":
                    self.native.received_at = self.wall(1075.)
                    self.native.observation_report_times = ((182,self.wall(1067.7)), (183,self.wall(1075.)))
                elif problem == "provider_future":
                    self.native.source_timestamps = ((182,self.wall(1067.7)),
                        (183,self.wall(1075.)), (184,None), (185,None))
                elif problem == "provider_missing":
                    self.native.source_timestamps = ()
                elif problem == "winner_missing":
                    self.native.received_at = None
                    self.native.observation_report_times = ((182,self.wall(1067.7)),)
                elif problem == "binding":
                    self.controller._configured_control_identity.return_value = "other-source"
                elif problem == "receiver":
                    self.native.source_identity = (99,12,13,14)
                elif problem == "family":
                    del self.adapter.native_current_observation
                elif problem == "lease":
                    self.now = 1120.
                    self.grid(18.5)
                    self.pulse()
                elif problem == "grid":
                    self.grid(18.5, 900.)
                else:
                    self.grid(38.01)
                await self.tick()
                if problem == "provider_missing":
                    # Existing provider-unknown handling is independent of the
                    # new cached-winner exception. It may keep the old cap;
                    # losing metadata must not retime the winning event or
                    # turn this into the new continuity permission.
                    self.assertEqual(r.last_target[0], 14)
                    self.assertEqual(self.adapter.calls, [])
                    self.assertEqual(self.controller._read_input().charger_report_ts,
                        self.wall(161.8).timestamp())
                    self.assertIsNone(r._positive_cap)
                    self.assertIsNone(r._minimum_confirmation)
                    self.assertNotEqual(self.controller.last_decision.house_load_basis,
                        "native_cached_winner_normal_continuity")
                    continue
                self.assertEqual(self.adapter.calls[-1].current_p1, 0,
                    self.controller.last_decision)
                self.assertNotEqual(self.controller.last_decision.house_load_basis,
                    "native_cached_winner_normal_continuity")

    async def test_stop_journal_has_bounded_private_decision_snapshot_without_public_attribute(self):
        for at in (100., 160.):
            self.publish(at, "awaiting_start", 8.4, .012, receipt=None, changed=90.)
            self.pulse()
            await self.tick()
        self.publish(165., "charging", 22.6, .012, receipt=None, grid_at=165.)
        self.pulse()
        await self.tick()
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, 190.)
        self.now = 170.
        self.grid(38.01)
        self.pulse()
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        if self.controller._incident_journal_task is not None:
            await self.controller._incident_journal_task
        private = self.controller._recent_stop_contexts[-1]["decision_diagnostics"]
        self.assertEqual(private["house_load_basis"], self.controller.last_decision.house_load_basis)
        self.assertEqual(private["house_load_l1"], self.controller.last_decision.house_load_l1)
        self.assertTrue(private["native_committed"])
        self.assertFalse(private["snapshot_used"])
        self.assertEqual(private["active_owner_caps_before"]["positive"], 14)
        self.assertEqual(private["active_owner_deadlines_before"]["positive"], 190.)
        self.assertFalse(private["established_value_match_before"])
        self.assertFalse(private["established_binding_match_before"])
        stored = self.hass._test_store_disk[
            "1fuseloader."+self.entry.entry_id+".incident_journal"]["stops"][-1]
        self.assertEqual(stored["decision_diagnostics"], private)
        self.assertNotIn("decision_diagnostics", self.controller.last_stop_context)
        public = self.controller.diagnostic_attributes()
        self.assertNotIn("decision_diagnostics", public["last_stop_context"])
        self.assertTrue(all("decision_diagnostics" not in item
            for item in public["recent_stop_contexts"]))


if __name__ == "__main__":
    unittest.main()
