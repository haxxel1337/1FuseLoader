"""Exercise real HA's public early-shutdown job protocol, entirely offline.

Each test owns a bare HomeAssistant with a temporary config directory and a
separate asyncio loop. No integration, broker, charger, live HA or SSH is used.
The STOP listener represents integration cleanup; MQTT itself is not started.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest


HAS_HA = importlib.util.find_spec("homeassistant") is not None
if os.environ.get("ONEFUSE_REQUIRE_HOMEASSISTANT") and not HAS_HA:
    raise RuntimeError("CI must install Home Assistant before shutdown contract tests")


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in the CI job")
class RealShutdownJobContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import CoreState, HomeAssistant

        self.CoreState = CoreState
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)

    async def asyncTearDown(self):
        try:
            if self.hass.state is not self.CoreState.stopped:
                await asyncio.wait_for(self.hass.async_stop(force=True), 10)
        finally:
            self.temp.cleanup()

    async def test_early_async_shutdown_job_completes_before_stop_cleanup(self):
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import HassJob, callback

        events = []
        sources_available = {"mqtt": True}

        async def prepare_handoff():
            events.append(("prepare_started", self.hass.state,
                           self.hass.is_stopping, sources_available["mqtt"]))
            await asyncio.sleep(0)
            events.append(("prepare_finished", self.hass.state,
                           self.hass.is_stopping, sources_available["mqtt"]))

        @callback
        def stop_cleanup(event):
            events.append(("stop_cleanup", self.hass.state,
                           self.hass.is_stopping, sources_available["mqtt"]))
            sources_available["mqtt"] = False

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_cleanup)
        self.hass.async_add_shutdown_job(HassJob(prepare_handoff))
        self.hass.set_state(self.CoreState.running)
        await asyncio.wait_for(self.hass.async_stop(), 10)

        self.assertEqual(events, [
            ("prepare_started", self.CoreState.running, False, True),
            ("prepare_finished", self.CoreState.running, False, True),
            ("stop_cleanup", self.CoreState.stopping, True, True),
        ])
        self.assertFalse(sources_available["mqtt"])
        self.assertIs(self.hass.state, self.CoreState.stopped)
        self.assertTrue(asyncio.get_running_loop().is_running())

    async def test_shutdown_job_unregister_callback_prevents_removed_job_running(self):
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import HassJob, callback

        events = []

        async def removed_job():
            events.append("removed")

        async def remaining_job():
            await asyncio.sleep(0)
            events.append("remaining")

        @callback
        def stop_cleanup(event):
            events.append("stop_cleanup")

        unregister = self.hass.async_add_shutdown_job(HassJob(removed_job))
        self.hass.async_add_shutdown_job(HassJob(remaining_job))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_cleanup)
        unregister()
        self.hass.set_state(self.CoreState.running)
        await asyncio.wait_for(self.hass.async_stop(), 10)

        self.assertEqual(events, ["remaining", "stop_cleanup"])
        self.assertIs(self.hass.state, self.CoreState.stopped)
        self.assertTrue(asyncio.get_running_loop().is_running())


if __name__ == "__main__":
    unittest.main()
