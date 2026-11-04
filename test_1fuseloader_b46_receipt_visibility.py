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


if __name__ == '__main__':
    unittest.main()
