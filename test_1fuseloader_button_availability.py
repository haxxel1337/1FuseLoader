"""Manual trial controls cannot act while automatic cycles own the lifecycle."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_config as config_fixtures
import test_1fuseloader_ha as ha_fixtures

COMPONENT = Path(__file__).parent / "custom_components" / "1fuseloader"
PACKAGE = "onefuse_button_availability_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load(name, filename):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", COMPONENT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Description:
    key: str
    name: str
    icon: str


class FakeButtonEntity:
    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1


class ServiceValidationError(Exception):
    pass


stubs = {name: ModuleType(name) for name in (
    "homeassistant", "homeassistant.components", "homeassistant.components.button",
    "homeassistant.config_entries", "homeassistant.core", "homeassistant.exceptions",
    "homeassistant.helpers", "homeassistant.helpers.entity",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform")}
stubs["homeassistant.components.button"].ButtonEntity = FakeButtonEntity
stubs["homeassistant.components.button"].ButtonEntityDescription = Description
stubs["homeassistant.config_entries"].ConfigEntry = object
stubs["homeassistant.core"].HomeAssistant = object
stubs["homeassistant.core"].callback = lambda function: function
stubs["homeassistant.exceptions"].ServiceValidationError = ServiceValidationError
stubs["homeassistant.helpers.entity"].DeviceInfo = dict
stubs["homeassistant.helpers.device_registry"].DeviceEntryType = SimpleNamespace(SERVICE="service")
stubs["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
load("const", "const.py")
with patch.dict(sys.modules, stubs):
    buttons = load("button", "button.py")


class Controller:
    def __init__(self, automatic=False):
        self.settings = SimpleNamespace(automatic_overload_cycles=automatic)
        self.listeners = []
        self.ledger = {"used_seconds": 42.0, "historical_failure": "synthetic_failure"}
        self.manual_effects = []
        self.adapter_calls = []
        self.async_start_overload_trial = AsyncMock(side_effect=lambda: self.manual_effects.append("start"))
        self.async_end_overload_trial = AsyncMock(side_effect=lambda: self.manual_effects.append("end"))
        self.async_reset_state = AsyncMock()
        self.async_tick = AsyncMock()

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def notify(self):
        for listener in list(self.listeners):
            listener()


class ButtonAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def entity(self, controller, key):
        # Deliberately stale entry options: the running policy is authoritative.
        entry = SimpleNamespace(entry_id="synthetic-entry", title="Test", options={})
        description = next(item for item in buttons.BUTTON_DESCRIPTIONS if item.key == key)
        return buttons.OneFuseButton(controller, entry, description)

    async def test_manual_button_availability_reacts_and_unsubscribes(self):
        controller = Controller()
        for key in ("start_overload_trial", "end_overload_trial"):
            with self.subTest(key=key):
                entity = self.entity(controller, key)
                self.assertTrue(entity.available)
                await entity.async_added_to_hass()
                self.assertIn(entity._handle_controller_update, controller.listeners)
                controller.settings.automatic_overload_cycles = True
                controller.notify()
                self.assertFalse(entity.available)
                self.assertEqual(entity.writes, 1)
                controller.settings.automatic_overload_cycles = False
                controller.notify()
                self.assertTrue(entity.available)
                self.assertEqual(entity.writes, 2)
                await entity.async_will_remove_from_hass()
                self.assertNotIn(entity._handle_controller_update, controller.listeners)
                controller.notify()
                self.assertEqual(entity.writes, 2)

    async def test_stale_direct_manual_presses_have_no_transaction_ledger_or_api_effect(self):
        controller = Controller(automatic=True)
        before = deepcopy(controller.ledger)
        for key in ("start_overload_trial", "end_overload_trial"):
            with self.subTest(key=key):
                entity = self.entity(controller, key)
                with self.assertRaises(ServiceValidationError):
                    await entity.async_press()
        controller.async_start_overload_trial.assert_not_awaited()
        controller.async_end_overload_trial.assert_not_awaited()
        controller.async_reset_state.assert_not_awaited()
        controller.async_tick.assert_not_awaited()
        self.assertEqual(controller.manual_effects, [])
        self.assertEqual(controller.ledger, before)
        self.assertEqual(controller.adapter_calls, [])

    async def test_default_manual_mode_still_routes_both_transactions(self):
        controller = Controller()
        for key in ("start_overload_trial", "end_overload_trial"):
            await self.entity(controller, key).async_press()
        controller.async_start_overload_trial.assert_awaited_once_with()
        controller.async_end_overload_trial.assert_awaited_once_with()
        controller.async_reset_state.assert_not_awaited()
        controller.async_tick.assert_not_awaited()
        self.assertEqual(controller.manual_effects, ["start", "end"])

    async def test_recalculate_and_internal_reset_remain_available_in_automatic_mode(self):
        controller = Controller(automatic=True)
        for key in ("recalculate_now", "reset_state"):
            entity = self.entity(controller, key)
            self.assertTrue(entity.available)
            await entity.async_press()
        controller.async_tick.assert_awaited_once_with()
        controller.async_reset_state.assert_awaited_once_with()
        controller.async_start_overload_trial.assert_not_awaited()
        controller.async_end_overload_trial.assert_not_awaited()


class HiddenLegacyHelpTests(unittest.TestCase):
    def test_hidden_help_removed_while_old_saved_settings_still_validate(self):
        keys = {"safe_l1_amps", "force_min_when_available_too_low", "unused_phase_current"}
        for filename in ("strings.json", "translations/en.json", "translations/sv.json"):
            content = json.loads((COMPONENT / filename).read_text(encoding="utf-8"))
            self.assertTrue(keys.isdisjoint(content["options"]["step"]["init"]["data"]))
        restored = config_fixtures.const.validate_options(dict(
            safe_l1_amps=23., force_min_when_available_too_low=True, unused_phase_current=0))
        self.assertEqual(restored["safe_l1_amps"], 23.)
        self.assertIs(restored["force_min_when_available_too_low"], False)
        self.assertEqual(restored["unused_phase_current"], 0)


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class RealHAButtonAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_real_entities_follow_live_policy_and_reject_stale_manual_calls(self):
        from homeassistant.exceptions import ServiceValidationError as HAValidationError

        module = importlib.import_module("custom_components.1fuseloader.button")
        controller = self.component.OneFuseLoaderController(self.hass, self.entry)
        for key in ("start_overload_trial", "end_overload_trial"):
            description = next(item for item in module.BUTTON_DESCRIPTIONS if item.key == key)
            entity = module.OneFuseButton(controller, self.entry, description)
            self.assertEqual(entity.unique_id, f"{self.entry.entry_id}_{key}")
            self.assertTrue(entity.available)
            with patch.object(entity, "async_write_ha_state", new=Mock()) as write:
                await entity.async_added_to_hass()
                controller.regulator.update_settings(replace(controller.settings,
                    peak_only_response=True, automatic_overload_cycles=True))
                controller.notify()
                self.assertFalse(entity.available)
                write.assert_called_once_with()
                ledger = deepcopy(controller.regulator.export_overload_state())
                with patch.object(controller, "async_start_overload_trial", new_callable=AsyncMock) as start, \
                     patch.object(controller, "async_end_overload_trial", new_callable=AsyncMock) as end:
                    with self.assertRaises(HAValidationError):
                        await entity.async_press()
                    start.assert_not_awaited()
                    end.assert_not_awaited()
                self.assertEqual(controller.regulator.export_overload_state(), ledger)
                controller.regulator.update_settings(replace(controller.settings,
                    automatic_overload_cycles=False))
                controller.notify()
                self.assertTrue(entity.available)
                self.assertEqual(write.call_count, 2)
                await entity.async_will_remove_from_hass()
                controller.notify()
                self.assertEqual(write.call_count, 2)


if __name__ == "__main__":
    unittest.main()
