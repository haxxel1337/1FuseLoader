"""Verified exposure checkpoints and conservative storage-fault recovery.

Only in-memory Store/adapter doubles are used. No HA, disk ledger, MQTT, API,
charger or household commands are sent. Real Store contracts are separate.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).parent / "custom_components" / "1fuseloader" / "overload_persistence.py"
SPEC = importlib.util.spec_from_file_location("onefuse_budget_recovery_tests", PATH)
persistence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = persistence
SPEC.loader.exec_module(persistence)


def state(**changes):
    return {"version":1, "used_seconds":54.0, "exposure_active":False,
            "transition_pending":False, "minimum_held":False,
            "failure_reason":None, **changes}


class MemoryDisk:
    def __init__(self, payload=None):
        self.payload = deepcopy(payload)
        self.mode = "normal"
        self.writes = 0
        self.reads = 0
        self.active_writes = 0
        self.overlap = False
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.read_error = False

    async def async_save(self, payload):
        self.writes += 1
        self.active_writes += 1
        self.overlap |= self.active_writes > 1
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.mode == "raise":
                raise OSError("simulated storage failure")
            if self.mode in {"swallow", "defer"}:
                return
            self.payload = deepcopy(payload)
        finally:
            self.active_writes -= 1

    async def async_load(self):
        self.reads += 1
        if self.read_error:
            raise OSError("simulated storage load failure")
        return deepcopy(self.payload)


class VerifiedStoreTests(unittest.IsolatedAsyncioTestCase):
    def wrapper(self, disk, **extra):
        return persistence.OverloadBudgetStore(
            disk, clock=lambda:1000.0, read_back=disk.async_load, **extra)

    async def test_unchanged_full_inactive_state_writes_once(self):
        disk=MemoryDisk(); store=self.wrapper(disk)
        self.assertTrue(await store.async_save(state()))
        first=deepcopy(disk.payload)
        for _ in range(20):
            self.assertFalse(await store.async_save(state()))
        self.assertEqual(disk.writes,1)
        self.assertEqual(disk.payload,first)
        self.assertTrue(store.state_is_durable(state()))

    async def test_running_exposure_is_not_rounded_or_deduplicated_away(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        for used in (54.0,54.00001,54.1,55.0):
            self.assertTrue(await store.async_save(state(used_seconds=used,exposure_active=True)))
        self.assertEqual(disk.writes,4)
        self.assertEqual(disk.payload["regulator"]["used_seconds"],55)

    async def test_critical_flags_and_failure_reason_each_force_checkpoint(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        states=[state(),state(exposure_active=True),state(transition_pending=True),
                state(minimum_held=True),state(failure_reason="response_timeout")]
        revisions=[]
        for item in states:
            await store.async_save(item);revisions.append(disk.payload["checkpoint_revision"])
        self.assertEqual(len(set(revisions)),5)

    async def test_swallowed_write_error_is_not_success(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        await store.async_save(state())
        disk.mode="swallow"
        with self.assertRaises(persistence.CheckpointVerificationError):
            await store.async_save(state(used_seconds=55))
        self.assertFalse(store.state_is_durable(state(used_seconds=55)))
        self.assertEqual(disk.payload["regulator"]["used_seconds"],54)

    async def test_stale_identical_state_and_clock_need_new_revision_after_fault(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        await store.async_save(state())
        # Simulate a failed attempt before the same semantic state returns.
        disk.mode="raise"
        with self.assertRaises(OSError):await store.async_save(state(used_seconds=55))
        disk.mode="defer"
        with self.assertRaises(persistence.CheckpointVerificationError):
            await store.async_save(state())
        self.assertFalse(store.state_is_durable(state()))

    async def test_timeout_retains_writer_and_prevents_newer_write_overtaking(self):
        disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        store=self.wrapper(disk,pending_writes=gate,pending_key="one")
        first=asyncio.create_task(store.async_save(state(used_seconds=55)))
        await disk.entered.wait()
        with self.assertRaises(TimeoutError):await asyncio.wait_for(first,0.01)
        self.assertEqual(disk.active_writes,1)
        self.assertTrue(gate)
        second=asyncio.create_task(store.async_save(state(used_seconds=56)))
        await asyncio.sleep(0)
        self.assertEqual(disk.writes,1)
        disk.release.set();await second
        self.assertFalse(disk.overlap)
        self.assertEqual(disk.writes,2)
        self.assertEqual(disk.payload["regulator"]["used_seconds"],56)
        self.assertTrue(store.state_is_durable(state(used_seconds=56)))

    async def test_reload_reader_waits_for_old_controller_and_debits_downtime(self):
        disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        old=self.wrapper(disk,pending_writes=gate,pending_key="one")
        writing=asyncio.create_task(old.async_save(state(used_seconds=54,exposure_active=True)))
        await disk.entered.wait()
        new=persistence.OverloadBudgetStore(disk,clock=lambda:1010,
            read_back=disk.async_load,pending_writes=gate,pending_key="one")
        loading=asyncio.create_task(new.async_load())
        await asyncio.sleep(0)
        self.assertFalse(loading.done())
        disk.release.set();await writing
        loaded=await loading
        self.assertEqual(loaded["used_seconds"],64)
        self.assertFalse(new.state_is_durable(loaded))

    async def test_reload_load_timeout_cannot_cancel_old_writer(self):
        disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        old=self.wrapper(disk,pending_writes=gate,pending_key="one")
        writing=asyncio.create_task(old.async_save(state()))
        await disk.entered.wait()
        new=self.wrapper(disk,pending_writes=gate,pending_key="one")
        with self.assertRaises(TimeoutError):await asyncio.wait_for(new.async_load(),0.01)
        self.assertEqual(disk.active_writes,1)
        disk.release.set();await writing
        self.assertEqual((await new.async_load())["used_seconds"],54)

    async def test_old_verified_snapshot_does_not_authorize_newer_usage(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        await store.async_save(state(used_seconds=54,exposure_active=True))
        self.assertFalse(store.state_is_durable(state(used_seconds=55,exposure_active=True)))

    async def test_new_payload_is_copied_before_yielding(self):
        disk=MemoryDisk();disk.release=asyncio.Event();store=self.wrapper(disk)
        original=state();writing=asyncio.create_task(store.async_save(original))
        await disk.entered.wait();original["used_seconds"]=999
        disk.release.set();await writing
        self.assertEqual(disk.payload["regulator"]["used_seconds"],54)
        self.assertFalse(store.state_is_durable(original))

    async def test_invalid_json_state_fails_before_any_write(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        with self.assertRaises(ValueError):await store.async_save(state(used_seconds=float("nan")))
        self.assertEqual(disk.writes,0)

    async def test_stopping_does_not_claim_a_deferred_checkpoint(self):
        disk=MemoryDisk();store=self.wrapper(disk,write_allowed=lambda:False)
        with self.assertRaises(persistence.CheckpointVerificationError):await store.async_save(state())
        self.assertEqual(disk.writes,0)
        self.assertFalse(store.state_is_durable(state()))

    async def test_existing_ledger_returning_none_is_not_new_zero_allowance(self):
        disk=MemoryDisk()
        async def exists():return True
        store=self.wrapper(disk,ledger_exists=exists)
        with self.assertRaises(persistence.CheckpointVerificationError):await store.async_load()

    async def test_legacy_envelope_loads_without_revision_or_exposure_credit(self):
        disk=MemoryDisk({"version":1,"saved_at":990,"regulator":state(exposure_active=True)})
        store=self.wrapper(disk)
        loaded=await store.async_load()
        self.assertEqual(loaded["used_seconds"],64)
        self.assertFalse(store.state_is_durable(loaded))

    async def test_failed_previous_writer_can_be_retried_without_overlap(self):
        disk=MemoryDisk();store=self.wrapper(disk)
        disk.mode="raise"
        with self.assertRaises(OSError):await store.async_save(state())
        disk.mode="normal"
        self.assertTrue(await store.async_save(state()))
        self.assertTrue(store.state_is_durable(state()))
        self.assertFalse(disk.overlap)

    async def test_three_waiters_share_gate_and_only_latest_hash_is_durable(self):
        disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        wrappers=[self.wrapper(disk,pending_writes=gate,pending_key="one") for _ in range(4)]
        first=asyncio.create_task(wrappers[0].async_save(state(used_seconds=54)))
        await disk.entered.wait()
        rest=[asyncio.create_task(wrapper.async_save(state(used_seconds=55+i)))
              for i,wrapper in enumerate(wrappers[1:])]
        await asyncio.sleep(0)
        self.assertEqual(disk.writes,1)
        disk.release.set();await asyncio.gather(first,*rest)
        self.assertEqual(disk.writes,4)
        self.assertFalse(disk.overlap)
        latest=disk.payload["regulator"]
        self.assertTrue(all(wrapper.state_is_durable(latest) for wrapper in wrappers))
        older=state(used_seconds=54)
        self.assertFalse(any(wrapper.state_is_durable(older) for wrapper in wrappers))

    async def test_snapshot_wall_anchor_includes_time_waiting_for_prior_write(self):
        clock=[1000.0];disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        old=persistence.OverloadBudgetStore(disk,clock=lambda:clock[0],
            read_back=disk.async_load,pending_writes=gate,pending_key="one")
        new=persistence.OverloadBudgetStore(disk,clock=lambda:clock[0],
            read_back=disk.async_load,pending_writes=gate,pending_key="one")
        first=asyncio.create_task(old.async_save(state(used_seconds=54,exposure_active=True)))
        await disk.entered.wait();clock[0]=1001
        second=asyncio.create_task(new.async_save(state(used_seconds=55,exposure_active=True)))
        await asyncio.sleep(0);clock[0]=1010;disk.release.set()
        await asyncio.gather(first,second)
        self.assertEqual(disk.payload["saved_at"],1001)
        self.assertEqual((await new.async_load())["used_seconds"],64)

    async def test_reload_load_and_new_write_wait_for_old_transaction(self):
        disk=MemoryDisk();disk.release=asyncio.Event();gate={}
        old=self.wrapper(disk,pending_writes=gate,pending_key="one")
        new=self.wrapper(disk,pending_writes=gate,pending_key="one")
        first=asyncio.create_task(old.async_save(state(used_seconds=54)))
        await disk.entered.wait()
        loading=asyncio.create_task(new.async_load())
        writing=asyncio.create_task(new.async_save(state(used_seconds=55)))
        await asyncio.sleep(0)
        self.assertFalse(loading.done());self.assertFalse(writing.done())
        disk.release.set();await asyncio.gather(first,loading,writing)
        self.assertFalse(disk.overlap)
        self.assertEqual(disk.payload["regulator"]["used_seconds"],55)

    async def test_stale_other_wrapper_cannot_refund_newer_known_exposure(self):
        disk=MemoryDisk();gate={}
        old=self.wrapper(disk,pending_writes=gate,pending_key="one")
        new=self.wrapper(disk,pending_writes=gate,pending_key="one")
        await old.async_save(state(used_seconds=54))
        await new.async_save(state(used_seconds=56))
        with self.assertRaises(persistence.CheckpointVerificationError):
            await old.async_save(state(used_seconds=55))
        self.assertEqual(disk.payload["regulator"]["used_seconds"],56)
        self.assertFalse(old.state_is_durable(state(used_seconds=54)))

    async def test_loaded_downtime_charge_cannot_be_replaced_by_preload_usage(self):
        disk=MemoryDisk({"version":1,"saved_at":990,"regulator":state(exposure_active=True)})
        store=self.wrapper(disk)
        loaded=await store.async_load()
        self.assertEqual(loaded["used_seconds"],64)
        with self.assertRaises(persistence.CheckpointVerificationError):
            await store.async_save(state(exposure_active=True))
        self.assertEqual(disk.writes,0)


class ControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_1fuseloader_runtime as runtime_tests
        self.rt=runtime_tests
        self.store_patch=patch.object(runtime_tests.runtime,"Store",
            side_effect=lambda *args,**kwargs:runtime_tests.FakeStore(*args))
        self.store_patch.start()
        self.fixture=runtime_tests.ControllerTests(methodName="runTest")
        await self.fixture.asyncSetUp()
        self.fixture.entry.options["overload_enabled"]=True
        self.fixture.states["sensor.grid"].state="2.9"
        self.fixture.states["sensor.car"].state="0.023"
        self.fixture.states["sensor.status"].state="awaiting_start"
        self.tick=100.0
        self.fixture.hass.loop=type("Loop",(),{"time":lambda _self:self.tick})()
        self.controller=runtime_tests.runtime.OneFuseLoaderController(
            self.fixture.hass,self.fixture.entry,self.fixture.adapter)
        self.disk=MemoryDisk()
        self.store=persistence.OverloadBudgetStore(self.disk,clock=lambda:1000+self.tick,
                                                   read_back=self.disk.async_load)
        self.controller._budget_store=self.store
        self.controller.regulator.import_overload_state(state())

    async def asyncTearDown(self):
        await self.controller.async_stop()
        self.store_patch.stop()

    async def test_transient_save_fault_blocks_then_recovers_only_after_latest_checkpoint(self):
        c=self.controller;self.disk.mode="raise"
        with self.assertLogs(self.rt.runtime._LOGGER,level="WARNING") as logs:
            await c.async_tick()
        self.assertIn("OSError", " ".join(logs.output))
        self.assertEqual(c.last_decision.current_p1,0)
        self.assertEqual(c._budget_error,"overload_budget_save_failed")
        self.assertEqual(c._budget_error_type,"OSError")
        self.disk.mode="normal";self.tick=110
        await c.async_tick()
        self.assertEqual(self.disk.writes,1)  # Bounded retry backoff.
        self.assertEqual(c._budget_error,"overload_budget_save_failed")
        self.tick=116;await c.async_tick()
        self.assertIsNone(c._budget_error)
        self.assertTrue(self.store.state_is_durable(c.regulator.export_overload_state()))
        self.assertEqual(c.last_decision.current_p1,0)  # No same-tick positive authorization.
        self.assertEqual(c.regulator.export_overload_state()["used_seconds"],54)
        self.tick=117;await c.async_tick()
        self.assertEqual(c.last_decision.current_p1,0)
        self.tick=177;await c.async_tick()
        self.assertEqual(c.last_decision.current_p1,16)  # Safe offer after the buffered restart threshold.
        self.assertEqual(c.regulator.export_overload_state()["used_seconds"],54)

    async def test_swallowed_error_cannot_clear_storage_fault(self):
        c=self.controller;self.disk.mode="swallow"
        with self.assertLogs(self.rt.runtime._LOGGER,level="WARNING"):
            await c.async_tick()
        self.assertEqual(c._budget_error_type,"CheckpointVerificationError")
        self.assertEqual(c.last_decision.current_p1,0)
        self.tick=116
        with self.assertLogs(self.rt.runtime._LOGGER,level="WARNING"):
            await c.async_tick()
        self.assertIsNotNone(c._budget_error)
        self.assertFalse(self.store.state_is_durable(c.regulator.export_overload_state()))

    async def test_restore_fault_never_saves_new_zero_over_unknown_older_budget(self):
        c=self.controller;self.disk.read_error=True
        with self.assertLogs(self.rt.runtime._LOGGER,level="WARNING"):
            await c.async_start()
        self.assertEqual(c._budget_error,"overload_budget_restore_failed")
        self.assertGreaterEqual(c.regulator.export_overload_state()["used_seconds"],1800)
        self.disk.read_error=False;self.tick+=16
        await c.async_tick()
        self.assertEqual(c._budget_error,"overload_budget_restore_failed")
        self.assertEqual(self.disk.writes,0)
        self.assertIsNone(self.disk.payload)
        self.assertTrue(all(d.current_p1==0 for d in self.fixture.adapter.calls))

    async def test_latest_checkpoint_does_not_reset_existing_hard_safety_latch(self):
        c=self.controller
        c.regulator.import_overload_state(state(failure_reason="response_timeout"))
        c._budget_error="overload_budget_save_failed"
        await c.async_tick()
        self.assertIsNone(c._budget_error)
        self.assertEqual(c.regulator.export_overload_state()["failure_reason"],"response_timeout")
        self.assertEqual(c.regulator.export_overload_state()["used_seconds"],54)

    async def test_corrupt_sibling_blocks_new_zero_allowance_across_controller_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            disk=MemoryDisk();disk.path=str(Path(directory)/"exposure")
            Path(disk.path+".corrupt.2026-10-17").touch()
            async def executor(function,*args):
                return await asyncio.to_thread(function,*args)
            self.fixture.hass.async_add_executor_job=executor
            self.fixture.hass.data={}
            with patch.object(self.rt.runtime,"Store",return_value=disk):
                for _ in range(2):
                    controller=self.rt.runtime.OneFuseLoaderController(
                        self.fixture.hass,self.fixture.entry,self.fixture.adapter)
                    with self.assertRaisesRegex(RuntimeError,"Existing exposure ledger could not be loaded"):
                        await controller._budget_store.async_load()
                    self.assertEqual(disk.writes,0)

    async def test_unload_timeout_leaves_old_verification_serialized_with_reload(self):
        c=self.controller;gate={};self.disk.release=asyncio.Event()
        self.store=persistence.OverloadBudgetStore(self.disk,clock=lambda:1000,
            read_back=self.disk.async_load,pending_writes=gate,pending_key="one")
        c._budget_store=self.store
        with patch.object(self.rt.runtime,"EXPOSURE_STORE_TIMEOUT_SECONDS",0.01):
            with self.assertLogs(self.rt.runtime._LOGGER,level="WARNING"):
                await c.async_tick()
                await c.async_stop()
        self.assertEqual(self.disk.active_writes,1)
        new=persistence.OverloadBudgetStore(self.disk,clock=lambda:1001,
            read_back=self.disk.async_load,pending_writes=gate,pending_key="one")
        loading=asyncio.create_task(new.async_load())
        await asyncio.sleep(0);self.assertFalse(loading.done())
        self.disk.release.set();loaded=await loading
        self.assertEqual(loaded["used_seconds"],54)
        self.assertFalse(self.disk.overlap)


if __name__=="__main__":unittest.main()
