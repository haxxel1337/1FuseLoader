from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import floor, isfinite
from typing import Optional

# Native Easee states, plus legacy aliases retained for existing test fixtures.
ALLOWED_ACTIVE_STATUSES = {
    "charging", "ready_to_charge", "awaiting_start", "awaiting_load_balancing",
    "awaiting_smart_start", "awaiting_scheduled_start", "start_charging",
    "stop_charging", "awaiting_authorization", "de_authorizing", "authenticating",
    "paused_due_to_equalizer", "searching_for_master", "waiting_for_schedule",
    "connected", "buffering", "paused",
}
ALLOWED_RESUME_STATUSES = {
    "awaiting_start", "ready_to_charge", "awaiting_load_balancing",
    "awaiting_smart_start", "awaiting_scheduled_start", "paused", "connected", "buffering",
}
IDLE_STATUSES = {"disconnected", "completed"}
PAUSED_STATUSES = {"paused"}
# These states must retain the charger's own authorization/schedule/pause policy.
START_ALLOWED_STATUSES = {
    "charging", "start_charging", "ready_to_charge", "awaiting_start",
    "awaiting_load_balancing", "connected", "buffering",
}
START_RESPONSE_DIAGNOSTIC_SECONDS = 300
PEAK_RESPONSE_TRIGGER_AMPS = 28.0
AUTOMATIC_QUALIFY_SECONDS = 30.0
AUTOMATIC_COOLING_SECONDS = 1800.0
AUTOMATIC_COOLING_AMPS = 23.0
AUTOMATIC_MAX_GRID_GAP_SECONDS = 15.0


def _valid_number(value: object, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, "unknown", "unavailable", "None", ""):
            return default
        number = float(value)
        return number if isfinite(number) else default
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class RegulatorSettings:
    main_fuse_amps: float = 25.0
    buffer_amps: float = 2.0
    min_charge_amps: int = 6
    max_charge_amps: int = 16
    startup_charge_amps: int = 7
    fast_recovery: bool = True
    # Explicit installation assumption; never infer it from a GET receipt.
    nominal_import_only_fallback: bool = False
    bounded_nominal_shedding: bool = False
    # Explicit installation policy: only measured raw >28 A arms electrical10s.
    # Existing installations keep their older response contract until selected.
    peak_only_response: bool = False
    automatic_overload_cycles: bool = False
    minimum_reserve_amps: float = 0.5
    increase_stable_seconds: int = 30
    increase_interval_seconds: int = 15
    restart_stable_seconds: int = 60
    measurement_settle_seconds: int = 10
    safe_l1_amps: float = 23.0
    soft_limit_amps: float = 28.0
    soft_for_seconds: int = 20
    soft_current_amps: int = 6
    pause_limit_amps: float = 28.0
    pause_for_seconds: int = 120
    resume_below_amps: float = 21.0
    resume_for_seconds: int = 120
    normal_ttl_seconds: int = 60
    soft_ttl_seconds: int = 120
    resend_limit_every_seconds: int = 30
    min_seconds_between_actions: int = 5
    max_sensor_age_seconds: int = 120
    active_control: bool = False
    dry_run: bool = True
    enable_dynamic_limit: bool = True
    enable_soft_guard: bool = True
    enable_pause_resume: bool = True
    # Retained for config compatibility; it cannot override physical headroom.
    force_min_when_available_too_low: bool = False
    controlled_phase: str = "L1"
    unused_phase_current: int = 0
    # Explicit opt-in experimental envelope; this is not a fuse thermal model.
    overload_enabled: bool = False
    overload_margin_amps: float = 4.0
    overload_budget_seconds: int = 1800
    overload_transition_limit_amps: float = 38.0
    overload_response_seconds: int = 10
    # Explicit post-response observation tolerance; zero preserves legacy timing.
    grid_confirmation_seconds: int = 0


@dataclass(slots=True)
class RegulatorInput:
    now_ts: float
    status: str
    grid_l1: object
    grid_l2: object = None
    grid_l3: object = None
    charger_current: object = 0
    data_valid: bool = True
    data_error: Optional[str] = None
    # Source clocks: validated grid report time and HA receipt or retained
    # vendor EV event time. Neither guarantees physical acquisition time.
    grid_report_ts: Optional[float] = None
    charger_report_ts: Optional[float] = None
    # UTC epoch clock for explicit report-age checks. now_ts stays monotonic.
    wall_now_ts: Optional[float] = None
    # The effective current/status pair was read in one authenticated cloud GET.
    # Its report timestamp may be a vendor state-event time, never an invented
    # physical acquisition time or a replacement with the local GET receipt.
    charger_snapshot_verified: bool = False
    # False for known invalid/conflicting current provenance. A valid unchanged
    # vendor event may remain a trusted cache state without fresh sample credit.
    charger_current_trusted: bool = True
    # Stable configured source/device binding, never a measured value or clock.
    # The optional identity opts a caller into the source-bound alignment path.
    control_source_id: Optional[str | tuple] = None
    # UTC values from distinct reporting clocks cannot establish replay or
    # response merely by their relative magnitude. Defaults preserve callers
    # that do not yet provide explicit observation-clock provenance.
    charger_report_family: str = "legacy"
    charger_report_provenance: Optional[tuple[str | int, ...]] = None
    charger_increase_ready: bool = True
    # The native SDK may publish HA state before our full raw payload commits.
    # This fence grants no authority, response credit or additional time.
    charger_publication_pending: bool = False
    # A complete selected native tuple, including a numeric-only bootstrap.
    # This capability supplies no EV sample age, response or subtraction.
    charger_native_committed: bool = False
    charger_event_source_ts: Optional[float] = None
    charger_event_source_known: bool = False
    charger_observation_report_times: Optional[tuple[tuple[int, float], ...]] = None
    charger_observation_sequences: Optional[tuple[tuple[int, int], ...]] = None


@dataclass(frozen=True, slots=True)
class RawBudgetRiseContext:
    """Immutable planning facts; a late reply cannot select newer baselines."""

    source_id: str | tuple
    previous_target: tuple[int, int, int, int]
    previous_limit_ts: float
    target: int
    grid_report_ts: float
    event_report_ts: Optional[float]
    response_deadline_ts: float
    response_window_seconds: float


@dataclass(frozen=True, slots=True)
class PeakResponseOwner:
    """A raw high-current event, not a thermal allowance or measurement timer."""

    source_id: str | tuple
    origin: Optional[float]
    deadline: Optional[float]
    grid_source_ts: Optional[float]
    previous_authority: int
    interrupted: bool = False


@dataclass(slots=True)
class MinimumGridConfirmation:
    """One fixed observation phase after a timely, source-proven EV response.

    Callers establish the original response proof. This phase can only retain
    that already accepted minimum offer; it never authorizes a write or a rise.
    All reduction routes use the same deadline, sample-order and value rules.
    The two UTC clocks remain conservative report bounds, not acquisition proof.
    """

    owner: str
    source_id: str | tuple
    cap: int
    deadline: float
    first_ev_ts: float
    floor: float
    value: float
    last_ev_ts: float
    last_grid_ts: float
    high_grid_floor: Optional[float] = None

    @classmethod
    def start(cls, *, owner: str, source_id: str | tuple, cap: int,
              now: float, accepted_at: float, response_at: float,
              response_deadline: Optional[float], seconds: int, lease_expires: float,
              ev_ts: float, grid_ts: float, current: float,
              high_grid_floor: Optional[float] = None):
        values = (now, accepted_at, response_at, lease_expires, ev_ts, grid_ts, current)
        if (any(isinstance(v, bool) or _valid_number(v) is None for v in values)
                or type(seconds) is not int or not 1 <= seconds <= 30
                or not accepted_at < response_at
                or response_deadline is not None and (
                    isinstance(response_deadline, bool) or _valid_number(response_deadline) is None
                    or response_at >= response_deadline)
                or not 1 <= current <= cap + .5):
            return None
        if high_grid_floor is not None and (isinstance(high_grid_floor, bool)
                or _valid_number(high_grid_floor) is None or high_grid_floor > ev_ts):
            return None
        deadline = min(response_at + seconds, lease_expires)
        if now >= deadline:
            return None
        return cls(owner, source_id, cap, deadline, ev_ts, ev_ts,
                   current, ev_ts, grid_ts, high_grid_floor)

    def observe(self, *, now: float, grid: float, current: float,
                grid_ts: float, ev_ts: float, ceiling: float,
                event_advanced: bool, event_regressed: bool,
                explicit_clock: bool, baseline_value_hold: bool = True):
        """Return wait/complete/error; unchanged events cannot postpone proof."""
        if any(isinstance(v, bool) or _valid_number(v) is None for v in
               (now, grid, current, grid_ts, ev_ts, ceiling)):
            return "source_or_control_invalid"
        if now >= self.deadline:
            return "timeout"
        if event_regressed or grid_ts < self.last_grid_ts:
            return "source_clock_regressed"
        high_floor = self.first_ev_ts if self.high_grid_floor is None else self.high_grid_floor
        if grid_ts >= high_floor and grid > ceiling:
            return "outside_ceiling"
        if abs(current - self.value) > .1:
            if not event_advanced:
                return "wait" if explicit_clock and baseline_value_hold else "value_changed_without_event"
            self.floor, self.value = ev_ts, current
        self.last_ev_ts, self.last_grid_ts = ev_ts, grid_ts
        if grid_ts > self.floor:
            if current > grid + 1 or grid - current + self.cap > ceiling:
                return "response_outside_ceiling"
            return "complete"
        return "wait"


@dataclass(slots=True)
class OrdinaryLowerOffer:
    """Accepted lower authority awaiting observations, not a peak countdown."""

    source_id: str | tuple
    cap: int
    previous: int
    grid_floor: float
    ev_floor: Optional[float]
    accepted_at: Optional[float] = None
    first_response: Optional[float] = None
    confirmation_expired: bool = False


@dataclass(slots=True)
class RegulatorDecision:
    mode: str
    reason: str
    command: Optional[str]
    service: Optional[str]
    current_p1: int
    current_p2: int
    current_p3: int
    ttl_seconds: int
    available_l1: int
    house_load_l1: float
    grid_l1: Optional[float]
    charger_current: float
    paused_by_us: bool
    should_call_service: bool
    dry_run: bool
    overload_used_seconds: float = 0.0
    overload_remaining_seconds: float = 0.0
    overload_transition_remaining_seconds: Optional[float] = None
    overload_failure_latched: bool = False
    startup_charge_amps: int = 7
    waiting_for_vehicle_seconds: float = 0.0
    measurement_alignment_remaining_seconds: Optional[float] = None
    current_refresh_required: bool = False
    house_load_basis: str = "grid_minus_reported_ev"
    house_load_anchor_l1: Optional[float] = None
    nominal_rejection_code: Optional[str] = None
    source_report_skew_seconds: Optional[float] = None
    grid_report_age_seconds: Optional[float] = None
    charger_report_age_seconds: Optional[float] = None
    source_alignment_window_seconds: Optional[float] = None
    charger_current_trusted: bool = True
    measurement_uncertainty_active: bool = False
    measurement_uncertainty_cap: Optional[int] = None
    measurement_uncertainty_stops: int = 0
    measurement_recovery_wait_seconds: float = 0.0
    nominal_response_remaining_seconds: Optional[float] = None
    nominal_response_deadline_ts: Optional[float] = None
    recovery_submission_ts: Optional[float] = None
    nominal_sync_deadline_ts: Optional[float] = None
    nominal_sync_remaining_seconds: Optional[float] = None
    overload_lifetime_used_seconds: float = 0.0
    overload_historical_failure_reason: Optional[str] = None
    raw_budget_rise_context: Optional[RawBudgetRiseContext] = None
    raw_budget_response_deadline_ts: Optional[float] = None
    source_processing_deadline_ts: Optional[float] = None
    grid_confirmation_deadline_ts: Optional[float] = None
    grid_confirmation_remaining_seconds: Optional[float] = None


