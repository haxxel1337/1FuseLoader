"""Offline contract tests against the real, pinned pyeasee implementation.

Only the HTTP transport's post method is replaced. Site, Circuit, Charger and
their public methods are real pyeasee 0.8.17 objects. No credentials, HA instance
or network connection are used. Optional locally; CI sets
ONEFUSE_REQUIRE_PYEASEE_CONTRACT=1 so a missing dependency is a failure.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

REQUIRE_PYEASEE = os.environ.get("ONEFUSE_REQUIRE_PYEASEE_CONTRACT") == "1"

try:
    import pyeasee
    from pyeasee.exceptions import (
        BadRequestException,
        ForbiddenServiceException,
        ServerFailureException,
    )
    from pyeasee.site import Circuit, Site
except ModuleNotFoundError as error:
    # A broken installation (missing transitive dependency) must not look like
    # an optional local skip. Only absence of pyeasee itself may be skipped.
    if error.name != "pyeasee" or REQUIRE_PYEASEE:
        raise RuntimeError(
            "The required pyeasee 0.8.17 contract dependency could not be imported."
        ) from error
    PYEASEE_AVAILABLE = False
else:
    PYEASEE_AVAILABLE = True


@unittest.skipUnless(
    PYEASEE_AVAILABLE,
    "Optional local contract test: pyeasee 0.8.17 is not installed.",
)
class RealPyeaseeContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.response = SimpleNamespace(status=202)
        self.transport = SimpleNamespace(post=AsyncMock(return_value=self.response))
        self.site = Site({"id": 123, "name": "Offline contract test"}, self.transport)
        self.circuit = Circuit(
            {
                "id": 456,
                "chargers": [
                    {
                        "id": "CONTRACT_TEST_CHARGER",
                        "name": "Offline contract test charger",
                        "productCode": 1,
                        "levelOfAccess": 1,
                    }
                ],
            },
            self.site,
            self.transport,
        )
        # Exercise the real factory so the ownership links used by the adapter
        # (charger.circuit and circuit.site) are part of the checked contract.
        self.charger = self.circuit.get_chargers()[0]

    async def test_exact_target_version_and_real_ownership_links(self):
        self.assertEqual(pyeasee.__version__, "0.8.17")
        self.assertIs(self.charger.circuit, self.circuit)
        self.assertIs(self.charger.site, self.site)
        self.assertIs(self.charger.easee, self.transport)
        self.transport.post.assert_not_awaited()

    async def test_dynamic_current_preserves_minute_ttl_and_explicit_zero_phases(self):
        for minutes in (1, 2, 3):
            with self.subTest(minutes=minutes):
                result = await self.circuit.set_dynamic_current(12, 0, 0, minutes)
                self.assertIs(result, self.response)
                self.transport.post.assert_awaited_with(
                    "/api/sites/123/circuits/456/dynamicCurrent",
                    json={"phase1": 12, "phase2": 0, "phase3": 0, "timeToLive": minutes},
                )

    async def test_identical_limits_reach_transport_each_time(self):
        await self.circuit.set_dynamic_current(12, 0, 0, 1)
        await self.circuit.set_dynamic_current(12, 0, 0, 1)
        self.assertEqual(self.transport.post.await_count, 2)
        self.assertEqual(
            self.transport.post.await_args_list[0],
            self.transport.post.await_args_list[1],
        )

    async def test_omitted_phase_limits_copy_phase_one_and_default_ttl_is_zero(self):
        # This is why the adapter must pass explicit zeros and a finite TTL.
        await self.circuit.set_dynamic_current(12)
        self.transport.post.assert_awaited_once_with(
            "/api/sites/123/circuits/456/dynamicCurrent",
            json={"phase1": 12, "phase2": 12, "phase3": 12, "timeToLive": 0},
        )

    async def test_pause_uses_public_command_endpoint_and_returns_response(self):
        result = await self.charger.pause()
        self.assertIs(result, self.response)
        self.transport.post.assert_awaited_once_with(
            "/api/chargers/CONTRACT_TEST_CHARGER/commands/pause_charging"
        )

    async def test_resume_uses_public_command_endpoint_and_returns_response(self):
        result = await self.charger.resume()
        self.assertIs(result, self.response)
        self.transport.post.assert_awaited_once_with(
            "/api/chargers/CONTRACT_TEST_CHARGER/commands/resume_charging"
        )

    async def test_server_failure_returns_none_for_each_public_method(self):
        for name, method, args in (
            ("limit", self.circuit.set_dynamic_current, (12, 0, 0, 1)),
            ("pause", self.charger.pause, ()),
            ("resume", self.charger.resume, ()),
        ):
            with self.subTest(method=name):
                self.transport.post.reset_mock()
                self.transport.post.side_effect = ServerFailureException("offline test")
                self.assertIsNone(await method(*args))
                self.transport.post.assert_awaited_once()

    async def test_non_server_api_failures_propagate(self):
        for error_type in (BadRequestException, ForbiddenServiceException):
            for name, method, args in (
                ("limit", self.circuit.set_dynamic_current, (12, 0, 0, 1)),
                ("pause", self.charger.pause, ()),
                ("resume", self.charger.resume, ()),
            ):
                with self.subTest(method=name, error=error_type.__name__):
                    error = error_type({"title": "Offline contract failure"})
                    self.transport.post.side_effect = error
                    with self.assertRaises(error_type) as raised:
                        await method(*args)
                    self.assertIs(raised.exception, error)

    async def test_cancellation_is_not_swallowed(self):
        self.transport.post.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.circuit.set_dynamic_current(0, 0, 0, 1)


if __name__ == "__main__":
    unittest.main()
