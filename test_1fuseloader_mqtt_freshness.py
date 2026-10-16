"""Actual MQTT receipt freshness, source binding and controller integration.

All MQTT calls are mocked; these tests never connect to HA or a broker. The
optional real-HA tests verify imports, callback scheduling and API signatures.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


COMPONENT = Path(__file__).parent / "custom_components" / "1fuseloader"
SPEC = importlib.util.spec_from_file_location("onefuse_mqtt_freshness_tests", COMPONENT / "mqtt_freshness.py")
source_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source_module
SPEC.loader.exec_module(source_module)
HAS_HA = importlib.util.find_spec("homeassistant") is not None
ENTITY_ID = "sensor.grid"
IDENTITY = "001122aabbcc"
TOPIC = f"currently/one/{IDENTITY}/state"


def identity_entries():
    entity = SimpleNamespace(entity_id=ENTITY_ID, platform="mqtt", unique_id=f"I1_{IDENTITY}",
                             device_id="meter-device", disabled_by=None)
    device = SimpleNamespace(id="meter-device", manufacturer="Stopgap AB", model="Currently One",
                             identifiers={("mqtt", f"C1_{IDENTITY}")}, disabled_by=None)
    return entity, device


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tick = 100.0
        self.utc = datetime.now(timezone.utc)
        self.changes = 0
        entity, device = identity_entries()
        self.config = source_module.MqttL1Config.from_registry(ENTITY_ID, entity, device)
        self.source = source_module.MqttL1Source(
            self.config, monotonic=lambda: self.tick, utc_now=lambda: self.utc,
            age_limit=lambda: 120, on_change=self.changed,
        )
        # Pure protocol tests start after the mocked subscription is active.
        self.source._active = True
        self.source._connected = True

    def changed(self):
        self.changes += 1

    def advance(self, seconds):
        self.tick += seconds
        self.utc += timedelta(seconds=seconds)

    def receive(self, current=18.2, *, stamp=None, retained=False, topic=TOPIC, **other):
        frame = {"I1": current, "sec": self.utc.timestamp() if stamp is None else stamp, **other}
        self.source.receive(SimpleNamespace(payload=json.dumps(frame), topic=topic, retain=retained))

    def test_constant_current_frames_remain_fresh_for_more_than_two_minutes(self):
        timestamps = []
        for _ in range(61):
            self.receive(L1=4158, I2=5, diagnostic_private="must not be stored")
            sample = self.source.snapshot()
            self.assertIsNone(sample.error)
            self.assertEqual(sample.current, 18.2)
            timestamps.append(sample.report_ts)
            self.advance(5)
        self.assertEqual(len(set(timestamps)), 61)
        self.assertEqual(self.changes, 61)
        self.assertNotIn("diagnostic_private", vars(self.source))
        self.assertEqual(timestamps[-1] - timestamps[0], 300)

    def test_advancing_other_fields_without_i1_immediately_invalidates(self):
        self.receive()
        self.advance(5)
        self.source.receive(SimpleNamespace(payload=json.dumps({"sec": self.utc.timestamp(), "I2": 8, "L1": 4200}),
                                            topic=TOPIC, retain=False))
        self.assertIsNone(self.source.snapshot().current)
        self.assertIn("missing", self.source.snapshot().error)

    def test_missing_sec_cannot_be_rescued_by_current_or_other_phases(self):
        self.receive()
        self.source.receive(SimpleNamespace(payload='{"I1":18.2,"I2":10}', topic=TOPIC, retain=False))
        self.assertIsNotNone(self.source.snapshot().error)

    def test_invalid_current_or_timestamp_is_rejected(self):
        for current in (None, True, "18.2", -1, float("nan"), float("inf"), 1001):
            with self.subTest(current=current):
                self.advance(5)
                self.receive(current)
                self.assertIsNotNone(self.source.snapshot().error)
        for stamp in (False, "bad", self.utc.timestamp() - 121, self.utc.timestamp() + 6):
            with self.subTest(stamp=stamp):
                self.receive(stamp=stamp)
                self.assertIsNotNone(self.source.snapshot().error)

    def test_invalid_json_and_non_dict_payloads_invalidate(self):
        for payload in ("not JSON", "[]", "null", b"{}", "x" * 65537):
            self.source.receive(SimpleNamespace(payload=payload, topic=TOPIC, retain=False))
            self.assertIsNotNone(self.source.snapshot().error)

    def test_retained_startup_does_not_establish_health(self):
        self.receive(retained=True)
        self.assertIsNotNone(self.source.snapshot().error)
        self.assertEqual(self.changes, 0)
        self.receive()
        accepted = self.source.snapshot()
        self.advance(10)
        self.receive(retained=True)
        self.assertEqual(self.source.snapshot().receipt_ts, accepted.receipt_ts)

    def test_nonretained_repeated_frame_does_not_extend_freshness(self):
        stamp = self.utc.timestamp()
        self.receive(stamp=stamp)
        first = self.source.snapshot()
        self.advance(120)
        self.receive(stamp=stamp)
        self.assertIsNone(self.source.snapshot().error)
        self.assertEqual(self.source.snapshot().receipt_ts, first.receipt_ts)
        self.advance(0.1)
        self.receive(stamp=stamp)
        self.assertIsNotNone(self.source.snapshot().error)

    def test_backward_or_conflicting_timestamp_invalidates(self):
        self.receive()
        stamp = self.utc.timestamp()
        self.receive(current=20, stamp=stamp)
        self.assertIsNotNone(self.source.snapshot().error)
        self.receive(stamp=stamp - 1)
        self.assertIsNotNone(self.source.snapshot().error)
        self.advance(5)
        self.receive(current=20)
        self.assertIsNone(self.source.snapshot().error)

    def test_genuine_receipt_gap_and_utc_clock_gap_still_expire(self):
        self.receive()
        self.tick += 121
        self.assertIn("receipt", self.source.snapshot().error)
        self.tick -= 121
        self.utc += timedelta(seconds=121)
        self.assertIn("sample", self.source.snapshot().error)

    def test_high_current_is_available_to_protective_logic_not_clamped(self):
        self.receive(current=38)
        self.assertEqual(self.source.snapshot().current, 38)
        self.assertIsNone(self.source.snapshot().error)

    def test_backwards_receipt_clock_is_invalid(self):
        self.receive()
        self.tick -= 1
        self.assertIsNotNone(self.source.snapshot().error)

    def test_disconnect_reconnect_requires_a_new_live_sample(self):
        self.receive()
        self.source.connection_changed(False)
        self.assertIsNotNone(self.source.snapshot().error)
        self.source.connection_changed(True)
        self.receive(retained=True)
        self.assertIsNotNone(self.source.snapshot().error)
        self.advance(5)
        self.receive()
        self.assertIsNone(self.source.snapshot().error)

    def test_unrelated_topic_cannot_validate_source(self):
        self.receive(topic="another/source")
        self.assertIsNotNone(self.source.snapshot().error)

    def test_future_sample_clock_never_claims_a_future_report(self):
        receipt = self.utc.timestamp()
        self.receive(stamp=receipt + 4)
        self.assertEqual(self.source.snapshot().report_ts, receipt)

    def test_auto_profile_requires_both_matching_public_registry_identities(self):
        self.assertEqual(self.config.topic, TOPIC)
        for owner, key, value in (
            ("entity", "platform", "template"), ("entity", "entity_id", "sensor.another"),
            ("entity", "unique_id", f"I2_{IDENTITY}"), ("entity", "disabled_by", "user"),
            ("entity", "device_id", "wrong-device"), ("device", "manufacturer", "Other"),
            ("device", "model", "Other"), ("device", "identifiers", {("mqtt", "C1_ffffffffff00")}),
            ("device", "disabled_by", "user"),
        ):
            with self.subTest(key=key, value=value):
                entity, device = identity_entries()
                setattr(entity if owner == "entity" else device, key, value)
                self.assertIsNone(source_module.MqttL1Config.from_registry(ENTITY_ID, entity, device))
        entity, device = identity_entries()
        self.assertIsNone(source_module.MqttL1Config.from_registry(ENTITY_ID, entity, None))
        self.assertIsNone(source_module.MqttL1Config.from_registry(ENTITY_ID, None, device))


class ControllerSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_1fuseloader_runtime as runtime_tests
        self.rt = runtime_tests
        self.runtime = runtime_tests.runtime
        self.fixture = runtime_tests.ControllerTests(methodName="runTest")
        await self.fixture.asyncSetUp()
        self.controller = self.fixture.controller
        self.hass = self.fixture.hass
        self.entity, self.device = identity_entries()
        self.entities = {ENTITY_ID: self.entity}
        self.message_callback = None
        self.connection_callback = None
        self.message_unsubscribed = 0
        self.connection_unsubscribed = 0

        async def subscribe(hass, topic, callback, **kwargs):
            self.assertEqual(topic, TOPIC)
            self.message_callback = callback
            return self.unsubscribe_messages

        def connection_subscribe(hass, callback):
            self.connection_callback = callback
            return self.unsubscribe_connection

        ha = ModuleType("homeassistant")
        ha.__path__ = []
        helpers = ModuleType("homeassistant.helpers")
        helpers.device_registry = SimpleNamespace(async_get=lambda _: SimpleNamespace(async_get=lambda _: self.device))
        components = ModuleType("homeassistant.components")
        components.mqtt = SimpleNamespace(is_connected=lambda _: True, async_subscribe=subscribe,
                                          async_subscribe_connection_status=connection_subscribe)
        core = ModuleType("homeassistant.core")
        core.callback = lambda function: function
        self.modules = {"homeassistant": ha, "homeassistant.helpers": helpers,
                        "homeassistant.components": components, "homeassistant.core": core}
        self.module_patch = patch.dict(sys.modules, self.modules)
        self.module_patch.start()
        self.registry_patch = patch.object(self.runtime.er, "async_get", create=True,
                                          new=lambda _: SimpleNamespace(async_get=self.entities.get))
        self.registry_patch.start()

    async def asyncTearDown(self):
        await self.controller.async_stop()
        self.registry_patch.stop()
        self.module_patch.stop()

    def unsubscribe_messages(self):
        self.message_unsubscribed += 1

    def unsubscribe_connection(self):
        self.connection_unsubscribed += 1

    def receive(self, current=18.2, stamp=None, retained=False):
        self.message_callback(SimpleNamespace(topic=TOPIC, retain=retained, payload=json.dumps({
            "I1": current, "sec": datetime.now(timezone.utc).timestamp() if stamp is None else stamp,
        })))

    async def test_auto_source_replaces_stale_ha_timestamp_for_validation_alignment_and_diagnostics(self):
        self.fixture.states[ENTITY_ID].last_reported -= timedelta(hours=1)
        await self.controller._async_configure_l1_source_locked()
        self.assertIsNotNone(self.controller._l1_source)
        self.assertFalse(self.controller._read_input().data_valid)
        self.receive()
        first = self.controller._read_input()
        self.assertTrue(first.data_valid)
        self.assertEqual(first.grid_l1, 18.2)
        old = self.fixture.states[ENTITY_ID].last_reported.timestamp()
        self.assertGreater(first.grid_report_ts, old + 3500)
        self.assertEqual(self.controller.source_diagnostics["grid_source_kind"], "currently_one_validated_mqtt")
        self.assertEqual(datetime.fromisoformat(self.controller.source_diagnostics["grid_reported_at_utc"]).timestamp(), first.grid_report_ts)
        self.assertEqual(datetime.fromisoformat(self.controller.source_diagnostics["grid_state_reported_at_utc"]).timestamp(), old)
        self.assertIsNotNone(self.controller.source_diagnostics["grid_mqtt_received_at_utc"])
        self.receive(stamp=first.grid_report_ts + 0.01)
        self.assertGreater(self.controller._read_input().grid_report_ts, first.grid_report_ts)
        serialized = json.dumps(self.controller.source_diagnostics)
        self.assertNotIn(TOPIC, serialized)
        self.assertNotIn(IDENTITY, serialized)

    async def test_invalid_sample_is_not_rescued_by_fresh_entity_report(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        self.receive(current=-1)
        snapshot = self.controller._read_input()
        self.assertFalse(snapshot.data_valid)
        self.assertIsNone(snapshot.grid_l1)
        self.assertIsNone(snapshot.grid_report_ts)

    async def test_restored_state_and_wrong_units_still_block(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        self.fixture.states[ENTITY_ID].attributes["restored"] = True
        self.assertFalse(self.controller._read_input().data_valid)
        self.fixture.states[ENTITY_ID].attributes.pop("restored")
        self.fixture.states[ENTITY_ID].attributes["unit_of_measurement"] = "kW"
        self.assertFalse(self.controller._read_input().data_valid)

    async def test_unknown_source_uses_existing_entity_report_failsafe(self):
        self.entity.platform = "other_integration"
        self.fixture.states[ENTITY_ID].last_reported -= timedelta(seconds=121)
        await self.controller._async_configure_l1_source_locked()
        self.assertIsNone(self.controller._l1_source)
        sample = self.controller._read_input()
        self.assertFalse(sample.data_valid)
        self.assertIn("L1 telemetry is stale", sample.data_error)

    async def test_selected_entity_change_unsubscribes_and_discards_old_receipts(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        old_callback = self.message_callback
        self.controller.entry.data["l1_entity"] = "sensor.new_grid"
        await self.controller._async_configure_l1_source_locked()
        self.assertEqual((self.message_unsubscribed, self.connection_unsubscribed), (1, 1))
        self.assertIsNone(self.controller._l1_source)
        old_callback(SimpleNamespace(topic=TOPIC, retain=False, payload='{"I1":18.2,"sec":1}'))
        self.assertFalse(self.controller._read_input().data_valid)

    async def test_unload_cleans_both_listeners_and_queued_receipt_is_ignored(self):
        await self.controller._async_configure_l1_source_locked()
        source = self.controller._l1_source
        callback = self.message_callback
        await self.controller.async_stop()
        self.assertEqual((self.message_unsubscribed, self.connection_unsubscribed), (1, 1))
        callback(SimpleNamespace(topic=TOPIC, retain=False, payload='{"I1":18.2,"sec":1}'))
        self.assertIsNotNone(source.snapshot().error)
        self.assertFalse(self.controller._pending_refresh)

    async def test_receipt_callback_schedules_coalesced_evaluation(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        task = self.controller._refresh_task
        self.assertIsNotNone(task)
        self.receive(stamp=datetime.now(timezone.utc).timestamp() + 0.01)
        self.assertIs(self.controller._refresh_task, task)
        await task
        self.assertTrue(self.controller.last_decision.grid_l1 == 18.2)

    async def test_disconnect_is_not_rescued_by_fresh_native_entity_state(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        self.connection_callback(False)
        self.assertFalse(self.controller._read_input().data_valid)
        self.connection_callback(True)
        self.receive(retained=True)
        self.assertFalse(self.controller._read_input().data_valid)
        self.receive(stamp=datetime.now(timezone.utc).timestamp() + 0.01)
        self.assertTrue(self.controller._read_input().data_valid)

    async def test_subscription_failure_cleans_connection_listener_and_fails_closed(self):
        self.modules["homeassistant.components"].mqtt.async_subscribe = AsyncMock(side_effect=RuntimeError("private detail"))
        await self.controller._async_configure_l1_source_locked()
        self.assertEqual(self.connection_unsubscribed, 1)
        self.assertIsNone(self.controller._l1_source)
        snapshot = self.controller._read_input()
        self.assertFalse(snapshot.data_valid)
        self.assertIsNone(snapshot.grid_l1)
        self.assertNotIn("private detail", snapshot.data_error)

    async def test_stop_during_pending_subscription_cleans_late_unsubscribe_handle(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def subscribe(*args, **kwargs):
            entered.set()
            await gate.wait()
            return self.unsubscribe_messages

        self.modules["homeassistant.components"].mqtt.async_subscribe = subscribe
        starting = asyncio.create_task(self.controller._async_configure_l1_source_locked())
        await entered.wait()
        await self.controller.async_stop()
        gate.set()
        await starting
        self.assertEqual((self.message_unsubscribed, self.connection_unsubscribed), (1, 1))
        self.assertIsNone(self.controller._l1_source)

    async def test_public_registry_rebinding_removes_the_old_vendor_subscription(self):
        await self.controller._async_configure_l1_source_locked()
        self.receive()
        self.device.identifiers = {("mqtt", "C1_ffffffffff00")}
        await self.controller._async_configure_l1_source_locked()
        self.assertEqual((self.message_unsubscribed, self.connection_unsubscribed), (1, 1))
        self.assertIsNone(self.controller._l1_source)


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in the CI job")
class RealHAMqttApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_mqtt_subscription_uses_loop_callbacks_and_unloads(self):
        from homeassistant.components import mqtt
        from homeassistant.core import HomeAssistant, is_callback
        entity, device = identity_entries()
        config = source_module.MqttL1Config.from_registry(ENTITY_ID, entity, device)
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            source = source_module.MqttL1Source(
                config, monotonic=hass.loop.time, utc_now=lambda: datetime.now(timezone.utc),
                age_limit=lambda: 120, on_change=Mock(),
            )
            unsub = Mock()
            connection_unsub = Mock()
            # Validate the actual exported APIs before mocks replace them.
            inspect.signature(mqtt.async_subscribe).bind(hass, TOPIC, source.receive, qos=0, encoding="utf-8")
            inspect.signature(mqtt.async_subscribe_connection_status).bind(hass, source.connection_changed)
            with patch.object(mqtt, "is_connected", return_value=True), \
                    patch.object(mqtt, "async_subscribe", new=AsyncMock(return_value=unsub)) as subscribe, \
                    patch.object(mqtt, "async_subscribe_connection_status", return_value=connection_unsub) as connection:
                await source.async_start(hass)
                args = subscribe.await_args
                self.assertEqual(args.args[:2], (hass, TOPIC))
                self.assertTrue(is_callback(args.args[2]))
                self.assertTrue(is_callback(connection.call_args.args[1]))
                self.assertEqual(args.kwargs, {"qos": 0, "encoding": "utf-8"})
                source.stop()
                unsub.assert_called_once_with()
                connection_unsub.assert_called_once_with()
            await hass.async_block_till_done()


if __name__ == "__main__":
    unittest.main()
