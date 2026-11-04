"""Latest local HTTP receipts survive large private incident summaries.

These assertions distinguish current receipt visibility from physical vehicle
response and device TTL, which remain explicitly unverified.
"""
from copy import deepcopy
from types import SimpleNamespace
import unittest

import test_1fuseloader_diagnostics_budget as fixture


class ReceiptVisibilityTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixture.DiagnosticBudgetTests.update_entry
    fresh_control_reports = fixture.DiagnosticBudgetTests.fresh_control_reports
    command = fixture.DiagnosticBudgetTests.command
    asyncSetUp = fixture.DiagnosticBudgetTests.asyncSetUp
    asyncTearDown = fixture.DiagnosticBudgetTests.asyncTearDown

    def receipts(self):
        self.controller.last_command_attempt = deepcopy(self.commands[-1])
        self.controller.last_accepted_limit = deepcopy(self.commands[-2])
        return {
            'last_attempt_started_at_utc': self.commands[-1]['started_at_utc'],
            'last_attempt_completed_at_utc': self.commands[-1]['completed_at_utc'],
            'last_attempt_current_p1': self.commands[-1]['target_p1'],
            'last_attempt_result': 'submitted_unverified',
            'last_attempt_http_status': 200,
            'last_limit_accepted_at_utc': self.commands[-2]['completed_at_utc'],
            'last_limit_current_p1': self.commands[-2]['target_p1'],
            'last_limit_http_status': 200,
        }

    async def test_current_receipts_survive_large_context_without_inventing_delivery(self):
        expected = self.receipts()
        first = self.controller.diagnostic_attributes()
        view = self.controller.compact_state_attributes(first)
        for key, value in expected.items():
            self.assertEqual(view[key], value, key)
        self.assertLessEqual(fixture.encoded_size(view),
                             fixture.fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)
        self.assertIsNot(view.get('physical_current_verified'), True)
        self.assertIsNot(view.get('device_ttl_verified'), True)

    async def test_last_attempt_sensor_keeps_receipts_after_second_publication(self):
        expected = self.receipts()
        module = fixture.sensor_module()
        description = next(d for d in module.SENSOR_DESCRIPTIONS if d.key == 'last_attempt_result')
        sensor = module.OneFuseSensor(self.controller,
            SimpleNamespace(entry_id='synthetic-entry', title='Synthetic controller'), description)
        view = sensor.extra_state_attributes
        for key, value in expected.items():
            self.assertEqual(view[key], value, key)
        self.assertLessEqual(fixture.encoded_size(view),
                             fixture.fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)

    async def test_missing_http_receipt_stays_missing(self):
        self.receipts()
        self.controller.last_command_attempt['delivery'] = None
        self.controller.last_accepted_limit['delivery'] = 'unexpected diagnostic shape'
        view = self.controller.diagnostic_attributes()
        self.assertIsNone(view['last_attempt_http_status'])
        self.assertIsNone(view['last_limit_http_status'])
        self.assertEqual(view['last_attempt_current_p1'], 16)

    async def test_two_publications_keep_collection_shape_and_detached_stop_receipts(self):
        expected = self.receipts()
        private_stop = deepcopy(self.controller.last_stop_context)
        first = self.controller.diagnostic_attributes()
        # Escaped strings and the full incident fixture force bounded fallback;
        # the second publication must retain usable collections and receipts.
        view = self.controller.compact_state_attributes({
            **first, 'reason': '\x00' * 20000,
            'last_stop_nominal_diagnostics': {'synthetic_detail': '\x00' * 20000},
        })
        self.assertLessEqual(fixture.encoded_size(view),
                             fixture.fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)
        for key in ('recent_command_journal', 'recent_stop_contexts', 'recent_command_deliveries'):
            self.assertIsInstance(view[key], list, key)
            self.assertLessEqual(len(view[key]), 2, key)
        receipts = view['last_stop_context']['preceding_positive_limit_receipts']
        self.assertEqual(len(receipts), 4)
        self.assertEqual([item['started_at_utc'] for item in receipts],
                         [item['started_at_utc'] for item in private_stop['preceding_positive_limit_receipts']])
        for key, value in expected.items():
            self.assertEqual(view[key], value, key)
        receipts[0]['target_p1'] = 999
        self.assertEqual(self.controller.last_stop_context, private_stop)
        self.assertIsNot(view.get('physical_current_verified'), True)
        self.assertIsNot(view.get('device_ttl_verified'), True)


if __name__ == '__main__':
    unittest.main()
