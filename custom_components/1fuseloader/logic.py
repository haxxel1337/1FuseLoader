from __future__ import annotations

from dataclasses import dataclass
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
    # HA report receipt timestamps, not guaranteed device acquisition times.
    grid_report_ts: Optional[float] = None
    charger_report_ts: Optional[float] = None
    # UTC epoch clock for explicit report-age checks. now_ts stays monotonic.
    wall_now_ts: Optional[float] = None
    # The effective current/status pair was read in one authenticated cloud GET.
    # charger_report_ts is its receipt time, never a physical acquisition time.
    charger_snapshot_verified: bool = False


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


class OneFuseRegulator:
    """Plan commands; acknowledge only a successfully submitted adapter request.

    Acknowledgement means the remote API accepted the request. It does not prove
    that the car physically changed its current or operating state.
    """

    def __init__(self, settings: RegulatorSettings | None = None) -> None:
        self.settings = settings or RegulatorSettings()
        self.reset_overload_budget()
        self.reset()
        self.update_settings(self.settings)

    def update_settings(self, settings: RegulatorSettings) -> None:
        if settings.controlled_phase != "L1" or settings.unused_phase_current != 0:
            raise ValueError("Only L1 with unused phase currents of 0 A is supported")
        if type(settings.startup_charge_amps) is not int or not 6 <= settings.startup_charge_amps <= 16:
            raise ValueError("Startup current must be an integer from 6 to 16 A")
        if type(settings.fast_recovery) is not bool:
            raise ValueError("Fast recovery must be a boolean")
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
        self.settings = settings
        self.reset_timers()
        # An options change cannot extend an alignment deadline using old bounds.
        self._clear_positive_alignment()

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
        self._clear_positive_alignment()

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
        self._positive_response_current: Optional[float] = None
        self._positive_skew_since: Optional[float] = None
        self._positive_alignment_used_seconds = 0.0
        self._positive_verified_low_current: Optional[float] = None
        self._positive_verified_low_ts: Optional[float] = None
        self._positive_grid_first_ts: Optional[float] = None
        self._positive_grid_first_closed = False

    def _positive_reports_advanced(self) -> bool:
        return (self._last_input_grid_ts is not None and self._positive_grid_ts is not None
                and self._last_input_grid_ts > self._positive_grid_ts
                and self._last_input_charger_ts is not None and self._positive_ev_ts is not None
                and self._last_input_charger_ts > self._positive_ev_ts)

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
                or self._last_input_charger_ts <= self._positive_ev_ts
                or self._last_input_charger_ts < self._positive_response_ts
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
            and self._last_input_charger_ts > self._reduction_charger_ts
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
                or self._last_input_charger_ts <= self._reduction_charger_ts
                or not self._fast_sources_fresh()):
            return
        if current > self._awaiting_reduction_target + 0.5:
            self._reduction_low_response_ts = None
        if self._reduction_previous_cap is not None and current > self._reduction_previous_cap + 0.5:
            return
        if current > self._reduction_high_current:
            self._reduction_high_current = current
            self._reduction_high_ev_ts = self._last_input_charger_ts

    def _reduction_low_is_response(self, cap: int, current: float, status: str) -> bool:
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
            and self._last_input_charger_ts > self._reduction_charger_ts
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
                or grid_ts < self._reduction_grid_ts or ev_ts < self._reduction_charger_ts
                or status != "charging"):
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
        if ev_ts > self._reduction_charger_ts and (current < low - 0.5 or completed_partial):
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
        return self._numeric_source_rejection() is None

    def _numeric_source_rejection(self) -> Optional[str]:
        """Describe existing numeric freshness checks without weakening them."""
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
                    and (self._reduction_charger_ts is None or ev >= self._reduction_charger_ts))

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
                    or self._last_input_charger_ts <= self._positive_response_ts
                    or self._positive_alignment_used_seconds + elapsed < settle):
                return False
            self._positive_alignment_used_seconds += elapsed
            self._positive_skew_since = None
            self._positive_response_ts = None
        self._positive_house = house
        reference = (self._positive_response_current if self._positive_response_current is not None
                     else self._positive_verified_low_current if self._positive_verified_low_current is not None
                     else self._positive_before_current)
        if reference is not None and current < reference - 0.5:
            self._positive_verified_low_current = current
            self._positive_verified_low_ts = self._last_input_charger_ts
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
        ev_advanced = (self._last_input_charger_ts is not None and self._positive_ev_ts is not None
                       and self._last_input_charger_ts > self._positive_ev_ts)
        reference = (self._positive_verified_low_current if self._positive_verified_low_current is not None
                     else self._positive_before_current or 0.0)
        increased = current > reference + 0.5
        fresh_low_response = (self._positive_verified_low_ts is not None
                              and self._last_input_charger_ts is not None
                              and self._last_input_charger_ts > self._positive_verified_low_ts)
        if (ev_advanced and increased and (self._positive_response_current is None
                or current > self._positive_response_current + 0.5 or fresh_low_response)):
            self._positive_response_ts = self._last_input_charger_ts
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
                              and self._last_input_charger_ts is not None and self._positive_ev_ts is not None
                              and self._last_input_charger_ts >= self._positive_ev_ts
                              and self._last_input_charger_ts >= self._positive_response_ts)
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
            # A coherent partial/tapered current is not a failed start and
            # must not cause timed zero/start retries. Retain eligibility for
            # the rest of this same accepted rise, without renewing any grace.
            return None
        if self._positive_skew_since is None:
            self._positive_skew_since = now
        return "hold"

    def reset_overload_budget(self) -> None:
        """Explicit new exposure state; never call for settings or HA reloads.

        This operational counter does not estimate fuse cooling. Its caller
        must not treat an explicit reset as confirmation of a cold fuse.
        """
        self._overload_used_seconds = 0.0
        self._overload_last_eval_ts: Optional[float] = None
        self._overload_exposure_active = False
        self._overload_failure_reason: Optional[str] = None
        self._overload_hold_active = False
        self._clear_overload_transition()

    def _clear_overload_transition(self) -> None:
        self._overload_transition_started: Optional[float] = None
        self._overload_entry_grid_ts: Optional[float] = None
        self._overload_entry_charger_ts: Optional[float] = None
        self._overload_anchor_house: Optional[float] = None
        self._overload_minimum_response_ts: Optional[float] = None
        self._overload_minimum_accepted_at: Optional[float] = None
        self._overload_minimum_ev_floor: Optional[float] = None
        self._overload_minimum_attempted_at: Optional[float] = None

    def _fail_overload(self, reason: str) -> None:
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
        }

    def import_overload_state(self, state: object, elapsed_seconds: float = 0.0) -> bool:
        """Restore exposure, charging unknown downtime to a preceding overload.

        Monotonic transition deadlines cannot survive a process restart. A
        pending response is therefore latched as interrupted, never renewed.
        Invalid persisted state fails closed instead of refunding exposure.
        """
        used = _valid_number(state.get("used_seconds")) if isinstance(state, dict) else None
        gap = _valid_number(elapsed_seconds)
        valid = (isinstance(state, dict) and state.get("version") == 1
                 and used is not None and used >= 0 and not isinstance(state.get("used_seconds"), bool)
                 and type(state.get("exposure_active")) is bool
                 and type(state.get("transition_pending")) is bool
                 and type(state.get("minimum_held")) is bool
                 and (state.get("failure_reason") is None or isinstance(state.get("failure_reason"), str))
                 and gap is not None and gap >= 0 and not isinstance(elapsed_seconds, bool))
        self._clear_overload_transition()
        self._overload_hold_active = False
        self._overload_last_eval_ts = None
        if not valid:
            self._overload_used_seconds = float(self.settings.overload_budget_seconds)
            self._overload_exposure_active = True
            self._overload_failure_reason = "invalid_persisted_exposure"
            return False
        assert isinstance(state, dict) and used is not None and gap is not None
        self._overload_used_seconds = used + (gap if state["exposure_active"] else 0.0)
        self._overload_exposure_active = state["exposure_active"]
        self._overload_hold_active = state["minimum_held"]
        self._overload_failure_reason = state.get("failure_reason")
        if state["transition_pending"]:
            self._fail_overload("interrupted_transition")
        return True

    def _observe_overload_time(self, now: Optional[float], grid: Optional[float], valid: bool) -> bool:
        """Charge left-held observed overload intervals, including transitions.

        Missing/invalid data does not turn the previous known overload into a
        cooling credit. Disabling control does not refund or freeze exposure.
        """
        if now is None or (self._overload_last_eval_ts is not None and now < self._overload_last_eval_ts):
            self._fail_overload("invalid_or_backward_clock")
            return False
        if self._overload_last_eval_ts is not None and self._overload_exposure_active:
            self._overload_used_seconds += now - self._overload_last_eval_ts
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
        ceiling = s.main_fuse_amps + s.overload_margin_amps
        reserve = min(s.buffer_amps, max(0.0, s.minimum_reserve_amps))
        needed = house + s.min_charge_amps > s.main_fuse_amps - reserve or grid > ceiling
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
        if self._overload_failure_reason is not None and (needed or grid > s.main_fuse_amps or pending):
            if (pending
                    or not self._blocked_overload_allows_nominal_reduction(now, grid, current, house, status)):
                return stop(self._overload_failure_reason)
            return None
        if (needed or pending) and self._overload_used_seconds >= s.overload_budget_seconds:
            return stop("cumulative_budget_exhausted")
        if (needed or pending) and (self._last_input_grid_ts is None or self._last_input_charger_ts is None):
            return stop("missing_report_timestamps")

        if pending:
            assert self._overload_transition_started is not None
            if now - self._overload_transition_started >= s.overload_response_seconds:
                return stop("response_timeout")
            grid_advanced = (self._last_input_grid_ts is not None and self._overload_entry_grid_ts is not None
                             and self._last_input_grid_ts > self._overload_entry_grid_ts)
            ev_advanced = (self._overload_minimum_accepted_at is not None
                           and self._last_input_charger_ts is not None
                           and self._overload_minimum_ev_floor is not None
                           and self._last_input_charger_ts > self._overload_minimum_ev_floor)
            if (self._meaningfully_charging(status, current)
                    and current <= s.min_charge_amps + 0.5 and ev_advanced):
                if self._overload_minimum_response_ts is None:
                    self._overload_minimum_response_ts = self._last_input_charger_ts
            else:
                self._overload_minimum_response_ts = None
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
            if (current <= s.min_charge_amps + 0.5 and grid_advanced and ev_advanced
                    and grid_at_or_after_minimum and grid > ceiling):
                return stop("minimum_observed_above_ceiling")
            if (current <= s.min_charge_amps + 0.5 and grid <= ceiling
                    and grid_advanced and ev_advanced and grid_after_minimum):
                if current > grid + 1 or house + s.min_charge_amps > ceiling:
                    return stop("minimum_response_outside_ceiling")
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
                        s.min_charge_amps, anchor)

        if not needed:
            self._overload_hold_active = False
            return None
        if current > grid + 1:
            return stop("inconsistent_current_observations")
        if house + s.min_charge_amps > ceiling:
            return stop("minimum_projection_exceeds_ceiling")
        # A low observation can predate a higher offer that the API has already
        # accepted. It cannot prove that a NEW reduction to minimum responded.
        # Keep the same bounded shedding path until the accepted minimum has
        # both a later EV receipt and a still later coherent grid receipt.
        higher_offer = self.last_target is not None and self.last_target[0] > s.min_charge_amps
        if current <= s.min_charge_amps + 0.5 and not higher_offer:
            if grid > ceiling:
                return stop("minimum_observed_above_ceiling")
            self._overload_hold_active = True
            self._increase_since = None
            return ("overload_minimum", "Retaining minimum current inside the opt-in post-response envelope and cumulative time budget.",
                    s.min_charge_amps, house)
        if self._overload_hold_active:
            return stop("minimum_response_lost")
        self._overload_transition_started = now
        self._overload_entry_grid_ts = self._last_input_grid_ts
        self._overload_entry_charger_ts = self._last_input_charger_ts
        self._overload_anchor_house = house
        self._increase_since = None
        return ("overload_shedding", "Requesting minimum current; a bounded transition starts now, including API latency.",
                s.min_charge_amps, house)

    def acknowledge(self, decision: RegulatorDecision, now_ts: float) -> None:
        """Commit bookkeeping after API submission succeeds, never before it."""
        if not decision.should_call_service or decision.dry_run or decision.command is None:
            return
        self.last_action_ts = now_ts
        self.last_command = decision.command
        if decision.command == "set_limit":
            old_target = self.last_target[0] if self.last_target is not None else None
            target = decision.current_p1
            changed = old_target != target
            same_cap_restart = not changed and target > 0 and self._restart_required
            positive_shedding_budget = None
            if (changed and target > 0 and old_target is not None and target < old_target
                    and self._positive_cap == old_target and self._positive_skew_since is not None):
                budget = min(self.settings.measurement_settle_seconds, self.settings.max_sensor_age_seconds)
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
                        and self._last_input_charger_ts is not None
                        and decision.house_load_l1 + target <= self.settings.main_fuse_amps - self.settings.buffer_amps):
                    self._positive_cap = target
                    self._positive_house = decision.house_load_l1
                    self._positive_grid_ts = self._last_input_grid_ts
                    self._positive_ev_ts = self._last_input_charger_ts
                    self._positive_before_current = decision.charger_current
            if same_cap_restart:
                # The zero may have physically stopped the car without an HTTP
                # ACK. An accepted, fully qualified restart is a new potential
                # positive response even if its cap matches the old lease.
                self._clear_positive_alignment()
                if (target > decision.charger_current + 0.5
                        and self._last_input_grid_ts is not None
                        and self._last_input_charger_ts is not None
                        and decision.house_load_l1 + target <= self.settings.main_fuse_amps - self.settings.buffer_amps):
                    self._positive_cap = target
                    self._positive_house = decision.house_load_l1
                    self._positive_grid_ts = self._last_input_grid_ts
                    self._positive_ev_ts = self._last_input_charger_ts
                    self._positive_before_current = decision.charger_current
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
                self._settle_started = None
                self._clear_partial_reduction_alignment()
                self._awaiting_reduction_target = target
                self._accepted_reduction_cap = target if target > 0 else None
                self._reduction_before_current = decision.charger_current
                self._reduction_previous_cap = previous_cap
                self._reduction_under_cap_baseline = (
                    target > 0 and old_target is not None and old_target > target
                    and decision.charger_current <= target + 0.5)
                self._reduction_high_current = decision.charger_current
                self._reduction_high_ev_ts = self._last_input_charger_ts
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
            if (target == self.settings.min_charge_amps
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
                if self._awaiting_reduction_target == target:
                    deadline = self._overload_transition_started + self.settings.overload_response_seconds
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
            self.last_set_limit_ts = now_ts
            self.last_target = (
                decision.current_p1, decision.current_p2, decision.current_p3, decision.ttl_seconds
            )
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
                               raw_zero_fresh: bool = False) -> int:
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
        if (current == self.settings.min_charge_amps
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
            self._overload_minimum_attempted_at = now
            return True
        if self.last_target != (current, 0, 0, ttl) or self.last_set_limit_ts is None:
            return True
        resend = min(self.settings.resend_limit_every_seconds, ttl / 2)
        return now - self.last_set_limit_ts >= max(1, resend)

    def evaluate(self, inp: RegulatorInput) -> RegulatorDecision:
        s = self.settings
        observed_now = _valid_number(inp.now_ts)
        now = observed_now if observed_now is not None else 0.0
        self._last_input_now_ts = None if isinstance(inp.now_ts, bool) else observed_now
        status = (inp.status or "").strip().lower()
        self._fast_recovery_wait = None
        self._nominal_overload_blocked = False
        self._nominal_rejection_code = None
        self._snapshot_aligned = False
        self._last_input_status = status
        if status != "paused":
            self._resume_submission_ts = None
        grid = _valid_number(inp.grid_l1)
        current = _valid_number(inp.charger_current)
        self._last_input_grid_ts = (None if isinstance(inp.grid_report_ts, bool) else _valid_number(inp.grid_report_ts))
        self._last_input_charger_ts = (None if isinstance(inp.charger_report_ts, bool) else _valid_number(inp.charger_report_ts))
        self._last_input_wall_now_ts = (None if isinstance(inp.wall_now_ts, bool) else _valid_number(inp.wall_now_ts))
        available, house = self.calculate_available_l1(grid, current or 0.0)
        current_fresh = self._fast_sources_fresh()
        refresh_required = False
        house_basis = "grid_minus_reported_ev"
        if self.last_target is None and not current_fresh:
            # Initial takeover cannot be seeded from an old retained EV value.
            available, house = self.calculate_available_l1(grid, 0.0)
            house_basis = "raw_grid_unverified_ev"

        def decision(mode: str, reason: str, command: str | None = None,
                     target: int = 0, ttl: int | None = None, emergency: bool = False) -> RegulatorDecision:
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
            return RegulatorDecision(
                mode=f"dry_run_{mode}" if s.dry_run and mode not in {"idle", "unavailable"} else mode,
                reason=reason, command=command,
                service=("easee.set_circuit_dynamic_limit" if command == "set_limit"
                         else "easee.action_command" if command else None),
                current_p1=target, current_p2=0, current_p3=0, ttl_seconds=ttl_value,
                available_l1=available, house_load_l1=house, grid_l1=grid,
                charger_current=current or 0.0, paused_by_us=self.paused_by_us,
                should_call_service=bool(should_call), dry_run=s.dry_run,
                overload_used_seconds=self._overload_used_seconds,
                overload_remaining_seconds=max(0.0, s.overload_budget_seconds - self._overload_used_seconds),
                overload_transition_remaining_seconds=(
                    max(0.0, s.overload_response_seconds - (now - self._overload_transition_started))
                    if self._overload_transition_started is not None else None),
                overload_failure_latched=self._overload_failure_reason is not None,
                startup_charge_amps=self.startup_current,
                waiting_for_vehicle_seconds=(max(0.0, now - self._waiting_for_vehicle_since)
                                             if self._waiting_for_vehicle_since is not None else 0.0),
                measurement_alignment_remaining_seconds=(
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
            )

        error = inp.data_error
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
        if error:
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
            return decision("idle", f"Status '{status}' does not require charge control.")

        assert grid is not None
        assert current is not None
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
        raw_zero_fresh = bool(raw_zero_restart and self._last_input_wall_now_ts is not None
                              and self._last_input_charger_ts is not None
                              and self._last_input_wall_now_ts - self._last_input_charger_ts <= s.max_sensor_age_seconds)
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
            return decision(mode, reason, "set_limit", target, 60, emergency=True)
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
                    and self._last_input_charger_ts >= self._positive_ev_ts
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
                              and self._last_input_charger_ts > self._reduction_charger_ts)
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
            reason += f" Nominal limiting only; experimental overload remains blocked: {self._overload_failure_reason}."
        if s.enable_soft_guard and soft:
            target = min(target, max(0, s.soft_current_amps))
            if 0 < target < s.min_charge_amps:
                target = 0
            ttl = s.soft_ttl_seconds
            mode = "soft_limit"
            reason = f"Soft guard caps the available headroom at {target} A."

        original_target = target
        target = self._limit_with_continuity(
            now, target, available, house, current, status,
            allow_minimum=not (s.enable_soft_guard and soft and s.soft_current_amps < s.min_charge_amps),
            raw_zero_restart=raw_zero_restart,
            raw_zero_fresh=raw_zero_fresh,
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
