"""Offline GET-snapshot and concurrent reload contracts; no HA or network."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ADAPTER_PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "easee_adapter.py"
SPEC = importlib.util.spec_from_file_location("onefuse_snapshot_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


class PublicState(Mapping):
    """Exercise inherited Mapping.get, as in public pyeasee BaseDict."""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


class SnapshotAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = SimpleNamespace(status=202, release=Mock())
        self.circuit = SimpleNamespace(set_dynamic_current=AsyncMock(return_value=self.response))
        self.charger = self.make_charger(self.circuit)
        self.product = SimpleNamespace(product=self.charger, state=self.health())
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
        modules = patch.dict(
            sys.modules,
            {
                "homeassistant": ModuleType("homeassistant"),
                "homeassistant.helpers": helpers,
                "custom_components.easee.const": easee_const,
                "pyeasee": pyeasee,
            },
        )
        modules.start()
        self.addCleanup(modules.stop)
        self.client = adapter.EaseeCommandAdapter(self.hass, "ha-device-id")

    @staticmethod
    def health():
        return {
            "signalRConnected": True,
            "isOnline": True,
            "latestPulse": datetime.now(timezone.utc),
        }

    def make_charger(self, circuit):
        return SimpleNamespace(
            id="selected-charger",
            circuit=circuit,
            get_state=AsyncMock(return_value=PublicState({"totalCurrent": 0.012, "chargerOpMode": 6})),
            pause=AsyncMock(return_value=self.response),
            resume=AsyncMock(return_value=self.response),
            start=AsyncMock(),
            force_update=AsyncMock(),
        )

    def assert_no_commands(self):
        self.circuit.set_dynamic_current.assert_not_awaited()
        for name in ("pause", "resume", "start", "force_update"):
            getattr(self.charger, name).assert_not_awaited()

    async def test_snapshot_uses_raw_public_get_and_has_immutable_utc_receipt(self):
        before = datetime.now(timezone.utc)
        snapshot = await self.client.async_read_current_snapshot()
        after = datetime.now(timezone.utc)
        self.assertEqual(snapshot.current, 0.012)
        self.assertIsInstance(snapshot.current, float)
        self.assertEqual(snapshot.status, "ready_to_charge")
        self.assertIs(snapshot.received_at.tzinfo, timezone.utc)
        self.assertLessEqual(before, snapshot.received_at)
        self.assertLessEqual(snapshot.received_at, after)
        self.assertEqual(snapshot.source_identity, tuple(id(value) for value in (
            self.controller, self.charger, self.circuit, self.product
        )))
        self.assertNotIn("source_identity", repr(snapshot))
        self.assertTrue(self.client.snapshot_is_current(snapshot))
        with self.assertRaises(FrozenInstanceError):
            snapshot.current = 12
        self.charger.get_state.assert_awaited_once_with(raw=True)
        self.assert_no_commands()

    async def test_api_zero_and_status_do_not_reuse_old_product_values(self):
        self.product.state.update(totalCurrent=16, chargerOpMode="charging")
        self.charger.get_state.return_value = PublicState({"totalCurrent": 0, "chargerOpMode": 4})
        snapshot = await self.client.async_read_current_snapshot()
        self.assertEqual((snapshot.current, snapshot.status), (0.0, "completed"))
        self.assert_no_commands()

    async def test_cached_snapshot_is_rejected_after_each_runtime_object_reloads(self):
        for replaced in ("controller", "charger", "circuit", "product"):
            with self.subTest(replaced=replaced):
                snapshot = await self.client.async_read_current_snapshot()
                self.assertTrue(self.client.snapshot_is_current(snapshot))
                if replaced == "controller":
                    self.controller = SimpleNamespace(
                        get_chargers=Mock(return_value=[self.charger]), chargers_data=[self.product]
                    )
                    self.hass.data["easee"]["controller"] = self.controller
                elif replaced == "charger":
                    self.charger = self.make_charger(self.circuit)
                    self.product.product = self.charger
                    self.controller.get_chargers.return_value = [self.charger]
                elif replaced == "circuit":
                    self.circuit = SimpleNamespace(set_dynamic_current=AsyncMock(return_value=self.response))
                    self.charger.circuit = self.circuit
                else:
                    self.product = SimpleNamespace(product=self.charger, state=self.health())
                    self.controller.chargers_data = [self.product]
                self.assertFalse(self.client.snapshot_is_current(snapshot))
        self.assert_no_commands()

    async def test_cached_snapshot_is_rejected_after_telemetry_health_loss(self):
        snapshot = await self.client.async_read_current_snapshot()
        self.product.state["signalRConnected"] = False
        self.assertFalse(self.client.snapshot_is_current(snapshot))
        self.registry.async_get.return_value = None
        self.assertFalse(self.client.snapshot_is_current(snapshot))
        self.assert_no_commands()

    async def test_explicit_raw_modes_preserve_fault_offline_schedule_and_ready_states(self):
        expected = {
            0: "offline", 1: "disconnected", 2: "awaiting_start", 3: "charging",
            4: "completed", 5: "error", 6: "ready_to_charge", 7: "awaiting_authorization",
            8: "de_authorizing", 100: "start_charging", 101: "stop_charging",
            102: "offline", 103: "awaiting_load_balancing", 104: "awaiting_authorization",
            105: "awaiting_smart_start", 106: "awaiting_scheduled_start", 107: "authenticating",
            108: "paused_due_to_equalizer", 109: "searching_for_master", 157: "erratic_ev",
            158: "error_temperature_too_high", 159: "error_dead_powerboard",
            160: "error_overcurrent", 161: "error_pen_fault",
        }
        for mode, status in expected.items():
            with self.subTest(mode=mode):
                self.charger.get_state.return_value = PublicState({"totalCurrent": 0, "chargerOpMode": mode})
                self.assertEqual((await self.client.async_read_current_snapshot()).status, status)
        self.assert_no_commands()

    async def test_missing_or_nonmapping_response_is_rejected(self):
        for state in (None, [], "state", SimpleNamespace(totalCurrent=0, chargerOpMode=6)):
            with self.subTest(state=state):
                self.charger.get_state.return_value = state
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_missing_or_invalid"):
                    await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_invalid_or_missing_current_is_rejected(self):
        for value in (None, True, False, float("nan"), float("inf"), -0.001, 1000.001, "bad", object()):
            with self.subTest(value=value):
                self.charger.get_state.return_value = PublicState({"totalCurrent": value, "chargerOpMode": 6})
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_invalid_current"):
                    await self.client.async_read_current_snapshot()
        self.charger.get_state.return_value = PublicState({"chargerOpMode": 6})
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_invalid_current"):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_numeric_current_boundaries_and_integral_float_mode(self):
        for value in (0, 1000, 6.125):
            with self.subTest(value=value):
                self.charger.get_state.return_value = PublicState({"totalCurrent": value, "chargerOpMode": 3.0})
                snapshot = await self.client.async_read_current_snapshot()
                self.assertEqual((snapshot.current, snapshot.status), (float(value), "charging"))

    async def test_non_numeric_unknown_and_missing_operation_modes_are_rejected(self):
        for value in (None, True, False, "3", "charging", float("nan"), float("inf"), 3.5, -1, 999):
            with self.subTest(mode=value):
                self.charger.get_state.return_value = PublicState({"totalCurrent": 0, "chargerOpMode": value})
                with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_invalid_operation_mode"):
                    await self.client.async_read_current_snapshot()
        self.charger.get_state.return_value = PublicState({"totalCurrent": 0})
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_invalid_operation_mode"):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_health_required_before_request(self):
        for key, value in (("signalRConnected", False), ("isOnline", False), ("latestPulse", datetime.now(timezone.utc) - timedelta(minutes=3))):
            with self.subTest(key=key):
                self.product.state = self.health()
                self.product.state[key] = value
                with self.assertRaises(adapter.EaseeAdapterError):
                    await self.client.async_read_current_snapshot()
        self.charger.get_state.assert_not_awaited()
        self.assert_no_commands()

    async def test_health_loss_during_request_discards_returned_state(self):
        for key, value in (("signalRConnected", False), ("isOnline", False), ("latestPulse", datetime.now(timezone.utc) - timedelta(minutes=3))):
            with self.subTest(key=key):
                self.product.state = self.health()

                async def read(*, raw):
                    self.product.state[key] = value
                    return PublicState({"totalCurrent": 0, "chargerOpMode": 6})

                self.charger.get_state.side_effect = read
                with self.assertRaises(adapter.EaseeAdapterError):
                    await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_charger_reload_during_request_discards_result(self):
        replacement = self.make_charger(self.circuit)

        async def read(*, raw):
            self.controller.get_chargers.return_value = [replacement]
            self.controller.chargers_data = [SimpleNamespace(product=replacement, state=self.health())]
            # Simulate a concurrent command/property resolving the replacement.
            self.assertIsNone(self.client.telemetry_block_reason)
            return PublicState({"totalCurrent": 0, "chargerOpMode": 6})

        self.charger.get_state.side_effect = read
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_runtime_changed_during_request"):
            await self.client.async_read_current_snapshot()
        replacement.get_state.assert_not_awaited()
        replacement.resume.assert_not_awaited()
        self.assert_no_commands()

    async def test_product_reload_during_request_discards_result(self):
        async def read(*, raw):
            self.controller.chargers_data = [SimpleNamespace(product=self.charger, state=self.health())]
            return PublicState({"totalCurrent": 0, "chargerOpMode": 6})

        self.charger.get_state.side_effect = read
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_runtime_changed_during_request"):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_controller_reload_with_same_child_objects_discards_result(self):
        async def read(*, raw):
            self.hass.data["easee"]["controller"] = SimpleNamespace(
                get_chargers=Mock(return_value=[self.charger]), chargers_data=[self.product]
            )
            return PublicState({"totalCurrent": 0, "chargerOpMode": 6})

        self.charger.get_state.side_effect = read
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_runtime_changed_during_request"):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_registry_disappearing_or_device_selection_changing_discards_result(self):
        for mutation in ("missing", "selection", "entry"):
            with self.subTest(mutation=mutation):
                self.registry.async_get.return_value = self.device
                self.client.device_id = "ha-device-id"

                async def read(*, raw):
                    if mutation == "missing":
                        self.registry.async_get.return_value = None
                    elif mutation == "selection":
                        self.client.device_id = "different-device-id"
                    else:
                        self.registry.async_get.return_value = SimpleNamespace(identifiers=self.device.identifiers.copy())
                    return PublicState({"totalCurrent": 0, "chargerOpMode": 6})

                self.charger.get_state.side_effect = read
                with self.assertRaises(adapter.EaseeAdapterError):
                    await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_missing_public_state_method_is_rejected_without_commands(self):
        self.charger.get_state = None
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_snapshot_api_unavailable"):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_request_exception_text_is_never_exposed(self):
        self.charger.get_state.side_effect = RuntimeError("private token remote URL")
        with self.assertRaises(adapter.EaseeAdapterError) as raised:
            await self.client.async_read_current_snapshot()
        self.assertEqual(str(raised.exception), "easee_snapshot_failed:RuntimeError")
        self.assertTrue(raised.exception.__suppress_context__)
        self.assert_no_commands()

    async def test_timeout_is_credential_free_and_bounded_to_ten_seconds(self):
        async def blocked(*, raw):
            await asyncio.sleep(60)

        self.charger.get_state.side_effect = blocked
        with patch.object(adapter, "REQUEST_TIMEOUT_SECONDS", 0.001):
            with self.assertRaisesRegex(adapter.EaseeAdapterError, "^easee_snapshot_failed:TimeoutError$"):
                await self.client.async_read_current_snapshot()
        real_timeout = asyncio.timeout
        self.charger.get_state.side_effect = None
        with patch.object(adapter, "REQUEST_TIMEOUT_SECONDS", 60), patch.object(adapter.asyncio, "timeout", wraps=real_timeout) as timeout:
            await self.client.async_read_current_snapshot()
            timeout.assert_called_once_with(10)
        self.assert_no_commands()

    async def test_cancellation_propagates_without_commands(self):
        self.charger.get_state.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client.async_read_current_snapshot()
        self.assert_no_commands()

    async def test_reload_after_accepted_cap_cannot_resume_either_charger(self):
        replacement = self.make_charger(self.circuit)

        async def cap(*args):
            self.controller.get_chargers.return_value = [replacement]
            self.controller.chargers_data = [SimpleNamespace(product=replacement, state=self.health())]
            await self.client.async_read_current_snapshot()
            return self.response

        self.circuit.set_dynamic_current.side_effect = cap
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_runtime_changed_during_request"):
            await self.client.async_execute(SimpleNamespace(
                command="resume", current_p1=6, current_p2=0, current_p3=0, ttl_seconds=60
            ))
        self.charger.resume.assert_not_awaited()
        replacement.resume.assert_not_awaited()
        self.response.release.assert_called_once_with()

    async def test_health_loss_after_accepted_cap_prevents_resume(self):
        async def cap(*args):
            self.product.state["isOnline"] = False
            return self.response

        self.circuit.set_dynamic_current.side_effect = cap
        with self.assertRaisesRegex(adapter.EaseeAdapterError, "easee_charger_offline"):
            await self.client.async_execute(SimpleNamespace(
                command="resume", current_p1=6, current_p2=0, current_p3=0, ttl_seconds=60
            ))
        self.charger.resume.assert_not_awaited()
        self.response.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
