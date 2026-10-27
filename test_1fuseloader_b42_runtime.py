"""Standalone-policy runtime boundaries, with synthetic clocks and sources.

Actual controller loading, Store checkpoints, native mapped publications and
SDK admission run offline. No household data or Home Assistant connection.
"""
from copy import deepcopy
from dataclasses import replace
import asyncio
import unittest
from unittest.mock import patch

import test_1fuseloader_button_availability as button_fixture
import test_1fuseloader_config as config_fixture
import test_1fuseloader_controlled_l1_runtime as controlled_fixture
import test_1fuseloader_meter_envelope_controller as meter_fixture
import test_1fuseloader_b40_fixture_evidence as phase_fixture


class StandaloneRuntimeTests(unittest.IsolatedAsyncioTestCase):
    # Reuse source/adapter fixtures, not their inherited regression methods.
    for _name in ("update_entry", "wall", "schedule", "active_timers", "set_native",
                  "snapshot", "install", "native_vector", "grid", "configure",
                  "publish", "tick", "asyncTearDown", "execute", "report",
                  "slow_first_checkpoint", "sample"):
        locals()[_name] = getattr(meter_fixture.MeterEnvelopeControllerTests, _name)
    del _name

    async def asyncSetUp(self):
        await meter_fixture.MeterEnvelopeControllerTests.asyncSetUp(self)
        independent = dict(meter_envelope_control=True, overload_enabled=False,
            nominal_import_only_fallback=False, bounded_nominal_shedding=False,
            peak_only_response=False, automatic_overload_cycles=False)
        self.entry.options.update(independent)
        self.controller.regulator.update_settings(replace(self.controller.settings, **independent))
        phase_fixture.install(self, adopted=90.)

    async def normal_offer(self):
        for at in range(100, 191, 5):
            self.report(float(at), "disconnected", 3., .02, 99.)
            await self.tick()
            if self.controller.regulator.last_target and self.controller.regulator.last_target[0] == 16:
                return self.controller.regulator
        self.fail(str(self.controller.last_decision))

    async def test_overload_off_normal_control_reaches_and_renews_maximum(self):
        r = await self.normal_offer()
        first = self.now
        for offset in range(5, 226, 5):
            self.report(first+offset, "charging", 20.2, 16.2, first+1.)
            await self.tick()
            self.assertEqual(r.last_target[0], 16, self.controller.last_decision)
        positive = [d for d in self.adapter.calls if d.current_p1 > 0]
        self.assertGreaterEqual(len(positive), 4)
        self.assertTrue(all(d.current_p2 == d.current_p3 == 0 and d.ttl_seconds == 60
                            for d in positive))
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls))
        self.assertFalse(self.controller.settings.overload_enabled)
        self.assertFalse(self.controller.settings.automatic_overload_cycles)

    async def test_selected_normal_state_is_durable_without_legacy_flags(self):
        r = await self.normal_offer()
        # Persist the acknowledged transition on the following ordinary tick;
        # a pre-SDK checkpoint cannot claim that the later ACK already exists.
        self.report(self.now+5., "disconnected", 3., .02, 99.)
        await self.tick()
        state = r.export_overload_state()
        self.assertIn("meter_control", state)
        self.assertEqual(state["meter_control"]["version"], 1)
        self.assertTrue(self.controller._budget_store.state_is_durable(state))
        self.assertFalse(self.controller._normal_control_checkpoint_pending)
        loaded = await self.controller._budget_store.async_load()
        self.assertEqual(loaded["meter_control"], state["meter_control"])

    async def test_disabled_extra_permission_cannot_admit_minimum_projected_above_nominal(self):
        await self.normal_offer()
        await self.sample(130., 21.2, 16.2, 129.)
        self.adapter.calls.clear()
        await self.sample(150., 37.4, 16.2, 149.9)
        self.assertFalse(self.controller.settings.overload_enabled)
        self.assertTrue(self.adapter.calls)
        self.assertTrue(all(d.current_p1 == 0 and d.meter_zero_cause is not None
                            for d in self.adapter.calls))

    async def test_disabled_extra_permission_still_allows_proven_normal_peak_reduction(self):
        r = await self.normal_offer()
        await self.sample(130., 21.2, 16.2, 129.)
        self.adapter.calls.clear()
        await self.sample(150., 31., 16.2, 149.9)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
        self.assertFalse(self.adapter.calls[-1].meter_overload_authority)
        self.assertEqual(r.peak_response_deadline(), 160.)
        await self.sample(151., 22., 7., 150.5)
        self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls),
            [(d.current_p1, d.mode, d.reason) for d in self.adapter.calls])
        self.assertFalse(self.controller.settings.overload_enabled)

    async def test_manual_controller_actions_cannot_create_or_end_selected_cycle(self):
        await self.normal_offer()
        before = deepcopy(self.controller.regulator.export_overload_state())
        generation = self.controller._generation
        calls = list(self.adapter.calls)
        for action in (self.controller.async_start_overload_trial,
                       self.controller.async_end_overload_trial):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(ValueError, "standalone"):
                await action()
        self.assertEqual(self.controller._generation, generation)
        self.assertEqual(self.controller.regulator.export_overload_state(), before)
        self.assertEqual(self.adapter.calls, calls)

    async def test_effective_diagnostics_explain_automatic_accounting_without_mutating_setting(self):
        await self.normal_offer()
        attrs = self.controller.diagnostic_attributes()
        self.assertTrue(attrs["automatic_overload_cycles"])
        self.assertFalse(attrs["automatic_overload_cycles_setting"])
        self.assertTrue(attrs["peak_only_response"])
        self.assertFalse(attrs["peak_only_response_setting"])
        self.assertEqual(attrs["response_policy"], "standalone_meter_episode")
        self.assertIn(attrs["meter_control_state"],
                      {"NORMAL", "REDUCING", "GRID_WAIT", "MINIMUM_HOLD", "STOP_RECOVERY"})
        self.assertNotIn("state", attrs)

    async def pending_peak(self):
        self.entry.options["overload_enabled"] = True
        self.controller.regulator.update_settings(replace(self.controller.settings, overload_enabled=True))
        r = await self.normal_offer()
        await self.sample(130., 21.2, 16.2, 129.)
        self.adapter.calls.clear()
        writer = self.controller._budget_store.store
        save, gate, first = writer.async_save, asyncio.Event(), True

        async def gated(payload):
            nonlocal first
            if first:
                first = False
                await gate.wait()
            return await save(payload)

        writer.async_save = gated
        with patch.object(meter_fixture.fixtures.runtime, "EXPOSURE_STORE_TIMEOUT_SECONDS", .05):
            await self.sample(150., 31., 16.2, 149.9)
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(r.peak_response_deadline(), 160.)
        return r, gate

    async def test_successful_pending_checkpoint_resumes_original_reduction_before_deadline(self):
        r, gate = await self.pending_peak()
        with patch.object(meter_fixture.fixtures.runtime, "EXPOSURE_STORE_TIMEOUT_SECONDS", .05):
            try:
                await self.sample(151., 31.)
                self.assertEqual(self.adapter.calls, [])
                self.assertEqual(r.peak_response_deadline(), 160.)
            finally:
                gate.set()
                await self.controller._budget_store.async_wait_pending()
            await self.sample(152., 31.)
            self.assertEqual([d.current_p1 for d in self.adapter.calls], [7])
            self.assertEqual(r.last_set_limit_ts, 152.)
            self.assertEqual(r.peak_response_deadline(), 160.)
            await self.sample(153., 28.5, 7., 152.5)
            self.assertFalse(any(d.current_p1 == 0 for d in self.adapter.calls),
                [(d.current_p1, d.mode, d.meter_zero_cause, d.reason) for d in self.adapter.calls])
            self.assertGreaterEqual(r.export_overload_state()["automatic_cycle"]["used_seconds"], 3.)

    async def test_hard_peak_aborts_pending_positive_without_waiting_for_checkpoint(self):
        r, gate = await self.pending_peak()
        with patch.object(meter_fixture.fixtures.runtime, "EXPOSURE_STORE_TIMEOUT_SECONDS", .05):
            try:
                await self.sample(151., 39.)
                self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
                self.assertEqual(self.adapter.calls[-1].meter_zero_cause, "hard_limit")
            finally:
                gate.set()
                await self.controller._budget_store.async_wait_pending()
            await self.sample(152., 39.)
            self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls))

    async def test_new_manual_pause_rejects_queued_normal_reduction_before_sdk(self):
        # This is a normal decrease, so the separate extra-overload admission
        # gate cannot accidentally mask a missing pre-SDK intent check.
        r = await self.normal_offer()
        await self.sample(130., 21.2, 16.2, 129.)
        self.report(150., "charging", 25.4, 16.2, 149.9)
        planned = self.controller._read_input()
        decision = r.evaluate(planned)
        self.assertGreater(decision.current_p1, 0)
        self.assertLess(decision.current_p1, 16)
        self.assertFalse(decision.meter_overload_authority)
        self.report(151., "paused", 25.4, 16.2, 149.9)
        self.assertFalse(r.can_continue_meter_dispatch(planned, self.controller._read_input(), decision))

    async def test_old_positive_ack_cannot_replace_newer_actual_off_authority(self):
        r = await self.normal_offer()
        old_positive = self.adapter.calls[-1]
        await self.sample(130., 39., 16.2, 129.)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.adapter.calls.clear()
        # Replay an HTTP acceptance for an SDK call that really existed. It
        # cannot undo a later actual OFF, become a lease renewal or supply EV
        # current when the fresh meter proves only 2 A of normal headroom.
        r.acknowledge(old_positive, 131.)
        await self.sample(132., 21., 0., 131.8)
        self.assertFalse(any(d.current_p1 > 0 for d in self.adapter.calls),
            [(d.current_p1, d.mode, d.reason) for d in self.adapter.calls])


class StandaloneAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_manual_flag_cannot_make_selected_trial_buttons_available(self):
        controller = button_fixture.Controller(automatic=False)
        controller.settings.meter_envelope_control = True
        for key in ("start_overload_trial", "end_overload_trial"):
            description = next(d for d in button_fixture.buttons.BUTTON_DESCRIPTIONS if d.key == key)
            entity = button_fixture.buttons.OneFuseButton(controller,
                button_fixture.SimpleNamespace(entry_id="selected", title="Synthetic"), description)
            self.assertFalse(entity.available)
            with self.assertRaises(button_fixture.ServiceValidationError):
                await entity.async_press()
        controller.async_start_overload_trial.assert_not_awaited()
        controller.async_end_overload_trial.assert_not_awaited()
        self.assertEqual(controller.manual_effects, [])


class StandaloneZeroEvidenceSourceTests(unittest.IsolatedAsyncioTestCase):
    for _name in ("update_entry", "wall", "schedule", "active_timers", "set_native",
                  "snapshot", "install", "asyncSetUp", "asyncTearDown"):
        locals()[_name] = getattr(controlled_fixture.ControlledL1RuntimeTests, _name)
    del _name

    def test_selected_delivery_health_does_not_depend_on_aggregate_max_quality(self):
        self.native.error = "synthetic_aggregate_failure"
        inp = self.controller._read_input()
        self.assertFalse(inp.data_valid)
        self.assertTrue(inp.meter_grid_health.valid)
        self.assertTrue(inp.meter_actuator_health.valid)
        self.assertTrue(inp.meter_ev_health.valid)
        evidence = self.controller._zero_readback_evidence(inp)
        self.assertTrue(evidence["healthy"])
        # No actual zero request exists: a healthy DTO is not an OFF proof.
        self.assertIsNone(evidence["ev_current"])

    def test_real_critical_fault_still_blocks_delivery_health(self):
        inp = self.controller._read_input()
        invalid = replace(inp, meter_grid_health=type(inp.meter_grid_health)(False, "synthetic_grid_fault"))
        self.assertFalse(self.controller._zero_readback_evidence(invalid)["healthy"])


class StandaloneConfigurationTests(unittest.TestCase):
    def test_normal_profile_needs_no_legacy_estimator_or_trial_switches(self):
        values = dict(meter_envelope_control=True, overload_enabled=False,
            nominal_import_only_fallback=False, peak_only_response=False,
            automatic_overload_cycles=False)
        saved = deepcopy(values)
        checked = config_fixture.const.validate_options(values)
        self.assertEqual(values, saved)
        self.assertTrue(checked["meter_envelope_control"])
        for key in values.keys()-{"meter_envelope_control"}:
            self.assertFalse(checked[key])

    def test_legacy_automatic_profile_still_requires_its_legacy_response_policy(self):
        with self.assertRaises(ValueError):
            config_fixture.const.validate_options(dict(automatic_overload_cycles=True,
                peak_only_response=False))

    def test_selected_numerical_profile_remains_explicit(self):
        for key, value in (("main_fuse_amps", 32), ("startup_charge_amps", 6),
                           ("max_charge_amps", 17), ("normal_ttl_seconds", 120),
                           ("enable_dynamic_limit", False)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                config_fixture.const.validate_options(dict(meter_envelope_control=True,
                    **{key: value}))


if __name__ == "__main__":
    unittest.main()
