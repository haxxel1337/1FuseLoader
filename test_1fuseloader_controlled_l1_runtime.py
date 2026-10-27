"""Explicit mapped-L1 DTO publication; aggregate clocks remain independent."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest

import test_1fuseloader_clock_provenance_runtime as clocks
import test_1fuseloader_meter_source_runtime as source


class ControlledL1RuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    install = clocks.ClockProvenanceRuntimeTests.install
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown

    async def asyncSetUp(self):
        await clocks.ClockProvenanceRuntimeTests.asyncSetUp(self)
        self.entry.options.update(**source.PROFILE, meter_envelope_control=True)
        self.controller._reload_settings_locked()
        self.adapter.async_read_current_snapshot = None
        self.set_native(8.34, received=99, ids=(182,), changed_at=90)
        self.line = SimpleNamespace(current=7.094, received_at=self.wall(98.5), sequence=3,
            evidence_credit=True, source_identity=self.owner, mapping_epoch=2,
            output_phase=10, mapping_adopted_at=self.wall(90), mapping_sequence=1,
            source_at=self.wall(98), source_highwater_at=self.wall(98),
            mapping_source_at=self.wall(89), mapping_source_highwater_at=self.wall(89),
            error=None, current_error=None, observation_id=183, committed=True)
        self.adapter.native_controlled_l1_observation = self.line

    def test_dto_uses_own_terminal_clock_and_keeps_aggregate_unchanged(self):
        inp = self.controller._read_input()
        self.assertEqual(inp.charger_current, 8.34)
        self.assertEqual(inp.charger_report_ts, self.wall(99).timestamp())
        self.assertEqual(inp.controlled_l1.current, 7.094)
        self.assertEqual(inp.controlled_l1.received_at_ts, self.wall(98.5).timestamp())
        self.assertEqual(inp.controlled_l1.sequence, 3)
        self.assertEqual(inp.controlled_l1.source_id, inp.control_source_id)
        self.assertTrue(inp.meter_ev_health.valid)
        with self.assertRaises(FrozenInstanceError):
            inp.controlled_l1.sequence = 4

    def test_phase_epoch_changes_revision_without_fabricating_an_ev_receipt(self):
        before = self.controller._read_input()
        self.line.mapping_epoch += 1
        self.line.mapping_adopted_at = self.wall()
        self.line.committed = False
        self.line.current_error = "controlled_current_after_mapping_required"
        after = self.controller._read_input()
        self.assertNotEqual(self.controller._input_revision(before), self.controller._input_revision(after))
        self.assertEqual(after.controlled_l1.received_at_ts, before.controlled_l1.received_at_ts)
        self.assertEqual(after.charger_report_ts, before.charger_report_ts)
        self.assertFalse(after.meter_ev_health.valid)

    def test_aggregate_error_does_not_misclassify_independent_mapped_sample(self):
        self.native.error = "synthetic_neutral_vector_error"
        inp = self.controller._read_input()
        self.assertFalse(inp.data_valid)  # Existing/MAX diagnostic contract.
        self.assertTrue(inp.meter_grid_health.valid)
        self.assertTrue(inp.meter_actuator_health.valid)
        self.assertTrue(inp.meter_ev_health.valid)
        self.assertEqual(inp.controlled_l1.current, 7.094)

    def test_context_without_current_is_explicit_not_a_bootstrap_response(self):
        self.line.current = None
        self.line.received_at = None
        self.line.sequence = 0
        self.line.evidence_credit = False
        self.line.committed = False
        self.line.current_error = "controlled_current_after_mapping_required"
        inp = self.controller._read_input()
        self.assertIsNotNone(inp.controlled_l1)
        self.assertEqual(inp.controlled_l1.output_phase, 10)
        self.assertIsNone(inp.controlled_l1.error)
        self.assertFalse(inp.controlled_l1.committed)
        self.assertFalse(inp.meter_ev_health.valid)

    def test_unknown_phase_never_becomes_a_default_ten(self):
        self.line.output_phase = None
        self.line.error = "controlled_mapping_unavailable"
        self.line.committed = False
        inp = self.controller._read_input()
        self.assertIsNone(inp.controlled_l1.output_phase)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertEqual(inp.charger_current, 8.34)

    def test_phase_clock_and_native_clock_survive_unrelated_ha_attribute_fanout(self):
        before = self.controller._read_input().controlled_l1
        self.now += 1
        self.states['sensor.car'].last_reported = self.wall()
        self.states['sensor.car'].attributes['unrelated'] = 'synthetic'
        self.states['sensor.grid'].last_reported = self.wall()
        after = self.controller._read_input().controlled_l1
        self.assertEqual(after, before)

    def test_legacy_policy_observes_phase_metadata_without_using_it_as_control(self):
        self.entry.options['meter_envelope_control'] = False
        self.controller._reload_settings_locked()
        inp = self.controller._read_input()
        self.assertIsNotNone(inp.controlled_l1)
        self.assertEqual(inp.controlled_l1.current, 7.094)
        self.assertEqual(inp.charger_current, 8.34)
        self.assertIsNone(inp.meter_ev_health)
        self.assertNotIn('controlled_l1_observed_current', self.controller.source_diagnostics)
        changed = replace(inp, controlled_l1=replace(inp.controlled_l1, mapping_epoch=99))
        self.assertEqual(self.controller._input_revision(inp), self.controller._input_revision(changed))
        before = self.controller.regulator.evaluate(inp)
        without = self.controller.regulator.evaluate(replace(inp, controlled_l1=None))
        self.assertEqual((before.command, before.current_p1, before.mode),
                         (without.command, without.current_p1, without.mode))


if __name__ == '__main__':
    unittest.main()
