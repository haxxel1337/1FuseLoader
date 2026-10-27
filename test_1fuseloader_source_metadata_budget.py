"""Primary source metadata survives bounded HA diagnostic publication.

Synthetic stop/transport histories exercise the real compactor and the actual
Source Health sensor's second publication pass. No source clocks are acquired
or fabricated here; the supplied values must survive exactly or be marked
explicitly omitted when they cannot fit a bounded scalar projection.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

import test_1fuseloader_diagnostics_budget as fixtures


runtime = fixtures.fixtures.runtime
METADATA = {
    "grid_reported_at_utc": "2026-01-02T03:04:05+00:00",
    "charger_current_reported_at_utc": "2026-01-02T03:04:01+00:00",
    "charger_status_reported_at_utc": "2026-01-02T03:04:02+00:00",
    "easee_stream_pulse_utc": "2026-01-02T03:04:06+00:00",
    "reported_circuit_reported_at_utc": "2026-01-02T03:04:03+00:00",
    "grid_source_kind": "currently_one_validated_mqtt",
    "grid_mqtt_received_at_utc": "2026-01-02T03:04:05.100000+00:00",
    "easee_source_health": "healthy",
    "effective_charger_reported_at_utc": "2026-01-02T03:04:01.250000+00:00",
}


def encoded_size(value):
    return max(len(json.dumps(value, ensure_ascii=ascii_mode).encode("utf-8"))
               for ascii_mode in (True, False))


def large_diagnostics():
    """Latest nested cause, not an unbounded public copy of private rings."""
    details = {f"synthetic_detail_{i}": "Diagnostic detail \u0000 \u26a1 " * 60
               for i in range(48)}
    return {**METADATA, "source_health": "healthy",
        "source_health_reason": "Required source checks passed.",
        "mode": "normal", "reason": "The accepted normal offer remains valid.",
        "current_p1": 7, "current_p2": 0, "current_p3": 0,
        "last_stop_context": {"reason": "Previous synthetic cause.",
            "mode": "unavailable", **details},
        "last_stop_nominal_diagnostics": deepcopy(details),
        "last_command_attempt": deepcopy(details),
        "last_accepted_command": deepcopy(details),
        "last_accepted_limit": deepcopy(details),
        "recent_command_journal": [deepcopy(details)] * 8,
        "recent_stop_contexts": [deepcopy(details)] * 8,
        "recent_command_deliveries": [deepcopy(details)] * 8,
        "recent_command_journal_count": 32, "recent_stop_contexts_count": 8,
        "overload_lifetime_used_seconds": 1200.,
        "overload_historical_failure_reason": "response_timeout"}


class SourceMetadataBudgetTests(unittest.TestCase):
    def assert_metadata(self, view, metadata=METADATA):
        for key, value in metadata.items():
            with self.subTest(key=key):
                self.assertIn(key, view)
                self.assertEqual(view[key], value)

    def assert_budget(self, view):
        self.assertLessEqual(encoded_size(view), runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)

    def test_large_stop_and_delivery_details_do_not_evict_source_metadata(self):
        view = runtime._compact_state_attributes(large_diagnostics())
        self.assert_metadata(view)
        self.assert_budget(view)
        self.assertTrue(view["diagnostics_truncated"])
        self.assertEqual(view["source_health"], "healthy")
        self.assertEqual(view["overload_lifetime_used_seconds"], 1200.)
        self.assertTrue(view["last_stop_context"]["reason"].startswith("Previous synthetic"))

    def test_source_health_sensor_second_compaction_preserves_all_clocks(self):
        payload = large_diagnostics()
        decision = fixtures.fixtures.logic.RegulatorDecision(
            mode="normal", reason="Synthetic current decision. " * 80,
            command=None, service=None, current_p1=7, current_p2=0, current_p3=0,
            ttl_seconds=60, available_l1=7, house_load_l1=5., grid_l1=12.,
            charger_current=7., paused_by_us=False, should_call_service=False, dry_run=False)
        controller = SimpleNamespace(last_decision=decision, source_diagnostics=deepcopy(METADATA),
            last_service_result="not_sent", last_service_error=None, control_block_reason=None,
            compact_state_attributes=runtime._compact_state_attributes,
            diagnostic_attributes=lambda: runtime._compact_state_attributes(payload))
        controller.source_diagnostics["source_health"] = "healthy"
        module = fixtures.sensor_module()
        description = next(d for d in module.SENSOR_DESCRIPTIONS if d.key == "source_health")
        entry = SimpleNamespace(entry_id="synthetic-entry", title="Synthetic controller")
        sensor = module.OneFuseSensor(controller, entry, description)
        self.assertEqual(sensor.native_value, "healthy")
        view = sensor.extra_state_attributes
        self.assert_metadata(view)
        self.assert_budget(view)
        self.assertTrue(view["diagnostics_truncated"])

    def test_explicitly_missing_metadata_stays_none_after_both_passes(self):
        payload = large_diagnostics()
        missing = {key: None for key in METADATA}
        payload.update(missing)
        first = runtime._compact_state_attributes(payload)
        second = runtime._compact_state_attributes({**first, "reason": "Second publication. " * 100})
        self.assert_metadata(second, missing)
        self.assert_budget(first)
        self.assert_budget(second)

    def test_source_health_is_never_inferred_from_missing_metadata(self):
        payload = large_diagnostics()
        payload.update({"source_health": "unhealthy", "easee_source_health": "easee_heartbeat_stale"})
        for key in METADATA:
            if key != "easee_source_health":
                payload.pop(key)
        view = runtime._compact_state_attributes(payload)
        self.assertEqual(view["source_health"], "unhealthy")
        self.assertEqual(view["easee_source_health"], "easee_heartbeat_stale")
        for key in METADATA:
            if key != "easee_source_health":
                self.assertNotIn(key, view)
        self.assert_budget(view)

    def test_unbounded_primary_metadata_is_explicitly_omitted_not_shortened_clock(self):
        payload = large_diagnostics()
        payload["grid_reported_at_utc"] = "2026-01-02T03:04:05+00:00" + "\u0000" * 20000
        payload["easee_source_health"] = {"unexpected_nested_diagnostic": "x" * 20000}
        first = runtime._compact_state_attributes(payload)
        view = runtime._compact_state_attributes(first)
        self.assertIsNone(view["grid_reported_at_utc"])
        self.assertIsNone(view["easee_source_health"])
        self.assertEqual(set(view["source_metadata_omitted_fields"]),
                         {"grid_reported_at_utc", "easee_source_health"})
        self.assertTrue(view["diagnostics_truncated"])
        self.assert_budget(view)
        self.assert_metadata(view, {key: value for key, value in METADATA.items()
                                  if key not in view["source_metadata_omitted_fields"]})

    def test_compaction_keeps_prior_omission_flags_and_private_payload_unchanged(self):
        payload = large_diagnostics()
        payload["diagnostics_omitted_fields_count"] = 17
        payload["source_metadata_omitted_fields"] = ["grid_reported_at_utc"]
        payload["grid_reported_at_utc"] = None
        before = deepcopy(payload)
        first = runtime._compact_state_attributes(payload)
        second = runtime._compact_state_attributes(first)
        self.assertGreaterEqual(second["diagnostics_omitted_fields_count"], 17)
        self.assertEqual(second["source_metadata_omitted_fields"], ["grid_reported_at_utc"])
        self.assertIsNone(second["grid_reported_at_utc"])
        self.assertEqual(payload, before)
        self.assert_budget(second)

    def test_control_character_details_cannot_overrun_hard_budget(self):
        payload = large_diagnostics()
        for key in list(payload):
            if key not in METADATA and key not in {"source_health", "mode", "current_p1"}:
                payload[key] = "\u0000" * 20000
        first = runtime._compact_state_attributes(payload)
        second = runtime._compact_state_attributes(first)
        self.assert_metadata(second)
        self.assert_budget(first)
        self.assert_budget(second)

    def test_all_metadata_omissions_survive_two_passes_without_list_truncation(self):
        payload = large_diagnostics()
        payload.update({key: {"unexpected": "x" * 20000} for key in METADATA})
        first = runtime._compact_state_attributes(payload)
        second = runtime._compact_state_attributes(first)
        self.assertEqual(set(second["source_metadata_omitted_fields"]), set(METADATA))
        self.assert_metadata(second, {key: None for key in METADATA})
        self.assert_budget(first)
        self.assert_budget(second)

    def test_largest_permitted_metadata_scalars_fit_final_projection_exactly(self):
        payload = large_diagnostics()
        values = {key: "x" * 254 for key in METADATA}
        payload.update(values)
        for key in list(payload):
            if key not in METADATA:
                payload[key] = "\u0000" * 20000
        first = runtime._compact_state_attributes(payload)
        second = runtime._compact_state_attributes(first)
        self.assert_metadata(second, values)
        self.assertNotIn("source_metadata_omitted_fields", second)
        self.assert_budget(first)
        self.assert_budget(second)

    def test_malformed_omission_marker_does_not_hide_valid_source_clocks(self):
        payload = large_diagnostics()
        payload["source_metadata_omitted_fields"] = [{"unexpected": "nested"}, "unknown_field", 3]
        view = runtime._compact_state_attributes(payload)
        self.assert_metadata(view)
        self.assertNotIn("source_metadata_omitted_fields", view)
        self.assert_budget(view)


if __name__ == "__main__":
    unittest.main()
