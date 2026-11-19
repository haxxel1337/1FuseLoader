"""Managed-restart action tests with real Home Assistant service dispatch."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


HAS_HA = importlib.util.find_spec("homeassistant") is not None
if os.environ.get("ONEFUSE_REQUIRE_HOMEASSISTANT") and not HAS_HA:
    raise RuntimeError("CI must install Home Assistant for the managed-restart action tests")


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in CI")
class ManagedRestartServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.core import Context, CoreState, HomeAssistant

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.set_state(CoreState.running)
        self.component = importlib.import_module("custom_components.1fuseloader")
        self.bridge = importlib.import_module("custom_components.1fuseloader.restart_bridge")
        self.admin = Context(user_id="offline-admin")
        self.user = Context(user_id="offline-user")
        self.hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(side_effect=lambda user_id: SimpleNamespace(
                is_admin=user_id == self.admin.user_id
            ))
        )
        self.entry_state = ConfigEntryState
        self.entries = [SimpleNamespace(entry_id="charger-a", state=ConfigEntryState.LOADED)]
        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: self.entries)
        self.trace = []
        self.controller = SimpleNamespace(
            async_restart_with_charging=AsyncMock(side_effect=self._verified)
        )
        self.hass.data["1fuseloader"] = {"charger-a": self.controller}
        self.hass.services.async_register("homeassistant", "check_config", self._check_config)
        self.hass.services.async_register("homeassistant", "restart", self._restart)
        await self.component.async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        await self.hass.async_block_till_done()

    async def _check_config(self, call):
        self.trace.append(("check_config", call.context))

    async def _restart(self, call):
        self.trace.append(("restart", call.context))

    async def _verified(self, request_restart):
        self.trace.append(("verified", None))
        await request_restart()

    async def _call(self, data=None, *, context):
        await self.hass.services.async_call(
            "1fuseloader", "restart_with_charging", data, blocking=True, context=context
        )

    async def test_action_checks_config_and_waits_for_verification_before_core_restart(self):
        await self._call(context=self.admin)
        self.assertEqual(self.trace, [
            ("check_config", self.admin), ("verified", None), ("restart", self.admin)
        ])

    async def test_real_core_restart_service_rechecks_config_at_its_own_stop_boundary(self):
        from homeassistant import config
        from homeassistant.components.homeassistant import async_set_stop_handler
        from homeassistant.components.homeassistant.services import async_setup_services

        async def check_config(hass):
            self.assertIs(hass, self.hass)
            self.trace.append(("config_validation", None))

        async def stop(hass, restart):
            self.assertIs(hass, self.hass)
            self.assertTrue(restart)
            self.trace.append(("stop_handler", None))

        async_setup_services(self.hass)
        async_set_stop_handler(self.hass, stop)
        with patch.object(config, "async_check_ha_config_file", side_effect=check_config):
            await self._call(context=self.admin)
        self.assertEqual(self.trace, [
            ("config_validation", None), ("verified", None),
            ("config_validation", None), ("stop_handler", None)
        ])

    async def test_action_does_not_restart_if_bridge_refuses(self):
        from homeassistant.exceptions import HomeAssistantError

        self.controller.async_restart_with_charging.side_effect = self.bridge.BridgeRefused(
            "bridge_managed_physical_timeout"
        )
        with self.assertRaisesRegex(HomeAssistantError, "bridge_managed_physical_timeout"):
            await self._call(context=self.admin)
        self.assertEqual(self.trace, [("check_config", self.admin)])

    async def test_action_aborts_before_touching_charging_if_core_config_check_fails(self):
        from homeassistant.exceptions import HomeAssistantError

        async def invalid_config(call):
            raise HomeAssistantError("Invalid offline config")

        self.hass.services.async_register("homeassistant", "check_config", invalid_config)
        with self.assertRaisesRegex(HomeAssistantError, "Invalid offline config"):
            await self._call(context=self.admin)
        self.controller.async_restart_with_charging.assert_not_awaited()
        self.assertEqual(self.trace, [])

    async def test_action_rejects_non_admin_and_no_user(self):
        from homeassistant.exceptions import Unauthorized

        for context in (self.user, None):
            with self.subTest(context=context), self.assertRaises(Unauthorized):
                await self._call(context=context)
        self.assertEqual(self.trace, [])
        self.controller.async_restart_with_charging.assert_not_awaited()

    async def test_action_rejects_unrecognized_fields_and_invalid_entry_ids(self):
        import probatio

        for data in ({"safe_mode": True}, {"entry_id": 42}):
            with self.subTest(data=data), self.assertRaises(probatio.Invalid):
                await self._call(data, context=self.admin)
        self.assertEqual(self.trace, [])

    async def test_multiple_entries_require_explicit_selection(self):
        from homeassistant.exceptions import ServiceValidationError

        self.entries.append(SimpleNamespace(entry_id="charger-b", state=self.entry_state.LOADED))
        second = SimpleNamespace(async_restart_with_charging=AsyncMock(side_effect=self._verified))
        self.hass.data["1fuseloader"]["charger-b"] = second
        with self.assertRaisesRegex(ServiceValidationError, "Select one"):
            await self._call(context=self.admin)
        self.assertEqual(self.trace, [])
        await self._call({"entry_id": "charger-b"}, context=self.admin)
        self.controller.async_restart_with_charging.assert_not_awaited()
        second.async_restart_with_charging.assert_awaited_once()

    async def test_controller_removed_during_check_prevents_preparation(self):
        from homeassistant.exceptions import ServiceValidationError

        async def remove_controller(call):
            self.hass.data["1fuseloader"].clear()

        self.hass.services.async_register("homeassistant", "check_config", remove_controller)
        with self.assertRaisesRegex(ServiceValidationError, "controller is not ready"):
            await self._call(context=self.admin)
        self.controller.async_restart_with_charging.assert_not_awaited()
        self.assertEqual(self.trace, [])

    async def test_concurrent_restart_is_rejected_while_first_action_is_preparing(self):
        from homeassistant.exceptions import HomeAssistantError

        started = asyncio.Event()
        proceed = asyncio.Event()

        async def prepare_once(request_restart):
            started.set()
            await proceed.wait()
            await self._verified(request_restart)

        self.controller.async_restart_with_charging.side_effect = prepare_once
        first = asyncio.create_task(self._call(context=self.admin))
        try:
            await asyncio.wait_for(started.wait(), 1)
            with self.assertRaisesRegex(HomeAssistantError, "already being prepared"):
                await self._call(context=self.admin)
        finally:
            proceed.set()
            await first
        self.assertEqual(self.trace, [
            ("check_config", self.admin), ("verified", None), ("restart", self.admin)
        ])


if __name__ == "__main__":
    unittest.main()
