"""Exercise the restart marker's real HA atomic disk path, entirely offline."""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HAS_HA = importlib.util.find_spec('homeassistant') is not None
if os.environ.get('ONEFUSE_REQUIRE_HOMEASSISTANT') and not HAS_HA:
    raise RuntimeError('CI must install HA for real restart Store contracts')


@unittest.skipUnless(HAS_HA, 'Real Home Assistant is installed in CI')
class RealRestartStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import storage
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.storage = storage
        module = importlib.import_module('custom_components.1fuseloader.restart_bridge')
        controller = SimpleNamespace(hass=self.hass,
            entry=SimpleNamespace(entry_id='offline_atomic_restart'))
        self.bridge = module.GracefulRestartBridge(controller)
        self.Refused = module.BridgeRefused
        # Only exercise persistence: this unacknowledged record supplies no
        # controller authority and makes no network/device request.
        self.prepared = dict(state='prepared', nonce='a'*32, cap=7,
            ttl_seconds=300, prepared_wall=1000., expires_wall=1300.)

    async def asyncTearDown(self):
        try:
            await self.bridge.async_close()
            await self.hass.async_block_till_done()
        finally:
            self.temp.cleanup()

    async def read_disk(self):
        from homeassistant.helpers.storage import Store
        return await Store(self.hass, 1, self.bridge.storage_key,
                           read_only=True).async_load()

    async def test_marker_uses_atomic_replace_and_independent_real_disk_read(self):
        original = self.storage.write_utf8_file_atomic
        with patch.object(self.storage, 'write_utf8_file_atomic', wraps=original) as atomic, \
                patch.object(self.storage, 'write_utf8_file',
                             side_effect=AssertionError('non-atomic marker write')):
            saved = await self.bridge._save(self.prepared)
        self.assertEqual(atomic.call_count, 1)
        self.assertEqual(await self.read_disk(), saved)
        self.assertTrue(Path(self.hass.config.path('.storage',
                                                 self.bridge.storage_key)).is_file())
        self.assertFalse(self.bridge.armed)

    async def test_failed_atomic_replacement_retains_old_nonce_and_refuses_new_record(self):
        saved = await self.bridge._save(self.prepared)
        with patch.object(self.storage, 'write_utf8_file_atomic',
                          side_effect=self.storage.WriteError('offline replacement failure')), \
                patch.object(self.storage, 'write_utf8_file',
                             side_effect=AssertionError('non-atomic marker write')):
            with self.assertRaises(self.Refused):
                await self.bridge._save({**self.prepared, 'nonce':'b'*32})
        self.assertEqual(await self.read_disk(), saved)
        self.assertFalse(self.bridge.armed)


if __name__ == '__main__':
    unittest.main()