class OneFuseRegulator:
    """Plan commands; acknowledge only a successfully submitted adapter request.

    Acknowledgement means the remote API accepted the request. It does not prove
    that the car physically changed its current or operating state.
    """

    def __init__(self, settings: RegulatorSettings | None = None) -> None:
        self.settings = settings or RegulatorSettings()
        self._ev_clock_watermarks: dict[tuple, float] = {}
        self._ev_clock_key = None
        self._ev_clock_baseline: Optional[float] = None
        self._ev_clock_credit = True
        self._ev_clock_error: Optional[str] = None
        self._ev_clock_explicit = False
        self._ev_owner_clocks: dict[str, dict] = {}
        self._ev_event_source_ts: Optional[float] = None
        self._ev_event_source_known = False
        self._ev_observation_watermarks: dict[tuple, float] = {}
        self._ev_current_observations: dict[tuple, float] = {}
        self._ev_current_winners: tuple[tuple, ...] = ()
        self._ev_observation_sequences: dict[tuple, int] = {}
        self._ev_current_sequences: dict[tuple, int] = {}
        self._ev_frame_wall_ts: Optional[float] = None
        self._ev_frame_grid_ts: Optional[float] = None
        self._ev_established_value: Optional[float] = None
        self._ev_established_binding = None
        self._minimum_confirmation: Optional[MinimumGridConfirmation] = None
        self._peak_response: Optional[PeakResponseOwner] = None
        self._peak_response_failure: Optional[str] = None
        self._peak_response_ready = True
        self._peak_completed_source_id = None
        self._peak_completed_grid_ts: Optional[float] = None
        self._peak_last_grid_value: Optional[float] = None
        self._peak_last_current_value: Optional[float] = None
        self._ordinary_lower: Optional[OrdinaryLowerOffer] = None
        self._automatic_cycle: Optional[dict] = None
        self._automatic_qualification_gt: Optional[float] = None
        self._automatic_qualification_since: Optional[float] = None
        self._automatic_cooling_gt: Optional[float] = None
        self._automatic_cooling_since: Optional[float] = None
        self._automatic_last_gt: Optional[float] = None
        self._automatic_last_now: Optional[float] = None
        self._automatic_observed_source = None
        self.reset_overload_budget()
        self.reset()
        self.update_settings(self.settings)

    def _observe_ev_clock(self, inp: RegulatorInput) -> None:
        """Validate each observation clock without changing its UTC value.

        An explicit domain switch establishes a baseline only. It cannot
        create EV response credit, headroom for a rise or another deadline.
        Keep every domain's high-water mark across switches and routine resets.
        """
        family, provenance = inp.charger_report_family, inp.charger_report_provenance
        self._ev_clock_error = None
        self._ev_frame_wall_ts = None if isinstance(inp.wall_now_ts, bool) else _valid_number(inp.wall_now_ts)
        self._ev_frame_grid_ts = None if isinstance(inp.grid_report_ts, bool) else _valid_number(inp.grid_report_ts)
        self._ev_current_observations = {}
        self._ev_current_winners = ()
        self._ev_current_sequences = {}
        self._ev_event_source_known = inp.charger_event_source_known is True
        self._ev_event_source_ts = (None if isinstance(inp.charger_event_source_ts, bool)
                                   else _valid_number(inp.charger_event_source_ts))
        self._ev_clock_explicit = family != "legacy" or provenance is not None
        if not self._ev_clock_explicit:
            if self._ev_clock_key is not None:
                self._ev_clock_error = "charger_clock_provenance_missing"
                self._ev_clock_credit = False
                return
            self._ev_clock_credit = True
            self._remember_ev_value(inp)
            return
        if (not isinstance(family, str) or family not in {"native_ha_receipt", "native_terminal_receipt", "vendor_event"}
                or not isinstance(provenance, tuple) or not provenance
                or any(not isinstance(part, (str, int)) or isinstance(part, bool)
                       or part == "" for part in provenance)):
            self._ev_clock_error = "charger_clock_provenance_invalid"
            self._ev_clock_credit = False
            return
        wall = None if isinstance(inp.wall_now_ts, bool) else _valid_number(inp.wall_now_ts)
        if wall is None:
            self._ev_clock_error = "charger_clock_timestamp_invalid"
            self._ev_clock_credit = False
            return
        if not inp.data_valid or inp.charger_current_trusted is not True:
            self._ev_clock_credit = False
            return
        vector_advance = self._observe_ev_vector(inp, family, provenance, wall)
        if self._ev_clock_error:
            self._ev_clock_credit = False
            return
        stamp = None if isinstance(inp.charger_report_ts, bool) else _valid_number(inp.charger_report_ts)
        if stamp is None:
            self._ev_clock_credit = False
            return
        if stamp > wall+5:
            self._ev_clock_error = "charger_clock_timestamp_invalid"
            self._ev_clock_credit = False
            return
        key = (family, provenance)
        high = self._ev_clock_watermarks.get(key)
        if high is not None and stamp < high:
            self._ev_clock_error = "charger_clock_regressed"
            self._ev_clock_credit = False
            return
        if high is None and len(self._ev_clock_watermarks) >= 64:
            self._ev_clock_error = "charger_clock_domain_capacity_exhausted"
            self._ev_clock_credit = False
            return
        if self._ev_clock_key is None:
            self._ev_clock_baseline = stamp
            self._ev_clock_credit = False
        elif key != self._ev_clock_key:
            self._ev_clock_baseline = stamp if high is None else high
            self._ev_clock_credit = high is not None and stamp > high
        elif self._ev_clock_baseline is not None and stamp > self._ev_clock_baseline:
            self._ev_clock_credit = True
        if vector_advance or any(self._ev_vector_after_owner(record) for record in self._ev_owner_clocks.values()):
            # The receiver clock already has per-conductor history. A fresh
            # winning conductor may be new to the aggregate MAX provenance,
            # while still advancing its own established clock after the ACK.
            self._ev_clock_credit = True
        self._ev_clock_key = key
        self._ev_clock_watermarks[key] = stamp if high is None else max(high, stamp)
        for record in self._ev_owner_clocks.values():
            record["baselines"].setdefault(key, stamp)
        self._remember_ev_value(inp)

    def _remember_ev_value(self, inp: RegulatorInput) -> None:
        value = None if isinstance(inp.charger_current, bool) else _valid_number(inp.charger_current)
        if (self._ev_clock_credit and self._ev_clock_error is None and inp.data_valid
                and inp.charger_current_trusted is True and value is not None and value >= 0
                and self._event_source_usable()):
            self._ev_established_value = value
            self._ev_established_binding = inp.control_source_id

    def _observe_ev_vector(self, inp: RegulatorInput, family: str, provenance: tuple,
                           wall: float) -> bool:
        vector, sequences = inp.charger_observation_report_times, inp.charger_observation_sequences
        if vector is None and sequences is None:
            return False
        try:
            marker = provenance.index("max_terminals")
            prefix, winners = provenance[:marker], provenance[marker+1:]
            if (vector is not None and not isinstance(vector, tuple) or any(type(item) is not int or item not in {182, 183, 184, 185}
                    for item in winners)):
                raise ValueError("Invalid observation vector")
            values = {}
            for item in vector or ():
                if not isinstance(item, tuple) or len(item) != 2:
                    raise ValueError("Invalid observation clock")
                observation, value = item
                stamp = None if isinstance(value, bool) else _valid_number(value)
                if type(observation) is not int or observation not in {182, 183, 184, 185} or stamp is None or stamp < 0 or stamp > wall+5:
                    raise ValueError("Invalid observation clock")
                key = (family, prefix, observation)
                if key in values:
                    raise ValueError("Duplicate observation clock")
                old = self._ev_observation_watermarks.get(key)
                if old is not None and stamp < old:
                    self._ev_clock_error = "charger_clock_regressed"
                    return False
                values[key] = stamp
            counters = {}
            if sequences is not None:
                if not isinstance(sequences, tuple):
                    raise ValueError("Invalid observation sequences")
                for item in sequences:
                    if (not isinstance(item, tuple) or len(item) != 2 or type(item[0]) is not int
                            or item[0] not in {182, 183, 184, 185} or type(item[1]) is not int or item[1] < 0):
                        raise ValueError("Invalid observation sequence")
                    key = (family, prefix, item[0])
                    if key in counters:
                        raise ValueError("Duplicate observation sequence")
                    if key in self._ev_observation_sequences and item[1] < self._ev_observation_sequences[key]:
                        self._ev_clock_error = "charger_clock_regressed"
                        return False
                    counters[key] = item[1]
            winner_keys = tuple((family, prefix, observation) for observation in winners)
            advanced = bool(winner_keys and all(key in values and key in self._ev_observation_watermarks
                and values[key] > self._ev_observation_watermarks[key] for key in winner_keys))
            if counters:
                advanced = bool(winner_keys and all(key in values and key in counters
                    and key in self._ev_observation_sequences and counters[key] > self._ev_observation_sequences[key]
                    for key in winner_keys))
            if len(set(self._ev_observation_watermarks) | set(values) | set(counters)) > 256:
                raise ValueError("Too many observation clocks")
            self._ev_current_observations = values
            self._ev_current_winners = winner_keys
            self._ev_current_sequences = counters
            self._ev_observation_watermarks.update(values)
            self._ev_observation_sequences.update(counters)
            for record in self._ev_owner_clocks.values():
                for key, stamp in values.items():
                    record["vectors"].setdefault(key, stamp)
                for key, sequence in counters.items():
                    record["sequences"].setdefault(key, sequence)
            return advanced
        except (ValueError, TypeError, AttributeError):
            self._ev_clock_error = "charger_clock_vector_invalid"
            return False

    def _ev_regressed_since(self, previous: Optional[float], current: Optional[float] = None) -> bool:
        if self._ev_clock_explicit:
            return self._ev_clock_error == "charger_clock_regressed"
        stamp = self._last_input_charger_ts if current is None else current
        return bool(previous is not None and stamp is not None and stamp < previous)

    def _pin_ev_owner(self, name: str, *, wall_floor: Optional[float] = None,
                      copy_from: Optional[str] = None) -> None:
        if copy_from is not None and copy_from in self._ev_owner_clocks:
            self._ev_owner_clocks[name] = deepcopy(self._ev_owner_clocks[copy_from])
            return
        baselines = ({self._ev_clock_key: self._last_input_charger_ts}
                     if self._ev_clock_key is not None and self._last_input_charger_ts is not None else {})
        self._ev_owner_clocks[name] = {"baselines": baselines, "wall_floor": wall_floor,
                                       "vectors": dict(self._ev_observation_watermarks),
                                       "sequences": dict(self._ev_observation_sequences)}

    def _set_ev_owner_wall(self, name: str, wall_floor: Optional[float]) -> None:
        if name not in self._ev_owner_clocks:
            self._pin_ev_owner(name, wall_floor=wall_floor)
        else:
            self._ev_owner_clocks[name]["wall_floor"] = wall_floor

    def _ack_wall(self, now: float) -> Optional[float]:
        if (self._last_input_wall_now_ts is not None and self._last_input_now_ts is not None
                and _valid_number(now) is not None and now >= self._last_input_now_ts):
            return self._last_input_wall_now_ts+now-self._last_input_now_ts
        return None

    def _event_source_usable(self, wall_floor: Optional[float] = None) -> bool:
        if not self._ev_event_source_known:
            return True
        event, wall, grid = self._ev_event_source_ts, self._ev_frame_wall_ts, self._ev_frame_grid_ts
        return bool(event is not None and wall is not None and grid is not None
                    and -5 <= wall-event <= self.settings.max_sensor_age_seconds
                    and abs(grid-event) <= self.settings.measurement_settle_seconds
                    and (wall_floor is None or event > wall_floor))

    def _ev_advanced_since(self, previous: Optional[float], owner: Optional[str] = None) -> bool:
        stamp = self._last_input_charger_ts
        if not self._ev_clock_explicit:
            return bool(previous is not None and stamp is not None and stamp > previous)
        record = self._ev_owner_clocks.get(owner)
        if not self._ev_clock_credit or self._ev_clock_error or stamp is None or record is None:
            return False
        baseline = record["baselines"].get(self._ev_clock_key)
        wall_floor = record["wall_floor"]
        if self._ev_current_winners:
            return self._ev_vector_after_owner(record)
        return bool(baseline is not None and stamp > baseline and wall_floor is not None
                    and stamp > wall_floor and self._event_source_usable(wall_floor))

    def _ev_vector_after_owner(self, record: dict) -> bool:
        wall_floor = record["wall_floor"]
        if wall_floor is None or not self._ev_current_winners or not self._event_source_usable(wall_floor):
            return False
        for key in self._ev_current_winners:
            stamp = self._ev_current_observations.get(key)
            if stamp is None or stamp <= wall_floor:
                return False
            if self._ev_current_sequences:
                if (key not in record["sequences"] or key not in self._ev_current_sequences
                        or self._ev_current_sequences[key] <= record["sequences"][key]):
                    return False
            elif key not in record["vectors"] or stamp <= record["vectors"][key]:
                return False
        return True

    def _clock_baseline_raw_hold(self, now: float, grid: float, current: float, status: str):
        """No-write retention under a whole-offer raw bound, without EV credit.

        This does not seed a response, renew authority or modify a pending
        owner's clocks. A terminal failure or expired owner remains terminal.
        """
        s = self.settings
        if (not self._ev_clock_explicit or self._ev_clock_credit or self._ev_clock_error
                or not s.nominal_import_only_fallback or not s.enable_dynamic_limit
                or self._restart_required or self.paused_by_us
                or status not in {"charging", "start_charging", "awaiting_start", "ready_to_charge"}
                or not self._last_input_current_trusted or self.last_target is None
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or self._last_input_control_source_id is None
                or self._last_input_control_source_id != self._accepted_control_source_id
                or self._last_input_wall_now_ts is None or self._last_input_grid_ts is None
                or not 0 <= self._last_input_wall_now_ts-self._last_input_grid_ts <= s.max_sensor_age_seconds
                or self._low_grid_failure is not None or self._normal_shed_failure is not None
                or self._uncertainty_stops > 0 or self._uncertainty_retry_submission_pending
                or self._overload_transition_started is not None or self._overload_hold_active):
            return None
        cap = self.last_target[0]
        whole = max(cap, self._reduction_previous_cap or 0 if self._awaiting_reduction_target is not None else cap,
                    self._low_grid_previous or 0 if self._low_grid_pending else cap,
                    self._uncertainty_previous_offer or 0 if self._uncertainty_ceiling is not None else cap)
        if (not s.min_charge_amps <= cap <= whole <= s.max_charge_amps
                or current > whole+.5 or grid+whole > s.main_fuse_amps-s.buffer_amps
                or self._normal_shed_deadline is not None and now >= self._normal_shed_deadline
                or self._low_grid_pending and self._low_grid_phase != "raw_hold"
                   and self._low_grid_deadline is not None and now >= self._low_grid_deadline
                or self._positive_skew_since is not None
                   and self._positive_alignment_used_seconds+now-self._positive_skew_since
                   >= min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                or self._awaiting_reduction_target is not None
                   and self._reduction_alignment_remaining(now) <= 0):
            return None
        return cap, grid

    def _publication_pending_positive_owner(self, now: float, grid: float, current: float, status: str):
        """Retain only an existing nominal rise owner through raw publication.

        The caller's finite process-coherence fence is not an electrical
        response owner. All earlier accepted authority, spent response time,
        source floors and failures remain exactly as they were.
        """
        s = self.settings
        cap, anchor = self._positive_cap, self._positive_house
        wall, gt = self._last_input_wall_now_ts, self._last_input_grid_ts
        identity = self._last_input_control_source_id
        budget = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
        if self._positive_raw_recovery:
            budget = min(budget, self._positive_raw_window_seconds or 0.)
        spent = self._positive_alignment_used_seconds
        if (not s.enable_dynamic_limit or self._restart_required or self.paused_by_us
                or status not in START_ALLOWED_STATUSES or not self._last_input_current_trusted
                or identity is None or identity != self._accepted_control_source_id
                or cap is None or anchor is None or self.last_target is None
                or self.last_target[0] != cap or not s.min_charge_amps <= cap <= s.max_charge_amps
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or _valid_number(anchor) is None or anchor < 0
                or grid > s.main_fuse_amps-s.buffer_amps or anchor+cap > s.main_fuse_amps-s.buffer_amps
                or current > cap+.5 or wall is None or gt is None
                or not 0 <= wall-gt <= s.max_sensor_age_seconds
                or self._positive_grid_ts is None or gt < self._positive_grid_ts
                or self._awaiting_reduction_target is not None or self._low_grid_pending
                or self._low_grid_failure is not None or self._normal_shed_failure is not None
                or self._normal_shed_deadline is not None
                or self._uncertainty_ceiling is not None or self._uncertainty_retry_submission_pending
                or self._overload_transition_started is not None or self._overload_hold_active
                or not 0 <= spent < budget
                or self._positive_skew_since is not None and spent+max(0., now-self._positive_skew_since) >= budget
                or s.enable_soft_guard and grid > s.soft_limit_amps
                or s.enable_pause_resume and grid > s.pause_limit_amps):
            return None
        return cap, anchor

    def _raw_budget_recovery_candidate(self, now: float, grid: float, current: float,
                                       status: str, inp: RegulatorInput) -> Optional[int]:
        """New nominal authority bounded by raw grid, crediting zero EV amps."""
        s = self.settings
        wall, gt = self._last_input_wall_now_ts, self._last_input_grid_ts
        identity = self._last_input_control_source_id
        if (s.nominal_import_only_fallback is not True or not s.fast_recovery or not s.enable_dynamic_limit
                or inp.charger_native_committed is not True or inp.charger_publication_pending is not False
                or inp.charger_increase_ready is not True or not self._last_input_current_trusted
                or self._ev_clock_error is not None or not self._ev_clock_explicit
                or inp.charger_report_family != "native_terminal_receipt"
                or set(key[2] for key in self._ev_current_sequences) != {182, 183, 184, 185}
                or not self._meaningfully_charging(status, current) or self.paused_by_us
                or identity is None or identity != self._accepted_control_source_id
                or self.last_target is None or not s.min_charge_amps <= self.last_target[0] <= s.max_charge_amps
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or current > self.last_target[0]+.5 or current > grid+1
                or self._restart_required or self._uncertainty_ceiling is not None or self._uncertainty_stops > 0
                or self._uncertainty_retry_submission_pending or self._uncertainty_restart
                or self._positive_cap is not None or self._awaiting_reduction_target is not None
                or (self._accepted_reduction_cap is not None or self._reduction_guard_deadline is not None)
                   and not self._reduction_response_completed
                or self._low_grid_pending or self._low_grid_failure is not None
                or self._normal_shed_deadline is not None or self._normal_shed_failure is not None
                or self._overload_transition_started is not None or self._overload_hold_active
                or self.effective_overload_failure_reason not in {None, "response_timeout", "cumulative_budget_exhausted"}
                or wall is None or gt is None or not 0 <= wall-gt < min(s.measurement_settle_seconds, s.max_sensor_age_seconds)):
            return None
        if self._ev_event_source_known and (self._ev_event_source_ts is None or self._ev_event_source_ts > wall+5):
            return None
        candidate = int(floor(min(s.max_charge_amps, s.main_fuse_amps-s.buffer_amps-grid)))
        return candidate if self.last_target[0] < candidate <= s.max_charge_amps else None

    def _reduction_source_processing_hold(self, now: float, grid: float, current: float,
                                          status: str, inp: RegulatorInput):
        """Preserve only an accepted decrease through one native source cohort.

        A changed nonwinning conductor can expose an old MAX winner before
        the next conductor arrives. It supplies no EV response. The first
        process fence is fixed, shares the reduction's spent budget, and is
        never restarted by channel, heartbeat, GET or same-cap TTL updates.
        """
        if self._reduction_processing_accounted is not None:
            until = min(now, self._reduction_processing_deadline)
            if until > self._reduction_processing_accounted:
                if self._settle_started is None:
                    self._partial_reduction_used_seconds += until-self._reduction_processing_accounted
                self._reduction_processing_accounted = until
        s = self.settings
        def reject():
            self._reduction_processing_accounted = None
            return None
        cap = self.last_target[0] if self.last_target is not None else None
        previous, anchor = self._reduction_previous_cap, self._house_before_reduction
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        identity = self._last_input_control_source_id
        reason = self._numeric_source_rejection()
        if (s.nominal_import_only_fallback is not True or not s.enable_dynamic_limit
                or inp.charger_native_committed is not True or inp.charger_report_family != "native_terminal_receipt"
                or not (inp.charger_increase_ready is False or inp.charger_publication_pending is True)
                or not self._last_input_current_trusted or self._ev_clock_error is not None
                or status != "charging" or self.paused_by_us or self._restart_required
                or self._uncertainty_ceiling is not None or self._uncertainty_stops > 0
                or self._uncertainty_retry_submission_pending or self._uncertainty_restart
                or identity is None or identity != self._accepted_control_source_id
                or cap is None or previous is None or not s.min_charge_amps <= cap < previous <= s.max_charge_amps
                or self._awaiting_reduction_target != cap or self._accepted_reduction_cap != cap
                or not cap+.5 < current <= previous+.5
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or anchor is None or anchor < 0 or anchor+cap > s.main_fuse_amps-s.buffer_amps
                or grid > s.main_fuse_amps-s.buffer_amps or current > grid+1
                or wall is None or gt is None or et is None or et > wall+5
                or not 0 <= wall-gt < min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                or self._reduction_grid_ts is None or gt < self._reduction_grid_ts
                or self._positive_cap is not None or self._normal_shed_deadline is not None
                or self._normal_shed_failure is not None or self._low_grid_pending or self._low_grid_failure is not None
                or self._overload_transition_started is not None or self._overload_hold_active
                or self.effective_overload_failure_reason not in {None, "response_timeout", "cumulative_budget_exhausted"}
                or reason not in {None, "charger_receipt_stale", "report_skew_exceeds_alignment_window"}
                or self._fast_sources_fresh()):
            return reject()
        if self._ev_event_source_known and (self._ev_event_source_ts is None or self._ev_event_source_ts > wall+5):
            return reject()
        remaining = self._reduction_alignment_remaining(now)
        if remaining <= 0:
            return reject()
        if self._reduction_processing_deadline is None:
            self._reduction_processing_deadline = min(now+min(1., s.measurement_settle_seconds),
                now+remaining, self.last_set_limit_ts+self.last_target[3])
            self._reduction_processing_accounted = now
        if now >= self._reduction_processing_deadline:
            return reject()
        if self._reduction_processing_accounted is None:
            self._reduction_processing_accounted = now
        return cap, anchor, self._reduction_processing_deadline

    def _raw_positive_response(self, now: float, grid: float, current: float, status: str):
        """Use the existing positive owner and its original sample-aged clock."""
        if not self._positive_raw_recovery or self._positive_cap is None:
            return None
        s = self.settings
        cap, anchor = self._positive_cap, self._positive_house
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        budget = min(s.measurement_settle_seconds, s.max_sensor_age_seconds, self._positive_raw_window_seconds or 0.)
        remaining = (self._positive_skew_since+budget-self._positive_alignment_used_seconds-now
                     if self._positive_skew_since is not None else 0.)
        if (self._restart_required or self.paused_by_us or status not in START_ALLOWED_STATUSES
                or not self._last_input_current_trusted or self._ev_clock_error is not None
                or self.last_target is None or self.last_target[0] != cap
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or self._last_input_control_source_id is None
                or self._last_input_control_source_id != self._accepted_control_source_id
                or wall is None or gt is None or not 0 <= wall-gt <= s.max_sensor_age_seconds
                or anchor is None or anchor+cap > s.main_fuse_amps-s.buffer_amps
                or current > cap+.5 or self._positive_grid_ts is None or gt < self._positive_grid_ts):
            return "invalid"
        if grid > s.main_fuse_amps-s.buffer_amps:
            return None  # The actual raw-load guards own this observation.
        response = bool(et is not None and self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts")
            and self._fast_sources_fresh() and cap-.5 <= current <= cap+.5
            and gt > et and current <= grid+1 and grid-current+cap <= s.main_fuse_amps-s.buffer_amps)
        if response:
            # An independent raw bound may have retained this same live offer
            # after its unconfirmed-response window expired. Fresh post-ACK EV
            # evidence and a later coherent grid now retire that owner; they
            # do not start another window or revive terminally stopped control.
            self._clear_positive_alignment()
            return None
        if remaining <= 0:
            return None if grid+cap <= s.main_fuse_amps-s.buffer_amps else "timeout"
        return "hold"

    @property
    def _positive_raw_deadline(self) -> Optional[float]:
        if not self._positive_raw_recovery or self._positive_skew_since is None:
            return None
        return (self._positive_skew_since+min(self.settings.measurement_settle_seconds,
            self.settings.max_sensor_age_seconds, self._positive_raw_window_seconds or 0.)-self._positive_alignment_used_seconds)

    def _peak_source_identity(self, value):
        identity = self._trial_identity(value)
        if (identity is None and self._accepted_control_source_id is None
                and not self._ev_clock_explicit):
            return ("legacy_peak_source",)
        return identity

    def _peak_whole_authority(self) -> int:
        values = [self.last_target[0] if self.last_target else 0,
                  self._uncertainty_previous_offer or 0,
                  self._positive_cap or 0]
        accepted = self.last_target[0] if self.last_target else 0
        late_high = self._peak_last_current_value is not None and self._peak_last_current_value > accepted+.5
        if (self._awaiting_reduction_target is not None
                or late_high and self._accepted_reduction_cap == accepted):
            values.append(self._reduction_previous_cap or 0)
        if self._low_grid_pending or late_high and self._low_grid_cap == accepted:
            values.append(self._low_grid_previous or 0)
        if self._peak_response is not None:
            values.append(self._peak_response.previous_authority)
        return int(max(values))

    @property
    def peak_response_active(self) -> bool:
        return self.settings.peak_only_response is True and self._peak_response is not None

    def peak_response_deadline(self, now: Optional[float] = None) -> Optional[float]:
        """Return the original deadline, including expired unresolved events.

        This accessor sends no zero and does not decide whether a qualified
        minimum response has entered the separate grid-confirmation phase.
        """
        return (self._peak_response.deadline
                if self.settings.peak_only_response is True and self._peak_response is not None else None)

    def observe_peak_response(self, inp: RegulatorInput) -> None:
        """Observe only valid raw28–38A events under live positive control.

        Falling below28, duplicate packets and lower-cap handoffs do not refund
        an unresolved event. After genuine completion a new crossing may arm
        another response, without altering any cumulative overload allowance.
        """
        if self.settings.peak_only_response is not True:
            return
        now, wall, grid, gt, current = (None if isinstance(value, bool) else _valid_number(value)
            for value in (inp.now_ts, inp.wall_now_ts, inp.grid_l1,
                          inp.grid_report_ts, inp.charger_current))
        identity = self._peak_source_identity(inp.control_source_id)
        accepted_identity = self._peak_source_identity(self._accepted_control_source_id)
        if (now is None or wall is None or grid is None or gt is None or current is None
                or not inp.data_valid or inp.data_error or inp.charger_current_trusted is not True
                or self._ev_clock_error is not None or identity is None
                or identity != accepted_identity or grid < 0 or current < 0
                or not 0 <= wall-gt <= self.settings.max_sensor_age_seconds):
            return
        self._peak_last_grid_value = grid
        self._peak_last_current_value = current
        if self._peak_response is not None:
            return
        if identity != self._peak_completed_source_id:
            self._peak_response_ready = True
            self._peak_completed_grid_ts = None
        elif self._peak_completed_grid_ts is not None and gt <= self._peak_completed_grid_ts:
            return
        if grid <= PEAK_RESPONSE_TRIGGER_AMPS:
            self._peak_response_ready = True
            return
        if (inp.status.strip().lower() != "charging" or current < 1
                or not self.settings.enable_dynamic_limit
                or self.last_target is None or self.last_target[0] <= 0
                or self.last_set_limit_ts is None
                or not 0 <= now-self.last_set_limit_ts < self.last_target[3]):
            return
        if grid > min(38., self.settings.overload_transition_limit_amps) or not self._peak_response_ready:
            return
        origin = now-(wall-gt)
        self._peak_response = PeakResponseOwner(identity, origin,
            origin+min(10., self.settings.overload_response_seconds), gt,
            self._peak_whole_authority())
        self._peak_response_failure = None
        self._peak_response_ready = False

    def _peak_minimum_proof(self, response_ts: float) -> bool:
        """Retain an already proven minimum through event-driven steady cache.

        This is only response ownership: it supplies no new sample, EV credit
        for an increase, or thermal allowance. A same-cap TTL ACK is irrelevant.
        """
        record = self._ev_owner_clocks.get("_accepted_control_ev_ts", {})
        floor = record.get("wall_floor")
        if floor is None or response_ts <= floor or self._ev_clock_error is not None:
            return False
        if (self._ev_event_source_known and (self._ev_event_source_ts is None
                or self._ev_event_source_ts <= floor
                or self._ev_event_source_ts > self._last_input_wall_now_ts+5)):
            return False
        if not self._ev_clock_explicit:
            return True
        if not self._ev_current_winners:
            baseline = record.get("baselines", {}).get(self._ev_clock_key)
            return baseline is not None and response_ts > baseline
        for key in self._ev_current_winners:
            if self._ev_current_observations.get(key, 0.) <= floor:
                return False
            if self._ev_current_sequences:
                if (key not in record.get("sequences", {}) or
                        self._ev_current_sequences.get(key, -1) <= record["sequences"][key]):
                    return False
            elif (key not in record.get("vectors", {}) or
                    self._ev_current_observations[key] <= record["vectors"][key]):
                return False
        return True

    def complete_peak_response(self, now: float, *, minimum_response_ts: Optional[float] = None,
                               ceiling: Optional[float] = None) -> bool:
        """Close only a proved response or an independent whole-authority bound.

        Closing an event never changes a lease, recovery latch or cumulative
        budget. Call before clearing the shared minimum-confirmation evidence.
        """
        owner = self._peak_response
        if self.settings.peak_only_response is not True or owner is None:
            return False
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        grid = getattr(self, "_peak_last_grid_value", None)
        current = self._peak_last_current_value
        identity = self._peak_source_identity(self._last_input_control_source_id)
        accepted_identity = self._peak_source_identity(self._accepted_control_source_id)
        if (isinstance(now, bool) or _valid_number(now) is None or wall is None or gt is None
                or grid is None or identity != owner.source_id or identity != accepted_identity
                or not self._last_input_current_trusted or self._ev_clock_error is not None
                or not 0 <= wall-gt <= self.settings.max_sensor_age_seconds
                or owner.grid_source_ts is not None and gt < owner.grid_source_ts):
            return False
        whole_raw_safe = grid+self._peak_whole_authority() <= self.settings.main_fuse_amps-self.settings.buffer_amps
        minimum_safe = False
        accepted_minimum = self.last_target[0] if self.last_target is not None else 0
        applicable_ceiling = self.settings.main_fuse_amps-self.settings.buffer_amps
        if (self.settings.overload_enabled and self.effective_overload_failure_reason is None
                and self.effective_overload_used_seconds < self.effective_overload_budget_seconds
                and (self.settings.automatic_overload_cycles or self._trial_epoch is None or self._trial_epoch.get("state") == "active"
                     and self._trial_activation_committed)):
            applicable_ceiling = self.settings.main_fuse_amps+self.settings.overload_margin_amps
        if (not whole_raw_safe and minimum_response_ts is not None
                and not isinstance(minimum_response_ts, bool) and _valid_number(minimum_response_ts) is not None
                and ceiling is not None and not isinstance(ceiling, bool) and _valid_number(ceiling) is not None
                and ceiling <= applicable_ceiling
                and not self._restart_required and not self.paused_by_us
                and self.last_target is not None
                and self.settings.min_charge_amps <= accepted_minimum <= self.startup_current
                and self.last_set_limit_ts is not None and 0 <= now-self.last_set_limit_ts < self.last_target[3]
                and self._last_input_status == "charging" and et is not None
                and current is not None and 1 <= current <= accepted_minimum+.5
                and gt > minimum_response_ts and grid <= ceiling
                and current <= grid+1 and grid-current+accepted_minimum <= ceiling):
            window = self._minimum_confirmation
            retained = bool(window is not None and window.source_id == identity
                and window.cap == accepted_minimum and window.first_ev_ts == minimum_response_ts
                and gt > window.floor and now < window.deadline)
            minimum_safe = retained or (minimum_response_ts == et and self._peak_minimum_proof(et))
        if not whole_raw_safe and not minimum_safe:
            return False
        self._peak_response = None
        self._peak_response_failure = None
        self._peak_completed_source_id = identity
        self._peak_completed_grid_ts = gt
        self._peak_response_ready = grid <= PEAK_RESPONSE_TRIGGER_AMPS
        return True

    def clear_peak_response_session(self) -> bool:
        """The existing observed idle/disconnected policy can retire a session."""
        if self._last_input_status not in IDLE_STATUSES:
            return False
        self._peak_response = None
        self._peak_response_failure = None
        self._peak_response_ready = True
        self._peak_completed_source_id = None
        self._peak_completed_grid_ts = None
        return True

    def _interrupt_peak_response(self, reason: str) -> None:
        owner = self._peak_response
        if owner is None:
            return
        self._peak_response = PeakResponseOwner(owner.source_id, None, None,
            owner.grid_source_ts, owner.previous_authority, True)
        self._peak_response_failure = reason
        self._restart_required = True
        self._uncertainty_ceiling = min(self._uncertainty_ceiling or self.startup_current, self.startup_current)
        self._uncertainty_previous_offer = max(self._uncertainty_previous_offer or 0, owner.previous_authority)
        self._uncertainty_stops = max(1, self._uncertainty_stops)
        self._uncertainty_stop_recorded = self._uncertainty_restart = True

    def _restore_peak_response(self, state: object) -> bool:
        """Pending electrical work restores interrupted, never with a new clock."""
        old = self._peak_response
        flag = state.get("peak_response_pending", False) if isinstance(state, dict) else False
        if flag is False and old is None:
            return True
        valid = type(flag) is bool
        if flag is True:
            identity = self._trial_identity(state.get("peak_response_source_id"))
            previous = state.get("peak_response_previous_authority")
            grid_ts = state.get("peak_response_grid_ts")
            reason = state.get("peak_response_failure")
            valid = bool(valid and identity is not None and type(previous) is int
                and 1 <= previous <= max(16, self.settings.max_charge_amps)
                and (grid_ts is None or not isinstance(grid_ts, bool)
                     and _valid_number(grid_ts) is not None and grid_ts >= 0)
                and (reason is None or isinstance(reason, str)))
        elif old is not None:
            identity, previous, grid_ts, reason = (old.source_id, old.previous_authority,
                old.grid_source_ts, self._peak_response_failure)
        else:
            identity = previous = grid_ts = reason = None
        if not valid:
            identity, previous, grid_ts = "invalid-persisted-peak", self.settings.max_charge_amps, None
            reason = "invalid_persisted_peak_response"
        self._peak_response = PeakResponseOwner(identity, None, None, grid_ts, previous, True)
        self._peak_response_ready = False
        self._interrupt_peak_response(reason or "interrupted_peak_response_after_restore")
        return valid

    def _ensure_automatic_cycle(self) -> dict:
        if self._automatic_cycle is None:
            self._automatic_cycle = dict(version=1, cycle_id=1, state="ready",
                used_seconds=0.0, source_id=None, failure_reason=None)
        return self._automatic_cycle

    def _break_automatic_continuity(self) -> None:
        self._automatic_qualification_gt = self._automatic_cooling_gt = None
        self._automatic_qualification_since = self._automatic_cooling_since = None
        self._automatic_last_gt = self._automatic_last_now = None
        self._automatic_observed_source = None
        if self._automatic_cycle is not None and self._automatic_cycle["state"] == "qualifying":
            self._automatic_cycle["state"] = "ready"

    @property
    def automatic_cycle_requires_checkpoint(self) -> bool:
        return bool(self.settings.automatic_overload_cycles and self._automatic_cycle is not None
                    and self._automatic_cycle["state"] in {"qualifying", "active"})

    def automatic_cycle_diagnostics(self) -> dict:
        cycle = self._automatic_cycle
        used = float(cycle["used_seconds"]) if cycle else 0.0
        gt, now = self._automatic_last_gt, self._automatic_last_now
        return dict(automatic_overload_cycles=self.settings.automatic_overload_cycles,
            automatic_cycle_state=cycle["state"] if cycle else "disabled",
            automatic_cycle_id=cycle["cycle_id"] if cycle else None,
            automatic_cycle_used_seconds=used,
            automatic_cycle_remaining_seconds=max(0., self.settings.overload_budget_seconds-used),
            automatic_cycle_failure_reason=cycle["failure_reason"] if cycle else None,
            automatic_qualification_seconds=max(0., min(gt-self._automatic_qualification_gt,
                now-self._automatic_qualification_since))
                if gt is not None and self._automatic_qualification_gt is not None else 0.,
            automatic_cooling_seconds=max(0., min(gt-self._automatic_cooling_gt,
                now-self._automatic_cooling_since))
                if gt is not None and self._automatic_cooling_gt is not None else 0.,
            automatic_cooling_threshold_amps=AUTOMATIC_COOLING_AMPS)

    def observe_automatic_cycle(self, inp: RegulatorInput, *, source_valid: bool = True) -> None:
        """Sampled operational cycles; never a fuse-temperature estimate.

        Qualification grants only the existing minimum-current envelope. Its
        first thirty seconds consume the same cumulative budget as active use.
        Only advancing same-source samples can prove qualification or cooling.
        """
        if not self.settings.automatic_overload_cycles or not self.settings.overload_enabled:
            self._break_automatic_continuity()
            return
        cycle = self._ensure_automatic_cycle()
        now, wall, gt, grid, current = (None if isinstance(v, bool) else _valid_number(v)
            for v in (inp.now_ts, inp.wall_now_ts, inp.grid_report_ts, inp.grid_l1, inp.charger_current))
        identity = self._trial_identity(inp.control_source_id)
        bound = self._trial_identity(self._accepted_control_source_id)
        cooling_binding = bool(grid is not None and grid < AUTOMATIC_COOLING_AMPS
            and bound is None and identity is not None
            and identity == self._trial_identity(cycle["source_id"]))
        valid = bool(source_valid and now is not None and wall is not None and gt is not None and grid is not None
            and current is not None and grid >= 0 and current >= 0
            and inp.data_valid and not inp.data_error and inp.charger_current_trusted is True
            and inp.status.strip().lower() in ALLOWED_ACTIVE_STATUSES | IDLE_STATUSES
            and self._ev_clock_error is None and identity is not None
            and (identity == bound or cooling_binding)
            and 0 <= wall-gt <= min(AUTOMATIC_MAX_GRID_GAP_SECONDS, self.settings.max_sensor_age_seconds))
        if not valid:
            self._break_automatic_continuity()
            if cycle["state"] in {"qualifying", "active"}:
                cycle["state"], cycle["failure_reason"] = "blocked", "source_control_invalid"
            return
        if cycle["source_id"] is not None and self._trial_identity(cycle["source_id"]) != identity:
            self._break_automatic_continuity()
            cycle["state"], cycle["failure_reason"] = "blocked", "source_binding_changed"
            cycle["source_id"] = identity
            return
        previous_gt = self._automatic_last_gt
        if self._automatic_observed_source != identity:
            self._break_automatic_continuity()
            previous_gt = None
        if previous_gt is not None and gt < previous_gt:
            self._break_automatic_continuity()
            cycle["state"], cycle["failure_reason"] = "blocked", "grid_clock_regressed"
            return
        if previous_gt is not None and gt == previous_gt:
            return
        if previous_gt is not None and gt-previous_gt > min(
                AUTOMATIC_MAX_GRID_GAP_SECONDS, self.settings.max_sensor_age_seconds):
            self._break_automatic_continuity()
        self._automatic_last_gt, self._automatic_last_now, self._automatic_observed_source = gt, now, identity
        if cycle["source_id"] is None:
            cycle["source_id"] = identity
        if grid < AUTOMATIC_COOLING_AMPS:
            self._automatic_qualification_gt = None
            self._automatic_qualification_since = None
            if self._automatic_cooling_gt is None:
                self._automatic_cooling_gt = gt
                self._automatic_cooling_since = now
            if (gt-self._automatic_cooling_gt >= AUTOMATIC_COOLING_SECONDS
                    and now-self._automatic_cooling_since >= AUTOMATIC_COOLING_SECONDS):
                # A full observed cooling run replenishes ONLY this software
                # cycle. Electrical owners, recovery and lifetime history stay.
                if cycle["used_seconds"] > 0 or cycle["state"] != "ready" or cycle["failure_reason"]:
                    cycle.update(cycle_id=cycle["cycle_id"]+1, state="ready",
                        used_seconds=0.0, failure_reason=None)
                self._automatic_cooling_gt = gt
                self._automatic_cooling_since = now
            elif cycle["state"] == "qualifying":
                cycle["state"] = "ready"
            return
        self._automatic_cooling_gt = self._automatic_cooling_since = None
        if cycle["used_seconds"] >= self.settings.overload_budget_seconds:
            cycle["state"] = "exhausted"
            self._automatic_qualification_gt = None
            self._automatic_qualification_since = None
            return
        if grid <= self.settings.main_fuse_amps:
            self._automatic_qualification_gt = None
            self._automatic_qualification_since = None
            if cycle["state"] == "qualifying":
                cycle["state"] = "ready"
            return
        if cycle["failure_reason"] is not None or cycle["state"] == "active":
            return
        live = bool(inp.status.strip().lower() == "charging" and current >= 1
            and self.settings.active_control and not self.settings.dry_run
            and self.settings.enable_dynamic_limit and not self._restart_required and not self.paused_by_us
            and not self._uncertainty_retry_submission_pending and self._uncertainty_stops == 0
            and inp.charger_native_committed is True and not inp.charger_publication_pending
            and self.last_target is not None and self.last_target[0] > 0
            and self.last_set_limit_ts is not None and 0 <= now-self.last_set_limit_ts < self.last_target[3]
            and (self._peak_response is None or not self._peak_response.interrupted))
        if not live:
            self._automatic_qualification_gt = None
            self._automatic_qualification_since = None
            if cycle["state"] == "qualifying":
                cycle["state"] = "ready"
            return
        if self._automatic_qualification_gt is None:
            self._automatic_qualification_gt = gt
            self._automatic_qualification_since = now
            cycle["state"] = "qualifying"
        if (gt-self._automatic_qualification_gt >= AUTOMATIC_QUALIFY_SECONDS
                and now-self._automatic_qualification_since >= AUTOMATIC_QUALIFY_SECONDS):
            cycle["state"] = "active"

    def _restore_automatic_cycle(self, state: object, gap: float, exposure: bool) -> bool:
        value = state.get("automatic_cycle") if isinstance(state, dict) else None
        previous = self._automatic_cycle
        self._break_automatic_continuity()
        if value is None:
            if self.settings.automatic_overload_cycles:
                self._ensure_automatic_cycle()
            return True
        valid = bool(isinstance(value, dict) and value.get("version") == 1
            and type(value.get("cycle_id")) is int and value["cycle_id"] >= 1
            and value.get("state") in {"ready", "qualifying", "active", "exhausted", "blocked"}
            and not isinstance(value.get("used_seconds"), bool)
            and _valid_number(value.get("used_seconds")) is not None and value["used_seconds"] >= 0
            and (value.get("source_id") is None or self._trial_identity(value["source_id"]) is not None)
            and (value.get("failure_reason") is None or isinstance(value["failure_reason"], str)))
        if not valid:
            self._automatic_cycle = dict(version=1, cycle_id=1, state="blocked",
                used_seconds=float(self.settings.overload_budget_seconds), source_id=None,
                failure_reason="invalid_persisted_automatic_cycle")
            return False
        restored = deepcopy(value)
        restored["used_seconds"] = float(value["used_seconds"])+(gap if exposure else 0.)
        if previous is not None:
            restored["used_seconds"] = max(restored["used_seconds"], previous["used_seconds"])
        if restored["state"] in {"qualifying", "active"}:
            restored["state"] = "blocked"
            restored["failure_reason"] = "interrupted_automatic_cycle"
        self._automatic_cycle = restored
        return True

    @staticmethod
    def _trial_identity(value: object):
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (tuple, list)) and value and all(
                isinstance(part, (str, int)) and not isinstance(part, bool) and part != "" for part in value):
            return tuple(value)
        return None

    @staticmethod
    def _trial_started_timestamp(value: object) -> float:
        if not isinstance(value, str):
            raise ValueError("Trial approval UTC is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError("Trial approval must use UTC")
            stamp = parsed.timestamp()
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("Trial approval UTC is invalid") from err
        if not isfinite(stamp) or stamp < 0:
            raise ValueError("Trial approval UTC is invalid")
        return stamp

    @staticmethod
    def _trial_policy(settings: RegulatorSettings) -> dict:
        snapshot = asdict(settings)
        defaults = asdict(RegulatorSettings())
        if any((type(v) is not bool if type(defaults[key]) is bool else
                not isinstance(v, str) if isinstance(defaults[key], str) else
                isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v))
               for key, v in snapshot.items()):
            raise ValueError("Trial settings must be finite")
        if (settings.main_fuse_amps != 25 or not 6 <= settings.min_charge_amps <= 7 <= settings.max_charge_amps
                or not settings.active_control or settings.dry_run or not settings.overload_enabled
                or not settings.enable_dynamic_limit
                or not 0 <= settings.buffer_amps < 25 or settings.minimum_reserve_amps < 0
                or settings.max_sensor_age_seconds <= 0 or settings.measurement_settle_seconds <= 0
                or settings.normal_ttl_seconds <= 0
                or not 0.5 <= settings.overload_margin_amps <= 4
                or not 60 <= settings.overload_budget_seconds <= 1800
                or not 25 + settings.overload_margin_amps <= settings.overload_transition_limit_amps <= 38
                or not 1 <= settings.overload_response_seconds <= 10
                or type(settings.grid_confirmation_seconds) is not int
                or not 0 <= settings.grid_confirmation_seconds <= 30):
            raise ValueError("Trial requires the approved bounded 25 A / 7 A profile")
        return dict(main_fuse_amps=25.0, minimum_current=7,
                    ceiling_amps=25.0 + settings.overload_margin_amps,
                    budget_seconds=settings.overload_budget_seconds,
                    transition_limit_amps=settings.overload_transition_limit_amps,
                    response_seconds=settings.overload_response_seconds, settings=snapshot)

    @property
    def effective_overload_failure_reason(self) -> Optional[str]:
        if self.settings.automatic_overload_cycles:
            if not self.settings.overload_enabled:
                return "automatic_cycle_disabled"
            cycle = self._ensure_automatic_cycle()
            if cycle["failure_reason"]:
                return cycle["failure_reason"]
            if cycle["used_seconds"] >= self.settings.overload_budget_seconds:
                return "cumulative_budget_exhausted"
            return None if cycle["state"] in {"qualifying", "active"} else "automatic_cycle_not_admitted"
        epoch = self._trial_epoch
        if epoch is None:
            return self._overload_failure_reason
        if epoch.get("failure_reason"):
            return epoch["failure_reason"]
        if epoch.get("state") == "active" and self._trial_activation_committed:
            return None
        return "trial_activation_pending" if epoch.get("state") in {"pending", "active"} else "trial_not_active"

    @property
    def nominal_after_overload_allowed(self) -> bool:
        # Withdrawing automatic29A permission is not itself a telemetry fault
        # and cannot prohibit a separately proved ordinary buffered decrease.
        return bool(self.settings.automatic_overload_cycles or
            self.effective_overload_failure_reason in {None, "response_timeout", "cumulative_budget_exhausted"})

    @property
    def effective_overload_used_seconds(self) -> float:
        if self.settings.automatic_overload_cycles:
            return float(self._ensure_automatic_cycle()["used_seconds"])
        epoch = self._trial_epoch
        if epoch is None:
            return self._overload_used_seconds
        closed = epoch.get("closed_used_seconds")
        return float(closed if closed is not None else
                     max(0.0, self._overload_used_seconds - epoch["baseline_used_seconds"]))

    @property
    def effective_overload_budget_seconds(self) -> float:
        if self.settings.automatic_overload_cycles:
            return self.settings.overload_budget_seconds
        return (self._trial_epoch["policy"]["budget_seconds"] if self._trial_epoch is not None
                else self.settings.overload_budget_seconds)

    @property
    def overload_minimum_current(self) -> int:
        explicit = (self._trial_epoch is not None and self._trial_epoch.get("state") == "active"
                    and self._trial_activation_committed)
        if self.settings.peak_only_response or explicit:
            minimum = self.startup_current if self.settings.peak_only_response else 7
            if self.last_target is not None and self.last_target[0] > 0:
                if self.last_target[0] < self.settings.min_charge_amps:
                    return 0  # Bounds changed: never raise an invalid old cap during a peak.
                minimum = min(minimum, self.last_target[0])
            return minimum
        return self.settings.min_charge_amps

    def trial_diagnostics(self) -> dict:
        epoch = self._trial_epoch
        return dict(
            overload_trial_id=epoch["trial_id"] if epoch else None,
            overload_trial_state=epoch["state"] if epoch else None,
            overload_trial_started_at_utc=epoch["started_at_utc"] if epoch else None,
            overload_trial_failure_reason=epoch.get("failure_reason") if epoch else None,
            overload_trial_baseline_used_seconds=epoch["baseline_used_seconds"] if epoch else None,
            overload_trial_used_seconds=self.effective_overload_used_seconds if epoch else None,
            overload_trial_remaining_seconds=max(0.0, self.effective_overload_budget_seconds
                                                - self.effective_overload_used_seconds) if epoch else None,
            overload_trial_activation_committed=self._trial_activation_committed,
            overload_trial_policy=deepcopy(epoch["policy"]) if epoch else None,
            overload_lifetime_used_seconds=self._overload_used_seconds,
            overload_historical_failure_reason=self._overload_failure_reason,
        )

    def _terminate_trial(self, state: str, reason: str) -> None:
        epoch = self._trial_epoch
        if epoch is None or epoch["state"] not in {"pending", "active"}:
            return
        epoch["closed_used_seconds"] = max(0.0, self._overload_used_seconds - epoch["baseline_used_seconds"])
        epoch["state"] = state
        epoch["failure_reason"] = epoch.get("failure_reason") or reason
        self._trial_activation_committed = False

    def _validate_trial_input(self, inp: RegulatorInput) -> tuple[float, float, object]:
        self._observe_ev_clock(inp)
        policy = self._trial_policy(self.settings)
        now, wall, grid, current, gt, et = (None if isinstance(value, bool) else _valid_number(value)
            for value in (inp.now_ts, inp.wall_now_ts, inp.grid_l1, inp.charger_current,
                          inp.grid_report_ts, inp.charger_report_ts))
        identity = self._trial_identity(inp.control_source_id)
        if (now is None or wall is None or grid is None or current is None or gt is None or et is None
                or not inp.data_valid or inp.data_error or inp.charger_current_trusted is not True
                or self._ev_clock_error is not None or not self._ev_clock_credit or not self._event_source_usable()
                or inp.status.strip().lower() != "charging" or current < 1 or grid < 0
                or identity is None or identity != self._accepted_control_source_id
                or self.last_target is None or not 0 < self.last_target[0] <= self.settings.max_charge_amps
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or current > self.last_target[0]+.5 or current > grid+1
                or grid > min(23.0, self.settings.main_fuse_amps-self.settings.buffer_amps)
                or not 0 <= wall-gt <= self.settings.max_sensor_age_seconds
                or not 0 <= wall-et <= self.settings.max_sensor_age_seconds
                or not abs(gt-et) <= self.settings.measurement_settle_seconds
                or self._restart_required or self.paused_by_us or self._awaiting_reduction_target is not None
                or self._positive_cap is not None or self._low_grid_pending
                or self._normal_shed_deadline is not None or self._normal_shed_failure is not None
                or self._uncertainty_ceiling is not None or self._uncertainty_stops > 0
                or self._uncertainty_retry_submission_pending
                or self._overload_transition_started is not None or self._overload_hold_active
                or self._overload_failure_reason not in {None, "response_timeout", "cumulative_budget_exhausted"}
                or self._trial_epoch is not None and self._trial_epoch.get("failure_reason") == "invalid_persisted_trial"
                or self._overload_last_eval_ts is not None and now < self._overload_last_eval_ts):
            raise ValueError("Trial start gates are not satisfied")
        if self._trial_epoch is not None and self._trial_epoch["state"] in {"pending", "active"}:
            if (self._trial_identity(self._trial_epoch["source_id"]) != identity
                    or self._trial_epoch["policy"] != policy
                    or self._trial_last_grid_ts is not None and gt < self._trial_last_grid_ts
                    or self._ev_regressed_since(self._trial_last_ev_ts, et)):
                raise ValueError("Trial source or frozen policy changed")
        if not self._observe_overload_time(now, grid, True):
            raise ValueError("Trial accounting clock is invalid")
        self._trial_last_grid_ts, self._trial_last_ev_ts = gt, et
        return now, wall, identity

    def prepare_overload_trial(self, inp: RegulatorInput, *, trial_id: str, started_at_utc: str) -> None:
        if self.settings.automatic_overload_cycles:
            raise ValueError("Manual trial activation is unavailable under automatic cycles")
        if (not isinstance(trial_id, str) or not trial_id.strip() or len(trial_id) > 128
                or self._trial_epoch is not None and (
                    self._trial_epoch["state"] in {"pending", "active"} or self._trial_epoch["trial_id"] == trial_id)):
            raise ValueError("Trial already exists or its ID is invalid")
        started = self._trial_started_timestamp(started_at_utc)
        wall = None if isinstance(inp.wall_now_ts, bool) else _valid_number(inp.wall_now_ts)
        if wall is None or started > wall + 2:
            raise ValueError("Trial approval UTC is in the future")
        _, _, identity = self._validate_trial_input(inp)
        self._trial_epoch = dict(version=1, trial_id=trial_id, started_at_utc=started_at_utc,
            source_id=list(identity) if isinstance(identity, tuple) else identity,
            policy=self._trial_policy(self.settings), baseline_used_seconds=self._overload_used_seconds,
            state="pending", failure_reason=None, closed_used_seconds=None)
        self._trial_activation_committed = False

    def stage_overload_trial_activation(self, trial_id: str, inp: RegulatorInput) -> None:
        if self._trial_epoch is None or self._trial_epoch["trial_id"] != trial_id or self._trial_epoch["state"] != "pending":
            raise ValueError("No matching pending trial")
        self._validate_trial_input(inp)
        self._trial_epoch["state"] = "active"
        self._trial_activation_committed = False

    def commit_overload_trial_activation(self, trial_id: str, inp: RegulatorInput) -> None:
        if (self._trial_epoch is None or self._trial_epoch["trial_id"] != trial_id
                or self._trial_epoch["state"] != "active" or self._trial_epoch["failure_reason"] is not None):
            raise ValueError("No matching staged trial")
        self._validate_trial_input(inp)
        self._trial_activation_committed = True

    def reject_overload_trial_activation(self, trial_id: str, reason: str) -> None:
        if self._trial_epoch is None or self._trial_epoch["trial_id"] != trial_id:
            raise ValueError("No matching trial to reject")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Trial rejection reason is missing")
        self._terminate_trial("failed", reason)

    def end_overload_trial(self, inp: RegulatorInput) -> None:
        if self._trial_epoch is None:
            return
        now = None if isinstance(inp.now_ts, bool) else _valid_number(inp.now_ts)
        grid = None if isinstance(inp.grid_l1, bool) else _valid_number(inp.grid_l1)
        valid = bool(inp.data_valid and not inp.data_error and grid is not None and grid >= 0)
        if not self._observe_overload_time(now, grid, valid):
            self._terminate_trial("interrupted", "invalid_end_clock")
            return
        self._terminate_trial("ended", "ended_by_user")

    def _trial_continuity_error(self, inp: RegulatorInput) -> Optional[str]:
        epoch = self._trial_epoch
        if epoch is None or epoch["state"] not in {"pending", "active"}:
            return None
        if epoch["policy"]["settings"] != asdict(self.settings):
            self._terminate_trial("interrupted", "settings_changed")
            return "Explicit trial policy changed; no trial authority remains."
        now, wall, grid, current, gt, et = (None if isinstance(value, bool) else _valid_number(value)
            for value in (inp.now_ts, inp.wall_now_ts, inp.grid_l1, inp.charger_current,
                          inp.grid_report_ts, inp.charger_report_ts))
        identity = self._trial_identity(inp.control_source_id)
        if (now is None or wall is None or grid is None or current is None or gt is None or et is None
                or not inp.data_valid or inp.data_error or inp.charger_current_trusted is not True
                or self._ev_clock_error is not None
                or inp.status.strip().lower() != "charging" or grid < 0 or current < 0
                or current > self.settings.max_charge_amps + .5
                or identity != self._trial_identity(epoch["source_id"])
                or identity != self._accepted_control_source_id
                or self._restart_required or self.paused_by_us
                or self._uncertainty_ceiling is not None or self._uncertainty_stops > 0
                or self._uncertainty_retry_submission_pending
                or self.last_target is None or self.last_target[0] <= 0
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or not 0 <= wall-gt <= self.settings.max_sensor_age_seconds or et > wall
                or self._trial_last_grid_ts is not None and gt < self._trial_last_grid_ts
                or self._ev_regressed_since(self._trial_last_ev_ts)):
            self._terminate_trial("interrupted", "source_control_or_session_invalid")
            return "Explicit trial source, live control lease or charging session is invalid."
        # An unchanged trusted EVENT need not change its timestamp while a
        # stable minimum is held. It grants no new response time or EV proof.
        self._trial_last_grid_ts, self._trial_last_ev_ts = gt, et
        return None

    def update_settings(self, settings: RegulatorSettings) -> None:
        previous_settings = self.settings
        if settings.controlled_phase != "L1" or settings.unused_phase_current != 0:
            raise ValueError("Only L1 with unused phase currents of 0 A is supported")
        if type(settings.startup_charge_amps) is not int or not 6 <= settings.startup_charge_amps <= 16:
            raise ValueError("Startup current must be an integer from 6 to 16 A")
        if type(settings.fast_recovery) is not bool:
            raise ValueError("Fast recovery must be a boolean")
        if type(settings.nominal_import_only_fallback) is not bool:
            raise ValueError("Nominal import-only fallback must be a boolean")
        if type(settings.bounded_nominal_shedding) is not bool:
            raise ValueError("Bounded nominal shedding must be a boolean")
        if type(settings.peak_only_response) is not bool:
            raise ValueError("Peak-only electrical response policy must be a boolean")
        if type(settings.automatic_overload_cycles) is not bool:
            raise ValueError("Automatic overload cycles must be a boolean")
        if settings.automatic_overload_cycles and (not settings.peak_only_response
                or settings.main_fuse_amps != 25 or settings.startup_charge_amps != 7):
            raise ValueError("Automatic cycles require the peak-only 25 A / 7 A overload policy")
        if type(settings.grid_confirmation_seconds) is not int or not 0 <= settings.grid_confirmation_seconds <= 30:
            raise ValueError("Post-response grid confirmation must be an integer from 0 to 30 seconds")
        overload_values = (
            settings.overload_margin_amps, settings.overload_budget_seconds,
            settings.overload_transition_limit_amps, settings.overload_response_seconds,
        )
        if any(isinstance(v, bool) or _valid_number(v) is None for v in overload_values):
            raise ValueError("Overload settings must be finite numbers")
        if (not 0.5 <= settings.overload_margin_amps <= 4
                or not 60 <= settings.overload_budget_seconds <= 1800
                or not 1 <= settings.overload_response_seconds <= 10
                or not 10 <= settings.overload_transition_limit_amps <= 38):
            raise ValueError("Overload settings are outside the experimental bounds")
        if settings.overload_enabled and (
                settings.main_fuse_amps + settings.overload_margin_amps > settings.overload_transition_limit_amps
                or settings.min_charge_amps >= settings.main_fuse_amps or not settings.enable_dynamic_limit):
            raise ValueError("Enabled overload requires dynamic limiting and a separate transition ceiling")
        if (self._trial_epoch is not None and self._trial_epoch["state"] in {"pending", "active"}
                and self._trial_epoch["policy"]["settings"] != asdict(settings)):
            self._terminate_trial("interrupted", "settings_changed")
        self.settings = settings
        if settings.automatic_overload_cycles:
            self._ensure_automatic_cycle()
        if previous_settings != settings:
            self._break_automatic_continuity()
            if self._automatic_cycle is not None and self._automatic_cycle["state"] == "active":
                self._automatic_cycle["state"] = "ready"
        self.reset_timers()
        if self._ordinary_lower is not None:
            self._ordinary_lower = None
            self._clear_minimum_confirmation("ordinary")
            self._restart_required = True
        if getattr(self, "_low_grid_pending", False):
            self._low_grid_failure = "settings_changed_during_alignment"
            self._restart_required = True
        if getattr(self, "_normal_shed_sync_deadline", None) is not None:
            self._normal_shed_failure = "settings_changed_during_sync"
            self._restart_required = True
        if getattr(self, "_overload_grid_confirmation_deadline", None) is not None:
            self._fail_overload("settings_changed_during_grid_confirmation")
            self._clear_overload_transition()
            self._restart_required = True
        # An options change cannot extend an alignment deadline using old bounds.
        self._clear_positive_alignment()
        if previous_settings != settings:
            self._interrupt_peak_response("settings_changed_during_peak_response")

    def reset_timers(self) -> None:
        self.above_soft_since: Optional[float] = None
        self.above_pause_since: Optional[float] = None
        self.below_resume_since: Optional[float] = None
        self._increase_since: Optional[float] = None
        self._restart_ready_since: Optional[float] = None
        self._raw_zero_restart_cap: Optional[int] = None
        self._previous_desired: Optional[int] = None

    def reset(self) -> None:
        # Routine controller resets must not grant another overload budget.
        self._break_automatic_continuity()
        self._terminate_trial("interrupted", "controller_reset")
        self._ordinary_lower = None
        self._clear_minimum_confirmation("ordinary")
        preserve_normal = None
        if (getattr(self, "_low_grid_pending", False)
                or getattr(self, "_normal_shed_deadline", None) is not None
                or getattr(self, "_uncertainty_stops", 0) > 0) or (
                getattr(self, "_uncertainty_ceiling", None) is not None
                and getattr(self, "settings", None) is not None and self.settings.bounded_nominal_shedding):
            preserve_normal = {key: value for key, value in vars(self).items()
                               if key.startswith("_normal_shed_") or key.startswith("_uncertainty_")
                               or key.startswith("_low_grid_")
                               or key == "_restored_normal_wait"}
        if getattr(self, "_overload_transition_started", None) is not None:
            self._fail_overload("interrupted_transition")
        self._clear_overload_transition()
        self.reset_timers()
        self.paused_by_us = False
        self._pause_observed = False
        self._pause_submission_ts: Optional[float] = None
        self._resume_submission_ts: Optional[float] = None
        self.last_action_ts: Optional[float] = None
        self.last_set_limit_ts: Optional[float] = None
        self.last_target: Optional[tuple[int, int, int, int]] = None
        self.last_command: Optional[str] = None
        self._last_increase_ts: Optional[float] = None
        self._last_limit_change_ts: Optional[float] = None
        self._restart_required = False
        self._awaiting_reduction_target: Optional[int] = None
        self._house_before_reduction: Optional[float] = None
        self._last_input_grid_ts: Optional[float] = None
        self._last_input_charger_ts: Optional[float] = None
        self._last_input_wall_now_ts: Optional[float] = None
        self._last_input_current_trusted = True
        self._last_input_control_source_id = None
        self._accepted_control_source_id = None
        self._accepted_control_ev_ts: Optional[float] = None
        self._last_input_now_ts: Optional[float] = None
        self._fast_recovery_wait: Optional[str] = None
        self._snapshot_aligned = False
        self._reduction_grid_ts: Optional[float] = None
        self._reduction_charger_ts: Optional[float] = None
        self._settle_started: Optional[float] = None
        self._clear_partial_reduction_alignment()
        self._waiting_for_vehicle_since: Optional[float] = None
        self._last_input_status: Optional[str] = None
        self._nominal_shedding_started: Optional[float] = None
        self._nominal_shedding_budget: Optional[float] = None
        self._nominal_overload_blocked = False
        self._nominal_rejection_code: Optional[str] = None
        self._clear_measurement_uncertainty()
        self._clear_bounded_nominal_shedding()
        self._clear_low_grid_alignment()
        self._clear_positive_alignment()
        if preserve_normal:
            for key, value in preserve_normal.items():
                setattr(self, key, value)
            # A routine reset cannot recreate positive ownership or a new
            # response attempt from an already running monotonic deadline.
            if self._normal_shed_deadline is not None:
                self._normal_shed_failure = "controller_reset_during_response"
            if self._low_grid_pending:
                self._low_grid_failure = "controller_reset_during_alignment"
            self._restart_required = True
        self._interrupt_peak_response("controller_reset_during_peak_response")

    def _clear_minimum_confirmation(self, owner: str) -> None:
        window = self._minimum_confirmation
        if window is not None and window.owner == owner:
            self._minimum_confirmation = None

    def _ordinary_lower_offer_plan(self, inp: RegulatorInput, now: float,
                                    grid: float, current: float, status: str):
        """A raw-bounded decrease is observation work, never a peak timeout.

        Retain the previous higher authority for proof/increase inhibition.
        The entire proposed minimum fits beside measured import with zero EV
        subtraction. Missing response events cannot invent an electrical peak.
        """
        s = self.settings
        ctx = self._ordinary_lower
        identity = self._last_input_control_source_id
        accepted = self.last_target[0] if self.last_target else None
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        if grid > PEAK_RESPONSE_TRIGGER_AMPS:
            # The actual peak owner now governs. Carry all previous authority
            # through the existing uncertainty/reduction records, not a second
            # ordinary clock or competing confirmation owner.
            self._clear_minimum_confirmation("ordinary")
            self._ordinary_lower = None
            return None
        if (not s.peak_only_response or not s.nominal_import_only_fallback or not s.bounded_nominal_shedding
                or not s.enable_dynamic_limit or identity is None
                or identity != self._accepted_control_source_id
                or accepted is None or accepted < s.min_charge_amps
                or self.last_set_limit_ts is None
                or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or self._restart_required or self.paused_by_us
                or self._uncertainty_retry_submission_pending
                or self._low_grid_failure is not None or self._normal_shed_failure is not None
                or status not in START_ALLOWED_STATUSES or not self._last_input_current_trusted
                or self._ev_clock_error is not None or inp.charger_publication_pending
                or wall is None or gt is None or not 0 <= wall-gt <= s.max_sensor_age_seconds
                or current > s.max_charge_amps+.5):
            return None
        cap = min(accepted, self.startup_current, self._uncertainty_ceiling
                  if self._uncertainty_ceiling is not None else accepted)
        if not s.min_charge_amps <= cap <= accepted <= s.max_charge_amps:
            return None
        pending = bool(ctx is not None or self._low_grid_pending
            or self._normal_shed_deadline is not None or self._awaiting_reduction_target is not None
            or current > grid+1 or current > accepted+.5
            or self._positive_raw_recovery and self._positive_raw_deadline is not None
               and now >= self._positive_raw_deadline)
        if not pending:
            return None
        raw_safe = grid+cap <= s.main_fuse_amps-s.buffer_amps
        if ctx is None:
            if not raw_safe or not inp.charger_native_committed:
                return None
            previous = max(accepted, self._reduction_previous_cap or 0,
                self._uncertainty_previous_offer or 0, self._low_grid_previous or 0,
                self._normal_shed_previous or 0)
            ctx = self._ordinary_lower = OrdinaryLowerOffer(identity, cap, previous, gt, et)
            if accepted == cap:
                ctx.accepted_at = (self._last_limit_change_ts if self._last_limit_change_ts is not None
                                   else self.last_set_limit_ts)
                ctx.ev_floor = self._accepted_control_ev_ts
                self._pin_ev_owner("_ordinary_ev_floor", copy_from="_accepted_control_ev_ts")
            else:
                self._pin_ev_owner("_ordinary_ev_floor")
            self._clear_positive_alignment()
            self._clear_low_grid_alignment()
            self._clear_bounded_nominal_shedding()
            self._uncertainty_ceiling = min(self._uncertainty_ceiling or cap, cap)
            self._uncertainty_previous_offer = max(self._uncertainty_previous_offer or 0, previous)
        if identity != ctx.source_id or cap != ctx.cap:
            return None
        pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
        soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
        if (s.enable_pause_resume and pause
                or s.enable_soft_guard and soft and s.soft_current_amps < cap):
            return None
        response = bool(accepted == cap and ctx.accepted_at is not None and et is not None
            and wall-(now-ctx.accepted_at) < et
            and not inp.charger_snapshot_verified
            and self._ev_advanced_since(ctx.ev_floor, "_ordinary_ev_floor")
            and 1 <= current <= cap+.5)
        if response and ctx.first_response is None:
            ctx.first_response = et
        if not ctx.confirmation_expired:
            sync = self._minimum_confirmation_plan(owner="ordinary", now=now, grid=grid,
                current=current, cap=cap, source_id=ctx.source_id, accepted_at=ctx.accepted_at,
                response_deadline=None, response_at=(now-(wall-ctx.first_response)
                    if ctx.first_response is not None else None),
                response_valid=response, response_owner="_ordinary_ev_floor",
                ceiling=s.main_fuse_amps-s.buffer_amps)
            if sync is not None:
                outcome, basis = sync
                if outcome == "wait":
                    return ("ordinary_grid_wait", "The accepted minimum responded; awaiting a later grid in the fixed observation phase. No electrical peak countdown, write or lease renewal.", None, cap, grid)
                if outcome == "complete":
                    self.complete_peak_response(now, minimum_response_ts=ctx.first_response,
                                                ceiling=s.main_fuse_amps-s.buffer_amps)
                    self._clear_minimum_confirmation("ordinary")
                    self._ordinary_lower = None
                    self._awaiting_reduction_target = self._house_before_reduction = self._settle_started = None
                    self._clear_partial_reduction_alignment()
                    if self._uncertainty_stops == 0:
                        self._uncertainty_ceiling = self._uncertainty_previous_offer = None
                    return ("ordinary_lower_complete", "A genuine lower-current event and later grid retire the previous higher offer.", "set_limit", cap, basis)
                if outcome not in {"timeout", "response_outside_ceiling", "value_changed_without_event"} or not raw_safe:
                    self._restart_required = True
                    self._clear_minimum_confirmation("ordinary")
                    self._ordinary_lower = None
                    return ("ordinary_observation_stop", "Minimum observation failed: "+outcome,
                            "set_limit", 0, grid)
                self._clear_minimum_confirmation("ordinary")
                ctx.confirmation_expired = True
        if (response and et is not None and gt > et and self._fast_sources_fresh()
                and current <= grid+1 and grid-current+cap <= s.main_fuse_amps-s.buffer_amps):
            basis = max(0., grid-current)
            self.complete_peak_response(now, minimum_response_ts=et,
                                        ceiling=s.main_fuse_amps-s.buffer_amps)
            self._ordinary_lower = None
            self._awaiting_reduction_target = self._house_before_reduction = self._settle_started = None
            self._clear_partial_reduction_alignment()
            if self._uncertainty_stops == 0:
                self._uncertainty_ceiling = self._uncertainty_previous_offer = None
            return ("ordinary_lower_complete", "The accepted decrease has genuine response and later buffered grid proof.", "set_limit", cap, basis)
        if not raw_safe:
            return None
        self._increase_since = None
        return ("ordinary_lower_offer", "Keeping only the lower raw-bounded offer while observations align; no EV subtraction, rise, resume or ten-second emergency timeout.", "set_limit", cap, grid)

    def _minimum_confirmation_plan(self, *, owner: str, now: float,
                                   grid: float, current: float, cap: int,
                                   source_id, accepted_at, response_deadline,
                                   response_at, response_valid: bool,
                                   response_owner: str, ceiling: float):
        """Single phase engine for every post-minimum observation route.

        None means no timely response qualified. An active phase always returns
        wait, complete or a terminal error, so a second handler cannot reinterpret
        it as an unobserved electrical response with an already spent deadline.
        """
        s = self.settings
        window = self._minimum_confirmation
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        if window is not None and window.owner != owner:
            return "competing_confirmation_owner", None
        if window is None:
            if (s.grid_confirmation_seconds <= 0 or not response_valid
                    or self._last_input_snapshot_verified or source_id is None
                    or accepted_at is None or response_at is None
                    or et is None or gt is None
                    or self.last_target is None or self.last_set_limit_ts is None):
                return None
            if (self._ev_event_source_known and (self._ev_event_source_ts is None
                    or wall is None
                    or self._ev_event_source_ts <= wall-(now-accepted_at))):
                return None
            window = MinimumGridConfirmation.start(owner=owner,
                source_id=source_id, cap=cap, now=now, accepted_at=accepted_at,
                response_at=response_at, response_deadline=response_deadline,
                seconds=s.grid_confirmation_seconds,
                lease_expires=self.last_set_limit_ts+self.last_target[3],
                ev_ts=et, grid_ts=gt, current=current,
                # A directly associated source event can conservatively reject
                # an already later HIGH sample despite network receipt lag. It
                # cannot shorten the response clock, prove success or supply
                # physical acquisition time; success retains its later floor.
                high_grid_floor=min(et, self._ev_event_source_ts)
                    if self._ev_event_source_known else None)
            if window is None:
                return None
            self._minimum_confirmation = window
            self._pin_ev_owner("_minimum_confirmation_ev", copy_from=response_owner)
        if (source_id != window.source_id or source_id != self._last_input_control_source_id
                or source_id != self._accepted_control_source_id
                or cap != window.cap or self.last_target is None or self.last_target[0] != cap
                or self.last_set_limit_ts is None
                or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or not self._last_input_current_trusted or self._restart_required or self.paused_by_us
                or self._ev_clock_error is not None
                or wall is None or gt is None or et is None
                or not 0 <= wall-gt <= s.max_sensor_age_seconds
                or not 0 <= wall-et <= s.max_sensor_age_seconds
                or not 1 <= current <= cap+.5
                or self._ev_event_source_known and (self._ev_event_source_ts is None
                    or not -5 <= wall-self._ev_event_source_ts <= s.max_sensor_age_seconds)
                or grid > min(38, s.overload_transition_limit_amps)):
            return "source_or_control_invalid", None
        result = window.observe(now=now, grid=grid, current=current,
            grid_ts=gt, ev_ts=et, ceiling=ceiling,
            event_advanced=self._ev_advanced_since(window.last_ev_ts, "_minimum_confirmation_ev"),
            event_regressed=self._ev_regressed_since(window.last_ev_ts),
            explicit_clock=self._ev_clock_explicit,
            baseline_value_hold=owner != "overload")
        if result == "complete":
            self.complete_peak_response(now, minimum_response_ts=window.first_ev_ts, ceiling=ceiling)
        self._pin_ev_owner("_minimum_confirmation_ev", wall_floor=(
            self._ev_owner_clocks.get(response_owner, {}).get("wall_floor")))
        return result, max(0.0, grid-current) if result == "complete" else None

    def _clear_low_grid_alignment(self) -> None:
        self._clear_minimum_confirmation("low_grid")
        self._low_grid_pending = False
        self._low_grid_deadline: Optional[float] = None
        self._low_grid_origin: Optional[float] = None
        self._low_grid_cap: Optional[int] = None
        self._low_grid_previous: Optional[int] = None
        self._low_grid_source_id = None
        self._low_grid_ev_floor: Optional[float] = None
        self._low_grid_grid_floor: Optional[float] = None
        self._low_grid_failure: Optional[str] = None
        self._low_grid_attempt_at: Optional[float] = None
        self._low_grid_phase: Optional[str] = None
        self._low_grid_accepted_at: Optional[float] = None
        self._low_grid_ack_ev_floor: Optional[float] = None
        self._low_grid_last_grid_ts: Optional[float] = None
        self._low_grid_last_ev_ts: Optional[float] = None
        self._low_grid_entry_house = 0.0

    def _low_grid_alignment_plan(self, now: float, grid: float, current: float, status: str):
        """A source-bound, only-decreasing offer under the explicit import model.

        Initial uncertainty uses a raw-grid bound. A later coherent pair may
        hand the SAME remaining time to the accepted reduction guard. If the
        entire unresolved prior offer fits beside raw grid, only the lower
        accepted cap may instead be retained without another response timer.
        No handoff treats a GET receipt or an accepted cap as physical proof.
        """
        s = self.settings
        identity = self._last_input_control_source_id
        numeric_conflict = (current > grid + 1
                           or (self._positive_cap is not None and current > self._positive_cap + .5))
        source_rejection = self._numeric_source_rejection()
        metadata_invalid = source_rejection not in {
            None, "charger_receipt_stale", "report_skew_exceeds_alignment_window"}
        known_offer = (self._low_grid_previous if self._low_grid_deadline is not None else
                       max(self.last_target[0] if self.last_target else 0,
                           self._uncertainty_previous_offer or 0, self._reduction_previous_cap or 0))
        beyond_known_offer = (source_rejection is not None
                              and current > min(s.max_charge_amps, known_offer) + .5)
        invalid_lease = bool(source_rejection is not None and self.last_target is not None
            and (self.last_set_limit_ts is None
                 or not 0 <= now - self.last_set_limit_ts < self.last_target[3]))
        # An unchanged healthy EVENT can legitimately be old or skewed. Age
        # alone is neither a numeric contradiction nor a physical response
        # that needs this additional deadline. Existing freshness and raw-cap
        # guards still govern ordinary control when no episode is pending.
        if not self._low_grid_pending and not (identity is not None and current >= 1
                and (numeric_conflict or metadata_invalid or beyond_known_offer or invalid_lease)):
            return None
        if (not self._low_grid_pending and self._low_grid_deadline is not None
                and self._low_grid_failure is None and not numeric_conflict
                and not metadata_invalid and not beyond_known_offer and not invalid_lease):
            # A completed coherent episode is not reopened solely because an
            # unchanged, healthy event-driven state later becomes old.
            return None
        # A completed episode retains its original window. A different accepted
        # cap (not a same-value TTL ACK) is needed to establish new authority.
        active = self._low_grid_pending
        cap = self._low_grid_cap if self._low_grid_deadline is not None else self.startup_current

        def stop(code: str):
            self._clear_minimum_confirmation("low_grid")
            self._low_grid_pending = False
            self._low_grid_phase = None
            self._low_grid_failure = code
            self._uncertainty_ceiling = min(self._uncertainty_ceiling or cap, cap)
            self._restart_required = True
            self.reset_timers()
            self._clear_positive_alignment()
            self._awaiting_reduction_target = None
            self._house_before_reduction = None
            self._settle_started = None
            self._clear_partial_reduction_alignment()
            return ("low_grid_alignment_stop", f"Low-grid measurement alignment failed: {code}; requesting 0 A without changing experimental allowance.", 0, grid)

        if self._low_grid_failure is not None:
            return stop(self._low_grid_failure) if active else None
        accepted = self.last_target[0] if self.last_target is not None else None
        if not active and not (s.nominal_import_only_fallback and s.enable_dynamic_limit
                and not self._restart_required and not self.paused_by_us
                and status in START_ALLOWED_STATUSES and accepted is not None and accepted >= cap
                and self._accepted_control_source_id == identity
                and self._overload_transition_started is None and not self._overload_hold_active
                and not (self._normal_shed_deadline is not None and self._normal_shed_failure is None)
                and grid + cap <= s.main_fuse_amps - s.buffer_amps):
            return None
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        previous = self._low_grid_previous if self._low_grid_deadline is not None else max(
            accepted or 0, self._uncertainty_previous_offer or 0,
            self._reduction_previous_cap or 0)
        timely_minimum = bool(s.grid_confirmation_seconds > 0 and active
            and accepted == cap and self._low_grid_accepted_at is not None
            and self._low_grid_ack_ev_floor is not None
            and self._low_grid_deadline is not None and wall is not None and et is not None
            and not self._last_input_snapshot_verified and 1 <= current <= cap+.5
            and et > self._low_grid_ack_ev_floor
            and self._ev_advanced_since(self._low_grid_ev_floor, "_low_grid_ev_floor")
            and self._low_grid_accepted_at < min(now, now-(wall-et)) < self._low_grid_deadline)
        valid = bool(s.nominal_import_only_fallback and s.enable_dynamic_limit
            and identity is not None and identity == self._accepted_control_source_id
            and (self._low_grid_source_id is None or identity == self._low_grid_source_id)
            and not self._restart_required and not self.paused_by_us
            and status in START_ALLOWED_STATUSES and self._last_input_current_trusted
            and self._overload_transition_started is None and not self._overload_hold_active
            and self.nominal_after_overload_allowed
            and accepted is not None and s.min_charge_amps <= cap <= accepted <= s.max_charge_amps
            and self.last_set_limit_ts is not None and 0 <= now - self.last_set_limit_ts < self.last_target[3]
            and wall is not None and gt is not None and et is not None
            and _valid_number(s.max_sensor_age_seconds) is not None
            and 0 <= wall - gt <= s.max_sensor_age_seconds and et <= wall + 2
            and current <= min(s.max_charge_amps, previous) + .5
            and (grid <= s.main_fuse_amps - s.buffer_amps
                 or self._minimum_confirmation is not None
                 and self._minimum_confirmation.owner == "low_grid"
                 or timely_minimum and gt is not None and gt < et
                 and grid <= min(38, s.overload_transition_limit_amps)))
        if not valid:
            return stop("source_control_or_raw_budget_invalid")
        if not active and not numeric_conflict:
            # Invalid provenance/value/lease bounds retain their protective zero;
            # they must never purchase a positive alignment window instead.
            return stop("source_control_or_raw_budget_invalid")
        if self._low_grid_deadline is None:
            configured = _valid_number(s.measurement_settle_seconds)
            if configured is None or configured <= 0:
                return stop("invalid_alignment_window")
            window = min(configured, s.max_sensor_age_seconds) if s.peak_only_response else min(10., configured, s.max_sensor_age_seconds)
            self._low_grid_origin = now - (wall - gt)
            self._low_grid_deadline = self._low_grid_origin + window
            peak_deadline = self.peak_response_deadline(now)
            if grid > PEAK_RESPONSE_TRIGGER_AMPS and peak_deadline is not None:
                self._low_grid_deadline = min(self._low_grid_deadline, peak_deadline)
            if self._positive_skew_since is not None:
                positive_deadline = (self._positive_skew_since
                    + min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                    - self._positive_alignment_used_seconds)
                self._low_grid_deadline = min(self._low_grid_deadline, positive_deadline)
            if self._awaiting_reduction_target is not None and self._settle_started is not None:
                self._low_grid_deadline = min(self._low_grid_deadline,
                    now + self._reduction_alignment_remaining(now))
            if self._reduction_guard_deadline is not None:
                self._low_grid_deadline = min(self._low_grid_deadline, self._reduction_guard_deadline)
            if self._nominal_shedding_started is not None:
                self._low_grid_deadline = min(self._low_grid_deadline,
                    self._nominal_shedding_started + (self._nominal_shedding_budget or 0.0))
            self._low_grid_cap, self._low_grid_previous = cap, previous
            self._low_grid_entry_house = grid
            self._low_grid_phase = "alignment"
            self._low_grid_source_id = identity
            # An EV-first event can itself start this episode. Pair that event
            # with the later grid; do not demand an additional value-change
            # event merely because the paired GET copied the same state.
            self._low_grid_ev_floor = self._accepted_control_ev_ts
            self._pin_ev_owner("_low_grid_ev_floor", copy_from="_accepted_control_ev_ts")
            self._low_grid_grid_floor = gt
            self._uncertainty_ceiling = min(self._uncertainty_ceiling or cap, cap)
            self._uncertainty_previous_offer = max(self._uncertainty_previous_offer or 0, previous)
        self._low_grid_pending = True
        if (gt < self._low_grid_grid_floor
                or self._low_grid_last_grid_ts is not None and gt < self._low_grid_last_grid_ts
                or self._ev_regressed_since(self._low_grid_last_ev_ts)):
            return stop("source_clock_regressed")
        self._low_grid_last_grid_ts, self._low_grid_last_ev_ts = gt, et
        if accepted == cap and self._low_grid_accepted_at is None:
            # An already accepted lower lease can precede this episode. Freeze
            # its UTC ACK floor once; identical renewals must never move it.
            self._low_grid_accepted_at = self.last_set_limit_ts
            self._low_grid_ack_ev_floor = wall - (now - self.last_set_limit_ts)
            self._set_ev_owner_wall("_low_grid_ev_floor", self._low_grid_ack_ev_floor)
        soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
        pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
        self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
        if s.enable_pause_resume and pause:
            return stop("pause_guard")
        if s.enable_soft_guard and soft and s.soft_current_amps < cap:
            return stop("soft_guard")
        self._increase_since = None
        if s.grid_confirmation_seconds > 0:
            response_valid = bool(accepted == cap and self._low_grid_ack_ev_floor is not None
                and et > self._low_grid_ack_ev_floor
                and self._ev_advanced_since(self._low_grid_ev_floor, "_low_grid_ev_floor")
                and 1 <= current <= cap+.5)
            response_at = min(now, now-(wall-et)) if response_valid else None
            sync = self._minimum_confirmation_plan(owner="low_grid", now=now,
                grid=grid, current=current, cap=cap,
                source_id=self._low_grid_source_id, accepted_at=self._low_grid_accepted_at,
                response_deadline=((self.peak_response_deadline(now)
                    if grid > PEAK_RESPONSE_TRIGGER_AMPS else None)
                    if s.peak_only_response else self._low_grid_deadline), response_at=response_at,
                response_valid=response_valid, response_owner="_low_grid_ev_floor",
                ceiling=s.main_fuse_amps-s.buffer_amps)
            if sync is not None:
                outcome, basis = sync
                if outcome == "complete":
                    self._clear_minimum_confirmation("low_grid")
                    self._low_grid_pending = False
                    self._low_grid_phase = None
                    self._awaiting_reduction_target = None
                    self._house_before_reduction = None
                    self._settle_started = None
                    self._clear_partial_reduction_alignment()
                    self._clear_positive_alignment()
                    return ("low_grid_alignment_complete", "The timely minimum-current response and a later coherent grid report complete the shared confirmation phase.", cap, basis)
                if outcome != "wait":
                    return stop("grid_confirmation_"+outcome)
                self._low_grid_phase = "grid_wait"
                return ("low_grid_grid_wait", "The minimum-current response was timely; retaining only the accepted offer while the shared fixed grid-confirmation phase awaits a later grid. No write or lease renewal.", cap, grid)
        anchor = max(self._house_before_reduction or 0.0, self._low_grid_entry_house,
                     max(0.0, grid - current))
        complete = bool(accepted == cap and (self._low_grid_ev_floor is not None or self._ev_clock_explicit)
            and self._low_grid_ack_ev_floor is not None
            and et > self._low_grid_ack_ev_floor
            and self._ev_advanced_since(self._low_grid_ev_floor, "_low_grid_ev_floor")
            and self._fast_sources_fresh() and gt > et
            and gt > self._low_grid_grid_floor and current <= cap + .5 and current <= grid + 1
            and anchor + cap <= s.main_fuse_amps - s.buffer_amps)
        if complete and (now < self._low_grid_deadline or self._low_grid_phase == "raw_hold"):
            self.complete_peak_response(now, minimum_response_ts=et,
                                        ceiling=s.main_fuse_amps-s.buffer_amps)
            self._low_grid_pending = False
            self._low_grid_phase = None
            self._awaiting_reduction_target = None
            self._house_before_reduction = None
            self._settle_started = None
            self._clear_partial_reduction_alignment()
            self._clear_positive_alignment()
            return ("low_grid_alignment_complete", "Accepted lower offer has an advanced trusted EV event and later coherent buffered grid; experimental allowance is unchanged.", cap, anchor)
        whole_authority = max(previous, cap)
        whole_authority_fits = grid + whole_authority <= s.main_fuse_amps - s.buffer_amps
        if (accepted == cap and whole_authority_fits
                and (now >= self._low_grid_deadline or self._low_grid_phase == "raw_hold")):
            # This is a normal raw-bound hold, not an extension of the response
            # deadline. Keep the pending marker so reload interrupts safely,
            # and retain ALL earlier authority until real response evidence.
            self._low_grid_phase = "raw_hold"
            self._reduction_guard_deadline = min(
                self._reduction_guard_deadline or self._low_grid_deadline,
                self._low_grid_deadline)
            return ("low_grid_raw_hold", f"Holding accepted {cap} A without EV subtraction: raw L1 plus the entire unresolved {whole_authority} A offer fits the normal buffer; original alignment time is unchanged and no rise or resume is permitted.", cap, grid)
        if now >= self._low_grid_deadline:
            return stop("alignment_timeout")
        coherent_pair = bool(accepted == cap and self._fast_sources_fresh()
            and gt > et and gt > self._low_grid_grid_floor and current <= grid + 1)
        if (self._low_grid_phase == "reduction"
                or coherent_pair and current > cap + .5 and previous > cap):
            if (accepted != cap or self._awaiting_reduction_target != cap
                    or self._accepted_reduction_cap != cap
                    or anchor + cap > s.main_fuse_amps - s.buffer_amps
                    or not self._fast_sources_fresh()):
                return stop("accepted_reduction_handoff_invalid")
            # The coherent pair establishes only a conservative house anchor.
            # An EV event preceding this lower ACK is NOT lower-cap response.
            self._low_grid_phase = "reduction"
            self._house_before_reduction = anchor
            self._reduction_guard_deadline = min(
                self._reduction_guard_deadline or self._low_grid_deadline,
                self._low_grid_deadline)
            if self._settle_started is None:
                self._begin_reduction_alignment(now, guarded=True)
            return None
        if self._low_grid_phase == "raw_hold" or grid + cap > s.main_fuse_amps - s.buffer_amps:
            return stop("unresolved_authority_exceeds_raw_budget")
        return ("low_grid_alignment", "Holding only the conservative lower offer within the original raw-grid alignment window; paired current/status refresh requested, without EV subtraction or resume.", cap, grid)

    def _clear_bounded_nominal_shedding(self) -> None:
        self._clear_minimum_confirmation("nominal")
        self._normal_shed_origin: Optional[float] = None
        self._normal_shed_deadline: Optional[float] = None
        self._normal_shed_cap: Optional[int] = None
        self._normal_shed_previous: Optional[int] = None
        self._normal_shed_entry_cap: Optional[int] = None
        self._normal_shed_last_grid_ts: Optional[float] = None
        self._normal_shed_last_ev_ts: Optional[float] = None
        self._normal_shed_grid_floor: Optional[float] = None
        self._normal_shed_ev_floor: Optional[float] = None
        self._normal_shed_accepted_at: Optional[float] = None
        self._normal_shed_response_ts: Optional[float] = None
        self._normal_shed_attempt_at: Optional[float] = None
        self._normal_shed_failure: Optional[str] = None
        self._normal_shed_source_id = None
        self._normal_shed_response_monotonic: Optional[float] = None
        self._normal_shed_sync_deadline: Optional[float] = None
        self._normal_shed_sync_value: Optional[float] = None
        self._normal_shed_sync_floor: Optional[float] = None
        self._normal_shed_sync_last_ev_ts: Optional[float] = None
        self._normal_shed_sync_last_grid_ts: Optional[float] = None
        self._normal_shed_grid_wait = False

    def _nominal_sync_plan(self, now: float, grid: float, current: float,
                           response_valid: bool, stop, complete):
        """Wait briefly for publication ordering, never for another EV rise.

        This is explicit sampling tolerance, not simultaneous measurements or
        a fuse thermal model. The first response owns the only sync deadline;
        a substantively changed current needs a later grid, without more time.
        """
        s = self.settings
        cap = self._normal_shed_cap
        active = self._normal_shed_sync_deadline is not None
        grid_wait = self._normal_shed_grid_wait if active else s.grid_confirmation_seconds > 0
        mode = "normal_shedding_grid_wait" if grid_wait else "normal_shedding_sync"
        identity = self._last_input_control_source_id
        wall, gt, et = self._last_input_wall_now_ts, self._last_input_grid_ts, self._last_input_charger_ts
        if grid_wait:
            sync = self._minimum_confirmation_plan(owner="nominal", now=now,
                grid=grid, current=current, cap=cap,
                source_id=self._normal_shed_source_id,
                accepted_at=self._normal_shed_accepted_at,
                response_deadline=((self.peak_response_deadline(now)
                    if grid > PEAK_RESPONSE_TRIGGER_AMPS else None)
                    if s.peak_only_response else self._normal_shed_deadline),
                response_at=self._normal_shed_response_monotonic,
                response_valid=response_valid, response_owner="_normal_shed_ev_floor",
                ceiling=s.main_fuse_amps-s.buffer_amps)
            if sync is None:
                return None
            outcome, basis = sync
            window = self._minimum_confirmation
            if window is not None:
                self._normal_shed_sync_deadline = window.deadline
                self._normal_shed_sync_floor = window.floor
                self._normal_shed_sync_value = window.value
                self._normal_shed_grid_wait = True
            if outcome == "complete":
                return complete(basis)
            if outcome != "wait":
                code = {"timeout": "grid_confirmation_timeout",
                        "outside_ceiling": "grid_confirmation_outside_nominal_budget",
                        "response_outside_ceiling": "grid_confirmation_outside_nominal_budget",
                        "source_clock_regressed": "sync_source_clock_regressed"}.get(
                            outcome, "sync_source_or_nominal_bounds_invalid")
                return stop(code)
            return (mode, "The minimum-current response was timely; awaiting a later grid report in the shared fixed confirmation phase. No write or lease renewal.",
                    None, cap, grid, s.normal_ttl_seconds)
        if not active:
            if (self._normal_shed_source_id is None or not response_valid
                    or self._normal_shed_response_monotonic is None
                    or self._normal_shed_accepted_at is None
                    or self._normal_shed_ev_floor is None
                    or self._normal_shed_grid_floor is None
                    or gt is None
                    or gt <= max(self._normal_shed_grid_floor, self._normal_shed_ev_floor)
                    or grid > s.main_fuse_amps - s.buffer_amps):
                return None
        if (identity is None or identity != self._normal_shed_source_id
                or identity != self._accepted_control_source_id
                or wall is None or gt is None or et is None
                or not self._last_input_current_trusted
                or not 0 <= wall - et <= s.max_sensor_age_seconds
                or not 0 <= wall - gt <= s.max_sensor_age_seconds
                or cap is None or not 1 <= current <= cap + .5
                or grid > s.main_fuse_amps - s.buffer_amps
                or self.last_target is None or self.last_target[0] != cap):
            return stop("sync_source_or_nominal_bounds_invalid")
        if active:
            if (self._ev_regressed_since(self._normal_shed_sync_last_ev_ts) or gt < self._normal_shed_sync_last_grid_ts):
                return stop("sync_source_clock_regressed")
        else:
            window = _valid_number(s.measurement_settle_seconds)
            if window is None or window <= 0:
                return stop("sync_alignment_window_invalid")
            first = self._normal_shed_response_monotonic
            if (not self._normal_shed_accepted_at < first
                    or not s.peak_only_response and first >= self._normal_shed_deadline):
                return None
            deadline = min(first + min(5.0, window), self._normal_shed_origin + window)
            if now >= deadline:
                # A grace never used cannot retroactively shorten the still
                # live original response window. It also cannot be restarted
                # from this later observation or a new identical EV receipt.
                return None
            self._normal_shed_sync_deadline = deadline
            self._normal_shed_grid_wait = False
            self._normal_shed_sync_value = current
            self._normal_shed_sync_floor = et
        if now >= self._normal_shed_sync_deadline:
            return stop("sync_timeout")
        if abs(current - self._normal_shed_sync_value) > .1:
            if not self._ev_advanced_since(self._normal_shed_sync_last_ev_ts, "_normal_shed_sync_last_ev_ts"):
                if self._ev_clock_explicit:
                    # A different reporting clock supplies no new response
                    # just by describing a different cached value. Preserve
                    # only this already fixed no-write synchronization window.
                    return (mode, "The changed observation has no post-ACK event credit yet; retaining the existing offer only inside its original synchronization deadline.",
                            None, cap, grid, s.normal_ttl_seconds)
                return stop("sync_value_changed_without_new_source_event")
            self._normal_shed_sync_value = current
            self._normal_shed_sync_floor = et
        self._normal_shed_sync_last_ev_ts = et
        self._pin_ev_owner("_normal_shed_sync_last_ev_ts", wall_floor=(
            self._ev_owner_clocks.get("_normal_shed_ev_floor", {}).get("wall_floor")))
        self._normal_shed_sync_last_grid_ts = gt
        if (gt > self._normal_shed_sync_floor and current <= grid + 1
                and grid - current + cap <= s.main_fuse_amps - s.buffer_amps):
            return complete(max(0.0, grid - current))
        return (mode, "The lower accepted offer has a timely reported response; awaiting one later grid observation inside the fixed post-response window. No command, increase, resume or lease renewal is permitted.",
                None, cap, grid, s.normal_ttl_seconds)

    def _bounded_normal_shedding_plan(self, now: float, grid: float, current: float,
                                      house: float, status: str, *, raw_timeout: bool = False):
        """Reduce existing authority inside one sample-aged response window.

        Entry credits no EV current. The old experimental failure/budget stay
        intact. A new post-ACK EV event and later grid must prove nominal
        response, unless the separate verified raw/import contract proves it.
        """
        s = self.settings
        pending = self._normal_shed_deadline is not None and self._normal_shed_failure is None
        if (s.automatic_overload_cycles and not pending and not raw_timeout
                and grid <= PEAK_RESPONSE_TRIGGER_AMPS and self.last_target is not None
                and self._numeric_source_rejection() is None and current <= grid+1):
            nominal = int(floor(min(s.max_charge_amps, s.main_fuse_amps-s.buffer_amps
                - max(house, self._house_before_reduction or 0.))))
            if s.min_charge_amps <= nominal <= self.last_target[0]:
                # A withdrawn automatic envelope does not require an arbitrary
                #7A request when ordinary coherent limiting already fits23A.
                return None

        def stop(code: str):
            self._clear_minimum_confirmation("nominal")
            if self._uncertainty_ceiling is None and self.last_target is not None:
                self._uncertainty_ceiling = min(self.last_target[0], self.startup_current)
                self._uncertainty_previous_offer = self.last_target[0]
            self._normal_shed_failure = code
            self._normal_shed_sync_deadline = None
            self.reset_timers()
            self._clear_positive_alignment()
            self._restart_required = True
            return ("normal_shedding_stop", f"Bounded nominal reduction failed: {code}; requesting 0 A. Experimental budget and failure remain unchanged.",
                    "set_limit", 0, grid, s.normal_ttl_seconds)

        def complete(basis: float):
            self.complete_peak_response(now, minimum_response_ts=self._normal_shed_response_ts,
                                        ceiling=s.main_fuse_amps-s.buffer_amps)
            self._awaiting_reduction_target = None
            self._house_before_reduction = None
            self._settle_started = None
            self._clear_partial_reduction_alignment()
            self._clear_positive_alignment()
            self._clear_bounded_nominal_shedding()
            return ("normal_shedding_complete", "Lower accepted offer has fresh nominal response evidence; experimental allowance remains blocked.",
                    "set_limit", cap, basis, s.normal_ttl_seconds)

        if self._normal_shed_failure is not None:
            if self._normal_shed_sync_deadline is not None:
                return stop(self._normal_shed_failure)
            return None
        # An explicit epoch owns its own response/budget failure. The older
        # nominal trial must not turn its exhaustion into another ten seconds.
        if not pending and self._trial_epoch is not None and not s.automatic_overload_cycles:
            return None
        if not pending and not (s.bounded_nominal_shedding and s.overload_enabled
                and s.enable_dynamic_limit and status == "charging" and current >= 1
                and self._last_input_current_trusted and not self._restart_required
                and not self.paused_by_us and self.last_target is not None
                and self.last_target[0] > self.startup_current
                and self._overload_transition_started is None and not self._overload_hold_active
                and self.nominal_after_overload_allowed
                and (raw_timeout or self.effective_overload_failure_reason is not None
                     or self.effective_overload_used_seconds >= self.effective_overload_budget_seconds)
                and (grid > s.main_fuse_amps or raw_timeout
                     and s.nominal_import_only_fallback
                     and grid+self.startup_current <= s.main_fuse_amps-s.buffer_amps)):
            return None
        wall, grid_ts, ev_ts = (self._last_input_wall_now_ts, self._last_input_grid_ts,
                                self._last_input_charger_ts)
        valid_source = bool(wall is not None and grid_ts is not None and ev_ts is not None
            and self._last_input_now_ts is not None and self._last_input_current_trusted
            and _valid_number(s.max_sensor_age_seconds) is not None
            and 0 <= wall - grid_ts <= s.max_sensor_age_seconds and ev_ts <= wall + 2)
        accepted = self.last_target[0] if self.last_target is not None else None
        inherited = (self._unresolved_reduction_owner(now, accepted, current)
                     if not pending else None)
        previous = (self._normal_shed_previous if pending else
                    inherited[0] if inherited is not None else accepted or 0)
        live_lease = bool(self.last_target is not None and self.last_set_limit_ts is not None
                          and 0 <= now - self.last_set_limit_ts < self.last_target[3])
        guards_valid = bool(s.bounded_nominal_shedding and s.overload_enabled and s.enable_dynamic_limit
            and not self._restart_required and not self.paused_by_us and status == "charging"
            and self._overload_transition_started is None and not self._overload_hold_active
            and self.nominal_after_overload_allowed
            and (not pending or self._normal_shed_source_id is None
                 or self._last_input_control_source_id == self._normal_shed_source_id == self._accepted_control_source_id)
            and accepted is not None and s.min_charge_amps <= accepted <= s.max_charge_amps
            and current >= 1 and current <= min(s.max_charge_amps, previous) + .5)
        if not pending:
            # An invalid entry must not create new positive authority. The
            # existing protective path still determines its zero diagnosis.
            if grid > min(38, s.overload_transition_limit_amps):
                return None
            if not valid_source or not live_lease or not guards_valid:
                return stop("source_or_prior_control_invalid")
            age = wall - grid_ts
            deadline = now - age + (min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                if s.peak_only_response else min(10, s.overload_response_seconds))
            peak_deadline = self.peak_response_deadline(now)
            if grid > PEAK_RESPONSE_TRIGGER_AMPS and peak_deadline is not None:
                deadline = min(deadline, peak_deadline)
            raw_owner_deadline = (self._positive_raw_deadline
                if not raw_timeout and self._positive_raw_recovery and self._positive_cap == accepted else None)
            if raw_owner_deadline is not None:
                deadline = min(deadline, raw_owner_deadline)
            if inherited is not None:
                # A later high-grid report changes the required lower offer,
                # not the owner of the already unobserved decrease. Preserve
                # its finite remaining clock, including spent alignment time.
                deadline = min(deadline, inherited[1])
            if deadline <= now:
                return stop("inherited_response_window_expired") if inherited is not None or raw_owner_deadline is not None else None
            self._normal_shed_deadline = deadline
            self._normal_shed_origin = now - age
            self._normal_shed_cap = self.startup_current
            self._normal_shed_previous = previous
            self._normal_shed_entry_cap = accepted
            self._normal_shed_grid_floor = grid_ts
            self._normal_shed_ev_floor = ev_ts
            self._pin_ev_owner("_normal_shed_ev_floor")
            self._normal_shed_source_id = self._last_input_control_source_id
            self._uncertainty_ceiling = min(self._uncertainty_ceiling or accepted, self.startup_current)
            self._uncertainty_previous_offer = max(self._uncertainty_previous_offer or accepted, accepted)
            if raw_timeout:
                # The earlier RISE did not acquire more time or authority. Its
                # already live cap is handed to a distinct, only-decreasing
                # minimum request bounded by the latest raw grid with zero EV
                # subtraction. The same fixed reduction phase owns every retry.
                self._clear_positive_alignment()
            pending = True
        assert self._normal_shed_deadline is not None and self._normal_shed_cap is not None
        if self._normal_shed_origin is not None:
            self._normal_shed_deadline = min(self._normal_shed_deadline,
                self._normal_shed_origin + (min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                    if s.peak_only_response else min(10, s.overload_response_seconds)))
            peak_deadline = self.peak_response_deadline(now)
            if grid > PEAK_RESPONSE_TRIGGER_AMPS and peak_deadline is not None:
                self._normal_shed_deadline = min(self._normal_shed_deadline, peak_deadline)
        cap = self._normal_shed_cap
        if (not valid_source or not guards_valid or not live_lease
                or not s.min_charge_amps <= cap <= s.max_charge_amps
                or accepted not in {cap, self._normal_shed_entry_cap}
                or grid_ts < self._normal_shed_grid_floor):
            return stop("source_or_control_invalid")
        if ((self._normal_shed_last_grid_ts is not None and grid_ts < self._normal_shed_last_grid_ts)
                or self._ev_regressed_since(self._normal_shed_last_ev_ts)):
            return stop("source_clock_regressed")
        self._normal_shed_last_grid_ts, self._normal_shed_last_ev_ts = grid_ts, ev_ts
        if grid > min(38, s.overload_transition_limit_amps):
            return stop("raw_peak_exceeded")
        if (s.grid_confirmation_seconds == 0 and self._normal_shed_sync_deadline is None
                and now >= self._normal_shed_deadline):
            return stop("response_timeout")
        soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
        pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
        self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
        if s.enable_pause_resume and pause:
            self._normal_shed_failure = "pause_guard"
            self._restart_required = True
            return ("pause", "Pause guard overrides bounded nominal reduction.", "pause", 0, grid, s.normal_ttl_seconds)
        if s.enable_soft_guard and soft and s.soft_current_amps < cap:
            # A distinct stronger protection must not inherit a fresh grace.
            return stop("soft_guard")
        self._increase_since = None
        if self._normal_shed_accepted_at is not None and accepted == cap:
            response_valid = bool((self._normal_shed_ev_floor is not None or self._ev_clock_explicit)
                and self._ev_advanced_since(self._normal_shed_ev_floor, "_normal_shed_ev_floor")
                and -2 <= wall - ev_ts <= s.max_sensor_age_seconds
                and 1 <= current <= cap + .5)
            if response_valid:
                if self._normal_shed_response_ts is None:
                    self._normal_shed_response_ts = ev_ts
                    self._pin_ev_owner("_normal_shed_response_ts", copy_from="_normal_shed_ev_floor")
                if self._normal_shed_response_monotonic is None:
                    self._normal_shed_response_monotonic = min(now, now - (wall - ev_ts))
            elif current > cap + .5:
                self._normal_shed_response_ts = None
            if self._normal_shed_sync_deadline is None and now >= self._normal_shed_deadline:
                # The extra phase can recognize only a genuinely timely EV
                # response. A late EV event or a cached/GET value never buys
                # another window, even when a later grid looks favorable.
                if s.grid_confirmation_seconds > 0:
                    sync = self._nominal_sync_plan(now, grid, current, response_valid, stop, complete)
                    if sync is not None:
                        return sync
                return stop("response_timeout")
            response = self._normal_shed_response_ts
            ev_proof = bool(response is not None and response_valid and grid_ts > ev_ts
                            and grid_ts > self._normal_shed_grid_floor and current <= grid + 1
                            and grid - current + cap <= s.main_fuse_amps - s.buffer_amps)
            raw_proof = bool(s.nominal_import_only_fallback and grid_ts > self._normal_shed_grid_floor
                             and grid + cap <= s.main_fuse_amps - s.buffer_amps)
            if self._normal_shed_sync_deadline is None and (ev_proof or raw_proof):
                basis = max(0.0, grid - current) if ev_proof else grid
                return complete(basis)
            sync = self._nominal_sync_plan(now, grid, current, response_valid, stop, complete)
            if sync is not None:
                return sync
        if now >= self._normal_shed_deadline:
            return stop("response_timeout")
        return ("normal_shedding", "Requesting only a lower accepted offer inside the original sample-aged response deadline; no EV credit, increase, resume or new overload budget.",
                "set_limit", cap, grid, s.normal_ttl_seconds)

    def _unresolved_reduction_owner(self, now: float, accepted: Optional[int], current: float):
        """Carry only an actual accepted, source-bound pending decrease.

        A lower API offer is not proof that the old higher draw responded.
        This bound can qualify another decrease; it grants no EV subtraction,
        rise, fresh deadline, experimental time or completed-response claim.
        """
        previous, origin = self._reduction_previous_cap, self._reduction_chain_origin
        identity = self._last_input_control_source_id
        gt, et = self._last_input_grid_ts, self._last_input_charger_ts
        if (accepted is None or self.last_target is None or self.last_target[0] != accepted
                or self._awaiting_reduction_target != accepted or self._accepted_reduction_cap != accepted
                or previous is None or not self.settings.min_charge_amps <= accepted < previous <= self.settings.max_charge_amps
                or origin is None or not isfinite(origin) or not 0 <= now-origin
                or identity is None or identity != self._accepted_control_source_id
                or self._reduction_grid_ts is None or self._reduction_charger_ts is None
                or gt is None or et is None or gt < self._reduction_grid_ts or self._ev_regressed_since(self._reduction_charger_ts)
                or self._partial_reduction_budget is None or self._partial_reduction_budget <= 0
                or self._partial_reduction_used_seconds < 0):
            return None
        # A distinct, trusted post-ACK response and later coherent grid retire
        # the earlier higher offer. The following lower request is then based
        # on that verified current bound, not borrowed old authority. A TTL
        # renewal does not change this genuinely changed-cap ACK floor.
        wall = self._last_input_wall_now_ts
        if (wall is not None and self._last_limit_change_ts is not None
                and 0 <= now-self._last_limit_change_ts
                and et > wall-(now-self._last_limit_change_ts)
                and self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts")
                and gt > et and self._fast_sources_fresh()
                and 1 <= current <= accepted + .5):
            return None
        deadline = min(origin + self._partial_reduction_budget,
                       now + self._reduction_alignment_remaining(now))
        if self._reduction_guard_deadline is not None:
            deadline = min(deadline, self._reduction_guard_deadline)
        return previous, deadline

    def _clear_measurement_uncertainty(self) -> None:
        """Only session/reset or sustained source recovery retires this mode."""
        self._uncertainty_ceiling: Optional[int] = None
        self._uncertainty_previous_offer: Optional[int] = None
        self._uncertainty_recovery_since: Optional[float] = None
        self._uncertainty_recovery_ev_ts: Optional[float] = None
        self._uncertainty_recovery_grid_ts: Optional[float] = None
        self._uncertainty_recovery_epochs = 0
        self._uncertainty_stops = 0
        self._uncertainty_stop_recorded = False
        self._uncertainty_retry_after: Optional[float] = None
        self._uncertainty_restart = False
        self._uncertainty_last_attempt: Optional[float] = None
        self._uncertainty_last_attempt_cap: Optional[int] = None
        self._uncertainty_retry_submission_pending = False
        self._restored_normal_wait = 0.0

    def _record_uncertainty_stop(self, now: float) -> None:
        if self._uncertainty_ceiling is None or self._uncertainty_stop_recorded:
            return
        self._uncertainty_stop_recorded = True
        self._uncertainty_stops += 1
        self._uncertainty_restart = True
        self._uncertainty_retry_after = now + min(
            600, self.settings.restart_stable_seconds * 2 ** min(self._uncertainty_stops - 1, 3))
        self._uncertainty_recovery_since = None
        self._uncertainty_recovery_epochs = 0

    def _observe_uncertainty_recovery(self, now: float, grid: float, current: float,
                                      status: str) -> bool:
        """Do not promote a lower offer from a new GET receipt alone.

        Vendor current is an event-driven state, not a periodic sample. An
        unchanged trusted state plus multiple new grid observations can prove
        durable reported-state continuity, not a new physical acquisition.
        Ordinary increases still require their existing strict freshness gate.
        """
        if self._uncertainty_ceiling is None or self.last_target is None:
            return False
        cap = self.last_target[0]
        available, _ = self.calculate_available_l1(grid, current)
        valid = (not self._restart_required and not self._uncertainty_restart
                 and self._meaningfully_charging(status, current)
                 and self.settings.min_charge_amps <= cap <= self.settings.max_charge_amps
                 and cap <= self._uncertainty_ceiling
                 and self._current_cache_and_grid_usable() and current <= cap + 0.5 and current <= grid + 1
                 and grid <= self.settings.main_fuse_amps - self.settings.buffer_amps and available >= cap)
        if not valid:
            self._uncertainty_recovery_since = None
            self._uncertainty_recovery_epochs = 0
            self._uncertainty_recovery_ev_ts = self._uncertainty_recovery_grid_ts = None
            return False
        if self._uncertainty_recovery_since is None:
            self._uncertainty_recovery_since = now
            self._uncertainty_recovery_epochs = 1
            self._uncertainty_recovery_ev_ts = self._last_input_charger_ts
            self._uncertainty_recovery_grid_ts = self._last_input_grid_ts
        elif self._last_input_grid_ts > self._uncertainty_recovery_grid_ts:
            self._uncertainty_recovery_epochs += 1
            self._uncertainty_recovery_ev_ts = self._last_input_charger_ts
            self._uncertainty_recovery_grid_ts = self._last_input_grid_ts
        if now - self._uncertainty_recovery_since < 30 or self._uncertainty_recovery_epochs < 2:
            return False
        # A tapered EV may already be below its offer. This retires only the
        # uncertainty mode; an old current event earns no new rise/resume.
        if self._awaiting_reduction_target == cap:
            self._finish_reduction_alignment(now)
        if self._positive_cap == cap:
            self._clear_positive_alignment()
        self._clear_measurement_uncertainty()
        return True

    def _current_cache_and_grid_usable(self) -> bool:
        """Health/trust and fresh raw grid, without forging an EV sample age."""
        s = self.settings
        wall, grid, ev = (self._last_input_wall_now_ts, self._last_input_grid_ts,
                          self._last_input_charger_ts)
        return bool(self._last_input_current_trusted
                    and wall is not None and grid is not None and ev is not None
                    and _valid_number(s.max_sensor_age_seconds) is not None
                    and -5 <= wall - grid <= s.max_sensor_age_seconds
                    and ev <= wall + 5)

    def _uncertainty_raw_cap(self, grid: float, current: float, status: str) -> Optional[int]:
        """Only under an explicit import/phase/RMS installation contract.

        This is a normal-budget cap, not an overload transition or EV credit.
        It cannot override an existing stop, real fault or experimental timer.
        """
        s = self.settings
        if (not s.nominal_import_only_fallback or not s.enable_dynamic_limit
                or self._restart_required or self.paused_by_us or status != "charging"
                or self._overload_transition_started is not None or self._overload_hold_active
                or self.last_target is None or self._last_input_wall_now_ts is None
                or self._last_input_grid_ts is None
                or not -5 <= self._last_input_wall_now_ts - self._last_input_grid_ts <= s.max_sensor_age_seconds
                or grid > s.main_fuse_amps - s.buffer_amps):
            return None
        accepted = self.last_target[0]
        if (self.last_set_limit_ts is None or self._last_input_now_ts is None
                or not 0 <= self._last_input_now_ts - self.last_set_limit_ts < self.last_target[3]):
            return None  # an expired cloud lease is not an accepted live offer
        if not s.min_charge_amps <= accepted <= s.max_charge_amps:
            return None
        previous = max(accepted, self._uncertainty_previous_offer or accepted)
        if (self._reduction_previous_cap is not None
                and (self._awaiting_reduction_target == accepted or self._accepted_reduction_cap == accepted)):
            previous = max(previous, self._reduction_previous_cap)
        if current > min(s.max_charge_amps, previous) + 0.5:
            return None  # do not hide a reported current beyond every known offer
        raw_available = int(floor(min(s.max_charge_amps, s.main_fuse_amps - s.buffer_amps - grid)))
        target = min(accepted, raw_available, self._uncertainty_ceiling or accepted)
        if target < s.min_charge_amps:
            return None
        self._uncertainty_previous_offer = previous
        self._uncertainty_ceiling = target
        return target

    @property
    def startup_current(self) -> int:
        """Keep an older installation's configured minimum/maximum authoritative."""
        s = self.settings
        return max(s.min_charge_amps, min(s.startup_charge_amps, s.max_charge_amps))

    def _meaningfully_charging(self, status: str, current: float) -> bool:
        # Idle leakage (around 0.01 A on the audited charger) is not charging.
        # A vehicle can legitimately taper below the offered 6 A minimum;
        # distinguish that measured draw from leakage without demanding 6 A.
        return status == "charging" and current >= 1.0

    def _start_allowed(self, status: str) -> bool:
        return status in START_ALLOWED_STATUSES or (status == "paused" and self.paused_by_us)

    def _clear_positive_alignment(self) -> None:
        self._positive_cap: Optional[int] = None
        self._positive_house: Optional[float] = None
        self._positive_grid_ts: Optional[float] = None
        self._positive_ev_ts: Optional[float] = None
        self._positive_before_current: Optional[float] = None
        self._positive_response_ts: Optional[float] = None
        self._positive_anchor_response_ts: Optional[float] = None
        self._positive_anchor_hold_used = False
        self._positive_response_current: Optional[float] = None
        self._positive_skew_since: Optional[float] = None
        self._positive_alignment_used_seconds = 0.0
        self._positive_verified_low_current: Optional[float] = None
        self._positive_verified_low_ts: Optional[float] = None
        self._positive_grid_first_ts: Optional[float] = None
        self._positive_grid_first_closed = False
        self._positive_raw_recovery = False
        self._positive_raw_window_seconds: Optional[float] = None

    def _positive_reports_advanced(self) -> bool:
        return (self._last_input_grid_ts is not None and self._positive_grid_ts is not None
                and self._last_input_grid_ts > self._positive_grid_ts
                and self._last_input_charger_ts is not None and self._positive_ev_ts is not None
                and self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts"))

    def _normal_grid_first_hold(self, now: float, grid: float, current: float,
                                house: float, status: str) -> bool:
        """Keep an existing nominal offer while the first grid rise is mixed.

        This is bounded observation of a previously accepted offer, not proof
        that a grid rise came from the car. A new EV receipt after that first
        grid must be evaluated normally; it cannot reopen this exception.
        """
        def reject() -> bool:
            if self._positive_grid_first_ts is not None:
                self._positive_grid_first_closed = True
            return False

        if self._positive_grid_first_closed:
            return False
        if (self._positive_anchor_hold_used and self._positive_anchor_response_ts is not None
                and self._last_input_grid_ts is not None
                and self._last_input_grid_ts > self._positive_anchor_response_ts):
            return reject()
        cap, anchor = self._positive_cap, self._positive_house
        if (self._restart_required or not self.settings.enable_dynamic_limit
                or cap is None or anchor is None or self.last_target is None
                or self.last_target[0] != cap
                or not self.settings.min_charge_amps <= cap <= self.settings.max_charge_amps
                or not self._meaningfully_charging(status, current) or self.paused_by_us
                or self._awaiting_reduction_target is not None
                or self._overload_transition_started is not None or self._overload_hold_active
                or self._positive_skew_since is None or self._positive_response_ts is None
                or not self._fast_sources_fresh()
                or self._last_input_grid_ts is None or self._positive_grid_ts is None
                or self._last_input_charger_ts is None or self._positive_ev_ts is None
                or self._last_input_grid_ts <= self._positive_grid_ts
                or not self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts")
                or self._ev_regressed_since(self._positive_response_ts)
                or current > cap + 0.5 or current > grid + 1):
            return reject()
        budget = min(self.settings.measurement_settle_seconds, self.settings.max_sensor_age_seconds)
        if self._positive_alignment_used_seconds + max(0.0, now - self._positive_skew_since) >= budget:
            return reject()
        normal = self.settings.main_fuse_amps - self.settings.buffer_amps
        if (anchor + cap > normal or grid > normal or grid > anchor + cap + 0.5
                or house + cap <= normal):
            return reject()
        first_grid = self._positive_grid_first_ts
        if first_grid is not None and self._last_input_grid_ts < first_grid:
            return reject()
        if first_grid is not None and self._last_input_charger_ts > first_grid:
            return reject()
        if self._last_input_grid_ts <= self._last_input_charger_ts:
            return reject()
        if first_grid is None:
            self._positive_grid_first_ts = self._last_input_grid_ts
        return True

    def _blocked_overload_allows_nominal_reduction(self, now: float, grid: float,
                                                   current: float, house: float,
                                                   status: str) -> bool:
        """A blocked opt-in envelope need not prohibit a safe nominal decrease.

        Trust only fresh numeric sources and a known accepted offer. Keep the
        conservative reduction anchor, never grant a rise, and bound the whole
        normal shedding attempt without renewing time on reports or leases.
        """
        s = self.settings
        def reject(code: str) -> bool:
            self._nominal_rejection_code = code
            return False
        for blocked, code in (
            (self._restart_required, "restart_observation_required"),
            (not s.enable_dynamic_limit, "dynamic_limiting_disabled"),
            (self._overload_transition_started is not None, "experimental_transition_active"),
            (self.last_target is None, "no_accepted_limit"),
            (not self._meaningfully_charging(status, current), "not_meaningfully_charging"),
            (self.paused_by_us, "owned_pause_active"),
        ):
            if blocked:
                return reject(code)
        freshness = self._numeric_source_rejection()
        if freshness:
            return reject(freshness)
        if current > grid + 1:
            return reject("current_observations_inconsistent")
        if grid > s.main_fuse_amps + s.overload_margin_amps:
            return reject("raw_grid_above_post_response_ceiling")
        assert self.last_target is not None
        accepted = self.last_target[0]
        previous = (self._reduction_previous_cap if self._awaiting_reduction_target is not None
                    and self._reduction_previous_cap is not None else accepted)
        if not s.min_charge_amps <= accepted <= s.max_charge_amps:
            return reject("accepted_limit_outside_current_bounds")
        if current > min(previous, s.max_charge_amps) + 0.5:
            return reject("current_exceeds_known_offer")
        conservative_house = max(house, self._house_before_reduction or 0.0)
        nominal = int(floor(min(s.max_charge_amps, s.main_fuse_amps - s.buffer_amps - conservative_house)))
        observed_ev_first = (
            self._awaiting_reduction_target == accepted and self._house_before_reduction is not None
            and self._house_before_reduction + accepted <= s.main_fuse_amps - s.buffer_amps
            and self._reduction_before_current is not None and current < self._reduction_before_current - 0.5
            and self._reduction_charger_ts is not None
            and self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts")
            and self._last_input_grid_ts <= self._last_input_charger_ts
            and self._reduction_grid_ceiling is not None and grid <= self._reduction_grid_ceiling
            and self._last_limit_change_ts is not None
            and 0 <= now - self._last_limit_change_ts < min(s.measurement_settle_seconds, s.max_sensor_age_seconds))
        if not s.min_charge_amps <= nominal <= accepted and not observed_ev_first:
            return reject("no_buffered_nominal_reduction")
        budget = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
        if budget <= 0:
            return reject("measurement_alignment_disabled")
        if self._nominal_shedding_started is None:
            self._nominal_shedding_started = now
            self._nominal_shedding_budget = budget
        elif now - self._nominal_shedding_started >= min(self._nominal_shedding_budget or 0.0, budget):
            return reject("nominal_response_window_expired")
        self._nominal_overload_blocked = True
        return True

    def _clear_partial_reduction_alignment(self) -> None:
        self._reduction_before_current: Optional[float] = None
        self._accepted_reduction_cap: Optional[int] = None
        self._reduction_previous_cap: Optional[int] = None
        self._reduction_chain_origin: Optional[float] = None
        self._reduction_under_cap_baseline = False
        self._reduction_high_current: Optional[float] = None
        self._reduction_high_ev_ts: Optional[float] = None
        self._reduction_low_response_ts: Optional[float] = None
        self._reduction_guard_deadline: Optional[float] = None
        self._reduction_grid_ceiling: Optional[float] = None
        self._partial_reduction_current: Optional[float] = None
        self._partial_reduction_response_ts: Optional[float] = None
        self._partial_reduction_used_seconds = 0.0
        self._partial_reduction_budget: Optional[float] = None
        self._reduction_response_completed = False
        self._reduction_processing_deadline: Optional[float] = None
        self._reduction_processing_accounted: Optional[float] = None

    def _reduction_alignment_remaining(self, now: float) -> float:
        budget = min(self._partial_reduction_budget or 0.0,
                     self.settings.measurement_settle_seconds,
                     self.settings.max_sensor_age_seconds)
        elapsed = max(0.0, now - self._settle_started) if self._settle_started is not None else 0.0
        remaining = max(0.0, budget - self._partial_reduction_used_seconds - elapsed)
        if self._reduction_guard_deadline is not None:
            remaining = min(remaining, max(0.0, self._reduction_guard_deadline - now))
        return remaining

    def _begin_reduction_alignment(self, now: float, *, guarded: bool = False) -> None:
        remaining = self._reduction_alignment_remaining(now)
        if self._settle_started is None:
            self._settle_started = now
        if guarded and self._reduction_guard_deadline is None:
            # The late-receipt extension never gets a fresh wall-clock window
            # after apparently coherent feedback or an identical GET/lease.
            self._reduction_guard_deadline = now + remaining

    def _finish_reduction_alignment(self, now: float) -> None:
        if self._settle_started is not None:
            self._partial_reduction_used_seconds += max(0.0, now - self._settle_started)
        self._settle_started = None
        self._awaiting_reduction_target = None
        self._partial_reduction_current = None
        self._partial_reduction_response_ts = None
        self._reduction_response_completed = True
        # Keep the accepted-cap history and spent time until an actual changed
        # cap is accepted. A proposed increase or API failure proves nothing.

    def _restore_late_reduction(self, current: float, status: str) -> bool:
        """Restore only historical lower-cap protection, never a new budget."""
        cap = self._accepted_reduction_cap
        previous = self._reduction_previous_cap
        if (self._awaiting_reduction_target is not None or cap is None or previous is None
                or self.last_target is None or self.last_target[0] != cap
                or status != "charging" or current <= cap + 0.5):
            return False
        if current > previous + 0.5:
            return True
        if self._fast_sources_fresh():
            self._awaiting_reduction_target = cap
            self._reduction_low_response_ts = None
        return False

    def _observe_reduction_current(self, current: float) -> None:
        """A late receipt may still describe the preceding accepted offer.

        Keep the measured high-water mark separately from the low value used
        when the cap was submitted. A new receipt of that same low value is
        not evidence that a physical reduction happened.
        """
        if (self._awaiting_reduction_target is None or self._reduction_high_current is None
                or self._last_input_charger_ts is None or self._reduction_charger_ts is None
                or not self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts")
                or not self._fast_sources_fresh()):
            return
        if current > self._awaiting_reduction_target + 0.5:
            self._reduction_low_response_ts = None
        if self._reduction_previous_cap is not None and current > self._reduction_previous_cap + 0.5:
            return
        if current > self._reduction_high_current:
            self._reduction_high_current = current
            self._reduction_high_ev_ts = self._last_input_charger_ts

    def _reduction_response_after_ack(self, cap: int, stamp: Optional[float]) -> bool:
        """Keep the source-bound handoff's ACK floor across legacy paths.

        Its coherent house estimate is not physical lower-cap response. Both
        full and partial response consumers must reject a pre-ACK EV event;
        unrelated established reduction paths keep their existing contract.
        """
        if not self._ev_clock_credit:
            return False
        if (self._low_grid_pending and self._low_grid_phase == "reduction"
                and cap == self._low_grid_cap):
            return bool(stamp is not None and self._low_grid_ack_ev_floor is not None
                        and stamp > self._low_grid_ack_ev_floor
                        and self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts"))
        return (not self._ev_clock_explicit or self._ev_advanced_since(
            self._reduction_charger_ts, "_reduction_charger_ts"))

    def _reduction_low_is_response(self, cap: int, current: float, status: str) -> bool:
        if not self._reduction_response_after_ack(cap, self._last_input_charger_ts):
            return False
        if (self._low_grid_pending and self._low_grid_phase == "reduction"
                and cap == self._low_grid_cap
                and (self._last_input_grid_ts is None or self._last_input_charger_ts is None
                     or self._last_input_grid_ts <= self._last_input_charger_ts)):
            # The handoff owner requires the matching later grid too. Do not
            # let a legacy EV-first full-response path retire its shared
            # pending context between the EV event and that grid report.
            return False
        if not self._reduction_under_cap_baseline:
            return True  # Preserve the established measured-decrease path.
        stamp = self._last_input_charger_ts
        if (stamp is None or self._reduction_charger_ts is None
                or stamp <= self._reduction_charger_ts or not self._fast_sources_fresh()):
            return False
        if self._reduction_low_response_ts is not None:
            return current <= cap + 0.5
        reached_cap = (self._meaningfully_charging(status, current)
                       and cap - 0.5 <= current <= cap + 0.5)
        fell_from_observed_high = (
            self._reduction_high_current is not None
            and self._reduction_high_current > cap + 0.5
            and self._reduction_high_ev_ts is not None and stamp > self._reduction_high_ev_ts
            and current < self._reduction_high_current - 0.5
        )
        if reached_cap or fell_from_observed_high:
            self._reduction_low_response_ts = stamp
            return True
        return False

    def _late_high_reduction_can_wait(self, grid: float, current: float, status: str) -> bool:
        """Only hold the accepted lower cap without subtracting uncertain EV.

        This narrower extension requires the full normal buffer. The legacy
        unchanged-EV path keeps its existing minimum-reserve contract.
        """
        cap = self._awaiting_reduction_target
        previous = self._reduction_previous_cap
        before = self._reduction_before_current
        return bool(
            status == "charging" and cap is not None and cap > 0
            and self.settings.min_charge_amps <= cap <= self.settings.max_charge_amps
            and self.last_target is not None and self.last_target[0] == cap
            and previous is not None and previous > cap and before is not None
            and cap + 0.5 < current <= previous + 0.5
            and self._reduction_grid_ceiling is not None and grid < self._reduction_grid_ceiling
            and self._last_input_grid_ts is not None and self._reduction_grid_ts is not None
            and self._last_input_grid_ts > self._reduction_grid_ts
            and self._last_input_charger_ts is not None and self._reduction_charger_ts is not None
            and self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts")
            and self._fast_sources_fresh()
            and grid + cap <= self.settings.main_fuse_amps - self.settings.buffer_amps
        )

    def _partial_reduction_alignment(self, now: float, grid: float,
                                     current: float, status: str) -> Optional[str]:
        """Recognize measured partial response before subtracting mixed data.

        An accepted cap alone starts no grace. A distinct physical EV decrease
        must be reported first, and a later grid report must confirm it. All
        partial episodes of this accepted cap share one nonrenewable budget.
        Receipt order is evidence only; a fresh higher grid value always wins.
        """
        cap = self._awaiting_reduction_target
        before = self._reduction_before_current
        grid_ts = self._last_input_grid_ts
        ev_ts = self._last_input_charger_ts
        if (cap is None or cap <= 0 or self.last_target is None or self.last_target[0] != cap
                or before is None or self._reduction_grid_ceiling is None
                or self._house_before_reduction is None or self._partial_reduction_budget is None
                or self._reduction_grid_ts is None or self._reduction_charger_ts is None
                or grid_ts is None or ev_ts is None
                or grid_ts < self._reduction_grid_ts or self._ev_regressed_since(self._reduction_charger_ts)
                or status != "charging"):
            return None
        if not self._reduction_response_after_ack(cap, ev_ts):
            return None
        # Do not replace the established full-response/grid-first paths unless
        # a partial response has actually been seen for this accepted cap.
        partial_seen = self._partial_reduction_current is not None
        if not partial_seen and current <= cap + 0.5:
            return None
        reserve = min(self.settings.buffer_amps, max(0.0, self.settings.minimum_reserve_amps))
        margin = reserve if cap == self.settings.min_charge_amps else self.settings.buffer_amps
        if (grid > self._reduction_grid_ceiling
                or self._house_before_reduction + cap > self.settings.main_fuse_amps - margin):
            return None
        low = self._partial_reduction_current if partial_seen else max(before, self._reduction_high_current or 0.0)
        completed_partial = (partial_seen and low > cap + 0.5
                             and current <= cap + 0.5 and current < low)
        if self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts") and (current < low - 0.5 or completed_partial):
            self._partial_reduction_current = current
            self._partial_reduction_response_ts = ev_ts
        response = self._partial_reduction_response_ts
        if response is None:
            return None
        # A reversed response must be evaluated normally, not hidden by the
        # earlier decrease. Its low-water mark cannot be reset by a renewal.
        if current > self._partial_reduction_current + 0.5:
            return None
        elapsed = now - self._settle_started if self._settle_started is not None else 0.0
        if self._reduction_alignment_remaining(now) <= 0:
            return "timeout"
        if grid_ts > response and current <= grid + 1:
            self._partial_reduction_used_seconds += elapsed
            self._settle_started = None
            self._partial_reduction_response_ts = None
            self._reduction_grid_ceiling = min(self._reduction_grid_ceiling, grid)
            return None
        if self._settle_started is None:
            self._begin_reduction_alignment(now, guarded=self._reduction_under_cap_baseline)
        return "hold"

    def _fast_sources_fresh(self) -> bool:
        """Require actual UTC receipt ages; a stream heartbeat is insufficient."""
        return (self._numeric_source_rejection() is None and self._ev_clock_credit
                and self._event_source_usable())

    def _numeric_source_rejection(self) -> Optional[str]:
        """Describe existing numeric freshness checks without weakening them."""
        if not self._last_input_current_trusted:
            return "charger_current_untrusted"
        wall = self._last_input_wall_now_ts
        grid = self._last_input_grid_ts
        ev = self._last_input_charger_ts
        if wall is None:
            return "wall_clock_missing_or_invalid"
        if grid is None:
            return "grid_receipt_missing_or_invalid"
        if ev is None:
            return "charger_receipt_missing_or_invalid"
        age_limit = self.settings.max_sensor_age_seconds
        if _valid_number(age_limit) is None:
            return "source_age_limit_invalid"
        if _valid_number(self.settings.measurement_settle_seconds) is None:
            return "source_alignment_window_invalid"
        if wall - grid < -5:
            return "grid_receipt_in_future"
        if not -5 <= wall - grid <= age_limit:
            return "grid_receipt_stale"
        if wall - ev < -5:
            return "charger_receipt_in_future"
        if not -5 <= wall - ev <= age_limit:
            return "charger_receipt_stale"
        if not abs(grid - ev) <= self.settings.measurement_settle_seconds:
            return "report_skew_exceeds_alignment_window"
        return None

    def _raw_zero_restart_allowed(self, grid_current: float, status: str, current: float) -> bool:
        """Observe raw headroom without claiming physical zero confirmation.

        A small residual can outlive an accepted zero indefinitely. No EV
        subtraction is needed here, so distinct receipt ordering is immaterial;
        both sources must have fresh absolute receipt times before offering
        positive current. An aged EV fallback can preserve only an observation
        that already began with fresh idle evidence; it cannot begin one.
        """
        if self._raw_clock_zero_observation(grid_current, status, current):
            return True
        wall, grid, ev = (self._last_input_wall_now_ts, self._last_input_grid_ts,
                          self._last_input_charger_ts)
        common = bool(
            self.settings.enable_dynamic_limit and self._restart_required
            and self._awaiting_reduction_target == 0
            and self.last_target is not None and self.last_target[0] == 0
            and status in START_ALLOWED_STATUSES and not self.paused_by_us
            and wall is not None and grid is not None and ev is not None
            and -5 <= wall - grid <= self.settings.max_sensor_age_seconds
            and wall - ev >= -5
            and (self._reduction_grid_ts is None or grid >= self._reduction_grid_ts)
            and grid_current + self.startup_current <= self.settings.main_fuse_amps - self.settings.buffer_amps
        )
        if not common:
            return False
        assert wall is not None and ev is not None
        if wall - ev > self.settings.max_sensor_age_seconds:
            return bool(self._restart_ready_since is not None and self._raw_zero_restart_cap is not None
                        and self.settings.min_charge_amps <= self._raw_zero_restart_cap <= self.settings.max_charge_amps)
        return bool(current > 0.5 and not self._meaningfully_charging(status, current)
                    and (self._reduction_charger_ts is None or not self._ev_regressed_since(self._reduction_charger_ts)))

    def _raw_clock_zero_observation(self, grid: float, status: str, current: float) -> bool:
        """Observe raw restart capacity without inventing a zero EV report.

        The receiver is active but may have no winning-conductor event yet.
        Only our already submitted zero and the existing continuous raw-grid
        restart gate qualify; the controller still orders native zero readback.
        """
        s = self.settings
        return bool(s.nominal_import_only_fallback and s.enable_dynamic_limit
            and self._ev_clock_explicit and self._ev_clock_error is None
            and self._ev_current_sequences and self._last_input_current_trusted
            and self._restart_required and not self.paused_by_us
            and status in {"charging", "start_charging", "awaiting_start", "ready_to_charge"}
            and 0 <= current <= .5 and self.last_target is not None and self.last_target[0] == 0
            and self._awaiting_reduction_target == 0
            and self._last_input_control_source_id is not None
            and self._last_input_control_source_id == self._accepted_control_source_id
            and not self._uncertainty_retry_submission_pending and self._uncertainty_stops < 3
            and self._last_input_wall_now_ts is not None and self._last_input_grid_ts is not None
            and 0 <= self._last_input_wall_now_ts-self._last_input_grid_ts <= s.max_sensor_age_seconds
            and grid+self.startup_current <= s.main_fuse_amps-s.buffer_amps)

    def _rebase_positive_house(self, now: float, grid: float, house: float, current: float, status: str,
                               snapshot_verified: bool) -> bool:
        """Refresh the house estimate without forgetting a possible late rise.

        A cloud GET confirms a source snapshot, not synchronized physical
        sampling. Require a later valid grid receipt and retain the accepted
        cap, original EV baseline and all already spent alignment time.
        """
        if (snapshot_verified is not True or not self._fast_sources_fresh()
                or self._positive_cap is None or self._awaiting_reduction_target is not None
                or self._last_input_grid_ts <= self._last_input_charger_ts
                or current > self._positive_cap + 0.5 or current > grid + 1
                or not self._start_allowed(status)):
            return False
        if house + self._positive_cap > self.settings.main_fuse_amps - self.settings.buffer_amps:
            return False
        if self._positive_skew_since is not None or self._positive_response_ts is not None:
            elapsed = (now - self._positive_skew_since
                       if self._positive_skew_since is not None else 0.0)
            settle = min(self.settings.measurement_settle_seconds, self.settings.max_sensor_age_seconds)
            # An expired partial episode can be confirmed by a NEW cloud read
            # and a later coherent grid receipt. Spend all elapsed uncertainty;
            # this neither refunds time nor grants another alignment window.
            if (self._positive_response_ts is None
                    or not self._ev_advanced_since(self._positive_response_ts, "_positive_response_ts")
                    or self._positive_alignment_used_seconds + elapsed < settle):
                return False
            self._positive_alignment_used_seconds += elapsed
            self._positive_skew_since = None
            self._positive_response_ts = None
            self._positive_anchor_response_ts = None
            self._positive_anchor_hold_used = False
        self._positive_house = house
        reference = (self._positive_response_current if self._positive_response_current is not None
                     else self._positive_verified_low_current if self._positive_verified_low_current is not None
                     else self._positive_before_current)
        if reference is not None and current < reference - 0.5:
            self._positive_verified_low_current = current
            self._positive_verified_low_ts = self._last_input_charger_ts
            self._pin_ev_owner("_positive_verified_low_ts", wall_floor=(
                self._ev_owner_clocks.get("_positive_ev_ts", {}).get("wall_floor")))
        return True

    def _positive_response_owns_uncertainty(
        self, now: float, grid: float, current: float, status: str,
        previous_grid_ts: Optional[float], previous_charger_ts: Optional[float],
    ) -> bool:
        """Route one accepted EV-first rise to its existing response owner.

        This grants neither a new offer nor another timer. The original
        positive-alignment handler still decides hold, expiry and completion
        AFTER the shared source, overload and custom guards. In particular an
        exhausted episode must reach that handler, not open a new raw-grid
        alignment window. A proposed cap, TTL renewal or receipt is not proof
        of physical response.
        """
        s = self.settings
        cap, anchor = self._positive_cap, self._positive_house
        wall, gt, et = (self._last_input_wall_now_ts, self._last_input_grid_ts,
                        self._last_input_charger_ts)
        identity = self._last_input_control_source_id
        if (not s.enable_dynamic_limit or self._restart_required or self.paused_by_us
                or not self._ev_clock_credit
                or status != "charging" or cap is None or anchor is None
                or self.last_target is None or self.last_target[0] != cap
                or not s.min_charge_amps <= cap <= s.max_charge_amps
                or self.last_set_limit_ts is None
                or not 0 <= now - self.last_set_limit_ts < self.last_target[3]
                or identity is None or identity != self._accepted_control_source_id
                or self._awaiting_reduction_target is not None or self._low_grid_pending
                or self._uncertainty_ceiling is not None
                or self._overload_transition_started is not None or self._overload_hold_active
                or (self._normal_shed_deadline is not None and self._normal_shed_failure is None)):
            return False
        normal = s.main_fuse_amps - s.buffer_amps
        whole_accepted_fits = _valid_number(normal) is not None and grid + cap <= normal
        source_rejection = self._numeric_source_rejection()
        if (source_rejection is not None and not (whole_accepted_fits and source_rejection in {
                "charger_receipt_stale", "report_skew_exceeds_alignment_window"})):
            return False
        assert wall is not None and gt is not None and et is not None
        reference = (self._positive_verified_low_current if self._positive_verified_low_current is not None
                     else self._positive_before_current)
        budget = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
        spent = self._positive_alignment_used_seconds
        if (not 0 <= wall - gt <= s.max_sensor_age_seconds
                or wall < et or (not whole_accepted_fits and wall - et > s.max_sensor_age_seconds)
                or self._positive_grid_ts is None or gt < self._positive_grid_ts
                or not self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts")
                or (previous_grid_ts is not None and gt < previous_grid_ts)
                or self._ev_regressed_since(previous_charger_ts)
                or self._ev_regressed_since(self._positive_response_ts)
                or reference is None or current <= reference + 0.5
                or current <= grid + 1 or current > cap + 0.5
                or _valid_number(normal) is None or _valid_number(anchor) is None
                or anchor < 0 or grid > normal or anchor + cap > normal
                or _valid_number(budget) is None or budget <= 0
                or _valid_number(spent) is None or spent < 0
                or (self._positive_skew_since is not None
                    and (not isfinite(self._positive_skew_since)
                         or self._positive_skew_since > now))):
            return False
        return True

    def _positive_alignment(self, now: float, grid: float, current: float,
                            house: float, status: str) -> Optional[str]:
        """Align an observed response to our accepted positive cap, once only.

        Receipt times are ordering evidence, not synchronized acquisition times.
        A delayed EV response may start this short window long after submission.
        Identical lease renewals never create a new window or move its deadline.
        The caller must handle invalid data and experimental overload first.
        """
        cap = self._positive_cap
        if cap is None:
            return None
        s = self.settings
        # Resume acceptance relinquishes pause ownership immediately. A short
        # unchanged paused-feedback interval may preserve only this already
        # accepted cap's measurement context, never permission to resume again.
        pending_resume_feedback = (status == "paused" and self._resume_submission_ts is not None
                                   and 0 <= now - self._resume_submission_ts <= 30)
        if (self.last_target is None or self.last_target[0] != cap
                or not (self._start_allowed(status) or pending_resume_feedback)):
            self._clear_positive_alignment()
            return None
        if current > cap + 0.5:
            self._clear_positive_alignment()
            return "excess_current"
        # Never hide a fresh high grid reading behind a prior low-load anchor.
        if grid > s.main_fuse_amps - s.buffer_amps:
            self._clear_positive_alignment()
            return None
        anchor = self._positive_house
        if anchor is None or anchor + cap > s.main_fuse_amps - s.buffer_amps:
            self._clear_positive_alignment()
            return None
        ev_advanced = self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts")
        reference = (self._positive_verified_low_current if self._positive_verified_low_current is not None
                     else self._positive_before_current or 0.0)
        increased = current > reference + 0.5
        fresh_low_response = (self._positive_verified_low_ts is not None
                              and self._last_input_charger_ts is not None
                              and self._ev_advanced_since(self._positive_verified_low_ts, "_positive_verified_low_ts"))
        if (ev_advanced and increased and (self._positive_response_current is None
                or current > self._positive_response_current + 0.5 or fresh_low_response)):
            if self._positive_response_ts is None:
                self._positive_anchor_response_ts = self._last_input_charger_ts
                self._positive_anchor_hold_used = False
            self._positive_response_ts = self._last_input_charger_ts
            self._pin_ev_owner("_positive_response_ts", wall_floor=(
                self._ev_owner_clocks.get("_positive_ev_ts", {}).get("wall_floor")))
            self._positive_response_current = current
            self._positive_verified_low_ts = None
        if self._positive_response_ts is None:
            # Before the observed rise, a newer household load may reduce the
            # safe target normally. It must never be subtracted away later.
            self._positive_house = max(anchor, house)
            return None
        settle = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
        active_elapsed = (now - self._positive_skew_since
                          if self._positive_skew_since is not None else 0.0)
        if self._positive_alignment_used_seconds + active_elapsed >= settle:
            # An elapsed alignment window does not mean the installation is
            # overloaded. Retain ONLY the already accepted cap when even the
            # worst case (the entire grid reading is household load, with no
            # EV subtraction) fits inside the normal buffered fuse budget.
            # This grants no new alignment time, increase, resume or overload.
            valid_receipts = (self._last_input_grid_ts is not None and self._positive_grid_ts is not None
                              and self._last_input_grid_ts >= self._positive_grid_ts
                              and self._last_input_charger_ts is not None
                              and not self._ev_regressed_since(self._positive_ev_ts)
                              and not self._ev_regressed_since(self._positive_response_ts))
            if valid_receipts and grid + cap <= s.main_fuse_amps - s.buffer_amps:
                grid_after_response = self._last_input_grid_ts > self._positive_response_ts
                if (grid_after_response and current <= grid + 1 and current >= cap - 0.5
                        and self._meaningfully_charging(status, current)):
                    self._clear_positive_alignment()
                    self._increase_since = None
                    return None
                return "safe_hold"
            self._clear_positive_alignment()
            return "timeout"
        grid_after_response = (self._last_input_grid_ts is not None
                               and self._last_input_grid_ts > self._positive_response_ts)
        if grid_after_response and current <= grid + 1:
            if current >= cap - 0.5 and self._meaningfully_charging(status, current):
                self._clear_positive_alignment()
                self._increase_since = None
            else:
                # Stop this coherent partial-response episode's clock, but
                # retain its spent budget and the EV response high-water mark.
                # Only a distinct further EV rise can open another episode;
                # identical reports and lease renewals refund no uncertainty.
                self._positive_alignment_used_seconds += active_elapsed
                self._positive_skew_since = None
                self._positive_response_ts = None
                self._positive_anchor_response_ts = None
                self._positive_anchor_hold_used = False
            # A coherent partial/tapered current is not a failed start and
            # must not cause timed zero/start retries. Retain eligibility for
            # the rest of this same accepted rise, without renewing any grace.
            return None
        if self._positive_skew_since is None:
            self._positive_skew_since = now
        return "hold"

    def _positive_ev_first_anchor_hold(self, now: float, grid: float, current: float,
                                       status: str, previous_grid_ts: Optional[float],
                                       previous_charger_ts: Optional[float]) -> bool:
        """Retain an existing rise's coherent house bound for one mixed pair.

        A partial EV response can be published just after the grid observation.
        Subtracting it from that earlier grid must not manufacture new house
        load. Keep only the accepted cap inside its ORIGINAL response budget;
        a later grid must return to normal load estimation and limiting.
        """
        s = self.settings
        cap, anchor = self._positive_cap, self._positive_house
        gt, et, wall = self._last_input_grid_ts, self._last_input_charger_ts, self._last_input_wall_now_ts
        identity = self._last_input_control_source_id
        if (not s.nominal_import_only_fallback or not s.enable_dynamic_limit
                or self._restart_required or self.paused_by_us
                or not self._meaningfully_charging(status, current)
                or cap is None or anchor is None or self.last_target is None
                or self.last_target[0] != cap or not s.min_charge_amps <= cap <= s.max_charge_amps
                or self.last_set_limit_ts is None or not 0 <= now-self.last_set_limit_ts < self.last_target[3]
                or identity is None or identity != self._accepted_control_source_id
                or self._awaiting_reduction_target is not None or self._low_grid_pending
                or self._uncertainty_ceiling is not None
                or self._overload_transition_started is not None or self._overload_hold_active
                or self._normal_shed_deadline is not None
                or self._numeric_source_rejection() is not None or not self._ev_clock_credit
                or wall is None or gt is None or et is None or et > wall or gt > wall
                or gt is None or et is None or self._positive_grid_ts is None
                or gt < self._positive_grid_ts or not self._ev_advanced_since(self._positive_ev_ts, "_positive_ev_ts")
                or previous_grid_ts is not None and gt < previous_grid_ts
                or self._ev_regressed_since(previous_charger_ts)
                or self._positive_response_ts is None or not gt <= self._positive_response_ts <= et
                or self._positive_anchor_response_ts is None or gt > self._positive_anchor_response_ts
                or self._positive_response_current is None or current < self._positive_response_current-.5
                or self._positive_before_current is None or current <= self._positive_before_current+.5
                or current > cap+.5 or self._positive_skew_since is None
                or not 0 <= now-self._positive_skew_since
                or self._positive_alignment_used_seconds + now-self._positive_skew_since
                    >= min(s.measurement_settle_seconds, s.max_sensor_age_seconds)):
            return False
        normal = s.main_fuse_amps-s.buffer_amps
        return bool(0 <= anchor and anchor+cap <= normal and grid <= normal
                    and grid <= anchor+cap+.5)

    def reset_overload_budget(self) -> None:
        """Explicit new exposure state; never call for settings or HA reloads.

        This operational counter does not estimate fuse cooling. Its caller
        must not treat an explicit reset as confirmation of a cold fuse.
        """
        if getattr(self, "_automatic_cycle", None) is not None:
            # Only a complete observed automatic cooling run replenishes it.
            return
        if getattr(self, "_trial_epoch", None) is not None:
            self._terminate_trial("interrupted", "budget_reset_attempt")
            return
        self._trial_epoch: Optional[dict] = None
        self._trial_activation_committed = False
        self._trial_last_grid_ts: Optional[float] = None
        self._trial_last_ev_ts: Optional[float] = None
        self._overload_used_seconds = 0.0
        self._overload_last_eval_ts: Optional[float] = None
        self._overload_exposure_active = False
        self._overload_failure_reason: Optional[str] = None
        self._overload_hold_active = False
        self._clear_overload_transition()

    def _clear_overload_transition(self) -> None:
        self._clear_minimum_confirmation("overload")
        self._overload_transition_started: Optional[float] = None
        self._overload_entry_grid_ts: Optional[float] = None
        self._overload_entry_charger_ts: Optional[float] = None
        self._overload_anchor_house: Optional[float] = None
        self._overload_minimum_response_ts: Optional[float] = None
        self._overload_minimum_response_monotonic: Optional[float] = None
        self._overload_grid_confirmation_deadline: Optional[float] = None
        self._overload_grid_confirmation_floor: Optional[float] = None
        self._overload_grid_confirmation_value: Optional[float] = None
        self._overload_grid_confirmation_last_ev_ts: Optional[float] = None
        self._overload_grid_confirmation_last_grid_ts: Optional[float] = None
        self._overload_grid_confirmation_source_id = None
        self._overload_minimum_accepted_at: Optional[float] = None
        self._overload_minimum_ev_floor: Optional[float] = None
        self._overload_minimum_attempted_at: Optional[float] = None

    def _fail_overload(self, reason: str) -> None:
        if self.settings.automatic_overload_cycles:
            cycle = self._ensure_automatic_cycle()
            cycle["state"] = "blocked"
            cycle["failure_reason"] = cycle["failure_reason"] or reason
            self._break_automatic_continuity()
        elif self._trial_epoch is not None:
            self._terminate_trial("failed", reason)
        else:
            self._overload_failure_reason = self._overload_failure_reason or reason

    def export_overload_state(self) -> dict[str, object]:
        """JSON-safe exposure state; the controller adds its UTC save time."""
        return {
            "version": 1,
            "used_seconds": self._overload_used_seconds,
            "exposure_active": self._overload_exposure_active,
            "transition_pending": self._overload_transition_started is not None,
            "minimum_held": self._overload_hold_active,
            "failure_reason": self._overload_failure_reason,
            **({"automatic_cycle": deepcopy(self._automatic_cycle)}
               if self._automatic_cycle is not None else {}),
            **({"peak_response_pending": True,
                "peak_response_source_id": self._peak_response.source_id,
                "peak_response_previous_authority": self._peak_response.previous_authority,
                "peak_response_grid_ts": self._peak_response.grid_source_ts,
                "peak_response_failure": self._peak_response_failure}
               if self._peak_response is not None else {}),
            **({"trial_epoch": deepcopy(self._trial_epoch)} if self._trial_epoch is not None else {}),
            "normal_control": {
                "version": 1,
                "response_pending": (self._normal_shed_deadline is not None and self._normal_shed_failure is None)
                    or self._ordinary_lower is not None,
                **({"alignment_pending": True} if self._low_grid_pending else {}),
                "cap": self._uncertainty_ceiling,
                "previous_offer": self._uncertainty_previous_offer,
                "stops": self._uncertainty_stops,
                "stop_recorded": self._uncertainty_stop_recorded,
                "restart": self._uncertainty_restart,
                "retry_wait_seconds": min(600, max(self._restored_normal_wait,
                    max(0.0, self._uncertainty_retry_after - (self._last_input_now_ts or 0.0))
                    if self._uncertainty_retry_after is not None else 0.0)),
                "response_failure": self._normal_shed_failure,
                "retry_submission_pending": self._uncertainty_retry_submission_pending,
            },
        }

    def _import_trial_epoch(self, value: object) -> bool:
        # An invalid additive epoch must block only its new authority; retain
        # the separately validated lifetime exposure and historical failure.
        fallback_policy = dict(main_fuse_amps=25.0, minimum_current=7, ceiling_amps=29.0,
            budget_seconds=1800, transition_limit_amps=38.0, response_seconds=10,
            settings=asdict(self.settings))
        self._trial_epoch = dict(version=1, trial_id="invalid-persisted-trial",
            started_at_utc="1970-01-01T00:00:00+00:00", source_id="invalid",
            policy=fallback_policy, baseline_used_seconds=self._overload_used_seconds,
            state="failed", failure_reason="invalid_persisted_trial", closed_used_seconds=0.0)
        try:
            if not isinstance(value, dict):
                return False
            epoch = deepcopy(value)
            baseline = epoch.get("baseline_used_seconds")
            closed = epoch.get("closed_used_seconds")
            state = epoch.get("state")
            identity = self._trial_identity(epoch.get("source_id"))
            started = self._trial_started_timestamp(epoch.get("started_at_utc"))
            policy = epoch.get("policy")
            validated = OneFuseRegulator(RegulatorSettings(**policy["settings"]))
            # Add only the disabled value to a comparison copy. Preserve the
            # original frozen policy and every historical exposure field.
            comparable_policy = deepcopy(policy)
            comparable_policy["settings"].setdefault("grid_confirmation_seconds", 0)
            comparable_policy["settings"].setdefault("peak_only_response", False)
            comparable_policy["settings"].setdefault("automatic_overload_cycles", False)
            valid = bool(type(epoch.get("version")) is int and epoch["version"] == 1
                and isinstance(epoch.get("trial_id"), str) and epoch["trial_id"].strip()
                and len(epoch["trial_id"]) <= 128 and identity is not None
                and started <= datetime.now(timezone.utc).timestamp() + 5
                and not isinstance(baseline, bool) and _valid_number(baseline) is not None
                and 0 <= baseline <= self._overload_used_seconds
                and comparable_policy == self._trial_policy(validated.settings)
                and state in {"pending", "active", "ended", "failed", "interrupted"}
                and (state in {"pending", "active"} and epoch.get("failure_reason") is None and closed is None
                     or state in {"ended", "failed", "interrupted"}
                     and isinstance(epoch.get("failure_reason"), str) and bool(epoch["failure_reason"])
                     and not isinstance(closed, bool) and _valid_number(closed) is not None
                     and 0 <= closed <= self._overload_used_seconds - baseline + 1e-6))
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            return False
        if not valid:
            return False
        self._trial_epoch = epoch
        self._terminate_trial("interrupted", "interrupted_after_restore")
        return True

    def import_overload_state(self, state: object, elapsed_seconds: float = 0.0) -> bool:
        """Restore exposure, charging unknown downtime to a preceding overload.

        Monotonic transition deadlines cannot survive a process restart. A
        pending response is therefore latched as interrupted, never renewed.
        Invalid persisted state fails closed instead of refunding exposure.
        """
        previous_trial = deepcopy(self._trial_epoch)
        peak_valid = self._restore_peak_response(state)
        previous_used, previous_failure = self._overload_used_seconds, self._overload_failure_reason
        used = _valid_number(state.get("used_seconds")) if isinstance(state, dict) else None
        gap = _valid_number(elapsed_seconds)
        valid = (isinstance(state, dict) and state.get("version") == 1
                 and used is not None and used >= 0 and not isinstance(state.get("used_seconds"), bool)
                 and type(state.get("exposure_active")) is bool
                 and type(state.get("transition_pending")) is bool
                 and type(state.get("minimum_held")) is bool
                 and (state.get("failure_reason") is None or isinstance(state.get("failure_reason"), str))
                 and gap is not None and gap >= 0 and not isinstance(elapsed_seconds, bool))
        self._trial_epoch = None
        self._trial_activation_committed = False
        self._trial_last_grid_ts = self._trial_last_ev_ts = None
        self._clear_overload_transition()
        self._overload_hold_active = False
        self._overload_last_eval_ts = None
        if not valid:
            if previous_trial is not None:
                self._overload_used_seconds = max(previous_used, float(self.settings.overload_budget_seconds))
                self._overload_failure_reason = previous_failure
                self._trial_epoch = previous_trial
                self._terminate_trial("interrupted", "invalid_persisted_exposure")
                return False
            self._overload_used_seconds = float(self.settings.overload_budget_seconds)
            self._overload_exposure_active = True
            self._overload_failure_reason = "invalid_persisted_exposure"
            if self.settings.automatic_overload_cycles or self._automatic_cycle is not None:
                self._restore_automatic_cycle({"automatic_cycle": {}}, 0., False)
            return False
        assert isinstance(state, dict) and used is not None and gap is not None
        self._overload_used_seconds = used + (gap if state["exposure_active"] else 0.0)
        if previous_trial is not None:
            self._overload_used_seconds = max(previous_used, self._overload_used_seconds)
        self._overload_exposure_active = state["exposure_active"]
        self._overload_hold_active = state["minimum_held"]
        self._overload_failure_reason = state.get("failure_reason")
        trial_valid = self._import_trial_epoch(state["trial_epoch"]) if "trial_epoch" in state else True
        if previous_trial is not None:
            self._overload_failure_reason = previous_failure or self._overload_failure_reason
            if ("trial_epoch" not in state or not trial_valid
                    or self._trial_epoch["trial_id"] != previous_trial["trial_id"]
                    or self._trial_epoch["baseline_used_seconds"] != previous_trial["baseline_used_seconds"]):
                self._trial_epoch = previous_trial
                self._terminate_trial("interrupted", "interrupted_by_import")
                trial_valid = False
        automatic_valid = self._restore_automatic_cycle(state, gap, state["exposure_active"])
        if state["transition_pending"]:
            self._fail_overload("interrupted_transition")
        normal = state.get("normal_control")
        if normal is not None:
            wait = _valid_number(normal.get("retry_wait_seconds")) if isinstance(normal, dict) else None
            valid_normal = bool(isinstance(normal, dict) and normal.get("version") == 1
                and type(normal.get("response_pending")) is bool
                and type(normal.get("alignment_pending", False)) is bool
                and type(normal.get("stops")) is int and 0 <= normal["stops"] <= 3
                and type(normal.get("stop_recorded")) is bool and type(normal.get("restart")) is bool
                and type(normal.get("retry_submission_pending", False)) is bool
                and (normal.get("cap") is None or type(normal["cap"]) is int and 6 <= normal["cap"] <= 32)
                and (normal.get("previous_offer") is None or type(normal["previous_offer"]) is int
                     and 6 <= normal["previous_offer"] <= 32)
                and wait is not None and not isinstance(normal.get("retry_wait_seconds"), bool) and 0 <= wait <= 600
                and (normal.get("response_failure") is None or isinstance(normal["response_failure"], str))
                and (not (normal["response_pending"] or normal.get("alignment_pending", False)
                          or normal["stops"] or normal["restart"])
                     or normal.get("cap") is not None)
                and (not normal.get("retry_submission_pending", False)
                     or normal["restart"] and normal["stops"] >= 1 and not normal["response_pending"]
                     and not normal.get("alignment_pending", False)))
            self._clear_bounded_nominal_shedding()
            self._clear_low_grid_alignment()
            if not valid_normal:
                self._uncertainty_ceiling = self.startup_current
                self._uncertainty_stops = 3
                self._uncertainty_restart = self._uncertainty_stop_recorded = True
                self._normal_shed_failure = "invalid_persisted_normal_control"
                self._restart_required = True
                return False
            self._uncertainty_ceiling = normal["cap"]
            self._uncertainty_previous_offer = normal["previous_offer"]
            self._uncertainty_stops = normal["stops"]
            self._uncertainty_stop_recorded = normal["stop_recorded"]
            self._uncertainty_restart = normal["restart"]
            self._normal_shed_failure = normal["response_failure"]
            self._restored_normal_wait = wait
            self._uncertainty_retry_submission_pending = False
            if normal["response_pending"] or normal.get("alignment_pending", False) or normal.get("retry_submission_pending", False):
                self._normal_shed_failure = ("interrupted_positive_retry" if normal.get("retry_submission_pending", False)
                                             else "interrupted_low_grid_alignment" if normal.get("alignment_pending", False)
                                             else "interrupted_normal_response")
                if normal.get("retry_submission_pending", False) or not self._uncertainty_stop_recorded:
                    self._uncertainty_stops = min(3, self._uncertainty_stops + 1)
                self._uncertainty_stop_recorded = True
                self._uncertainty_restart = True
                self._restored_normal_wait = max(wait, min(600,
                    self.settings.restart_stable_seconds * 2 ** min(self._uncertainty_stops - 1, 3)))
            if self._uncertainty_ceiling is not None:
                self._restart_required = True
        if self._peak_response is not None and self._peak_response.interrupted:
            self._interrupt_peak_response(self._peak_response_failure or "interrupted_peak_response_after_restore")
        return trial_valid and peak_valid and automatic_valid

    def _observe_overload_time(self, now: Optional[float], grid: Optional[float], valid: bool) -> bool:
        """Charge left-held observed overload intervals, including transitions.

        Missing/invalid data does not turn the previous known overload into a
        cooling credit. Disabling control does not refund or freeze exposure.
        """
        if now is None or (self._overload_last_eval_ts is not None and now < self._overload_last_eval_ts):
            self._fail_overload("invalid_or_backward_clock")
            return False
        if self._overload_last_eval_ts is not None and self._overload_exposure_active:
            delta = now-self._overload_last_eval_ts
            self._overload_used_seconds += delta
            if self._automatic_cycle is not None:
                self._automatic_cycle["used_seconds"] += delta
        self._overload_last_eval_ts = now
        if valid and grid is not None and grid >= 0:
            if self.settings.overload_enabled or self._overload_exposure_active or self._overload_used_seconds > 0:
                self._overload_exposure_active = grid > self.settings.main_fuse_amps
        return True

    def _overload_plan(self, now: float, grid: float, current: float, house: float,
                       status: str) -> Optional[tuple[str, str, int, float]]:
        """Return an opt-in minimum-current plan before ordinary headroom logic.

        The post-response ceiling and the bounded shedding transition are
        separate envelopes. Accepted API commands never count as an observed
        EV response, and refreshing reports or leases cannot renew a deadline.
        """
        s = self.settings
        if not s.overload_enabled:
            return None
        peak_policy = s.peak_only_response
        peak_deadline = self.peak_response_deadline(now) if peak_policy else None
        minimum = self.overload_minimum_current
        ceiling = s.main_fuse_amps + s.overload_margin_amps
        reserve = min(s.buffer_amps, max(0.0, s.minimum_reserve_amps))
        peak_needed = peak_policy and self.peak_response_active and grid > PEAK_RESPONSE_TRIGGER_AMPS
        needed = house + minimum > s.main_fuse_amps - reserve or grid > ceiling or peak_needed
        if self._trial_epoch is not None and self._trial_activation_committed:
            # Keep the explicitly approved 7 A envelope until full NORMAL
            # buffered headroom returns. The generic 6 A continuity rule must
            # not take over just because a successful response fell below25.
            needed = (house + minimum > s.main_fuse_amps - s.buffer_amps
                      or grid > s.main_fuse_amps or peak_needed)
        pending = self._overload_transition_started is not None

        def stop(reason: str) -> tuple[str, str, int, float]:
            self._fail_overload(reason)
            self._clear_overload_transition()
            self._overload_hold_active = False
            self._restart_required = True
            self._restart_ready_since = None
            explanation = (f" Nominal reduction rejected: {self._nominal_rejection_code}."
                           if self._nominal_rejection_code else "")
            return "overload_stop", f"Overload allowance blocked: {reason}; requesting 0 A.{explanation}", 0, house

        if status != "charging" or current <= 0 or self.paused_by_us:
            if pending or self._overload_hold_active:
                return stop("charging_interrupted_or_paused")
            return None
        if grid > s.overload_transition_limit_amps:
            return stop("transition_current_exceeded")
        if self.effective_overload_failure_reason is not None and (needed or grid > s.main_fuse_amps or pending):
            if (pending
                    or not self._blocked_overload_allows_nominal_reduction(now, grid, current, house, status)):
                return stop(self.effective_overload_failure_reason)
            return None
        if (needed or pending) and self.effective_overload_used_seconds >= self.effective_overload_budget_seconds:
            return stop("cumulative_budget_exhausted")
        if (needed or pending) and (self._last_input_grid_ts is None or self._last_input_charger_ts is None):
            return stop("missing_report_timestamps")

        if pending:
            assert self._overload_transition_started is not None
            grid_advanced = (self._last_input_grid_ts is not None and self._overload_entry_grid_ts is not None
                             and self._last_input_grid_ts > self._overload_entry_grid_ts)
            ev_advanced = (self._ev_clock_credit and self._overload_minimum_accepted_at is not None
                           and self._last_input_charger_ts is not None
                           and (self._overload_minimum_ev_floor is not None or self._ev_clock_explicit)
                           and self._ev_advanced_since(self._overload_minimum_ev_floor, "_overload_minimum_ev_floor"))
            if (self._meaningfully_charging(status, current)
                    and current <= minimum + 0.5 and ev_advanced):
                if self._overload_minimum_response_ts is None:
                    self._overload_minimum_response_ts = self._last_input_charger_ts
                    if self._last_input_wall_now_ts is not None:
                        self._overload_minimum_response_monotonic = min(now, now - (
                            self._last_input_wall_now_ts - self._last_input_charger_ts))
            elif self._overload_grid_confirmation_deadline is None:
                self._overload_minimum_response_ts = None
                self._overload_minimum_response_monotonic = None
            # The opt-in policy gets electrical admission only from the raw
            # peak owner. Default-off retains the earlier response deadline.
            physical_deadline = (peak_deadline if grid > PEAK_RESPONSE_TRIGGER_AMPS else None) if peak_policy else (
                self._overload_transition_started + s.overload_response_seconds)
            response_first = self._overload_minimum_response_monotonic
            live_minimum = bool(self.last_target is not None and self.last_target[0] == minimum
                and self.last_set_limit_ts is not None and 0 <= now - self.last_set_limit_ts < self.last_target[3])
            bound_source = (self._last_input_control_source_id is not None
                and self._last_input_control_source_id == self._accepted_control_source_id)
            if (physical_deadline is not None and now >= physical_deadline
                    and (response_first is None or response_first >= physical_deadline)):
                return stop("response_timeout")
            sync = self._minimum_confirmation_plan(owner="overload", now=now,
                grid=grid, current=current, cap=minimum,
                source_id=self._accepted_control_source_id,
                accepted_at=self._overload_minimum_accepted_at,
                response_deadline=physical_deadline, response_at=response_first,
                response_valid=bool(live_minimum and bound_source and ev_advanced
                    and self._meaningfully_charging(status, current) and current <= minimum+.5),
                response_owner="_overload_minimum_ev_floor", ceiling=ceiling)
            if sync is not None:
                outcome, basis = sync
                window = self._minimum_confirmation
                if window is not None:
                    self._overload_grid_confirmation_deadline = window.deadline
                    self._overload_grid_confirmation_floor = window.floor
                    self._overload_grid_confirmation_value = window.value
                if outcome == "complete":
                    house = basis
                    if peak_policy:
                        self.complete_peak_response(now,
                            minimum_response_ts=self._overload_minimum_response_ts, ceiling=ceiling)
                    self._clear_overload_transition()
                    self._overload_hold_active = grid > s.main_fuse_amps or needed
                    self._awaiting_reduction_target = None
                    self._house_before_reduction = None
                    self._settle_started = None
                    if not needed:
                        return None
                    return ("overload_minimum", "Minimum current and a later grid report confirm the existing opt-in envelope.", minimum, house)
                if outcome != "wait":
                    code = {"timeout": "grid_confirmation_timeout",
                            "outside_ceiling": "minimum_observed_above_ceiling",
                            "response_outside_ceiling": "minimum_response_outside_ceiling",
                            "source_clock_regressed": "grid_confirmation_source_clock_regressed",
                            "value_changed_without_event": "grid_confirmation_value_changed_without_event"}.get(
                                outcome, "grid_confirmation_source_or_control_invalid")
                    return stop(code)
                anchor = self._overload_anchor_house if self._overload_anchor_house is not None else house
                return ("overload_grid_wait", "The accepted minimum has a qualified response; retaining only its live offer while awaiting a later grid inside the fixed post-response window. No write or lease renewal.", minimum, anchor)
            if physical_deadline is not None and now >= physical_deadline:
                return stop("response_timeout")
            # A grid report from an earlier partial EV reduction is not proof
            # that the final minimum-current response exceeded the ceiling.
            # Wait only inside the original deadline; identical EV reports do
            # not move this first minimum-response timestamp or refund time.
            grid_after_minimum = (self._overload_minimum_response_ts is not None
                                  and self._last_input_grid_ts is not None
                                  and self._last_input_grid_ts > self._overload_minimum_response_ts)
            # A simultaneous high observation is enough to stop; success is
            # intentionally stricter and requires the grid AFTER minimum EV.
            grid_at_or_after_minimum = (self._overload_minimum_response_ts is not None
                                        and self._last_input_grid_ts is not None
                                        and self._last_input_grid_ts >= self._overload_minimum_response_ts)
            if (current <= minimum + 0.5 and grid_advanced and ev_advanced
                    and grid_at_or_after_minimum and grid > ceiling):
                return stop("minimum_observed_above_ceiling")
            if (current <= minimum + 0.5 and grid <= ceiling
                    and grid_advanced and ev_advanced and grid_after_minimum):
                if current > grid + 1 or house + minimum > ceiling:
                    return stop("minimum_response_outside_ceiling")
                if peak_policy:
                    self.complete_peak_response(now,
                        minimum_response_ts=self._overload_minimum_response_ts, ceiling=ceiling)
                self._clear_overload_transition()
                self._overload_hold_active = grid > s.main_fuse_amps or needed
                self._awaiting_reduction_target = None
                self._house_before_reduction = None
                self._settle_started = None
                if not needed:
                    return None
            else:
                # In an EV-first mixed snapshot, use the entry estimate until
                # fresh post-command reports confirm the reduction or timeout.
                anchor = self._overload_anchor_house if self._overload_anchor_house is not None else house
                self._increase_since = None
                return ("overload_shedding", "Requesting minimum current and waiting within the original response deadline.",
                        minimum, anchor)

        if not needed:
            self._overload_hold_active = False
            return None
        if current > grid + 1:
            return stop("inconsistent_current_observations")
        higher_offer = self.last_target is not None and self.last_target[0] > minimum
        if (peak_policy and grid > ceiling and peak_deadline is not None and now < peak_deadline
                and self.last_target is not None and self.last_target[0] == minimum
                and self.last_set_limit_ts is not None
                and 0 <= now-self.last_set_limit_ts < self.last_target[3]
                and self._meaningfully_charging(status, current) and current <= minimum+.5):
            # The minimum is already live: there is no lower positive offer
            # to request or fresh identical ACK that could prove a response.
            # Only this existing peak clock permits the transient dwell.
            return ("overload_peak_hold", "Retaining the existing minimum only while the validated transient peak settles inside its original deadline. No new ACK, write, lease renewal or grid-wait allowance.", minimum, house)
        if house + minimum > ceiling:
            return stop("minimum_projection_exceeds_ceiling")
        # A low observation can predate a higher offer that the API has already
        # accepted. It cannot prove that a NEW reduction to minimum responded.
        # Keep the same bounded shedding path until the accepted minimum has
        # both a later EV receipt and a still later coherent grid receipt.
        if current <= minimum + 0.5 and not higher_offer:
            if grid > ceiling:
                return stop("minimum_observed_above_ceiling")
            if peak_policy:
                self.complete_peak_response(now,
                    minimum_response_ts=self._last_input_charger_ts, ceiling=ceiling)
            if (grid > PEAK_RESPONSE_TRIGGER_AMPS and self.peak_response_active
                    and peak_deadline is not None and now >= peak_deadline):
                return stop("response_timeout")
            self._overload_hold_active = True
            self._increase_since = None
            return ("overload_minimum", "Retaining minimum current inside the opt-in post-response envelope and cumulative time budget.",
                    minimum, house)
        if self._overload_hold_active:
            return stop("minimum_response_lost")
        origin = now
        if self._trial_epoch is not None and self._trial_activation_committed:
            # Preserve the source origin; only the legacy policy derives its
            # response deadline here instead of using the independent owner.
            origin -= max(0.0, self._last_input_wall_now_ts - self._last_input_grid_ts)
            if not peak_policy and now-origin >= s.overload_response_seconds:
                return stop("response_timeout")
        if grid > PEAK_RESPONSE_TRIGGER_AMPS and peak_deadline is not None and now >= peak_deadline:
            return stop("response_timeout")
        self._overload_transition_started = origin
        self._overload_entry_grid_ts = self._last_input_grid_ts
        self._overload_entry_charger_ts = self._last_input_charger_ts
        self._overload_anchor_house = house
        self._increase_since = None
        return ("overload_shedding", "Requesting minimum current; a bounded transition starts now, including API latency.",
                minimum, house)

    def begin_submission(self, decision: RegulatorDecision, now_ts: float) -> bool:
        """Commit retry timing only at the controller's actual API boundary.

        Planning and checkpoint revalidation can run repeatedly without any
        request leaving the controller. They must not spend the retry throttle
        for a lower offer. A failed or ambiguous actual API attempt does spend
        it, independently of whether a later acknowledgement is received.
        This method grants no authority and changes no response/lease clocks.
        """
        now = None if isinstance(now_ts, bool) else _valid_number(now_ts)
        if (now is None or now < 0 or self.settings.active_control is not True
                or self.settings.dry_run is not False
                or decision.should_call_service is not True or decision.dry_run is not False
                or decision.command not in {"set_limit", "pause", "resume"}):
            return False
        fields = []
        if decision.command == "set_limit" and decision.current_p1 > 0:
            if decision.mode == "low_grid_alignment":
                fields.append("_low_grid_attempt_at")
            elif decision.mode == "normal_shedding":
                fields.append("_normal_shed_attempt_at")
            if (decision.current_p1 == self.overload_minimum_current
                    and self._overload_transition_started is not None
                    and self._overload_minimum_accepted_at is None):
                fields.append("_overload_minimum_attempted_at")
        if any(getattr(self, field) is not None and now < getattr(self, field) for field in fields):
            return False
        for field in fields:
            setattr(self, field, now)
        return True

    def reject_submission(self, decision: RegulatorDecision, now_ts: float) -> bool:
        """Debit a failed/aborted reserved recovery without inventing an ACK.

        The controller calls this for a failed, ambiguous or aborted positive
        retry. Without any outcome, its single-flight reservation stays closed.
        The PRE checkpoint already makes a crash at either point conservative.
        """
        if (not self._uncertainty_retry_submission_pending or not self._uncertainty_restart
                or decision.command != "set_limit" or decision.current_p1 <= 0
                or decision.recovery_submission_ts is None
                or decision.recovery_submission_ts != self._uncertainty_last_attempt):
            return False
        now = None if isinstance(now_ts, bool) else _valid_number(now_ts)
        self._uncertainty_retry_submission_pending = False
        self._uncertainty_stops = min(3, self._uncertainty_stops + 1)
        self._uncertainty_stop_recorded = True
        self._uncertainty_recovery_since = None
        self._uncertainty_recovery_epochs = 0
        self._normal_shed_failure = "positive_retry_submission_failed"
        if now is None:
            self._uncertainty_stops = 3
        else:
            self._uncertainty_retry_after = now + min(600,
                self.settings.restart_stable_seconds * 2 ** min(self._uncertainty_stops - 1, 3))
        self._restart_required = True
        self._restart_ready_since = None
        return True

    def acknowledge(self, decision: RegulatorDecision, now_ts: float) -> None:
        """Commit bookkeeping after API submission succeeds, never before it."""
        if not decision.should_call_service or decision.dry_run or decision.command is None:
            return
        if (decision.command == "set_limit" and decision.current_p1 > 0
                and decision.recovery_submission_ts is not None
                and (not self._uncertainty_retry_submission_pending
                     or decision.recovery_submission_ts != self._uncertainty_last_attempt)):
            # A reply from a different recovery attempt must not retire the
            # current reservation or bypass its terminal restart requirement.
            self.last_action_ts = self.last_set_limit_ts = now_ts
            self.last_command = "set_limit"
            self.last_target = (decision.current_p1, decision.current_p2,
                                decision.current_p3, decision.ttl_seconds)
            self._restart_required = True
            self._normal_shed_failure = "obsolete_positive_retry_reply"
            return
        if (decision.mode == "low_grid_raw_hold" and decision.current_p1 > 0
                and (self._low_grid_failure is not None or not self._low_grid_pending
                     or self._low_grid_phase != "raw_hold" or self._restart_required
                     or isinstance(now_ts, bool) or _valid_number(now_ts) is None
                     or self.last_target is None or self.last_target[0] != decision.current_p1
                     or self.last_set_limit_ts is None
                     or not 0 <= now_ts - self.last_set_limit_ts < self.last_target[3])):
            # An old conservative renewal cannot reverse a newer zero, rebind
            # or expired lease. Record API acceptance, retain protective stop.
            self.last_action_ts = self.last_set_limit_ts = now_ts
            self.last_command = "set_limit"
            self.last_target = (decision.current_p1, decision.current_p2,
                                decision.current_p3, decision.ttl_seconds)
            self._restart_required = True
            self._low_grid_failure = "obsolete_or_expired_raw_hold_reply"
            return
        if (decision.mode in {"normal_shedding", "low_grid_alignment"} and decision.current_p1 > 0
                and ((self._normal_shed_failure is not None if decision.mode == "normal_shedding"
                      else self._low_grid_failure is not None)
                     or isinstance(now_ts, bool) or _valid_number(now_ts) is None
                     or not self.settings.peak_only_response and decision.nominal_response_deadline_ts is None
                     or decision.nominal_response_deadline_ts is not None
                     and now_ts >= decision.nominal_response_deadline_ts)):
            # A late reply to an OLD reduction is not a qualified restart.
            # Record the accepted lease truthfully, but preserve the terminal
            # zero/restart requirement even if that zero's HTTP call failed.
            self.last_action_ts = self.last_set_limit_ts = now_ts
            self.last_command = "set_limit"
            self.last_target = (decision.current_p1, decision.current_p2,
                                decision.current_p3, decision.ttl_seconds)
            self._normal_shed_failure = "ack_after_deadline_or_terminal_stop"
            self._restart_required = True
            return
        raw_context = decision.raw_budget_rise_context
        if decision.house_load_basis == "raw_grid_bounded_recovery":
            valid_raw_reply = bool(isinstance(raw_context, RawBudgetRiseContext)
                and type(now_ts) in (int, float) and isfinite(now_ts)
                and now_ts < raw_context.response_deadline_ts
                and decision.current_p1 == raw_context.target
                and self.last_target == raw_context.previous_target
                and self.last_set_limit_ts == raw_context.previous_limit_ts
                and 0 <= now_ts-raw_context.previous_limit_ts < raw_context.previous_target[3]
                and self._last_input_control_source_id == raw_context.source_id == self._accepted_control_source_id
                and self._last_input_status == "charging" and self._last_input_current_trusted
                and not self._restart_required and not self.paused_by_us
                and self._positive_cap is None and self._awaiting_reduction_target is None
                and not self._low_grid_pending and self._low_grid_failure is None
                and self._normal_shed_deadline is None and self._normal_shed_failure is None
                and self._uncertainty_ceiling is None and self._uncertainty_stops == 0
                and not self._uncertainty_retry_submission_pending
                and self._overload_transition_started is None and not self._overload_hold_active)
            if not valid_raw_reply:
                self.last_action_ts = self.last_set_limit_ts = now_ts
                self.last_command = "set_limit"
                self.last_target = (decision.current_p1, decision.current_p2, decision.current_p3, decision.ttl_seconds)
                self._restart_required = True
                self._clear_positive_alignment()
                return
        self.last_action_ts = now_ts
        self.last_command = decision.command
        if decision.command == "set_limit":
            old_target = self.last_target[0] if self.last_target is not None else None
            target = decision.current_p1
            changed = old_target != target
            raw_rise_ack = bool(changed and old_target is not None and old_target > 0 and target > old_target
                and decision.house_load_basis == "raw_grid_bounded_recovery")
            raw_prior_lease_live = bool(self.last_set_limit_ts is not None and self.last_target is not None
                and 0 <= now_ts-self.last_set_limit_ts < self.last_target[3])
            same_cap_restart = not changed and target > 0 and self._restart_required
            accepted_restart = target > 0 and self._restart_required
            self._accepted_control_source_id = self._last_input_control_source_id
            if changed or accepted_restart:
                self._accepted_control_ev_ts = self._last_input_charger_ts
                self._pin_ev_owner("_accepted_control_ev_ts", wall_floor=self._ack_wall(now_ts))
            if (self._low_grid_pending and target == self._low_grid_cap
                    and self._low_grid_accepted_at is None):
                self._low_grid_accepted_at = now_ts
                if (not isinstance(now_ts, bool) and _valid_number(now_ts) is not None
                        and self._last_input_now_ts is not None
                        and self._last_input_wall_now_ts is not None
                        and now_ts >= self._last_input_now_ts):
                    self._low_grid_ack_ev_floor = (self._last_input_wall_now_ts
                        + now_ts - self._last_input_now_ts)
                    self._set_ev_owner_wall("_low_grid_ev_floor", self._low_grid_ack_ev_floor)
            if (accepted_restart or (changed and target > 0 and self._low_grid_cap is not None
                                     and target != self._low_grid_cap)):
                self._clear_low_grid_alignment()
            positive_shedding_budget = None
            if (changed and target > 0 and old_target is not None and target < old_target
                    and self._positive_cap == old_target and self._positive_skew_since is not None):
                budget = min(self.settings.measurement_settle_seconds, self.settings.max_sensor_age_seconds)
                if self._positive_raw_recovery:
                    budget = min(budget, self._positive_raw_window_seconds or 0.)
                positive_shedding_budget = (
                    budget,
                    self._positive_alignment_used_seconds + max(0.0, now_ts - self._positive_skew_since),
                    self._positive_skew_since + budget - self._positive_alignment_used_seconds,
                )
            if changed:
                self._last_limit_change_ts = now_ts
                waiting_offer_reduction = (
                    self._positive_cap is not None and old_target is not None
                    and 0 < target < old_target and target > decision.charger_current + 0.5
                    and not self._meaningfully_charging(self._last_input_status or "", decision.charger_current)
                )
                if waiting_offer_reduction:
                    # The car may start much later at this smaller offer too.
                    # A decrease must not refund any uncertainty already spent.
                    self._positive_cap = target
                    self._positive_house = decision.house_load_l1
                else:
                    self._clear_positive_alignment()
                # Only a genuinely changed, accepted increase may establish
                # alignment context. Failed calls and identical TTL renewals do
                # not acknowledge physical response or reset the grace period.
                if (target > 0 and (old_target is None or target > old_target)
                        and target > decision.charger_current + 0.5
                        and self._last_input_grid_ts is not None
                        and (self._last_input_charger_ts is not None or self._ev_clock_explicit and self._ev_current_sequences)
                        and decision.house_load_l1 + target <= self.settings.main_fuse_amps - self.settings.buffer_amps):
                    self._positive_cap = target
                    self._positive_house = decision.house_load_l1
                    self._positive_grid_ts = self._last_input_grid_ts
                    self._positive_ev_ts = self._last_input_charger_ts
                    self._pin_ev_owner("_positive_ev_ts", wall_floor=self._ack_wall(now_ts))
                    self._positive_before_current = decision.charger_current
            if same_cap_restart:
                # The zero may have physically stopped the car without an HTTP
                # ACK. An accepted, fully qualified restart is a new potential
                # positive response even if its cap matches the old lease.
                self._clear_positive_alignment()
                if (target > decision.charger_current + 0.5
                        and self._last_input_grid_ts is not None
                        and (self._last_input_charger_ts is not None or self._ev_clock_explicit and self._ev_current_sequences)
                        and decision.house_load_l1 + target <= self.settings.main_fuse_amps - self.settings.buffer_amps):
                    self._positive_cap = target
                    self._positive_house = decision.house_load_l1
                    self._positive_grid_ts = self._last_input_grid_ts
                    self._positive_ev_ts = self._last_input_charger_ts
                    self._pin_ev_owner("_positive_ev_ts", wall_floor=self._ack_wall(now_ts))
                    self._positive_before_current = decision.charger_current
            if raw_rise_ack and self._positive_cap == target:
                # This is the same positive owner and original raw sample,
                # not a second response window. API/checkpoint latency spends
                # its existing alignment budget before acknowledgement.
                self._positive_raw_recovery = True
                self._positive_raw_window_seconds = raw_context.response_window_seconds
                self._positive_skew_since = raw_context.response_deadline_ts-raw_context.response_window_seconds
                self._positive_grid_ts = raw_context.grid_report_ts
                self._positive_ev_ts = raw_context.event_report_ts
                if not raw_prior_lease_live:
                    self._restart_required = True
            if changed and (target < decision.charger_current or
                            (old_target is not None and target < old_target)):
                # A submitted cap is not a measured current. Retain the last
                # house estimate, and inhibit rises until the EV responds.
                same_pending_chain = (
                    target > 0 and old_target is not None and target < old_target
                    and self._reduction_previous_cap is not None
                    and self._partial_reduction_budget is not None
                    and (self._awaiting_reduction_target == old_target
                         or (self._accepted_reduction_cap == old_target
                             and self._reduction_guard_deadline is not None
                             and now_ts < self._reduction_guard_deadline))
                )
                previous_cap = self._reduction_previous_cap if same_pending_chain else old_target
                chain_origin = self._reduction_chain_origin if same_pending_chain else now_ts
                budget = (self._partial_reduction_budget if same_pending_chain else
                          min(self.settings.measurement_settle_seconds, self.settings.max_sensor_age_seconds))
                spent = self._partial_reduction_used_seconds if same_pending_chain else 0.0
                if same_pending_chain and self._settle_started is not None:
                    spent += max(0.0, now_ts - self._settle_started)
                deadline = self._reduction_guard_deadline if same_pending_chain else None
                grid_ceiling = decision.grid_l1
                if same_pending_chain:
                    # A further reduction cannot refund uncertainty or replace
                    # the known preceding offer by the smaller intermediate cap.
                    self._house_before_reduction = max(
                        self._house_before_reduction or 0.0, decision.house_load_l1)
                    if self._reduction_grid_ceiling is not None and grid_ceiling is not None:
                        grid_ceiling = max(grid_ceiling, self._reduction_grid_ceiling)
                else:
                    self._house_before_reduction = decision.house_load_l1
                # Every accepted cap needs response baselines for THAT command,
                # even when its uncertainty belongs to an older active chain.
                self._reduction_grid_ts = self._last_input_grid_ts
                self._reduction_charger_ts = self._last_input_charger_ts
                self._pin_ev_owner("_reduction_charger_ts", wall_floor=self._ack_wall(now_ts))
                self._settle_started = None
                self._clear_partial_reduction_alignment()
                self._awaiting_reduction_target = target
                self._accepted_reduction_cap = target if target > 0 else None
                self._reduction_before_current = decision.charger_current
                self._reduction_previous_cap = previous_cap
                self._reduction_chain_origin = chain_origin
                self._reduction_under_cap_baseline = (
                    target > 0 and old_target is not None and old_target > target
                    and decision.charger_current <= target + 0.5)
                self._reduction_high_current = decision.charger_current
                self._reduction_high_ev_ts = self._last_input_charger_ts
                self._pin_ev_owner("_reduction_high_ev_ts", wall_floor=self._ack_wall(now_ts))
                self._reduction_grid_ceiling = grid_ceiling
                self._partial_reduction_budget = budget
                self._partial_reduction_used_seconds = spent
                self._reduction_guard_deadline = deadline
                if positive_shedding_budget is not None:
                    # The lower cap is a response inside the ORIGINAL positive
                    # alignment, not permission for a fresh response window.
                    # Capture before clearing the positive context above and
                    # charge the API's elapsed time before this acknowledgement.
                    source_budget, source_spent, source_deadline = positive_shedding_budget
                    self._partial_reduction_budget = min(budget, source_budget)
                    self._partial_reduction_used_seconds = max(spent, source_spent)
                    self._reduction_guard_deadline = (
                        min(deadline, source_deadline) if deadline is not None else source_deadline)
                self._increase_since = None
            elif changed:
                self._awaiting_reduction_target = None
                self._house_before_reduction = None
                self._settle_started = None
                self._clear_partial_reduction_alignment()
            if changed and (old_target is None or target > old_target):
                self._last_increase_ts = now_ts
                self._nominal_shedding_started = self._nominal_shedding_budget = None
            if (target == self.overload_minimum_current
                    and self._overload_transition_started is not None
                    and self._overload_minimum_accepted_at is None):
                self._overload_minimum_accepted_at = now_ts
                self._overload_minimum_ev_floor = None
                # Include API latency in the receipt ordering floor as well as
                # in the original monotonic response deadline. A report that
                # arrived while the command was pending is not its response.
                accepted_now = None if isinstance(now_ts, bool) else _valid_number(now_ts)
                if (self._last_input_wall_now_ts is not None and self._last_input_now_ts is not None
                        and accepted_now is not None and accepted_now >= self._last_input_now_ts):
                    accepted_wall = _valid_number(
                        self._last_input_wall_now_ts + accepted_now - self._last_input_now_ts)
                    if accepted_wall is not None and self._last_input_charger_ts is not None:
                        self._overload_minimum_ev_floor = max(self._last_input_charger_ts, accepted_wall)
                        self._pin_ev_owner("_overload_minimum_ev_floor", wall_floor=accepted_wall)
                peak_deadline = (self.peak_response_deadline(now_ts) if self.settings.peak_only_response
                                 else self._overload_transition_started+self.settings.overload_response_seconds)
                if (self._awaiting_reduction_target == target and peak_deadline is not None
                        and (not self.settings.peak_only_response or decision.grid_l1 is not None
                             and decision.grid_l1 > PEAK_RESPONSE_TRIGGER_AMPS)):
                    deadline = peak_deadline
                    self._partial_reduction_budget = min(
                        self._partial_reduction_budget if self._partial_reduction_budget is not None
                        else self.settings.overload_response_seconds,
                        self.settings.overload_response_seconds)
                    self._reduction_guard_deadline = min(
                        self._reduction_guard_deadline if self._reduction_guard_deadline is not None
                        else deadline, deadline)
            if target == 0:
                self._nominal_shedding_started = self._nominal_shedding_budget = None
                self._restart_required = True
                self._waiting_for_vehicle_since = None
            elif self._restart_required:
                # A genuinely accepted restart after the full observation can
                # reuse the old cap when the preceding zero never got an ACK.
                # It is a new nominal attempt, not a TTL refund of the old one.
                self._nominal_shedding_started = self._nominal_shedding_budget = None
                self._restart_required = False
                self._restart_ready_since = None
            if target > 0 and self._uncertainty_restart:
                # Only actual acceptance of the qualified, capped restart
                # begins the next bounded recovery attempt.
                self._uncertainty_ceiling = target
                self._uncertainty_restart = False
                self._uncertainty_stop_recorded = False
                self._uncertainty_last_attempt = self._uncertainty_last_attempt_cap = None
                self._uncertainty_retry_submission_pending = False
            if accepted_restart:
                self._clear_bounded_nominal_shedding()
            elif (self._normal_shed_deadline is not None and self._normal_shed_failure is None
                    and self._normal_shed_accepted_at is None and decision.mode == "normal_shedding"
                    and target == self._normal_shed_cap):
                accepted_now = None if isinstance(now_ts, bool) else _valid_number(now_ts)
                if accepted_now is None or accepted_now >= self._normal_shed_deadline:
                    self._normal_shed_failure = "ack_after_deadline"
                    self._restart_required = True
                else:
                    self._normal_shed_accepted_at = accepted_now
                    # A local HTTP receipt is not an EV source event. Only a
                    # later event can prove the physical lower-current state.
                    if (self._last_input_wall_now_ts is not None and self._last_input_now_ts is not None
                            and accepted_now >= self._last_input_now_ts):
                        self._normal_shed_ev_floor = max(self._normal_shed_ev_floor or 0.0,
                            self._last_input_wall_now_ts + accepted_now - self._last_input_now_ts)
                        self._set_ev_owner_wall("_normal_shed_ev_floor", self._ack_wall(accepted_now))
                    else:
                        self._normal_shed_ev_floor = None
            self.last_set_limit_ts = now_ts
            self.last_target = (
                decision.current_p1, decision.current_p2, decision.current_p3, decision.ttl_seconds
            )
            ordinary = self._ordinary_lower
            if (ordinary is not None and decision.mode == "ordinary_lower_offer"
                    and target == ordinary.cap and ordinary.accepted_at is None):
                ordinary.accepted_at = now_ts
                ordinary.ev_floor = self._last_input_charger_ts
                self._pin_ev_owner("_ordinary_ev_floor", wall_floor=self._ack_wall(now_ts))
            if target == 0 or accepted_restart:
                self._ordinary_lower = None
                self._clear_minimum_confirmation("ordinary")
        elif decision.command == "pause":
            self._clear_positive_alignment()
            self._resume_submission_ts = None
            self.paused_by_us = True
            self._restart_required = True
            self._restart_ready_since = None
            self._increase_since = None
            self._pause_observed = False
            self._pause_submission_ts = now_ts
        elif decision.command == "resume":
            self.paused_by_us = False
            self._pause_observed = False
            self._pause_submission_ts = None
            self._resume_submission_ts = now_ts
            self.below_resume_since = None

    def calculate_available_l1(self, grid_l1: Optional[float], charger_current: float) -> tuple[int, float]:
        if grid_l1 is None or grid_l1 < 0:
            return 0, 0.0
        house = max(0.0, grid_l1 - max(0.0, charger_current))
        available = self.settings.main_fuse_amps - house - self.settings.buffer_amps
        if available < self.settings.min_charge_amps:
            return 0, house
        return int(floor(min(available, self.settings.max_charge_amps))), house

    def _limit_with_continuity(self, now: float, desired: int, available: int,
                               house: float, current: float, status: str,
                               allow_minimum: bool = True, raw_zero_restart: bool = False,
                               raw_zero_fresh: bool = False, raw_budget_rise: bool = False) -> int:
        """Lower promptly, retain minimum within budget, and raise slowly.

        The reserve may spend part of the normal operating buffer, but never
        intentionally permits total estimated current above the fuse rating.
        """
        s = self.settings
        physically_charging = self._meaningfully_charging(status, current)
        reserve = min(s.buffer_amps, max(0.0, s.minimum_reserve_amps))
        if (allow_minimum and desired < s.min_charge_amps and physically_charging
                and house + s.min_charge_amps <= s.main_fuse_amps - reserve):
            desired = s.min_charge_amps

        if desired < s.min_charge_amps:
            self._restart_required = True
            self._restart_ready_since = None
            self._increase_since = None
            self._previous_desired = 0
            return 0

        # A stopped/starting car must first have full buffered capacity for a
        # continuous observation period. Borrowing buffer is for continuity.
        if self.last_target is None and not physically_charging:
            self._restart_required = True
        startup = self.startup_current if self._start_allowed(status) else s.min_charge_amps
        if self._restart_required:
            self._increase_since = None
            self._previous_desired = desired
            if self._uncertainty_restart and self._uncertainty_stops >= 3:
                self._restart_ready_since = None
                return 0
            if raw_zero_restart:
                # Keep the lowest whole-cap raw allowance observed throughout
                # this period. A last-second load drop cannot turn sixty
                # seconds of 7 A headroom into an immediate 16 A restart.
                cap = min(desired, available)
                if self._restart_ready_since is None or self._raw_zero_restart_cap is None:
                    self._raw_zero_restart_cap = cap
                else:
                    self._raw_zero_restart_cap = min(self._raw_zero_restart_cap, cap)
            else:
                self._raw_zero_restart_cap = None
            ready = self._timer_reached(
                now, "_restart_ready_since", available >= startup and desired >= startup
                and (self._awaiting_reduction_target is None or raw_zero_restart),
                s.restart_stable_seconds,
            )
            if (self._uncertainty_restart and self._uncertainty_retry_after is not None
                    and now < self._uncertainty_retry_after):
                return 0
            if raw_zero_restart:
                return self._raw_zero_restart_cap if ready and raw_zero_fresh else 0
            return (desired if self._start_allowed(status) else startup) if ready else 0

        if self.last_target is None:
            # Legacy takeover seeds from measured current. Fast recovery may
            # use full buffered headroom only with explicit fresh receipts.
            base = max(s.min_charge_amps, int(floor(current)))
            self._increase_since = None
            self._previous_desired = desired
            if s.fast_recovery and desired > base:
                if self._fast_sources_fresh() and self._awaiting_reduction_target is None and self._positive_cap is None:
                    return desired
                self._fast_recovery_wait = "fresh"
            return min(desired, base)

        base = self.last_target[0]
        if not physically_charging:
            self._increase_since = None
            self._previous_desired = desired
            if not self._start_allowed(status):
                # Setting a safe cap must not bypass a schedule, authorization,
                # Equalizer hold or somebody else's manual pause.
                self._restart_ready_since = None
                return min(desired, base)
            if base < startup:
                ready = self._timer_reached(
                    now, "_restart_ready_since", desired >= startup and available >= startup
                    and self._awaiting_reduction_target is None,
                    s.restart_stable_seconds,
                )
                return desired if ready else min(desired, base)
            # Startup is a minimum-capacity threshold, not a maximum offer.
            # The caller uses raw grid headroom while the vehicle is waiting.
            self._restart_ready_since = None
            return desired
        if desired <= base:
            self._increase_since = None
            self._previous_desired = desired
            return desired

        load_rose = self._previous_desired is not None and desired < self._previous_desired
        self._previous_desired = desired
        if s.fast_recovery:
            if raw_budget_rise:
                return desired
            if (self._awaiting_reduction_target is not None
                    or (self._positive_cap is not None and not self._snapshot_aligned)):
                self._fast_recovery_wait = "aligned"
            elif not self._fast_sources_fresh():
                self._fast_recovery_wait = "fresh"
            else:
                return desired
            self._increase_since = None
            return max(s.min_charge_amps, base)
        if (self._awaiting_reduction_target is not None
                or (self._positive_cap is not None and not self._positive_reports_advanced())
                or load_rose):
            self._increase_since = None
            return max(s.min_charge_amps, base)

        # Legacy gradual recovery retains its original load-stability policy.
        stable = self._timer_reached(now, "_increase_since", True, s.increase_stable_seconds)
        interval_elapsed = (self._last_increase_ts is None or
                            now - self._last_increase_ts >= s.increase_interval_seconds)
        if stable and interval_elapsed:
            return min(desired, base + 1)
        return max(s.min_charge_amps, base)

    def _timer_reached(self, now: float, attr: str, condition: bool, seconds: int) -> bool:
        if not condition:
            setattr(self, attr, None)
            return False
        since = getattr(self, attr)
        if since is None:
            setattr(self, attr, now)
            return seconds <= 0
        return now - since >= seconds

    def _action_allowed(self, now: float) -> bool:
        return self.last_action_ts is None or now - self.last_action_ts >= self.settings.min_seconds_between_actions

    def _should_send_limit(self, now: float, current: int, ttl: int) -> bool:
        if (current == self.overload_minimum_current
                and self._overload_transition_started is not None
                and self._overload_minimum_accepted_at is None):
            # A still-valid identical lease may have been accepted BEFORE the
            # overload transition. Submit its minimum once more so the new
            # response has an explicit acceptance floor. A failed or blocked
            # proposal proves nothing; retry only at the normal action rate,
            # inside the SAME original transition deadline.
            if not self.settings.active_control or self.settings.dry_run:
                return False
            if (self._overload_minimum_attempted_at is not None
                    and now - self._overload_minimum_attempted_at < self.settings.min_seconds_between_actions):
                return False
            return True
        if self.last_target != (current, 0, 0, ttl) or self.last_set_limit_ts is None:
            return True
        resend = min(self.settings.resend_limit_every_seconds, ttl / 2)
        return now - self.last_set_limit_ts >= max(1, resend)

    def evaluate(self, inp: RegulatorInput) -> RegulatorDecision:
        s = self.settings
        previous_grid_ts, previous_charger_ts = self._last_input_grid_ts, self._last_input_charger_ts
        self._observe_ev_clock(inp)
        observed_now = _valid_number(inp.now_ts)
        now = observed_now if observed_now is not None else 0.0
        self._last_input_now_ts = None if isinstance(inp.now_ts, bool) else observed_now
        if self._restored_normal_wait > 0 and self._last_input_now_ts is not None:
            self._uncertainty_retry_after = now + self._restored_normal_wait
            self._restored_normal_wait = 0.0
        status = (inp.status or "").strip().lower()
        self._fast_recovery_wait = None
        self._nominal_overload_blocked = False
        self._nominal_rejection_code = None
        self._snapshot_aligned = False
        self._last_input_status = status
        if status != "paused":
            self._resume_submission_ts = None
        grid = None if isinstance(inp.grid_l1, bool) else _valid_number(inp.grid_l1)
        current = None if isinstance(inp.charger_current, bool) else _valid_number(inp.charger_current)
        self._last_input_grid_ts = (None if isinstance(inp.grid_report_ts, bool) else _valid_number(inp.grid_report_ts))
        self._last_input_charger_ts = (None if isinstance(inp.charger_report_ts, bool) else _valid_number(inp.charger_report_ts))
        self._last_input_wall_now_ts = (None if isinstance(inp.wall_now_ts, bool) else _valid_number(inp.wall_now_ts))
        self._last_input_current_trusted = inp.charger_current_trusted is True
        self._last_input_snapshot_verified = inp.charger_snapshot_verified is True
        source_id = inp.control_source_id
        self._last_input_control_source_id = source_id if (
            isinstance(source_id, str) and bool(source_id.strip()) or
            isinstance(source_id, tuple) and bool(source_id) and all(
                isinstance(part, (str, int)) and not isinstance(part, bool) and part != ""
                for part in source_id)) else None
        # Some protective branches retire their context before constructing
        # the zero decision. Retain only the cause, not any positive authority.
        measurement_conflict = bool(grid is not None and current is not None
            and (not self._last_input_current_trusted or current > grid + 1
                 or (self._positive_cap is not None and current > self._positive_cap + 0.5)))
        available, house = self.calculate_available_l1(grid, (current or 0.0) if self._last_input_current_trusted else 0.0)
        current_fresh = self._fast_sources_fresh()
        refresh_required = False
        house_basis = "grid_minus_reported_ev"
        source_processing_deadline = None
        if self.last_target is None and not current_fresh:
            # Initial takeover cannot be seeded from an old retained EV value.
            available, house = self.calculate_available_l1(grid, 0.0)
            house_basis = "raw_grid_unverified_ev"
        if (self._ev_clock_explicit and (self._ev_established_value != current
                or self._ev_established_binding != self._last_input_control_source_id)):
            # An unknown first frame or metadata-only clock baseline supplies
            # no new numeric EV subtraction. A previously reported unchanged
            # value may remain an established cache state under its binding.
            available, house = self.calculate_available_l1(grid, 0.0)
            house_basis = "raw_grid_without_established_ev_value"

        def decision(mode: str, reason: str, command: str | None = None,
                     target: int = 0, ttl: int | None = None, emergency: bool = False) -> RegulatorDecision:
            if command == "pause" or command == "set_limit" and target == 0:
                # A terminal protection has one outcome. An old observation
                # phase must not survive zero as diagnostic or future authority.
                self._minimum_confirmation = None
                self._ordinary_lower = None
                self._normal_shed_sync_deadline = None
                self._overload_grid_confirmation_deadline = None
            family_rejection = bool(s.bounded_nominal_shedding and mode == "overload_stop"
                and self._nominal_rejection_code in {"charger_receipt_stale", "report_skew_exceeds_alignment_window",
                                                     "not_meaningfully_charging"})
            if (command == "set_limit" and target == 0 and mode in {"unavailable", "overload_stop"}
                    and self._uncertainty_ceiling is None and self.last_target is not None
                    and self.last_target[0] > 0 and inp.data_valid and not inp.data_error
                    and status == "charging" and grid is not None and current is not None
                    and current <= s.max_charge_amps + 0.5
                    and (measurement_conflict or family_rejection)):
                self._uncertainty_ceiling = min(self.last_target[0], s.max_charge_amps)
                self._uncertainty_previous_offer = self.last_target[0]
            if self._uncertainty_ceiling is not None and command in {"set_limit", "resume"}:
                if target > 0:
                    ceiling = self.startup_current if self._uncertainty_restart else self._uncertainty_ceiling
                    target = min(target, ceiling)
                elif command == "set_limit":
                    self._record_uncertainty_stop(now)
                    if self._uncertainty_stops >= 3:
                        mode = "measurement_recovery_blocked"
                        reason += " Repeated measurement recovery failed; needs attention. Reconnect after diagnosis; reload/reset does not grant new attempts."
            ttl_value = s.normal_ttl_seconds if ttl is None else ttl
            should_call = s.active_control and command is not None
            if command == "set_limit":
                reduction = self.last_target is not None and target < self.last_target[0]
                should_call = should_call and self._should_send_limit(now, target, ttl_value)
                # Renewing a lease must not postpone an otherwise due rise.
                change_allowed = (self._last_limit_change_ts is None or
                                  now - self._last_limit_change_ts >= s.min_seconds_between_actions)
                should_call = should_call and (emergency or reduction or change_allowed)
            else:
                should_call = should_call and ((emergency and command == "pause") or self._action_allowed(now))
            live_same_cap = bool(self.last_target is not None and self.last_target[0] == target > 0
                and self.last_set_limit_ts is not None
                and 0 <= now-self.last_set_limit_ts < self.last_target[3]
                and not self._restart_required)
            if (inp.charger_increase_ready is not True and should_call
                    and (command == "resume" or command == "set_limit" and target > 0
                         and (not live_same_cap and (self.last_target is None
                              or target >= self.last_target[0] or self._restart_required)))):
                should_call = False
                mode = "waiting_for_stable_current_frame"
                reason = "Current-conductor reports are still changing; deferring only new positive authority until the bounded quiet period ends."
            if (inp.charger_publication_pending is True and should_call
                    and (command == "resume" or command == "set_limit" and target > 0)):
                # This is before retry reservation. Waiting for the additive
                # raw observer must spend neither an API attempt nor a lease.
                # All existing safety/owner deadlines were still evaluated.
                should_call = False
                mode = "waiting_for_current_publication"
                reason = "The native current payload is not yet committed; deferring positive commands without changing source clocks, accepted authority or response deadlines."
            recovery_submission_ts = None
            if (self._uncertainty_restart and command == "set_limit" and target > 0 and should_call):
                if self._uncertainty_retry_submission_pending:
                    should_call = False
                    mode = "waiting_for_command_result"
                    reason = "A positive recovery submission has no verified outcome; no second retry is permitted."
                elif (self._uncertainty_last_attempt is not None
                        and now - self._uncertainty_last_attempt < s.min_seconds_between_actions):
                    should_call = False
                elif not s.dry_run:
                    # Reserved BEFORE the controller's durable precommand
                    # checkpoint. An ambiguous ACK/crash must spend an attempt
                    # on restore, rather than replaying the same retry forever.
                    self._uncertainty_retry_submission_pending = True
                    self._uncertainty_last_attempt = now
                    self._uncertainty_last_attempt_cap = target
                    recovery_submission_ts = now
            raw_context = None
            if (house_basis == "raw_grid_bounded_recovery" and command == "set_limit" and target > 0
                    and self.last_target is not None and target > self.last_target[0]):
                window = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                raw_context = RawBudgetRiseContext(self._last_input_control_source_id, self.last_target,
                    self.last_set_limit_ts, target, self._last_input_grid_ts, self._last_input_charger_ts,
                    now-(self._last_input_wall_now_ts-self._last_input_grid_ts)+window, window)
            grid_confirmation_deadline = (self._minimum_confirmation.deadline
                if self._minimum_confirmation is not None else self._normal_shed_sync_deadline
                if self._normal_shed_grid_wait and self._normal_shed_failure is None else
                self._overload_grid_confirmation_deadline)
            if s.peak_only_response:
                response_deadline = (self.peak_response_deadline(now)
                    if grid is not None and grid > PEAK_RESPONSE_TRIGGER_AMPS
                    and self._minimum_confirmation is None else None)
                overload_remaining = max(0., response_deadline-now) if response_deadline is not None else None
            else:
                response_deadline = (self._low_grid_deadline
                    if self._low_grid_pending and self._low_grid_failure is None
                    and self._low_grid_phase not in {"raw_hold", "grid_wait"} else
                    self._normal_shed_deadline if self._normal_shed_failure is None
                    and self._normal_shed_sync_deadline is None else None)
                overload_remaining = (max(0., s.overload_response_seconds-(now-self._overload_transition_started))
                    if self._overload_transition_started is not None
                    and self._overload_grid_confirmation_deadline is None else None)
            return RegulatorDecision(
                mode=f"dry_run_{mode}" if s.dry_run and mode not in {"idle", "unavailable"} else mode,
                reason=reason, command=command,
                service=("easee.set_circuit_dynamic_limit" if command == "set_limit"
                         else "easee.action_command" if command else None),
                current_p1=target, current_p2=0, current_p3=0, ttl_seconds=ttl_value,
                available_l1=available, house_load_l1=house, grid_l1=grid,
                charger_current=current or 0.0, paused_by_us=self.paused_by_us,
                should_call_service=bool(should_call), dry_run=s.dry_run,
                overload_used_seconds=self.effective_overload_used_seconds,
                overload_remaining_seconds=max(0.0, self.effective_overload_budget_seconds - self.effective_overload_used_seconds),
                overload_transition_remaining_seconds=overload_remaining,
                overload_failure_latched=self.effective_overload_failure_reason is not None,
                overload_lifetime_used_seconds=self._overload_used_seconds,
                overload_historical_failure_reason=self._overload_failure_reason,
                raw_budget_rise_context=raw_context,
                raw_budget_response_deadline_ts=(raw_context.response_deadline_ts if raw_context else
                    self._positive_raw_deadline if self._positive_raw_deadline is not None
                    and now < self._positive_raw_deadline else None),
                source_processing_deadline_ts=source_processing_deadline,
                grid_confirmation_deadline_ts=grid_confirmation_deadline,
                grid_confirmation_remaining_seconds=(max(0.0, grid_confirmation_deadline - now)
                    if grid_confirmation_deadline is not None else None),
                startup_charge_amps=self.startup_current,
                waiting_for_vehicle_seconds=(max(0.0, now - self._waiting_for_vehicle_since)
                                             if self._waiting_for_vehicle_since is not None else 0.0),
                measurement_alignment_remaining_seconds=(
                    max(0.0, self._positive_raw_deadline-now) if self._positive_raw_recovery
                    and self._positive_raw_deadline is not None else
                    max(0.0, min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                        - self._positive_alignment_used_seconds - (now - self._positive_skew_since))
                    if self._positive_skew_since is not None else
                    self._reduction_alignment_remaining(now) if self._settle_started is not None
                    and self._awaiting_reduction_target is not None else None),
                current_refresh_required=refresh_required or self._fast_recovery_wait == "fresh",
                house_load_basis=house_basis,
                house_load_anchor_l1=self._positive_house,
                nominal_rejection_code=self._nominal_rejection_code,
                source_report_skew_seconds=(abs(self._last_input_grid_ts - self._last_input_charger_ts)
                    if self._last_input_grid_ts is not None and self._last_input_charger_ts is not None else None),
                grid_report_age_seconds=(self._last_input_wall_now_ts - self._last_input_grid_ts
                    if self._last_input_wall_now_ts is not None and self._last_input_grid_ts is not None else None),
                charger_report_age_seconds=(self._last_input_wall_now_ts - self._last_input_charger_ts
                    if self._last_input_wall_now_ts is not None and self._last_input_charger_ts is not None else None),
                source_alignment_window_seconds=s.measurement_settle_seconds,
                charger_current_trusted=self._last_input_current_trusted,
                measurement_uncertainty_active=self._uncertainty_ceiling is not None,
                measurement_uncertainty_cap=self._uncertainty_ceiling,
                measurement_uncertainty_stops=self._uncertainty_stops,
                measurement_recovery_wait_seconds=(max(0.0, self._uncertainty_retry_after - now)
                    if self._uncertainty_retry_after is not None else 0.0),
                nominal_response_remaining_seconds=(max(0., response_deadline-now) if response_deadline is not None else None),
                nominal_response_deadline_ts=response_deadline,
                recovery_submission_ts=recovery_submission_ts,
                nominal_sync_deadline_ts=(self._normal_shed_sync_deadline
                    if self._normal_shed_failure is None and not self._normal_shed_grid_wait else None),
                nominal_sync_remaining_seconds=(max(0.0, self._normal_shed_sync_deadline - now)
                    if self._normal_shed_sync_deadline is not None and self._normal_shed_failure is None else None),
            )

        error = inp.data_error or self._ev_clock_error
        if not inp.data_valid:
            error = error or "Required telemetry is missing or stale."
        elif grid is None or grid < 0:
            error = "L1 current is missing, invalid or negative."
        elif current is None or current < 0:
            error = "Charger current is missing, invalid or negative."
        elif status not in ALLOWED_ACTIVE_STATUSES | IDLE_STATUSES:
            error = f"Charger status '{status}' is unavailable, unsupported or a fault."
        if not self._observe_overload_time(observed_now, grid, not bool(error)):
            error = "Controller time is invalid or moved backwards."
        self.observe_automatic_cycle(inp, source_valid=not bool(error))
        trial_error = self._trial_continuity_error(inp)
        if trial_error is not None:
            error = error or trial_error
        if error:
            if self._low_grid_pending:
                self._low_grid_failure = "source_invalid"
                self._low_grid_pending = False
                self._awaiting_reduction_target = None
                self._house_before_reduction = None
                self._settle_started = None
                self._clear_partial_reduction_alignment()
            if self._normal_shed_deadline is not None and self._normal_shed_failure is None:
                self._normal_shed_failure = "source_invalid"
            if s.overload_enabled and (self._overload_transition_started is not None or self._overload_hold_active):
                self._fail_overload("invalid_telemetry_during_overload")
                self._clear_overload_transition()
                self._overload_hold_active = False
            self.reset_timers()
            self._clear_positive_alignment()
            self._resume_submission_ts = None
            self._waiting_for_vehicle_since = None
            self._restart_required = True
            return decision("unavailable", f"{error} Requesting 0 A; cloud control is best effort.",
                            "set_limit", emergency=True)

        if status in IDLE_STATUSES:
            self.clear_peak_response_session()
            self._ordinary_lower = None
            self._clear_minimum_confirmation("ordinary")
            if self._overload_transition_started is not None:
                self._fail_overload("charging_interrupted_or_paused")
            self._clear_overload_transition()
            self._overload_hold_active = False
            self.paused_by_us = False
            self._pause_observed = False
            self._pause_submission_ts = None
            self._resume_submission_ts = None
            self.reset_timers()
            self._restart_required = True
            self._awaiting_reduction_target = None
            self._house_before_reduction = None
            self._clear_partial_reduction_alignment()
            self._clear_positive_alignment()
            self._waiting_for_vehicle_since = None
            self._clear_measurement_uncertainty()
            self._clear_bounded_nominal_shedding()
            self._clear_low_grid_alignment()
            return decision("idle", f"Status '{status}' does not require charge control.")

        assert grid is not None
        assert current is not None
        self.observe_peak_response(inp)
        # A new independently raw-safe frame can close an ended peak without
        # changing any restart/delivery/failure barrier or claiming EV response.
        self.complete_peak_response(now)
        if (self.last_target is not None and self.last_target[0] > 0
                and self._accepted_control_source_id is not None
                and self._last_input_control_source_id != self._accepted_control_source_id):
            # Reject the binding itself before either raw fallback can turn
            # another device/source's reading into authority for this lease.
            self.reset_timers()
            self._clear_positive_alignment()
            if s.overload_enabled and (self._overload_transition_started is not None or self._overload_hold_active):
                self._fail_overload("control_source_changed")
                self._clear_overload_transition()
                self._overload_hold_active = False
            if self._normal_shed_deadline is not None:
                self._normal_shed_failure = "control_source_changed"
            self._restart_required = True
            return decision("unavailable", "Accepted positive response source binding changed; requesting 0 A.",
                            "set_limit", emergency=True)
        ordinary_plan = self._ordinary_lower_offer_plan(inp, now, grid, current, status)
        if ordinary_plan is not None:
            mode, reason, command, cap, house = ordinary_plan
            house_basis = "raw_bounded_ordinary_observation"
            available = int(floor(min(s.max_charge_amps, s.main_fuse_amps-s.buffer_amps-house)))
            refresh_required = True
            return decision(mode, reason, command, cap)
        raw_recovery_candidate = self._raw_budget_recovery_candidate(now, grid, current, status, inp)
        reduction_processing = self._reduction_source_processing_hold(now, grid, current, status, inp)
        if reduction_processing is not None:
            cap, house, source_processing_deadline = reduction_processing
            house_basis = "accepted_reduction_owner_during_native_cohort"
            refresh_required = True
            soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            if s.enable_pause_resume and pause:
                return decision("pause", "Pause guard overrides native source-cohort processing.", "pause", emergency=True)
            if s.enable_soft_guard and soft and s.soft_current_amps < cap:
                target = max(0, s.soft_current_amps)
                if target < s.min_charge_amps:
                    target = 0
                    self._restart_required = True
                return decision("soft_limit", "Soft guard overrides native source-cohort processing.",
                    "set_limit", target, s.soft_ttl_seconds, emergency=True)
            return decision("waiting_for_current_cohort", "Preserving only the live accepted decrease while native conductor publications complete; no command, EV credit, lease renewal or new response time.",
                None, cap, self.last_target[3])
        publication_owner = (self._publication_pending_positive_owner(now, grid, current, status)
                             if inp.charger_publication_pending is True else None)
        if publication_owner is not None:
            cap, house = publication_owner
            available = int(floor(min(s.max_charge_amps, s.main_fuse_amps-s.buffer_amps-house)))
            house_basis = "accepted_positive_owner_during_raw_publication"
            refresh_required = True
            self._timer_reached(now, "above_soft_since", False, s.soft_for_seconds)
            self._timer_reached(now, "above_pause_since", False, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            return decision("waiting_for_current_publication", "Retaining only the existing live nominal offer and its prior qualified house anchor while the raw payload commits; no command, response credit or additional response time.",
                            None, cap, self.last_target[3])
        raw_response = self._raw_positive_response(now, grid, current, status)
        raw_reduction_plan = (self._bounded_normal_shedding_plan(
            now, grid, current, house, status, raw_timeout=True)
            if raw_response == "timeout" else None)
        if raw_reduction_plan is not None:
            raw_response = None
        if raw_response in {"invalid", "timeout"}:
            self.reset_timers()
            self._clear_positive_alignment()
            self._restart_required = True
            return decision("unavailable", f"Raw-budget positive response {raw_response}; requesting 0 A.", "set_limit", emergency=True)
        if raw_response == "hold":
            cap, house = self._positive_cap, self._positive_house
            house_basis = "raw_budget_positive_response_anchor"
            refresh_required = True
            soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            if s.enable_pause_resume and pause:
                return decision("pause", "Pause guard overrides raw-budget response alignment.", "pause", emergency=True)
            if s.enable_soft_guard and soft and s.soft_current_amps < cap:
                target = max(0, s.soft_current_amps)
                if target < s.min_charge_amps:
                    target = 0
                    self._restart_required = True
                return decision("soft_limit", "Soft guard overrides raw-budget response alignment.",
                    "set_limit", target, s.soft_ttl_seconds, emergency=True)
            return decision("settling", "Keeping only the accepted raw-budget offer inside its original sample-aged response window; no EV subtraction, rise, resume or lease renewal.", None, cap, self.last_target[3])
        baseline_hold = (None if raw_recovery_candidate is not None else self._clock_baseline_raw_hold(now, grid, current, status))
        if baseline_hold is not None:
            cap, house = baseline_hold
            available, _ = self.calculate_available_l1(grid, 0.0)
            house_basis = "whole_authority_raw_bound_without_clock_credit"
            refresh_required = True
            return decision("low_grid_raw_hold", "Retaining only the existing live offer without a command: fresh raw L1 plus its entire unresolved authority fits the normal buffered budget; the observation clock is still a baseline.",
                            None, cap, self.last_target[3])
        positive_response_owner = self._positive_response_owns_uncertainty(
            now, grid, current, status, previous_grid_ts, previous_charger_ts,
        )
        # A response already owned by an accepted rise must pass through BOTH
        # raw fallbacks to the original alignment handler. Otherwise the first
        # fallback can accept a needless decrease and the second can invent a
        # different lower cap for the very same asynchronous EV-first report.
        low_grid_plan = (None if positive_response_owner else
                         self._low_grid_alignment_plan(now, grid, current, status))
        if low_grid_plan is not None:
            mode, reason, cap, house = low_grid_plan
            headroom = s.main_fuse_amps - s.buffer_amps - house
            available = int(floor(min(s.max_charge_amps, headroom))) if headroom >= s.min_charge_amps else 0
            house_basis = ("verified_import_only_whole_authority_bound" if mode == "low_grid_raw_hold"
                           else "verified_lower_cap_response_anchor" if mode == "low_grid_alignment_complete"
                           else "verified_import_only_low_grid_bound")
            refresh_required = mode in {"low_grid_alignment", "low_grid_raw_hold", "low_grid_grid_wait"}
            result = decision(mode, reason, None if mode == "low_grid_grid_wait" else "set_limit", cap, emergency=True)
            if mode == "low_grid_alignment" and result.should_call_service:
                if (self._low_grid_attempt_at is not None
                        and now - self._low_grid_attempt_at < s.min_seconds_between_actions):
                    result.should_call_service = False
            return result
        if self._low_grid_pending and self._low_grid_phase == "reduction":
            refresh_required = True
        bounded_plan = raw_reduction_plan or self._bounded_normal_shedding_plan(now, grid, current, house, status)
        if bounded_plan is not None:
            mode, reason, command, cap, house, ttl = bounded_plan
            house_basis = ("nominal_response_verified" if mode == "normal_shedding_complete"
                           else "raw_grid_bounded_normal_response")
            refresh_required = mode in {"normal_shedding", "normal_shedding_sync", "normal_shedding_grid_wait"}
            result = decision(mode, reason, command, cap, ttl, emergency=True)
            if mode == "normal_shedding" and result.should_call_service:
                if (self._normal_shed_attempt_at is not None
                        and now - self._normal_shed_attempt_at < s.min_seconds_between_actions):
                    result.should_call_service = False
            return result
        if (self._uncertainty_ceiling is not None and not self._restart_required
                and self.last_target is not None and self.last_target[0] > 0
                and (self.last_set_limit_ts is None
                     or not 0 <= now - self.last_set_limit_ts < self.last_target[3])):
            self.reset_timers()
            self._clear_positive_alignment()
            self._restart_required = True
            return decision("unavailable", "The accepted uncertainty-mode lease expired; requesting 0 A before a qualified capped restart.",
                            "set_limit", emergency=True)
        self._observe_uncertainty_recovery(now, grid, current, status)
        uncertain = (not self._last_input_current_trusted or current > grid + 1
                     or (self._positive_cap is not None and current > self._positive_cap + 0.5)
                     or self._uncertainty_ceiling is not None)
        if uncertain and not positive_response_owner:
            cap = self._uncertainty_raw_cap(grid, current, status)
            if cap is not None:
                soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
                pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
                self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
                self._increase_since = None
                available, house = self.calculate_available_l1(grid, 0.0)
                house_basis = "verified_import_only_raw_bound"
                refresh_required = True
                if s.enable_pause_resume and pause:
                    return decision("pause", "Pause guard overrides measurement-uncertainty fallback.", "pause", emergency=True)
                if s.enable_soft_guard and soft:
                    cap = min(cap, max(0, s.soft_current_amps))
                    if cap < s.min_charge_amps:
                        self._restart_required = True
                        return decision("soft_limit", "Soft guard overrides measurement-uncertainty fallback.",
                                        "set_limit", 0, s.soft_ttl_seconds, emergency=True)
                    self._uncertainty_ceiling = min(self._uncertainty_ceiling, cap)
                result = decision("measurement_uncertainty", "Holding only a lower nominal offer under the explicit import-only raw-current assumption; no EV subtraction, increase or resume.",
                                  "set_limit", cap, self.last_target[3])
                # Same failed proposal cannot be retried on every source event.
                # A genuinely smaller protective offer can still be sent now.
                if (result.should_call_service and self._uncertainty_last_attempt is not None
                        and cap >= self._uncertainty_last_attempt_cap
                        and now - self._uncertainty_last_attempt < s.min_seconds_between_actions):
                    result.should_call_service = False
                elif result.should_call_service and not result.dry_run:
                    self._uncertainty_last_attempt = now
                    self._uncertainty_last_attempt_cap = cap
                return result
        if not self._last_input_current_trusted:
            if s.overload_enabled and (self._overload_transition_started is not None or self._overload_hold_active):
                self._fail_overload("untrusted_current_during_overload")
                self._clear_overload_transition()
                self._overload_hold_active = False
            self.reset_timers()
            self._clear_positive_alignment()
            self._restart_required = True
            return decision("unavailable", "Charger current source is not trusted; no EV credit or restart is permitted. Requesting 0 A.",
                            "set_limit", emergency=True)
        if not current_fresh:
            refresh_required = True
        if self._restart_required:
            # A requested zero is terminal even if its API call failed. Old
            # accepted offers must not bypass the normal restart observation
            # through a later favorable/reverted source snapshot.
            self._clear_positive_alignment()
            if self._nominal_shedding_started is not None:
                self._awaiting_reduction_target = None
                self._house_before_reduction = None
                self._settle_started = None
                self._clear_partial_reduction_alignment()
        raw_zero_restart = self._raw_zero_restart_allowed(grid, status, current)
        raw_zero_fresh = bool(raw_zero_restart and (self._raw_clock_zero_observation(grid, status, current) or (
                              self._last_input_wall_now_ts is not None
                              and self._last_input_charger_ts is not None
                              and self._last_input_wall_now_ts - self._last_input_charger_ts <= s.max_sensor_age_seconds
                              and self._ev_clock_credit and self._event_source_usable())))
        if not raw_zero_restart and self._raw_zero_restart_cap is not None:
            self._raw_zero_restart_cap = None
            self._restart_ready_since = None
        if (self._nominal_shedding_started is not None and self.last_target is not None
                and self._awaiting_reduction_target is None
                and self._accepted_reduction_cap == self.last_target[0]
                and self._meaningfully_charging(status, current) and current_fresh
                and current <= self.last_target[0] + 0.5
                and grid <= s.main_fuse_amps - s.buffer_amps
                and self._last_input_grid_ts > self._last_input_charger_ts):
            self._nominal_shedding_started = self._nominal_shedding_budget = None
        if self._normal_grid_first_hold(now, grid, current, house, status):
            # The normal raw envelope is already below every experimental
            # ceiling. Observe custom guards before retaining the old offer.
            soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            self._increase_since = None
            cap = self._positive_cap
            assert cap is not None and self.last_target is not None and self._positive_house is not None
            house = self._positive_house
            house_basis = "prior_coherent_house_grid_first_hold"
            available = int(floor(min(s.max_charge_amps, s.main_fuse_amps - s.buffer_amps - house)))
            if s.enable_pause_resume and pause:
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.",
                                "pause", emergency=True)
            if s.enable_soft_guard and soft and s.soft_current_amps < cap:
                soft_target = max(0, s.soft_current_amps)
                if soft_target < s.min_charge_amps:
                    soft_target = 0
                if soft_target == 0:
                    self._restart_required = True
                    self._positive_grid_first_closed = True
                return decision("soft_limit", "Soft guard overrides nominal grid-first alignment.",
                                "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
            refresh_required = True
            return decision("settling", "Holding only the accepted nominal offer briefly while a grid-first rise awaits fresh EV evidence; no increase or resume.",
                            "set_limit", cap, self.last_target[3])
        overload = self._overload_plan(now, grid, current, house, status)
        if overload is not None:
            mode, reason, target, house = overload
            # Overload leases must be one minute. The controller additionally
            # refuses a positive lease too close to budget/deadline expiry.
            return decision(mode, reason, None if mode in {"overload_grid_wait", "overload_peak_hold"} else "set_limit", target, 60, emergency=True)
        physically_charging = self._meaningfully_charging(status, current)
        if physically_charging:
            self._waiting_for_vehicle_since = None
        elif (self.last_target is not None and self.last_target[0] > 0
              and self._waiting_for_vehicle_since is None):
            self._waiting_for_vehicle_since = now
        # A retained positive EV value during a waiting state is not permission
        # to subtract it from the household load and offer a higher start cap.
        if not physically_charging:
            available, house = self.calculate_available_l1(grid, 0.0)
            house_basis = "raw_grid_waiting_vehicle"
        self._snapshot_aligned = self._rebase_positive_house(
            now, grid, house, current, status, inp.charger_snapshot_verified,
        )
        if self._snapshot_aligned:
            house_basis = ("raw_grid_verified_waiting" if not physically_charging
                           else "verified_snapshot_then_grid")
        alignment = self._positive_alignment(now, grid, current, house, status)
        if alignment in {"timeout", "excess_current"}:
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", f"Positive-current measurement alignment {alignment}; requesting 0 A.",
                            "set_limit", emergency=True)
        if alignment == "safe_hold":
            assert self._positive_cap is not None and self.last_target is not None
            # Report the deliberately conservative estimate used for this
            # hold. Actual EV current is displayed separately and is never
            # subtracted to justify the held cap or any higher permission.
            house = grid
            house_basis = "raw_grid_alignment_safe_hold"
            raw_available = s.main_fuse_amps - grid - s.buffer_amps
            available = (int(floor(min(raw_available, s.max_charge_amps)))
                         if raw_available >= s.min_charge_amps else 0)
            self._increase_since = None
            soft_during_alignment = self._timer_reached(
                now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause_during_alignment = self._timer_reached(
                now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            if (s.enable_pause_resume and pause_during_alignment
                    and status in {"charging", "start_charging"}):
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.", "pause")
            if s.enable_soft_guard and soft_during_alignment and s.soft_current_amps < self._positive_cap:
                soft_target = max(0, s.soft_current_amps)
                if soft_target < s.min_charge_amps:
                    soft_target = 0
                return decision("soft_limit", "Soft guard overrides nominal safe alignment hold.",
                                "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
            return decision(
                "waiting_for_aligned_sources",
                f"Alignment time elapsed; holding accepted {self._positive_cap} A because raw L1 plus the full cap "
                "fits the normal buffered fuse budget. No increase or resume until post-response reports align.",
                "set_limit", self._positive_cap, self.last_target[3],
            )
        if alignment == "hold":
            assert self._positive_cap is not None and self.last_target is not None
            if self._positive_ev_first_anchor_hold(
                    now, grid, current, status, previous_grid_ts, previous_charger_ts):
                # This EV-first observation belongs to the accepted rise.
                # Credit no mixed-pair subtraction and grant no higher cap,
                # resume, fresh response timer or experimental allowance.
                house = self._positive_house
                house_basis = "prior_coherent_house_ev_first_hold"
                self._positive_anchor_hold_used = True
            else:
                house = max(house, self._positive_house or 0.0)
                house_basis = "positive_response_anchor"
            raw_available = s.main_fuse_amps - house - s.buffer_amps
            available = (int(floor(min(raw_available, s.max_charge_amps)))
                         if raw_available >= s.min_charge_amps else 0)
            headroom_exceeded = house + self._positive_cap > s.main_fuse_amps - s.buffer_amps
            # Still observe real grid guards during the hold. No resume or new
            # rise is reachable until post-response source reports agree.
            soft_during_alignment = self._timer_reached(
                now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause_during_alignment = self._timer_reached(
                now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            self._increase_since = None
            if (s.enable_pause_resume and pause_during_alignment
                    and status in {"charging", "start_charging"}):
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.",
                                "pause", emergency=headroom_exceeded)
            soft_target = None
            if s.enable_soft_guard and soft_during_alignment and s.soft_current_amps < self._positive_cap:
                soft_target = max(0, s.soft_current_amps)
                if soft_target < s.min_charge_amps:
                    soft_target = 0
                if soft_target == 0:
                    return decision("soft_limit", "Soft guard overrides positive-current alignment.",
                                    "set_limit", 0, s.soft_ttl_seconds, emergency=True)
            if headroom_exceeded:
                # Do not subtract an unaligned EV value to grant this lower
                # cap. Even if the entire measured grid were household load,
                # the reduced offer must fit the full normal operating buffer.
                raw_target = int(floor(min(s.max_charge_amps,
                    s.main_fuse_amps - s.buffer_amps - grid)))
                target = min(raw_target, soft_target) if soft_target is not None else raw_target
                receipts_valid = (
                    self._last_input_grid_ts is not None and self._positive_grid_ts is not None
                    and self._last_input_grid_ts >= self._positive_grid_ts
                    and self._last_input_charger_ts is not None and self._positive_ev_ts is not None
                    and not self._ev_regressed_since(self._positive_ev_ts)
                    and self._fast_sources_fresh())
                remaining = min(s.measurement_settle_seconds, s.max_sensor_age_seconds) - (
                    self._positive_alignment_used_seconds + max(0.0, now - self._positive_skew_since))
                if (self._meaningfully_charging(status, current) and receipts_valid and remaining > 0
                        and s.min_charge_amps <= target < self.last_target[0]
                        and self.last_target[0] == self._positive_cap):
                    house = grid
                    house_basis = "raw_grid_positive_reduction"
                    available = max(0, raw_target)
                    soft_wins = soft_target is not None and soft_target <= raw_target
                    return decision(
                        "soft_limit" if soft_wins else "alignment_shedding",
                        f"Reducing the accepted offer to {target} A while sources align; raw L1 plus the full lower cap "
                        "fits the normal buffered budget. No increase, resume or extra alignment time.",
                        "set_limit", target, s.soft_ttl_seconds if soft_wins else self.last_target[3],
                        emergency=True)
                self._clear_positive_alignment()
                self.reset_timers()
                self._restart_required = True
                return decision("unavailable", "Positive-current alignment exceeds buffered headroom; requesting 0 A.",
                                "set_limit", emergency=True)
            if soft_target is not None:
                return decision("soft_limit", "Soft guard overrides positive-current alignment.",
                                "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
            return decision("settling", "Holding the accepted positive cap briefly while EV and grid reports align.",
                            "set_limit", self._positive_cap, self.last_target[3])
        if self._positive_cap is not None:
            if self._positive_house is not None and self._positive_house > house:
                house_basis = "positive_cap_house_envelope"
                refresh_required = True
            house = max(house, self._positive_house or 0.0)
            raw_available = s.main_fuse_amps - house - s.buffer_amps
            available = (int(floor(min(raw_available, s.max_charge_amps)))
                         if raw_available >= s.min_charge_amps else 0)
        if self._restore_late_reduction(current, status):
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", "Charger current exceeds the known pre-reduction offer; requesting 0 A.",
                            "set_limit", emergency=True)
        accepted_lower_cap = self._awaiting_reduction_target
        if (accepted_lower_cap is not None and accepted_lower_cap > 0
                and not s.min_charge_amps <= accepted_lower_cap <= s.max_charge_amps):
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", "The accepted lower cap is outside the configured current bounds; requesting 0 A.",
                            "set_limit", emergency=True)
        self._observe_reduction_current(current)
        partial_reduction = self._partial_reduction_alignment(now, grid, current, status)
        if partial_reduction == "timeout":
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", "Partial-current reduction alignment timed out; requesting 0 A.",
                            "set_limit", emergency=True)
        if partial_reduction == "hold":
            cap = self._awaiting_reduction_target
            assert cap is not None and self.last_target is not None
            # Keep the coherent pre-reduction estimate for diagnostics. No
            # increase or resume is reachable from this mixed snapshot.
            house = self._house_before_reduction
            assert house is not None
            raw_available = s.main_fuse_amps - house - s.buffer_amps
            available = (int(floor(min(raw_available, s.max_charge_amps)))
                         if raw_available >= s.min_charge_amps else 0)
            self._increase_since = None
            soft_during_alignment = self._timer_reached(
                now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
            pause_during_alignment = self._timer_reached(
                now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
            self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
            if s.enable_pause_resume and pause_during_alignment:
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.",
                                "pause", emergency=self._nominal_overload_blocked)
            if s.enable_soft_guard and soft_during_alignment and s.soft_current_amps < cap:
                soft_target = max(0, s.soft_current_amps)
                if soft_target < s.min_charge_amps:
                    soft_target = 0
                return decision("soft_limit", "Soft guard overrides partial-current reduction alignment.",
                                "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
            return decision("settling", "Waiting briefly for the grid report after observed partial EV reduction.",
                            "set_limit", cap, self.last_target[3])
        if current > grid + 1:
            # A locally aged numeric fallback can look inconsistent even while
            # every raw-grid observation remains safe. It still requests zero:
            # preserve ONLY an already established raw observation clock, not
            # any permission to charge or evidence of physical zero response.
            raw_observation = None
            if raw_zero_restart and not raw_zero_fresh:
                raw_available, _ = self.calculate_available_l1(grid, 0.0)
                self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
                self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
                raw_observation = (self._restart_ready_since,
                                   min(self._raw_zero_restart_cap, raw_available),
                                   self.above_soft_since, self.above_pause_since)
            # The grid can report the reduction before the EV source does.
            # Hold only our accepted lower cap, with enough headroom even if
            # the actual EV draw is already zero. An inconsistent subtraction
            # must never create permission for an increase or a resume.
            cap = self._awaiting_reduction_target
            reserve = min(s.buffer_amps, max(0.0, s.minimum_reserve_amps))
            grid_updated = (self._last_input_grid_ts is not None
                            and self._reduction_grid_ts is not None
                            and self._last_input_grid_ts > self._reduction_grid_ts)
            ev_not_updated = (self._last_input_charger_ts is not None
                              and self._reduction_charger_ts is not None
                              and self._last_input_charger_ts == self._reduction_charger_ts)
            late_high_can_wait = self._late_high_reduction_can_wait(grid, current, status)
            if (cap is not None and cap > 0 and self.last_target is not None
                    and s.min_charge_amps <= cap <= s.max_charge_amps
                    and self.last_target[0] == cap and grid_updated
                    and ((ev_not_updated and grid + cap <= s.main_fuse_amps - reserve)
                         or late_high_can_wait)):
                if self._settle_started is None:
                    self._begin_reduction_alignment(now, guarded=late_high_can_wait)
                elif late_high_can_wait:
                    self._begin_reduction_alignment(now, guarded=True)
                if self._reduction_alignment_remaining(now) > 0:
                    # This is a new valid grid report. Keep its continuous
                    # observations current even though mixed EV data must not
                    # trigger a pause, resume, or increase from this snapshot.
                    soft_during_alignment = self._timer_reached(
                        now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
                    pause_during_alignment = self._timer_reached(
                        now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
                    self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
                    self._increase_since = None
                    if (s.enable_pause_resume and pause_during_alignment
                            and status in {"charging", "start_charging"}):
                        return decision("pause", "L1 has exceeded the pause threshold for its full observation period.", "pause")
                    if s.enable_soft_guard and soft_during_alignment and s.soft_current_amps < cap:
                        soft_target = max(0, s.soft_current_amps)
                        if soft_target < s.min_charge_amps:
                            soft_target = 0
                        return decision("soft_limit", "Soft guard overrides current-reduction alignment.",
                                        "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
                    if late_high_can_wait:
                        house = grid
                        house_basis = "accepted_lower_cap_raw_grid_hold"
                        available, _ = self.calculate_available_l1(grid, 0.0)
                    return decision("settling", "Holding the accepted lower cap briefly after observed grid reduction; no increase or resume.",
                                    "set_limit", cap, self.last_target[3])
            self.reset_timers()
            if raw_observation is not None:
                (self._restart_ready_since, self._raw_zero_restart_cap,
                 self.above_soft_since, self.above_pause_since) = raw_observation
            self._restart_required = True
            return decision("unavailable", "Charger and grid currents are inconsistent; cannot estimate house load safely. "
                            "Requesting 0 A; cloud control is best effort.", "set_limit", emergency=True)
        if self._awaiting_reduction_target is not None and not raw_zero_restart:
            if (current <= self._awaiting_reduction_target + 0.5
                    and self._reduction_low_is_response(self._awaiting_reduction_target, current, status)):
                # An EV event can arrive before the corresponding grid event.
                # For a strictly bounded interval keep the already accepted
                # lower cap; subtracting the new EV current from the old grid
                # current would manufacture a sudden household load increase.
                grid_not_updated = (self._last_input_grid_ts is not None
                                    and self._reduction_grid_ts is not None
                                    and self._last_input_grid_ts <= self._reduction_grid_ts)
                ev_updated = (self._last_input_charger_ts is not None
                              and self._reduction_charger_ts is not None
                              and self._ev_advanced_since(self._reduction_charger_ts, "_reduction_charger_ts"))
                grid_before_under_cap_response = (
                    self._reduction_under_cap_baseline and self._last_input_grid_ts is not None
                    and self._reduction_low_response_ts is not None
                    and self._last_input_grid_ts <= self._reduction_low_response_ts)
                if (grid_not_updated or grid_before_under_cap_response) and ev_updated:
                    if self._settle_started is None:
                        self._begin_reduction_alignment(now, guarded=self._reduction_under_cap_baseline)
                    if self._reduction_alignment_remaining(now) > 0:
                        self._increase_since = None
                        cap = self._awaiting_reduction_target
                        ttl = self.last_target[3] if self.last_target else s.normal_ttl_seconds
                        soft_during_alignment = self._timer_reached(
                            now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
                        pause_during_alignment = self._timer_reached(
                            now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
                        self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
                        if (s.enable_pause_resume and pause_during_alignment
                                and status in {"charging", "start_charging"}):
                            return decision("pause", "L1 has exceeded the pause threshold for its full observation period.",
                                            "pause", emergency=self._nominal_overload_blocked)
                        if s.enable_soft_guard and soft_during_alignment and s.soft_current_amps < cap:
                            soft_target = max(0, s.soft_current_amps)
                            if soft_target < s.min_charge_amps:
                                soft_target = 0
                            return decision("soft_limit", "Soft guard overrides current-reduction alignment.",
                                            "set_limit", soft_target, s.soft_ttl_seconds, emergency=True)
                        return decision("settling", "Waiting briefly for the grid report after observed EV reduction.",
                                        "set_limit", cap, ttl)
                    self.reset_timers()
                    self._restart_required = True
                    return decision("unavailable", "Current-reduction measurement alignment timed out; requesting 0 A.",
                                    "set_limit", emergency=True)
                if self._awaiting_reduction_target > 0:
                    self._finish_reduction_alignment(now)
                else:
                    self._awaiting_reduction_target = None
                    self._house_before_reduction = None
                    self._settle_started = None
                    self._clear_partial_reduction_alignment()
            else:
                # With asynchronous sources the subtraction can temporarily
                # understate household load. Never recover headroom from the
                # accepted command alone while its reduction is unobserved.
                house = max(house, self._house_before_reduction or 0.0)
                house_basis = "pending_reduction_house_envelope"
                raw_available = s.main_fuse_amps - house - s.buffer_amps
                available = (int(floor(min(raw_available, s.max_charge_amps)))
                             if raw_available >= s.min_charge_amps else 0)
        if raw_zero_restart:
            # Pending zero remains unconfirmed until a positive submission is
            # actually accepted. The new offer credits NONE of its residual
            # current and must fit the entire normal buffer for sixty seconds.
            available, house = self.calculate_available_l1(grid, 0.0)
            house_basis = "raw_grid_unconfirmed_zero_restart"
        soft = self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
        pause = self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
        resume = self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
        if (self.paused_by_us and not self._pause_observed
                and self._pause_submission_ts is not None
                and now - self._pause_submission_ts > 30):
            # An accepted request without prompt state feedback must not claim
            # ownership of a person's later independent pause.
            self.paused_by_us = False
            self._pause_submission_ts = None
        if self.paused_by_us and status in ALLOWED_RESUME_STATUSES:
            self._pause_observed = True
        elif self.paused_by_us and self._pause_observed and status == "charging":
            self.paused_by_us = False
            self._pause_observed = False
            self._pause_submission_ts = None

        if s.enable_pause_resume and pause and status in {"charging", "start_charging"}:
            if not self.paused_by_us or status == "charging":
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.",
                                "pause", emergency=self._nominal_overload_blocked)

        if not s.enable_dynamic_limit:
            return decision("monitor", "Dynamic limiting is disabled; automatic resume is inhibited.")

        if raw_zero_restart and s.enable_pause_resume and pause:
            self._restart_ready_since = None
            self._raw_zero_restart_cap = None
            return decision("restart_wait", "The raw-grid pause guard inhibits an unconfirmed-zero restart.",
                            "set_limit", 0, emergency=True)

        target = available
        if (not current_fresh and not raw_zero_restart
                and (self.last_target is None or target > self.last_target[0])):
            raw_available, raw_house = self.calculate_available_l1(grid, 0.0)
            base = self.last_target[0] if self.last_target is not None else 0
            if raw_available > base or self.last_target is None:
                target = min(target, raw_available)
                available, house = target, raw_house
                house_basis = "raw_grid_unverified_ev"
            else:
                # Retain only the accepted cap; a live stream alone cannot
                # turn an old large current into credit for a new increase.
                target = min(target, base)
                available = target
                house_basis = "accepted_cap_unverified_ev_no_increase"
        ttl = s.normal_ttl_seconds
        mode = "normal"
        reason = f"Calculated charge headroom is {available} A."
        if self._nominal_overload_blocked:
            reason += f" Nominal limiting only; experimental overload remains blocked: {self.effective_overload_failure_reason}."
        if s.enable_soft_guard and soft:
            target = min(target, max(0, s.soft_current_amps))
            if 0 < target < s.min_charge_amps:
                target = 0
            ttl = s.soft_ttl_seconds
            mode = "soft_limit"
            reason = f"Soft guard caps the available headroom at {target} A."

        original_target = target
        if self._uncertainty_ceiling is not None:
            target = min(target, self.startup_current if self._uncertainty_restart else self._uncertainty_ceiling)
            original_target = target
        raw_budget_rise = raw_recovery_candidate is not None and target == raw_recovery_candidate
        if raw_budget_rise:
            house, house_basis = grid, "raw_grid_bounded_recovery"
        target = self._limit_with_continuity(
            now, target, available, house, current, status,
            allow_minimum=not (s.enable_soft_guard and soft and s.soft_current_amps < s.min_charge_amps),
            raw_zero_restart=raw_zero_restart,
            raw_zero_fresh=raw_zero_fresh,
            raw_budget_rise=raw_budget_rise,
        )
        if target > original_target:
            mode = "minimum_continuity"
            reason = (f"Retaining {target} A within the fuse budget and minimum reserve; "
                      "using part of the normal buffer.")
        elif target == 0 and original_target >= s.min_charge_amps:
            mode = "restart_wait"
            reason = "Waiting for continuously stable buffered headroom before restarting at minimum current."
        elif 0 < target < original_target:
            mode = "ramp_up"
            reason = f"Limiting recovery to {target} A while load stabilizes and current rises gradually."
        if self._fast_recovery_wait is not None:
            mode = ("waiting_for_fresh_sources" if self._fast_recovery_wait == "fresh"
                    else "waiting_for_aligned_sources")
            reason = ("Fast recovery is waiting for fresh, coherent grid and EV reports; keeping the accepted safe cap."
                      if self._fast_recovery_wait == "fresh" else
                      "Fast recovery is waiting for the accepted current change to be observed and aligned; keeping the safe cap.")

        if not physically_charging:
            startup = self.startup_current
            if not self._start_allowed(status):
                mode = "waiting_for_vehicle"
                reason = f"Retaining a safe cap while charger status '{status}' controls authorization, schedule or pause."
            elif available < startup or (0 < target < startup):
                mode = "start_headroom_wait"
                reason = f"Waiting for full buffered headroom for the configured {startup} A start; no stale EV current is subtracted."
            elif target > 0:
                stalled = (self._waiting_for_vehicle_since is not None
                           and now - self._waiting_for_vehicle_since >= START_RESPONSE_DIAGNOSTIC_SECONDS)
                mode = "waiting_for_vehicle_stalled" if stalled else (
                    "starting" if self.last_target is None or self.last_target[0] != target else "waiting_for_vehicle")
                reason = (f"Keeping a stable {target} A start offer; waiting for actual charging. "
                          "No automatic start/stop retry is issued.")

        if (s.enable_pause_resume and self.paused_by_us and status in ALLOWED_RESUME_STATUSES
                and self._start_allowed(status) and resume):
            if not current_fresh:
                refresh_required = True
                return decision(
                    "waiting_for_fresh_sources",
                    "Resume inhibited until a fresh coherent current/status source is verified; keeping the safe cap.",
                    "set_limit", target, ttl,
                )
            if target < s.min_charge_amps:
                return decision("paused", "Resume inhibited: insufficient verified charging headroom.", "set_limit", 0, ttl)
            if target < self.startup_current or available < self.startup_current:
                return decision(
                    "start_headroom_wait",
                    f"Resume inhibited: the configured {self.startup_current} A startup requires full buffered headroom and a safe accepted start cap.",
                    "set_limit", target, ttl,
                )
            fresh_cap = (self.last_target == (target, 0, 0, ttl)
                         and self.last_set_limit_ts is not None
                         and now - self.last_set_limit_ts < ttl / 2)
            if fresh_cap:
                return decision("resume", "Our pause may resume after stable low load and an accepted safe cap.", "resume", target, ttl)
        return decision(mode, reason, "set_limit", target, ttl)
