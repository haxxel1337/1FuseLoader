# Changelog

## Unreleased

- After an accepted normal decrease, retain only that original lower lease
  while the meter still represents the pre-dispatch sample. A later EV report
  alone must not count the same meter excess twice or renew the lease.
- A stronger measured load, new source/critical fault, original lease expiry
  or electrical episode still takes precedence. A genuinely later meter
  sample permits the next normal allocation.
- This scoped correction does not change telemetry freshness, overload
  permission or physical charger behavior; it does not establish the cause
  of separate pilot-zero interruptions.

## 0.4.0b43

- Bind missing or changed mapped-phase provenance at a later actual accepted
  normal SDK renewal. A first command may precede phase identification; keeping
  its missing floor forever blocked otherwise measured normal recovery.
- Keep a valid same-scope floor fixed across renewals. Phase adoption, cached
  observations or an HTTP reply alone still cannot establish recovery; a new
  mapped L1 sample after the new SDK boundary and later coherent grid are needed.
- Preserve the electrical episode's original response/grid deadlines, finite
  leases, exposure, cycle id and incident history. No extra current or time is
  granted by repairing normal measurement provenance.

## 0.4.0b42

- Rework selected meter control around one explicit electrical episode and
  separate normal, reduction, grid-confirmation, minimum-hold and recovery
  states. Legacy estimator and response owners no longer control this path.
- Separate recovery from a resolved operational fault from replenishing the
  overload budget. Full measured normal recovery can release an unspent
  blocked cycle while retaining its id, consumed exposure and fault history.
  Exhausted budget still requires the full existing below-23 A rearm interval.
- Keep ordinary meter regulation usable with experimental overload disabled;
  the overload switch gates extra permission rather than normal operation.
- Keep original electrical deadlines, mapped-L1 evidence, finite actual SDK
  leases, delivery barriers, durable accounting and manual session intent.
  Add independent controller and plant scenarios for recovery and true stops.
- Resume the same unsent proposal after a slow verified checkpoint instead of
  starving it with moving snapshots. Retain only existing finite authority
  while waiting; critical faults and original deadlines still preempt it.
- Order accepted authority by actual dispatch so an obsolete positive reply
  cannot replace a newer off command or fabricate a fresh lease.

## 0.4.0b41

- Compare controlled-L1 provider timestamps only with their own captured
  provider ordering floor. Do not require a genuinely new native response's
  provider bucket to exceed the HA SDK-entry wall-clock bucket: clock offset
  can otherwise falsely reject a fast response. Local receipt and sequence
  must still advance after the actual SDK boundary; mapping, native evidence
  credit, provider nonregression, replay and freshness guards remain intact.
- Add real adapter/runtime/SDK regressions for fast response with a lagging
  provider clock, preserving the original electrical and grid deadlines.

## 0.4.0b40

- Keep aggregate input-current diagnostics separate from a validated controlled
  L1 observation. Under documented TN output mode 10, native terminal T3/183
  supplies L1 evidence with its own receipt, sequence and mapping epoch;
  calculated neutral T2 is not substituted for the controlled phase.
- Require actual mapped-phase evidence after dispatch for minimum response,
  and independently eligible mapped L1 plus coherent grid data for EV
  subtraction or increases. Unknown, switched or unsupported arrangements
  cannot acquire this capability from cached values, pilot current or a
  different terminal with a lower reading. Legacy aggregate behavior remains
  separate.
- Anchor unresolved positive authority to actual possible positive dispatches
  and their original finite horizons. Repeated zero acknowledgements cannot
  extend positive uncertainty or continually restart normal recovery.
  Preserve genuine delivery barriers, historical failures and overload
  accounting while allowing separately safe ordinary restart.
- Preserve already qualified mapped response and fixed confirmation through
  legitimate same-source handoffs. Capture observational phase floors in
  legacy SDK records without changing legacy decisions, and invalidate older
  off evidence on every later positive dispatch, including unknown outcomes.
- Preserve the original electrical response and grid-confirmation deadlines,
  actual grid limits, finite leases, overload admission and automatic recovery
  conditions. Add native-to-controller regressions for terminal mapping and
  post-zero recovery.

