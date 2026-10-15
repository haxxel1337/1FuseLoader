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
    "homeassistant.helpers.typing",
)}
stubs["homeassistant.config_entries"].ConfigEntry = object
stubs["homeassistant.const"].Platform = str
stubs["homeassistant.core"].HomeAssistant = object
stubs["homeassistant.core"].callback = lambda function: function
stubs["homeassistant.helpers.typing"].ConfigType = dict
stubs["homeassistant.helpers.event"].async_track_time_interval = lambda *args: lambda: None
stubs["homeassistant.helpers.event"].async_track_state_change_event = lambda *args: lambda: None
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
            title="Test", data={"device_id": "test", "controlled_phase": "L1",
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


if __name__ == "__main__":
    unittest.main()
