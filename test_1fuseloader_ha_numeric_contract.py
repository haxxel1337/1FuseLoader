"""Check representation identity against the installed real HA formatter."""
import importlib
import importlib.util
import os
import struct
import unittest

try:
    HAS_HA = importlib.util.find_spec("homeassistant") is not None
except ValueError:
    HAS_HA = False  # Standalone synthetic suites may already have installed fakes.
if os.environ.get("ONEFUSE_REQUIRE_HOMEASSISTANT") and not HAS_HA:
    raise RuntimeError("CI must install real Home Assistant for the numeric contract")


@unittest.skipUnless(HAS_HA, "Real Home Assistant is installed in the CI job")
class RealHaNumericContractTests(unittest.TestCase):
    def test_native_float_identity_uses_actual_entity_state_serialization(self):
        from homeassistant.helpers.entity import Entity, FLOAT_PRECISION
        component = importlib.import_module("custom_components.1fuseloader")

        class NativeFloatEntity(Entity):
            def __init__(self, value):
                self._value = value

            @property
            def state(self):
                return self._value

        self.assertEqual(component._HA_FLOAT_PRECISION, FLOAT_PRECISION)
        changed = 0
        for amperes in (16.059, 9.965, 1.418, 4.190, 3.318, 7.094):
            with self.subTest(amperes=amperes):
                raw = struct.unpack("<f", struct.pack("<f", amperes))[0]
                # Call HA itself: this test deliberately does not supply a
                # copied .15 formatter as the input representation.
                projected = NativeFloatEntity(raw)._stringify_state(True)
                changed += float(projected) != raw
                self.assertEqual(component._native_current_for_ha_state(projected, raw), raw)
                self.assertIsNone(component._native_current_for_ha_state(projected, raw+.001))
        self.assertGreater(changed, 0, "Fixture must exercise real serialization precision loss")


if __name__ == "__main__":
    unittest.main(verbosity=2)
