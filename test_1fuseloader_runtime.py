"""Safety and controller regressions, runnable without a Home Assistant install."""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

COMPONENT = Path(__file__).parent / "custom_components" / "1fuseloader"
PACKAGE = "onefuse_runtime_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load(name, filename):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", COMPONENT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


const = load("const", "const.py")
logic = load("logic", "logic.py")
load("easee_adapter", "easee_adapter.py")

# Isolate HA imports to this module load; never replace a real HA runtime for
# the rest of the test suite.
stubs = {name: ModuleType(name) for name in (
    "homeassistant", "homeassistant.config_entries", "homeassistant.const",
    "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.event",
    "homeassistant.helpers.typing", "homeassistant.helpers.storage",
)}
stubs["homeassistant.config_entries"].ConfigEntry = object
stubs["homeassistant.const"].Platform = str
stubs["homeassistant.core"].HomeAssistant = object
stubs["homeassistant.core"].callback = lambda function: function
stubs["homeassistant.helpers.typing"].ConfigType = dict
stubs["homeassistant.helpers.event"].async_track_time_interval = lambda *args: lambda: None
stubs["homeassistant.helpers.event"].async_track_state_change_event = lambda *args: lambda: None
stubs["homeassistant.helpers.event"].async_call_later = lambda *args: lambda: None


class FakeStore:
    def __init__(self, *args):
        self.data = None
        self.failure = False

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        if self.failure:
            raise OSError("storage unavailable")
        self.data = data


stubs["homeassistant.helpers.storage"].Store = FakeStore
stubs["homeassistant.helpers"].entity_registry = SimpleNamespace()
with patch.dict(sys.modules, stubs):
    runtime = load("runtime", "__init__.py")


