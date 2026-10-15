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


class OneFuseRegulator:
    """Plan commands; acknowledge only a successfully submitted adapter request.

    Acknowledgement means the remote API accepted the request. It does not prove
    that the car physically changed its current or operating state.
    """

    def __init__(self, settings: RegulatorSettings | None = None) -> None:
        self.settings = settings or RegulatorSettings()
        self.reset()
        self.update_settings(self.settings)

    def update_settings(self, settings: RegulatorSettings) -> None:
        if settings.controlled_phase != "L1" or settings.unused_phase_current != 0:
            raise ValueError("Only L1 with unused phase currents of 0 A is supported")
        self.settings = settings
        self.reset_timers()

    def reset_timers(self) -> None:
        self.above_soft_since: Optional[float] = None
        self.above_pause_since: Optional[float] = None
        self.below_resume_since: Optional[float] = None
        self._increase_since: Optional[float] = None
        self._restart_ready_since: Optional[float] = None
        self._previous_desired: Optional[int] = None

    def reset(self) -> None:
        self.reset_timers()
        self.paused_by_us = False
        self._pause_observed = False
        self._pause_submission_ts: Optional[float] = None
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
        self._reduction_grid_ts: Optional[float] = None
        self._reduction_charger_ts: Optional[float] = None
        self._settle_started: Optional[float] = None

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
            if changed:
                self._last_limit_change_ts = now_ts
            if changed and (target < decision.charger_current or
                            (old_target is not None and target < old_target)):
                # A submitted cap is not a measured current. Retain the last
                # house estimate, and inhibit rises until the EV responds.
                self._awaiting_reduction_target = target
                self._house_before_reduction = decision.house_load_l1
                self._reduction_grid_ts = self._last_input_grid_ts
                self._reduction_charger_ts = self._last_input_charger_ts
                self._settle_started = None
                self._increase_since = None
            if changed and (old_target is None or target > old_target):
                self._last_increase_ts = now_ts
            if target == 0:
                self._restart_required = True
            elif self._restart_required:
                self._restart_required = False
                self._restart_ready_since = None
            self.last_set_limit_ts = now_ts
            self.last_target = (
                decision.current_p1, decision.current_p2, decision.current_p3, decision.ttl_seconds
            )
        elif decision.command == "pause":
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
                               allow_minimum: bool = True) -> int:
        """Lower promptly, retain minimum within budget, and raise slowly.

        The reserve may spend part of the normal operating buffer, but never
        intentionally permits total estimated current above the fuse rating.
        """
        s = self.settings
        physically_charging = status == "charging" and current > 0
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
        if self._restart_required:
            self._increase_since = None
            self._previous_desired = desired
            ready = self._timer_reached(
                now, "_restart_ready_since", available >= s.min_charge_amps
                and self._awaiting_reduction_target is None,
                s.restart_stable_seconds,
            )
            return s.min_charge_amps if ready else 0

        if self.last_target is None:
            # On takeover, do not interpret unused headroom as permission for
            # an immediate jump to the maximum current.
            base = max(s.min_charge_amps, int(floor(current)))
            self._increase_since = None
            self._previous_desired = desired
            return min(desired, base)

        base = self.last_target[0]
        if desired <= base:
            self._increase_since = None
            self._previous_desired = desired
            return desired

        load_rose = self._previous_desired is not None and desired < self._previous_desired
        self._previous_desired = desired
        if (not physically_charging or self._awaiting_reduction_target is not None
                or load_rose):
            self._increase_since = None
            return max(s.min_charge_amps, base)

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
        if self.last_target != (current, 0, 0, ttl) or self.last_set_limit_ts is None:
            return True
        resend = min(self.settings.resend_limit_every_seconds, ttl / 2)
        return now - self.last_set_limit_ts >= max(1, resend)

    def evaluate(self, inp: RegulatorInput) -> RegulatorDecision:
        s = self.settings
        now = float(inp.now_ts)
        status = (inp.status or "").strip().lower()
        grid = _valid_number(inp.grid_l1)
        current = _valid_number(inp.charger_current)
        self._last_input_grid_ts = _valid_number(inp.grid_report_ts)
        self._last_input_charger_ts = _valid_number(inp.charger_report_ts)
        available, house = self.calculate_available_l1(grid, current or 0.0)

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
                should_call = should_call and self._action_allowed(now)
            return RegulatorDecision(
                mode=f"dry_run_{mode}" if s.dry_run and mode not in {"idle", "unavailable"} else mode,
                reason=reason, command=command,
                service=("easee.set_circuit_dynamic_limit" if command == "set_limit"
                         else "easee.action_command" if command else None),
                current_p1=target, current_p2=0, current_p3=0, ttl_seconds=ttl_value,
                available_l1=available, house_load_l1=house, grid_l1=grid,
                charger_current=current or 0.0, paused_by_us=self.paused_by_us,
                should_call_service=bool(should_call), dry_run=s.dry_run,
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
        if error:
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", f"{error} Requesting 0 A; cloud control is best effort.",
                            "set_limit", emergency=True)

        if status in IDLE_STATUSES:
            self.paused_by_us = False
            self._pause_observed = False
            self._pause_submission_ts = None
            self.reset_timers()
            self._restart_required = True
            self._awaiting_reduction_target = None
            self._house_before_reduction = None
            return decision("idle", f"Status '{status}' does not require charge control.")

        assert grid is not None
        assert current is not None
        if current > grid + 1:
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
                              and self._last_input_charger_ts <= self._reduction_charger_ts)
            if (cap is not None and cap > 0 and self.last_target is not None
                    and self.last_target[0] == cap and grid_updated and ev_not_updated
                    and grid + cap <= s.main_fuse_amps - reserve):
                if self._settle_started is None:
                    self._settle_started = now
                settle_seconds = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                if now - self._settle_started < settle_seconds:
                    # This is a new valid grid report. Keep its continuous
                    # observations current even though mixed EV data must not
                    # trigger a pause, resume, or increase from this snapshot.
                    self._timer_reached(now, "above_soft_since", grid > s.soft_limit_amps, s.soft_for_seconds)
                    self._timer_reached(now, "above_pause_since", grid > s.pause_limit_amps, s.pause_for_seconds)
                    self._timer_reached(now, "below_resume_since", grid < s.resume_below_amps, s.resume_for_seconds)
                    self._increase_since = None
                    return decision("settling", "Waiting briefly for the EV report after observed grid reduction.",
                                    "set_limit", cap, self.last_target[3])
            self.reset_timers()
            self._restart_required = True
            return decision("unavailable", "Charger and grid currents are inconsistent; cannot estimate house load safely. "
                            "Requesting 0 A; cloud control is best effort.", "set_limit", emergency=True)
        if self._awaiting_reduction_target is not None:
            if current <= self._awaiting_reduction_target + 0.5:
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
                if grid_not_updated and ev_updated:
                    if self._settle_started is None:
                        self._settle_started = now
                    settle_seconds = min(s.measurement_settle_seconds, s.max_sensor_age_seconds)
                    if now - self._settle_started < settle_seconds:
                        self._increase_since = None
                        cap = self._awaiting_reduction_target
                        ttl = self.last_target[3] if self.last_target else s.normal_ttl_seconds
                        return decision("settling", "Waiting briefly for the grid report after observed EV reduction.",
                                        "set_limit", cap, ttl)
                self._awaiting_reduction_target = None
                self._house_before_reduction = None
                self._settle_started = None
            else:
                # With asynchronous sources the subtraction can temporarily
                # understate household load. Never recover headroom from the
                # accepted command alone while its reduction is unobserved.
                house = max(house, self._house_before_reduction or 0.0)
                raw_available = s.main_fuse_amps - house - s.buffer_amps
                available = (int(floor(min(raw_available, s.max_charge_amps)))
                             if raw_available >= s.min_charge_amps else 0)
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
                return decision("pause", "L1 has exceeded the pause threshold for its full observation period.", "pause")

        if not s.enable_dynamic_limit:
            return decision("monitor", "Dynamic limiting is disabled; automatic resume is inhibited.")

        target = available
        ttl = s.normal_ttl_seconds
        mode = "normal"
        reason = f"Calculated charge headroom is {available} A."
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

        if s.enable_pause_resume and self.paused_by_us and status in ALLOWED_RESUME_STATUSES and resume:
            if target < s.min_charge_amps:
                return decision("paused", "Resume inhibited: insufficient verified charging headroom.", "set_limit", 0, ttl)
            fresh_cap = (self.last_target == (target, 0, 0, ttl)
                         and self.last_set_limit_ts is not None
                         and now - self.last_set_limit_ts < ttl / 2)
            if fresh_cap:
                return decision("resume", "Our pause may resume after stable low load and an accepted safe cap.", "resume", target, ttl)
        return decision(mode, reason, "set_limit", target, ttl)
