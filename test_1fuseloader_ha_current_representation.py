"""Public offline regressions for HA's numeric-state representation boundary."""
from dataclasses import replace
from math import nextafter
import struct
import unittest

import test_1fuseloader_clock_provenance_runtime as clocks


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def ha_state(value):
    # HA Entity._stringify_state, FLOAT_PRECISION derived from binary64 epsilon.
    return f"{value:.15}"


class HaCurrentRepresentationTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    asyncSetUp = clocks.ClockProvenanceRuntimeTests.asyncSetUp
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    install = clocks.ClockProvenanceRuntimeTests.install

    def set_serialized(self, value, **kwargs):
        self.set_native(value, **kwargs)
        self.states["sensor.car"].state = ha_state(value)

    async def test_float32_receipt_and_numeric_value_survive_ha_serialization(self):
        for amperes in (16.059, 9.965, 1.418, 4.190, 3.318, 7.094):
            with self.subTest(amperes=amperes):
                raw = float32(amperes)
                self.set_serialized(raw, received=98, changed_at=90)
                inp = self.controller._read_input()
                self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())
                self.assertEqual(inp.charger_current, raw)
                self.assertEqual(inp.charger_report_family, "native_terminal_receipt")
                self.assertEqual(dict(inp.charger_observation_sequences)[182], 1)

    async def test_adjacent_binary_values_in_same_display_bucket_keep_exact_native_value(self):
        raw = float32(16.059)
        for displayed_native in (nextafter(raw, float("-inf")), raw, nextafter(raw, float("inf"))):
            with self.subTest(displayed_native=displayed_native):
                self.assertEqual(ha_state(raw), ha_state(displayed_native))
                self.set_native(raw, received=98, changed_at=90)
                self.states["sensor.car"].state = ha_state(displayed_native)
                inp = self.controller._read_input()
                self.assertEqual(inp.charger_report_ts, self.wall(98).timestamp())
                self.assertEqual(inp.charger_current, raw)

    async def test_different_rounding_bucket_and_one_milliamp_are_not_matches(self):
        for raw, displayed in ((1.23456789012344, 1.23456789012346), (float32(7.094), float32(7.094)+.001)):
            with self.subTest(raw=raw, displayed=displayed):
                self.assertNotEqual(ha_state(raw), ha_state(displayed))
                self.set_native(raw, received=98, changed_at=90)
                self.states["sensor.car"].state = ha_state(displayed)
                inp = self.controller._read_input()
                self.assertIsNone(inp.charger_report_ts)
                self.assertNotEqual(inp.charger_current, raw)

    async def test_boolean_nonfinite_and_invalid_entity_values_supply_no_receipt(self):
        for invalid in (True, False, "nan", "inf", "-inf", "unknown", "not-a-number"):
            with self.subTest(invalid=invalid):
                self.set_native(1, received=98, changed_at=90)
                self.states["sensor.car"].state = invalid
                self.assertIsNone(self.controller._read_input().charger_report_ts)

    async def test_native_binding_or_winning_id_mismatch_cannot_supply_receipt(self):
        self.set_serialized(8., received=98, changed_at=90)
        self.native.source_identity = (21, 22, 23, 24)
        self.assertIsNone(self.controller._read_input().charger_report_ts)
        self.set_serialized(8., received=98, ids=(181,), changed_at=90)
        self.assertIsNone(self.controller._read_input().charger_report_ts)

    async def test_get_cannot_waive_native_source_fault_or_invalid_ha_value(self):
        self.set_serialized(8., received=98, changed_at=90)
        self.native.error = "conflicting_terminal_observations"
        self.install(self.snapshot(current=8., observed=99))
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_current_trusted)
        self.assertFalse(inp.data_valid)
        self.assertEqual(self.controller.source_diagnostics["source_health"], "unhealthy")
        self.assertFalse(inp.charger_snapshot_verified)
        self.set_serialized(8., received=98, changed_at=90)
        self.states["sensor.car"].state = True
        self.install(self.snapshot(current=8., observed=99))
        inp = self.controller._read_input()
        self.assertFalse(inp.charger_snapshot_verified)
        self.assertIs(inp.charger_current, True)

    async def test_revision_uses_source_bound_number_not_text_format(self):
        raw = float32(16.059)
        self.set_native(raw, received=98, changed_at=90)
        exact_revision = self.controller._charger_source_revision()
        self.states["sensor.car"].state = ha_state(raw)
        self.assertEqual(exact_revision, self.controller._charger_source_revision())
        self.states["sensor.car"].state = ha_state(raw+.001)
        self.assertNotEqual(exact_revision, self.controller._charger_source_revision())

    async def test_matching_get_stays_unassociated_with_serialized_new_a_event(self):
        raw = float32(7.094)
        self.set_serialized(raw, received=98, changed_at=90)
        self.controller._read_input()
        self.now += 1
        self.set_serialized(float32(5.28), received=99, changed_at=90)
        self.controller._read_input()
        self.now += 1
        self.set_serialized(raw, received=100, changed_at=90)
        before = self.controller._read_input()
        self.install(self.snapshot(current=raw, observed=90))
        after = self.controller._read_input()
        self.assertEqual(after.charger_report_ts, before.charger_report_ts)
        self.assertEqual(after.charger_report_family, "native_terminal_receipt")
        self.assertFalse(after.charger_event_source_known)
        self.assertFalse(after.charger_snapshot_verified)

    async def test_same_domain_replay_remains_invalid_with_serialized_values(self):
        raw = float32(7.094)
        self.set_serialized(raw, received=98, changed_at=90)
        self.controller.regulator.evaluate(self.controller._read_input())
        self.set_serialized(raw, received=97, changed_at=90)
        result = self.controller.regulator.evaluate(self.controller._read_input())
        self.assertEqual(result.current_p1, 0)
        self.assertIn("regressed", result.reason)

    async def test_serialized_post_ack_source_floor_is_preserved(self):
        self.set_serialized(float32(7.094), received=100, source=98, changed_at=90)
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_report_ts, self.wall(100).timestamp())
        self.assertEqual(inp.charger_event_source_ts, self.wall(98).timestamp())
        self.assertTrue(inp.charger_event_source_known)

    async def test_full_controller_serialized_start_partial_rise_and_minimum_response(self):
        self.controller.regulator.update_settings(replace(self.controller.settings,
            nominal_import_only_fallback=True, bounded_nominal_shedding=True,
            enable_soft_guard=False, enable_pause_resume=False))
        self.adapter.async_read_current_snapshot = None
        self.states["sensor.status"].state = "awaiting_start"
        self.states["sensor.grid"].state = "4"
        self.set_serialized(0., received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.now += 60
        self.set_serialized(0., received=None, ids=(182, 183, 184, 185))
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.now += 1
        self.states["sensor.status"].state = "charging"
        self.states["sensor.status"].last_changed = self.wall()
        self.states["sensor.grid"].state = "10.6"
        self.set_serialized(float32(6.625), received=self.now, changed_at=self.now-1,
            sequences=((182, 1), (183, 0), (184, 0), (185, 0)))
        await self.controller.async_tick()
        self.now += 4
        self.states["sensor.grid"].state = "20.1"
        self.set_serialized(float32(16.059), received=self.now, changed_at=self.now-1,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        self.now += .3
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertNotEqual(self.controller.last_decision.mode, "unavailable")
        self.now += 1
        self.states["sensor.grid"].state = "32"
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 7)
        self.now += 1
        self.states["sensor.grid"].state = "23"
        self.set_serialized(float32(7.094), received=self.now, changed_at=self.now-1,
            sequences=((182, 3), (183, 0), (184, 0), (185, 0)))
        self.now += .3
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.controller.last_decision.current_p1, 7)
        self.assertTrue(all(call.current_p1 > 0 for call in self.adapter.calls[1:]))
        self.assertIsNone(self.controller.regulator._awaiting_reduction_target)


if __name__ == "__main__":
    unittest.main()
