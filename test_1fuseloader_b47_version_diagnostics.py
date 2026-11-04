"""Loaded compatibility versions stay advisory, bounded and inspectable."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

import test_1fuseloader_diagnostics_budget as fixture
import test_1fuseloader_source_metadata_budget as pressure


VERSIONS = {
    'easee_loaded_version': '2026.11.0',
    'pyeasee_loaded_version': '0.8.22',
    'easee_runtime_pair_supported': True,
}


class VersionDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    update_entry = fixture.DiagnosticBudgetTests.update_entry
    fresh_control_reports = fixture.DiagnosticBudgetTests.fresh_control_reports
    command = fixture.DiagnosticBudgetTests.command
    asyncSetUp = fixture.DiagnosticBudgetTests.asyncSetUp
    asyncTearDown = fixture.DiagnosticBudgetTests.asyncTearDown

    def read_versions(self, reader=None, error=None):
        original = self.controller._adapter
        self.controller._adapter = (SimpleNamespace(runtime_contract_diagnostics=reader)
                                    if reader is not None else SimpleNamespace())
        try:
            self.controller._read_source_diagnostics(None, None, None, error,
                                                    [error] if error else [])
            return deepcopy(self.controller.source_diagnostics)
        finally:
            self.controller._adapter = original

    async def test_actual_loaded_pair_is_visible_even_while_control_is_blocked(self):
        metadata = {**VERSIONS, 'easee_runtime_pair_supported': False}
        view = self.read_versions(lambda: metadata,
                                  'unsupported_easee_pyeasee_combination')
        for key, value in metadata.items():
            self.assertEqual(view[key], value)
        self.assertEqual(view['source_health'], 'unhealthy')
        self.assertEqual(view['easee_source_health'], 'unsupported_easee_pyeasee_combination')

    async def test_supported_pair_never_repairs_an_independent_transport_fault(self):
        view = self.read_versions(lambda: VERSIONS, 'easee_stream_disconnected')
        self.assertTrue(view['easee_runtime_pair_supported'])
        self.assertEqual(view['source_health'], 'unhealthy')
        self.assertEqual(view['easee_source_health'], 'easee_stream_disconnected')

    async def test_absent_or_failing_diagnostic_does_not_change_control_health(self):
        def broken():
            raise RuntimeError('PRIVATE_ERROR_MUST_NOT_ESCAPE')
        for reader in (None, broken):
            view = self.read_versions(reader, 'easee_stream_disconnected')
            self.assertEqual(view['source_health'], 'unhealthy')
            self.assertNotIn('PRIVATE_ERROR_MUST_NOT_ESCAPE', str(view))
            self.assertTrue(all(key not in view for key in VERSIONS))

    async def test_unknown_fields_and_unbounded_objects_do_not_escape(self):
        view = self.read_versions(lambda: {
            'easee_loaded_version': 'x' * 10000,
            'pyeasee_loaded_version': {'private': 'never publish'},
            'easee_runtime_pair_supported': 'true',
            'private_site_data': 'never publish',
        })
        self.assertEqual({key: view[key] for key in VERSIONS},
                         {key: None for key in VERSIONS})
        self.assertNotIn('private_site_data', view)
        self.assertNotIn('never publish', str(view))
        short_private = self.read_versions(lambda: {
            'easee_loaded_version': 'https://private/token',
            'pyeasee_loaded_version': 'PRIVATE_SHORT_VALUE',
            'easee_runtime_pair_supported': False,
        })
        self.assertIsNone(short_private['easee_loaded_version'])
        self.assertIsNone(short_private['pyeasee_loaded_version'])
        self.assertNotIn('PRIVATE_SHORT_VALUE', str(short_private))

    async def test_version_scalars_survive_two_publications_under_size_pressure(self):
        payload = pressure.large_diagnostics()
        payload.update(VERSIONS)
        first = self.controller.compact_state_attributes(payload)
        second = self.controller.compact_state_attributes({**first, 'reason': '\x00' * 20000})
        for key, value in VERSIONS.items():
            self.assertEqual(second[key], value)
        self.assertLessEqual(pressure.encoded_size(second),
                             fixture.fixtures.runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)
        self.assertIsNot(second.get('physical_current_verified'), True)


if __name__ == '__main__':
    unittest.main()
