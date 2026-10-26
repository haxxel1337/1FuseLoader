"""Disconnected readiness is an additive normal-budget opt-in."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_1fuseloader_ha as ha_fixtures
import test_1fuseloader_runtime as runtime_fixtures
import test_1fuseloader_trial_epochs as trial_fixtures

const = runtime_fixtures.const
logic = trial_fixtures.logic
KEY = "disconnected_readiness"


class ReadinessSettingsTests(unittest.TestCase):
    def test_upgrade_preserves_existing_options_and_leaves_readiness_off(self):
        self.assertEqual(const.OPT_DISCONNECTED_READINESS, KEY)
        self.assertIs(const.DEFAULT_OPTIONS[KEY], False)
        self.assertIs(logic.RegulatorSettings().disconnected_readiness, False)
        legacy = {key: value for key, value in const.DEFAULT_OPTIONS.items() if key != KEY}
        legacy.update(active_control=True, dry_run=False, max_charge_amps=7,
                      grid_confirmation_seconds=30, fast_recovery=False)
        original = deepcopy(legacy)
        upgraded = const.validate_options(legacy)
        self.assertIs(upgraded[KEY], False)
        self.assertEqual({key: upgraded[key] for key in legacy}, legacy)
        self.assertEqual(legacy, original)
        settings = runtime_fixtures.runtime._settings_from_entry(SimpleNamespace(
            data={"controlled_phase": "L1"}, options=legacy))
        self.assertIs(settings.disconnected_readiness, False)
        self.assertEqual(settings.max_charge_amps, 7)

    def test_only_explicit_boolean_values_are_accepted(self):
        for value in (False, True):
            options = const.validate_options({KEY: value})
            self.assertIs(options[KEY], value)
            self.assertIs(logic.OneFuseRegulator(logic.RegulatorSettings(**options)).settings.disconnected_readiness, value)
        for value in ("true", "false", 0, 1, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    const.validate_options({KEY: value})
                with self.assertRaises(ValueError):
                    logic.OneFuseRegulator(logic.RegulatorSettings(disconnected_readiness=value))

    def test_readiness_does_not_require_experimental_policy_or_fixed_fuse_profile(self):
        for profile in ({}, {"main_fuse_amps": 35, "startup_charge_amps": 6}):
            options = const.validate_options({KEY: True, **profile})
            settings = logic.OneFuseRegulator(logic.RegulatorSettings(**options)).settings
            self.assertTrue(settings.disconnected_readiness)
            self.assertFalse(settings.overload_enabled)
            self.assertFalse(settings.peak_only_response)
            self.assertFalse(settings.automatic_overload_cycles)
            self.assertFalse(settings.active_control)
            self.assertTrue(settings.dry_run)

    def test_shipped_labels_and_help_describe_normal_only_opt_in(self):
        component = Path(__file__).parent / "custom_components" / "1fuseloader"
        documents = {}
        for filename in ("strings.json", "translations/en.json", "translations/sv.json"):
            document = json.loads((component / filename).read_text(encoding="utf-8"))
            documents[filename] = document
            page = document["options"]["step"]["init"]
            self.assertTrue(page["data"][KEY])
            self.assertTrue(page["data_description"][KEY])
        self.assertEqual(documents["strings.json"], documents["translations/en.json"])
        help_text = documents["strings.json"]["options"]["step"]["init"]["data_description"][KEY]
        self.assertIn("entire offer", help_text)
        self.assertIn("no start or resume", help_text)
        self.assertIn("Off by default", help_text)


class ReadinessControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = runtime_fixtures.ControllerTests()
        await self.fixture.asyncSetUp()

    async def test_switch_updates_existing_controller_without_manual_ledger_or_charger_effect(self):
        controller = self.fixture.controller
        regulator = controller.regulator
        before = deepcopy(regulator.export_overload_state())
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock) as tick:
            for value in (True, False):
                await controller.async_set_switch(KEY, value)
                self.assertIs(controller.regulator, regulator)
                self.assertIs(self.fixture.entry.options[KEY], value)
                self.assertIs(controller.settings.disconnected_readiness, value)
                tick.assert_awaited_once_with()
                tick.reset_mock()
        self.assertEqual(regulator.export_overload_state(), before)
        self.assertEqual(self.fixture.adapter.calls, [])

    async def test_nonboolean_option_is_rejected_before_persistence_or_runtime_update(self):
        controller = self.fixture.controller
        before = deepcopy(self.fixture.entry.options)
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock) as tick:
            with self.assertRaises(ValueError):
                await controller.async_set_option(KEY, "true")
            tick.assert_not_awaited()
        self.assertEqual(self.fixture.entry.options, before)
        self.assertFalse(controller.settings.disconnected_readiness)
        self.assertEqual(self.fixture.adapter.calls, [])


class ReadinessLedgerTests(unittest.TestCase):
    def test_old_frozen_policy_missing_field_is_not_rewritten_or_refunded(self):
        helper = trial_fixtures.ExplicitTrialTests()
        old = deepcopy(helper.active().export_overload_state())
        old["trial_epoch"]["policy"]["settings"].pop(KEY, None)
        original = deepcopy(old)
        for selected in (False, True):
            with self.subTest(selected=selected):
                restored = helper.regulator(disconnected_readiness=selected)
                self.assertTrue(restored.import_overload_state(old))
                after = restored.export_overload_state()
                self.assertEqual(after["trial_epoch"]["policy"], old["trial_epoch"]["policy"])
                self.assertNotIn(KEY, after["trial_epoch"]["policy"]["settings"])
                self.assertEqual(after["used_seconds"], old["used_seconds"])
                self.assertEqual(after["failure_reason"], old["failure_reason"])
                self.assertEqual(after["trial_epoch"]["baseline_used_seconds"], old["trial_epoch"]["baseline_used_seconds"])
                self.assertEqual(after["trial_epoch"]["state"], "interrupted")
                self.assertFalse(restored.trial_diagnostics()["overload_trial_activation_committed"])
                self.assertEqual(old, original)


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class ReadinessRealHATests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_options_flow_exposes_false_default_and_allows_independent_explicit_opt_in(self):
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
        accepted = await flow.async_step_init({KEY: True})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertTrue(accepted["data"][KEY])
        self.assertFalse(accepted["data"]["peak_only_response"])
        self.assertFalse(accepted["data"]["overload_enabled"])
        self.assertTrue(self.component._settings_from_entry(SimpleNamespace(
            data=self.entry.data, options=accepted["data"])).disconnected_readiness)
        self.assertFalse(self.entry.options[KEY])

    async def test_switch_identity_and_calls_use_readiness_setting(self):
        switches = importlib.import_module("custom_components.1fuseloader.switch")
        description = next(item for item in switches.SWITCH_DESCRIPTIONS if item.key == KEY)
        self.assertEqual(description.name, "Charging Readiness While Disconnected")
        controller = SimpleNamespace(async_set_switch=AsyncMock())
        entity = switches.OneFuseSwitch(controller, self.entry, description)
        self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_{KEY}")
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        controller.async_set_switch.assert_awaited_once_with(KEY, True)
        controller.async_set_switch.reset_mock()
        await entity.async_turn_off()
        controller.async_set_switch.assert_awaited_once_with(KEY, False)


if __name__ == "__main__":
    unittest.main()
