"""A slow, real serialized Store transaction is not a completed write failure."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from time import monotonic
import unittest

import test_1fuseloader_startup_owner_runtime as owners
import test_1fuseloader_runtime as fixtures


class CheckpointPendingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = owners.StartupOwnerRuntimeTests.update_entry
    wall = owners.StartupOwnerRuntimeTests.wall
    schedule = owners.StartupOwnerRuntimeTests.schedule
    active_timers = owners.StartupOwnerRuntimeTests.active_timers
    asyncSetUp = owners.StartupOwnerRuntimeTests.asyncSetUp
    set_native = owners.StartupOwnerRuntimeTests.set_native
    snapshot = owners.StartupOwnerRuntimeTests.snapshot
    install = owners.StartupOwnerRuntimeTests.install
    native_vector = owners.StartupOwnerRuntimeTests.native_vector
    grid = owners.StartupOwnerRuntimeTests.grid
    publish = owners.StartupOwnerRuntimeTests.publish
    bootstrap = owners.StartupOwnerRuntimeTests.bootstrap

    async def asyncTearDown(self):
        self.controller._stopped = True
        await self.controller._budget_store.async_wait_pending()
        await owners.StartupOwnerRuntimeTests.asyncTearDown(self)

    async def accepted_normal_cap(self):
        regulator = await self.bootstrap()
        # Complete the distinct startup response before exercising an ordinary
        # lower offer; an uncompleted ramp owner must remain hard no-write.
        for at in (162., 164.):
            self.publish(at, 22.4, 14., receipt=at-.2)
            await self.controller.async_tick()
        self.assertIsNone(regulator._positive_cap)
        self.adapter.calls.clear()
        return regulator

    async def steady_normal_cap(self):
        for at in (100., 160.):
            self.publish(at, 4., .012, status="awaiting_start")
            await self.controller.async_tick()
        regulator = self.controller.regulator
        self.assertEqual(regulator.last_target[0], 16)
        for at in (162., 164.):
            self.publish(at, 20.1, 16.1, receipt=at-.2)
            await self.controller.async_tick()
        self.assertIsNone(regulator._positive_cap)
        self.adapter.calls.clear()
        return regulator

    async def delayed_store(self, *, fail=False, completed_startup=True, steady=False, aged_cache=False):
        regulator = await (self.steady_normal_cap() if steady else
            self.accepted_normal_cap() if completed_startup else self.bootstrap())
        if aged_cache:
            self.publish(170., 13., 8., receipt=169.7)
            await self.controller.async_tick()
            self.assertEqual(regulator.last_target[0], 16)
            self.assertEqual(regulator.last_set_limit_ts, 160.)
            self.adapter.calls.clear()
        self.controller._handle_source_change = fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(
            self.controller)
        # A conservative synthetic debit requires a genuine new checkpoint;
        # it grants no new allowance and is not refunded during the test.
        regulator._overload_used_seconds += .25
        store = self.controller._budget_store
        original = store.store.async_save
        self.write_started = asyncio.Event()
        self.write_finished = asyncio.Event()
        self.writes = []

        async def slow(payload):
            self.writes.append(deepcopy(payload))
            self.write_started.set()
            await asyncio.sleep(2.5)
            try:
                if fail:
                    raise OSError("synthetic permanent write failure")
                await original(payload)
            finally:
                self.write_finished.set()

        store.store.async_save = slow
        return regulator, store, original

    async def test_real_two_point_five_second_store_does_not_zero_raw_bounded_normal_decrease(self):
        regulator, store, original = await self.delayed_store()
        self.publish(170., 13., .012, receipt=169.7)
        before = regulator.export_overload_state()["used_seconds"]
        began = monotonic()
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertLess(monotonic()-began, 2.4)
        self.assertEqual([item.current_p1 for item in self.adapter.calls], [10],
                         [(item.current_p1, item.mode, item.reason) for item in self.adapter.calls])
        self.assertEqual(self.controller._budget_error, "overload_budget_save_pending")
        self.assertTrue(store.verification_pending)
        self.assertFalse(self.write_finished.is_set())
        self.assertEqual(regulator.last_target[0], 10)
        self.assertEqual(regulator.export_overload_state()["used_seconds"], before)
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertTrue(self.write_finished.is_set())
        self.assertTrue(all(0 < item.current_p1 <= 10 and 13+item.current_p1 <= 23
                            for item in self.adapter.calls),
                        [(item.current_p1, item.mode, item.reason) for item in self.adapter.calls])
        self.assertIsNone(self.controller._budget_error)
        self.assertTrue(store.state_is_durable(regulator.export_overload_state()))

    async def test_real_pending_write_still_blocks_new_overload_permission_at_two_seconds(self):
        regulator, store, original = await self.delayed_store()
        self.publish(170., 30., 14., receipt=169.7)
        began = monotonic()
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertLess(monotonic()-began, 2.4)
        self.assertEqual([item.current_p1 for item in self.adapter.calls], [0])
        self.assertTrue(store.verification_pending)
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls),
                        [(item.current_p1, item.mode, item.reason) for item in self.adapter.calls])
        self.assertTrue(regulator._restart_required)

    async def test_protective_zero_retires_pending_overload_before_late_checkpoint_completion(self):
        regulator, store, original = await self.delayed_store()
        self.publish(170., 30., 14., receipt=169.7)
        planned = regulator.evaluate(self.controller._read_input())
        self.assertEqual(planned.current_p1, 7)
        self.assertIsNotNone(regulator._overload_transition_started)
        # The dispatcher has an unresolved protective-zero obligation from a
        # coalesced transient fault. Inputs are healthy again before this tick;
        # its forced zero must still retire the original pure response owner.
        self.controller._protective_zero_pending = True
        lifetime = regulator.export_overload_state()["used_seconds"]
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual([item.current_p1 for item in self.adapter.calls], [0])
        self.assertIsNone(regulator._overload_transition_started)
        self.assertTrue(regulator._restart_required)
        self.assertEqual(regulator.effective_overload_failure_reason, "protective_zero_preempts_positive")
        self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], lifetime)
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls))
        self.assertIsNone(regulator._overload_transition_started)
        self.assertTrue(regulator._restart_required)

    async def test_source_fault_or_hard_peak_during_pending_cannot_dispatch_old_normal_offer(self):
        for kind in ("source", "peak"):
            with self.subTest(kind=kind):
                if kind == "peak":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                regulator, store, original = await self.delayed_store()
                self.publish(170., 13., .012, receipt=169.7)
                with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
                    task = asyncio.create_task(self.controller.async_tick())
                    await self.write_started.wait()
                    if kind == "source":
                        self.native.error = "synthetic native source fault"
                    else:
                        self.publish(171., 38.1, .012)
                    self.controller._handle_source_change(None)
                    await task
                    await asyncio.sleep(.05)
                self.assertTrue(self.adapter.calls)
                self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls))
                store.store.async_save = original
                await store.async_wait_pending()
                await asyncio.sleep(.05)
                self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls))
                self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], .25)

    async def test_completed_io_error_is_not_hidden_as_pending_verification(self):
        regulator, store, original = await self.delayed_store(fail=True)
        self.publish(170., 13., .012, receipt=169.7)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
            await self.write_finished.wait()
            await asyncio.sleep(.05)
        self.assertEqual(self.adapter.calls[0].current_p1, 10)
        self.assertEqual(self.adapter.calls[-1].current_p1, 0)
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")
        self.assertIsNotNone(self.controller._budget_error_type)
        self.assertFalse(store.state_is_durable(regulator.export_overload_state()))
        store.store.async_save = original

    async def test_pending_retention_cannot_renew_lease_or_resume(self):
        regulator, store, original = await self.delayed_store()
        self.publish(170., 8.4, .012, receipt=169.7)
        accepted_at = regulator.last_set_limit_ts
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        self.assertEqual(regulator.last_target[0], 14)
        self.assertIsNone(self.controller.last_decision.command)
        self.controller._stopped = True
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.calls, [])

    async def test_real_pending_store_retains_centrally_qualified_partial_ramp_without_any_write(self):
        regulator, store, original = await self.delayed_store(completed_startup=False)
        self.publish(170., 13., 1.8, receipt=167., grid_at=169.)
        accepted_at = regulator.last_set_limit_ts
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        decision = self.controller.last_decision
        self.assertEqual(decision.mode, "positive_ramp_hold")
        self.assertEqual(decision.current_p1, 14)
        self.assertEqual((decision.command, decision.service, decision.should_call_service), (None, None, False))
        deadline = decision.positive_response_deadline_ts
        self.assertIsNotNone(deadline)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        self.assertTrue(store.verification_pending)
        self.assertGreater(13+14, 23)  # This is no dispatch/raw-subtraction exception.
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        self.assertEqual(self.controller.last_decision.positive_response_deadline_ts, deadline)

    async def test_pending_partial_ramp_cannot_hide_completed_io_failure(self):
        regulator, store, original = await self.delayed_store(fail=True, completed_startup=False)
        self.publish(170., 13., 1.8, receipt=167., grid_at=169.)
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
            self.assertEqual(self.adapter.calls, [])
            await self.write_finished.wait()
            await asyncio.sleep(.05)
        self.assertEqual([item.current_p1 for item in self.adapter.calls], [0])
        self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")
        self.assertTrue(regulator._restart_required)
        store.store.async_save = original

    async def test_pending_partial_ramp_rechecks_source_peak_and_original_deadline_after_wait(self):
        for fault in ("source", "peak", "deadline"):
            with self.subTest(fault=fault):
                if fault != "source":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                regulator, store, original = await self.delayed_store(completed_startup=False)
                self.publish(170., 13., 1.8, receipt=167., grid_at=169.)
                with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
                    task = asyncio.create_task(self.controller.async_tick())
                    await self.write_started.wait()
                    if fault == "source":
                        self.native.error = "synthetic invalid native source"
                    elif fault == "peak":
                        self.publish(171., 38.1, 1.8, receipt=170.8)
                    else:
                        self.publish(195., 13., 1.8, receipt=167., grid_at=195.)
                    self.controller._handle_source_change(None)
                    await task
                    await asyncio.sleep(.05)
                self.assertTrue(self.adapter.calls)
                self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls))
                store.store.async_save = original
                await store.async_wait_pending()
    async def test_pending_predicate_rejects_rise_retry_expired_lease_and_unbounded_offer(self):
        regulator = await self.accepted_normal_cap()
        self.publish(170., 13., .012, receipt=169.7)
        inp = self.controller._read_input()
        decision = regulator.evaluate(inp)
        self.assertEqual(decision.current_p1, 10)
        self.controller._budget_error = "overload_budget_save_pending"
        store = self.controller._budget_store
        gate = asyncio.create_task(asyncio.Event().wait())
        store._gate.pending = gate
        try:
            self.assertTrue(self.controller._pending_checkpoint_allows_normal(decision, inp))
            for changed in (replace(decision, current_p1=15), replace(decision, command="resume"),
                            replace(decision, current_p1=11),
                            replace(decision, mode="normal", current_p1=14,
                                    command=None, should_call_service=False)):
                self.assertFalse(self.controller._pending_checkpoint_allows_normal(changed, inp))
            previous = regulator.last_set_limit_ts
            regulator.last_set_limit_ts = 100.
            self.assertFalse(self.controller._pending_checkpoint_allows_normal(decision, inp))
            regulator.last_set_limit_ts = previous
            regulator._uncertainty_retry_submission_pending = True
            self.assertFalse(self.controller._pending_checkpoint_allows_normal(decision, inp))
        finally:
            gate.cancel()
            await asyncio.gather(gate, return_exceptions=True)
            store._gate.pending = None

    async def test_real_pending_store_retains_steady_normal_upper_bound_without_renewal(self):
        regulator, store, original = await self.delayed_store(steady=True)
        self.publish(190., 13., 8., receipt=189.7)
        planned = regulator.evaluate(self.controller._read_input())
        self.assertEqual((planned.mode, planned.current_p1, planned.command), ("normal", 16, "set_limit"))
        self.assertTrue(planned.should_call_service)
        self.assertAlmostEqual(planned.house_load_l1, 5.)
        accepted_at = regulator.last_set_limit_ts
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertEqual((self.controller.last_decision.command, self.controller.last_decision.service,
                          self.controller.last_decision.should_call_service), (None, None, False))
        self.assertEqual(regulator.last_set_limit_ts, accepted_at)
        self.assertTrue(store.verification_pending)
        self.controller._stopped = True
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.calls, [])

    async def test_steady_normal_retention_cannot_hide_failure_expiry_source_or_peak(self):
        for fault in ("io", "lease", "source", "future", "peak"):
            with self.subTest(fault=fault):
                if fault != "io":
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                regulator, store, original = await self.delayed_store(steady=True, fail=fault == "io")
                self.publish(190., 13., 8., receipt=189.7)
                lifetime = regulator.export_overload_state()["used_seconds"]
                with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
                    task = asyncio.create_task(self.controller.async_tick())
                    await self.write_started.wait()
                    if fault == "lease":
                        self.publish(220., 13., 8., receipt=219.7)
                    elif fault == "source":
                        self.native.error = "synthetic invalid native source"
                    elif fault == "future":
                        self.native.received_at = self.wall(200.)
                        self.native.observation_report_times = ((182, self.wall(200.)), (183, self.wall(200.)))
                    elif fault == "peak":
                        self.publish(191., 38.1, 8., receipt=190.7)
                    if fault != "io":
                        self.controller._handle_source_change(None)
                    await task
                    if fault == "io":
                        self.assertEqual(self.adapter.calls, [])
                    store.store.async_save = original
                    await self.write_finished.wait()
                    await asyncio.sleep(.05)
                self.assertTrue(self.adapter.calls)
                self.assertTrue(all(item.current_p1 == 0 for item in self.adapter.calls))
                self.assertTrue(regulator._restart_required)
                self.assertGreaterEqual(regulator.export_overload_state()["used_seconds"], lifetime)
                if fault == "io":
                    self.assertEqual(self.controller._budget_error, "overload_budget_save_failed")

    async def test_pending_steady_native_cache_older_than_alignment_cannot_create_false_zero(self):
        regulator, store, original = await self.delayed_store(steady=True, aged_cache=True)
        native_before = deepcopy(self.native)
        self.now = 200.
        self.grid(13., 200.)  # No new 8 A observation or synthesized EV event.
        inp = self.controller._read_input()
        self.assertGreater(inp.wall_now_ts-inp.charger_report_ts, self.controller.settings.measurement_settle_seconds)
        planned = regulator.evaluate(inp)
        self.assertEqual((planned.mode, planned.current_p1, planned.command), ("normal", 16, "set_limit"))
        with self.assertLogs(fixtures.runtime._LOGGER, level="WARNING"):
            await self.controller.async_tick()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertEqual((self.controller.last_decision.command, self.controller.last_decision.should_call_service),
                         (None, False))
        self.assertEqual(regulator.last_set_limit_ts, 160.)
        self.assertEqual(self.native.observation_report_times, native_before.observation_report_times)
        self.assertEqual(self.native.observation_sequences, native_before.observation_sequences)
        self.assertTrue(store.verification_pending)
        self.controller._stopped = True
        store.store.async_save = original
        await store.async_wait_pending()
        await asyncio.sleep(.05)
        self.assertEqual(self.adapter.calls, [])

    def test_storage_failure_and_pending_state_survive_recorder_compaction(self):
        actual = dict(budget_storage_error="overload_budget_save_pending",
            budget_storage_error_type="TimeoutError", budget_verification_pending=True,
            budget_latest_state_durable=False, budget_checkpoint_verified_at_utc="2026-01-01T00:00:00+00:00")
        attributes = {**actual, **{f"optional_source_detail_{i}": "x"*6000 for i in range(40)}}
        compact = fixtures.runtime._compact_state_attributes(attributes)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=True).encode()),
                             fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)
        for key, value in actual.items():
            self.assertEqual(compact[key], value)


if __name__ == "__main__":
    unittest.main()
