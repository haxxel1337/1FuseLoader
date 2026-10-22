# Changelog

## 0.4.0b22

- Keep an accepted positive-current response with its original alignment
  owner across EV-first reports. Neither of the earlier uncertainty fallbacks
  may start a competing decrease for that same qualified response.
- Hand a coherent lower-offer observation to the remaining reduction guard
  without treating a pre-acceptance EV event as physical response or granting
  another deadline.
- Retain only an existing lower offer when fresh raw grid plus the entire
  unresolved previous offer fits the normal buffered envelope. This raw hold
  supplies no EV subtraction, increase, resume or new alignment time. Source,
  lease, clock and whole-offer bounds continue to apply; interrupted holds
  remain fail-closed after restart.
- Preserve historical experimental exposure and failures. This update does
  not activate a new overload trial, replenish time or estimate fuse cooling.

## 0.4.0b21

- Separate the lower-current response deadline from subsequent meter/EV
  synchronization. A trusted post-acceptance low-current event received within
  the original window and a buffered-normal post-command grid report can enter
  one fixed synchronization phase, at most five seconds from that response.
- Retain only the existing lower offer during synchronization: no writes,
  renewals, increases or resumes. A raw grid above the normal buffered ceiling,
  rising EV current, source/control fault or expired lease terminates it.
- Anchor later-grid ordering to the first qualifying response rather than a
  continually moving latest EV event. Preserve interrupted-response recovery,
  cumulative exposure and existing overload latches.

## 0.4.0b20

- Fix an old, unchanged EV current event starting a new ten-second low-grid
  response window despite numerically coherent currents and a safe raw-grid
  budget. Event age/skew alone now stays with the existing freshness and
  conservative-cap policy; it cannot manufacture a new response obligation.
- Keep genuine numeric-conflict windows, fixed deadlines, source faults,
  raw-current limits and persisted recovery/overload latches unchanged.
- Add regressions for first activation with a stable event, aging/skew,
  genuine conflict reentry, timeout, restore and an unchanged full-buffer edge.

## 0.4.0b19

- Correlate bounded HTTP receipts with exact Easee CommandResponse IDs, and
  observe selected native circuit-limit events separately from cached reads.
  Fence callbacks and late replies across connection/controller replacement.
- Cancel superseded positive requests for new protective conditions, reserve
  SDK rate capacity for protection, and confirm zero ordering before a new
  offer. Suppress only a conservatively confirmed redundant zero; never renew
  response, lease or overload accounting from diagnostic receipts.
- Add a source-bound, fixed low-grid alignment window behind the existing
  verified-import option. Reduce a live higher cap to 7 A when its entire
  offer fits the normal raw-grid budget. Persist interrupted alignment and
  retain the original deadline and existing bounded retry family.
- Audit both pyeasee 0.8.17 and 0.8.19, including SDK throttling/cancellation,
  and test both against Home Assistant 2026.10.1 and 2026.10.2. Coordinate the
  native Easee update to upstream PR 701 for the transport-cookie fix.
- Preserve spent experimental time and failure latches. Offline validation
  does not establish physical charger response; normal-load verification
  remains required after activation.

## 0.4.0b18

- Add an explicit default-off, sample-aged lower-current response trial for
  charging sessions blocked by an old experimental timeout or spent budget.
  A live higher accepted cap can only be reduced; new post-acceptance EV and
  later grid evidence must establish normal buffered operation.
- Charge checkpoint and API latency to the fixed local response deadline.
  Prevent stale positive dispatch, bound API waiting, and re-evaluate without
  depending on another sensor report. Cloud delivery is still best effort.
- Persist recovery caps, attempt counts and conservative backoff. An
  interrupted response fails closed after restart. Bound telemetry-stop
  recovery to startup current and two automatic retries, including the
  old-latch rejection family. Preserve experimental usage and failure.
- Add independent physical/transport and runtime race regressions. No fuse
  cooling model, automatic budget reset or renewed 30-minute allowance is
  introduced.

## 0.4.0b17

- Preserve Easee observation timestamps independently of HTTP receipts. Reject
  missing, invalid, conflicting or regressing timestamp metadata for new
  observation credit. An unchanged event timestamp is not itself a fault or
  a new physical sample. A cached GET cannot overwrite a newer native current
  or a native pause, schedule or authorization state.
- Add an explicit, default-off import-only fallback for delayed or inconsistent
  EV reports. Under the verified same-phase importing-load model, it can only
  retain or reduce an accepted positive cap whose entire offered current fits
  the fresh raw-grid normal budget. It cannot create EV credit, force 6 A,
  resume a pause or replenish experimental overload time.
- Keep measurement-uncertainty recovery separate from normal increases and
  experimental failure accounting. Bound repeated recovery attempts and their
  startup offers instead of cycling directly back to the maximum current.
- Keep a bounded command and first-stop timeline, including source clock
  provenance and API receipt times. Persist incident evidence locally outside
  the control path; diagnostic write failures do not alter the overload ledger.
