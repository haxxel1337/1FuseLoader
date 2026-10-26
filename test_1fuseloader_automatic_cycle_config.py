"""Configuration gates for the separately selected automatic cycle lifecycle."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_1fuseloader_config as config_fixtures
import test_1fuseloader_ha as ha_fixtures

const = config_fixtures.const
KEY = "automatic_overload_cycles"


class AutomaticCycleSettingsTests(unittest.TestCase):
    def test_default_and_legacy_options_do_not_enable_automatic_cycles(self):
        self.assertEqual(const.OPT_AUTOMATIC_OVERLOAD_CYCLES, KEY)
        self.assertIs(const.DEFAULT_OPTIONS[KEY], False)
        legacy = dict(const.DEFAULT_OPTIONS)
        del legacy[KEY]
        legacy.update(peak_only_response=True, overload_enabled=True,
                      active_control=True, dry_run=False, grid_confirmation_seconds=30)
        original = deepcopy(legacy)
        upgraded = const.validate_options(legacy)
        self.assertIs(upgraded[KEY], False)
        self.assertEqual({key: upgraded[key] for key in legacy}, legacy)
        self.assertEqual(legacy, original)

    def test_boolean_validation_cannot_enable_mode_from_strings_or_numbers(self):
        for value in (False, True):
            options = const.validate_options({KEY: value, "peak_only_response": True})
            self.assertIs(options[KEY], value)
        for value in ("true", "false", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                const.validate_options({KEY: value, "peak_only_response": True})

    def test_enabling_automatic_cycles_requires_explicit_peak_policy(self):
        for options in ({KEY: True}, {KEY: True, "peak_only_response": False}):
            original = deepcopy(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                const.validate_options(options)
            self.assertEqual(options, original)
        selected = const.validate_options({KEY: True, "peak_only_response": True})
        previous = deepcopy(selected)
        with self.assertRaises(ValueError):
            const.validate_options({**selected, "peak_only_response": False})
        self.assertEqual(selected, previous)

    def test_overload_operation_can_be_disabled_without_discarding_selected_policy(self):
        selected = const.validate_options({KEY: True, "peak_only_response": True,
                                           "overload_enabled": True})
        disabled = const.validate_options({**selected, "overload_enabled": False})
        self.assertIs(disabled[KEY], True)
        self.assertIs(disabled["peak_only_response"], True)
        self.assertIs(disabled["overload_enabled"], False)
        # Selecting a lifecycle must not itself switch on writes or experimental operation.
        defaults = const.validate_options({KEY: True, "peak_only_response": True})
        self.assertIs(defaults["active_control"], False)
        self.assertIs(defaults["dry_run"], True)
        self.assertIs(defaults["overload_enabled"], False)

    def test_automatic_profile_rejects_engine_incompatible_settings_before_storage(self):
        for change in ({"main_fuse_amps": 35}, {"startup_charge_amps": 6}):
            options = {KEY: True, "peak_only_response": True, "overload_enabled": False, **change}
            original = deepcopy(options)
            with self.subTest(change=change), self.assertRaises(ValueError):
                const.validate_options(options)
            self.assertEqual(options, original)
            # The additive automatic profile must not make legacy settings unloadable.
            self.assertIs(const.validate_options({**options, KEY: False})[KEY], False)

    def test_option_has_shipped_names_in_both_languages(self):
        component = Path(__file__).parent / "custom_components" / "1fuseloader"
        documents = {}
        for filename in ("strings.json", "translations/en.json", "translations/sv.json"):
            content = json.loads((component / filename).read_text(encoding="utf-8"))
            documents[filename] = content
            with self.subTest(filename=filename):
                self.assertTrue(content["options"]["step"]["init"]["data"][KEY])
        self.assertEqual(documents["strings.json"], documents["translations/en.json"])


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class AutomaticCycleRealHATests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_options_flow_checks_peak_dependency_and_retains_off_operation_gate(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        form = await flow.async_step_init()
        fields = form["data_schema"].schema
        marker = next(key for key in fields if str(key.schema) == KEY)
        self.assertIs(marker.default(), False)
        self.assertIs(fields[marker], bool)
        rejected = await flow.async_step_init({KEY: True})
        self.assertEqual(rejected["type"], "form")
        self.assertEqual(rejected["errors"].get("base"), "invalid_settings")
        accepted = await flow.async_step_init({KEY: True, "peak_only_response": True})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertIs(accepted["data"][KEY], True)
        self.assertIs(accepted["data"]["overload_enabled"], False)
        settings = self.component._settings_from_entry(SimpleNamespace(
            data=self.entry.data, options=accepted["data"]))
        self.assertIs(settings.automatic_overload_cycles, True)
        self.assertIs(self.entry.options[KEY], False)

    async def test_switch_exposes_selected_policy_with_stable_identity(self):
        switches = importlib.import_module("custom_components.1fuseloader.switch")
        description = next(item for item in switches.SWITCH_DESCRIPTIONS if item.key == KEY)
        self.assertEqual(description.name, "Automatic Overload Cycles")
        self.assertEqual(description.option_key, KEY)
        controller = SimpleNamespace(async_set_switch=AsyncMock())
        entity = switches.OneFuseSwitch(controller, self.entry, description)
        self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_{KEY}")
        self.assertFalse(entity.is_on)
        await entity.async_turn_on()
        controller.async_set_switch.assert_awaited_once_with(KEY, True)
        controller.async_set_switch.reset_mock()
        await entity.async_turn_off()
        controller.async_set_switch.assert_awaited_once_with(KEY, False)

    async def test_options_flow_rejects_unsupported_automatic_profile_before_create_entry(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        for change in ({"main_fuse_amps": 35}, {"startup_charge_amps": 6}):
            with self.subTest(change=change):
                flow = self.flow_module.OneFuseLoaderOptionsFlow()
                flow.hass = self.hass
                flow.handler = self.entry.entry_id
                original = dict(self.entry.options)
                result = await flow.async_step_init({KEY: True, "peak_only_response": True, **change})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"].get("base"), "invalid_settings")
                self.assertEqual(dict(self.entry.options), original)


if __name__ == "__main__":
    unittest.main()
