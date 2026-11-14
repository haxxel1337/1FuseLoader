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


@dataclass(frozen=True)
class OperatingPermission:
    """One measured-feedback action for an actual admitted normal lease.

    A decrease is provisional curtailment, not a household/EV subtraction
    certificate. A hold cannot submit a command or extend any lease.
    """
    kind: str
    cap: int
    accepted_cap: int
    raw: float
    normal_budget: float
    lease_expires: float
    source_id: object
    hold_until: Optional[float] = None


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
class RawExcursion:
    """A fixed measured-total deadline, independent of EV reduction I/O.

    These runtime clocks never survive boot as renewed authority. Persistence
    records an active excursion only so a new process enters recovery instead
    of manufacturing another ten-second allowance.
    """
    source_id: object
    origin: float
    deadline: float
    first_grid_ts: float
    last_high_grid_ts: float
    ceiling: float


@dataclass(frozen=True)
class MinimumCertificate:
    """One genuine minimum observation under an actual accepted boundary.

    Advisory publication gaps may retain this proof within its original
    finite bound. No persisted state, proposed cap or later callback can
    construct it, and it supplies no household subtraction/increase credit.
    """
    source_id: object
    mapping_key: tuple
    accepted_floor: dict
    receiver_scope: object
    expires: float
    received_at_ts: float
    sequence: int


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
    # Runtime proof of an actual accepted ordinary allocation. Persisted
    # history, a proposed cap and a mapping update cannot manufacture it.
    normal_admitted: bool = False
    # Actual ACKs only. A known own decrease may retain its preceding possible
    # command horizons; imported or unacknowledged authority is not a chain.
    normal_commands: dict = field(default_factory=dict)
    feedback_parents: dict = field(default_factory=dict)

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
    operating_since: Optional[float] = None
    operating_grid_since: Optional[float] = None
    operating_last_grid: Optional[float] = None
    operating_origin: Optional[float] = None
    operating_source: object = None
    operating_dispatch: Optional[float] = None
    operating_dispatch_wall: Optional[float] = None
    operating_cap: Optional[int] = None
    operating_binding: object = None
