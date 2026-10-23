"""Dispatch races and first-stop diagnostics, without HA or network access."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_runtime as fixtures


class DispatchFreshnessTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixtures.ControllerTests.update_entry
    fresh_control_reports = fixtures.ControllerTests.fresh_control_reports

    async def asyncSetUp(self):
        await fixtures.ControllerTests.asyncSetUp(self)
        self.fresh_control_reports()
        # Keep re-evaluation observable and deterministic: tests explicitly run
        # the queued evaluation after asserting the old command was discarded.
        self.controller._handle_source_change = Mock()

    async def asyncTearDown(self):
        await self.controller.async_stop()

    def sources(self, grid, current, status="charging"):
        for entity_id, value in (("sensor.grid", grid), ("sensor.car", current),
                                 ("sensor.status", status)):
            self.states[entity_id].state = str(value)
        self.fresh_control_reports()

    def seed_accepted_cap(self, cap):
        # This controls the regulator plan only. Stop-context tests below use
        # an actual fake HTTP submission to arm incident capture instead.
        r = self.controller.regulator
        then = self.hass.loop.time() - 120
        r.last_target = (cap, 0, 0, 60)
        r.last_set_limit_ts = then
        r.last_action_ts = then
        r._last_limit_change_ts = then
        r._overload_used_seconds = 1  # Exercise the real checkpoint await.

    def block_checkpoint(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.controller._budget_store.async_save

        async def save(state):
            entered.set()
            await release.wait()
            return await original(state)

        self.controller._budget_store.async_save = save
        return entered, release

    async def changed_during_checkpoint(self, kind, change):
        if kind == "reduction":
            self.sources(26, 16)
            self.seed_accepted_cap(16)
            expected = 13
        elif kind == "renewal":
            self.sources(20, 9)
            self.seed_accepted_cap(12)
            expected = 12
        else:
            raise AssertionError("Unknown fixture plan")
        entered, release = self.block_checkpoint()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.adapter.calls, [])
            change()
            release.set()
            await tick
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.current_p1, expected)
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertFalse(self.controller.last_decision.should_call_service)
        self.controller._handle_source_change.assert_called_once_with(None)

    async def test_hard_grid_during_checkpoint_discards_positive_reduction(self):
        await self.changed_during_checkpoint("reduction", lambda: self.sources(40, 16))
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_fault_during_checkpoint_discards_positive_reduction(self):
        await self.changed_during_checkpoint("reduction", lambda: self.sources(26, 16, "error"))
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_schedule_status_during_checkpoint_discards_positive_reduction(self):
        await self.changed_during_checkpoint(
            "reduction", lambda: self.sources(26, 16, "awaiting_scheduled_start"))
        await self.controller.async_tick()
        self.assertFalse(any(d.command == "resume" for d in self.adapter.calls))

    async def test_hard_grid_during_checkpoint_discards_positive_renewal(self):
        await self.changed_during_checkpoint("renewal", lambda: self.sources(40, 9))
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_fault_during_checkpoint_discards_positive_renewal(self):
        await self.changed_during_checkpoint("renewal", lambda: self.sources(20, 9, "error"))
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_manual_pause_during_checkpoint_discards_positive_renewal(self):
        await self.changed_during_checkpoint("renewal", lambda: self.sources(20, 9, "paused"))
        await self.controller.async_tick()
        self.assertFalse(any(d.command == "resume" for d in self.adapter.calls))

    async def test_same_values_with_new_receipt_still_replan_renewal(self):
        await self.changed_during_checkpoint("renewal", self.fresh_control_reports)

    async def test_owned_resume_is_discarded_when_fault_arrives_during_checkpoint(self):
        self.sources(10, 0, "awaiting_start")
        self.seed_accepted_cap(13)
        r = self.controller.regulator
        r.last_set_limit_ts = self.hass.loop.time() - 1
        r.paused_by_us = True
        r._pause_observed = True
        r.below_resume_since = self.hass.loop.time() - 130
        entered, release = self.block_checkpoint()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.sources(10, 0, "error")
            release.set()
            await tick
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual(self.controller.last_decision.command, "resume")
        self.assertEqual(self.controller.last_decision.mode, "waiting_for_latest_sources")
        self.assertEqual(self.adapter.calls, [])
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])

    async def test_unchanged_checkpoint_allows_safe_positive_reduction(self):
        self.sources(26, 16)
        self.seed_accepted_cap(16)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [13])
        self.assertEqual(self.controller.last_accepted_limit["result"], "submitted_unverified")

    async def test_unchanged_checkpoint_allows_same_cap_lease_renewal(self):
        self.sources(20, 9)
        self.seed_accepted_cap(12)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [12])
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 12)

    async def test_zero_keeps_priority_when_sources_recover_during_checkpoint(self):
        self.sources(40, 16)
        self.seed_accepted_cap(16)
        entered, release = self.block_checkpoint()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.sources(10, 0, "ready_to_charge")
            release.set()
            await tick
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [0])
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 0)

    async def test_pause_keeps_priority_when_grid_recovers_during_checkpoint(self):
        self.sources(30, 6)
        self.seed_accepted_cap(16)
        self.controller.regulator.above_pause_since = self.hass.loop.time() - 130
        entered, release = self.block_checkpoint()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.sources(10, 0, "ready_to_charge")
            release.set()
            await tick
        finally:
            release.set()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual([d.command for d in self.adapter.calls], ["pause"])

    async def changed_during_positive_api(self, change):
        self.sources(20, 9)
        self.seed_accepted_cap(12)
        self.adapter.gate = asyncio.Event()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(self.adapter.entered.wait(), 1)
            change()
            self.adapter.gate.set()
            await tick
        finally:
            self.adapter.gate.set()
            await asyncio.gather(tick, return_exceptions=True)
        # The first HTTP submission really succeeded. It must not be erased or
        # misreported as cancelled simply because new telemetry arrived later.
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [12])
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 12)
        self.assertEqual(self.controller.last_accepted_limit["result"], "submitted_unverified")
        self.assertIsNotNone(self.controller.last_accepted_limit["completed_at_utc"])
        self.controller._handle_source_change.assert_called_once_with(None)
        await self.controller.async_tick()
        self.assertEqual([d.current_p1 for d in self.adapter.calls], [12, 0])

    async def test_heartbeat_loss_during_positive_api_preserves_acceptance_then_replans_zero(self):
        await self.changed_during_positive_api(
            lambda: setattr(self.adapter, "telemetry_block_reason", "easee_heartbeat_stale"))

    async def test_hard_grid_during_positive_api_preserves_acceptance_then_replans_zero(self):
        await self.changed_during_positive_api(lambda: self.sources(40, 9))

    async def submit_positive(self):
        self.sources(16, 8)
        await self.controller.async_tick()
        self.assertGreater(self.controller.last_accepted_limit["target_p1"], 0)
        return self.controller.last_accepted_limit["target_p1"]

    async def first_heartbeat_stop(self, *, fail=False):
        accepted = await self.submit_positive()
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        if fail:
            self.adapter.failure = OSError("PRIVATE_REMOTE_DETAIL")
        await self.controller.async_tick()
        context = deepcopy(self.controller.last_stop_context)
        self.assertIsNotNone(context)
        self.assertIn("easee_heartbeat_stale", context["reason"])
        self.assertEqual(context["previous_submitted_limit_p1"], accepted)
        return context

    async def test_first_stop_context_keeps_first_reason_through_secondary_mismatch(self):
        first = await self.first_heartbeat_stop()
        self.adapter.telemetry_block_reason = None
        self.sources(10, 12)
        self.controller.regulator.last_set_limit_ts = self.hass.loop.time() - 31
        await self.controller.async_tick()
        self.assertIn("inconsistent", self.controller.last_decision.reason)
        self.assertEqual(self.controller.last_stop_context, first)

    async def test_first_failed_stop_attempt_is_not_rewritten_by_successful_retry(self):
        first = await self.first_heartbeat_stop(fail=True)
        self.assertEqual(first["command_result"], "error")
        self.assertEqual(first["command_error"], "OSError")
        self.assertNotIn("PRIVATE_REMOTE_DETAIL", str(first))
        self.adapter.failure = None
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_accepted_limit["target_p1"], 0)
        self.assertEqual(self.controller.last_stop_context, first)

    async def test_stop_context_keeps_planned_sources_and_budget_while_zero_is_awaited(self):
        previous_cap = await self.submit_positive()
        self.controller.regulator._overload_used_seconds = 300
        self.controller.regulator._overload_failure_reason = "response_timeout"
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        self.adapter.gate = asyncio.Event()
        self.adapter.entered.clear()
        tick = asyncio.create_task(self.controller.async_tick())
        try:
            await asyncio.wait_for(self.adapter.entered.wait(), 1)
            self.sources(3, 0, "awaiting_start")
            self.adapter.telemetry_block_reason = None
            self.adapter.gate.set()
            await tick
        finally:
            self.adapter.gate.set()
            await asyncio.gather(tick, return_exceptions=True)
        context = self.controller.last_stop_context
        self.assertEqual(context["grid_l1"], 16)
        self.assertEqual(context["charger_current"], 8)
        self.assertEqual(context["status"], "charging")
        self.assertEqual(context["source_error"], "easee_heartbeat_stale")
        self.assertEqual(context["previous_submitted_limit_p1"], previous_cap)
        self.assertEqual(context["overload_used_seconds"], 300)
        self.assertEqual(context["overload_failure_reason"], "response_timeout")
        self.assertEqual(context["command_result"], "submitted_unverified")
        self.assertLessEqual(context["plan_utc"], context["command_started_at_utc"])
        self.assertLessEqual(context["command_started_at_utc"], context["command_completed_at_utc"])

    async def test_new_accepted_positive_rearms_the_next_stop(self):
        first = await self.first_heartbeat_stop()
        self.adapter.telemetry_block_reason = None
        self.controller.regulator.reset()
        await self.submit_positive()
        self.assertEqual(self.controller.last_stop_context, first)
        self.states["sensor.grid"].last_reported -= timedelta(minutes=3)
        await self.controller.async_tick()
        second = self.controller.last_stop_context
        self.assertNotEqual(second["plan_utc"], first["plan_utc"])
        self.assertIn("L1 telemetry is stale", second["reason"])
        self.assertNotIn("easee_heartbeat_stale", second["reason"])

    async def test_stop_context_is_whitelisted_and_diagnostics_return_a_copy(self):
        pulse = datetime.now(timezone.utc) - timedelta(seconds=121)
        self.states["binary_sensor.online"] = SimpleNamespace(
            state="on", last_reported=pulse,
            attributes={"state_latestPulse": pulse, "private_remote_key": "PRIVATE_SENTINEL"})
        online = SimpleNamespace(platform="easee", disabled_by=None, entity_id="binary_sensor.online")
        registry = fixtures.ControllerTests.cached_registry(self, [online])
        with patch.object(fixtures.runtime, "er", registry):
            await self.first_heartbeat_stop()
        context = self.controller.last_stop_context
        expected = {
            "plan_utc", "reason", "mode", "grid_l1", "charger_current", "status",
            "grid_report_utc", "charger_report_utc", "source_error",
            "product_update_received_at_utc", "previous_submitted_limit_p1",
            "overload_used_seconds", "overload_failure_reason",
            "overload_effective_used_seconds", "overload_effective_failure_reason",
            "overload_trial_id", "overload_trial_state", "overload_trial_used_seconds",
            "overload_trial_remaining_seconds", "overload_trial_failure_reason",
            "overload_lifetime_used_seconds", "overload_historical_failure_reason",
            "command_started_at_utc", "command_completed_at_utc", "command_result",
            "command_error", "clock_scope", "preceding_positive_limit_receipts",
        }
        self.assertEqual(set(context), expected)
        self.assertEqual(context["product_update_received_at_utc"], pulse.isoformat())
        self.assertEqual(context["command_result"], "submitted_unverified")
        self.assertIsNone(context["command_error"])
        for key in ("plan_utc", "grid_report_utc", "charger_report_utc",
                    "command_started_at_utc", "command_completed_at_utc"):
            self.assertIsNotNone(datetime.fromisoformat(context[key]).utcoffset())
        self.assertNotIn("PRIVATE_SENTINEL", str(context))
        public = self.controller.diagnostic_attributes()["last_stop_context"]
        public["reason"] = "client-side mutation"
        self.assertNotEqual(self.controller.last_stop_context["reason"], public["reason"])

    async def test_blocked_zero_is_not_a_real_stop_attempt(self):
        await self.submit_positive()
        self.adapter.control_block_reason = "unsupported_easee_version"
        self.adapter.telemetry_block_reason = "easee_heartbeat_stale"
        await self.controller.async_tick()
        self.assertIsNone(self.controller.last_stop_context)
        self.assertEqual(self.controller.last_service_result, "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
