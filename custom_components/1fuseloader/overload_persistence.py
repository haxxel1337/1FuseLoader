"""Persist an operational exposure counter through HA's public Store API.

No sensor history, identifiers or credentials are stored. Downtime following
observed overload is charged in full; it never supplies assumed cooling credit.
"""
from __future__ import annotations

from math import isfinite
from time import time
from typing import Any


class OverloadBudgetStore:
    def __init__(self, store: Any, clock=time) -> None:
        self.store = store
        self.clock = clock

    async def async_load(self) -> dict[str, Any] | None:
        payload = await self.store.async_load()
        if payload is None:
            return None
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                or payload["version"] != 1):
            raise ValueError("Invalid exposure envelope")
        saved_at, now = payload.get("saved_at"), self.clock()
        if (isinstance(saved_at, bool) or not isinstance(saved_at, (int, float))
                or not isfinite(saved_at) or not isfinite(now) or saved_at < 0
                or saved_at > now + 5):
            raise ValueError("Invalid exposure wall clock")
        state = payload.get("regulator")
        if not isinstance(state, dict):
            raise ValueError("Invalid exposure state")
        state = dict(state)
        used = state.get("used_seconds")
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not isfinite(used) or used < 0
                or type(state.get("exposure_active")) is not bool
                or type(state.get("minimum_held")) is not bool
                or type(state.get("transition_pending")) is not bool):
            raise ValueError("Invalid exposure accounting")
        if state["exposure_active"]:
            state["used_seconds"] = used + max(0.0, now - saved_at)
        return state

    async def async_save(self, state: dict[str, Any]) -> None:
        await self.store.async_save({
            "version": 1, "saved_at": self.clock(), "regulator": state,
        })