class SafetyTests(unittest.TestCase):
    def regulator(self, **settings):
        return logic.OneFuseRegulator(logic.RegulatorSettings(active_control=True, dry_run=False, **settings))

    def evaluate(self, regulator, now, status="charging", grid=16, current=8, **kwargs):
        return regulator.evaluate(logic.RegulatorInput(now, status, grid, charger_current=current, **kwargs))

    def test_insufficient_headroom_never_forces_minimum(self):
        decision = self.evaluate(self.regulator(), 0, grid=22, current=0)
        self.assertEqual(decision.current_p1, 0)

    def test_soft_guard_never_increases_an_already_zero_limit(self):
        regulator = self.regulator()
        self.evaluate(regulator, 0, grid=29, current=6)
        decision = self.evaluate(regulator, 21, grid=29, current=6)
        self.assertEqual(decision.mode, "soft_limit")
        self.assertEqual(decision.current_p1, 0)

    def test_disabled_soft_guard_preserves_normal_headroom(self):
        regulator = self.regulator(enable_soft_guard=False)
        self.evaluate(regulator, 0, grid=29, current=16)
        decision = self.evaluate(regulator, 21, grid=29, current=16)
        self.assertEqual((decision.mode, decision.current_p1), ("normal", 10))

    def test_missing_invalid_or_inconsistent_telemetry_requests_zero(self):
        cases = [
            {"grid": "unavailable"}, {"grid": -1}, {"grid": float("nan")},
            {"current": "unavailable"}, {"current": float("inf")}, {"current": -1},
            {"current": 30}, {"status": "offline"}, {"status": "unknown"},
            {"data_valid": False, "data_error": "stale"},
        ]
        for case in cases:
            with self.subTest(case=case):
                decision = self.evaluate(self.regulator(), 0, **case)
                self.assertEqual((decision.mode, decision.command, decision.current_p1), ("unavailable", "set_limit", 0))
                self.assertTrue(decision.should_call_service)

    def test_planning_does_not_claim_submission_or_pause_ownership(self):
        regulator = self.regulator()
        decision = self.evaluate(regulator, 0)
        self.assertIsNone(regulator.last_target)
        self.assertIsNone(regulator.last_command)
        regulator.acknowledge(decision, 0)
        self.assertEqual(regulator.last_target, (8, 0, 0, 60))
        self.assertFalse(self.evaluate(regulator, 1).should_call_service)
        regulator = self.regulator()
        self.evaluate(regulator, 0, grid=29, current=6)
        pause = self.evaluate(regulator, 121, grid=29, current=6)
        self.assertEqual(pause.command, "pause")
        self.assertFalse(regulator.paused_by_us)
        regulator.acknowledge(pause, 121)
        self.assertTrue(regulator.paused_by_us)

    def test_manual_pause_is_never_resumed(self):
        regulator = self.regulator()
        first = self.evaluate(regulator, 0, status="paused", grid=10, current=0)
        regulator.acknowledge(first, 0)
        self.assertNotEqual(self.evaluate(regulator, 121, status="paused", grid=10, current=0).command, "resume")

    def test_high_house_load_does_not_take_ownership_of_a_manual_awaiting_start(self):
        regulator = self.regulator()
        self.evaluate(regulator, 0, status="awaiting_start", grid=29, current=0)
        decision = self.evaluate(regulator, 121, status="awaiting_start", grid=29, current=0)
        self.assertEqual((decision.command, decision.current_p1), ("set_limit", 0))
        regulator.acknowledge(decision, 121)
        self.assertFalse(regulator.paused_by_us)

    def test_unobserved_pause_cannot_claim_a_later_manual_pause(self):
        regulator = self.regulator()
        self.evaluate(regulator, 0, grid=29, current=6)
        regulator.acknowledge(self.evaluate(regulator, 121, grid=29, current=6), 121)
        self.assertTrue(regulator.paused_by_us)
        self.evaluate(regulator, 160, status="awaiting_start", grid=10, current=0)
        self.assertFalse(regulator.paused_by_us)

    def test_data_gap_resets_continuous_resume_observation(self):
        regulator = self.regulator()
        regulator.paused_by_us = True
        self.evaluate(regulator, 0, status="awaiting_start", grid=10, current=0)
        self.evaluate(regulator, 60, status="awaiting_start", grid="unavailable", current=0)
        decision = self.evaluate(regulator, 121, status="awaiting_start", grid=10, current=0)
        self.assertNotEqual(decision.command, "resume")
        self.assertEqual(regulator.below_resume_since, 121)

    def test_resume_needs_fresh_positive_accepted_limit(self):
        regulator = self.regulator()
        regulator.paused_by_us = True
        first = self.evaluate(regulator, 0, status="awaiting_start", grid=10, current=0)
        regulator.acknowledge(first, 0)
        cap = self.evaluate(regulator, 121, status="awaiting_start", grid=10, current=0)
        self.assertEqual(cap.command, "set_limit")
        self.assertGreaterEqual(cap.current_p1, 6)
        regulator.acknowledge(cap, 121)
        resume = self.evaluate(regulator, 126, status="awaiting_start", grid=10, current=0)
        self.assertEqual(resume.command, "resume")
        self.assertEqual(resume.current_p1, cap.current_p1)
        self.assertTrue(regulator.paused_by_us)
        regulator.acknowledge(resume, 126)
        self.assertFalse(regulator.paused_by_us)

    def test_low_grid_threshold_alone_is_not_enough_headroom_to_resume(self):
        regulator = self.regulator()
        regulator.paused_by_us = True
        self.evaluate(regulator, 0, status="awaiting_start", grid=20, current=0)
        decision = self.evaluate(regulator, 121, status="awaiting_start", grid=20, current=0)
        self.assertEqual((decision.command, decision.current_p1), ("set_limit", 0))

    def test_disconnect_releases_pause_ownership(self):
        regulator = self.regulator()
        regulator.paused_by_us = True
        self.evaluate(regulator, 0, status="disconnected", grid=10, current=0)
        self.assertFalse(regulator.paused_by_us)

    def test_emergency_reduction_bypasses_action_cooldown(self):
        regulator = self.regulator()
        regulator.acknowledge(self.evaluate(regulator, 0), 0)
        decision = self.evaluate(regulator, 1, grid="unavailable")
        self.assertEqual(decision.current_p1, 0)
        self.assertTrue(decision.should_call_service)


