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

    async def test_audited_new_sdk_is_allowed_other_versions_stay_closed(self):
        self.pyeasee_module.__version__ = "0.8.19"
        await self.client.async_execute(decision())
        self.circuit.set_dynamic_current.assert_awaited_once_with(12, 0, 0, 1)

    async def test_receipt_only_retains_valid_matching_correlation_and_no_body(self):
        self.response.json = AsyncMock(return_value=[
            {"commandId": 22, "ticks": 639281732354474790, "device": "PRIVATE"},
            {"commandId": 22, "ticks": "639281732354474790"},
            {"commandId": 48, "ticks": 123},
            {"commandId": True, "ticks": 456},
            {"commandId": 22, "ticks": "token-secret"},
        ])
        receipt = await self.client.async_execute(decision())
        self.assertEqual(receipt.correlation_ids, ((22, "639281732354474790"),))
        self.assertEqual(receipt.requested_phases, (12, 0, 0))
        self.assertEqual(receipt.http_status, 202)
        self.assertIsNotNone(receipt.accepted_at.tzinfo)
        self.assertLessEqual(receipt.request_started_monotonic, receipt.request_completed_monotonic)
        self.assertIs(receipt, self.client.last_submission_receipt)
        self.assertNotIn("PRIVATE", repr(receipt))
        self.assertNotIn("secret", repr(receipt))
        self.response.release.assert_called_once()

    async def test_200_204_and_malformed_202_are_unverified_not_delivery(self):
        for status, body in ((200, {"commandId": 22, "ticks": 123}),
                             (204, {"commandId": 22, "ticks": 123}),
                             (202, {"unknown": "private token"})):
            self.response.status = status
            self.response.json = AsyncMock(return_value=body)
            receipt = await self.client.async_execute(decision())
            self.assertEqual(receipt.correlation_ids, ())
            if status != 202:
                self.response.json.assert_not_awaited()

    async def test_bounded_body_reader_drops_oversized_metadata(self):
        self.response.content = SimpleNamespace(read=AsyncMock(return_value=b"x" * 8193))
        receipt = await self.client.async_execute(decision())
        self.assertEqual(receipt.correlation_ticks, ())
        self.response.content.read.assert_awaited_once_with(8193)
        self.response.release.assert_called_once()

    async def test_nonzero_admission_includes_reductions_and_reserves_zero(self):
        for amps in (16, 15, 14, 13, 12, 11, 10, 9):
            await self.client.async_execute(decision(amps=amps))
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "positive_rate_capacity_exhausted"):
            await self.client.async_execute(decision(amps=7))
        await self.client.async_execute(decision(amps=0))
        self.assertEqual(self.circuit.set_dynamic_current.await_count, 9)
        self.circuit.set_dynamic_current.assert_awaited_with(0, 0, 0, 1)

    async def test_expired_admission_history_reopens_without_background_work(self):
        with patch.object(adapter, "_monotonic", return_value=100):
            for _ in range(8):
                await self.client.async_execute(decision())
            with self.assertRaisesRegex(adapter.EaseeAdapterError, "positive_rate_capacity_exhausted"):
                await self.client.async_execute(decision())
        with patch.object(adapter, "_monotonic", return_value=161):
            await self.client.async_execute(decision())
        self.assertEqual(self.circuit.set_dynamic_current.await_count, 9)

    async def test_close_cancels_queued_sdk_write_no_late_positive(self):
        started = asyncio.Event()
        release = asyncio.Event()
        actual_writes = []

        async def queued(*args):
            started.set()
            await release.wait()
            actual_writes.append(args)
            return self.response

        self.circuit.set_dynamic_current.side_effect = queued
        request = asyncio.create_task(self.client.async_execute(decision()))
        await started.wait()
        await self.client.async_close()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(actual_writes, [])
        self.assertEqual(self.client.control_block_reason, "easee_adapter_closed")

    def attach_stream(self):
        callbacks = []
        self._cap_callbacks = []
        stream = SimpleNamespace(on=lambda event, cb: (callbacks if event == "CommandResponse"
                                                      else self._cap_callbacks).append(cb))
        self.charger.easee = SimpleNamespace(sr_connection=stream)
        return callbacks

    def delivery(self, **updates):
        result = {"serialNumber": "selected-charger", "id": 22,
                  "ticks": 639281732354474790, "wasAccepted": True,
                  "resultCode": None, "timestamp": "2026-01-01T00:00:00Z",
                  "deliveredAt": "2026-01-01T00:00:01Z", "comment": "PRIVATE"}
        result.update(updates)
        return result

    async def test_public_hook_preserves_early_delivery_before_http_receipt(self):
        callbacks = self.attach_stream()
        events = []
        unsubscribe = self.client.set_delivery_callback(events.append)
        self.assertTrue(self.client.delivery_listener_available)
        self.assertEqual(len(callbacks), 1)

        async def submit(*args):
            await callbacks[0]([self.delivery()])
            self.assertEqual(len(events), 1)
            self.assertIsNone(self.client.last_submission_receipt)
            return self.response

        self.circuit.set_dynamic_current.side_effect = submit
        self.response.json = AsyncMock(return_value={"commandId": 22, "ticks": 639281732354474790})
        receipt = await self.client.async_execute(decision())
        self.assertEqual(events[0].ticks, receipt.correlation_ticks[0])
        self.assertNotIn("PRIVATE", repr(events[0]))
        self.assertNotIn("selected-charger", repr(events[0]))
        unsubscribe()
        await callbacks[0]([self.delivery()])
        self.assertEqual(len(events), 1)

    async def test_foreign_malformed_and_stale_connection_delivery_cannot_confirm(self):
        old_callbacks = self.attach_stream()
        events = []
        self.client.set_delivery_callback(events.append)
        for change in ({"serialNumber": "other"}, {"ticks": True}, {"wasAccepted": 1},
                       {"ticks": "private"}, {"resultCode": "private"}):
            await old_callbacks[0]([self.delivery(**change)])
        self.assertEqual(events, [])
        new_callbacks = self.attach_stream()
        self.assertTrue(self.client.delivery_listener_available)
        await old_callbacks[0]([self.delivery()])
        self.assertEqual(events, [])
        await new_callbacks[0]([self.delivery()])
        self.assertEqual(len(events), 1)
        await self.client.async_close()
        await new_callbacks[0]([self.delivery()])
        self.assertEqual(len(events), 1)

    async def test_readback_zero_validation_is_strict_and_not_a_health_override(self):
        self.assertIsNone(self.client.reported_limits)
        for phase in (1, 2, 3):
            self.product.state[f"dynamicCircuitCurrentP{phase}"] = 0
            self.product.state[f"offlineMaxCircuitCurrentP{phase}"] = 0
        self.assertEqual(self.client.reported_limits, (0, 0, 0))
        self.assertTrue(self.client.offline_limits_verified_zero)
        self.product.state["offlineMaxCircuitCurrentP2"] = False
        self.assertFalse(self.client.offline_limits_verified_zero)
        self.product.state["signalRConnected"] = False
        self.assertEqual(self.client.telemetry_block_reason, "easee_stream_disconnected")

    async def test_reload_same_connection_fences_old_hook_and_control_identity(self):
        callbacks = self.attach_stream()
        events = []
        self.client.set_delivery_callback(events.append)
        first = self.client.control_identity
        self.hass.data["easee"]["controller"] = SimpleNamespace(
            get_chargers=lambda: [self.charger], chargers_data=[self.product])
        second = self.client.control_identity
        self.assertNotEqual(first, second)
        self.assertEqual(len(callbacks), 2)
        await callbacks[0]([self.delivery()])
        self.assertEqual(events, [])
        await callbacks[1]([self.delivery()])
        self.assertEqual(len(events), 1)

    async def test_delivery_sink_exception_does_not_break_native_dispatch(self):
        callbacks = self.attach_stream()
        self.client.set_delivery_callback(Mock(side_effect=ValueError("private error")))
        await callbacks[0]([self.delivery()])

    async def test_timed_out_sdk_queue_cannot_write_after_gate_opens(self):
        gate = asyncio.Event()
        actual_writes = []

        async def queued(*args):
            await gate.wait()
            actual_writes.append(args)
            return self.response

        self.circuit.set_dynamic_current.side_effect = queued
        with patch.object(adapter, "REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(adapter.EaseeAdapterError, "TimeoutError"):
                await self.client.async_execute(decision())
        gate.set()
        await asyncio.sleep(0)
        self.assertEqual(actual_writes, [])
        self.assertEqual(self.client._pending_requests, set())

    async def test_native_phase_receipt_only_changes_on_selected_valid_product_event(self):
        self.attach_stream()
        self.client.set_delivery_callback(lambda event: None)
        for phase in (1, 2, 3):
            self.product.state[f"dynamicCircuitCurrentP{phase}"] = 0
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))
        frame = {"mid": "selected-charger", "id": 111, "dataType": 3, "value": "0.0"}
        for update in ({"mid": "other"}, {"value": True}, {"value": "NaN"},
                       {"dataType": True}, {"id": 109}, {"value": "private"}):
            await self._cap_callbacks[0]([{**frame, **update}])
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))
        with patch.object(adapter, "_monotonic", return_value=100):
            await self._cap_callbacks[0]([frame])
        self.assertEqual(self.client.reported_limit_receipts, (100, None, None))
        with patch.object(adapter, "_monotonic", return_value=101):
            # A newly received unchanged-zero event is distinct from merely
            # rereading a cached HA value; it still is not acquisition proof.
            await self._cap_callbacks[0]([frame])
        self.assertEqual(self.client.reported_limit_receipts, (101, None, None))
        self.product.state["dynamicCircuitCurrentP1"] = 7
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))

    async def test_cap_receipts_reset_and_old_stream_cannot_repopulate_them(self):
        self.attach_stream()
        self.client.set_delivery_callback(lambda event: None)
        old = self._cap_callbacks[0]
        for phase in (1, 2, 3):
            self.product.state[f"dynamicCircuitCurrentP{phase}"] = 0
        frame = {"mid": "selected-charger", "id": 111, "dataType": 4, "value": 0}
        await old([frame])
        self.assertIsNotNone(self.client.reported_limit_receipts[0])
        self.attach_stream()
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))
        await old([frame])
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))
        await self._cap_callbacks[0]([frame])
        self.assertIsNotNone(self.client.reported_limit_receipts[0])
        await self.client.async_close()
        self.assertEqual(self.client.reported_limit_receipts, (None, None, None))


if __name__ == "__main__":
    unittest.main()
