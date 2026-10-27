"""Independent synthetic controller traces for observation/normal separation.

The actual controller, durable Store gate, dispatch callback, HTTP acceptance
and native conductor vectors run offline. No private response owner is seeded.
An unchanged native event never acquires a new receipt from meter/status churn.
"""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_1fuseloader_connected_full_readiness_controller as connected
import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_normal_observation_proof as public_proof
from test_1fuseloader_ha_current_representation import float32, ha_state


class NormalProofControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = connected.ConnectedFullReadinessControllerTests.update_entry
    wall = connected.ConnectedFullReadinessControllerTests.wall
    schedule = connected.ConnectedFullReadinessControllerTests.schedule
    active_timers = connected.ConnectedFullReadinessControllerTests.active_timers
    set_native = connected.ConnectedFullReadinessControllerTests.set_native
    snapshot = connected.ConnectedFullReadinessControllerTests.snapshot
    install = connected.ConnectedFullReadinessControllerTests.install
    native_vector = connected.ConnectedFullReadinessControllerTests.native_vector
    grid = connected.ConnectedFullReadinessControllerTests.grid
    configure = connected.ConnectedFullReadinessControllerTests.configure
    publish = connected.ConnectedFullReadinessControllerTests.publish
    tick = connected.ConnectedFullReadinessControllerTests.tick
    asyncSetUp = connected.ConnectedFullReadinessControllerTests.asyncSetUp
    asyncTearDown = connected.ConnectedFullReadinessControllerTests.asyncTearDown
    execute = connected.ConnectedFullReadinessControllerTests.execute
    report = connected.ConnectedFullReadinessControllerTests.report
    start = connected.ConnectedFullReadinessControllerTests.start
    maximum_after_recovery = connected.ConnectedFullReadinessControllerTests.maximum_after_recovery
    assert_history = connected.ConnectedFullReadinessControllerTests.assert_history
    slow_first_checkpoint = ready.DisconnectedReadinessRuntimeTests.slow_first_checkpoint

    def pulse(self):
        self.circuit.attributes["state_latestPulse"] = self.wall().isoformat()

    async def sample(self, at, raw):
        self.now = at
        self.grid(raw)
        self.pulse()
        await self.tick()
        return self.controller.last_decision

    async def stable_seven_to_nine(self):
        r = await self.start()
        base = self.recovered_at
        for offset in range(5, 41, 5):
            self.report(base+offset, "charging", 13.1 if offset <= 35 else 13.2,
                7.009, base+5)
            await self.tick()
        self.assertEqual(r.last_target[0], 9, self.controller.last_decision)
        self.assertEqual(r._ev_established_value, float32(7.009))
        self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])
        self.adapter.calls.clear()
        return r, base

    async def normal_sixteen_to_fourteen(self):
        r = await self.maximum_after_recovery(interrupted=False)
        self.report(163., "charging", 20.7, 16.086, 162.)
        self.native_vector(float32(16.086), (182,), {182:162., 183:162.2},
            {182:2, 183:2}, 162.2)
        await self.tick()
        for at, raw in ((175.,20.7), (190.,20.7), (220.,20.7),
                        (245.,21.9), (246.,24.4), (270.,21.3)):
            await self.sample(at, raw)
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        self.assertEqual(r._ev_established_value, float32(16.086))
        self.adapter.calls.clear()
        return r

    def old_max(self, at=273., raw=21.3):
        self.now = at
        self.grid(raw)
        self.native_vector(float32(16.078), (183,), {182:at-.3, 183:162.2},
            {182:3, 183:2}, at-.3)
        self.pulse()

    def assert_no_zero(self):
        self.assertTrue(all(d.command == "set_limit" and d.current_p1 > 0
            for d in self.adapter.calls),
            [(d.current_p1, d.mode, d.reason) for d in self.adapter.calls])

    def publication(self, at=273., started=272.9):
        self.now = at
        self.grid(21.3)
        self.pulse()
        self.native.sdk_current = float32(16.078)
        self.native.native_publication_pending = True
        self.native.divergence_started_monotonic = started
        self.native.publication_generation = 1
        self.states["sensor.car"].state = ha_state(16.078)

    async def test_whole_raw_safe_nine_survives_original_observation_expiry(self):
        r, base = await self.stable_seven_to_nine()
        native = deepcopy(vars(self.native))
        original_receipt = self.controller._read_input().charger_report_ts
        for offset in range(45, 121, 5):
            await self.sample(base+offset, 13.2)
            self.assertEqual(r.last_target[0], 9, self.controller.last_decision)
            self.assertEqual(vars(self.native), native)
            self.assertEqual(self.controller._read_input().charger_report_ts, original_receipt)
            self.assert_no_zero()
        self.assert_history()

    async def test_delayed_readback_does_not_overwrite_independently_normal_nine(self):
        r = await self.start()
        execute = self.execute
        delayed = []

        async def accept_without_immediate_readback(decision, on_dispatch):
            before = deepcopy(vars(self.circuit))
            receipts = self.adapter.reported_limit_receipts
            result = await execute(decision, on_dispatch)
            if decision.current_p1 == 9:
                delayed.append(self.now)
                self.circuit.__dict__.clear()
                self.circuit.__dict__.update(before)
                self.adapter.reported_limit_receipts = receipts
            return result

        self.adapter.async_execute_with_dispatch = accept_without_immediate_readback
        base = self.recovered_at
        for offset in range(5, 66, 5):
            self.report(base+offset, "charging", 13.1 if offset < 40 else 13.2,
                7.009, base+5)
            await self.tick()
        self.assertTrue(delayed)
        self.assertEqual(r.last_target[0], 9, self.controller.last_decision)
        self.circuit.attributes["state_dynamicCircuitCurrentP1"] = 9
        self.circuit.state = "9"
        self.circuit.last_reported = self.wall()
        self.adapter.reported_limit_receipts = (self.now,) * 3
        await self.sample(base+70, 13.2)
        self.assertEqual(r.last_target[0], 9)
        self.assertTrue(all(d.current_p1 != 0 for d in self.adapter.calls))
        self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])

    async def test_stable_cache_preserves_same_offer_after_raw_no_longer_fits_entire_offer(self):
        r, base = await self.stable_seven_to_nine()
        receipt = self.controller._read_input().charger_report_ts
        for offset in range(45, 126, 5):
            await self.sample(base+offset, 13.2 if offset < 120 else 16.5)
            self.assertEqual(r.last_target[0], 9, self.controller.last_decision)
            self.assert_no_zero()
            self.assertEqual(self.controller._read_input().charger_report_ts, receipt)
        self.assertEqual(r._ev_established_value, float32(7.009))
        self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])
        self.assert_history()

    async def test_cached_joint_upper_survives_old_max_winner_without_false_response(self):
        r = await self.normal_sixteen_to_fourteen()
        accepted, receipt = r.last_set_limit_ts, self.controller._read_input().charger_report_ts
        self.old_max()
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.assertEqual(self.controller._read_input().charger_report_ts,
            self.wall(162.2).timestamp())
        self.assertLessEqual(receipt, self.controller._read_input().charger_report_ts)
        self.assertEqual(r._ev_established_value, float32(16.086))
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        self.assertLessEqual(r._committed_normal.house+16, 23)

    async def test_joint_cache_renews_for_three_ttls_without_resetting_native_clock(self):
        r = await self.normal_sixteen_to_fourteen()
        self.old_max()
        await self.tick()
        original = deepcopy(vars(self.native))
        for at in range(276, 487, 10):
            await self.sample(float(at), 21.3)
            self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
            self.assertEqual(vars(self.native), original)
            self.assert_no_zero()
            self.assertEqual(r._committed_normal.unresolved_cap, 16)
            deadlines = [getattr(self.controller.last_decision, name) for name in
                ("ordinary_response_deadline_ts", "positive_response_deadline_ts",
                 "nominal_response_deadline_ts")]
            self.assertTrue(all(value is None or value > self.now for value in deadlines), deadlines)
        renewed = [row for row in self.dispatches if row["at"] >= 246.]
        self.assertGreaterEqual(len(renewed), 6)
        for previous, following in zip(renewed, renewed[1:]):
            self.assertLess(following["at"], previous["at"]+previous["ttl"])
        self.assert_history()

    async def test_full_raw_headroom_can_raise_fourteen_despite_unchanged_old_sixteen(self):
        r = await self.normal_sixteen_to_fourteen()
        self.old_max()
        await self.tick()
        self.assertEqual(r.last_target[0], 14)
        receipt = self.controller._read_input().charger_report_ts
        native = deepcopy(vars(self.native))
        await self.sample(280., 5.3)
        self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.assertLessEqual(5.3+16, 23.)
        self.assertEqual(self.controller._read_input().charger_report_ts, receipt)
        self.assertEqual(vars(self.native), native)
        self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])
        self.assertEqual(r._committed_normal.unresolved_cap, 16)

    async def test_pending_store_retains_old_accepted_cap_without_sdk_or_new_lease(self):
        r = await self.normal_sixteen_to_fourteen()
        self.old_max()
        await self.tick()
        accepted = r.last_set_limit_ts
        prior = self.controller._budget_store
        self.controller._budget_store = type(prior)(prior.store,
            clock=prior.clock, read_back=prior.store.async_load)
        self.slow_first_checkpoint()
        await self.sample(280., 21.3)
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_target[0], 14)
        self.assertEqual(r.last_set_limit_ts, accepted)
        await self.controller._budget_store.async_wait_pending()

    async def test_due_cached_renewal_waits_without_writes_for_native_quiet(self):
        r = await self.normal_sixteen_to_fourteen()
        accepted = r.last_set_limit_ts
        self.old_max(at=280.)
        self.native.last_tuple_change_monotonic = 279.95
        self.native.observation_report_times = ((182,self.wall(279.95)), (183,self.wall(162.2)))
        self.assertFalse(self.controller._read_input().charger_increase_ready)
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.now = 280.3
        self.pulse()
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 14)
        self.assertEqual(r.last_target[0], 14)

    async def test_existing_publication_fence_retains_only_old_cap_until_commit(self):
        r = await self.normal_sixteen_to_fourteen()
        accepted = r.last_set_limit_ts
        self.publication()
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        self.assertEqual(self.adapter.calls, [])
        deadline = self.controller._current_publication_deadline
        self.assertEqual(deadline, 273.9)
        self.publication(at=273.6, started=273.6)
        await self.tick()
        self.assertEqual(self.controller._current_publication_deadline, deadline)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.last_set_limit_ts, accepted)
        self.old_max(at=273.7)
        await self.tick()
        self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
        self.assert_no_zero()

    async def test_publication_timeout_remains_fixed_and_preempts_normal_ancestry(self):
        r = await self.normal_sixteen_to_fourteen()
        self.publication()
        await self.tick()
        self.assertEqual(self.adapter.calls, [])
        self.publication(at=274., started=274.)
        await self.tick()
        self.assertEqual(r.last_target[0], 0, self.controller.last_decision)
        self.assertEqual(self.controller._current_publication_deadline, 273.9)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_status_and_grid_handoff_in_both_orders_never_renews_status_anchor(self):
        for grid_first in (False, True):
            with self.subTest(grid_first=grid_first):
                if grid_first:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.normal_sixteen_to_fourteen()
                self.old_max()
                await self.tick()
                self.adapter.calls.clear()
                accepted = r.last_set_limit_ts
                if grid_first:
                    await self.sample(274., 5.3)
                    self.report(274.4, "awaiting_start", 5.3, .157, 274.3)
                    await self.tick()
                else:
                    self.report(274., "awaiting_start", 21.3, .157, 273.9)
                    await self.tick()
                    self.assertEqual(self.adapter.calls, [])
                    self.assertEqual(r.last_target[0], 14, self.controller.last_decision)
                    self.assertEqual(r.last_set_limit_ts, accepted)
                    await self.sample(274.4, 5.3)
                # A later independent full-RAW frame can use the existing
                # RAW-budgeted rise path:5.3+entire16<=23. This is not cached
                # EV subtraction or a renewal granted by the status anchor.
                self.assertGreaterEqual(r.last_target[0], 14, self.controller.last_decision)
                self.assertLessEqual(r.last_target[0], 16)
                self.assert_no_zero()
                for request in self.adapter.calls:
                    self.assertLessEqual(5.3+request.current_p1, 23.)
                self.assertFalse(self.controller._delivery.latest.public()["physical_response_verified"])

    async def test_expired_accepted_lease_cannot_be_repaired_by_cached_normal_proof(self):
        r = await self.normal_sixteen_to_fourteen()
        self.old_max()
        await self.tick()
        expiry = r.last_set_limit_ts+r.last_target[3]
        await self.sample(expiry, 21.3)
        self.assertEqual(r.last_target[0], 0, self.controller.last_decision)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)

    async def test_ambiguous_sdk_failure_is_not_reclassified_as_normal_continuity(self):
        r = await self.normal_sixteen_to_fourteen()
        self.old_max()
        await self.tick()
        self.dispatch_failure = RuntimeError("synthetic post-dispatch transport failure")
        await self.sample(280., 21.3)
        self.assertEqual(self.adapter.calls[-1].current_p1, 14)
        self.assertEqual(self.controller._delivery.latest.state, "submission_failed_unverified")
        self.dispatch_failure = None
        await self.sample(281., 21.3)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0, self.controller.last_decision)
        self.assertEqual(r.last_target[0], 0)

    async def test_source_fault_and_hard_peak_preempt_cached_normal_proof(self):
        for failure in ("native_invalid", "binding", "hard_peak", "replay",
                        "future", "uncommitted", "stale_grid"):
            with self.subTest(failure=failure):
                if failure != "native_invalid":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                r = await self.normal_sixteen_to_fourteen()
                self.old_max()
                await self.tick()
                self.adapter.calls.clear()
                self.now = 274.
                self.grid(38.01 if failure == "hard_peak" else 21.3)
                self.pulse()
                if failure == "native_invalid":
                    self.native.error = "native_sdk_current_invalid"
                elif failure == "binding":
                    self.controller._configured_control_identity.return_value = "other-source"
                elif failure == "replay":
                    self.native.observation_sequences = ((182,3), (183,1), (184,0), (185,0))
                elif failure == "future":
                    self.native.received_at = self.wall(280.)
                    self.native.observation_report_times = ((182,self.wall(272.7)), (183,self.wall(280.)))
                elif failure == "uncommitted":
                    self.native.committed = False
                elif failure == "stale_grid":
                    self.grid(21.3, 0.)
                await self.tick()
                self.assertEqual(r.last_target[0], 0, self.controller.last_decision)
                self.assertTrue(all(d.current_p1 == 0 for d in self.adapter.calls))

    async def test_real_peak_keeps_original_ten_second_vehicle_deadline(self):
        r = await self.normal_sixteen_to_fourteen()
        first = await self.sample(274., 32.)
        self.assertEqual(first.current_p1, 7)
        self.assertEqual(first.nominal_response_deadline_ts, 284.)
        hold = await self.sample(283.9, 32.)
        self.assertEqual(hold.current_p1, 7)
        self.assertEqual(hold.nominal_response_deadline_ts, 284.)
        expired = await self.sample(284., 32.)
        self.assertEqual(expired.current_p1, 0, expired)
        self.assertEqual(r.last_target[0], 0)

    async def test_timely_seven_still_gets_separate_thirty_second_grid_confirmation(self):
        r = await self.normal_sixteen_to_fourteen()
        first = await self.sample(274., 32.)
        self.assertEqual(first.nominal_response_deadline_ts, 284.)
        self.report(276., "charging", 32., 7.009, 275.7)
        self.grid(32., 274.)
        await self.tick()
        decision = self.controller.last_decision
        deadline = decision.grid_confirmation_deadline_ts
        self.assertAlmostEqual(deadline, 305.7, places=6)
        self.assertEqual(decision.current_p1, 7)
        self.assertIsNone(decision.nominal_response_deadline_ts)
        self.now = 284.
        self.pulse()
        await self.tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertEqual(self.controller.last_decision.grid_confirmation_deadline_ts, deadline)
        self.now = deadline+.01
        self.pulse()
        await self.tick()
        self.assertEqual(r.last_target[0], 0, self.controller.last_decision)