## 0.4.0b39

- Add an explicit, separate meter-envelope control policy for the existing
  import-only automatic overload profile. Upgrades leave this option off.
  Fresh grid measurements and actuator health govern continued operation;
  uncertain vehicle estimates can withhold an increase but cannot independently
  order an ordinary charging stop.
- Give every stop in this policy a typed electrical or control fault owner.
  Legacy startup, partial-ramp and ordinary decrease observation deadlines do
  not participate in its decisions. Accepted commands, unresolved authority,
  finite leases and genuine electrical response deadlines remain distinct.
- Separate critical grid/control health from vehicle estimate quality, and
  retain real command admission, delivery and overload durability checks.
  A pending ordinary metadata checkpoint may retain only the genuinely live
  accepted normal offer; it grants no extra overload authority.
- Preserve the configured peak response, minimum current, grid confirmation,
  cumulative overload budget, automatic low-load recovery and offline limits.
  Enabling the new policy does not reset exposure or historical faults.
- Keep a separately qualified ordinary current decrease available below the
  peak trigger when it restores the normal budget without granting new
  overload permission. Continued measured overload remains protected.
- Add independent controller and physical-plant regressions for asynchronous
  observations, partial ramps, lease renewal, storage/HTTP delays and genuine
  protection faults. Physical verification is still required after activation.

## 0.4.0b38

- Under the existing peak-triggered policy, assess independent normal operating
  evidence before an observation-only
  decrease or stop. An elapsed command-observation window does not invalidate
  the same accepted offer when fresh grid data independently bounds the whole
  unresolved offer within the normal buffered limit.
- Separate permission to continue and renew that same finite offer from proof
  that the vehicle responded to a particular command. Preserve unresolved
  higher authority and the original response history; cached vehicle state
  grants no new response event, EV-based increase or response window.
- Keep source clocks and source-kind metadata in the bounded diagnostic
  projection, including after the sensor's second compaction pass. Diagnostic
  size limits remain unchanged.
- Preserve electrical response limits, source and delivery faults, durable
  command admission, finite leases and overload accounting. This change does
  not establish the cause of an offered-current zero received before a local
  zero command.

## 0.4.0b37

- With the existing import-only raw fallback enabled, release a completed
  normal measurement recovery while a connected vehicle is waiting without
  drawing power. The existing 30-second observation and
  advancing fresh grid reports must prove that raw L1 plus the whole previous,
  accepted and prospective normal offer fits the normal buffered budget.
  A full vehicle therefore need not draw current to release an obsolete 7 A
  recovery ceiling. A higher offer still requires its ordinary durable command
  submission and actual acknowledgement.
- Let an independently whole-raw-bounded waiting offer reach the ordinary
  current calculation and timely finite-lease renewal. Keeping an old accepted
  cap without writing must not indefinitely suppress a safe increase or delay
  renewal until after expiry. Active response observation keeps its fixed
  deadlines and receives no new response time from a renewal.
- Keep restart, retry, source, lease and electrical fault guards. The existing
  normal recovery exit does not reset overload exposure, cycle budget or
  historical faults, and sends no start or resume command.

## 0.4.0b36

- Keep committed normal command authority separate from proposed changes.
  One shared normal-operating proof can preserve the accepted cap across
  delayed reports, connected waiting labels and unfinished metadata writes.
  It grants no increase, resume, lease renewal or overload permission.
- Preserve a qualified accepted normal decrease through an incomplete native
  publication/cohort at nominal grid load, using the existing fixed one-second
  processing fence and original response/lease bounds. A completed coherent
  pair can independently qualify the same cap during a pending checkpoint.
- Pin native event baselines at actual SDK dispatch, before the HTTP reply.
  Compare provider timestamps within their own domain; a coarse provider
  timestamp need not be later than the local acknowledgement. Genuine in-flight
  observations retain their original response time and electrical deadline.
- Handle ordered native value changes inside one provider timestamp bucket.
  Duplicate and ambiguous repeated values receive no new response credit;
  replay, future-clock, source-binding and numeric checks remain in force.