class FakeAdapter:
    control_block_reason = None
    telemetry_block_reason = None
    overload_block_reason = None

    def __init__(self):
        self.calls = []
        self.failure = None
        self.gate = None
        self.entered = asyncio.Event()

    async def async_initialize(self):
        return None

    async def async_execute(self, decision):
        self.calls.append(decision)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise self.failure


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        now = datetime.now(timezone.utc)
        self.states = {
            "sensor.grid": SimpleNamespace(state="16", last_reported=now, attributes={"unit_of_measurement": "A"}),
            # Stable Easee entities can be hours old while the stream is healthy.
            "sensor.status": SimpleNamespace(state="charging", last_reported=now - timedelta(hours=2), attributes={}),
            "sensor.car": SimpleNamespace(state="8", last_reported=now - timedelta(hours=2), attributes={"unit_of_measurement": "A"}),
        }
        self.entry = SimpleNamespace(
            title="Test", entry_id="test-entry", data={"device_id": "test", "controlled_phase": "L1",
                "l1_entity": "sensor.grid", "status_entity": "sensor.status",
                "charger_current_entity": "sensor.car"},
            options={"active_control": True, "dry_run": False},
        )
        self.hass = SimpleNamespace(loop=asyncio.get_running_loop(), states=SimpleNamespace(get=self.states.get), async_create_task=asyncio.create_task)
        self.hass.config_entries = SimpleNamespace(async_update_entry=self.update_entry)
        self.adapter = FakeAdapter()
        self.controller = runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)

    def update_entry(self, entry, **kwargs):
        entry.options = kwargs["options"]

    async def test_accepted_submission_is_recorded_as_unverified(self):
        await self.controller.async_tick()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.controller.last_service_result, "submitted_unverified")
        self.assertEqual(self.controller.regulator.last_command, "set_limit")

    async def test_failed_submission_never_commits_target(self):
        self.adapter.failure = RuntimeError("private detail which must not escape")
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_service_result, "error")
        self.assertEqual(self.controller.last_service_error, "RuntimeError")
        self.assertIsNone(self.controller.regulator.last_target)
        self.assertNotIn("private detail", self.controller.last_decision.reason)

    async def test_no_action_preserves_actual_failed_attempt_and_error(self):
        self.adapter.failure = RuntimeError("private detail which must not escape")
        await self.controller.async_tick()
        failed = dict(self.controller.last_command_attempt)
        self.assertEqual(failed["result"], "error")
        self.assertEqual(failed["error"], "RuntimeError")
        self.assertIsNotNone(datetime.fromisoformat(failed["started_at_utc"]).utcoffset())
        self.entry.options["active_control"] = False
        await self.controller.async_reconfigure()
        self.assertEqual(self.controller.last_service_result, "not_sent")
        self.assertEqual(self.controller.last_service_error, "RuntimeError")
        self.assertEqual(self.controller.last_command_attempt, failed)
        self.assertIsNone(self.controller.last_accepted_limit)
        self.assertNotIn("private detail", str(self.controller.diagnostic_attributes()))

    async def test_failed_reduction_does_not_replace_last_accepted_limit(self):
        await self.controller.async_tick()
        accepted = dict(self.controller.last_accepted_limit)
        self.assertEqual(accepted["target_p1"], 8)
        self.states["sensor.grid"].state = "25"
        self.adapter.failure = OSError("private remote endpoint")
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_command_attempt["target_p1"], 6)
        self.assertEqual(self.controller.last_command_attempt["result"], "error")
        self.assertEqual(self.controller.last_accepted_limit, accepted)
        self.assertEqual(self.controller.last_accepted_command, accepted)
        self.assertEqual(self.controller.regulator.last_target[0], 8)

    async def test_dry_run_and_blocked_decisions_are_not_actual_attempts(self):
        self.adapter.control_block_reason = "unsupported_easee_version"
        await self.controller.async_tick()
        self.assertIsNone(self.controller.last_command_attempt)
        self.adapter.control_block_reason = None
        self.entry.options["dry_run"] = True
        await self.controller.async_reconfigure()
        self.assertIsNone(self.controller.last_command_attempt)
        self.assertIsNone(self.controller.last_accepted_command)

    def cached_registry(self, entries):
        registry = object()
        def selected(actual_registry, device_id):
            self.assertIs(actual_registry, registry)
            self.assertEqual(device_id, "test")
            return entries
        return SimpleNamespace(async_get=lambda hass: registry, async_entries_for_device=selected)

    def circuit_source(self, **overrides):
        attributes = {
            "unit_of_measurement": "A",
            "state_dynamicCircuitCurrentP1": 8,
            "state_dynamicCircuitCurrentP2": 16,
            "state_dynamicCircuitCurrentP3": 0,
            "site.siteKey": "PRIVATE_SENTINEL",
        }
        attributes.update(overrides)
        self.states["sensor.circuit"] = SimpleNamespace(
            state="16", attributes=attributes,
            last_reported=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        return SimpleNamespace(platform="easee", disabled_by=None, entity_id="sensor.circuit")

    async def test_readback_uses_selected_device_explicit_phases_not_native_max(self):
        circuit = self.circuit_source()
        with patch.object(runtime, "er", self.cached_registry([circuit])):
            await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertEqual(diagnostics["reported_circuit_p1"], 8)
        self.assertEqual(diagnostics["reported_circuit_p2"], 16)
        self.assertEqual(diagnostics["reported_circuit_p3"], 0)
        self.assertFalse(diagnostics["reported_cap_matches_last_accepted_limit"])
        self.assertFalse(diagnostics["reported_cap_report_after_acceptance"])
        self.assertEqual(diagnostics["readback_scope"], "cached_circuit_cap_only")
        self.assertFalse(diagnostics["physical_current_verified"])
        self.assertFalse(diagnostics["device_ttl_verified"])
        self.assertNotIn("PRIVATE_SENTINEL", str(diagnostics))
        self.assertNotIn("site.siteKey", str(diagnostics))

    async def test_matching_old_report_is_not_physical_or_ttl_confirmation(self):
        circuit = self.circuit_source(state_dynamicCircuitCurrentP2=0)
        with patch.object(runtime, "er", self.cached_registry([circuit])):
            await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertTrue(diagnostics["reported_cap_matches_last_accepted_limit"])
        self.assertFalse(diagnostics["reported_cap_report_after_acceptance"])
        self.assertGreater((datetime.now(timezone.utc) - datetime.fromisoformat(
            diagnostics["reported_circuit_reported_at_utc"])).total_seconds(), 3500)
        self.assertFalse(diagnostics["physical_current_verified"])
        self.assertFalse(diagnostics["device_ttl_verified"])

    async def test_matching_new_report_and_idle_tick_keep_submission_unverified(self):
        circuit = self.circuit_source(state_dynamicCircuitCurrentP2=0)
        with patch.object(runtime, "er", self.cached_registry([circuit])):
            await self.controller.async_tick()
            accepted = dict(self.controller.last_accepted_limit)
            self.states[circuit.entity_id].last_reported = datetime.now(timezone.utc)
            await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertEqual(self.controller.last_service_result, "not_sent")
        self.assertEqual(self.controller.last_command_attempt, accepted)
        self.assertEqual(self.controller.last_accepted_limit, accepted)
        self.assertTrue(diagnostics["reported_cap_matches_last_accepted_limit"])
        self.assertTrue(diagnostics["reported_cap_report_after_acceptance"])
        self.assertEqual(accepted["result"], "submitted_unverified")
        self.assertFalse(diagnostics["physical_current_verified"])
        self.assertFalse(diagnostics["device_ttl_verified"])

    async def test_restored_ambiguous_or_invalid_readback_is_unavailable(self):
        for overrides in ({"restored": True}, {"state_dynamicCircuitCurrentP1": True},
                          {"state_dynamicCircuitCurrentP1": float("nan")}, {"unit_of_measurement": "W"}):
            with self.subTest(overrides=overrides):
                circuit = self.circuit_source(**overrides)
                with patch.object(runtime, "er", self.cached_registry([circuit])):
                    await self.controller.async_tick()
                self.assertIsNone(self.controller.source_diagnostics["reported_circuit_p1"])
                self.assertFalse(self.controller.source_diagnostics["reported_circuit_available"])
                self.assertIsNone(self.controller.diagnostic_attributes()["reported_cap_matches_last_accepted_limit"])
        circuit = self.circuit_source()
        duplicate = SimpleNamespace(platform="easee", disabled_by=None, entity_id="sensor.other_circuit")
        self.states[duplicate.entity_id] = self.states[circuit.entity_id]
        with patch.object(runtime, "er", self.cached_registry([circuit, duplicate])):
            await self.controller.async_tick()
        self.assertFalse(self.controller.source_diagnostics["reported_circuit_available"])
        self.assertEqual(self.controller.source_diagnostics["reported_circuit_source"], "ambiguous_selected_device_sources")

    async def test_fresh_pulse_does_not_relabel_old_current_as_fresh_measurement(self):
        pulse = datetime.now(timezone.utc)
        self.states["binary_sensor.online"] = SimpleNamespace(
            state="on", last_reported=pulse,
            attributes={"state_latestPulse": pulse, "site.siteKey": "PRIVATE_SENTINEL"},
        )
        online = SimpleNamespace(platform="easee", disabled_by=None, entity_id="binary_sensor.online")
        with patch.object(runtime, "er", self.cached_registry([online])):
            await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertEqual(diagnostics["source_health"], "healthy")
        self.assertLess((datetime.now(timezone.utc) - datetime.fromisoformat(
            diagnostics["easee_stream_pulse_utc"])).total_seconds(), 2)
        self.assertGreater((datetime.now(timezone.utc) - datetime.fromisoformat(
            diagnostics["charger_current_reported_at_utc"])).total_seconds(), 7100)
        self.assertEqual(diagnostics["report_time_scope"], "ha_receipt_not_physical_acquisition")
        self.assertFalse(diagnostics["physical_current_verified"])
        self.assertFalse(any(key.endswith("_age_seconds") for key in diagnostics))

    async def test_stale_grid_is_unhealthy_despite_healthy_easee_stream(self):
        self.states["sensor.grid"].last_reported -= timedelta(minutes=3)
        await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertEqual(diagnostics["source_health"], "unhealthy")
        self.assertEqual(diagnostics["easee_source_health"], "healthy")
        self.assertIn("L1 telemetry is stale", diagnostics["source_health_reason"])
        self.assertTrue(diagnostics["source_errors"])
        self.assertEqual(self.controller.last_decision.mode, "unavailable")

    async def test_invalid_required_values_are_unhealthy_with_healthy_easee_stream(self):
        for entity_id, value, label in (
            ("sensor.grid", "unavailable", "L1"),
            ("sensor.grid", "nan", "L1"),
            ("sensor.grid", "-1", "L1"),
            ("sensor.car", "unavailable", "Charger current"),
            ("sensor.status", "unknown", "Charger status"),
        ):
            with self.subTest(entity_id=entity_id, value=value):
                previous = self.states[entity_id].state
                self.states[entity_id].state = value
                await self.controller.async_tick()
                diagnostics = self.controller.diagnostic_attributes()
                self.assertEqual(diagnostics["source_health"], "unhealthy")
                self.assertEqual(diagnostics["easee_source_health"], "healthy")
                self.assertIn(label, diagnostics["source_health_reason"])
                self.assertEqual(self.controller.last_decision.mode, "unavailable")
                self.states[entity_id].state = previous

    async def test_policy_headroom_wait_is_not_a_source_transport_fault(self):
        self.states["sensor.grid"].state = "22"
        self.states["sensor.car"].state = "0"
        self.states["sensor.status"].state = "awaiting_start"
        await self.controller.async_tick()
        diagnostics = self.controller.diagnostic_attributes()
        self.assertEqual(diagnostics["source_health"], "healthy")
        self.assertEqual(diagnostics["easee_source_health"], "healthy")
        self.assertFalse(diagnostics["source_errors"])
        self.assertEqual(self.controller.last_decision.current_p1, 0)

    async def test_stale_grid_requests_zero(self):
        self.states["sensor.grid"].last_reported -= timedelta(minutes=3)
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[0].current_p1, 0)
        self.assertEqual(self.controller.last_decision.mode, "unavailable")

    async def test_restored_critical_entities_request_zero(self):
        for entity_id in ("sensor.grid", "sensor.status", "sensor.car"):
            with self.subTest(entity_id=entity_id):
                self.states[entity_id].attributes["restored"] = True
                self.controller.regulator.reset()
                self.adapter.calls.clear()
                await self.controller.async_tick()
                self.assertEqual(self.adapter.calls[0].current_p1, 0)
                self.states[entity_id].attributes.pop("restored")

    async def test_runtime_unit_changes_request_zero(self):
        for entity_id in ("sensor.grid", "sensor.car"):
            with self.subTest(entity_id=entity_id):
                self.states[entity_id].attributes["unit_of_measurement"] = "W"
                self.controller.regulator.reset()
                self.adapter.calls.clear()
                await self.controller.async_tick()
                self.assertEqual(self.adapter.calls[0].current_p1, 0)
                self.states[entity_id].attributes["unit_of_measurement"] = "A"

    async def test_unhealthy_easee_stream_requests_zero(self):
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[0].current_p1, 0)

    async def test_unknown_adapter_capability_blocks_actual_writes(self):
        self.adapter.control_block_reason = "unsupported_easee_version"
        await self.controller.async_tick()
        self.assertFalse(self.adapter.calls)
        self.assertEqual(self.controller.last_service_result, "blocked")
        self.assertEqual(self.controller.last_decision.mode, "control_blocked")

    async def test_dry_run_does_not_submit_or_acknowledge(self):
        self.entry.options["dry_run"] = True
        await self.controller.async_reconfigure()
        self.assertFalse(self.adapter.calls)
        self.assertIsNone(self.controller.regulator.last_command)
        self.assertEqual(self.controller.last_service_result, "dry_run")

    async def test_ticks_coalesce_and_stop_waits_for_inflight_request(self):
        self.adapter.gate = asyncio.Event()
        first = asyncio.create_task(self.controller.async_tick())
        await self.adapter.entered.wait()
        await asyncio.gather(*(self.controller.async_tick() for _ in range(10)))
        stop = asyncio.create_task(self.controller.async_stop())
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        self.adapter.gate.set()
        await asyncio.gather(first, stop)
        await self.controller.async_tick()
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_disable_serializes_with_inflight_action(self):
        self.adapter.gate = asyncio.Event()
        first = asyncio.create_task(self.controller.async_tick())
        await self.adapter.entered.wait()
        disable = asyncio.create_task(self.controller.async_set_option("active_control", False))
        await asyncio.sleep(0)
        self.assertFalse(disable.done())
        self.adapter.gate.set()
        await asyncio.gather(first, disable)
        await self.controller.async_tick()
        self.assertFalse(self.controller.settings.active_control)
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_source_changes_coalesce_and_apply_newest_boost_after_inflight_call(self):
        self.adapter.gate = asyncio.Event()
        first = asyncio.create_task(self.controller.async_tick())
        await self.adapter.entered.wait()
        self.states["sensor.grid"].state = "26"
        for _ in range(20):
            self.controller._handle_source_change(None)
        refresh = self.controller._refresh_task
        await asyncio.sleep(0)
        self.assertEqual(len(self.adapter.calls), 1)
        self.adapter.gate.set()
        await first
        await refresh
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.calls[-1].current_p1, 6)
        self.assertIsNone(self.controller._refresh_task)

    async def test_disable_invalidates_a_waiting_source_refresh(self):
        self.adapter.gate = asyncio.Event()
        first = asyncio.create_task(self.controller.async_tick())
        await self.adapter.entered.wait()
        self.controller._handle_source_change(None)
        refresh = self.controller._refresh_task
        await asyncio.sleep(0)
        disable = asyncio.create_task(self.controller.async_set_option("active_control", False))
        await asyncio.sleep(0)
        self.adapter.gate.set()
        await asyncio.gather(first, refresh, disable)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertFalse(self.controller.settings.active_control)

    async def test_bmw_context_failure_does_not_change_protective_decision(self):
        class BrokenBMW:
            def snapshot(self):
                raise RuntimeError("source unavailable")

        self.controller._bmw_context = BrokenBMW()
        await self.controller.async_tick()
        self.assertTrue(self.adapter.calls)
        self.assertGreaterEqual(self.adapter.calls[0].current_p1, 6)
        self.assertEqual(self.controller.bmw_context, {})

    async def test_invalid_option_is_rejected_without_mutation(self):
        with self.assertRaises(ValueError):
            await self.controller.async_set_option("buffer_amps", -100)
        self.assertNotIn("buffer_amps", self.entry.options)
        self.assertFalse(self.adapter.calls)

    async def test_listener_failure_does_not_prevent_protective_action(self):
        self.controller.add_listener(lambda: 1 / 0)
        await self.controller.async_tick()
        self.assertEqual(len(self.adapter.calls), 1)

    def overload_controller(self):
        self.entry.options["overload_enabled"] = True
        self.states["sensor.grid"].state = "28"
        self.states["sensor.car"].state = "6"
        self.controller = runtime.OneFuseLoaderController(self.hass, self.entry, self.adapter)
        return self.controller

    async def test_overload_storage_write_failure_forces_zero_before_submission(self):
        controller = self.overload_controller()
        controller._budget_store.store.failure = True
        await controller.async_tick()
        self.assertEqual([decision.current_p1 for decision in self.adapter.calls], [0])
        self.assertEqual(controller.last_decision.mode, "overload_control_blocked")
        self.assertEqual(controller.last_decision.reason, "overload_budget_save_failed")
        self.assertEqual(controller.regulator.last_target[0], 0)
        # A later successful write must not silently clear the storage fault.
        controller._budget_store.store.failure = False
        await controller.async_tick()
        self.assertTrue(all(decision.current_p1 == 0 for decision in self.adapter.calls))
        self.assertEqual(controller._budget_error, "overload_budget_save_failed")

    async def test_unverified_nonzero_offline_fallback_forces_zero(self):
        controller = self.overload_controller()
        self.adapter.overload_block_reason = "offline_fallback_above_zero"
        await controller.async_tick()
        self.assertEqual([decision.current_p1 for decision in self.adapter.calls], [0])
        self.assertIn("offline_fallback_above_zero", controller.last_decision.reason)
        self.assertEqual(controller.regulator.last_target[0], 0)

    async def test_persisted_pending_response_restores_latched_without_new_grace(self):
        controller = self.overload_controller()
        controller._budget_store.clock = lambda: 1030
        controller._budget_store.store.data = {
            "version": 1, "saved_at": 1000,
            "regulator": {"version": 1, "used_seconds": 120,
                          "exposure_active": True, "transition_pending": True,
                          "minimum_held": False, "failure_reason": None},
        }
        await controller.async_start()
        try:
            restored = controller.regulator.export_overload_state()
            self.assertEqual(restored["used_seconds"], 150)
            self.assertEqual(restored["failure_reason"], "interrupted_transition")
            self.assertFalse(restored["transition_pending"])
            self.assertTrue(controller.last_decision.overload_failure_latched)
            self.assertEqual([decision.current_p1 for decision in self.adapter.calls], [0])
        finally:
            await controller.async_stop()

    async def test_reset_internal_state_and_setting_changes_keep_used_budget(self):
        controller = self.overload_controller()
        restored = {"version": 1, "used_seconds": 312,
                    "exposure_active": False, "transition_pending": False,
                    "minimum_held": False, "failure_reason": None}
        self.assertTrue(controller.regulator.import_overload_state(restored))
        await controller.async_reset_state()
        self.assertEqual(controller.regulator.export_overload_state()["used_seconds"], 312)
        await controller.async_set_option("buffer_amps", 2.5)
        self.assertGreaterEqual(controller.regulator.export_overload_state()["used_seconds"], 312)
        await controller.async_set_switch("overload_enabled", False)
        await controller.async_set_switch("overload_enabled", True)
        self.assertGreaterEqual(controller.regulator.export_overload_state()["used_seconds"], 312)

    async def test_invalid_persisted_state_blocks_overload_instead_of_refunding(self):
        controller = self.overload_controller()
        controller._budget_store.clock = lambda: 1030
        controller._budget_store.store.data = {
            "version": 1, "saved_at": 1000,
            "regulator": {"version": 999, "used_seconds": 120,
                          "exposure_active": True, "transition_pending": False,
                          "minimum_held": False, "failure_reason": None},
        }
        await controller.async_start()
        try:
            self.assertEqual(controller._budget_error, "overload_budget_restore_failed")
            self.assertEqual([decision.current_p1 for decision in self.adapter.calls], [0])
            self.assertGreaterEqual(controller.regulator.export_overload_state()["used_seconds"], 1800)
        finally:
            await controller.async_stop()

    async def test_overload_positive_lease_needs_the_full_ninety_second_reserve(self):
        for remaining, target in ((90, 6), (89.9, 0)):
            with self.subTest(remaining=remaining):
                self.adapter.calls.clear()
                controller = self.overload_controller()
                self.assertTrue(controller.regulator.import_overload_state({
                    "version": 1, "used_seconds": 1800 - remaining,
                    "exposure_active": False, "transition_pending": False,
                    "minimum_held": False, "failure_reason": None,
                }))
                await controller.async_tick()
                self.assertEqual([decision.current_p1 for decision in self.adapter.calls], [target])
                self.assertEqual(self.adapter.calls[0].ttl_seconds, 60)

    async def test_restored_minimum_hold_requires_both_post_restart_reports(self):
        controller = self.overload_controller()
        controller._budget_store.clock = lambda: 1030
        controller._budget_store.store.data = {
            "version": 1, "saved_at": 1000,
            "regulator": {"version": 1, "used_seconds": 120,
                          "exposure_active": True, "transition_pending": False,
                          "minimum_held": True, "failure_reason": None},
        }
        await controller.async_start()
        try:
            self.assertEqual(self.adapter.calls[-1].current_p1, 0)
            self.assertEqual(controller.last_decision.mode, "overload_control_blocked")
            floor = controller._overload_restore_receipt_floor
            fresh = datetime.fromtimestamp(floor + 0.1, timezone.utc)
            self.states["sensor.grid"].last_reported = fresh
            await controller.async_tick()
            self.assertEqual(controller.last_decision.current_p1, 0)
            self.states["sensor.car"].last_reported = fresh
            await controller.async_tick()
            self.assertEqual(controller.last_decision.current_p1, 6)
            self.assertGreaterEqual(controller.last_decision.overload_used_seconds, 150)
        finally:
            await controller.async_stop()

    async def test_store_latency_is_subtracted_from_the_response_deadline(self):
        controller = self.overload_controller()
        decision = controller.regulator.evaluate(logic.RegulatorInput(
            0, "charging", 38, charger_current=16,
            grid_report_ts=1000, charger_report_ts=1000,
        ))
        self.assertEqual(decision.overload_transition_remaining_seconds, 10)
        for elapsed, expected_delay in ((2, 8), (12, 0.05)):
            with self.subTest(elapsed=elapsed):
                with patch.object(runtime, "async_call_later", return_value=lambda: None) as schedule:
                    controller._schedule_overload_deadline(decision, elapsed)
                self.assertEqual(schedule.call_args.args[1], expected_delay)


if __name__ == "__main__":
    unittest.main()
