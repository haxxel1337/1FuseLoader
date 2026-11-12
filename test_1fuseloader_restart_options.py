"""Restart continuity is an explicit bounded installation option, not a reset."""
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


const = runtime_fixtures.const
logic = runtime_fixtures.logic
KEY = "graceful_restart_bridge"


def profile(**changes):
    return {"meter_envelope_control": True,
            "nominal_import_only_fallback": True, **changes}


class RestartOptionTests(unittest.TestCase):
    def test_upgrade_keeps_bridge_off_without_rewriting_existing_preferences(self):
        self.assertEqual(const.OPT_GRACEFUL_RESTART_BRIDGE, KEY)
        self.assertIs(const.DEFAULT_OPTIONS[KEY], False)
        self.assertIs(logic.RegulatorSettings().graceful_restart_bridge, False)
        legacy = {key: value for key, value in const.DEFAULT_OPTIONS.items() if key != KEY}
        legacy.update(profile(active_control=True, dry_run=False,
                              overload_enabled=True, grid_confirmation_seconds=30))
        original = deepcopy(legacy)
        upgraded = const.validate_options(legacy)
        self.assertIs(upgraded[KEY], False)
        self.assertEqual({key: upgraded[key] for key in legacy}, original)
        self.assertEqual(legacy, original)
        settings = runtime_fixtures.runtime._settings_from_entry(SimpleNamespace(
            data={"controlled_phase": "L1"}, options=legacy))
        self.assertIs(settings.graceful_restart_bridge, False)

    def test_only_explicit_booleans_are_accepted(self):
        for value in (False, True):
            options = const.validate_options(profile(**{KEY: value}))
            self.assertIs(options[KEY], value)
        for value in ("true", "false", 0, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                const.validate_options(profile(**{KEY: value}))
        self.assertNotIn(KEY, const.OPTION_NUMBER_RANGES)

    def test_opt_in_requires_selected_meter_and_verified_import_profile(self):
        for changed in ({"meter_envelope_control": False},
                        {"nominal_import_only_fallback": False},
                        {"main_fuse_amps": 32},
                        {"buffer_amps": 1},
                        {"buffer_amps": 3},
                        {"min_charge_amps": 8},
                        {"max_charge_amps": 6}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                const.validate_options(profile(**{KEY: True, **changed}))
        for minimum, maximum in ((6, 7), (7, 7), (6, 16)):
            options = const.validate_options(profile(**{
                KEY: True, "min_charge_amps": minimum, "max_charge_amps": maximum}))
            self.assertTrue(options[KEY])

    def test_selection_does_not_activate_control_or_overload_or_change_normal_ttl(self):
        selected = const.validate_options(profile(**{KEY: True}))
        unselected = const.validate_options(profile())
        self.assertEqual({key: value for key, value in selected.items() if key != KEY},
                         {key: value for key, value in unselected.items() if key != KEY})
        self.assertFalse(selected["active_control"])
        self.assertTrue(selected["dry_run"])
        self.assertFalse(selected["overload_enabled"])
        self.assertEqual(selected["normal_ttl_seconds"], 60)
        self.assertEqual(selected["unused_phase_current"], 0)

    def test_disabling_bridge_and_active_control_or_enabling_dry_run_remains_possible(self):
        enabled = const.validate_options(profile(**{KEY: True,
            "active_control": True, "dry_run": False}))
        disabled = const.validate_options({**enabled, KEY: False,
            "meter_envelope_control": False, "nominal_import_only_fallback": False})
        self.assertFalse(disabled[KEY])
        self.assertFalse(disabled["meter_envelope_control"])
        self.assertFalse(disabled["nominal_import_only_fallback"])
        self.assertFalse(const.validate_options({**enabled, "active_control": False})["active_control"])
        self.assertTrue(const.validate_options({**enabled, "dry_run": True})["dry_run"])

    def test_translations_explain_finite_blind_normal_only_permission(self):
        component = Path(__file__).parent / "custom_components/1fuseloader"
        documents = {}
        for filename in ("strings.json", "translations/en.json", "translations/sv.json"):
            document = json.loads((component / filename).read_text(encoding="utf-8"))
            documents[filename] = document
            page = document["options"]["step"]["init"]
            self.assertIn("7 A", page["data"][KEY])
            self.assertIn("5 min", page["data"][KEY])
            for wording in ("7 A", "5 min", "0/0/0", "NORMAL"):
                self.assertIn(wording, page["data_description"][KEY])
        self.assertEqual(documents["strings.json"], documents["translations/en.json"])
        help_text = documents["strings.json"]["options"]["step"]["init"]["data_description"][KEY]
        self.assertIn("Off by default", help_text)
        self.assertIn("no live load balancing", help_text)
        self.assertIn("cannot guarantee uninterrupted charging", help_text)
        swedish = documents["translations/sv.json"]["options"]["step"]["init"]["data_description"][KEY]
        self.assertIn("utan löpande lastbalansering", swedish)


class RestartOptionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = runtime_fixtures.ControllerTests()
        await self.fixture.asyncSetUp()

    async def test_switch_changes_only_preference_without_resetting_budget_or_charger_calls(self):
        controller = self.fixture.controller
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock):
            await controller.async_set_switch("nominal_import_only_fallback", True)
            await controller.async_set_switch("meter_envelope_control", True)
            regulator = controller.regulator
            before = deepcopy(regulator.export_overload_state())
            for value in (True, False):
                await controller.async_set_switch(KEY, value)
                self.assertIs(controller.regulator, regulator)
                self.assertIs(controller.settings.graceful_restart_bridge, value)
                self.assertIs(self.fixture.entry.options[KEY], value)
                self.assertEqual(regulator.export_overload_state(), before)
        self.assertEqual(self.fixture.adapter.calls, [])

    async def test_missing_prerequisites_reject_opt_in_before_persisting_or_charger_calls(self):
        controller = self.fixture.controller
        before = deepcopy(self.fixture.entry.options)
        with patch.object(controller, "_async_tick_locked", new_callable=AsyncMock) as tick:
            with self.assertRaises(ValueError):
                await controller.async_set_switch(KEY, True)
            tick.assert_not_awaited()
        self.assertEqual(self.fixture.entry.options, before)
        self.assertFalse(controller.settings.graceful_restart_bridge)
        self.assertEqual(self.fixture.adapter.calls, [])


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class RestartOptionRealHATests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_options_flow_exposes_boolean_and_rejects_implicit_prerequisites(self):
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
        self.assertEqual(rejected["errors"], {"base": "invalid_settings"})
        accepted = await flow.async_step_init(profile(**{KEY: True}))
        self.assertEqual(accepted["type"], "create_entry")
        self.assertTrue(accepted["data"][KEY])
        self.assertFalse(accepted["data"]["active_control"])
        self.assertTrue(accepted["data"]["dry_run"])
        self.assertFalse(accepted["data"]["overload_enabled"])
        self.assertTrue(self.component._settings_from_entry(SimpleNamespace(
            data=self.entry.data, options=accepted["data"])).graceful_restart_bridge)

    async def test_switch_identity_and_calls_are_bound_to_bridge_preference(self):
        switches = importlib.import_module("custom_components.1fuseloader.switch")
        description = next(item for item in switches.SWITCH_DESCRIPTIONS if item.key == KEY)
        self.assertEqual(description.option_key, KEY)
        self.assertIn("7 A", description.name)
        self.assertIn("5 min", description.name)
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
