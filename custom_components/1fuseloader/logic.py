from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Optional

ALLOWED_ACTIVE_STATUSES = {
    "charging",
    "ready_to_charge",
    "waiting_for_schedule",
    "awaiting_start",
    "connected",
    "buffering",
}

ALLOWED_RESUME_STATUSES = {
    "awaiting_start",
    "ready_to_charge",
    "paused",
    "connected",
    "buffering",
}

PAUSED_STATUSES = {"paused"}


def _valid_number(value: object, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, "unknown", "unavailable", "None", ""):
            return default
        val = float(value)  # type: ignore[arg-type]
        if not isfinite(val):
            return default
        return val
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class RegulatorSettings:
    # Same heart as your old template sensor:
    # available = main_fuse - max(0, grid_l1 - charger_current) - buffer
    main_fuse_amps: float = 25.0
    buffer_amps: float = 2.0
    min_charge_amps: int = 6
    max_charge_amps: int = 16
    safe_l1_amps: float = 23.0

    # Guards from the old active automations
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

    # Safety / behaviour switches
    active_control: bool = False
    dry_run: bool = True
    enable_dynamic_limit: bool = True
    enable_soft_guard: bool = True
    enable_pause_resume: bool = True
    force_min_when_available_too_low: bool = True

    # Easee single-phase forcing. Keep P2/P3 at 0 unless you intentionally test another setup.
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
    """Small stateful load regulator for a 1-phase BMW/Easee setup.

    This class is intentionally Home Assistant independent so it can be unit-tested
    from a normal Python script.
    """

    def __init__(self, settings: RegulatorSettings | None = None) -> None:
        self.settings = settings or RegulatorSettings()
        self.above_soft_since: Optional[float] = None
        self.above_pause_since: Optional[float] = None
        self.below_resume_since: Optional[float] = None
        self.paused_by_us: bool = False
        self.last_action_ts: Optional[float] = None
        self.last_set_limit_ts: Optional[float] = None
        self.last_target: Optional[tuple[int, int, int, int]] = None
        self.last_command: Optional[str] = None

    def update_settings(self, settings: RegulatorSettings) -> None:
        self.settings = settings

    def reset(self) -> None:
        self.above_soft_since = None
        self.above_pause_since = None
        self.below_resume_since = None
        self.paused_by_us = False
        self.last_action_ts = None
        self.last_set_limit_ts = None
        self.last_target = None
        self.last_command = None

    def calculate_available_l1(self, grid_l1: Optional[float], charger_current: float) -> tuple[int, float]:
        s = self.settings
        if grid_l1 is None or grid_l1 < 0:
            return 0, 0.0
        house_load_l1 = grid_l1 - max(0.0, charger_current)
        if house_load_l1 < 0:
            house_load_l1 = 0.0
        available = s.main_fuse_amps - house_load_l1 - s.buffer_amps
        if available < s.min_charge_amps:
            return 0, house_load_l1
        return int(floor(min(available, s.max_charge_amps))), house_load_l1

    def _phase_currents(self, controlled_current: int) -> tuple[int, int, int]:
        s = self.settings
        unused = int(max(0, s.unused_phase_current))
        phase = (s.controlled_phase or "L1").upper()
        if phase == "L2":
            return unused, int(controlled_current), unused
        if phase == "L3":
            return unused, unused, int(controlled_current)
        return int(controlled_current), unused, unused

    def _timer_reached(self, now: float, attr: str, condition: bool, seconds: int) -> bool:
        if condition:
            since = getattr(self, attr)
            if since is None:
                setattr(self, attr, now)
                return seconds <= 0
            return (now - since) >= seconds
        setattr(self, attr, None)
        return False

    def _action_allowed(self, now: float) -> bool:
        s = self.settings
        if self.last_action_ts is None:
            return True
        return (now - self.last_action_ts) >= s.min_seconds_between_actions

    def _should_send_limit(self, now: float, p1: int, p2: int, p3: int, ttl: int) -> bool:
        s = self.settings
        target = (p1, p2, p3, ttl)
        if self.last_target != target:
            return True
        if self.last_set_limit_ts is None:
            return True
        resend_after = max(5, min(s.resend_limit_every_seconds, max(5, int(ttl * 0.75))))
        return (now - self.last_set_limit_ts) >= resend_after

    def evaluate(self, inp: RegulatorInput) -> RegulatorDecision:
        s = self.settings
        now = float(inp.now_ts)
        status = (inp.status or "").strip().lower()
        grid_l1 = _valid_number(inp.grid_l1, None)
        charger_current = _valid_number(inp.charger_current, 0.0) or 0.0
        available_l1, house_load_l1 = self.calculate_available_l1(grid_l1, charger_current)

        if grid_l1 is None:
            return RegulatorDecision(
                mode="unavailable",
                reason="L1-sensorn saknas/är unavailable, skickar inget.",
                command=None,
                service=None,
                current_p1=0,
                current_p2=0,
                current_p3=0,
                ttl_seconds=s.normal_ttl_seconds,
                available_l1=0,
                house_load_l1=0.0,
                grid_l1=None,
                charger_current=charger_current,
                paused_by_us=self.paused_by_us,
                should_call_service=False,
                dry_run=s.dry_run,
            )

        # Keep timers updated even if HA reports paused/awaiting states.
        soft_reached = self._timer_reached(now, "above_soft_since", grid_l1 > s.soft_limit_amps, s.soft_for_seconds)
        pause_reached = self._timer_reached(now, "above_pause_since", grid_l1 > s.pause_limit_amps, s.pause_for_seconds)
        resume_reached = self._timer_reached(now, "below_resume_since", grid_l1 < s.resume_below_amps, s.resume_for_seconds)

        # If user/charger reports paused, treat resume guard as meaningful even after restart.
        maybe_paused = self.paused_by_us or status in PAUSED_STATUSES

        if status not in ALLOWED_ACTIVE_STATUSES and not (maybe_paused and status in ALLOWED_RESUME_STATUSES):
            return RegulatorDecision(
                mode="idle",
                reason=f"Status '{status}' styrs inte.",
                command=None,
                service=None,
                current_p1=0,
                current_p2=0,
                current_p3=0,
                ttl_seconds=s.normal_ttl_seconds,
                available_l1=available_l1,
                house_load_l1=house_load_l1,
                grid_l1=grid_l1,
                charger_current=charger_current,
                paused_by_us=self.paused_by_us,
                should_call_service=False,
                dry_run=s.dry_run,
            )

        # PAUSED -> RESUME when L1 has been low long enough.
        if s.enable_pause_resume and maybe_paused and resume_reached:
            should_call = s.active_control and self._action_allowed(now)
            if should_call and not s.dry_run:
                self.paused_by_us = False
                self.last_action_ts = now
                self.last_command = "resume"
            return RegulatorDecision(
                mode="resume",
                reason=f"L1 har varit under {s.resume_below_amps:g}A i {s.resume_for_seconds}s, återupptar.",
                command="resume",
                service="easee.action_command",
                current_p1=0,
                current_p2=0,
                current_p3=0,
                ttl_seconds=s.normal_ttl_seconds,
                available_l1=available_l1,
                house_load_l1=house_load_l1,
                grid_l1=grid_l1,
                charger_current=charger_current,
                paused_by_us=self.paused_by_us,
                should_call_service=should_call,
                dry_run=s.dry_run,
            )

        # Too high too long -> PAUSE.
        if s.enable_pause_resume and pause_reached and status not in PAUSED_STATUSES:
            should_call = s.active_control and self._action_allowed(now)
            if should_call and not s.dry_run:
                self.paused_by_us = True
                self.last_action_ts = now
                self.last_command = "pause"
            return RegulatorDecision(
                mode="pause",
                reason=f"L1 har varit över {s.pause_limit_amps:g}A i {s.pause_for_seconds}s, pausar.",
                command="pause",
                service="easee.action_command",
                current_p1=0,
                current_p2=0,
                current_p3=0,
                ttl_seconds=s.normal_ttl_seconds,
                available_l1=available_l1,
                house_load_l1=house_load_l1,
                grid_l1=grid_l1,
                charger_current=charger_current,
                paused_by_us=self.paused_by_us or True,
                should_call_service=should_call,
                dry_run=s.dry_run,
            )

        if not s.enable_dynamic_limit:
            return RegulatorDecision(
                mode="monitor",
                reason="Dynamic limit är avstängd, övervakar bara.",
                command=None,
                service=None,
                current_p1=0,
                current_p2=0,
                current_p3=0,
                ttl_seconds=s.normal_ttl_seconds,
                available_l1=available_l1,
                house_load_l1=house_load_l1,
                grid_l1=grid_l1,
                charger_current=charger_current,
                paused_by_us=self.paused_by_us,
                should_call_service=False,
                dry_run=s.dry_run,
            )

        # High but not long enough to pause -> SOFT current.
        ttl = s.normal_ttl_seconds
        mode = "normal"
        reason = "Normal L1-balansering."
        target_current = available_l1

        if available_l1 >= s.min_charge_amps:
            target_current = min(available_l1, s.max_charge_amps)
            reason = f"Avail L1={available_l1}A från huslastberäkningen."
        elif s.force_min_when_available_too_low and grid_l1 < s.safe_l1_amps:
            target_current = s.min_charge_amps
            reason = f"Avail < {s.min_charge_amps}A men L1 är under safe-gränsen {s.safe_l1_amps:g}A, håller min {s.min_charge_amps}A."
        else:
            target_current = 0
            reason = f"Avail < {s.min_charge_amps}A och L1={grid_l1:g}A är inte under safe-gränsen, sätter 0A."

        if s.enable_soft_guard and soft_reached:
            ttl = s.soft_ttl_seconds
            target_current = min(max(0, s.soft_current_amps), s.max_charge_amps)
            mode = "soft_limit"
            reason = f"L1 har varit över {s.soft_limit_amps:g}A i {s.soft_for_seconds}s, mjukbromsar till {target_current}A."

        p1, p2, p3 = self._phase_currents(target_current)
        should_call = s.active_control and self._should_send_limit(now, p1, p2, p3, ttl) and self._action_allowed(now)
        if should_call and not s.dry_run:
            self.last_set_limit_ts = now
            self.last_action_ts = now
            self.last_target = (p1, p2, p3, ttl)
            self.last_command = "set_limit"

        return RegulatorDecision(
            mode=mode if not s.dry_run else f"dry_run_{mode}",
            reason=reason,
            command="set_limit",
            service="easee.set_circuit_dynamic_limit",
            current_p1=p1,
            current_p2=p2,
            current_p3=p3,
            ttl_seconds=ttl,
            available_l1=available_l1,
            house_load_l1=house_load_l1,
            grid_l1=grid_l1,
            charger_current=charger_current,
            paused_by_us=self.paused_by_us,
            should_call_service=should_call,
            dry_run=s.dry_run,
        )
