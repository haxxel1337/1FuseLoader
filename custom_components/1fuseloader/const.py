from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

DOMAIN = "1fuseloader"
NAME = "1FuseLoader"
PLATFORMS = ["sensor", "number", "switch", "button"]

CONF_NAME = "name"
CONF_DEVICE_ID = "device_id"
CONF_BMW_DEVICE_ID = "bmw_device_id"
CONF_STATUS_ENTITY = "status_entity"
CONF_L1_ENTITY = "l1_entity"
CONF_L2_ENTITY = "l2_entity"
CONF_L3_ENTITY = "l3_entity"
CONF_CHARGER_CURRENT_ENTITY = "charger_current_entity"
CONF_CONTROLLED_PHASE = "controlled_phase"

DEFAULT_NAME = "1FuseLoader Parking"
# Home Assistant Easee device_id is installation-specific. Never hard-code it in a public repo.
DEFAULT_DEVICE_ID = ""
DEFAULT_STATUS_ENTITY = "sensor.parking_status"
DEFAULT_L1_ENTITY = "sensor.l1ampfusecurrentlyuse"
DEFAULT_L2_ENTITY = "sensor.l2ampfusecurrentlyuse"
DEFAULT_L3_ENTITY = "sensor.l3ampfusecurrentlyuse"
DEFAULT_CHARGER_CURRENT_ENTITY = "sensor.parking_current"
DEFAULT_CONTROLLED_PHASE = "L1"

OPT_MAIN_FUSE_AMPS = "main_fuse_amps"
OPT_BUFFER_AMPS = "buffer_amps"
OPT_MIN_CHARGE_AMPS = "min_charge_amps"
OPT_MAX_CHARGE_AMPS = "max_charge_amps"
OPT_SAFE_L1_AMPS = "safe_l1_amps"
OPT_SOFT_LIMIT_AMPS = "soft_limit_amps"
OPT_SOFT_FOR_SECONDS = "soft_for_seconds"
OPT_SOFT_CURRENT_AMPS = "soft_current_amps"
OPT_PAUSE_LIMIT_AMPS = "pause_limit_amps"
OPT_PAUSE_FOR_SECONDS = "pause_for_seconds"
OPT_RESUME_BELOW_AMPS = "resume_below_amps"
OPT_RESUME_FOR_SECONDS = "resume_for_seconds"
OPT_NORMAL_TTL_SECONDS = "normal_ttl_seconds"
OPT_SOFT_TTL_SECONDS = "soft_ttl_seconds"
OPT_RESEND_LIMIT_EVERY_SECONDS = "resend_limit_every_seconds"
OPT_MIN_SECONDS_BETWEEN_ACTIONS = "min_seconds_between_actions"
OPT_ACTIVE_CONTROL = "active_control"
OPT_DRY_RUN = "dry_run"
OPT_ENABLE_DYNAMIC_LIMIT = "enable_dynamic_limit"
OPT_ENABLE_SOFT_GUARD = "enable_soft_guard"
OPT_ENABLE_PAUSE_RESUME = "enable_pause_resume"
OPT_FORCE_MIN_WHEN_AVAILABLE_TOO_LOW = "force_min_when_available_too_low"
OPT_UNUSED_PHASE_CURRENT = "unused_phase_current"
OPT_MAX_SENSOR_AGE_SECONDS = "max_sensor_age_seconds"
OPT_MINIMUM_RESERVE_AMPS = "minimum_reserve_amps"
OPT_INCREASE_STABLE_SECONDS = "increase_stable_seconds"
OPT_INCREASE_INTERVAL_SECONDS = "increase_interval_seconds"
OPT_RESTART_STABLE_SECONDS = "restart_stable_seconds"
OPT_MEASUREMENT_SETTLE_SECONDS = "measurement_settle_seconds"

DEFAULT_OPTIONS = {
    OPT_MAIN_FUSE_AMPS: 25.0,
    OPT_BUFFER_AMPS: 2.0,
    OPT_MIN_CHARGE_AMPS: 6,
    OPT_MAX_CHARGE_AMPS: 16,
    OPT_SAFE_L1_AMPS: 23.0,
    OPT_SOFT_LIMIT_AMPS: 28.0,
    OPT_SOFT_FOR_SECONDS: 20,
    OPT_SOFT_CURRENT_AMPS: 6,
    OPT_PAUSE_LIMIT_AMPS: 28.0,
    OPT_PAUSE_FOR_SECONDS: 120,
    OPT_RESUME_BELOW_AMPS: 21.0,
    OPT_RESUME_FOR_SECONDS: 120,
    OPT_NORMAL_TTL_SECONDS: 60,
    OPT_SOFT_TTL_SECONDS: 120,
    OPT_RESEND_LIMIT_EVERY_SECONDS: 30,
    OPT_MIN_SECONDS_BETWEEN_ACTIONS: 5,
    OPT_ACTIVE_CONTROL: False,
    OPT_DRY_RUN: True,
    OPT_ENABLE_DYNAMIC_LIMIT: True,
    OPT_ENABLE_SOFT_GUARD: True,
    OPT_ENABLE_PAUSE_RESUME: True,
    OPT_FORCE_MIN_WHEN_AVAILABLE_TOO_LOW: False,
    OPT_UNUSED_PHASE_CURRENT: 0,
    OPT_MAX_SENSOR_AGE_SECONDS: 120,
    OPT_MINIMUM_RESERVE_AMPS: 0.5,
    OPT_INCREASE_STABLE_SECONDS: 30,
    OPT_INCREASE_INTERVAL_SECONDS: 15,
    OPT_RESTART_STABLE_SECONDS: 60,
    OPT_MEASUREMENT_SETTLE_SECONDS: 10,
}