- Distinguish a proven local refusal before SDK dispatch from an ambiguous
  dispatched command. Defer locally refused work without inventing an API
  attempt, protective zero or retry debit. Reserve request capacity for real
  protective minimum decreases and zero commands within the existing SDK
  circuit-limit quota.
- Validate configuration before canceling in-flight work, make identical
  settings updates inert, enforce effective automatic-profile current bounds,
  and handle an absent raw-restart observation without a background exception.
- Save a small private native-event timeline at an incident, including offered
  pilot zero before a software stop. Normal diagnostic telemetry stays in memory and does
  not create periodic journal writes or control evaluations.
- Add an independent synthetic plant and virtual event scheduler covering
  source clocks, partial ramps, HTTP/Store delays, physical TTL and electrical
  protection. Installation still requires supervised physical verification.

## 0.4.0b35

- Let a qualified expired startup hand off to an independently raw-bounded
  lower offer even when its partial current and grid values look numerically
  consistent. Keep the previous higher authority unresolved until the new
  lower acknowledgement and genuine response are observed.
- Preserve that accepted decrease through delayed higher-current feedback
  with its frozen proposal anchor, source-aged observation budget and finite
  lease. Waiting makes no charger call and grants no overload permission.
- Connect an already accepted minimum of at most 7 A to the configured
  shared grid-confirmation phase after a timely genuine minimum response.
  Its first response owns the fixed deadline; renewals and late feedback
  cannot extend it or revive it after expiry.
- Apply absolute watchdogs, hard no-write guards, checkpoint revalidation and
  conservative interrupted-state restore to these observation handoffs.
- Preserve an already established normal cap across a replay-clean cached
  native MAX-winner change only when that entire existing offer still fits
  the buffered budget. Keep the original receiver provenance and conservative
  current/house bounds; grant no increase, restart or response credit.
- Include the resolved house basis and transition proof context in the
  private stop journal to distinguish these decisions from charger-first
  interruptions.

## 0.4.0b34

- Keep an already accepted startup/rise response with its existing owner
  before ordinary lower-offer routing can reinterpret an EV-first report.
  A genuine partial vehicle ramp ahead of the meter can retain the accepted
  cap within the original bounded observation phase, rather than triggering
  an unnecessary decrease followed by a low-grid stop.
- Preserve the same source binding, finite lease, household anchor and
  original response deadline. This changes routing, not overload admission,
  exposure accounting, source timestamp ordering or electrical peak limits.

## 0.4.0b33

- Permit independently raw-bounded normal recovery while an automatic
  overload cycle has not been admitted. This prevents a steady low-current
  EV report from leaving the accepted cap stuck solely because the extra
  overload allowance is unavailable. No uncertain EV current is subtracted.
- Preserve the existing one-second, no-write native publication hold for
  an accepted normal decrease under that same automatic-cycle condition.
  Its original lease, household anchor, source checks and fixed deadline
  remain binding. This grants no renewal, increase or overload permission.

## 0.4.0b32

- Add default-off Charging Readiness While Disconnected. Fresh raw grid
  headroom can prepare a finite normal limit before plug-in, without cached
  EV subtraction, overload permission or start/resume commands.
- Carry a genuinely acknowledged readiness offer into a healthy connected
  startup without an idle restart delay or an intervening zero. Preserve its
  original lease, source identity and protective barriers; cached EV values
  are not a new vehicle response.
- Withhold a proposed normal increase during pending checkpoint verification
  while retaining a separately proved, already accepted normal offer. This
  sends no charger call and extends no lease.

## 0.4.0b31

- Preserve an already accepted startup/rise offer through partial vehicle
  reports before evaluating a conflicting overload estimate. The prior house
  anchor plus the entire accepted offer and fresh raw L1 must both fit the
  normal buffer. One fixed observation deadline, source binding and live
  lease apply; the hold sends no write, renewal, increase or resume.
- Treat an automatic period that has not qualified as an admission condition,
  rather than manufacturing a failed electrical episode. Repair only the
  narrow, unused false admission marker under fresh normal-source conditions;
  exposure, historical failures and actual protective barriers remain.