- Add independent process/transport regressions for delayed EV ramps, old
  observations in new GET responses, lower physical demand, and true protective
  stops. These offline checks do not establish physical end-to-end performance.

## 0.4.0b16

- Refresh a quiet charging session's numeric current/status pair before its
  alignment/age deadline instead of waiting for an already expired source to
  request a read. Native telemetry takes priority; requests remain outside
  the control lock, single-flight and bounded by a minimum interval and error
  backoff. Short windows or unavailable cloud data retain their protective
  response rather than extending freshness.
- Preserve the saved experimental-overload failure independently from the
  concrete reason a normal current reduction was rejected. Include input age,
  receipt skew and the active window in the first stop diagnostics.
- Expose the saved overload failure for an accurate dashboard status. An
  enabled experimental switch does not restore spent cumulative time or clear
  a timeout latch. No overload budget, response window, current limit or
  source-health guard is reset or relaxed by this update.

## 0.4.0b15

- Keep an existing timed-overload failure or exhausted allowance from
  preempting a proven, fully buffered normal reduction of an accepted offer.
  Bound its unconfirmed response; do not grant an increase, resume, new
  overload allowance or extra response time through that normal path.
- Handle grid-first feedback during a known accepted positive startup within
  the original alignment window and normal raw-grid envelope. Retain only
  that accepted offer; later low EV reports, excess load, stale sources and
  the original deadline retain their protective response.
- Avoid an indefinite pending-zero restart wait when the entire new offer
  fits the fresh raw-grid budget for the stable restart period. Do not
  subtract idle EV readings or declare physical zero confirmation. A real
  accepted restart establishes new response tracking even for an unchanged
  numeric offer; failed calls and TTL renewals do not reset its budget.
- Recheck latest source data after checkpoint I/O before every positive
  command, including reductions and lease renewals. Reevaluate after API
  acceptance when source data changed during the request, while keeping
  cloud acceptance separate from physical charger feedback.
- Preserve the first zero/pause request's reason, input clocks and command
  result through subsequent inconsistent feedback. Clarify that Easee's
  latestPulse is a local ProductUpdate receipt, not a transport ping or a
  physical current sample. Old product data still requests zero; this update
  does not repair a cloud/SignalR outage or clear saved overload protection.

## 0.4.0b14

- When a higher accepted offer is reduced to the experimental minimum, an
  earlier low EV report cannot count as a completed minimum-current response.
  Keep the existing bounded shedding transition active across mixed reports
  instead of handing an unconfirmed reduction to ordinary headroom logic.
- Require an accepted minimum offer, a later minimum-current EV report and
  subsequent coherent grid feedback before completing that transition. API
  latency, lease renewals and report-order changes do not extend its original
  deadline or replenish cumulative exposure.
- Obtain a fresh minimum-offer acknowledgement when an existing identical
  lease would otherwise suppress the first transition submission. Failed
  attempts remain unconfirmed and bounded by the same response deadline.
- Preserve existing safety latches and the response timeout. This update does
  not reset an earlier timeout or prove that slow current reporting meets the
  configured response window.

## 0.4.0b13

- When fresh but misaligned feedback makes an accepted positive offer exceed
  buffered headroom, an actually charging vehicle may receive a lower cap
  calculated from raw grid current alone. The entire reduced cap must fit the
  full normal buffer; no uncertain EV current is subtracted.
- Keep the original alignment deadline across the accepted reduction. A
  proposal, failed submission or lease renewal cannot grant more response time.
  Faults, stale data, excessive EV current, timeout and overload/soft/pause
  protections retain priority. This fallback cannot start or resume a car.
- Allow up to 10 seconds for the initial exposure-ledger read during setup,
  while retaining the 2-second runtime write deadline. Validation and
  fail-closed handling remain unchanged; this avoids treating every startup
  read slower than 2 seconds as a permanently failed restore.

## 0.4.0b12

- Preserve an accepted lower current cap while delayed EV/grid reports catch
  up after a reduction during startup. An unchanged EV value well below the
  new cap cannot prove that the vehicle has responded to the reduction.
- Bound delayed high EV reports by the preceding accepted offer, and retain
  only the lower accepted cap within conservative raw-grid headroom. Do not
  propose an increase or resume while this response remains unverified.
- Share a nonrenewable alignment budget across report-order changes and lease
  renewals. Retain lower-cap protection after near-cap confirmation without
  pinning a stable vehicle at its previous cap. Distinguish further reductions
  in the same response chain from a new reduction after confirmed recovery.
  Keep telemetry faults, timeout and overload protection in control. Preserve
  configuration, cumulative exposure and the charger API.

## 0.4.0b11

- Read the selected charger's measured terminal currents and operation mode
  through the same observation API used by Easee 0.9.74, in one bounded GET.
  Validate every required observation ID and declared data type; reject missing,
  duplicated or invalid measurements without substituting offered current.
- Expose a fixed allowlist of underlying failure types without logging private
  exception text. Keep recovery policy, request throttling, race checks and
  cumulative exposure unchanged.

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
