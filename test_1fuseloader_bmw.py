"""Optional BMW telemetry context never supplies the protective current loop."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


COMPONENT = Path(__file__).parent / "custom_components" / "1fuseloader"
stubs = {name: ModuleType(name) for name in (
    "homeassistant", "homeassistant.helpers", "homeassistant.helpers.entity_registry",
)}
entity_registry = stubs["homeassistant.helpers.entity_registry"]
entity_registry.async_get = lambda hass: hass.registry
entity_registry.async_entries_for_device = lambda registry, device_id: [
    entry for entry in registry if entry.device_id == device_id
]
spec = importlib.util.spec_from_file_location("onefuse_bmw_context_tests", COMPONENT / "bmw_context.py")
bmw = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = bmw
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(bmw)


class BMWContextTests(unittest.TestCase):
    def setUp(self):
        self.states = {}
        self.entries = []
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get), registry=self.entries,
        )
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        self.context = bmw.BMWContext(self.hass, "selected_bmw")

    def source(self, field, value, *, age=10, descriptor=None, device="selected_bmw",
               platform="cardata", disabled=False, attributes=None, entity_id=None):
        descriptor = descriptor or bmw.FIELD_DESCRIPTORS[field][0]
        entity_id = entity_id or f"sensor.renamed_{len(self.entries)}"
        self.entries.append(SimpleNamespace(
            entity_id=entity_id, device_id=device, platform=platform,
            unique_id=f"private_vehicle_identifier_{descriptor}", disabled_by="user" if disabled else None,
        ))
        self.states[entity_id] = SimpleNamespace(state=value, attributes={
            "timestamp": (self.now - timedelta(seconds=age)).isoformat(),
            "unrelated_secret": "must_not_escape", **(attributes or {}),
        })
        return entity_id

    def snapshot(self):
        now = self.now

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        with patch.object(bmw, "datetime", FixedDatetime):
            return self.context.snapshot()

    def test_missing_device_is_optional_and_does_not_access_registry(self):
        context = bmw.BMWContext(SimpleNamespace(), None)
        self.assertEqual(set(context.snapshot()), set(bmw.FIELD_DESCRIPTORS))
        self.assertTrue(all(field["reason"] == "not_configured" for field in context.snapshot().values()))

    def test_registry_mapping_survives_renames_and_exports_no_identifiers(self):
        self.source("ac_current", "16", entity_id="sensor.user_renamed_the_car")
        result = self.snapshot()
        self.assertEqual(result["ac_current"], {
            "value": 16.0, "source_timestamp": "2026-10-15T11:59:50+00:00",
            "age_seconds": 10.0, "available": True, "fresh": True, "reason": "fresh",
        })
        serialized = json.dumps(result)
        for private in ("private_vehicle_identifier", "selected_bmw", "must_not_escape", "user_renamed_the_car"):
            self.assertNotIn(private, serialized)

    def test_other_device_integration_disabled_or_suffix_near_match_is_ignored(self):
        self.source("ac_current", "16", device="another_bmw")
        self.source("ac_current", "16", platform="template")
        self.source("ac_current", "16", disabled=True)
        self.source("ac_current", "16", descriptor=bmw.FIELD_DESCRIPTORS["ac_current"][0] + ".extra")
        self.assertEqual(self.snapshot()["ac_current"]["reason"], "not_found")

    def test_staleness_is_per_field_and_uses_bmw_timestamp(self):
        self.source("ac_current", "16", age=121)
        self.source("state_of_charge", "75", age=121)
        result = self.snapshot()
        self.assertFalse(result["ac_current"]["fresh"])
        self.assertEqual(result["ac_current"]["value"], 16)
        self.assertEqual(result["ac_current"]["age_seconds"], 121)
        self.assertTrue(result["state_of_charge"]["fresh"])

    def test_restore_unknown_and_unavailable_are_not_available(self):
        for value, attrs, reason in (
            ("16", {"restored": True}, "restored"),
            ("unknown", {}, "unavailable"), ("unavailable", {}, "unavailable"),
        ):
            with self.subTest(value=value, attrs=attrs):
                self.entries.clear()
                self.source("ac_current", value, attributes=attrs)
                result = self.snapshot()["ac_current"]
                self.assertFalse(result["available"])
                self.assertIsNone(result["value"])
                self.assertEqual(result["reason"], reason)

    def test_absent_source_time_cannot_use_home_assistant_change_time(self):
        self.source("ac_current", "16", attributes={"timestamp": None})
        self.assertEqual(self.context.snapshot()["ac_current"]["reason"], "missing_source_timestamp")

    def test_timestamp_parser_rejects_offset_free_and_invalid_strings(self):
        for timestamp in (None, "not-a-date", "2026-10-15T12:00:00"):
            with self.subTest(timestamp=timestamp):
                self.assertIsNone(bmw._timestamp(timestamp))
        self.assertEqual(bmw._timestamp("2026-10-15T14:00:00+02:00"), self.now)
        self.assertEqual(bmw._timestamp(self.now), self.now)

    def test_future_clock_values_are_explicitly_unavailable(self):
        self.source("ac_current", "16", age=-60)
        self.assertEqual(self.snapshot()["ac_current"]["reason"], "future_source_timestamp")
        self.assertFalse(self.snapshot()["ac_current"]["available"])

    def test_invalid_numeric_values_are_not_exported(self):
        for value in ("nan", "inf", "-1", "not-a-number"):
            with self.subTest(value=value):
                self.entries.clear()
                self.source("ac_current", value)
                self.assertFalse(self.snapshot()["ac_current"]["available"])
        self.source("state_of_charge", "101")
        self.assertFalse(self.snapshot()["state_of_charge"]["available"])

    def test_phases_are_numeric_and_predicted_soc_is_excluded(self):
        self.source("phase_count", "1-PHASES")
        self.source("state_of_charge", "99", descriptor="vehicle.predicted_soc")
        self.source("state_of_charge", "88", descriptor="vehicle.magic_soc")
        result = self.snapshot()
        self.assertEqual(result["phase_count"]["value"], 1)
        self.assertFalse(result["state_of_charge"]["available"])

    def test_genuine_soc_falls_back_to_fresh_hv_header(self):
        self.source("state_of_charge", "45", age=7200)
        self.source("state_of_charge", "70", descriptor="vehicle.drivetrain.batteryManagement.header")
        self.assertEqual(self.snapshot()["state_of_charge"]["value"], 70)

    def test_disconnected_retained_three_phase_value_is_not_current_session_context(self):
        self.source("phase_count", "3-PHASES")
        self.source("charging_state", "NOCHARGING")
        self.source("plug_state", "DISCONNECTED")
        self.source("ac_current", "0")
        phase = self.snapshot()["phase_count"]
        self.assertEqual(phase["value"], 3)
        self.assertTrue(phase["available"])
        self.assertFalse(phase["fresh"])
        self.assertEqual(phase["reason"], "not_active_charging_session")

    def test_phase_context_needs_fresh_connected_positive_active_charging_descriptors(self):
        cases = (
            ("CHARGINGACTIVE", "CONNECTED", "16", 10, True),
            ("NOCHARGING", "CONNECTED", "16", 10, False),
            ("CHARGINGACTIVE", "DISCONNECTED", "16", 10, False),
            ("CHARGINGACTIVE", "CONNECTED", "0", 10, False),
            ("CHARGINGACTIVE", "CONNECTED", "unknown", 10, False),
            ("CHARGINGACTIVE", "CONNECTED", "16", 121, False),
        )
        for status, plug, current, age, fresh in cases:
            with self.subTest(status=status, plug=plug, current=current, age=age):
                self.entries.clear()
                self.source("phase_count", "1-PHASES")
                self.source("charging_state", status, age=age)
                self.source("plug_state", plug)
                self.source("ac_current", current)
                self.assertEqual(self.snapshot()["phase_count"]["fresh"], fresh)

    def test_phase_without_session_descriptors_remains_visible_but_unqualified(self):
        self.source("phase_count", "3-PHASES")
        phase = self.snapshot()["phase_count"]
        self.assertEqual(phase["value"], 3)
        self.assertFalse(phase["fresh"])
        self.assertEqual(phase["reason"], "not_active_charging_session")


@unittest.skipUnless(importlib.util.find_spec("homeassistant"), "requires Home Assistant runtime")
class BMWConfigFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("custom_components.1fuseloader.config_flow")
        self.flow = self.module.OneFuseLoaderConfigFlow()
        self.flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={"unit_of_measurement": "A"})))
        self.data = {"device_id": "charger", "controlled_phase": "L1", "status_entity": "sensor.status",
                     "l1_entity": "sensor.grid1", "l2_entity": "sensor.grid2", "l3_entity": "sensor.grid3",
                     "charger_current_entity": "sensor.charger"}

    def test_bmw_selector_is_optional_and_filters_cardata(self):
        schema = self.module._config_schema()
        fields = {marker.schema: (marker, field) for marker, field in schema.schema.items()}
        marker, selector = fields["bmw_device_id"]
        self.assertIsInstance(marker, self.module.vol.Optional)
        self.assertEqual(selector.config["integration"], "cardata")

    def test_bmw_device_must_belong_to_cardata_when_selected(self):
        registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(device_id="charger", platform="easee"))
        with patch.object(self.module.er, "async_get", return_value=registry), patch.object(
            self.module.dr, "async_get_device_and_config_entry_for_domain",
            side_effect=lambda hass, device_id, domain: (SimpleNamespace(id="charger"), object()) if domain == "easee" else (None, None),
        ):
            self.assertEqual(self.flow._input_errors(self.data), {})
            self.data["bmw_device_id"] = "wrong_device"
            self.assertEqual(self.flow._input_errors(self.data), {"bmw_device_id": "device_not_cardata"})


if __name__ == "__main__":
    unittest.main()