- Distinguish a still-running shielded checkpoint from a completed storage
  failure. Independently raw-bounded normal decreases may remain possible
  during pending verification; new overload, increase and retry permissions
  still require a verified durable checkpoint.

## 0.4.0b30

- Remove unused internal state and the unused controller name property, while
  retaining saved-option and frozen-policy compatibility.
- Make manual trial buttons unavailable during automatic overload cycles.
  Availability follows live controller updates; direct stale-UI calls are
  rejected before any manual transaction or charger action.
- Remove help labels for already hidden legacy options and correct outdated
  import-fallback, ordinary-response and legacy 6 A wording. Current electrical
  thresholds, response rules and exposure accounting are unchanged.

## 0.4.0b29

- Add default-off **Automatic Overload Cycles**, requiring the peak-triggered
  response policy. Advancing raw L1 observations above 25 A for 30 seconds
  qualify an automatic period; extra overload permission during qualification
  already requires an at-most-7 A EV offer, sustained ceiling of at most 29 A
  and the same 1800-second cumulative budget above 25 A. Independently proven
  normal buffered offers remain allowed. A reading at or below 25 A clears unfinished qualification
  without refunding time. Re-arm requires fresh, advancing raw L1 strictly
  below 23 A for 1800 continuous seconds, including for a partially used cycle.
  Gaps, restarts and rebinding break that observation. Lifetime exposure and
  historical faults remain; current guards are not cleared. This is an
  operational test rule, not verified fuse cooling. Default/manual behavior
  remains unchanged.
- Add the explicit, default-off `peak_only_response` installation policy.
  Only a validated raw L1 sample above 28 A and within the configured hard
  peak limit creates its electrical response countdown. Ordinary observation
  work cannot create that countdown from a house estimate or numeric mismatch.
  Existing installations retain their prior policy until deliberately selected.
- Under that policy, keep an independently raw-bounded lower offer while
  trustworthy observations align. Its entire minimum must fit beside raw import
  inside the normal buffer, with zero EV subtraction; no increase or resume is
  authorized by this fallback. A genuine post-ACK minimum response may enter
  the fixed grid-confirmation phase without a fictitious electrical deadline.
- Keep a single source-aged peak owner across repeated frames and threshold
  oscillation. Pending peak state is interrupted on restore/reset; neither
  loading the update nor selecting the policy refills the exposure budget.
- Use one fixed post-minimum confirmation model for low-grid alignment,
  bounded nominal shedding and an explicit overload transition when the
  optional confirmation window is enabled. The new policy separates ordinary
  response admission from the at-most-10-second electrical peak interval;
  another route cannot reinterpret the active observation phase as a fresh
  unobserved electrical response.
- Debit retry throttles only immediately before an actual adapter invocation.
  A proposed 7 A request discarded during checkpoint/source verification no
  longer consumes the action interval without ever reaching the adapter.
- Reject a known post-response high grid sample using its directly associated
  EV provider-event floor even when the local EV receipt arrives later. This
  stricter rejection floor cannot create response credit, shorten the original
  response interval or prove successful confirmation from an earlier sample.
- Retire a fully qualified raw-budget rise response before its unconfirmed
  response-timeout branch. If an unconfirmed rise times out and explicit
  import-only/bounded control prove that the complete minimum fits beside raw
  grid, hand its existing authority to a distinct source-aged minimum decrease.
  This grants neither more time to the rise nor new overload permission.
- Preserve source provenance, leases, peak/ceiling protections, terminal zeros,
  manual policy, recovery barriers and cumulative trial history. Download or
  restart does not start a new trial or clear a spent budget. Observed report
  order still does not prove synchronized physical sampling or timely cloud
  delivery; the cause of every native charger interruption is not established.

## 0.4.0b28

- Add a separate, default-off `grid_confirmation_seconds` setting (0–30 s)
  for awaiting a later grid report after a qualifying minimum-current EV
  response. The original EV response window remains at most 10 seconds.
- Anchor the optional wait to the first qualifying response and the accepted
  offer's existing lease. Repeated reports and GET receipts do not restart it;
  no API command or TTL renewal is sent while awaiting confirmation.
