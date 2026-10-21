"""Command correlation and protection races, without network or HA writes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_runtime as fixtures

delivery = fixtures.load("command_delivery_tests", "command_delivery.py")


def receipt(ticks="100", status=202):
    return SimpleNamespace(http_status=status, correlation_ids=((22, ticks),) if ticks else ())


def event(ticks="100", accepted=True, kind=22):
    return SimpleNamespace(command_type=kind, ticks=ticks, was_accepted=accepted)


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.tracker = delivery.CommandDeliveryTracker()

    def begin(self, phases=(0, 0, 0), started=1, ticks="100"):
        record = self.tracker.begin("set_limit", phases, started)
        self.tracker.accepted(record, receipt(ticks), started + 1)
        return record

    def evidence(self, **overrides):
        values = dict(phases=(0, 0, 0), after_submission=True, report_age=1,
                      ev_current=0, healthy=True, offline_zero=True, max_age=120)
        values.update(overrides)
        return values

    def test_ack_before_http_response_matches_exact_ticks(self):
        record = self.tracker.begin("set_limit", (7, 0, 0), 1)
        self.assertFalse(self.tracker.delivery(event(), 2))
        self.tracker.accepted(record, receipt(), 3)
        self.assertTrue(record.vendor_accepted)
        self.assertEqual(record.delivery_received, 2)

    def test_old_zero_ack_does_not_confirm_new_positive(self):
        old = self.begin()
        current = self.begin((7, 0, 0), 4, "101")
        self.assertFalse(self.tracker.delivery(event(), 5))
        self.assertTrue(old.vendor_accepted)
        self.assertIsNone(current.vendor_accepted)

    def test_wrong_kind_or_ticks_cannot_confirm(self):
        record = self.begin()
        for value in (event("101"), event(kind=23), event(ticks="bad"), event(accepted="true")):
            self.assertFalse(self.tracker.delivery(value, 3))
        self.assertIsNone(record.vendor_accepted)

    def test_reused_correlation_is_not_a_new_confirmation(self):
        self.begin()
        current = self.begin((7, 0, 0), 4)
        self.assertEqual(current.correlations, ())
        self.tracker.delivery(event(), 6)
        self.assertIsNone(current.vendor_accepted)

    def test_duplicate_cannot_renew_receipt_or_reverse_rejection(self):
        record = self.begin()
        self.tracker.delivery(event(accepted=False), 3)
        self.tracker.delivery(event(accepted=True), 30)
        self.assertFalse(record.vendor_accepted)
        self.assertEqual(record.delivery_received, 3)
        self.assertFalse(self.tracker.can_skip_zero(**self.evidence()))

    def test_uncorrelated_200_stays_unverified(self):
        record = self.tracker.begin("set_limit", (7, 0, 0), 1)
        self.tracker.accepted(record, receipt(None, 200), 2)
        self.tracker.delivery(event(), 3)
        self.assertIsNone(record.vendor_accepted)
        self.assertFalse(record.public()["physical_response_verified"])
        self.assertFalse(record.public()["device_ttl_verified"])

    def test_early_ack_before_request_or_expired_is_not_matching(self):
        self.tracker.delivery(event(), 0)
        record = self.begin(started=2)
        self.assertIsNone(record.vendor_accepted)
        self.tracker.reset()
        self.tracker.delivery(event(), 2)
        record = self.tracker.begin("set_limit", (7, 0, 0), 70)
        self.tracker.accepted(record, receipt(), 72)
        self.assertIsNone(record.vendor_accepted)

    def test_cancelled_request_cannot_gain_late_acceptance(self):
        record = self.begin()
        self.tracker.failed(record, superseded=True)
        self.tracker.delivery(event(), 4)
        self.assertIsNone(record.vendor_accepted)
        self.assertFalse(self.tracker.can_skip_zero(**self.evidence()))

    def test_only_confirmed_fresh_all_phase_zero_can_suppress(self):
        self.begin()
        self.assertFalse(self.tracker.can_skip_zero(**self.evidence()))
        self.tracker.delivery(event(), 3)
        self.assertTrue(self.tracker.can_skip_zero(**self.evidence()))
        for invalid in (dict(phases=(0, 1, 0)), dict(after_submission=False), dict(report_age=121),
                        dict(report_age=-1), dict(ev_current=None), dict(ev_current=1),
                        dict(healthy=False), dict(offline_zero=False)):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.tracker.can_skip_zero(**self.evidence(**invalid)))

    def test_positive_attempt_or_generation_reset_invalidates_zero(self):
        self.begin()
        self.tracker.delivery(event(), 3)
        self.begin((7, 0, 0), 4, "101")
        self.assertFalse(self.tracker.can_skip_zero(**self.evidence()))
        self.tracker.reset()
        self.assertIsNone(self.tracker.latest)

    def test_public_diagnostics_omit_raw_correlations(self):
        self.begin(ticks="638123456789000001")
        self.assertNotIn("638123456789000001", str(self.tracker.diagnostics()))

    def test_old_unresolved_zero_survives_new_zero_and_bounded_history(self):
        self.begin()
        self.begin(started=4, ticks="101")
        self.tracker.delivery(event("101"), 6)
        self.assertTrue(self.tracker.unresolved_correlated_zero)
        self.tracker.delivery(event("100"), 7)
        self.assertFalse(self.tracker.unresolved_correlated_zero)
        self.begin(started=8, ticks="102")
        for index in range(40):
            self.begin((7, 0, 0), 10 + index, str(200 + index))
        self.assertTrue(self.tracker.unresolved_correlated_zero)


class DispatchRaceTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry
    fresh_control_reports = fixtures.ControllerTests.fresh_control_reports

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.fresh_control_reports()

    async def asyncTearDown(self):
        await self.controller.async_stop()

    async def test_new_hard_load_cancels_positive_without_waiting_for_transport_gate(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        posted = []

        async def send(decision):
            if decision.current_p1:
                started.set()
                await gate.wait()
            posted.append(decision.current_p1)
        self.adapter.async_execute = send
        task = asyncio.create_task(self.controller.async_tick())
        await asyncio.wait_for(started.wait(), 1)
        self.states["sensor.grid"].state = "40"
        self.fresh_control_reports()
        self.controller._handle_source_change(None)
        await asyncio.wait_for(task, 1)
        if self.controller._refresh_task:
            await asyncio.wait_for(self.controller._refresh_task, 1)
        gate.set()
        await asyncio.sleep(0)
        self.assertEqual(posted, [0])
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 0)

    async def test_late_2xx_after_cancellation_does_not_rearm_positive_owner(self):
        started = asyncio.Event()
        async def send(decision):
            if decision.current_p1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return receipt("222")  # Ambiguous already-started HTTP.
            return receipt("223")
        self.adapter.async_execute = send
        task = asyncio.create_task(self.controller.async_tick())
        await asyncio.wait_for(started.wait(), 1)
        self.states["sensor.grid"].state = "40"
        self.fresh_control_reports()
        self.controller._handle_source_change(None)
        await asyncio.wait_for(task, 1)
        if self.controller._refresh_task:
            await asyncio.wait_for(self.controller._refresh_task, 1)
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 0)
        self.assertEqual(self.controller._recent_commands[0]["result"], "error")
        self.assertFalse(self.controller._stop_context_armed)

    async def test_positive_capacity_failure_gets_zero_next_instead_of_retrying_old_offer(self):
        calls = []
        async def send(decision):
            calls.append(decision.current_p1)
            if decision.current_p1:
                raise RuntimeError("positive_rate_capacity_exhausted")
        self.adapter.async_execute = send
        await self.controller.async_tick()
        if self.controller._refresh_task:
            await asyncio.wait_for(self.controller._refresh_task, 1)
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[0], 0)
        self.assertEqual(calls[1], 0)

    async def test_post_zero_positive_waits_for_delivery_and_native_evidence(self):
        self.states["sensor.grid"].state = "40"
        self.adapter.async_execute = Mock(side_effect=lambda _: asyncio.sleep(0, result=receipt()))
        await self.controller.async_tick()
        self.states["sensor.grid"].state = "2"
        self.states["sensor.car"].state = "0"
        self.states["sensor.status"].state = "ready_to_charge"
        self.fresh_control_reports()
        self.controller.regulator.last_action_ts -= 120
        self.controller.regulator.last_set_limit_ts -= 120
        positive = replace(self.controller.last_decision, current_p1=7, should_call_service=True)
        with patch.object(self.controller.regulator, "evaluate", return_value=positive):
            await self.controller.async_tick()
            self.assertEqual(self.adapter.async_execute.call_count, 1)
            self.assertEqual(self.controller.last_service_result, "not_sent_waiting_for_zero_confirmation")

    async def test_cached_zero_publication_cannot_suppress_and_native_event_can(self):
        self.states["sensor.grid"].state = "40"
        self.adapter.async_execute = Mock(side_effect=lambda _: asyncio.sleep(0, result=receipt()))
        await self.controller.async_tick()
        self.states["sensor.car"].state = "0"
        self.fresh_control_reports()
        inp = self.controller._read_input()
        self.controller.source_diagnostics.update(reported_circuit_available=True,
            reported_circuit_p1=0, reported_circuit_p2=0, reported_circuit_p3=0,
            reported_circuit_reported_at_utc=datetime.now(timezone.utc).isoformat())
        self.adapter.offline_limits_verified_zero = True
        self.controller._delivery.delivery(event(), self.hass.loop.time())
        self.assertFalse(self.controller._delivery.can_skip_zero(**self.controller._zero_readback_evidence(inp)))
        self.adapter.reported_limit_receipts = (self.hass.loop.time(), None, None)
        self.assertTrue(self.controller._delivery.can_skip_zero(**self.controller._zero_readback_evidence(inp)))

    async def test_unchanged_ev_zero_can_order_but_cannot_claim_fresh_physical_zero(self):
        self.states["sensor.grid"].state = "40"
        self.states["sensor.car"].state = "0"
        self.fresh_control_reports()
        self.adapter.async_execute = Mock(side_effect=lambda _: asyncio.sleep(0, result=receipt()))
        await self.controller.async_tick()
        inp = self.controller._read_input()  # EV=0 event is older than request.
        self.controller.source_diagnostics.update(reported_circuit_available=True,
            reported_circuit_p1=0, reported_circuit_p2=0, reported_circuit_p3=0)
        self.adapter.offline_limits_verified_zero = True
        self.adapter.reported_limit_receipts = (self.hass.loop.time(), None, None)
        self.controller._delivery.delivery(event(), self.hass.loop.time())
        self.assertFalse(self.controller._delivery.can_skip_zero(**self.controller._zero_readback_evidence(inp)))
        self.assertTrue(self.controller._delivery.can_skip_zero(**self.controller._zero_readback_evidence(inp, require_new_current=False)))

    async def test_zero_timeout_cannot_be_followed_by_positive_without_repair(self):
        self.states["sensor.grid"].state = "40"
        calls = []
        async def send(decision):
            calls.append(decision.current_p1)
            if len(calls) == 1:
                raise TimeoutError()
            return receipt()
        self.adapter.async_execute = send
        await self.controller.async_tick()
        self.assertTrue(self.controller._protective_zero_pending)
        positive = replace(self.controller.last_decision, current_p1=7, should_call_service=True)
        self.states["sensor.grid"].state = "2"
        self.states["sensor.car"].state = "0"
        self.fresh_control_reports()
        with patch.object(self.controller.regulator, "evaluate", return_value=positive):
            await self.controller.async_tick()
        self.assertEqual(calls, [0, 0])

    async def test_native_connection_rebind_requires_new_protection_not_old_ack(self):
        self.adapter.control_identity = (1,)
        self.controller._sync_command_identity()
        self.controller._delivery.begin("set_limit", (7, 0, 0), self.hass.loop.time())
        self.adapter.control_identity = (2,)
        self.controller._sync_command_identity()
        self.assertTrue(self.controller._protective_zero_pending)
        self.assertIsNone(self.controller._delivery.latest)

    async def test_experimental_envelope_does_not_preempt_at_ordinary_25a_crossing(self):
        self.entry.options.update(overload_enabled=True)
        self.controller._reload_settings_locked()
        self.adapter.gate = asyncio.Event()
        task = asyncio.create_task(self.controller.async_tick())
        await asyncio.wait_for(self.adapter.entered.wait(), 1)
        self.states["sensor.grid"].state = "28"
        self.fresh_control_reports()
        self.controller._handle_source_change(None)
        self.assertFalse(self.controller._dispatch_superseded)
        self.adapter.gate.set()
        await asyncio.wait_for(task, 1)

    async def test_nominal_deadline_scheduled_without_experimental_profile(self):
        await self.controller.async_tick()
        decision = replace(self.controller.last_decision, current_p1=7,
                           nominal_response_deadline_ts=self.hass.loop.time() + 3)
        with patch.object(fixtures.runtime, "async_call_later", return_value=Mock()) as timer:
            self.controller._schedule_overload_deadline(decision)
        self.assertEqual(timer.call_count, 1)
        self.assertGreater(timer.call_args.args[1], 0)
        self.assertLessEqual(timer.call_args.args[1], 3)


if __name__ == "__main__":
    unittest.main()
