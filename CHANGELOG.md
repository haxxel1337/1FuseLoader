# Changelog

## 0.4.0b1

- Correct Easee TTL units and renew identical limits through a narrowly versioned
  adapter for Easee 0.9.74 / pyeasee 0.8.17, without changing those integrations.
- Record submitted commands only after HTTP acceptance; serialize controller
  operations and expose blocked/error/proposed command diagnostics.
- Reject missing or stale telemetry, reset continuity timers across gaps, and
  request 0 A when control is active and required telemetry is untrustworthy.
- Respect manual pauses and require sufficient headroom plus a fresh accepted
  current cap before resuming a pause owned by this runtime.
- Remove forced minimum current above headroom; soft limiting can only lower it.
- Validate settings across all UI entry points, restrict control to L1, and add
  current-unit checks, safe reconfiguration, and complete EN/SV flow text.
- Add regression suites and real Home Assistant 2026.10.1 / Python 3.14 CI checks.
  This is a pilot build; physical charging acceptance remains a rollout step.

## 0.3.4

- Lowered HACS installation minimum Home Assistant version to `2026.6.0`.
- Updated manifest version to `0.3.4` for a clean HACS release.

## 0.3.2

- Removed installation-specific Easee `device_id` from defaults and README.
- Added config-flow validation so the Easee device ID must be entered during setup.
- Updated manifest version to `0.3.2`.
- Confirmed GitHub metadata uses `haxxel1337/1FuseLoader`.

## 0.3.1

- Added GitHub/HACS-ready repository files.
- Added `.gitignore`, `hacs.json`, `.editorconfig`, and a small GitHub Actions workflow.
- Updated manifest version to `0.3.1`.
- Added `integration_type: helper` and `single_config_entry: true` to the manifest.

## 0.3.0

- Targeted Home Assistant 2026.7 conventions.
- Updated options flow structure for current Home Assistant config entry handling.

## 0.2.0

- Included old L1 available-current formula directly in Python logic.
- Combined Force L1 balancing, soft guard, pause guard, and resume guard.
