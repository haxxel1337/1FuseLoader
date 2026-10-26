"""Synthetic pre-SDK admission and dispatch boundaries; no network/device."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_adapter as fixtures

adapter = fixtures.adapter


class LocalAdmissionAdapterTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AdapterTests.setUp

    async def test_ninth_normal_request_is_known_unsent_and_spends_no_slot(self):
        clock = [100.]
        with patch.object(adapter, "_monotonic", lambda: clock[0]):
            for _ in range(8):
                await self.client.async_execute_with_dispatch(fixtures.decision(), lambda: True)
            callback = Mock(return_value=True)
            with self.assertRaises(adapter.CommandNotSent) as err:
                await self.client.async_execute_with_dispatch(fixtures.decision(), callback)
            self.assertEqual(err.exception.code, "positive_rate_capacity_exhausted")
            self.assertGreaterEqual(err.exception.retry_after_seconds, 60.01)
            callback.assert_not_called()
            self.assertEqual(self.circuit.set_dynamic_current.await_count, 8)
            self.assertEqual(len(self.client._request_starts), 8)
            clock[0] += err.exception.retry_after_seconds
            await self.client.async_execute_with_dispatch(fixtures.decision(), callback)
            callback.assert_called_once_with()

    async def test_three_reserved_reductions_leave_twelfth_slot_for_zero(self):
        with patch.object(adapter, "_monotonic", lambda: 100.):
            for _ in range(8):
                await self.client.async_execute(fixtures.decision())
            lower = fixtures.decision(amps=7)
            lower.command_urgency = "protective_reduction"
            lower.committed_current_p1 = 16
            for _ in range(3):
                await self.client.async_execute_with_dispatch(lower, lambda: True)
            self.assertEqual(self.circuit.set_dynamic_current.await_count, 11)
            with self.assertRaises(adapter.CommandNotSent):
                await self.client.async_execute_with_dispatch(lower, lambda: True)
            await self.client.async_execute_with_dispatch(fixtures.decision(amps=0), lambda: True)
            self.circuit.set_dynamic_current.assert_awaited_with(0, 0, 0, 1)
            self.assertEqual(self.circuit.set_dynamic_current.await_count, 12)
            self.charger.pause.assert_not_awaited()

    async def test_urgency_label_cannot_grant_reserve_to_rise_renewal_or_resume(self):
        with patch.object(adapter, "_monotonic", lambda: 100.):
            for _ in range(8):
                await self.client.async_execute(fixtures.decision())
            for command, cap, previous in (("set_limit", 16, 7), ("set_limit", 7, 7),
                                           ("resume", 7, 16), ("set_limit", 8, 16)):
                decision = fixtures.decision(command=command, amps=cap)
                decision.command_urgency = "protective_reduction"
                decision.committed_current_p1 = previous
                with self.subTest(command=command, cap=cap, previous=previous):
                    with self.assertRaises(adapter.CommandNotSent):
                        await self.client.async_execute_with_dispatch(decision, lambda: True)
            self.assertEqual(self.circuit.set_dynamic_current.await_count, 8)

    async def test_callback_is_after_queue_and_before_sdk_and_receipt_origin(self):
        clock, order = [100.], []
        await self.client._request_lock.acquire()
        def dispatch():
            order.append("dispatch")
            return True
        async def sdk(*_):
            order.append("sdk")
            clock[0] = 108.
            return self.response
        self.circuit.set_dynamic_current.side_effect = sdk
        with patch.object(adapter, "_monotonic", lambda: clock[0]):
            task = asyncio.create_task(self.client.async_execute_with_dispatch(fixtures.decision(), dispatch))
            await asyncio.sleep(0)
            self.assertEqual(order, [])
            clock[0] = 105.
            self.client._request_lock.release()
            receipt = await task
        self.assertEqual(order, ["dispatch", "sdk"])
        self.assertEqual(receipt.request_started_monotonic, 105.)

    async def test_rejected_callback_is_unsent_but_sdk_failure_is_unknown(self):
        with self.assertRaises(adapter.CommandNotSent):
            await self.client.async_execute_with_dispatch(fixtures.decision(), lambda: False)
        self.circuit.set_dynamic_current.assert_not_awaited()
        self.assertEqual(len(self.client._request_starts), 0)
        self.circuit.set_dynamic_current.side_effect = RuntimeError("PRIVATE_REMOTE_TEXT")
        with self.assertRaises(adapter.EaseeAdapterError) as err:
            await self.client.async_execute_with_dispatch(fixtures.decision(), lambda: True)
        self.assertNotIsInstance(err.exception, adapter.CommandNotSent)
        self.assertNotIn("PRIVATE_REMOTE_TEXT", str(err.exception))

    async def test_resume_counts_one_circuit_slot_and_pause_uses_separate_sdk_scope(self):
        # In both supported SDKs Circuit.set_dynamic_current has its own12/60
        # throttler. Charger.pause/resume use client.post directly (general
        # 500/300 quota), so resume has one circuit write plus a separate POST.
        with patch.object(adapter, "_monotonic", lambda: 100.):
            for _ in range(7):
                await self.client.async_execute(fixtures.decision())
            await self.client.async_execute(fixtures.decision(command="pause"))
            self.assertEqual(len(self.client._request_starts),7)
            await self.client.async_execute(fixtures.decision(command="resume"))
            self.assertEqual(len(self.client._request_starts),8)
            self.assertEqual(self.circuit.set_dynamic_current.await_count,8)
            self.charger.pause.assert_awaited_once_with()
            self.charger.resume.assert_awaited_once_with()
            callback=Mock(return_value=True)
            with self.assertRaises(adapter.CommandNotSent):
                await self.client.async_execute_with_dispatch(
                    fixtures.decision(command="resume"),callback)
            callback.assert_not_called()
            self.assertEqual(len(self.client._request_starts),8)
            self.assertEqual(self.charger.resume.await_count,1)


if __name__ == "__main__":
    unittest.main()
