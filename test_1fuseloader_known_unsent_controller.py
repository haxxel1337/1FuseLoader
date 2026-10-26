"""Actual controller: a proven pre-SDK refusal creates no remote authority.

All sources, IDs, UTC clocks, SDK replies and storage are synthetic. No running
Home Assistant, household history or charger network is used.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_1fuseloader_cached_native_normal_controller as normal
import test_1fuseloader_clock_provenance_adapter as raw_adapter
import test_1fuseloader_runtime as fixtures


class KnownUnsentControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = normal.CachedNativeNormalControllerTests.update_entry
    wall = normal.CachedNativeNormalControllerTests.wall
    schedule = normal.CachedNativeNormalControllerTests.schedule
    active_timers = normal.CachedNativeNormalControllerTests.active_timers
    asyncSetUp = normal.CachedNativeNormalControllerTests.asyncSetUp
    asyncTearDown = normal.CachedNativeNormalControllerTests.asyncTearDown
    set_native = normal.CachedNativeNormalControllerTests.set_native
    snapshot = normal.CachedNativeNormalControllerTests.snapshot
    install = normal.CachedNativeNormalControllerTests.install
    native_vector = normal.CachedNativeNormalControllerTests.native_vector
    grid = normal.CachedNativeNormalControllerTests.grid
    configure = normal.CachedNativeNormalControllerTests.configure
    publish = normal.CachedNativeNormalControllerTests.publish
    tick = normal.CachedNativeNormalControllerTests.tick
    pulse = normal.CachedNativeNormalControllerTests.pulse
    established = normal.CachedNativeNormalControllerTests.established
    slow_first_checkpoint = normal.CachedNativeNormalControllerTests.slow_first_checkpoint

    def local_refusal(self, *, after_entry=False):
        exception=getattr(fixtures.runtime,"CommandNotSent",RuntimeError)
        accepted=self.adapter.async_execute
        self.refuse=True
        self.callbacks=0
        async def execute(decision,on_dispatch=None):
            refused = self.refuse and decision.command == "set_limit" and decision.current_p1 > 0
            if refused and not after_entry:
                if hasattr(fixtures.runtime,"CommandNotSent"):
                    raise exception("positive_rate_capacity_exhausted",retry_after_seconds=10.)
                raise exception("positive_rate_capacity_exhausted")
            if on_dispatch is not None:
                self.assertTrue(on_dispatch())
                self.callbacks+=1
            if refused:
                raise OSError("synthetic ambiguous post-SDK failure")
            return await accepted(decision)
        self.adapter.async_execute=lambda decision:execute(decision)
        self.adapter.async_execute_with_dispatch=execute

    async def test_local_refused_renewal_retains_only_old_actual_lease_and_one_deferred_retry(self):
        r=await self.established()
        self.local_refusal()
        accepted_at=r.last_set_limit_ts
        delivery=deepcopy(self.controller._delivery.latest.public())
        old_attempt=deepcopy(self.controller.last_command_attempt)
        state=deepcopy(r.export_overload_state())
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        await self.tick()
        self.assertFalse(self.controller._protective_zero_pending)
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual(self.callbacks,0)
        self.assertEqual(r.last_set_limit_ts,accepted_at)
        self.assertEqual(self.controller.last_command_attempt,old_attempt)
        self.assertEqual(self.controller._delivery.latest.public()["sequence"],delivery["sequence"])
        self.assertEqual(self.controller._delivery.latest.public()["http_status"],delivery["http_status"])
        self.assertEqual(self.controller.last_decision.current_p1,14)
        self.assertEqual(r.export_overload_state()["normal_control"]["stops"],
            state["normal_control"]["stops"])
        retry=self.controller._local_retry_after
        self.assertEqual(retry,1100.)
        for at in (1091.,1093.,1096.):
            self.now=at
            self.grid(18.5)
            self.pulse()
            await self.tick()
            self.assertEqual(self.controller._local_retry_after,retry)
            self.assertEqual(r.last_set_limit_ts,accepted_at)
            self.assertEqual(self.adapter.calls,[])
        self.refuse=False
        self.now=1100.
        self.grid(18.5)
        self.pulse()
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls],[14])
        self.assertEqual(self.callbacks,1)
        self.assertEqual(r.last_set_limit_ts,1100.)
        self.assertFalse(self.controller._protective_zero_pending)

    async def test_failure_after_actual_sdk_boundary_is_not_treated_as_known_unsent(self):
        r=await self.established()
        self.local_refusal(after_entry=True)
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        with self.assertLogs(fixtures.runtime._LOGGER,level="WARNING"):
            await self.tick()
        self.assertEqual(self.callbacks,1)
        self.assertTrue(self.controller._protective_zero_pending)
        self.assertEqual(self.controller._delivery.latest.state,"submission_failed_unverified")
        self.assertEqual(r.last_set_limit_ts,1060.)
        self.assertEqual(self.controller.last_command_attempt["result"],"error")

    async def test_invalid_option_has_no_generation_or_transport_side_effect(self):
        self.configure()
        self.controller._cancel_positive_dispatch=Mock()
        generation=self.controller._generation
        old=dict(self.entry.options)
        with self.assertRaises(ValueError):
            await self.controller.async_set_option("peak_only_response",False)
        self.assertEqual(self.controller._generation,generation)
        self.controller._cancel_positive_dispatch.assert_not_called()
        self.assertEqual(self.entry.options,old)

    async def test_cancel_before_sdk_entry_has_no_attempt_delivery_or_protective_zero(self):
        r=await self.established()
        previous=deepcopy(self.controller.last_command_attempt)
        delivery=self.controller._delivery.latest.sequence
        entered=asyncio.Event()
        async def queued(_decision,_callback):
            entered.set()
            await asyncio.Event().wait()
        self.adapter.async_execute_with_dispatch=queued
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        task=asyncio.create_task(self.tick())
        await entered.wait()
        self.assertFalse(self.controller._dispatch_started)
        self.controller._cancel_positive_dispatch()
        await task
        self.assertFalse(self.controller._protective_zero_pending)
        self.assertEqual(self.controller.last_command_attempt,previous)
        self.assertEqual(self.controller._delivery.latest.sequence,delivery)
        self.assertEqual(r.last_set_limit_ts,1060.)
        self.assertEqual(self.adapter.calls,[])

    async def test_source_fault_preempts_local_defer_without_waiting_for_retry(self):
        await self.established()
        self.local_refusal()
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        await self.tick()
        self.adapter.telemetry_block_reason="synthetic_source_fault"
        self.now=1091.
        self.grid(18.5)
        self.pulse()
        await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls],[0])

    async def test_adapter_receipt_without_dispatch_callback_cannot_create_ack_or_lease(self):
        r=await self.established()
        async def broken(_decision,_callback):
            return object()
        self.adapter.async_execute_with_dispatch=broken
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        with self.assertLogs(fixtures.runtime._LOGGER,level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._protective_zero_pending)
        self.assertEqual(r.last_set_limit_ts,1060.)
        self.assertEqual(self.controller.last_command_attempt["boundary_scope"],"adapter_contract_unknown")
        self.assertEqual(self.controller._delivery.latest.state,"submission_failed_unverified")

    async def test_ambiguous_native_return_is_numeric_only_and_cannot_raise_normal_offer(self):
        r=await self.established()
        established=r._ev_established_value
        self.publish(1068.,"charging",18.5,14.1,receipt=1067.8,grid_at=1068.)
        self.native.observation_evidence_credit=tuple((key,key not in (182,183))
            for key in (182,183,184,185))
        self.pulse()
        await self.tick()
        inp=self.controller._read_input()
        self.assertFalse(inp.charger_increase_ready)
        self.assertEqual(dict(inp.charger_observation_evidence_credit)[182],False)
        self.assertFalse(r._ev_clock_credit)
        self.assertEqual(r._ev_established_value,established)
        self.assertEqual(r.last_target[0],14)
        self.assertEqual(self.adapter.calls,[])
        self.now=1090.
        self.grid(11.)
        self.pulse()
        await self.tick()
        self.assertEqual(r.last_target[0],14)
        self.assertFalse(any(d.current_p1>14 for d in self.adapter.calls))
        self.assertEqual(r._ev_established_value,established)

    async def test_same_normalized_switch_and_entry_listener_do_not_cancel_inflight(self):
        await self.established()
        accepted=self.adapter.async_execute
        entered,release=asyncio.Event(),asyncio.Event()
        async def in_flight(decision,on_dispatch):
            self.assertTrue(on_dispatch())
            entered.set()
            await release.wait()
            return await accepted(decision)
        self.adapter.async_execute_with_dispatch=in_flight
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        task=asyncio.create_task(self.tick())
        await entered.wait()
        generation=self.controller._generation
        await self.controller.async_set_switch("peak_only_response",True)
        listener=asyncio.create_task(self.controller.async_reconfigure())
        await asyncio.sleep(0)
        self.assertEqual(self.controller._generation,generation)
        self.assertFalse(task.done())
        self.assertFalse(self.controller._dispatch_superseded)
        self.assertFalse(self.controller._protective_zero_pending)
        for value in ("off",0,1,None):
            with self.assertRaises(ValueError):
                await self.controller.async_set_switch("peak_only_response",value)
        self.assertEqual(self.controller._generation,generation)
        release.set()
        await task
        await listener
        self.assertEqual([d.current_p1 for d in self.adapter.calls],[14])
        self.assertFalse(self.controller._protective_zero_pending)

    async def test_actual_policy_change_still_cancels_inflight_positive(self):
        await self.established()
        accepted=self.adapter.async_execute
        entered=asyncio.Event()
        async def in_flight(decision,on_dispatch):
            self.assertTrue(on_dispatch())
            entered.set()
            await asyncio.Event().wait()
            return await accepted(decision)
        self.adapter.async_execute_with_dispatch=in_flight
        self.now=1090.
        self.grid(18.5)
        self.pulse()
        task=asyncio.create_task(self.tick())
        await entered.wait()
        generation=self.controller._generation
        await self.controller.async_set_switch("active_control",False)
        await task
        self.assertEqual(self.controller._generation,generation+1)
        self.assertFalse(self.controller.settings.active_control)
        self.assertTrue(self.controller._protective_zero_pending)
        self.assertEqual(self.controller._delivery.latest.state,"superseded_unverified")

    async def test_actual_adapter_seq_zero_bootstrap_can_prearm_without_measurement_credit(self):
        raw=raw_adapter.ConductorClockTests()
        raw.setUp()
        try:
            observation=raw.client.native_current_observation
            self.assertTrue(all(value is False for _,value in observation.observation_evidence_credit))
            self.assertEqual(dict(observation.observation_sequences),{182:0,183:0,184:0,185:0})
            self.assertIsNone(observation.received_at)
            self.configure()
            self.adapter.observation_clock_identity=observation.source_identity
            self.adapter.native_current_observation=observation
            self.states["sensor.car"].state="0"
            self.states["sensor.status"].state="disconnected"
            self.now=100.
            self.grid(5.)
            inp=self.controller._read_input()
            self.assertTrue(inp.charger_increase_ready)
            self.assertIsNone(inp.charger_report_ts)
            await self.tick()
            self.assertEqual([d.current_p1 for d in self.adapter.calls],[16])
            self.assertFalse(self.controller.regulator._ev_clock_credit)
        finally:
            raw.doCleanups()

    async def test_actual_adapter_ambiguous_return_allows_separate_whole_raw_normal_rise(self):
        raw=raw_adapter.ConductorClockTests()
        raw.setUp()
        try:
            self.configure(max_charge_amps=7)
            def selected(status,grid):
                observation=raw.client.native_current_observation
                self.adapter.observation_clock_identity=observation.source_identity
                self.adapter.native_current_observation=observation
                self.states["sensor.car"].state=str(observation.current)
                self.states["sensor.status"].state=status
                self.states["sensor.status"].last_changed=self.wall(self.now-.5)
                self.grid(grid)
                self.pulse()
                return observation
            raw.now=self.wall(100.)
            raw.mono=self.now=100.
            raw.product.state["latestPulse"]=raw.now
            selected("disconnected",5.)
            await self.tick()
            self.assertEqual([d.current_p1 for d in self.adapter.calls],[7])
            raw.now=self.wall(102.)
            raw.mono=102.
            raw.product.state["latestPulse"]=raw.now
            await raw.emit({182:7.},stamp=raw.now)
            self.now=103.
            selected("charging",12.)
            await self.tick()
            self.assertEqual(self.controller.regulator.last_target[0],7)
            stamp=self.wall(104.)
            for at,value in ((104.1,7.),(104.2,8.),(104.3,7.)):
                raw.now=self.wall(at)
                raw.mono=at
                raw.product.state["latestPulse"]=raw.now
                await raw.emit({182:value},stamp=stamp)
            self.now=105.
            observation=selected("charging",5.)
            self.assertFalse(dict(observation.observation_evidence_credit)[182])
            self.assertGreater(dict(observation.observation_sequences)[182],0)
            self.entry.options["max_charge_amps"]=16
            self.controller.regulator.update_settings(replace(self.controller.settings,max_charge_amps=16))
            self.adapter.calls.clear()
            inp=self.controller._read_input()
            self.assertTrue(inp.charger_increase_ready)  # tuple is coherent and quiet
            await self.tick()
            self.assertEqual([d.current_p1 for d in self.adapter.calls],[16],self.controller.last_decision)
            self.assertFalse(self.controller.regulator._ev_clock_credit)
            self.assertEqual(self.controller.regulator.export_overload_state()["used_seconds"],0)
        finally:
            raw.doCleanups()

    async def committed_thirteen_before_lower_proposal(self):
        self.configure(enabled=False)
        for at in (100.,160.):
            self.publish(at,"awaiting_start",5.,.02,receipt=None,changed=90.)
            self.pulse()
            await self.tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls],[0,16])
        self.publish(163.,"charging",21.1,16.1,receipt=162.,grid_at=163.,changed=161.5)
        self.pulse()
        await self.tick()
        self.publish(175.,"charging",7.,2.,receipt=172.,grid_at=175.,changed=172.)
        self.native.source_timestamps=tuple((key,self.wall(164.)) for key in (182,183,184,185))
        self.pulse()
        await self.tick()
        self.now=180.
        self.grid(11.4)
        self.pulse()
        await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1,13,self.controller.last_decision)
        self.assertEqual(self.controller.regulator.last_target[0],13)
        self.adapter.calls.clear()
        return self.controller.regulator

    async def test_first_lower_proposal_while_real_checkpoint_pending_keeps_committed_thirteen(self):
        r=await self.committed_thirteen_before_lower_proposal()
        old=self.controller._budget_store
        # A new real public Store wrapper has no verified revision and must
        # complete an actual transaction; no pending/private proof is seeded.
        self.controller._budget_store=type(old)(old.store,clock=old.clock,
            read_back=old.store.async_load)
        self.slow_first_checkpoint()
        self.now=180.1
        self.grid(11.4,180.)
        self.pulse()
        with self.assertLogs(fixtures.runtime._LOGGER,level="WARNING"):
            await self.tick()
        self.assertTrue(self.controller._budget_store.verification_pending)
        self.assertEqual(self.adapter.calls,[],self.controller.last_decision)
        self.assertEqual(r.last_target[0],13)
        self.assertEqual(r.last_set_limit_ts,180.)
        self.assertEqual(self.controller.last_decision.current_p1,13)
        self.assertFalse(self.controller._protective_zero_pending)
        await self.controller._budget_store.async_wait_pending()

    async def test_permanent_checkpoint_failure_still_requests_zero(self):
        await self.committed_thirteen_before_lower_proposal()
        async def fail(_):
            raise OSError("synthetic persistent disk failure")
        old=self.controller._budget_store
        self.controller._budget_store=type(old)(old.store,clock=old.clock,
            read_back=old.store.async_load)
        old.store.async_save=fail
        self.now=180.1
        self.grid(11.4,180.)
        self.pulse()
        with self.assertLogs(fixtures.runtime._LOGGER,level="WARNING"):
            await self.tick()
        self.assertEqual(self.adapter.calls[-1].current_p1,0)
        self.assertEqual(self.controller._budget_error,"overload_budget_save_failed")


if __name__ == "__main__":
    unittest.main()