class NormalUpperPublicAcceptanceTests(unittest.TestCase):
    def test_independent_raw_nine_to_fourteen_retains_unconfirmed_sixteen(self):
        helper = public_proof.NormalObservationProofTests()
        r = helper.fourteen()
        epoch = public_proof.EPOCH
        helper.send(r, 173., 21.3, 16.078, receipt=69.2, sequence=1,
            charger_report_provenance=("receiver", "max_terminals",183),
            charger_observation_report_times=((182,epoch+172.7),(183,epoch+69.2)),
            charger_observation_sequences=((182,2),(183,1),(184,0),(185,0)))
        # Public policy changes create the narrower real SDK/ACK offer; no
        # private owner or fabricated accepted command is inserted.
        r.update_settings(replace(r.settings, enable_soft_guard=True,
            soft_limit_amps=20, soft_for_seconds=0, soft_current_amps=9))
        _, lower = helper.send(r, 174., 24.4, 16.078, receipt=173.5, sequence=3)
        self.assertEqual((lower.mode, r.last_target[0]), ("soft_limit",9))
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        r.update_settings(replace(r.settings, max_charge_amps=14))
        # The newly delivered native report predates the9 A SDK boundary.
        # WholeRAW6.8+entire16<=23 independently permits14; that does not
        # convert this earlier native report into a physical response.
        inp, rise = helper.send(r, 180., 6.8, 7., receipt=173.6, sequence=4)
        self.assertEqual((rise.command, r.last_target[0]), ("set_limit",14), rise)
        self.assertEqual(rise.house_load_basis, "raw_grid_bounded_recovery")
        self.assertEqual(r._committed_normal.unresolved_cap, 16)
        self.assertTrue(r._committed_normal.observation_history)
        self.assertFalse(r._ev_advanced_since(r._accepted_control_ev_ts,
            "_accepted_control_ev_ts"))
        self.assertEqual(inp.charger_report_ts, epoch+173.6)


if __name__ == "__main__":
    unittest.main()
