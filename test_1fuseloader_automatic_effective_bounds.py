"""The automatic profile must retain its effective 7 A start/minimum bound."""
from copy import deepcopy
from dataclasses import replace
import importlib
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_1fuseloader_config as settings
import test_1fuseloader_ha as ha
import test_1fuseloader_peak_response_policy as peak

const = settings.const
PROFILE = dict(automatic_overload_cycles=True, peak_only_response=True,
               main_fuse_amps=25, startup_charge_amps=7)


class EffectiveAutomaticBoundsTests(unittest.TestCase):
    def test_automatic_profile_accepts_only_bounds_containing_seven(self):
        for minimum in (6, 7, 8, 10, 16):
            for maximum in (6, 7, 16, 32):
                options = {**PROFILE, "min_charge_amps": minimum,
                           "max_charge_amps": maximum}
                original = deepcopy(options)
                with self.subTest(minimum=minimum, maximum=maximum):
                    if 6 <= minimum <= 7 <= maximum:
                        normalized = const.validate_options(options)
                        self.assertEqual(normalized["startup_charge_amps"], 7)
                    else:
                        with self.assertRaises(ValueError):
                            const.validate_options(options)
                    self.assertEqual(options, original)

    def test_inactive_master_does_not_make_incompatible_selected_profile_valid(self):
        for change in ({"min_charge_amps": 8}, {"max_charge_amps": 6}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                const.validate_options({**PROFILE, "overload_enabled": False, **change})

    def test_legacy_manual_minimum_maximum_clamping_remains_supported(self):
        for change in ({"min_charge_amps": 8}, {"max_charge_amps": 6}):
            options = const.validate_options({**PROFILE,
                "automatic_overload_cycles": False, **change})
            self.assertEqual(options["startup_charge_amps"], 7)
            self.assertFalse(options["automatic_overload_cycles"])

    def test_core_update_rejects_the_same_incompatible_bounds_without_mutation(self):
        r = peak.logic.OneFuseRegulator(peak.logic.RegulatorSettings(**PROFILE))
        previous = r.settings
        for change in ({"min_charge_amps": 8}, {"max_charge_amps": 6}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                r.update_settings(replace(previous, **change))
            self.assertIs(r.settings, previous)

    def test_an_already_accepted_stricter_six_is_never_raised_during_a_peak(self):
        helper = peak.PeakResponsePolicyTests()
        r = helper.legacy_profile(True)
        first = helper.legacy_step(r, 0., 23., 6.)
        self.assertEqual(first.current_p1, 6)
        self.assertEqual(r.last_target[0], 6)
        r.update_settings(replace(r.settings, automatic_overload_cycles=True))
        decision = helper.legacy_step(r, 1., 31., 6., accept=False)
        self.assertLessEqual(decision.current_p1, 6)
        self.assertEqual((decision.current_p2, decision.current_p3), (0, 0))
        self.assertNotEqual(decision.command, "resume")
        self.assertEqual(r.last_target[0], 6)


@unittest.skipUnless(ha.HAS_HA, "Real Home Assistant is installed in CI")
class EffectiveAutomaticBoundsRealHATests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ha.RealHomeAssistantTests.asyncSetUp
    asyncTearDown = ha.RealHomeAssistantTests.asyncTearDown

    def profile_entry(self):
        # Real HA deliberately exposes immutable entry options. Configure a
        # real entry through the same public constructor as the base fixture.
        entry = self.entry
        return type(entry)(
            version=entry.version, minor_version=entry.minor_version,
            domain=entry.domain, title=entry.title, data=dict(entry.data),
            options={**entry.options, **PROFILE}, source=entry.source,
            unique_id=entry.unique_id, discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )

    async def test_options_flow_rejects_effectively_non_seven_profile_atomically(self):
        self.entry = self.profile_entry()
        self.hass.config_entries = SimpleNamespace(async_get_known_entry=lambda _: self.entry)
        for change in ({"min_charge_amps": 8}, {"max_charge_amps": 6}):
            with self.subTest(change=change):
                previous = dict(self.entry.options)
                flow = self.flow_module.OneFuseLoaderOptionsFlow()
                flow.hass, flow.handler = self.hass, self.entry.entry_id
                result = await flow.async_step_init(change)
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"].get("base"), "invalid_settings")
                self.assertEqual(dict(self.entry.options), previous)

    async def test_numbers_reject_invalid_effective_profile_before_controller_update(self):
        self.entry = self.profile_entry()
        numbers = importlib.import_module("custom_components.1fuseloader.number")
        for key, value in (("min_charge_amps", 8), ("max_charge_amps", 6)):
            with self.subTest(key=key):
                controller = SimpleNamespace(async_set_option=AsyncMock())
                description = next(item for item in numbers.NUMBER_DESCRIPTIONS if item.key == key)
                entity = numbers.OneFuseNumber(controller, self.entry, description)
                from homeassistant.exceptions import ServiceValidationError
                previous = dict(self.entry.options)
                with self.assertRaises(ServiceValidationError):
                    await entity.async_set_native_value(value)
                controller.async_set_option.assert_not_awaited()
                self.assertEqual(dict(self.entry.options), previous)


if __name__ == "__main__":
    unittest.main()
