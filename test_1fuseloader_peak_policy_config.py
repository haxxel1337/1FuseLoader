"""Peak-only response is an explicit option, never an upgrade or ledger reset.

The runtime fixture has no HA connection. The optional real-HA cases exercise
the installed options-flow and switch classes without contacting a charger.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_1fuseloader_ha as ha_fixtures
import test_1fuseloader_runtime as runtime_fixtures
import test_1fuseloader_trial_epochs as trial_fixtures

const = runtime_fixtures.const
logic = trial_fixtures.logic
KEY = "peak_only_response"


class PeakPolicySettingsTests(unittest.TestCase):
    def test_default_and_legacy_upgrade_leave_peak_policy_disabled(self):
        self.assertIs(const.DEFAULT_OPTIONS[KEY], False)
        self.assertIs(logic.RegulatorSettings().peak_only_response, False)
        legacy = dict(const.DEFAULT_OPTIONS)
        del legacy[KEY]
        legacy.update(active_control=True, dry_run=False, buffer_amps=2.5,
                      fast_recovery=False, grid_confirmation_seconds=30)
        original = deepcopy(legacy)
        upgraded = const.validate_options(legacy)
        self.assertIs(upgraded[KEY], False)
        self.assertEqual({key: upgraded[key] for key in legacy}, legacy)
        self.assertEqual(legacy, original)
        entry = SimpleNamespace(data={"controlled_phase": "L1"}, options=legacy)
        settings = runtime_fixtures.runtime._settings_from_entry(entry)
        self.assertIs(settings.peak_only_response, False)
        self.assertEqual(settings.grid_confirmation_seconds, 30)
        self.assertFalse(settings.fast_recovery)

    def test_explicit_boolean_is_validated_by_options_and_regulator(self):
        for value in (False, True):
            with self.subTest(value=value):
                options = const.validate_options({KEY: value})
                self.assertIs(options[KEY], value)
                regulator = logic.OneFuseRegulator(logic.RegulatorSettings(**options))
                self.assertIs(regulator.settings.peak_only_response, value)
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value):
                original = {KEY: value}
                with self.assertRaises(ValueError):
                    const.validate_options(original)
                self.assertEqual(original, {KEY: value})
                with self.assertRaises(ValueError):
                    logic.OneFuseRegulator(logic.RegulatorSettings(peak_only_response=value))

    def test_peak_option_does_not_widen_electrical_response_range(self):
        for seconds in (1, 10):
            options = const.validate_options({KEY: True, "overload_response_seconds": seconds})
            self.assertEqual(options["overload_response_seconds"], seconds)
        for seconds in (0, 11, 30):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                const.validate_options({KEY: True, "overload_response_seconds": seconds})


class PeakPolicyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.helper = trial_fixtures.ExplicitTrialTests()

    def test_active_trial_freezes_true_flag_and_change_interrupts_without_refund(self):
        regulator, inp = self.helper.ready(peak_only_response=True, grid_confirmation_seconds=30)
        self.helper.activate(regulator, inp)
        before = deepcopy(regulator.export_overload_state())
        self.assertIs(before["trial_epoch"]["policy"]["settings"][KEY], True)
        regulator.update_settings(replace(regulator.settings, peak_only_response=False))
        after = regulator.export_overload_state()
        self.assertEqual(after["trial_epoch"]["policy"], before["trial_epoch"]["policy"])
        self.assertEqual(after["trial_epoch"]["state"], "interrupted")
        self.assertEqual(after["trial_epoch"]["failure_reason"], "settings_changed")
        self.assertEqual(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], before["failure_reason"])
        self.assertEqual(after["trial_epoch"]["baseline_used_seconds"],
                         before["trial_epoch"]["baseline_used_seconds"])

    def test_old_policy_missing_flag_is_compared_as_false_without_rewriting_history(self):
        old = deepcopy(self.helper.active().export_overload_state())
        del old["trial_epoch"]["policy"]["settings"][KEY]
        original = deepcopy(old)
        for selected in (False, True):
            with self.subTest(current_opt_in=selected):
                restored = self.helper.regulator(peak_only_response=selected)
                self.assertTrue(restored.import_overload_state(old))
                state = restored.export_overload_state()
                self.assertIs(restored.settings.peak_only_response, selected)
                self.assertEqual(state["trial_epoch"]["policy"], old["trial_epoch"]["policy"])
                self.assertNotIn(KEY, state["trial_epoch"]["policy"]["settings"])
                self.assertEqual(state["used_seconds"], old["used_seconds"])
                self.assertEqual(state["failure_reason"], old["failure_reason"])
                self.assertEqual(state["trial_epoch"]["baseline_used_seconds"],
                                 old["trial_epoch"]["baseline_used_seconds"])
                self.assertEqual(state["trial_epoch"]["state"], "interrupted")
                self.assertEqual(state["trial_epoch"]["failure_reason"], "interrupted_after_restore")
                self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                self.assertEqual(old, original)

    def test_true_policy_round_trip_keeps_frozen_flag_and_prior_failure(self):
        regulator, inp = self.helper.ready(peak_only_response=True)
        self.helper.activate(regulator, inp)
        saved = deepcopy(regulator.export_overload_state())
        restored = self.helper.regulator(peak_only_response=True)
        self.assertTrue(restored.import_overload_state(saved))
        state = restored.export_overload_state()
        self.assertEqual(state["trial_epoch"]["policy"], saved["trial_epoch"]["policy"])
        self.assertIs(state["trial_epoch"]["policy"]["settings"][KEY], True)
        self.assertEqual(state["used_seconds"], saved["used_seconds"])
        self.assertEqual(state["failure_reason"], "response_timeout")
        self.assertEqual(state["trial_epoch"]["state"], "interrupted")


class PeakPolicyControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = runtime_fixtures.ControllerTests()
        await self.fixture.asyncSetUp()

    async def test_switch_updates_running_controller_and_entry_without_recreating_regulator(self):
        controller = self.fixture.controller
        regulator = controller.regulator
        self.assertNotIn(KEY, self.fixture.entry.options)
        self.assertIs(controller.settings.peak_only_response, False)
        self.assertTrue(regulator.import_overload_state(dict(
            version=1, used_seconds=123.0, exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout")))
        before = deepcopy(regulator.export_overload_state())
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock) as tick:
            await controller.async_set_switch(KEY, True)
            self.assertIs(controller.regulator, regulator)
            self.assertIs(self.fixture.entry.options[KEY], True)
            self.assertIs(controller.settings.peak_only_response, True)
            tick.assert_awaited_once_with()
            tick.reset_mock()
            await controller.async_set_switch(KEY, False)
            self.assertIs(controller.regulator, regulator)
            self.assertIs(self.fixture.entry.options[KEY], False)
            self.assertIs(controller.settings.peak_only_response, False)
            tick.assert_awaited_once_with()
        after = regulator.export_overload_state()
        self.assertEqual(after["used_seconds"], before["used_seconds"])
        self.assertEqual(after["failure_reason"], before["failure_reason"])
        self.assertEqual(self.fixture.adapter.calls, [])

    async def test_invalid_option_is_rejected_before_entry_or_runtime_changes(self):
        controller = self.fixture.controller
        original = deepcopy(self.fixture.entry.options)
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock) as tick:
            with self.assertRaises(ValueError):
                await controller.async_set_option(KEY, "true")
            tick.assert_not_awaited()
        self.assertEqual(self.fixture.entry.options, original)
        self.assertIs(controller.settings.peak_only_response, False)
        self.assertEqual(self.fixture.adapter.calls, [])


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class PeakPolicyRealHATests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup only; do not inherit or rerun the existing HA test suite.
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_options_flow_exposes_false_default_and_accepts_only_explicit_boolean(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        form = await flow.async_step_init()
        fields = form["data_schema"].schema
        marker = next(key for key in fields if str(key.schema) == KEY)
        self.assertIs(marker.default(), False)
        self.assertIs(fields[marker], bool)
        rejected = await flow.async_step_init({KEY: "true"})
        self.assertEqual(rejected["type"], "form")
        self.assertEqual(rejected["errors"].get("base"), "invalid_settings")
        accepted = await flow.async_step_init({KEY: True})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertIs(accepted["data"][KEY], True)
        self.assertIs(self.component._settings_from_entry(SimpleNamespace(
            data=self.entry.data, options=accepted["data"])).peak_only_response, True)
        self.assertIs(self.entry.options[KEY], False)

    async def test_switch_entity_has_stable_identity_and_routes_changes_to_controller(self):
        module = importlib.import_module("custom_components.1fuseloader.switch")
        description = next(item for item in module.SWITCH_DESCRIPTIONS if item.key == KEY)
        self.assertEqual(description.option_key, KEY)
        controller = SimpleNamespace(async_set_switch=AsyncMock())
        entity = module.OneFuseSwitch(controller, self.entry, description)
        self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_{KEY}")
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        controller.async_set_switch.assert_awaited_once_with(KEY, True)
        controller.async_set_switch.reset_mock()
        await entity.async_turn_off()
        controller.async_set_switch.assert_awaited_once_with(KEY, False)


if __name__ == "__main__":
    unittest.main()
