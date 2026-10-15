"""Offline contract tests; these never connect to HA or Easee."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ADAPTER_PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "easee_adapter.py"
SPEC = importlib.util.spec_from_file_location("onefuse_easee_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


def decision(command="set_limit", amps=12, ttl=60):
    return SimpleNamespace(
        command=command, current_p1=amps, current_p2=0, current_p3=0, ttl_seconds=ttl
    )


class Response:
    def __init__(self, status=202):
        self.status = status
        self.release = Mock()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = Response()
        self.circuit = SimpleNamespace(set_dynamic_current=AsyncMock(return_value=self.response))
        self.charger = SimpleNamespace(
            id="selected-charger",
            circuit=self.circuit,
            pause=AsyncMock(return_value=self.response),
            resume=AsyncMock(return_value=self.response),
        )
        self.product = SimpleNamespace(
            product=self.charger,
            state={
                "signalRConnected": True,
                "isOnline": True,
                "latestPulse": datetime.now(timezone.utc),
            },
        )
        self.controller = SimpleNamespace(
            get_chargers=Mock(return_value=[self.charger]), chargers_data=[self.product]
        )
        self.device = SimpleNamespace(identifiers={("easee", "selected-charger")})
        self.registry = SimpleNamespace(async_get=Mock(return_value=self.device))
        self.hass = SimpleNamespace(data={"easee": {"controller": self.controller}})
        helpers = ModuleType("homeassistant.helpers")
        helpers.device_registry = SimpleNamespace(async_get=Mock(return_value=self.registry))
        easee_const = ModuleType("custom_components.easee.const")
        easee_const.VERSION = "0.9.74"
        pyeasee = ModuleType("pyeasee")
        pyeasee.__version__ = "0.8.17"
        self.version_module = easee_const
        self.pyeasee_module = pyeasee
        self.modules = patch.dict(
            sys.modules,
            {
                "homeassistant": ModuleType("homeassistant"),
                "homeassistant.helpers": helpers,
                "custom_components.easee.const": easee_const,
                "pyeasee": pyeasee,
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.client = adapter.EaseeCommandAdapter(self.hass, "ha-device-id")

    async def test_initialize_is_read_only(self):
        await self.client.async_initialize()
        self.assertIsNone(self.client.control_block_reason)
        self.circuit.set_dynamic_current.assert_not_awaited()
        self.charger.pause.assert_not_awaited()
        self.charger.resume.assert_not_awaited()

    async def test_overload_requires_zero_offline_readback_on_every_phase(self):
        self.assertEqual(self.client.overload_block_reason, "offline_fallback_not_zero_or_unverified")
        for phase in (1, 2, 3):
            self.product.state[f"offlineMaxCircuitCurrentP{phase}"] = 0
        self.assertIsNone(self.client.overload_block_reason)
        for phase in (1, 2, 3):
            for value in (10, 6, -1, None, True, float("nan"), "unknown"):
                with self.subTest(phase=phase, value=value):
                    self.product.state[f"offlineMaxCircuitCurrentP{phase}"] = value
                    self.assertEqual(self.client.overload_block_reason, "offline_fallback_not_zero_or_unverified")
            self.product.state[f"offlineMaxCircuitCurrentP{phase}"] = 0

    async def test_overload_offline_guard_also_requires_healthy_stream(self):
        for phase in (1, 2, 3):
            self.product.state[f"offlineMaxCircuitCurrentP{phase}"] = 0
        self.product.state["signalRConnected"] = False
        self.assertIsNotNone(self.client.overload_block_reason)
        await self.client.async_execute(decision(amps=0))
        self.circuit.set_dynamic_current.assert_awaited_once_with(0, 0, 0, 1)

    async def test_identical_limit_renews_ttl_on_every_call(self):
        await self.client.async_execute(decision())
        await self.client.async_execute(decision())
        self.assertEqual(self.circuit.set_dynamic_current.await_count, 2)
        self.circuit.set_dynamic_current.assert_awaited_with(12, 0, 0, 1)
        self.assertEqual(self.response.release.call_count, 2)

    async def test_ttl_seconds_round_up_to_whole_minutes(self):
        for seconds, minutes in ((1, 1), (60, 1), (61, 2), (120, 2), (121, 3)):
            with self.subTest(seconds=seconds):
                await self.client.async_execute(decision(ttl=seconds))
                self.circuit.set_dynamic_current.assert_awaited_with(12, 0, 0, minutes)

    async def test_persistent_and_invalid_ttl_are_rejected(self):
        for ttl in (0, -1, float("nan"), float("inf"), True, "60", 64801):
            with self.subTest(ttl=ttl):
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "finite_ttl_required"):
                    await self.client.async_execute(decision(ttl=ttl))
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_current_validation(self):
        for amps in (-1, 41, True, 6.5, "6"):
            with self.subTest(amps=amps):
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "invalid_phase_current"):
                    await self.client.async_execute(decision(amps=amps))
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_pause_resume_use_public_charger_methods(self):
        await self.client.async_execute(decision(command="pause"))
        await self.client.async_execute(decision(command="resume"))
        self.charger.pause.assert_awaited_once_with()
        self.charger.resume.assert_awaited_once_with()
        self.circuit.set_dynamic_current.assert_awaited_once_with(12, 0, 0, 1)

    async def test_resume_requires_accepted_positive_cap_first(self):
        order = []

        async def cap(*args):
            order.append("cap")
            return Response()

        async def resume():
            order.append("resume")
            return Response()

        self.circuit.set_dynamic_current.side_effect = cap
        self.charger.resume.side_effect = resume
        await self.client.async_execute(decision(command="resume"))
        self.assertEqual(order, ["cap", "resume"])

    async def test_failed_cap_never_resumes(self):
        self.circuit.set_dynamic_current.return_value = None
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_api_no_acceptance"):
            await self.client.async_execute(decision(command="resume"))
        self.charger.resume.assert_not_awaited()

    async def test_malformed_resume_never_calls_api(self):
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "resume_requires_safe_l1_cap"):
            await self.client.async_execute(decision(command="resume", amps=0))
        self.circuit.set_dynamic_current.assert_not_awaited()
        self.charger.resume.assert_not_awaited()

    async def test_unrelated_commands_are_rejected(self):
        for command in ("reboot", "update_firmware", "start", "Pause", None):
            with self.subTest(command=command):
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "unsupported_easee_command"):
                    await self.client.async_execute(decision(command=command))

    async def test_none_result_is_a_failure(self):
        self.circuit.set_dynamic_current.return_value = None
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_api_no_acceptance"):
            await self.client.async_execute(decision())

    async def test_http_rejection_is_failure_and_releases_connection(self):
        self.response.status = 500
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_api_rejected"):
            await self.client.async_execute(decision())
        self.response.release.assert_called_once_with()

    async def test_empty_successful_response_is_accepted(self):
        self.response.status = 204
        await self.client.async_execute(decision())
        self.response.release.assert_called_once_with()

    async def test_exception_text_is_never_exposed(self):
        self.charger.pause.side_effect = RuntimeError("private token in remote exception")
        with self.assertRaises(adapter.EaseeAdapterError) as raised:
            await self.client.async_execute(decision(command="pause"))
        self.assertEqual(str(raised.exception), "easee_request_failed:RuntimeError")

    async def test_request_timeout_is_a_failure(self):
        async def blocked(*args):
            await asyncio.sleep(60)

        self.circuit.set_dynamic_current.side_effect = blocked
        with patch.object(adapter, "REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(adapter.EaseeAdapterError, "TimeoutError"):
                await self.client.async_execute(decision())

    async def test_cancellation_propagates(self):
        self.charger.pause.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client.async_execute(decision(command="pause"))

    async def test_unsupported_versions_fail_closed(self):
        self.version_module.VERSION = "0.9.75"
        self.assertEqual(self.client.control_block_reason, "unsupported_or_unloaded_easee_version")
        with self.assertRaises(adapter.EaseeAdapterError):
            await self.client.async_execute(decision(amps=0))
        self.circuit.set_dynamic_current.assert_not_awaited()
        self.version_module.VERSION = "0.9.74"
        self.pyeasee_module.__version__ = "0.8.18"
        self.assertEqual(self.client.control_block_reason, "unsupported_or_unloaded_pyeasee_version")

    async def test_missing_or_ambiguous_registry_target_is_blocked(self):
        self.registry.async_get.return_value = None
        self.assertEqual(self.client.control_block_reason, "easee_device_not_found")
        self.registry.async_get.return_value = self.device
        self.device.identifiers.add(("easee", "another-charger"))
        self.assertEqual(self.client.control_block_reason, "ambiguous_or_missing_easee_identifier")

    async def test_unmatched_charger_cannot_fall_back_to_another_device(self):
        self.device.identifiers = {("easee", "not-the-selected-charger")}
        self.assertEqual(self.client.control_block_reason, "easee_charger_not_unique")
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_controller_reload_resolves_new_objects(self):
        new_circuit = SimpleNamespace(set_dynamic_current=AsyncMock(return_value=Response()))
        self.charger.circuit = new_circuit
        new_controller = SimpleNamespace(
            get_chargers=lambda: [self.charger], chargers_data=[self.product]
        )
        self.hass.data["easee"]["controller"] = new_controller
        await self.client.async_execute(decision())
        new_circuit.set_dynamic_current.assert_awaited_once_with(12, 0, 0, 1)
        self.circuit.set_dynamic_current.assert_not_awaited()

    async def test_healthy_event_stream_does_not_need_entity_last_updated(self):
        self.assertIsNone(self.client.telemetry_block_reason)
        await self.client.async_execute(decision())
        # No hass.states or entity timestamps exist in this test fixture.
        self.circuit.set_dynamic_current.assert_awaited_once()

    async def test_unhealthy_stream_blocks_increases_and_resume_but_allows_zero_and_pause(self):
        self.product.state["latestPulse"] -= timedelta(minutes=3)
        self.assertEqual(self.client.telemetry_block_reason, "easee_heartbeat_stale")
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_heartbeat_stale"):
            await self.client.async_execute(decision())
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_heartbeat_stale"):
            await self.client.async_execute(decision(command="resume"))
        await self.client.async_execute(decision(amps=0))
        await self.client.async_execute(decision(command="pause"))
        self.circuit.set_dynamic_current.assert_awaited_once_with(0, 0, 0, 1)
        self.charger.pause.assert_awaited_once()
        self.charger.resume.assert_not_awaited()

    async def test_stream_and_charger_health_flags_are_both_required(self):
        self.product.state["signalRConnected"] = False
        self.assertEqual(self.client.telemetry_block_reason, "easee_stream_disconnected")
        self.product.state["signalRConnected"] = True
        self.product.state["isOnline"] = False
        self.assertEqual(self.client.telemetry_block_reason, "easee_charger_offline")

    async def test_missing_naive_and_future_heartbeat_are_rejected(self):
        for pulse, reason in (
            (None, "easee_heartbeat_missing"),
            (datetime.now(), "easee_heartbeat_missing"),
            (datetime.now(timezone.utc) + timedelta(minutes=1), "easee_heartbeat_stale"),
        ):
            with self.subTest(reason=reason):
                self.product.state["latestPulse"] = pulse
                self.assertEqual(self.client.telemetry_block_reason, reason)


if __name__ == "__main__":
    unittest.main()
