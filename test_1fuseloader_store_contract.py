"""Storage contract tests using actual Home Assistant Store and temporary files.

Run on HA 2026.10.1 and 2026.10.2 with Python 3.14. No broker, live Home
Assistant, network, charger or household configuration is accessed. Private HA
implementation functions are patched only for fault injection in these tests;
the integration uses the public Store API.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch


HAS_HA = importlib.util.find_spec("homeassistant") is not None
if os.environ.get("ONEFUSE_REQUIRE_HOMEASSISTANT") and not HAS_HA:
    raise RuntimeError("CI must install Home Assistant before running storage contract tests")


def load_persistence():
    """Load this repository's persistence wrapper without integration setup."""
    root = Path(__file__).resolve().parent
    component = root / "custom_components" / "1fuseloader" / "overload_persistence.py"
    spec = importlib.util.spec_from_file_location("onefuse_real_store_contract", component)
    if spec is None or spec.loader is None:
        raise RuntimeError("Persistence module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def exposure(**changes):
    return {
        "version": 1, "used_seconds": 120.0, "exposure_active": False,
        "transition_pending": False, "minimum_held": False,
        "failure_reason": None, **changes,
    }


class BlockingAtomicWrite:
    """Hold the first real executor write; later writes expose overlap bugs."""

    def __init__(self, loop, original):
        self.loop = loop
        self.original = original
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls += 1
            index = self.calls
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if index == 1:
                self.loop.call_soon_threadsafe(self.entered.set)
                if not self.release.wait(10):
                    raise RuntimeError("Test did not release its temporary executor write")
            return self.original(*args, **kwargs)
        finally:
            with self.lock:
                self.active -= 1


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in the CI job")
class RealStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import CoreState, HomeAssistant
        from homeassistant.helpers import storage

        self.storage = storage
        self.CoreState = CoreState
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.persistence = load_persistence()
        self.key = "1fuseloader.contract-test.overload"
        self.now = time.time()
        self.pending = {}
        self.wrappers = []
        self.wrapper = self.make_wrapper()

    async def asyncTearDown(self):
        # Every blocked-write test releases its executor gate in a finally.
        # Shielded disk transactions must finish before the temporary folder is
        # removed; cancelling the test coroutine must never strand a writer.
        for wrapper in self.wrappers:
            await wrapper.async_wait_pending()
        await self.hass.async_block_till_done()
        self.temp.cleanup()

    def writer(self):
        return self.storage.Store(self.hass, 1, self.key, atomic_writes=True)

    async def fresh_read(self):
        return await self.storage.Store(self.hass, 1, self.key, read_only=True).async_load()

    async def ledger_exists(self):
        return await self.hass.async_add_executor_job(Path(self.wrapper.store.path).exists)

    def make_wrapper(self, *, read_back=None, write_allowed=None):
        wrapper = self.persistence.OverloadBudgetStore(
            self.writer(), clock=lambda: self.now,
            read_back=read_back or self.fresh_read,
            pending_writes=self.pending, pending_key=self.key,
            write_allowed=write_allowed or (lambda: self.hass.state is not self.CoreState.stopping),
            ledger_exists=self.ledger_exists,
        )
        self.wrappers.append(wrapper)
        return wrapper

    async def test_atomic_roundtrip_reads_actual_file_and_preserves_exposure(self):
        state = exposure(used_seconds=123.5, exposure_active=True)
        original_atomic = self.storage.write_utf8_file_atomic
        original_load = self.storage.json_util.load_json
        with patch.object(self.storage, "write_utf8_file_atomic", wraps=original_atomic) as atomic, \
                patch.object(self.storage, "write_utf8_file", side_effect=AssertionError("Non-atomic write used")), \
                patch.object(self.storage.json_util, "load_json", wraps=original_load) as disk_read:
            self.assertTrue(await self.wrapper.async_save(state))
            atomic.assert_called_once()
            # A separate reader must perform I/O after Store invalidates its
            # startup cache; same-object pending _data is not proof of disk.
            disk_read.assert_called_once_with(self.wrapper.store.path)
        persisted = await self.fresh_read()
        self.assertEqual(persisted["regulator"], state)
        self.assertEqual(persisted["saved_at"], self.now)
        self.assertRegex(persisted["checkpoint_revision"], r"^[0-9a-f]{32}$")
        self.assertTrue(self.wrapper.state_is_durable(state))
        self.now += 30
        reloaded = self.make_wrapper()
        loaded = await reloaded.async_load()
        self.assertEqual(loaded["used_seconds"], 153.5)
        self.assertEqual(loaded["exposure_active"], True)

    async def test_identical_state_does_not_rewrite_but_changed_exposure_does(self):
        state = exposure(used_seconds=120.0000001)
        await self.wrapper.async_save(state)
        first = await self.fresh_read()
        with patch.object(self.storage, "write_utf8_file_atomic", wraps=self.storage.write_utf8_file_atomic) as atomic:
            self.now += 60
            self.assertFalse(await self.wrapper.async_save(deepcopy(state)))
            atomic.assert_not_called()
            changed = exposure(used_seconds=120.0000002)
            self.assertTrue(await self.wrapper.async_save(changed))
            atomic.assert_called_once()
        last = await self.fresh_read()
        self.assertNotEqual(first["checkpoint_revision"], last["checkpoint_revision"])
        self.assertEqual(last["regulator"], changed)

    async def test_actual_store_swallows_writeerror_but_wrapper_detects_old_disk_revision(self):
        # Keep semantic state and saved_at identical: a nonce is necessary to
        # distinguish a newly committed checkpoint from an older identical one.
        state = exposure()
        await self.wrapper.async_save(state)
        previous = await self.fresh_read()
        reloaded = self.make_wrapper()
        with patch.object(self.storage, "write_utf8_file_atomic", side_effect=self.storage.WriteError("injected write failure")), \
                self.assertLogs("homeassistant.helpers.storage", level="ERROR") as logs:
            with self.assertRaises(self.persistence.CheckpointVerificationError):
                await reloaded.async_save(state)
        self.assertTrue(any("Error writing config" in line for line in logs.output))
        self.assertEqual(await self.fresh_read(), previous)
        self.assertFalse(reloaded.state_is_durable(state))
        self.assertIsNone(reloaded.last_verified_revision)

    async def test_actual_store_swallows_serializationerror_but_wrapper_detects_failure(self):
        await self.wrapper.async_save(exposure())
        previous = await self.fresh_read()
        changed = exposure(used_seconds=180)
        with patch.object(self.storage.json_helper, "prepare_save_json", side_effect=self.storage.json_util.SerializationError("injected serialization failure")), \
                self.assertLogs("homeassistant.helpers.storage", level="ERROR"):
            with self.assertRaises(self.persistence.CheckpointVerificationError):
                await self.wrapper.async_save(changed)
        self.assertEqual(await self.fresh_read(), previous)
        self.assertFalse(self.wrapper.state_is_durable(changed))

    async def test_revision_mismatch_is_rejected_even_when_other_fields_match(self):
        async def stale_revision_read():
            actual = await self.fresh_read()
            actual["checkpoint_revision"] = "stale-revision"
            return actual

        wrapper = self.make_wrapper(read_back=stale_revision_read)
        with self.assertRaises(self.persistence.CheckpointVerificationError):
            await wrapper.async_save(exposure())
        self.assertFalse(wrapper.state_is_durable(exposure()))
        self.assertIsNone(wrapper.last_verified_at)

    async def test_stopping_guard_blocks_before_actual_store_can_defer_save(self):
        self.hass.set_state(self.CoreState.stopping)
        try:
            with patch.object(self.wrapper.store, "async_save", new=AsyncMock()) as save:
                with self.assertRaises(self.persistence.CheckpointVerificationError):
                    await self.wrapper.async_save(exposure())
                save.assert_not_awaited()
        finally:
            self.hass.set_state(self.CoreState.not_running)

    async def test_nonce_also_detects_actual_ha_shutdown_deferred_save(self):
        state = exposure()
        await self.wrapper.async_save(state)
        previous = await self.fresh_read()
        # Deliberately bypass the wrapper's normal stopping guard to exercise
        # Store's actual deferred-return contract and independent nonce check.
        reloaded = self.make_wrapper(write_allowed=lambda: True)
        self.hass.set_state(self.CoreState.stopping)
        try:
            with patch.object(self.storage, "write_utf8_file_atomic") as write:
                with self.assertRaises(self.persistence.CheckpointVerificationError):
                    await reloaded.async_save(state)
                write.assert_not_called()
            self.assertEqual(await self.fresh_read(), previous)
            self.assertFalse(reloaded.state_is_durable(state))
        finally:
            self.hass.set_state(self.CoreState.not_running)
            # Flush through the public API to remove the temporary final-write
            # listener registered by the deliberately deferred save.
            await reloaded.store.async_save(previous)

    async def test_timeout_preserves_pending_writer_and_serializes_new_instance(self):
        blocked = BlockingAtomicWrite(self.hass.loop, self.storage.write_utf8_file_atomic)
        second_task = None
        with patch.object(self.storage, "write_utf8_file_atomic", side_effect=blocked):
            first = asyncio.create_task(self.wrapper.async_save(exposure(used_seconds=120)))
            try:
                await asyncio.wait_for(blocked.entered.wait(), 5)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(first, 0.01)
                self.assertEqual(len(self.pending), 1)
                pending = self.pending[self.key]
                self.assertFalse(pending.cancelled())
                reloaded = self.make_wrapper()
                latest = exposure(used_seconds=240)
                second_task = asyncio.create_task(reloaded.async_save(latest))
                await asyncio.sleep(0.01)
                self.assertEqual(blocked.calls, 1)
                self.assertFalse(second_task.done())
                self.assertFalse(reloaded.state_is_durable(latest))
                blocked.release.set()
                self.assertTrue(await asyncio.wait_for(second_task, 5))
                self.assertEqual(blocked.calls, 2)
                self.assertEqual(blocked.maximum_active, 1)
                self.assertEqual((await self.fresh_read())["regulator"], latest)
                self.assertTrue(reloaded.state_is_durable(latest))
            finally:
                blocked.release.set()
                await asyncio.gather(first, *([second_task] if second_task else []), return_exceptions=True)
                await self.wrapper.async_wait_pending()

    async def test_reload_load_timeout_cannot_cancel_older_pending_write(self):
        blocked = BlockingAtomicWrite(self.hass.loop, self.storage.write_utf8_file_atomic)
        with patch.object(self.storage, "write_utf8_file_atomic", side_effect=blocked):
            first = asyncio.create_task(self.wrapper.async_save(exposure(used_seconds=180)))
            try:
                await asyncio.wait_for(blocked.entered.wait(), 5)
                reloaded = self.make_wrapper()
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(reloaded.async_load(), 0.01)
                self.assertFalse(self.pending[self.key].cancelled())
                self.assertFalse(first.done())
                self.assertEqual(blocked.calls, 1)
                blocked.release.set()
                await asyncio.wait_for(first, 5)
                loaded = await reloaded.async_load()
                self.assertEqual(loaded["used_seconds"], 180)
            finally:
                blocked.release.set()
                await asyncio.gather(first, return_exceptions=True)
                await self.wrapper.async_wait_pending()


if __name__ == "__main__":
    unittest.main()
