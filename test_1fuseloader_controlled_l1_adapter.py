"""Independent native phase/context and T3 clocks; no HA or device I/O."""
from datetime import timedelta
import unittest

import test_1fuseloader_clock_provenance_adapter as raw


class ControlledL1AdapterTests(unittest.IsolatedAsyncioTestCase):
    health = staticmethod(raw.ConductorClockTests.health)
    make_charger = raw.ConductorClockTests.make_charger
    assert_no_commands = raw.ConductorClockTests.assert_no_commands
    setUp = raw.ConductorClockTests.setUp
    advance = raw.ConductorClockTests.advance
    emit = raw.ConductorClockTests.emit

    async def phase(self, mode, *, stamp=None, data_type=4):
        self.product.state["outputPhase"] = mode
        item = {"mid": self.charger.id, "id": 110, "dataType": data_type,
                "value": str(mode), "timestamp": (stamp or self.now).isoformat()}
        await self.emit({}, extra=(item,))
        return self.client.native_controlled_l1_observation

    async def mapped_sample(self, *, neutral=8.34, line=7.094):
        await self.phase(10)
        self.advance(.1)
        maximum = await self.emit({182: neutral, 183: line}, stamp=self.now)
        return maximum, self.client.native_controlled_l1_observation

    async def test_neutral_max_and_controlled_l1_have_distinct_values_and_clocks(self):
        maximum, line = await self.mapped_sample()
        self.assertEqual(maximum.current, 8.34)
        self.assertEqual(maximum.observation_ids, (182,))
        self.assertEqual(line.current, 7.094)
        self.assertEqual(line.observation_id, 183)
        self.assertTrue(line.committed)
        self.assertIsNone(line.error)
        self.assertGreater(line.received_at, line.mapping_adopted_at)
        original = line
        self.advance(1)
        await self.emit({182: 9.7}, stamp=self.now)
        line = self.client.native_controlled_l1_observation
        self.assertEqual(line.received_at, original.received_at)
        self.assertEqual(line.sequence, original.sequence)
        self.assertEqual(line.source_at, original.source_at)
        self.assert_no_commands()

    async def test_reverse_values_never_select_the_lower_neutral(self):
        maximum, line = await self.mapped_sample(neutral=7.094, line=8.34)
        self.assertEqual(maximum.current, 8.34)
        self.assertEqual(line.current, 8.34)
        self.assertTrue(line.committed)

    async def test_explicit_boot_context_is_not_a_current_sample(self):
        self.product.state["outputPhase"] = 10
        self.product.state["inCurrentT3"] = 7.
        line = self.client.native_controlled_l1_observation
        self.assertEqual(line.output_phase, 10)
        self.assertEqual(line.mapping_sequence, 0)
        self.assertIsNone(line.error)
        self.assertFalse(line.committed)
        self.assertIsNone(line.received_at)
        adopted = line.mapping_adopted_at
        self.advance(.1)
        await self.emit({183: 7.}, stamp=self.now)
        line = self.client.native_controlled_l1_observation
        self.assertTrue(line.committed)
        self.assertEqual(line.mapping_adopted_at, adopted)

    async def test_no_default_mapping_and_no_it_or_other_phase_selection(self):
        await self.emit({182: 8., 183: 7.}, stamp=self.now)
        self.assertIsNone(self.client.native_controlled_l1_observation.output_phase)
        self.assertFalse(self.client.native_controlled_l1_observation.committed)
        for mode in (0, 11, 12, 13, 14, 15, 20, 21, 22, 30):
            self.advance()
            await self.phase(mode)
            self.advance(.1)
            await self.emit({183: 7.}, stamp=self.now)
            line = self.client.native_controlled_l1_observation
            self.assertFalse(line.committed, mode)
            self.assertIsNotNone(line.error, mode)

    async def test_stable_phase_events_cannot_refresh_a_terminal_sample(self):
        _, before = await self.mapped_sample()
        self.advance(10)
        await self.phase(10)
        after = self.client.native_controlled_l1_observation
        self.assertEqual(after.mapping_epoch, before.mapping_epoch)
        self.assertEqual(after.mapping_adopted_at, before.mapping_adopted_at)
        self.assertEqual(after.received_at, before.received_at)
        self.assertEqual(after.sequence, before.sequence)

    async def test_phase_switch_requires_a_new_terminal_after_the_new_epoch(self):
        _, before = await self.mapped_sample()
        self.advance(1)
        await self.phase(12)
        self.advance(1)
        switched = await self.phase(10)
        self.assertGreater(switched.mapping_epoch, before.mapping_epoch)
        self.assertFalse(switched.committed)
        self.assertEqual(switched.received_at, before.received_at)
        self.advance(.1)
        await self.emit({183: 7.094}, stamp=self.now)
        self.assertTrue(self.client.native_controlled_l1_observation.committed)

    async def test_replayed_mapping_cannot_restore_an_old_lower_conductor(self):
        original_stamp = self.now
        await self.mapped_sample()
        self.advance(1)
        await self.phase(12)
        self.advance(1)
        await self.phase(10, stamp=original_stamp)
        self.advance(.1)
        await self.emit({183: 7.094}, stamp=self.now)
        line = self.client.native_controlled_l1_observation
        self.assertFalse(line.committed)
        self.assertIsNotNone(line.error)

    async def test_conflicting_mapping_bucket_requires_a_later_witness(self):
        same = self.now
        await self.phase(10, stamp=same)
        self.advance(.1)
        await self.phase(12, stamp=same)
        self.advance(.1)
        line = await self.phase(10, stamp=same)
        self.assertFalse(line.committed)
        self.assertIsNotNone(line.error)
        self.advance(1)
        await self.phase(10)
        self.advance(.1)
        await self.emit({183: 7.}, stamp=self.now)
        self.assertTrue(self.client.native_controlled_l1_observation.committed)

    async def test_transport_gap_invalidates_context_and_old_terminal_credit(self):
        _, before = await self.mapped_sample()
        self.product.state["signalRConnected"] = False
        self.assertIsNone(self.client.native_controlled_l1_observation)
        self.advance(1)
        self.product.state["signalRConnected"] = True
        returned = self.client.native_controlled_l1_observation
        self.assertGreater(returned.mapping_epoch, before.mapping_epoch)
        self.assertFalse(returned.committed)
        self.advance(.1)
        await self.emit({183: 7.094}, stamp=self.now)
        self.assertTrue(self.client.native_controlled_l1_observation.committed)

    async def test_bad_terminal_does_not_destroy_a_known_mapping_context(self):
        _, before = await self.mapped_sample()
        self.advance(1)
        bad = {"mid": self.charger.id, "id": 183, "dataType": 4, "value": 7.}
        await self.emit({}, extra=(bad,))
        line = self.client.native_controlled_l1_observation
        self.assertEqual(line.mapping_epoch, before.mapping_epoch)
        self.assertIsNone(line.error)
        self.assertFalse(line.committed)
        self.assertIsNotNone(line.current_error)

    async def test_timestamp_free_phase_cycle_cannot_restore_mapping(self):
        self.product.state["outputPhase"] = 10
        self.client.native_controlled_l1_observation
        for mode in (12, 10):
            self.advance(1)
            self.product.state["outputPhase"] = mode
            item = {"mid": self.charger.id, "id": 110, "dataType": 4, "value": mode}
            await self.emit({}, extra=(item,))
        self.advance(.1)
        await self.emit({183: 7.}, stamp=self.now)
        line = self.client.native_controlled_l1_observation
        self.assertFalse(line.committed)
        self.assertIsNotNone(line.error)

    async def test_malformed_mapping_type_never_escapes_native_callback(self):
        await self.mapped_sample()
        item = {"mid": self.charger.id, "id": 110, "dataType": [], "value": 10}
        await self.emit({}, extra=(item,))
        self.assertIsNotNone(self.client.native_controlled_l1_observation.error)

    async def test_bad_neutral_or_unused_terminal_cannot_replace_valid_t3_evidence(self):
        _, before = await self.mapped_sample()
        for key in (182, 184, 185):
            self.advance(1)
            self.product.state[f"inCurrentT{key-180}"] = float("nan")
            item = {"mid": self.charger.id, "id": key, "dataType": 3, "value": "NaN"}
            await self.emit({}, extra=(item,))
            line = self.client.native_controlled_l1_observation
            self.assertTrue(line.committed)
            self.assertEqual(line.received_at, before.received_at)
            self.assertEqual(line.sequence, before.sequence)
            self.assertEqual(line.current, 7.094)
            self.assertIsNotNone(self.client.native_current_observation.error)

    async def test_truncated_prefix_cannot_hide_a_phase_cycle_and_keep_current_credit(self):
        _, before = await self.mapped_sample()
        self.advance(1)
        records = [{"mid": self.charger.id, "id": 183, "dataType": 3,
                    "value": 7., "timestamp": self.now.isoformat()}]
        records += [{"mid": self.charger.id, "id": 999, "dataType": 4, "value": 0}] * 64
        records += [{"mid": self.charger.id, "id": 110, "dataType": 4,
                     "value": mode, "timestamp": self.now.isoformat()} for mode in (12, 10)]
        self.product.state.update(outputPhase=10, inCurrentT3=7.)
        for handler in tuple(self.connection.handlers["ProductUpdate"]):
            await handler(records)
        after = self.client.native_controlled_l1_observation
        self.assertGreater(after.mapping_epoch, before.mapping_epoch)
        self.assertEqual(after.output_phase, 10)  # New context, not old proof.
        self.assertFalse(after.committed)
        self.advance(.1)
        await self.emit({183: 7.}, stamp=self.now)
        self.assertTrue(self.client.native_controlled_l1_observation.committed)

    async def test_completed_sdk_context_can_readopt_without_redundant_phase_event(self):
        _, before = await self.mapped_sample()
        self.product.state['outputPhase'] = None
        pending = self.client.native_controlled_l1_observation
        self.assertFalse(pending.committed)
        self.advance(1)
        self.product.state['outputPhase'] = 10
        adopted = self.client.native_controlled_l1_observation
        self.assertGreater(adopted.mapping_epoch, before.mapping_epoch)
        self.assertEqual(adopted.output_phase, 10)
        self.assertIsNone(adopted.error)
        self.assertFalse(adopted.committed)
        self.assertEqual(adopted.received_at, before.received_at)
        self.advance(.1)
        await self.emit({183: 7.094}, stamp=self.now)
        self.assertTrue(self.client.native_controlled_l1_observation.committed)


if __name__ == "__main__":
    unittest.main()
