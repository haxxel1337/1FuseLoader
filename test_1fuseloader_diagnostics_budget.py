"""Recorder-safe diagnostic views and durable stop receipts, offline only."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_runtime as fixtures


def encoded_size(value):
    return max(len(json.dumps(value, ensure_ascii=ascii_mode, default=str).encode("utf-8"))
               for ascii_mode in (True, False))


def sensor_module():
    names = ("homeassistant.components.sensor", "homeassistant.config_entries",
             "homeassistant.const", "homeassistant.core", "homeassistant.helpers.entity",
             "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform")
    stubs = {name: ModuleType(name) for name in names}
    stubs[names[0]].SensorEntity = object
    stubs[names[0]].SensorEntityDescription = lambda **values: SimpleNamespace(**values)
    stubs[names[0]].SensorDeviceClass = SimpleNamespace(CURRENT="current", BATTERY="battery")
    stubs[names[0]].SensorStateClass = SimpleNamespace(MEASUREMENT="measurement")
    stubs[names[1]].ConfigEntry = object
    stubs[names[2]].PERCENTAGE = "%"
    stubs[names[2]].UnitOfElectricCurrent = SimpleNamespace(AMPERE="A")
    stubs[names[3]].HomeAssistant = object
    stubs[names[3]].callback = lambda function: function
    stubs[names[4]].DeviceInfo = dict
    stubs[names[5]].DeviceEntryType = SimpleNamespace(SERVICE="service")
    stubs[names[6]].AddEntitiesCallback = object
    with patch.dict(sys.modules, stubs):
        return fixtures.load("sensor_diagnostics_budget", "sensor.py")


class DiagnosticBudgetTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry
    fresh_control_reports = fixtures.ControllerTests.fresh_control_reports

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.fresh_control_reports()
        self.controller._handle_source_change = Mock()
        await self.controller.async_tick()
        self.controller.last_decision = replace(self.controller.last_decision,
            mode="normal_shedding_stop", current_p1=0,
            reason="Latest cause: source_or_prior_control_invalid. " + "ström ⚡ " * 3000)
        self.long_reason = "Latest stop: retained first cause. " + "ström ⚡ " * 3000
        self.commands = [self.command(i) for i in range(32)]
        self.stops = [dict(plan_utc=f"2026-01-01T00:{i:02}:00+00:00",
                           reason=self.long_reason, mode="normal_shedding_stop",
                           grid_l1=26.5, charger_current=16.1, status="charging",
                           overload_used_seconds=2400.0,
                           preceding_positive_limit_receipts=deepcopy(self.commands[-4:]))
                      for i in range(8)]
        self.controller._recent_commands.clear()
        self.controller._recent_commands.extend(deepcopy(self.commands))
        self.controller._recent_stop_contexts.extend(deepcopy(self.stops))
        self.controller.last_stop_context = deepcopy(self.stops[-1])
        self.controller.last_stop_nominal_diagnostics = {"nominal_rejection_code": "source_or_prior_control_invalid"}
        self.trial = dict(overload_trial_id="synthetic-trial", overload_trial_state="active",
            overload_trial_failure_reason=None, overload_trial_used_seconds=15.0,
            overload_trial_remaining_seconds=1785.0, overload_lifetime_used_seconds=2415.0,
            overload_historical_failure_reason="response_timeout", overload_trial_policy=dict(
                fuse_amps=25, minimum_current=7, sustained_ceiling=29, budget_seconds=1800,
                peak_ceiling=38, response_seconds=10, settings={"unused": "x" * 10000},
                source_id=["synthetic-device", "sensor.synthetic"], fingerprint="a" * 64))
        self.controller.regulator.trial_diagnostics = Mock(return_value=deepcopy(self.trial))
        self.controller._delivery.reset()
        for record in self.commands:
            delivery = self.controller._delivery.begin("set_limit", (16, 0, 0), float(record["target_p1"]))
            self.controller._delivery.accepted(delivery, SimpleNamespace(http_status=200), 100.0)
            record["delivery"]["sequence"] = delivery.sequence
        self.controller._recent_commands.clear()
        self.controller._recent_commands.extend(deepcopy(self.commands))

    async def asyncTearDown(self):
        await self.controller.async_stop()

    def command(self, i):
        return dict(started_at_utc=f"2026-01-01T00:{i:02}:00+00:00",
            completed_at_utc=f"2026-01-01T00:{i:02}:01+00:00", command="set_limit",
            target_p1=16 if i % 3 else 7, target_p2=0, target_p3=0, ttl_seconds=60,
            result="submitted_unverified", error=None, reason="renewal " + "rå ⚡ " * 400,
            delivery=dict(sequence=i + 1, state="http_accepted_unverified", http_status=200,
                          correlation_available=False, vendor_accepted=None, delivery_received=False,
                          readback_matches=None, readback_after_submission=False,
                          physical_response_verified=False, device_ttl_verified=False))

    async def test_full_ring_and_active_policy_fit_recorder_budget(self):
        attributes = self.controller.diagnostic_attributes()
        self.assertLess(encoded_size(attributes), 12 * 1024)
        self.assertTrue(attributes["diagnostics_truncated"])
        self.assertIn("full_incident_journal", attributes["diagnostics_scope"])
        self.assertEqual(attributes["recent_command_journal_count"], 32)
        self.assertEqual(attributes["recent_stop_contexts_count"], 8)
        self.assertLessEqual(len(attributes["recent_command_journal"]), 2)
        self.assertLessEqual(len(attributes["recent_stop_contexts"]), 2)
        self.assertTrue(attributes["last_stop_context"]["reason"].startswith("Latest stop:"))
        self.assertEqual(attributes["overload_lifetime_used_seconds"], 2415.0)
        self.assertEqual(attributes["overload_trial_used_seconds"], 15.0)
        self.assertEqual(attributes["overload_trial_state"], "active")
        self.assertNotIn("settings", attributes["overload_trial_policy"])

    async def test_every_sensor_bounds_merged_decision_and_policy_attributes(self):
        module = sensor_module()
        for description in module.SENSOR_DESCRIPTIONS:
            with self.subTest(key=description.key):
                sensor = module.OneFuseSensor(self.controller, self.entry, description)
                attributes = sensor.extra_state_attributes
                self.assertLess(encoded_size(attributes), 12 * 1024)
                if description.key == "source_health":
                    self.assertEqual(attributes["mode"], "normal_shedding_stop")
                    self.assertEqual(attributes["current_p1"], 0)
                    self.assertEqual(attributes["source_health"], "healthy")
                    self.assertTrue(attributes["reason"].startswith("Latest cause:"))
                    self.assertTrue(attributes["last_stop_context"]["reason"].startswith("Latest stop:"))

    async def test_compact_state_does_not_truncate_private_journal_or_change_control(self):
        before = deepcopy(self.controller.regulator.export_overload_state())
        accepted = deepcopy(self.controller.last_accepted_limit)
        calls = list(self.adapter.calls)
        self.controller.diagnostic_attributes()
        self.controller._queue_incident_journal()
        await self.controller._incident_journal_task
        key = "1fuseloader.test-entry.incident_journal"
        payload = self.hass._test_store_disk[key]
        self.assertEqual(payload["commands"], self.commands)
        self.assertEqual(payload["stops"], self.stops)
        self.assertEqual(self.controller.regulator.export_overload_state(), before)
        self.assertEqual(self.controller.last_accepted_limit, accepted)
        self.assertEqual(self.adapter.calls, calls)

    async def test_stop_context_keeps_four_preceding_positive_receipts_after_rollover(self):
        original = deepcopy(self.commands[-4:])
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        self.controller._stop_context_armed = True
        await self.controller.async_tick()
        receipts = self.controller.last_stop_context["preceding_positive_limit_receipts"]
        self.assertEqual(len(receipts), 4)
        self.assertEqual([item["started_at_utc"] for item in receipts],
                         [item["started_at_utc"] for item in original])
        self.assertEqual([item["ttl_seconds"] for item in receipts], [60] * 4)
        self.assertTrue(all(item["delivery"]["state"] == "http_accepted_unverified" for item in receipts))
        before = deepcopy(receipts)
        self.controller._recent_commands.extend(self.command(i) for i in range(32, 64))
        self.assertEqual(self.controller.last_stop_context["preceding_positive_limit_receipts"], before)
        await self.controller._incident_journal_task
        payload = self.hass._test_store_disk["1fuseloader.test-entry.incident_journal"]
        self.assertEqual(payload["stops"][-1]["preceding_positive_limit_receipts"], before)

    async def test_nested_public_view_cannot_mutate_private_receipts(self):
        attributes = self.controller.diagnostic_attributes()
        attributes["last_stop_context"]["preceding_positive_limit_receipts"][0]["target_p1"] = 999
        self.assertNotEqual(self.controller.last_stop_context["preceding_positive_limit_receipts"][0]["target_p1"], 999)

    async def test_unexpected_nested_source_details_cannot_exceed_hard_state_budget(self):
        self.controller.source_diagnostics.update({f"future_detail_{i}": "⚡" * 3000 for i in range(100)})
        self.controller.source_diagnostics["source_errors"] = [{"future_nested_detail": "⚡" * 3000}] * 20
        attributes = self.controller.diagnostic_attributes()
        self.assertLess(encoded_size(attributes), 12 * 1024)
        self.assertTrue(attributes["diagnostics_truncated"])
        self.assertEqual(attributes["source_health"], "healthy")
        self.assertTrue(attributes["last_stop_context"]["reason"].startswith("Latest stop:"))
        self.assertEqual(attributes["overload_lifetime_used_seconds"], 2415.0)

    async def test_stop_receipt_uses_latest_correlated_delivery_evidence(self):
        latest = self.controller._delivery.latest
        latest.vendor_accepted = True
        latest.delivery_received = self.hass.loop.time()
        latest.state = "vendor_accepted"
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        self.controller._stop_context_armed = True
        await self.controller.async_tick()
        receipts = self.controller.last_stop_context["preceding_positive_limit_receipts"]
        self.assertEqual(receipts[-1]["delivery"]["sequence"], latest.sequence)
        self.assertTrue(receipts[-1]["delivery"]["vendor_accepted"])
        self.assertTrue(receipts[-1]["delivery"]["delivery_received"])
        await self.controller._incident_journal_task

    async def test_escaped_control_characters_fit_even_the_final_projection(self):
        attributes = {key: "\x00" * 20000 for key in self.controller.diagnostic_attributes()}
        attributes["last_stop_context"] = {key: "\x00" * 20000 for key in (
            "plan_utc", "reason", "mode", "grid_l1", "charger_current", "status",
            "command_result", "command_error", "overload_failure_reason",
        )}
        view = self.controller.compact_state_attributes(attributes)
        self.assertLessEqual(encoded_size(view), fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)
        self.assertTrue(view["diagnostics_truncated"])
        self.assertIn("reason", view["last_stop_context"])


if __name__ == "__main__":
    unittest.main()
