"""Offline source classes and explicit admission for the meter-led opt-in."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from types import SimpleNamespace

import test_1fuseloader_clock_provenance_runtime as clocks
import test_1fuseloader_config as config
import test_1fuseloader_ha as ha_fixtures
import test_1fuseloader_source_metadata_budget as publication


KEY = "meter_envelope_control"
PROFILE = dict(nominal_import_only_fallback=True, peak_only_response=True,
               automatic_overload_cycles=True, overload_enabled=True)


class MeterEnvelopeConfigTests(unittest.TestCase):
    def test_legacy_default_is_off_without_mutating_existing_profile(self):
        previous = config.const.validate_options(PROFILE)
        previous.pop(KEY)
        original = deepcopy(previous)
        selected = config.const.validate_options(previous)
        self.assertIs(selected[KEY], False)
        self.assertEqual(previous, original)
        self.assertEqual({key: selected[key] for key in previous}, previous)

    def test_only_explicit_boolean_supported_profile_can_enable_policy(self):
        self.assertTrue(config.const.validate_options({**PROFILE, KEY: True})[KEY])
        for value in ("true", "false", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.const.validate_options({**PROFILE, KEY: value})
        # The standalone policy does not depend on the legacy estimator and
        # automatic-cycle switches merely to provide ordinary regulation.
        for key in PROFILE:
            with self.subTest(independent=key):
                selected=config.const.validate_options({**PROFILE, KEY: True, key: False})
                self.assertTrue(selected[KEY])
                self.assertFalse(selected[key])
        with self.assertRaises(ValueError):
            config.const.validate_options({**PROFILE, KEY: True, "enable_dynamic_limit": False})
        with self.assertRaises(ValueError):
            config.const.validate_options({**PROFILE, KEY: True, "max_charge_amps": 17})
        with self.assertRaises(ValueError):
            config.const.validate_options({**PROFILE, KEY: True, "normal_ttl_seconds": 120})

    def test_selecting_policy_does_not_enable_writes_or_reset_other_options(self):
        selected = config.const.validate_options({**PROFILE, KEY: True})
        self.assertFalse(selected["active_control"])
        self.assertTrue(selected["dry_run"])
        self.assertEqual(selected["normal_ttl_seconds"], 60)
        self.assertEqual(selected["overload_budget_seconds"], 1800)
        self.assertEqual(selected["overload_response_seconds"], 10)

    def test_opt_in_has_matching_flow_labels_and_explanation(self):
        component = Path(__file__).parent / "custom_components" / "1fuseloader"
        docs = [json.loads((component / name).read_text(encoding="utf-8"))
                for name in ("strings.json", "translations/en.json", "translations/sv.json")]
        self.assertEqual(docs[0], docs[1])
        for doc in docs:
            form = doc["options"]["step"]["init"]
            self.assertTrue(form["data"][KEY])
            self.assertTrue(form["data_description"][KEY])


class MeterSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    update_entry = clocks.ClockProvenanceRuntimeTests.update_entry
    wall = clocks.ClockProvenanceRuntimeTests.wall
    schedule = clocks.ClockProvenanceRuntimeTests.schedule
    active_timers = clocks.ClockProvenanceRuntimeTests.active_timers
    set_native = clocks.ClockProvenanceRuntimeTests.set_native
    snapshot = clocks.ClockProvenanceRuntimeTests.snapshot
    asyncTearDown = clocks.ClockProvenanceRuntimeTests.asyncTearDown

    async def asyncSetUp(self):
        await clocks.ClockProvenanceRuntimeTests.asyncSetUp(self)
        self.entry.options.update(**PROFILE, meter_envelope_control=True)
        self.controller._reload_settings_locked()
        self.adapter.async_read_current_snapshot = None
        self.set_native(8, received=99, changed_at=90)

    def assert_critical_sources_valid(self, inp):
        self.assertTrue(inp.meter_grid_health.valid, inp.meter_grid_health)
        self.assertTrue(inp.meter_actuator_health.valid, inp.meter_actuator_health)

    def test_new_policy_receives_separate_source_classes(self):
        # This case has explicitly mapped native L1 evidence. The aggregate
        # MAX-only case below must not silently assume the same phase context.
        self.adapter.native_controlled_l1_observation = SimpleNamespace(
            current=8., received_at=self.wall(99), sequence=1, evidence_credit=True,
            source_identity=self.owner, mapping_epoch=1, output_phase=10,
            mapping_adopted_at=self.wall(98), mapping_sequence=1,
            source_at=self.wall(99), source_highwater_at=self.wall(99),
            mapping_source_at=self.wall(98), mapping_source_highwater_at=self.wall(98),
            error=None, observation_id=183, committed=True, current_error=None)
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertTrue(inp.meter_ev_health.valid)
        self.assertEqual(inp.charger_report_ts, self.wall(99).timestamp())

    def test_selected_max_only_adapter_has_no_mapped_l1_increase_credit(self):
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertIsNone(inp.controlled_l1)
        self.assertEqual(inp.charger_current, 8.)

    def test_invalid_native_vector_is_ev_quality_not_a_grid_or_actuator_fault(self):
        self.native.error = "synthetic_invalid_observation_vector"
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertFalse(inp.data_valid)  # The legacy error contract is retained.
        self.assertFalse(inp.charger_current_trusted)

    def test_missing_ev_numeric_value_does_not_reclassify_valid_raw(self):
        del self.states["sensor.car"]
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertFalse(inp.data_valid)

    def test_old_ev_sample_with_fresh_source_withdraws_only_increase_credit(self):
        self.set_native(8, received=-1000, changed_at=90)
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertEqual(inp.charger_report_ts, self.wall(-1000).timestamp())

    def test_new_receipt_of_old_provider_event_is_not_new_rise_evidence(self):
        self.set_native(8, received=99, source=-1000, changed_at=90)
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertEqual(inp.charger_report_ts, self.wall(99).timestamp())
        self.assertEqual(inp.charger_event_source_ts, self.wall(-1000).timestamp())

    def test_stale_raw_and_invalid_units_are_critical_even_with_fresh_ev(self):
        for change in ("clock", "unit", "number"):
            with self.subTest(change=change):
                self.states["sensor.grid"].last_reported = self.wall()
                self.states["sensor.grid"].attributes["unit_of_measurement"] = "A"
                self.states["sensor.grid"].state = "16"
                if change == "clock":
                    self.states["sensor.grid"].last_reported = self.wall(-1000)
                elif change == "unit":
                    self.states["sensor.grid"].attributes["unit_of_measurement"] = "W"
                else:
                    self.states["sensor.grid"].state = "nan"
                inp = self.controller._read_input()
                self.assertFalse(inp.meter_grid_health.valid)
                self.assertTrue(inp.meter_actuator_health.valid)

    def test_stream_fault_unknown_status_and_binding_change_are_critical(self):
        self.adapter.telemetry_block_reason = "synthetic_stream_lost"
        self.assertFalse(self.controller._read_input().meter_actuator_health.valid)
        self.adapter.telemetry_block_reason = None
        self.states["sensor.status"].state = "error"
        self.assertFalse(self.controller._read_input().meter_actuator_health.valid)
        self.states["sensor.status"].state = "charging"
        self.native.source_identity = (91, 92, 93, 94)
        self.assertFalse(self.controller._read_input().meter_actuator_health.valid)

    def test_manual_pause_is_session_state_without_fabricating_a_source_fault(self):
        self.states["sensor.status"].state = "paused"
        inp = self.controller._read_input()
        self.assert_critical_sources_valid(inp)
        self.assertEqual(inp.status, "paused")

    def test_runtime_persistence_fault_remains_critical_when_ev_is_invalid(self):
        self.native.error = "synthetic_invalid_observation_vector"
        inp = self.controller._critical_runtime_input(self.controller._read_input(), "overload_budget_save_failed")
        self.assertTrue(inp.meter_grid_health.valid)
        self.assertFalse(inp.meter_actuator_health.valid)
        self.assertFalse(inp.meter_ev_health.valid)
        self.assertFalse(inp.data_valid)

    def test_legacy_input_keeps_error_semantics_and_no_new_source_authority(self):
        self.entry.options[KEY] = False
        self.controller._reload_settings_locked()
        self.native.error = "synthetic_invalid_observation_vector"
        inp = self.controller._read_input()
        self.assertFalse(inp.data_valid)
        self.assertIsNone(inp.meter_grid_health)
        self.assertIsNone(inp.meter_actuator_health)
        self.assertIsNone(inp.meter_ev_health)

    async def test_unsupported_switch_update_is_atomic_before_transport_or_generation(self):
        # 17 A is valid for the legacy controller but outside the selected
        # single-phase vehicle profile. Legacy estimator opt-outs are now valid.
        self.entry.options.update(meter_envelope_control=False, max_charge_amps=17)
        self.controller._reload_settings_locked()
        before = deepcopy(self.entry.options)
        generation = self.controller._generation
        with self.assertRaises(ValueError):
            await self.controller.async_set_switch(KEY, True)
        self.assertEqual(self.entry.options, before)
        self.assertEqual(self.controller._generation, generation)
        self.assertEqual(self.adapter.calls, [])

    async def seed_legacy_live_offer(self):
        self.entry.options[KEY] = False
        self.controller._reload_settings_locked()
        self.states["sensor.grid"].state = "12"
        self.set_native(8, received=99, changed_at=90)
        await self.controller.async_tick()
        self.now += 1
        self.set_native(8, received=self.now, changed_at=self.now,
            sequences=((182, 2), (183, 0), (184, 0), (185, 0)))
        self.now += .3
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual(self.adapter.calls[-1].current_p1, 16)
        self.now += 5
        self.states["sensor.grid"].last_reported = self.wall()
        self.adapter.calls.clear()

    async def test_handoff_preserves_real_accepted_lease_and_exposure_without_zero(self):
        await self.seed_legacy_live_offer()
        r = self.controller.regulator
        accepted = (r.last_target, r.last_set_limit_ts, r._accepted_control_source_id)
        exposure = deepcopy(r.export_overload_state())
        await self.controller.async_set_switch(KEY, True)
        self.assertTrue(self.controller.settings.meter_envelope_control)
        self.assertEqual((r.last_target, r.last_set_limit_ts, r._accepted_control_source_id), accepted)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.controller.last_decision.current_p1, 16)
        self.assertIsNone(self.controller.last_decision.meter_zero_cause)
        after = r.export_overload_state()
        self.assertEqual(after["used_seconds"], exposure["used_seconds"])
        self.assertEqual(after["failure_reason"], exposure["failure_reason"])

    async def test_handoff_cannot_clear_actual_unknown_delivery_barrier(self):
        await self.seed_legacy_live_offer()
        self.controller._protective_zero_pending = True
        await self.controller.async_set_switch(KEY, True)
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertEqual(self.controller.last_decision.meter_zero_cause,
                         publication.fixtures.fixtures.logic.ElectricalStopCause.UNKNOWN_DELIVERY)
        view = self.controller.diagnostic_attributes()
        self.assertEqual(view["meter_control_health"], "blocked")
        self.assertTrue(view["meter_grid_valid"])
        self.assertTrue(view["meter_actuator_valid"])
        self.assertEqual(view["easee_source_health"], "healthy")

    async def test_estimate_only_failure_keeps_real_live_offer_and_renews_without_ev_clock_credit(self):
        await self.seed_legacy_live_offer()
        await self.controller.async_set_switch(KEY, True)
        ev_clock = self.controller._read_input().charger_report_ts
        self.native.error = "synthetic_invalid_observation_vector"
        # More than three real finite leases. Only RAW advances; neither the
        # old EV receipt nor product/command receipts become new EV samples.
        for _ in range(20):
            self.now += 10
            self.states["sensor.grid"].last_reported = self.wall()
            await self.controller.async_tick()
            self.assertEqual(self.controller.last_decision.current_p1, 16)
            self.assertIsNone(self.controller.last_decision.meter_zero_cause)
            self.assertEqual(self.controller._read_input().charger_report_ts, ev_clock)
        self.assertGreaterEqual(len(self.adapter.calls), 6)
        self.assertTrue(all(call.current_p1 == 16 for call in self.adapter.calls))
        self.assertEqual(self.controller.source_diagnostics["source_health"], "unhealthy")
        self.assertEqual(self.controller.source_diagnostics["meter_control_health"], "healthy")
        self.assertEqual(self.controller.source_diagnostics["meter_estimate_quality"], "unavailable_for_increase")

    async def test_actual_stream_failure_still_protects_under_low_raw_load(self):
        await self.seed_legacy_live_offer()
        await self.controller.async_set_switch(KEY, True)
        self.adapter.telemetry_block_reason = "synthetic_stream_lost"
        self.now += 1
        self.states["sensor.grid"].last_reported = self.wall()
        await self.controller.async_tick()
        self.assertEqual([call.current_p1 for call in self.adapter.calls], [0])
        self.assertEqual(self.controller.last_decision.meter_zero_cause,
                         publication.fixtures.fixtures.logic.ElectricalStopCause.ACTUATOR_INVALID)


class MeterSourcePublicationTests(unittest.TestCase):
    def test_actual_sensor_two_pass_compaction_retains_critical_and_estimate_distinction(self):
        runtime = publication.runtime
        typed = dict(meter_envelope_control=True, meter_control_health="healthy",
            meter_estimate_quality="unavailable_for_increase", meter_grid_valid=True,
            meter_actuator_valid=True, meter_ev_valid=False,
            meter_state="normal", meter_zero_cause=None)
        payload = {**publication.large_diagnostics(), **typed, "source_health": "unhealthy"}
        decision = publication.fixtures.fixtures.logic.RegulatorDecision(
            mode="meter_normal", reason="Synthetic delayed EV estimate. " * 100,
            command=None, service=None, current_p1=12, current_p2=0, current_p3=0,
            ttl_seconds=60, available_l1=12, house_load_l1=10., grid_l1=22.6,
            charger_current=12.61, paused_by_us=False, should_call_service=False,
            dry_run=False, meter_envelope=True, meter_state="normal")
        controller = SimpleNamespace(last_decision=decision, source_diagnostics=deepcopy(payload),
            last_service_result="not_sent", last_service_error=None, control_block_reason=None,
            compact_state_attributes=runtime._compact_state_attributes,
            diagnostic_attributes=lambda: runtime._compact_state_attributes(payload))
        module = publication.fixtures.sensor_module()
        description = next(d for d in module.SENSOR_DESCRIPTIONS if d.key == "source_health")
        sensor = module.OneFuseSensor(controller,
            SimpleNamespace(entry_id="synthetic-entry", title="Synthetic controller"), description)
        self.assertEqual(sensor.native_value, "unhealthy")  # Legacy diagnostics stay honest.
        view = sensor.extra_state_attributes
        for key, value in typed.items():
            with self.subTest(key=key):
                self.assertEqual(view[key], value)
        self.assertLessEqual(publication.encoded_size(view), runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)

    def test_control_block_is_visible_without_misreporting_meter_or_transport_health(self):
        runtime = publication.runtime
        typed = dict(meter_envelope_control=True, meter_control_health="healthy",
            meter_estimate_quality="usable", meter_grid_valid=True,
            meter_actuator_valid=True, meter_ev_valid=True)
        payload = {**publication.large_diagnostics(), **typed}
        cause = publication.fixtures.fixtures.logic.ElectricalStopCause.UNKNOWN_DELIVERY.value
        decision = publication.fixtures.fixtures.logic.RegulatorDecision(
            mode="meter_fault", reason="Synthetic unknown command delivery.",
            command="set_limit", service="easee.set_circuit_dynamic_limit",
            current_p1=0, current_p2=0, current_p3=0, ttl_seconds=60,
            available_l1=0, house_load_l1=3., grid_l1=3., charger_current=0.,
            paused_by_us=False, should_call_service=True, dry_run=False,
            meter_envelope=True, meter_state="meter_fault", meter_zero_cause=cause)
        controller = SimpleNamespace(last_decision=decision, source_diagnostics=payload,
            last_service_result="submitted_unverified", last_service_error=None, control_block_reason=None,
            compact_state_attributes=runtime._compact_state_attributes,
            diagnostic_attributes=lambda: runtime._compact_state_attributes(payload))
        module = publication.fixtures.sensor_module()
        for key in ("source_health", "target_p1", "reason"):
            with self.subTest(key=key):
                sensor = module.OneFuseSensor(controller,
                    SimpleNamespace(entry_id="synthetic-entry", title="Synthetic controller"),
                    next(d for d in module.SENSOR_DESCRIPTIONS if d.key == key))
                view = sensor.extra_state_attributes
                self.assertEqual(view["meter_control_health"], "blocked")
                self.assertTrue(view["meter_grid_valid"])
                self.assertTrue(view["meter_actuator_valid"])
                self.assertEqual(view["meter_zero_cause"], cause)
                self.assertLessEqual(publication.encoded_size(view), runtime.DIAGNOSTIC_ATTRIBUTE_BUDGET_BYTES)


@unittest.skipUnless(ha_fixtures.HAS_HA, "Real Home Assistant is installed in the CI job")
class MeterEnvelopeOptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha_fixtures.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha_fixtures.RealHomeAssistantTests.asyncTearDown

    async def test_flow_requires_explicit_supported_opt_in_without_entry_mutation(self):
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        flow = self.flow_module.OneFuseLoaderOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        before = dict(self.entry.options)
        form = await flow.async_step_init()
        marker = next(key for key in form["data_schema"].schema if str(key.schema) == KEY)
        self.assertIs(marker.default(), False)
        self.assertIs(form["data_schema"].schema[marker], bool)
        invalid = await flow.async_step_init({KEY: True, "max_charge_amps": 17})
        self.assertEqual(invalid["type"], "form")
        self.assertEqual(invalid["errors"].get("base"), "invalid_settings")
        self.assertEqual(dict(self.entry.options), before)
        ordinary = await flow.async_step_init({KEY: True})
        self.assertEqual(ordinary["type"], "create_entry")
        self.assertIs(ordinary["data"][KEY], True)
        for key in PROFILE:
            self.assertEqual(ordinary["data"][key], before[key])
        self.assertEqual(dict(self.entry.options), before)
        accepted = await flow.async_step_init({**PROFILE, KEY: True})
        self.assertEqual(accepted["type"], "create_entry")
        self.assertIs(accepted["data"][KEY], True)
        self.assertEqual(dict(self.entry.options), before)


if __name__ == "__main__":
    unittest.main()
