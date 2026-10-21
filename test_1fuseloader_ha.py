"""Smoke tests against real HA; CI requires Home Assistant 2026.10.1.

The standalone safety suites use fakes. This file deliberately imports the real
Home Assistant classes to catch API/import/selector regressions before rollout.
It never connects to a charger or a running Home Assistant installation.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

HAS_HA = importlib.util.find_spec("homeassistant") is not None
if os.environ.get("ONEFUSE_REQUIRE_HOMEASSISTANT") and not HAS_HA:
    raise RuntimeError("CI must install Home Assistant before running this suite")


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in the CI job")
class RealHomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.config_entries import ConfigEntry

        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.component = importlib.import_module("custom_components.1fuseloader")
        self.const = importlib.import_module("custom_components.1fuseloader.const")
        self.flow_module = importlib.import_module("custom_components.1fuseloader.config_flow")
        self.entry = ConfigEntry(
            version=1, minor_version=1, domain="1fuseloader", title="Test charger",
            data={
                "device_id": "test-device", "controlled_phase": "L1",
                "status_entity": "sensor.test_status", "l1_entity": "sensor.test_l1",
                "l2_entity": "sensor.test_l2", "l3_entity": "sensor.test_l3",
                "charger_current_entity": "sensor.test_current",
            },
            options=dict(self.const.DEFAULT_OPTIONS), source="user", unique_id="test-device",
            discovery_keys=MappingProxyType({}), subentries_data=None,
        )

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    async def test_all_platforms_import_and_entities_construct(self):
        from homeassistant.helpers.device_registry import DeviceEntryType

        controller = self.component.OneFuseLoaderController(self.hass, self.entry)
        for platform, klass, descriptions in (
            ("sensor", "OneFuseSensor", "SENSOR_DESCRIPTIONS"),
            ("number", "OneFuseNumber", "NUMBER_DESCRIPTIONS"),
            ("switch", "OneFuseSwitch", "SWITCH_DESCRIPTIONS"),
            ("button", "OneFuseButton", "BUTTON_DESCRIPTIONS"),
        ):
            module = importlib.import_module(f"custom_components.1fuseloader.{platform}")
            entity_descriptions = list(getattr(module, descriptions))
            if platform == "sensor":
                entity_descriptions.extend(module.BMW_SENSOR_DESCRIPTIONS)
            for description in entity_descriptions:
                entity = getattr(module, klass)(controller, self.entry, description)
                self.assertIsNotNone(entity.unique_id)
                self.assertIsNotNone(entity.device_info)
                self.assertEqual(entity.device_info["entry_type"], DeviceEntryType.SERVICE)
                self.assertEqual(entity.device_info["identifiers"], {("1fuseloader", self.entry.entry_id)})
                self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_{description.key}")

    async def test_setup_form_uses_current_ha_selectors(self):
        flow = self.flow_module.OneFuseLoaderConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        result = await flow.async_step_user()
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "user")
        self.assertIsNotNone(result["data_schema"])

    async def test_options_form_uses_current_config_entry_property(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        result = await flow.async_step_init()
        self.assertEqual(result["type"], "form")
        result = await flow.async_step_init({"min_charge_amps": 16, "max_charge_amps": 6})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"].get("base"), "invalid_settings")

    async def test_defaults_are_observation_only(self):
        settings = self.component._settings_from_entry(self.entry)
        self.assertFalse(settings.active_control)
        self.assertTrue(settings.dry_run)
        self.assertFalse(settings.force_min_when_available_too_low)
        self.assertTrue(settings.fast_recovery)
        self.assertFalse(settings.nominal_import_only_fallback)
        self.assertFalse(settings.bounded_nominal_shedding)

    async def test_long_incident_reason_is_valid_ha_state_with_complete_attribute(self):
        module = importlib.import_module("custom_components.1fuseloader.sensor")
        logic = importlib.import_module("custom_components.1fuseloader.logic")
        controller = self.component.OneFuseLoaderController(self.hass, self.entry)
        controller.last_decision = controller.regulator.evaluate(logic.RegulatorInput(
            0, "charging", 14, charger_current=8, data_valid=False, data_error="evidence " * 60))
        description = next(item for item in module.SENSOR_DESCRIPTIONS if item.key == "reason")
        entity = module.OneFuseSensor(controller, self.entry, description)
        self.assertLessEqual(len(entity.native_value), 255)
        self.assertGreater(len(entity.extra_state_attributes["reason"]), 255)
        self.hass.states.async_set("sensor.test_reason", entity.native_value, entity.extra_state_attributes)
        self.assertEqual(self.hass.states.get("sensor.test_reason").state, entity.native_value)

    async def test_import_only_option_requires_explicit_boolean_and_is_translated(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        result = await flow.async_step_init()
        fields = result["data_schema"].schema
        marker = next(key for key in fields if str(key.schema) == "nominal_import_only_fallback")
        self.assertFalse(marker.default())
        self.assertIs(fields[marker], bool)
        rejected = await flow.async_step_init({"nominal_import_only_fallback": "true"})
        self.assertEqual(rejected["errors"].get("base"), "invalid_settings")
        accepted = await flow.async_step_init({"nominal_import_only_fallback": True})
        self.assertTrue(accepted["data"]["nominal_import_only_fallback"])
        for name in ("strings.json", "translations/en.json", "translations/sv.json"):
            content = json.loads((Path(__file__).parent / "custom_components" / "1fuseloader" / name).read_text(encoding="utf-8"))
            self.assertTrue(content["options"]["step"]["init"]["data"]["nominal_import_only_fallback"])

    async def test_bounded_response_trial_is_explicit_validated_and_translated(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        result = await flow.async_step_init()
        fields = result["data_schema"].schema
        marker = next(key for key in fields if str(key.schema) == "bounded_nominal_shedding")
        self.assertFalse(marker.default())
        self.assertIs(fields[marker], bool)
        rejected = await flow.async_step_init({"bounded_nominal_shedding": "true"})
        self.assertEqual(rejected["errors"].get("base"), "invalid_settings")
        accepted = await flow.async_step_init({"bounded_nominal_shedding": True, "overload_enabled": True})
        self.assertTrue(accepted["data"]["bounded_nominal_shedding"])
        for name in ("strings.json", "translations/en.json", "translations/sv.json"):
            content = json.loads((Path(__file__).parent / "custom_components" / "1fuseloader" / name).read_text(encoding="utf-8"))
            self.assertTrue(content["options"]["step"]["init"]["data"]["bounded_nominal_shedding"])

    async def test_fast_recovery_options_field_is_a_validated_boolean(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        result = await flow.async_step_init()
        fields = result["data_schema"].schema
        marker = next(key for key in fields if str(key.schema) == "fast_recovery")
        self.assertTrue(marker.default())
        self.assertIs(fields[marker], bool)
        invalid = await flow.async_step_init({"fast_recovery": "false"})
        self.assertEqual(invalid["type"], "form")
        self.assertEqual(invalid["errors"].get("base"), "invalid_settings")
        accepted = await flow.async_step_init({"fast_recovery": False})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertIs(accepted["data"]["fast_recovery"], False)

    async def test_fast_recovery_switch_keeps_stable_identity_and_routes_boolean_changes(self):
        module = importlib.import_module("custom_components.1fuseloader.switch")
        description = next(item for item in module.SWITCH_DESCRIPTIONS if item.key == "fast_recovery")
        self.assertEqual(description.name, "Fast Headroom Recovery")
        controller = SimpleNamespace(async_set_switch=AsyncMock())
        entity = module.OneFuseSwitch(controller, self.entry, description)
        self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_fast_recovery")
        self.assertTrue(entity.is_on)
        await entity.async_turn_off()
        controller.async_set_switch.assert_awaited_once_with("fast_recovery", False)
        controller.async_set_switch.reset_mock()
        await entity.async_turn_on()
        controller.async_set_switch.assert_awaited_once_with("fast_recovery", True)

    async def test_fast_recovery_option_is_named_in_all_shipped_languages(self):
        component = Path(__file__).parent / "custom_components" / "1fuseloader"
        for filename in ("strings.json", "translations/en.json", "translations/sv.json"):
            with self.subTest(filename=filename):
                content = json.loads((component / filename).read_text(encoding="utf-8"))
                label = content["options"]["step"]["init"]["data"]["fast_recovery"]
                self.assertIsInstance(label, str)
                self.assertTrue(label.strip())

    async def test_start_timer_and_stop_use_the_ha_event_loop_without_writes(self):
        from homeassistant.core import HassJob, HassJobType

        adapter = SimpleNamespace(
            control_block_reason=None, telemetry_block_reason=None,
            async_initialize=AsyncMock(), async_execute=AsyncMock(),
        )
        for entity_id, value, attributes in (
            ("sensor.test_status", "charging", {}),
            ("sensor.test_l1", "18", {"unit_of_measurement": "A"}),
            ("sensor.test_current", "16", {"unit_of_measurement": "A"}),
        ):
            self.hass.states.async_set(entity_id, value, attributes)
        controller = self.component.OneFuseLoaderController(self.hass, self.entry, adapter)
        scheduled = {}
        real_track = self.component.async_track_time_interval

        def capture_timer(hass, action, interval):
            scheduled["action"] = action
            return real_track(hass, action, interval)

        with patch.object(self.component, "async_track_time_interval", side_effect=capture_timer):
            await controller.async_start()
        try:
            # A plain synchronous lambda would be classified as an executor job
            # and call loop-only APIs from a worker thread when the timer fires.
            job = HassJob(scheduled["action"])
            self.assertEqual(job.job_type, HassJobType.Coroutinefunction)
            task = self.hass.async_run_hass_job(job, datetime.now(timezone.utc))
            if task is not None:
                await task
            self.assertEqual(controller.last_decision.current_p1, 16)
            self.assertFalse(controller.last_decision.should_call_service)
            # The new report is processed through HA's event bus immediately,
            # without waiting for the 5-second watchdog timer.
            self.hass.states.async_set("sensor.test_l1", "31", {"unit_of_measurement": "A"})
            await self.hass.async_block_till_done()
            self.assertEqual(controller.last_decision.current_p1, 8)
            adapter.async_execute.assert_not_awaited()
        finally:
            await controller.async_stop()
        self.assertIsNone(controller.unsub_timer)
        self.assertIsNone(controller.unsub_states)
        self.hass.states.async_set("sensor.test_l1", "18", {"unit_of_measurement": "A"})
        await self.hass.async_block_till_done()
        self.assertEqual(controller.last_decision.current_p1, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
