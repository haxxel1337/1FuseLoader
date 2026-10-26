"""Provider seconds are buckets, never unique native event identifiers.

Uses the real adapter's additive raw ProductUpdate handler and synthetic SDK
projection. No Home Assistant, device, account or network is contacted.
"""
import unittest

import test_1fuseloader_clock_provenance_adapter as raw


class CoarseNativeBucketTests(unittest.IsolatedAsyncioTestCase):
    health=staticmethod(raw.ConductorClockTests.health)
    make_charger=raw.ConductorClockTests.make_charger
    assert_no_commands=raw.ConductorClockTests.assert_no_commands
    setUp=raw.ConductorClockTests.setUp
    advance=raw.ConductorClockTests.advance
    emit=raw.ConductorClockTests.emit

    async def test_new_values_in_one_second_have_real_ordered_receipts_and_credit(self):
        stamp=self.now
        observations=[]
        for value in (2.,8.,16.):
            self.advance(.2)
            event=await self.emit({182:value},stamp=stamp)
            self.assertIsNone(event.error)
            self.assertFalse(event.native_publication_pending)
            self.assertEqual(event.current,value)
            self.assertTrue(dict(event.observation_evidence_credit)[182])
            observations.append(event)
        self.assertEqual([dict(event.observation_sequences)[182] for event in observations],[1,2,3])
        self.assertLess(observations[0].received_at,observations[1].received_at)
        self.assertLess(observations[1].received_at,observations[2].received_at)
        self.assertEqual(dict(observations[-1].source_timestamps)[182],stamp)
        self.assert_no_commands()

    async def test_ambiguous_return_updates_numeric_state_without_fresh_evidence(self):
        stamp=self.now
        for value in (2.,8.,2.):
            self.advance(.2)
            returned=await self.emit({182:value},stamp=stamp)
        self.assertEqual(returned.current,2.)
        self.assertIsNone(returned.error)
        self.assertFalse(returned.native_publication_pending)
        self.assertFalse(dict(returned.observation_evidence_credit)[182])
        self.assertEqual(returned.received_at,self.now)
        original_receipt=returned.received_at
        original_sequence=returned.observation_sequences
        self.advance(.2)
        duplicate=await self.emit({182:2.},stamp=stamp)
        self.assertEqual(duplicate.received_at,original_receipt)
        self.assertEqual(duplicate.observation_sequences,original_sequence)
        self.assertFalse(dict(duplicate.observation_evidence_credit)[182])
        self.advance(1.)
        later=await self.emit({182:2.},stamp=self.now)
        self.assertTrue(dict(later.observation_evidence_credit)[182])
        self.assertGreater(later.received_at,original_receipt)
        self.assertEqual(dict(later.observation_sequences)[182],4)

    async def test_fingerprint_memory_is_bounded_and_overflow_is_numeric_only(self):
        stamp=self.now
        for value in range(1,66):
            self.advance(.01)
            event=await self.emit({182:float(value)},stamp=stamp)
        self.assertEqual(len(self.client._native_current_bucket_values[182]),64)
        self.assertEqual(event.current,65.)
        self.assertIsNone(event.error)
        self.assertFalse(dict(event.observation_evidence_credit)[182])
        self.advance(1.)
        later=await self.emit({182:65.},stamp=self.now)
        self.assertTrue(dict(later.observation_evidence_credit)[182])
        self.assertEqual(len(self.client._native_current_bucket_values[182]),1)

    async def test_actual_provider_regression_still_faults_and_getters_cannot_repair_it(self):
        old=self.now
        await self.emit({182:7.},stamp=old)
        self.advance(2.)
        latest=await self.emit({182:8.},stamp=self.now)
        self.advance(.2)
        bad=await self.emit({182:7.},stamp=old)
        self.assertEqual(bad.error,"easee_observation_timestamp_regressed")
        self.assertEqual(bad.current,8.)
        self.assertEqual(bad.received_at,None)
        for _ in range(5):
            self.client._seed_native_current()
            reread=self.client.native_current_observation
            self.assertEqual(reread.error,"easee_observation_timestamp_regressed")
            self.assertEqual(reread.observation_sequences,latest.observation_sequences)
        self.assert_no_commands()


if __name__=="__main__":
    unittest.main()