- Keep peak, source, lease, cumulative exposure and stronger guard protections.
  A new grid report above the applicable normal/trial ceiling still stops the
  wait. The setting grants neither an unconditional 30-second hold nor a new
  overload allowance.
- Freeze the setting in explicit trial policies. Accept older policies with
  the missing setting interpreted as disabled only in the validation copy;
  retain original history, exposure and failures. Restored active/pending
  periods remain interrupted.

## 0.4.0b26

- Match HA's 15-significant-digit numeric representation without adding an
  ampere tolerance. Retain the selected source's exact current and real receipt
  instead of discarding its clock because the sensor state was serialized.
- Commit complete raw conductor records atomically. Native/HA publication can
  precede the additive observer; it must not mix a new number with old metadata
  or manufacture a missing-receipt stop during that publication.
- Fence positive writes during native/raw divergence with one nonrenewable
  local watchdog of at most one second, bounded by the existing alignment
  setting. Keep source, peak, zero, lease and original response protections.
  Invalid raw records on any conductor invalidate the current tuple.
- Add HA 2026.10.4 to real HA/SDK CI coverage. Preserve all electrical limits,
  historical exposure/failures and explicit overload-period permissions.

## 0.4.0b25

- Track the selected charger's individual current observations through the
  additive ProductUpdate observer. An update of another conductor can no
  longer make an unchanged cached maximum look like a fresh current report.
- Separate local observation clocks, component identity/sequences and directly
  associated provider timestamps. Pin response-owner acknowledgement floors;
  a clock-family change or cached GET receipt supplies no response credit,
  fresh deadline or increase permission. Preserve per-source replay checks.
- Keep a qualified positive-response house anchor through an EV-first partial
  response until its first corresponding grid report, within the original
  budget. Repeated partial reports cannot move that first-response floor.
- Coalesce a changing current tuple briefly before new positive authority.
  Decreases, zero requests and ordinary identical renewals keep their existing
  dispatch path. Preserve raw-bound bootstrap and the first genuine response
  without requiring another value change from an otherwise steady vehicle.
- Add Home Assistant 2026.10.3 to the real HA/SDK test matrix. Preserve all
  fuse limits, response timeouts, recovery barriers, exposure and explicit
  trial permissions. This update does not start a period or establish the
  physical cause of every native status/current drop.

## 0.4.0b24

- Preserve a qualified unresolved previous current bound when an accepted
  intermediate reduction needs a further decrease to 7 A. A lower API offer
  is not proof that measured draw has already responded. Keep the original
  chain's remaining alignment/response deadline, source identity, lease and
  post-acceptance observation floors through the handoff.
- Bound detached entity diagnostics to 11 KiB of serialized JSON, including
  escaping and sensor decision fields, so Recorder can retain them. Expose
  compact recent summaries, full-ring counts and explicit truncation metadata;
  keep the full private incident journal. Each stop now retains its four
  preceding positive limit receipts and matching delivery summaries.
- Preserve exposure, historical failures and explicit trial state. This update
  neither starts a new overload trial nor replenishes time. It does not add a
  waiting-status grace or establish the cause of charger-first output drops.

## 0.4.0b23

- Add explicit Start/End Overload Trial buttons and period state/time sensors.
  Start requires an already charging, healthy normal-load session; it sends no
  charger command and cannot override normal recovery or delivery barriers.
- Freeze each explicit period at 7 A minimum, 29 A sustained total, 1800
  cumulative seconds above 25 A and the existing 38 A/10 s response bounds.
  Preserve lifetime exposure and historical failures separately; dips do not
  replenish time and a new period never starts automatically.
- Verify pending and active checkpoints before granting memory permission.
  Interrupt every restored pending/active period and end permission on source,
  policy, reset or storage failures. Duplicate presses and late writes cannot
  refill or restore a period. Legacy records without a trial retain b22 behavior.
- Keep normal minimum current, existing source protections and command leases.
  This software trial does not estimate fuse cooling or guarantee physical
  response, uninterrupted charging or exactly 30 minutes of available time.

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