OPTION_KEYS = set(DEFAULT_OPTIONS)

# Shared by the options flow, Number entities, and controller updates. A second
# UI must not be able to bypass the limits enforced by the first one.
OPTION_NUMBER_RANGES = {
    OPT_MAIN_FUSE_AMPS: (10, 63, 0.5),
    OPT_BUFFER_AMPS: (0, 10, 0.5),
    OPT_MIN_CHARGE_AMPS: (6, 16, 1),
    OPT_MAX_CHARGE_AMPS: (6, 32, 1),
    OPT_SAFE_L1_AMPS: (5, 63, 0.5),
    OPT_SOFT_LIMIT_AMPS: (10, 80, 0.5),
    OPT_SOFT_FOR_SECONDS: (0, 300, 5),
    OPT_SOFT_CURRENT_AMPS: (0, 16, 1),
    OPT_PAUSE_LIMIT_AMPS: (10, 80, 0.5),
    OPT_PAUSE_FOR_SECONDS: (0, 600, 5),
    OPT_RESUME_BELOW_AMPS: (5, 63, 0.5),
    OPT_RESUME_FOR_SECONDS: (0, 600, 5),
    OPT_NORMAL_TTL_SECONDS: (60, 600, 5),
    OPT_SOFT_TTL_SECONDS: (60, 600, 5),
    OPT_RESEND_LIMIT_EVERY_SECONDS: (5, 300, 5),
    OPT_MIN_SECONDS_BETWEEN_ACTIONS: (1, 60, 1),
    OPT_UNUSED_PHASE_CURRENT: (0, 0, 1),
    OPT_MAX_SENSOR_AGE_SECONDS: (15, 3600, 5),
    OPT_MINIMUM_RESERVE_AMPS: (0, 5, 0.1),
    OPT_INCREASE_STABLE_SECONDS: (5, 300, 5),
    OPT_INCREASE_INTERVAL_SECONDS: (5, 120, 5),
    OPT_RESTART_STABLE_SECONDS: (15, 600, 5),
    OPT_MEASUREMENT_SETTLE_SECONDS: (1, 30, 1),
}


def validate_option_value(key: str, value: Any) -> bool | float | int:
    """Validate and normalize a single setting without truncating integers."""
    if key not in DEFAULT_OPTIONS:
        raise ValueError(f"Unknown setting: {key}")
    default = DEFAULT_OPTIONS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return value
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError(f"{key} must be a finite number") from err
    if not isfinite(number):
        raise ValueError(f"{key} must be a finite number")
    if isinstance(default, int) and not number.is_integer():
        raise ValueError(f"{key} must be a whole number")
    minimum, maximum, _step = OPTION_NUMBER_RANGES[key]
    if not minimum <= number <= maximum:
        raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
    return int(number) if isinstance(default, int) else number


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return complete settings, rejecting unsafe combinations atomically."""
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    merged = {**DEFAULT_OPTIONS, **options}
    if OPT_MINIMUM_RESERVE_AMPS not in options:
        # Existing installations can have a smaller normal buffer. Adding the
        # new reserve must not make their previously valid settings unloadable.
        buffer_value = validate_option_value(OPT_BUFFER_AMPS, merged[OPT_BUFFER_AMPS])
        merged[OPT_MINIMUM_RESERVE_AMPS] = min(merged[OPT_MINIMUM_RESERVE_AMPS], buffer_value)
    result = {key: validate_option_value(key, value) for key, value in merged.items()}
    # Legacy entries may still carry True; the unsafe forced-current policy has
    # been removed and must not be re-enabled by restoring older options.
    result[OPT_FORCE_MIN_WHEN_AVAILABLE_TOO_LOW] = False
    if result[OPT_MIN_CHARGE_AMPS] > result[OPT_MAX_CHARGE_AMPS]:
        raise ValueError("Minimum charge current must not exceed maximum charge current")
    if result[OPT_SOFT_CURRENT_AMPS] > result[OPT_MAX_CHARGE_AMPS]:
        raise ValueError("Soft current must not exceed maximum charge current")
    if result[OPT_MINIMUM_RESERVE_AMPS] > result[OPT_BUFFER_AMPS]:
        raise ValueError("Minimum reserve must not exceed the normal buffer")
    # Kept only for legacy storage compatibility; the old threshold no longer
    # drives control and must not prevent editing the real fuse rating.
    result[OPT_SAFE_L1_AMPS] = min(result[OPT_SAFE_L1_AMPS], result[OPT_MAIN_FUSE_AMPS])
    if result[OPT_RESUME_BELOW_AMPS] >= min(result[OPT_SOFT_LIMIT_AMPS], result[OPT_PAUSE_LIMIT_AMPS]):
        raise ValueError("Resume threshold must be below both overload thresholds")
    minimum_ttl = min(result[OPT_NORMAL_TTL_SECONDS], result[OPT_SOFT_TTL_SECONDS])
    if result[OPT_RESEND_LIMIT_EVERY_SECONDS] > minimum_ttl / 2:
        raise ValueError("Resend interval must be at most half the shortest TTL")
    if result[OPT_MIN_SECONDS_BETWEEN_ACTIONS] >= minimum_ttl / 2:
        raise ValueError("Action cooldown must be less than half the shortest TTL")
    return result
