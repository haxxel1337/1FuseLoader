"""Exposure persistence regressions without a Home Assistant installation."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


COMPONENT = Path(__file__).parent / "custom_components" / "1fuseloader"
spec = importlib.util.spec_from_file_location("onefuse_persistence_tests", COMPONENT / "overload_persistence.py")
assert spec and spec.loader
persistence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(persistence)


class MemoryStore:
    def __init__(self, data=None):
        self.data = deepcopy(data)
        self.read_error = None
        self.write_error = None

    async def async_load(self):
        if self.read_error:
            raise self.read_error
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.write_error:
            raise self.write_error
        self.data = deepcopy(data)


def state(**changes):
    return {"version": 1, "used_seconds": 120.0, "exposure_active": True,
            "transition_pending": False, "minimum_held": False, "failure_reason": None, **changes}


def envelope(**changes):
    return {"version": 1, "saved_at": 1000.0, "regulator": state(), **changes}


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_storage_is_a_new_counter_not_an_invalid_payload(self):
        store = persistence.OverloadBudgetStore(MemoryStore(), clock=lambda: 1010)
        self.assertIsNone(await store.async_load())

    async def test_invalid_outer_payload_and_version_are_rejected(self):
        for payload in ([], "invalid", {}, envelope(version=2), envelope(version=True), envelope(regulator=None)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await persistence.OverloadBudgetStore(MemoryStore(payload), clock=lambda: 1010).async_load()

    async def test_invalid_save_timestamps_are_rejected(self):
        for saved_at in (None, True, "1000", -1, float("nan"), float("inf"), 1016):
            with self.subTest(saved_at=saved_at), self.assertRaises(ValueError):
                await persistence.OverloadBudgetStore(MemoryStore(envelope(saved_at=saved_at)), clock=lambda: 1010).async_load()

    async def test_small_forward_clock_skew_does_not_refund_exposure(self):
        saved = MemoryStore(envelope(saved_at=1015))
        loaded = await persistence.OverloadBudgetStore(saved, clock=lambda: 1010).async_load()
        self.assertEqual(loaded["used_seconds"], 120)

    async def test_invalid_current_clock_is_rejected(self):
        for now in (float("nan"), float("inf")):
            with self.subTest(now=now), self.assertRaises(ValueError):
                await persistence.OverloadBudgetStore(MemoryStore(envelope()), clock=lambda: now).async_load()

    async def test_downtime_consumes_budget_only_after_unresolved_exposure(self):
        for active, expected in ((True, 150), (False, 120)):
            with self.subTest(active=active):
                raw = MemoryStore(envelope(regulator=state(exposure_active=active)))
                loaded = await persistence.OverloadBudgetStore(raw, clock=lambda: 1030).async_load()
                self.assertEqual(loaded["used_seconds"], expected)
                self.assertEqual(loaded["exposure_active"], active)
                self.assertEqual(raw.data["regulator"]["used_seconds"], 120)

    async def test_invalid_counters_and_flags_do_not_become_fresh_budget(self):
        for changes in (
            {"used_seconds": -1}, {"used_seconds": True}, {"used_seconds": float("nan")},
            {"used_seconds": float("inf")}, {"exposure_active": 1}, {"transition_pending": "false"},
            {"minimum_held": "false"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await persistence.OverloadBudgetStore(MemoryStore(envelope(regulator=state(**changes))), clock=lambda: 1030).async_load()

    async def test_save_reload_keeps_exhausted_budget_and_latched_failure(self):
        raw = MemoryStore()
        clock = [1000.0]
        store = persistence.OverloadBudgetStore(raw, clock=lambda: clock[0])
        exhausted = state(used_seconds=1800, exposure_active=False,
                          transition_pending=True, failure_reason="response_timeout")
        await store.async_save(exhausted)
        self.assertEqual(raw.data["regulator"], exhausted)
        self.assertEqual(raw.data["version"], 1)
        self.assertEqual(raw.data["saved_at"], 1000)
        self.assertRegex(raw.data["checkpoint_revision"], r"^[0-9a-f]{32}$")
        clock[0] += 3600
        self.assertEqual(await store.async_load(), exhausted)
        # Saving and reloading it again is not an implicit new test allowance.
        await store.async_save(await store.async_load())
        clock[0] += 3600
        self.assertEqual(await store.async_load(), exhausted)

    async def test_storage_read_and_write_failures_propagate_to_controller(self):
        raw = MemoryStore(envelope())
        original = deepcopy(raw.data)
        store = persistence.OverloadBudgetStore(raw, clock=lambda: 1030)
        raw.read_error = OSError("read failure")
        with self.assertRaises(OSError):
            await store.async_load()
        raw.write_error = OSError("write failure")
        with self.assertRaises(OSError):
            await store.async_save(state(used_seconds=1800))
        self.assertEqual(raw.data, original)


if __name__ == "__main__":
    unittest.main()
