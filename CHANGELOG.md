# Changelog

## 0.4.0b10

- Correct the read-only charger snapshot schema: derive measured current from
  all four required `inCurrentT2`–`inCurrentT5` fields, matching Easee 0.9.74's
  Current sensor. Never substitute offered current or a synthetic total field.
- Add public-schema regression coverage and expose only fixed, credential-free
  snapshot error codes, preserving conservative fallback when a read fails.

## 0.4.0b9

- Treat Startup Charge as the minimum fully buffered start threshold. After
  stable headroom, a waiting vehicle may receive the safe available offer up
  to its configured maximum, without explicit start/stop retry commands.
- Refresh a selected charger's current/status pair using a bounded, read-only
  authenticated cloud GET when recovery needs newer evidence. Reads run in
  the background, at most once per 30 seconds, so protective ticks remain
  available. Preserve original HA report times and label the cloud receipt
  separately; a heartbeat is never relabelled as a current measurement.
- Discard snapshots after new relevant events, reload, selection changes,
  health loss or expiry. Recalculate on the newest grid after the read and
  before an increase if checkpoint verification yielded to new telemetry.
- Rebase obsolete house-load anchors only on verified coherent feedback,
  preserving possible delayed-start response context and spent alignment
  time. Never grant startup/takeover headroom from stale high EV readings.
- Expose estimation basis, conservative house anchor and current-refresh
  diagnostics. Preserve exposure storage, budgets, safeguards and settings.

## 0.4.0b8

- Recover a transient exposure-storage fault only after the latest conservative
  checkpoint has been written and independently read back. Re-evaluate fresh
  charging inputs on a later tick; preserve consumed budget, restart waits and
  all overload safety latches.
- Skip unchanged exposure checkpoints and serialize shielded writes across
  controller reloads. A caller timeout cannot leave an untracked disk write
  which later overwrites a newer checkpoint.
- Verify unique checkpoint revisions through Home Assistant's public Store API,
  including storage errors which its save method may log without raising.
  Keep unknown or failed restores blocked and preserve the older ledger.
- Expose storage error type and checkpoint verification diagnostics, and log
  failures for investigation. Test the storage contract against real Home
  Assistant 2026.10.1 and 2026.10.2, including delayed and failed writes.

## 0.4.0b7

- Recognize the verified Currently One MQTT source and validate its actual L1
  frame receipts and advancing source timestamps. Identical current values can
  remain fresh and align with EV reports without forcing MQTT sensor updates.
  Invalid, replayed, retained or stale frames cannot renew source health, and
  connection loss still blocks control.
- Retain an accepted lower cap during a bounded, physically observed partial EV
  reduction while the corresponding grid report catches up. Partial responses
  and lease renewals cannot replenish the alignment budget or permit increases.
- During an experimental overload transition, wait for grid feedback reflecting
  the final minimum-current response within the original response deadline.
  Intermediate grid reports cannot falsely complete that response.
- Keep experimental overload an explicit opt-in. Its maximum +4 A / 1800 s
  cumulative budget, 38 A transition ceiling and 10 s response deadline remain
  separate from normal headroom control and physical installation limits.

## 0.4.0b6

- Default to fast recovery during actual charging: offer buffered available
  capacity directly, up to the configured maximum, only after fresh and coherent
  EV/grid reports with no pending limit response. Keep startup and restart
  hysteresis, immediate reductions and the optional legacy gradual mode.
- After positive report-alignment timeout, retain only the already accepted cap
  when raw grid current plus the entire cap still fits the full nominal buffer.
  This conservative hold permits neither increases nor resume and grants no new
  mismatch budget. Invalid, stale, missing or excessive current still takes
  precedence; an insufficient conservative budget still requests zero.
- Cover the observed partial/full-response timeout sequence and direct recovery
  with new protection, timestamp and compatibility regressions.

## 0.4.0b5

- Separate the default 7 A startup offer from the 6 A running minimum; require
  full buffered startup headroom, respect configured bounds and do not ramp
  indefinitely while the vehicle is waiting. Expose starting/waiting/stalled
  diagnostics without adding explicit start or schedule-override commands.
- Bound positive-start/increase report alignment as well as reduction alignment;
  retain conservative house-load estimates and prevent further rises while
  sources catch up. TTL renewals cannot extend the mismatch deadline.
- Retain actual command attempt/acceptance receipts, expose explicit circuit P1
  readback and source health, and distinguish these from physical EV current.
- Classify the integration and logical device as a service so an existing entry
  appears under Integrations rather than Helpers, preserving identifiers.
- Add regression coverage for startup, report ordering, protections, diagnostics
  and additive settings. Physical charging acceptance remains a supervised step;
  the separate experimental overload policy is unchanged.

## 0.4.0b4

- Add a disabled-by-default experimental timed-overload profile: request minimum
  EV current, permit a separately bounded response window, then hold minimum
  current within a sustained L1 ceiling and cumulative exposure budget.
- Bound the available settings to at most +4 A, 1800 s cumulative exposure,
  38 A transition peak and 10 s response time; require explicit opt-in and
  cross-validate the settings through all Home Assistant UI entry points.
- Persist experimental exposure and unresolved response state across restart
  and configuration changes; brief dips and switch toggles do not replenish
  the budget. Surface budget, transition and failure diagnostics.
- Account for whole-minute Easee leases before permitting another positive
  overload command; charging can end before the nominal budget expires.
- Document HACS prerelease installation, the separate overload opt-in and the
  need to verify the installation and charger offline fallback before using it.
  These operational settings do not certify a fuse or a physical stop deadline.

## 0.4.0b3

- Handle the opposite grid/EV report order during an acknowledged reduction
  within a bounded alignment window and conservative nominal-current check.
- Keep completed/disconnected sessions from receiving an unnecessary zero
  command solely because their final current reports arrive out of order.

## 0.4.0b2

- Preserve minimum charging through moderate house-load peaks by using the
  normal buffer only within the fuse budget and a separate retained reserve.
- React to source changes immediately, coalesce updates during in-flight
  commands, and raise current gradually after stable headroom.
- Add restart hysteresis and a bounded wait for grid reports after observed EV
  reductions; TTL renewals do not reset ramp/restart observation timers.
- Add optional read-only BMW CarData diagnostics with per-field source times;
  stale/session-inapplicable values do not enter the current-control loop.
- Keep deliberate overload disabled pending the actual main-fuse specification
  and supervised charging tests. No HA restart or active takeover is implied.

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
