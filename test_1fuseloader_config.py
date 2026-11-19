"""Settings regression tests; the HA flow cases also run when HA is installed."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).parent
COMPONENT = ROOT / "custom_components" / "1fuseloader"
spec = importlib.util.spec_from_file_location("onefuse_config_constants", COMPONENT / "const.py")
assert spec and spec.loader
const = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = const
spec.loader.exec_module(const)


class SettingsTests(unittest.TestCase):
    def test_defaults_remain_monitor_only_and_load_legacy_options(self):
        options = const.validate_options({})
        self.assertFalse(options["active_control"])
        self.assertTrue(options["dry_run"])
        self.assertFalse(options["force_min_when_available_too_low"])
        self.assertEqual(options["unused_phase_current"], 0)
        self.assertEqual(options["max_sensor_age_seconds"], 120)
        self.assertFalse(options["overload_enabled"])
        self.assertEqual(options["overload_margin_amps"], 4)
        self.assertEqual(options["overload_budget_seconds"], 1800)
        self.assertEqual(options["overload_transition_limit_amps"], 38)
        self.assertEqual(options["overload_response_seconds"], 10)
        # Existing 0.3.x entries used a 30-second resend and a 60-second TTL.
        legacy = dict(const.DEFAULT_OPTIONS)
        legacy.pop("max_sensor_age_seconds")
        self.assertEqual(const.validate_options(legacy), options)

    def test_legacy_forced_minimum_is_disabled_on_validation(self):
        self.assertFalse(const.validate_options({"force_min_when_available_too_low": True})["force_min_when_available_too_low"])

    def test_fast_recovery_is_additive_and_boolean(self):
        self.assertTrue(const.DEFAULT_OPTIONS["fast_recovery"])
        self.assertTrue(const.validate_options({})["fast_recovery"])
        self.assertFalse(const.validate_options({"fast_recovery": False})["fast_recovery"])
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                const.validate_options({"fast_recovery": value})

    def test_startup_setting_keeps_legacy_minimum_and_maximum_editable(self):
        options = const.validate_options({})
        self.assertEqual(options["startup_charge_amps"], 7)
        self.assertEqual(options["min_charge_amps"], 6)
        # The regulator clamps the requested start offer to the configured car
        # range. A newly introduced default must not unload an old 6 A entry.
        legacy = const.validate_options({"max_charge_amps": 6})
        self.assertEqual(legacy["max_charge_amps"], 6)
        self.assertEqual(legacy["startup_charge_amps"], 7)
        for value in (5, 17, 6.5, True, float("nan")):
            with self.subTest(startup=value), self.assertRaises(ValueError):
                const.validate_options({"startup_charge_amps": value})

    def test_rejects_unsafe_numeric_values_before_storage(self):
        cases = [
            {"buffer_amps": -100}, {"main_fuse_amps": float("nan")},
            {"max_charge_amps": float("inf")}, {"max_charge_amps": True},
            {"min_charge_amps": 6.5}, {"normal_ttl_seconds": -5},
            {"normal_ttl_seconds": 59}, {"unused_phase_current": 1},
            {"max_sensor_age_seconds": 0}, {"dry_run": "false"},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                const.validate_options(options)

    def test_rejects_valid_individual_values_with_unsafe_combinations(self):
        cases = [
            {"min_charge_amps": 16, "max_charge_amps": 6},
            {"max_charge_amps": 6, "soft_current_amps": 10},
            {"resume_below_amps": 28, "pause_limit_amps": 28},
            {"resume_below_amps": 29, "soft_limit_amps": 28},
            {"resend_limit_every_seconds": 35, "normal_ttl_seconds": 60},
            {"min_seconds_between_actions": 30, "normal_ttl_seconds": 60},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                const.validate_options(options)

    def test_failed_update_does_not_mutate_the_callers_options(self):
        options = dict(const.DEFAULT_OPTIONS)
        options["min_charge_amps"] = 16
        options["max_charge_amps"] = 6
        original = dict(options)
        with self.assertRaises(ValueError):
            const.validate_options(options)
        self.assertEqual(options, original)

    def test_continuity_reserve_fits_normal_buffer_and_legacy_zero_migrates(self):
        self.assertEqual(const.validate_options({"buffer_amps": 0})["minimum_reserve_amps"], 0)
        with self.assertRaises(ValueError):
            const.validate_options({"buffer_amps": 1, "minimum_reserve_amps": 2})
        # The hidden, unused old threshold must not make a real fuse value
        # impossible to configure in the options flow.
        self.assertEqual(const.validate_options({"main_fuse_amps": 16})["safe_l1_amps"], 16)

    def test_valid_values_are_normalized_without_losing_decimal_margin(self):
        options = const.validate_options({"buffer_amps": "2.5", "max_charge_amps": "16"})
        self.assertEqual(options["buffer_amps"], 2.5)
        self.assertIsInstance(options["buffer_amps"], float)
        self.assertIsInstance(options["max_charge_amps"], int)

    def test_experimental_overload_requires_an_explicit_boolean_opt_in(self):
        self.assertFalse(const.validate_options({})["overload_enabled"])
        enabled = const.validate_options({"overload_enabled": True})
        self.assertTrue(enabled["overload_enabled"])
        self.assertEqual(enabled["main_fuse_amps"] + enabled["overload_margin_amps"], 29)
        with self.assertRaises(ValueError):
            const.validate_options({"overload_enabled": "true"})

    def test_experimental_overload_caps_are_not_bypassable_by_options(self):
        for options in (
            {"overload_margin_amps": 4.1}, {"overload_margin_amps": 0},
            {"overload_budget_seconds": 1801}, {"overload_budget_seconds": 59},
            {"overload_transition_limit_amps": 38.1},
            {"overload_response_seconds": 11}, {"overload_response_seconds": 0},
            {"overload_budget_seconds": 60.5},
            {"overload_transition_limit_amps": float("nan")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                const.validate_options(options)

    def test_enabled_overload_checks_the_current_fuse_and_dynamic_control(self):
        for options in (
            {"overload_enabled": True, "enable_dynamic_limit": False},
            {"overload_enabled": True, "main_fuse_amps": 10, "min_charge_amps": 10},
            {"overload_enabled": True, "main_fuse_amps": 35},
            {"overload_enabled": True, "overload_transition_limit_amps": 28.5},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                const.validate_options(options)
        # Keeping the disabled experimental defaults must not make an existing
        # larger installation unloadable. Its real policy still needs tuning
        # before opt-in, rather than silently expanding the experimental cap.
        self.assertFalse(const.validate_options({"main_fuse_amps": 63})["overload_enabled"])
        self.assertEqual(
            const.validate_options({"overload_enabled": True, "overload_transition_limit_amps": 29})["overload_transition_limit_amps"],
            29,
        )

    def test_custom_integration_translations_include_full_flow_text(self):
        def leaf_keys(value, prefix=""):
            keys = set()
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else key
                keys.update(leaf_keys(child, path) if isinstance(child, dict) else {path})
            return keys

        english = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
        swedish = json.loads((COMPONENT / "translations" / "sv.json").read_text(encoding="utf-8"))
        self.assertEqual(leaf_keys(english), leaf_keys(swedish))
        self.assertIn("config.error.current_unit_required", leaf_keys(english))
        self.assertIn("config.step.reconfigure.data.l1_entity", leaf_keys(english))
        self.assertIn("options.step.init.data.max_sensor_age_seconds", leaf_keys(english))
        for option in ("overload_enabled", "overload_margin_amps", "overload_budget_seconds", "overload_transition_limit_amps", "overload_response_seconds"):
            self.assertIn(f"options.step.init.data.{option}", leaf_keys(english))
        self.assertEqual(
            json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8")), english
        )


@unittest.skipUnless(importlib.util.find_spec("homeassistant"), "requires Home Assistant runtime")
class HomeAssistantFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow_module = importlib.import_module("custom_components.1fuseloader.config_flow")

    async def test_options_flow_rejects_invalid_combination_and_retains_input(self):
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        with patch.object(type(flow), "config_entry", new_callable=PropertyMock) as entry:
            entry.return_value = SimpleNamespace(options={})
            result = await flow.async_step_init({"min_charge_amps": 16, "max_charge_amps": 6})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_settings"})

    async def test_options_flow_preserves_hidden_single_phase_safety_settings(self):
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        with patch.object(type(flow), "config_entry", new_callable=PropertyMock) as entry:
            entry.return_value = SimpleNamespace(options={})
            result = await flow.async_step_init({"buffer_amps": 2.5})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["buffer_amps"], 2.5)
        self.assertEqual(result["data"]["unused_phase_current"], 0)

    async def test_options_flow_exposes_validated_experimental_settings(self):
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        with patch.object(type(flow), "config_entry", new_callable=PropertyMock) as entry:
            entry.return_value = SimpleNamespace(options={})
            form = await flow.async_step_init()
            fields = {marker.schema: field for marker, field in form["data_schema"].schema.items()}
            self.assertIs(fields["overload_enabled"], bool)
            self.assertEqual(fields["overload_transition_limit_amps"].config["max"], 38)
            self.assertEqual(fields["overload_budget_seconds"].config["min"], 60)
            rejected = await flow.async_step_init({"overload_enabled": True, "overload_transition_limit_amps": 28})
            self.assertEqual(rejected["errors"], {"base": "invalid_settings"})
            accepted = await flow.async_step_init({"overload_enabled": True})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertTrue(accepted["data"]["overload_enabled"])

    async def test_experimental_entity_ranges_match_shared_validation(self):
        numbers = importlib.import_module("custom_components.1fuseloader.number")
        switches = importlib.import_module("custom_components.1fuseloader.switch")
        experimental = [item for item in numbers.NUMBER_DESCRIPTIONS if item.option_key.startswith("overload_")]
        self.assertEqual(len(experimental), 4)
        for description in experimental:
            with self.subTest(option=description.option_key):
                self.assertEqual(
                    (description.native_min_value, description.native_max_value, description.native_step),
                    const.OPTION_NUMBER_RANGES[description.option_key],
                )
        self.assertIn("overload_enabled", {item.option_key for item in switches.SWITCH_DESCRIPTIONS})

    async def test_config_input_rejects_missing_entity_and_wrong_current_unit(self):
        flow = self.flow_module.OneFuseLoaderConfigFlow()
        states = {
            "sensor.status": SimpleNamespace(attributes={}),
            "sensor.current": SimpleNamespace(attributes={"unit_of_measurement": "A"}),
            "sensor.power": SimpleNamespace(attributes={"unit_of_measurement": "W"}),
        }
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        data = {"device_id": "charger", "controlled_phase": "L1", "status_entity": "sensor.status",
                "l1_entity": "sensor.power", "l2_entity": "sensor.missing", "l3_entity": "sensor.current",
                "charger_current_entity": "sensor.current"}
        registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(device_id="charger", platform="easee"))
        with patch.object(self.flow_module.dr, "async_get_device_and_config_entry_for_domain", return_value=(SimpleNamespace(id="charger"), object())), patch.object(self.flow_module.er, "async_get", return_value=registry):
            errors = flow._input_errors(data)
        self.assertEqual(errors["l1_entity"], "current_unit_required")
        self.assertEqual(errors["l2_entity"], "entity_not_found")

    async def test_config_input_rejects_grid_and_charger_aliases(self):
        flow = self.flow_module.OneFuseLoaderConfigFlow()
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={"unit_of_measurement": "A"})))
        data = {"device_id": "charger", "controlled_phase": "L1", "status_entity": "sensor.status",
                "l1_entity": "sensor.grid", "l2_entity": "sensor.grid", "l3_entity": "sensor.grid3",
                "charger_current_entity": "sensor.grid3"}
        registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(device_id="charger", platform="easee"))
        with patch.object(self.flow_module.dr, "async_get_device_and_config_entry_for_domain", return_value=(SimpleNamespace(id="charger"), object())), patch.object(self.flow_module.er, "async_get", return_value=registry):
            errors = flow._input_errors(data)
        self.assertEqual(errors, {"l1_entity": "duplicate_current_entity", "l2_entity": "duplicate_current_entity", "charger_current_entity": "duplicate_current_entity"})

    async def test_reconfigure_does_not_offer_an_ignored_name_field(self):
        schema = self.flow_module._config_schema({"name": "Existing title"}, include_name=False)
        self.assertNotIn("name", {marker.schema for marker in schema.schema})

    async def test_current_selectors_use_the_supported_nested_unit_filter(self):
        schema = self.flow_module._config_schema()
        fields = {marker.schema: field for marker, field in schema.schema.items()}
        for key in ("l1_entity", "l2_entity", "l3_entity", "charger_current_entity"):
            with self.subTest(key=key):
                self.assertNotIn("unit_of_measurement", fields[key].config)
                filters = fields[key].config["filter"]
                self.assertEqual(filters[0]["domain"], ["sensor"])
                self.assertEqual(filters[0]["unit_of_measurement"], ["A"])
        self.assertEqual(fields["charger_current_entity"].config["filter"][0]["integration"], "easee")

    async def test_charger_telemetry_must_belong_to_the_selected_easee_device(self):
        flow = self.flow_module.OneFuseLoaderConfigFlow()
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={"unit_of_measurement": "A"})))
        data = {"device_id": "charger", "controlled_phase": "L1", "status_entity": "sensor.status",
                "l1_entity": "sensor.grid1", "l2_entity": "sensor.grid2", "l3_entity": "sensor.grid3",
                "charger_current_entity": "sensor.car_current"}
        sources = {
            "sensor.status": SimpleNamespace(device_id="charger", platform="easee"),
            "sensor.car_current": SimpleNamespace(device_id="another_car", platform="easee"),
        }
        registry = SimpleNamespace(async_get=sources.get)
        with patch.object(self.flow_module.dr, "async_get_device_and_config_entry_for_domain", return_value=(SimpleNamespace(id="charger"), object())), patch.object(self.flow_module.er, "async_get", return_value=registry):
            self.assertEqual(flow._input_errors(data), {"charger_current_entity": "charger_sensor_mismatch"})
            sources["sensor.car_current"].device_id = "charger"
            self.assertEqual(flow._input_errors(data), {})
            sources["sensor.status"].platform = "template"
            self.assertEqual(flow._input_errors(data), {"status_entity": "charger_sensor_mismatch"})


if __name__ == "__main__":
    unittest.main()
