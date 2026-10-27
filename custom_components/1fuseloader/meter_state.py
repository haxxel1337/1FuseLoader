"""Standalone electrical control state, distinct from budget and history."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ControlState(str, Enum):
    NORMAL = 'NORMAL'
    REDUCING = 'REDUCING'
    GRID_WAIT = 'GRID_WAIT'
    MINIMUM_HOLD = 'MINIMUM_HOLD'
    STOP_RECOVERY = 'STOP_RECOVERY'


@dataclass
class ElectricalEpisode:
    episode_id: int
    source_id: object
    origin: Optional[float]
    deadline: Optional[float]
    grid_source_ts: Optional[float]
    previous_authority: float
    phase: ControlState = ControlState.REDUCING
    interrupted: bool = False
    failure: Optional[str] = None
    minimum_dispatch: Optional[float] = None
    minimum_floor: Optional[dict] = None
    response_ts: Optional[float] = None
    response_at: Optional[float] = None
    grid_deadline: Optional[float] = None
    grid_floor: Optional[float] = None
    current_value: Optional[float] = None
    ceiling: Optional[float] = None
    overload_authority: bool = True

    @property
    def unresolved(self):
        return self.phase in {ControlState.REDUCING, ControlState.GRID_WAIT} or self.interrupted or self.failure is not None


@dataclass
class LeaseState:
    cap: int = 0
    origin: Optional[float] = None
    expires: Optional[float] = None
    source_id: object = None
    accepted_floor: Optional[dict] = None
    upper: float = 0.
    possible_positive: dict = field(default_factory=dict)
    zero_floor: Optional[dict] = None
    zero_dispatch: Optional[float] = None
    cold_until: Optional[float] = None
    cold_pending: bool = False
    minimum_verified: bool = False
    normal_house: Optional[float] = None
    normal_origin: Optional[float] = None

    def live(self, now):
        return self.cap > 0 and self.origin is not None and self.expires is not None and self.origin <= now < self.expires


@dataclass
class RecoveryState:
    required: bool = False
    stage: str = 'none'
    stable_since: Optional[float] = None
    stable_cap: Optional[int] = None
    normal_dispatch: Optional[float] = None
    normal_cap: Optional[int] = None
    normal_floor: Optional[dict] = None
    candidate_plan: Optional[int] = None
    old_authority_until: Optional[float] = None
