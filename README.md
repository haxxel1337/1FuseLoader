# 1FuseLoader

Home Assistant custom integration for Axel's Easee/BMW single-phase fuse guard.

This is a **soft load balancer** for an Easee charger where a BMW is effectively charged on one phase. It monitors grid current, estimates house load, and then dynamically lowers/raises Easee's circuit limit. It can also soft-limit, pause, and resume charging when L1 gets too high.

> Safety note: this is not a certified hardware fuse guard. Keep proper electrical protection in place.

## Target

- Home Assistant Core: **2026.10.1** (minimum and CI target)
- Compatibility adapter: **Easee 0.9.74 / pyeasee 0.8.17 or 0.8.19**; unsupported versions block writes. The audited upstream revision with 0.8.19 is `397eaedd1a56a95e81bfa37d45130349ba3de7a8` (the version label alone does not identify this revision).
- HACS/custom repository layout
- UI setup through config flow
- Runtime tuning through options flow and Number/Switch entities
- Brand assets in `custom_components/1fuseloader/brand/`

## Repository layout

```text
custom_components/1fuseloader/
  __init__.py
  manifest.json
  config_flow.py
  logic.py
  sensor.py
  number.py
  switch.py
  button.py
  strings.json
  translations/
  brand/
hacs.json
README.md
CHANGELOG.md
test_1fuseloader_logic.py
```

## Default entities

These defaults match the original installation and can be changed in the setup UI:

- `sensor.parking_status`
- `sensor.l1ampfusecurrentlyuse`
- `sensor.l2ampfusecurrentlyuse`
- `sensor.l3ampfusecurrentlyuse`
- `sensor.parking_current`

The Easee `device_id` is **not** hard-coded because it is specific to each Home Assistant installation. Enter your own Easee device ID during setup.

## Logic

### Late phase identification during normal recovery (b43)

The first normal command can precede the charger's valid phase identification.
An actual accepted normal renewal can then bind a missing or changed-scope
mapped-L1 floor from that renewal's captured SDK boundary. A valid existing
same-scope floor is preserved across renewals. Neither a mapping update nor an
HTTP acknowledgement supplies the subsequent physical measurement: recovery
still needs a new qualified L1 sample after that SDK and later coherent grid
feedback. This repairs normal provenance only; original electrical deadlines,
finite authority, overload permission, cycle/exposure and history are unchanged.

### Standalone meter control and operational recovery (b42)

The selected meter policy has one active electrical episode and five primary
states: normal operation, reducing, grid confirmation, minimum hold and stop
recovery. It keeps actual accepted/possibly delivered SDK authority, current
fault recovery, cumulative exposure and incident history separate. The default
legacy policy remains available for entries that have not selected meter
control. Selecting the new policy does not enable writes or refill a budget.

A historical timeout is not a current electrical fault. A blocked **unspent**
cycle can recover after healthy bound sources, retirement of earlier possible
positive authority, the configured stable normal restart interval, an actual
accepted normal command, a new qualified mapped-L1 response and later coherent
normal grid feedback. This retains the same cycle id, used seconds, lifetime
exposure and historical record. It does not create a new overload period.
An exhausted cycle still needs 30 continuous observed minutes strictly below
23 A to replenish its budget. Operational recovery is not a fuse cooling model.

Ordinary meter control does not require the old estimator/peak/automatic-cycle
flags. Experimental Timed Overload gates extra overload permission; selected
meter control owns its automatic exposure accounting. Manual trial actions are
unavailable in this mode. Numerical installation bounds and finite leases
remain enforced. Unknown EV estimates suppress unsafe increases while an
independently allowed normal lease can continue.

The existing 38 A/10-second transition describes reducing a higher or
unverified offer, followed by at most 30 seconds for grid confirmation after
a genuine minimum response. This update does not grant every new spike above
29 A another window when 7 A is already verified. Actual source/control faults,
unknown delivery, lease expiry and electrical limits still have stop authority.

### Controlled L1 evidence and post-zero recovery (b40)

The selected meter policy distinguishes aggregate input RMS from current on
the phase it controls. Easee documents output mode `110 = 10` as TN single-phase
N+L1 on T2/T3; its observation table describes T2/182 as calculated input RMS
and T3/183 as input current RMS. A validated live mode-10 mapping can therefore
supply separate T3 evidence for controlled L1. This is not a minimum-of-all-
terminals rule. See [output phases](https://developer.easee.com/docs/enumerations)
and [terminal observations](https://developer.easee.com/docs/amqp-connect).

Mapped L1 evidence retains its own native receipt, sequence, provider ordering
and mapping identity. A phase/context update is not a new current sample.
Cached state, pilot offer, HTTP acknowledgement and cloud GET cannot prove a
post-dispatch current response. Unknown, changed, IT or multiphase mappings
do not inherit this narrow L1 capability. Stable valid mapping need not change
every time current changes.

From b41, the provider ordering floor is compared within that provider's own
clock domain. It is not required to exceed HA's SDK-entry wall-clock bucket.
A genuinely new fast native response can carry an advancing provider timestamp
that still predates the local SDK time because those clocks have an offset.
Local native receipt and sequence must advance after the actual SDK boundary;
all existing mapping, replay and freshness checks still apply. These receipt
checks do not establish synchronized physical acquisition times.

Aggregate MAX remains a distinct diagnostic/upper bound. It cannot be blindly
subtracted from grid L1: an inflated neutral value would understate house load.
A higher offer needs independently eligible mapped L1 and coherent later grid
evidence. Response proof alone grants no increase and no new response window.
Legacy control retains its separate representation.

After a protective zero, only real possible positive commands can own a
positive uncertainty horizon. Sending or acknowledging another zero cannot
move it forward. Genuine physical-off evidence or expiry of the original
possible-positive horizon can retire active uncertainty; fresh normal
headroom and the existing restart interval then govern ordinary recovery.
Historical faults, blocked/spent overload permission, exposure and user pauses
remain distinct and are preserved.

### Separate meter-envelope policy (b39, explicit opt-in)

`meter_envelope_control` selects a separate controller. It defaults to **off**
and requires the configured import-only, peak-triggered automatic overload
profile, dynamic current control and a maximum offer of 16 A. Existing
installations retain the legacy policy until the operator explicitly enables
it. The sections below describing older observation windows apply to that
legacy policy.

The meter-envelope controller distinguishes three inputs: validated grid
current, critical actuator/control health, and vehicle estimate quality. A
late, incomplete or inconsistent vehicle estimate can prevent a higher offer.
It cannot by itself turn an ordinary observation deadline into a 0 A command.
With fresh valid grid/control inputs and a genuinely live accepted offer, the
controller can retain or lower that offer under measured-grid supervision.
This is measured feedback, not a certificate that the entire unresolved
vehicle allowance fits below the buffered nominal limit. A higher offer still
requires independently eligible evidence.

Every 0 A decision has a typed critical cause. Real grid/control faults, unknown
delivery, expired leases and actual electrical limits keep stop authority.
Normal observation bookkeeping cannot borrow that authority or create restart
debt. A plan, an SDK request, a successful HTTP response, a returned limit and
physical vehicle response remain different events.

After a real peak, an actually accepted live 7 A offer may continue when a
new validated grid report has returned to at most 25 A. A delayed EV report
alone then does not order a stop. This grants no increase or EV response
credit: unresolved higher authority and the original peak deadline remain.
If high load returns without independent qualifying evidence after that
deadline, there is no fresh ten-second allowance. A separately qualified
normal operating state can retire the old electrical need without claiming
that the vehicle answered the earlier command on time.

The selected profile's electrical rules and persisted accounting remain in
force: 7 A minimum response to a measured peak above 28 A, the original 10-second
response deadline, the separately configured grid confirmation, the 29 A
sustained ceiling, immediate protection above 38 A, and the 30-minute cumulative
overload budget after qualification. Automatic recovery still requires 30
continuous minutes strictly below 23 A. These are experimental operating
settings, not a verified thermal rating for a particular fuse. Policy selection
does not reset exposure, manufacture a lease or resume a user-paused session.
The existing 90-second reserve for admitting a finite overload lease also
remains: a new 60-second lease is refused when that reserve no longer fits.
Consequently a continuous overload can stop before all 1,800 seconds are used.

### Observation expiry and normal continuation (b38)

A command-response observation and permission to continue an accepted normal
offer are separate decisions. Under the existing peak-triggered policy,
before an observation-only decrease or stop,
the regulator checks independent normal evidence for the entire unresolved
accepted offer. Fresh raw grid current plus that whole offer can establish
normal permission without subtracting uncertain vehicle current. Qualified
same-receiver native state uses its original evidence and matching joint
house-load/upper-current bound.

Independent normal evidence can retain and ordinarily renew the same accepted
finite offer. It does not prove command causality, advance a vehicle clock,
erase unresolved higher physical authority, extend a response window or grant
an EV-based increase. A separately qualified whole-raw increase remains a
different operation. Bounded status and incomplete-publication holds remain
no-write holds under their original deadlines and live lease.

Actual electrical response deadlines, invalid sources, unknown delivery,
storage failures, retry barriers and overload accounting still take precedence.
Source clock metadata remains visible when larger historical diagnostics are
truncated; omitted or invalid metadata is not presented as fresh measurement.

### Normal operation and command evidence (b36)

An actual accepted command and a proposed command are separate records. A
proposed lower offer cannot erase the still-live accepted cap or its normal
house-load bound while metadata is being saved. One common normal-operating
qualification governs retention across connected waiting labels and delayed
reports. Independent normal-state retention requires the whole unresolved
accepted authority to fit the normal buffered envelope, with the original
source binding, native receiver and finite lease.
An anchor-only waiting hold retains only that cap; it sends no command,
renewal, increase or resume. Independently qualified same-cap renewal under
the peak-response policy is described above.
Independent fresh raw-grid headroom can still authorize an ordinary offer
without subtracting uncertain EV current.

Normal operation and command response answer different questions. A fresh
trusted native current report and later coherent grid can qualify the existing
normal offer without proving that a particular API call caused the current.
This can discharge measurement-only observation work. Unconfirmed higher
physical authority remains until genuine response evidence resolves it.
It cannot clear a real
peak, an interrupted recovery barrier, an overload failure or spent exposure,
or create another response window or overload cycle.

Under the opt-in peak-triggered policy, a qualified, actually accepted normal
decrease can also retain its lower cap through an incomplete native publication
or short conductor cohort while raw grid current remains at most the nominal
fuse rating. This uses the existing fixed processing fence of at most one
second, capped by the original lower-response deadline and lease. It sends no
API request, renewal or increase. Churn cannot refill the fence; source faults,
electrical protections and an expired original bound still take precedence.
A completed coherent pair is assessed independently against the full normal
buffered authority, including while a healthy metadata checkpoint is pending.

Response observation floors are pinned immediately before the SDK request,
after local admission. HTTP acceptance commits the command; it does not start
a fresh response clock. A genuinely advanced native observation received while
that request is in flight can be considered after successful acceptance using
its original receipt and the original deadline. Provider timestamps are
checked within their own source domain rather than required to exceed the
local HTTP acknowledgement time. [Easee's command example](https://developer.easee.com/docs/amqp-commands)
shows why these times need not have that ordering.

An ordered native value change can share a provider timestamp bucket with an
earlier value. A bounded per-terminal history distinguishes a first unseen
value from a duplicate or ambiguous return. Ambiguous reported state grants
no new measurement, response or EV-based increase credit; independent whole-raw
normal permission remains separate. GET replies cannot create bucket events.
True timestamp regression, invalid clocks and source rebinding still reject
the data.

A proven local refusal before SDK dispatch records no attempted API command,
acceptance, lease or retry debit. It defers through one bounded timer while
valid committed normal authority may remain unchanged. Once the SDK starts,
an unknown result remains a stronger protection. Normal requests use at most
eight local circuit-limit slots per minute; genuine protective decreases can
use three reserved slots while leaving a slot for zero inside the audited
12-per-minute SDK circuit-limit quota. A resume uses one circuit-limit call
plus a separate charger call; pause uses no circuit-limit call. The SDK's
general request quota is separate. No local reservation guarantees shared
network capacity or device delivery.

Private incident diagnostics include a small allowlisted native-event ring so
an offered-current zero can be distinguished from a later software zero.
Diagnostic telemetry stays in memory during normal operation. It neither wakes the control loop nor
creates periodic incident-journal writes.

The 29 A allowance, 38 A peak ceiling, original at-most-10-second electrical
response, selected at-most-30-second post-minimum grid confirmation and
automatic qualification/accounting/re-arm policy below are unchanged. A
planned HA restart still stops lease renewal: a one-minute lease can expire
to the configured offline fallback before HA returns. That planned interruption
is separate from an avoidable zero during ordinary operation. See
[Easee's TTL and fallback description](https://developer.easee.com/docs/load-balancing).

### Normal recovery beside an unadmitted automatic cycle (b33)

An automatic overload cycle that has not qualified grants no extra overload
permission. That condition alone does not veto separately proved normal
operation. With the explicit verified import-only option, an already accepted
7 A offer beside fresh raw L1 of 11 A can qualify a conservative 12 A offer:
the entire proposed offer plus raw grid current fits the 23 A normal buffer,
without subtracting old EV current. A genuine vehicle response and later grid
feedback can then qualify further normal recovery. A matching cloud read
still cannot manufacture a new native current event or permission to jump
directly to maximum.

The same distinction preserves the existing native publication hold during
an already accepted normal decrease. It retains that cap without an API call
for its original fixed interval of at most one second, bounded by the original
lease and remaining observation budget. Churn cannot renew it. Source faults,
binding changes, invalid clocks, actual peaks and stronger guards retain
priority. Neither path admits a cycle, refunds exposure or extends an overload
deadline.

### Startup expiry and accepted minimum confirmation (b35)

An expired accepted startup offer can hand off to an independently safe
strictly lower ordinary offer. This is considered even if a partial EV report
is numerically consistent with the grid but still lacks a later meter sample.
The entire lower offer must fit beside fresh raw grid current within the
normal buffer. This ends the old startup owner without granting more time or
retaining the expired higher offer. The earlier higher authority remains
recorded until the actual lower ACK and subsequent physical-report evidence.

After that lower ACK, the same decrease can retain only its accepted cap
through delayed feedback from the previous higher offer. Its conservative
whole-raw proposal anchor, source-aged observation end and original lower
lease are fixed. Fresh raw L1 must remain at or below the main fuse rating;
waiting sends no write, renewal, increase or resume and subtracts no uncertain
EV current. A higher raw load is delegated to the existing overload and peak
policy. This observation phase cannot grant a 29 A allowance or restart an
electrical response clock.

An already accepted startup minimum of at most 7 A can use the configured
shared grid-confirmation phase when a genuine native post-dispatch response
qualified within the original startup window. The first qualifying response
owns its fixed grid deadline, capped by the existing lease. A copied GET,
late response or same-cap renewal cannot create another window. This is the
same configured post-minimum confirmation duration used by the other response
routes, not an extension of the original startup window or permission to
hold a higher startup offer for longer.

Both waiting routes use absolute watchdogs and prohibit charger calls.
Checkpoint completion rechecks the latest sources and the original deadline;
a later proposal cannot silently replace the retained accepted cap. Restoring
an interrupted pending observation does not resume it with fresh time.
Invalid sources, changed bindings, expired authority, actual peaks and stronger
configured guards retain priority. Physical current and power must still be
observed after activation.

An established normal offer can also survive a change between already known
cached native current records. The original native receiver, report family,
source binding and replay-clean observation vector must still match. The
lower of the established and cached current values gives a conservative
subtraction floor, and any retained house-load bound remains a lower bound.
The entire existing offer must still fit the normal buffered budget. This
only retains or renews the same accepted cap: it does not create a new current
measurement, authorize an increase or restart, retire an unresolved reduction,
or bypass an active response owner. A new receiver, invalid clocks, replay,
insufficient budget or stronger guard still rejects this continuation.

### Charging readiness while disconnected (b32, explicit opt-in)

**Charging Readiness While Disconnected** (`disconnected_readiness`) is off by
default. When enabled, an actually disconnected charger can receive and renew
a finite normal circuit limit before the vehicle connects. The calculation
uses fresh raw grid current without subtracting a cached vehicle reading:
`floor(main fuse - buffer - raw L1)`, bounded by the configured startup and
maximum current. Other phases remain zero. This grants no overload permission
and sends no start, resume, authentication or scheduling commands.

Only a successful adapter acknowledgement creates an in-memory readiness
owner. Its original finite lease, source identity and normal headroom proof
remain binding. A healthy connection can hand that accepted offer into the
normal startup observation path without an artificial idle restart delay or
an intervening zero. Old vehicle measurements do not count as a new response.
Completed, paused and scheduled sessions are not disconnected readiness.
Source, delivery, actual peak and durable protective guards remain active;
readiness is not restored as an accepted offer across a Home Assistant restart.

While a checkpoint is still being verified, a previously accepted normal
offer may remain unchanged when independently safe, even if a higher normal
offer is available. The higher proposal is withheld: retaining the old offer
does not renew its lease or authorize another charger call.

### Accepted startup and rise responses (b31)

With the peak-triggered policy selected, a partial vehicle ramp does not become
a new household-load anchor before the accepted startup/rise has responded.
For example, an accepted 14 A offer beside a previously qualified 8.4 A house
load fits the 23 A normal buffer. A later 22.6 A grid report paired with a
partial 1.8 A EV report can retain that same offer while the reports catch up.
Fresh raw L1 and the entire offer beside its prior house anchor must both fit
the normal buffer, with the same binding, live lease and valid source guards.
The hold sends no write, renewal, increase or resume.

From b34, this existing startup/rise owner is considered before the ordinary
lower-offer fallback as well as the low-grid fallback. An EV-first partial
report therefore cannot replace the same active response with a new lower
offer merely because the meter still shows the pre-start household load.
The original owner must qualify under its existing guards; it receives no
new lease, observation time or authority from this routing precedence.

The first qualifying mismatch fixes one source-aged observation deadline inside
the existing configured alignment budget. Grid-first observations provide no
EV response credit; later partial reports cannot restart the clock. A genuine
timely vehicle report and a strictly later coherent grid report complete the
owner when the derived house load plus the entire offer fits the normal buffer.
An offered limit is an upper bound: a vehicle drawing 7 or 8 A may legitimately
keep a 16 A offer. It is not required to draw 16 A to complete this check.
Source faults, an expired owner/lease, actual peaks and stronger configured
guards still take priority. A validated peak above 28 A may attempt a strict
decrease to the existing permitted minimum before a delayed EV estimate alone
rejects its projected household load. It retains the original electrical
deadline and requires the later actual minimum/grid ceiling checks.

A two-second foreground checkpoint timeout can leave the serialized atomic
write and independent readback running. B31 distinguishes that pending state
from a completed storage failure. It grants no new overload, increase or retry
permission. A separately proved normal decrease can still fit its entire offer
beside raw grid current. A qualified startup owner or a fresh coherent normal
offer can retain only the same already accepted live cap without renewing its
lease. Source/peak/deadline checks remain active;
late storage completion schedules a fresh evaluation and cannot revive a
protected zero. Completed write failures remain visible and protective.

### Automatic overload cycles (b29, explicit opt-in)

**Automatic Overload Cycles** (`automatic_overload_cycles`) is off by default.
It requires **Peak-triggered Response Policy**; **Experimental Timed Overload**
remains the separate gate for allowing overload operation. Existing entries keep
the manual policy unless this option is deliberately enabled. The automatic
profile grants extra overload permission only for a 25 A main fuse, an EV offer
of at most 7 A and a sustained raw L1 ceiling of at most 29 A. Independently
proven normal offers may still use the normal buffered capacity. An already accepted lower offer is
never raised merely to enter this mode, and an idle vehicle is not started.
Configuration also requires a 7 A startup offer and effective current bounds
`6 <= minimum <= 7 <= maximum`. Invalid combinations are rejected before
interrupting a live request; they are not silently clamped to another automatic
profile current.
The manual **Start New Overload Trial (7 A)** and **End Overload Trial** buttons
are unavailable in automatic mode. Their availability updates when the mode
changes; a direct call from an outdated UI is also rejected before any manual
trial transaction or charger action.

| Phase | Observation and effect |
| --- | --- |
| Qualification | Fresh, advancing raw L1 observations must remain above 25 A for 30 seconds. Extra overload permission is already restricted to an EV offer of at most 7 A and sustained L1 of at most 29 A. Independently proven normal offers remain allowed. |
| Active cycle | At most 1800 cumulative observed seconds above 25 A are available, including qualification and peak-response time. This is not a fresh 30 minutes after qualification. |
| Re-arm observation | Fresh, advancing raw L1 must remain strictly below 23 A for 1800 continuous seconds. A completed observation can re-arm an exhausted or partially used cycle without a manual trial button. |

A reading at or below 25 A clears unfinished qualification without refunding
consumed time. During an active cycle, short dips pause exposure accounting;
they do not create a new allowance. Re-arm requires the complete below-23 A
observation: 23 A itself, a data gap, invalid or stale measurements, source
rebinding and HA restart break that observation. Cached high or low frames
cannot establish either continuous interval. Toggling options or restarting
does not manufacture a fresh budget.

For example, a fresh 26.6 A reading while the EV still reports 16.1 A may
permit an independently verified reduction to 12 A inside the normal 23 A
buffered ceiling. Qualification alone does not force that safe normal offer
down to 7 A. Any extra permission to remain above the normal envelope still
requires the bounded lower offer and the separate peak protections.

The initial 30-second qualification, electrical peak response and post-EV grid
confirmation are separate phases. A validated raw peak above 28 A and up to
the configured hard limit of at most 38 A retains its original, sample-aged
response bound of at most 10 seconds. A selected grid-confirmation window of
at most 30 seconds can follow a qualifying genuine minimum-current response;
waiting sends no API command or TTL renewal. Qualification and re-arm cannot
renew either deadline. A known later grid sample above the applicable ceiling,
excess peak, source fault, expired lease or stronger guard can stop earlier.

When time is exhausted, the extra 29 A permission ends. Charging may continue
only under independently proven normal limits; otherwise zero is requested.
Cycle accounting and interrupted state are persisted. Re-arming retains the
lifetime exposure and historical failure record, and does not clear a current
protective fault. Restarts and source gaps cannot count as observed recovery.

The below-23 A/1800-second rule is an operational test setting. It is **not a
verified fuse-cooling model**, a measured fuse temperature or a guarantee of
safe electrical overload. Cloud control and minimum-minute positive leases
also cannot guarantee an exact physical reduction or stop deadline.

### Raw-budget recovery and pending-reduction cohorts (b27)

With the verified import-only option enabled, unchanged EV telemetry need not
block every increase. A fresh raw-grid sample can authorize a higher offer
only when the **entire proposed offer plus raw grid current** fits the normal
fuse-minus-buffer budget. No old EV current is subtracted. The selected atomic
native tuple must remain valid and bound to the same charger. At bootstrap,
missing native receipts and zero event sequences stay missing and zero;
matching cloud reads cannot replace them with new measurement evidence.

An accepted raw-budget increase has one original, sample-aged response window.
Only genuine post-dispatch conductor observations and a later coherent grid
sample can establish its response. Renewals, checkpoint/API latency and late
replies cannot refill that window or revive a terminal stop.

When a qualified reduction is already accepted, sequential conductor updates
can briefly select another maximum with an old clock. A narrow processing
fence may retain that same lower offer **without a write** while the actual
tuple settles. Its first deadline is at most one second, bounded further by
the existing lease and response deadlines. Fresh raw grid must remain within
the normal buffer; invalid sources, high raw load, expired protection and
custom guards retain priority. Churn and cloud reads cannot extend the fence.
This adds no overload permission, budget, EV credit, restart or lease renewal.

### Numeric representation and atomic current records (b26)

Home Assistant serializes floating sensor values to 15 significant digits.
The selected SDK current and its HA state therefore need not be identical
binary floats. Match that exact representation boundary, then keep the
source-bound SDK number for control. This adds no current tolerance: different
representation buckets, invalid values and mismatched source bindings remain
different observations.

The native SDK can publish conductor states before the additive raw observer
has received the whole payload. The adapter now commits the four-conductor
record atomically and returns its numeric maximum together with the matching
component IDs, receipts, sequences and errors. An incomplete native/HA update
cannot replace that committed number or erase its actual observation clock.
Replayed values with unchanged source metadata grant no response credit.

While native publication differs from the committed record, positive writes
(including reductions and lease renewals) wait for coherence. A fixed watchdog
starts at the first mismatch, lasts at most one second and is capped by the
configured measurement-alignment window. Churn and reads cannot extend it.
Source faults, invalid raw records on any conductor, peak protection and zero
requests retain priority. Existing response, lease and overload deadlines
continue to run; this is no additional electrical response allowance.

A still-qualified accepted positive offer can be retained without a write
during this publication fence when fresh raw L1 fits the normal buffer and
the preceding qualified house estimate plus the entire accepted offer also
fits that buffer. This preserves the same response owner and its remaining
time; it supplies no new start, renewal or retry.

No historical failure, exposure, recovery barrier or explicit overload-period
permission is cleared. An enabled experimental switch still does not mean a
new 30-minute period has been started.

### Component observations and response clocks (b25)

Easee's Current entity is the maximum of four conductor values. Its HA entity
receipt can change when a different conductor is updated while the maximum
itself remains cached. The regulator now observes the selected charger's raw
current records separately, retaining the contributing component identities,
value-event sequences and actual local receipt times. A non-winning update
cannot refresh the maximum's evidence. These receipts are local observations,
not certified physical acquisition times.

Provider timestamps remain separate. A matching value in an old GET response
cannot retroactively identify or relabel a newer native event. A response owner
pins its actual SDK dispatch and component baselines; metadata enrichment,
clock-family switches and identical renewals cannot manufacture response,
refill alignment time or permit another increase. Genuine replay and invalid
metadata still fail the source checks. A first real post-dispatch component
event can complete bootstrap without requiring an artificial second value
change from a steady vehicle.

New positive authority briefly waits for the current tuple to settle. This
does not delay reductions or zero requests. A qualified EV-first partial rise
keeps its preceding house anchor only until the first corresponding later grid
report and within its original response budget. The coherent pair then governs
normal control, including a lower offer when needed. Source faults, expired
leases and existing protective conditions retain priority.

This changes neither the overload trial policy nor the configured response
timeout. An absent qualifying response can still stop control, and a native
status/current drop can precede the controller's decision. Passing source,
unit and adapter tests is not proof of uninterrupted physical charging.

### Accepted reductions and diagnostic history (b24)

An accepted lower circuit limit and the measured vehicle current can describe
different moments. During a qualified pending reduction, a further decrease
retains the previous unresolved physical bound while tracking the latest
accepted offer separately. The entire decrease chain shares its original
remaining deadline and source/lease guards. Intermediate offers and identical
renewals cannot restart that clock, prove physical response or grant a new
overload period.

Entity attributes contain detached, compact diagnostic summaries capped at
11 KiB of serialized JSON. Truncation is explicit; counts describe the full
bounded rings. The private incident journal keeps full records, and a stop
also retains four preceding positive limit receipts for later TTL/delivery
analysis. HTTP acceptance, reported limits, offered current and measured draw
remain separate evidence. A charger-first output drop is not established as
a controller fault merely because a later protective zero is recorded.

### Explicit experimental trial periods (b23)

This is the default/manual lifecycle. The separately selected automatic policy
above does not require these manual start/reset actions.

**Start New Overload Trial (7 A)** starts a separately recorded software
allowance. It sends no charger command and cannot start an idle vehicle. It
requires an already charging vehicle, a live accepted positive offer, verified
zero offline fallback, fresh coherent current reports and raw L1 within the
normal buffered ceiling (23 A with the default 25 A fuse and 2 A buffer).
Storage, command, source and normal-recovery barriers must be clear.

Each period freezes this policy: reduce to 7 A at high load, require at most
29 A total after the observed response, and count at most 1800 cumulative
seconds above 25 A, including the bounded response phase. The existing peak
ceiling of 38 A still applies. Under the default response policy, the
minimum-current EV response must qualify inside the original fixed response
window of at most 10 seconds. With the peak-triggered policy selected, that
electrical window applies to an actual raw peak, as described below. A separately
selected grid-confirmation window may follow only after that timely response.
An infeasible 7 A offer, missing or invalid measurements, failed response or
other protective condition may request zero earlier. This is not a fuse
temperature model or a guarantee of an uninterrupted 30-minute session.

Activation requires two independently verified checkpoints before permission
is granted in memory. Concurrent presses cannot create additional time. The
period has its own ID, frozen policy and baseline in the monotonic lifetime
exposure journal. A new explicit period does not erase the historical exposure
or failure; short load dips never replenish its time.

**End Overload Trial** withdraws that allowance and saves the terminal state.
It does not send a charger command itself; the next ordinary evaluation applies
the remaining normal protections. Restart, reset, source or policy changes and
source faults interrupt the period. Every restored pending or active period is
interrupted, so a late write or restart cannot resume it automatically. A later
manual period requires a new eligible, explicit start. This manual lifecycle
does not infer cooling or automatic re-arm.

### Command and response phases (b29)

**Peak-triggered Response Policy** (`peak_only_response`) is an explicit
installation option, available in Configure and as a Switch. It defaults to
off so upgrading an existing installation does not silently change its policy.

When selected, only a valid raw L1 observation **above 28 A** arms the electrical
response interval of at most **10 seconds**, within the configured hard peak
limit of at most **38 A**. No A/s derivative is invented. A house-load estimate,
cached EV observation or numeric mismatch at low grid load cannot arm it.
Repeated frames, dips and cap changes do not reset an unresolved peak's first
clock. A genuine completed response or an independent full-authority raw bound
must retire that event; restored/reset pending events remain interrupted.

Ordinary lower-current observation is a different phase. With explicit import-
only and bounded control selected, the entire lower offer may be retained when
it fits beside fresh raw import inside the normal buffer, with zero EV current
subtraction. Previous higher authority remains recorded; this grants no rise
or resume. A low-load mismatch or observation expiry alone does not create the
electrical ten-second countdown. Source faults, binding/control loss, lease
expiry and unavailable safe authority remain separate protective conditions.

An active overload period may hold its minimum at 26–28 A total
without inventing that peak countdown. Its 29 A ceiling and cumulative time
above 25 A remain unchanged. The peak-response option alone does not start a
period, clear history or turn a spent legacy budget into a new allowance. The
separate automatic policy applies only under its qualification and re-arm rules.
An existing stricter
accepted limit (for example 6 A) is never raised to 7 A during a peak.

A proposal, an actual adapter attempt, HTTP acceptance, reported EV response
and grid confirmation are separate events. Only an actual adapter attempt
starts the retry throttle. Discarding an unsent proposal after a checkpoint or
source revision neither debits that throttle nor restarts its response clock.

With the optional grid-confirmation setting enabled, low-grid alignment,
bounded nominal shedding and explicit overload transitions use the same fixed
minimum-response confirmation phase. Its first qualifying EV response owns
the only deadline, capped by the existing lease. Waiting sends no command,
increase, resume or TTL renewal. A completed response retires its old owner;
a protective zero ends the observation phase rather than leaving it alive.

A raw-budget rise has its own bounds and receives no minimum-response grace.
If that unconfirmed rise expires, explicit import-only/bounded control may
instead request a distinct minimum decrease when raw grid plus the entire
minimum fits the normal buffered budget. The old higher authority remains
accounted for while that decrease awaits its own post-dispatch response.
Its source-aged response deadline is fixed once; retries do not renew it.

An expired lease, invalid source/binding or terminal protection cannot use
this handoff. It does not activate an overload period or clear history.
Cloud acknowledgement and delayed telemetry remain best-effort evidence;
offline tests do not establish the cause of every physical interruption.

### Optional grid confirmation after a timely response (introduced in b28)

**Grid Confirmation After Timely Minimum Response** is a separate setting,
`grid_confirmation_seconds`, with **0** as its default. Zero keeps the existing
deadlines; upgrading does not automatically grant additional waiting time.
The setting accepts whole seconds up to **30** and is available in Configure
and as a Number entity.

When explicitly selected, this window applies only after a qualifying reported
minimum-current EV response. Under the peak-triggered policy, an actual peak
requires that response inside its original at-most-**10-second** interval;
ordinary lower-offer observation has no invented ten-second admission gate.
With the peak-triggered policy off, the earlier response contract is retained.
The window permits waiting for a later grid observation in the
low-grid minimum alignment, bounded nominal reduction or an explicit overload
period, including an automatic cycle. For a setting of
30, the fixed grid deadline is anchored to the **first** qualifying response.
Repeated current reports, HTTP receipts, GET requests and lease renewals cannot
start another 30 seconds. It grants no extra electrical peak-response time, current
increase, resume, new exposure budget or bypass of source/current protections.
While waiting, the controller retains only the already accepted live minimum
offer: it sends no API command and does not renew its TTL. The wait is also
bounded by that offer's existing lease expiry.

For rejection of a high grid sample, a directly associated, valid post-dispatch EV
provider event supplies an additional conservative floor when available. A
later local EV receipt must not disguise a grid sample already after that
known event. This floor cannot prove successful confirmation or create EV
response credit; success retains the stricter report-order requirement, and
the duration is still anchored to the first qualified local response receipt.

Only a high grid observation from before the qualifying minimum-current
response may remain unconfirmed during this phase. A new grid report at or
after the response that exceeds the applicable ceiling requests protection:
the full normal buffer for a nominal reduction, or at most 29 A in the
explicit default overload trial. The 38 A peak ceiling, source faults, lease
expiry and any stronger configured guards retain priority. Thirty seconds is
an upper bound for awaiting confirmation, not an unconditional hold.

All elapsed exposure still contributes to the existing cumulative accounting.
The trial freezes this setting with the rest of its policy. Changing it
interrupts an active trial; it does not extend a running deadline or erase saved
exposure and failures. Earlier trial policies without this setting retain their
original behavior, and restored active/pending trials remain interrupted.

Set this option through Home Assistant only after b28 has been loaded by an
approved restart. A newly downloaded component is not yet the loaded runtime.
Observed reports and cloud commands still do not establish synchronized physical
measurements or guarantee delivery within either software window.

The three **Overload Trial** sensors show state, used seconds and remaining
seconds. Their attributes distinguish period failure from historical failure
and lifetime exposure. An installation without a recorded trial or selected
automatic policy retains the legacy experimental behavior described below.
Upgrading does not clear a spent
legacy budget, start a period or start charging. Normal minimum current remains
separately configurable; the explicit period minimum is 7 A.

### Response ownership and conservative retention (b22)

The same accepted current change has one response owner. A qualified EV-first
rise stays with the existing positive-response context through both earlier
uncertainty gates. The accepted lease, selected source binding, original
alignment budget and conservative household anchor must remain valid. A
source fault, higher raw load or configured protective guard still takes
priority. Accepting or renewing an API request is not physical-response proof.

When a lower offer has already been accepted, a subsequent coherent grid/EV
pair can hand it to the remaining reduction guard. That handoff preserves
the previous possible offer, post-dispatch EV floor and earliest deadline.
It cannot count an EV event preceding actual SDK dispatch as response to the reduction.

An expired alignment clock alone does not imply excessive load. With the
verified import-only option, a fresh raw grid plus the **entire unresolved
previous offer** may establish a conservative normal-envelope bound. In that
case `low_grid_raw_hold` retains only the accepted lower offer. It grants no
new alignment time, increase, resume, EV subtraction or experimental allowance.
Ordinary same-cap renewal requires that whole-authority bound and a live lease
at every evaluation. Losing the bound or source/lease validity requests zero.
The unresolved hold is checkpointed; restoring it cannot create another
positive response attempt. A genuinely post-dispatch lower-current event
and later coherent grid observation can complete the held response.

These nominal-control repairs did not change the experimental overload contract
below. An enabled experimental switch with exhausted time or a saved failure
does not grant another 30-minute period. B23 adds the separate explicit start
described above; it adds no automatic reset to the default/manual policy.

### Command delivery and low-grid alignment (b19)

From b20, event age or skew alone cannot open a new low-grid alignment
episode. An old, unchanged EV event follows the existing freshness policy:
it provides no new credit for an increase, but does not manufacture a new
ten-second response obligation. A genuine numeric conflict still uses the
bounded alignment path and its original deadline, including after reentry.
The b22 ownership and whole-authority retention rules above apply before
opening a competing response or treating an expired clock as overload.

Each command records HTTP acceptance separately from an exactly correlated
Easee CommandResponse and native circuit-limit observations. A missing or
ambiguous correlation remains unverified. Vendor clocks are not used to
match requests. None of these receipts establishes physical current or TTL.
Old callbacks and late superseded replies cannot confirm a newer command.
Positive requests are serialized, and a new protective source condition can
cancel a waiting positive request. Cancellation cannot undo an already sent
cloud command; a subsequent zero is confirmed before another offer. Repeated
zero writes are suppressed only with healthy sources, offline limits zero,
post-request native zero evidence and a fresh trusted zero-current event.

Nonzero limit writes reserve capacity in the new SDK's shared rate limiter.
The entire queue/request wait consumes the local deadline; urgent zero still
depends on the shared SDK, network and cloud. No physical ten-second delivery
guarantee is made.

With the existing default-off **verified import-only** option enabled, a
source-bound low-grid/EV conflict can reduce a live accepted cap to 7 A for
one fixed window of at most ten seconds under the default response policy.
With peak-triggered response selected, ordinary observation has no invented
electrical ten-second deadline; actual raw peaks retain their separate bound.
The whole 7 A offer must fit below
the fresh raw-grid normal ceiling without subtracting EV current. A genuinely
advanced EV event and later coherent grid event complete alignment. GETs,
renewals, changing measurements and restarts cannot grant a new window.
Interrupted alignment is checkpointed and uses the existing bounded recovery
policy. Experimental exposure and failure latches remain preserved.

Upgrade the native Easee integration as a complete audited component, not
by editing its dependency manifest alone. Native upstream PR 701 updates
pyeasee/pysignalr for connection-cookie fixes. This addresses the known
transport failure; it does not prove that every physical charge stop is fixed.
Verify sustained physical charging under normal load before any stress test.

### Delayed observations and bounded recovery (b17)

Easee current and operating-mode observations are event states. Their source
timestamp can legitimately remain unchanged while the value is steady. A new
HTTP response confirms a cloud read, not a new physical sample. B17 preserves
both clocks and prefers independent newer native events, including a manual
pause or schedule. Observation metadata alone is never proof of physical
command response.

The **Conservative reduction with delayed EV data (verified import-only
supply)** option is off by default. Enable it only after verifying that the
grid measurement includes the controlled L1, and that the installation uses
comparable importing loads without generation, storage or cancelling current.
The raw grid magnitude is not a general upper bound on household RMS current
when these assumptions do not hold.

When that model is explicitly enabled and an accepted positive offer is
already charging, an uncertain EV report can cause a conservative reduction
instead of an immediate zero. The entire new offer must fit the normal
raw-grid budget without subtracting EV current, and may never exceed the
accepted cap or the uncertainty episode's previous cap. For example, a 25 A
fuse with 2 A buffer and 14 A raw grid current allows at most 9 A on this path.
If the minimum current cannot fit, the path still stops. Physical faults,
stale grid measurements and existing overload-response limits still apply.

Recovery does not repeatedly offer the car maximum after a telemetry stop.
It uses bounded attempts and a conservative startup offer. A measurement
recovery block requires attention or a new physical session. Recovery caps,
failure attempts and backoff are checkpointed with the operational ledger;
an HA restart cannot grant fresh attempts. The separate persistent overload
budget and failure latch are never cleared by this path.

Source Health exposes recent command and stop receipts. On each new stop,
bounded evidence is saved by the integration to its local
`1fuseloader.<entry-id>.incident_journal` Store. This is private diagnostic
data, not a control input. It distinguishes proposed limits, API acceptance,
cached readback and reported physical current. Offline process tests cover
transport ordering separately from physical EV response; actual charging must
still be verified under supervision after activation.

### Bounded lower-current trial (b18)

From b21, a lower-current report received before the original response deadline
can enter a separate, nonrenewable synchronization phase while waiting for the
next meter report. This requires a live accepted lower offer, a trusted
post-dispatch EV event at that offer and a post-command grid report below the
full normal buffered ceiling. The phase lasts at most five seconds from the
first qualifying response, also bounded by the existing alignment window.
It sends no command or lease renewal and permits no increase or resume.
Raw grid above the normal buffered ceiling, excessive EV current, invalid source
or control metadata and an expired lease terminate the phase. Unchanged events
and small measurement jitter do not move the response floor. A meaningful EV
value change requires a grid event after that change without granting more time.
Only a verified configured source binding can enter this phase.

This is explicitly bounded sampling tolerance, not a proof of simultaneous
physical measurements. A grid report of 18.6 A followed by a 7 A EV report does
not meet the stronger whole-offer raw-grid bound (18.6 + 7 = 25.6 A). The short
synchronization policy depends on the reported reduction continuing and prompt
fresh meter feedback; it cannot guarantee that no intervening household load
change occurred. The original high-load response deadline, experimental budget
and persisted failures are not renewed. Reloading an interrupted phase retains
the existing failed-response recovery behavior.

The separate **Bounded lower-current trial after overload lockout** option is
off by default. It requires the experimental profile to be enabled. When an
old response timeout or spent experimental budget would block continued
overload, a healthy charging session with a live accepted higher cap may
instead try a strictly lower startup-current cap. This is a short response
trial, not another sustained-overload allowance.

The first qualifying grid sample anchors one fixed deadline, at most ten
seconds and at most the configured response window. Its age, checkpoint
verification and command dispatch consume that same window. API acceptance,
cached readback, GET receipts, new load samples and setting changes cannot
renew it. A newer post-dispatch EV observation at the lower current and a
still later grid observation must establish buffered normal operation. The
separate verified-import fallback can instead prove a conservative raw-grid
envelope; it remains its own explicit opt-in.

Missing response, excess peak current, unhealthy sources or conflicting new
EV observations request zero. Recovery remains capped at startup current,
backs off, and stops automatic attempts after two retries. An interrupted
persisted response is failed on restore, rather than restarted with a fresh
window. Neither the prior experimental failure nor consumed time is erased.

This deadline bounds local decisions. Easee rounds positive TTL to at least
one minute and the cloud command is best effort; physical reduction or stop
within ten seconds is not guaranteed. Do not use a blown fuse to calibrate a
cooling model. Automatic cycle re-arm uses the separately documented raw-load
observation rule; it is not a verified thermal-budget reset.

Same base formula as the old `sensor.easee_max_available_l1_v2`:

```text
house_load_l1 = max(0, grid_l1 - charger_current)
available_l1 = main_fuse - house_load_l1 - buffer
if available_l1 < min_charge_amps: available_l1 = 0
else floor and cap at max_charge_amps
```

The original guard thresholds are included:

- Force L1 dynamic circuit limit
- Soft guard: high L1 for short duration → set low current
- Pause guard: high L1 for longer duration → pause charging
- Resume guard: low L1 for stable duration → resume charging

Normal current follows the buffered headroom. An already charging car can keep
the minimum 6 A by using some of the normal buffer, provided the estimated
house load plus 6 A still fits below the main fuse rating with a separate
minimum reserve (default 0.5 A). This replaces the old unconditional minimum
fallback; with experimental timed overload disabled, it does not intentionally
permit an overload. L1 is the only supported
controlled phase, and L2/L3 limits stay at 0 A.

For the default 25 A fuse and 2 A normal buffer:

| Estimated house load on L1 | EV target after a load increase | Estimated total |
| --- | --- | --- |
| 16 A | 7 A | 23 A |
| 18 A | 6 A, using part of the buffer | 24 A |
| 18.5 A | 6 A, retaining 0.5 A reserve | 24.5 A |
| 20 A | 0 A; 6 A would exceed the budget | 20 A |

Decreases run as soon as a source state change is received, with a coalesced
latest-snapshot worker and a 5-second watchdog. **Fast Headroom Recovery** is
on by default: during actual charging it can raise directly to buffered
available current, capped by the configured car maximum. For example, 10 A EV
draw within 14 A total L1 means an estimated 4 A house load; a 16 A offer would
project 20 A total and fits a 25 A fuse with a 2 A buffer. The direct rise still
requires fresh, mutually consistent grid/EV report times and no active response
mismatch or unobserved reduction. When unchanged event-driven values are too
old for an increase, a bounded read-only GET obtains a coherent current/status
pair from the selected charger. From **0.4.0b16**, an actively controlled,
actually charging session schedules a read before its effective current
receipt reaches the alignment/age deadline. New native reports postpone that
read. This closes the gap between the previous 30-second request cooldown and
a shorter alignment window without relaxing the current checks.

Reads remain outside the control lock and single-flight. Their adaptive
interval has a five-second minimum; failed requests use increasing backoff.
Very short alignment windows, slow requests and cloud outages can still fail
the original freshness checks and stop charging. The GET returns the cloud's
last-reported observations: its receipt is not a new physical sample time or
proof that the charger responded. Original HA report times remain visible.
New relevant HA events, reconfiguration, health loss and cache expiry discard
the pair. A heartbeat alone never refreshes a current measurement.

The observation endpoint documents a limit of 100 requests per five minutes;
the integration's own five-second floor leaves room below that limit, but
other clients share API capacity and rate-limited requests still require
backoff. See [Easee Get Observations](https://developer.easee.com/reference/getobservations).

The read uses `max(inCurrentT2, inCurrentT3, inCurrentT4, inCurrentT5)`,
matching the supported Easee integration's Current sensor. All four measured
values must be present, numeric and valid; `outputCurrent` is offered pilot
current and is never used as a measured-current fallback. The snapshot error
reason diagnostic contains only fixed codes, without raw API responses.
The GET uses the supported integration's `get_observations` API for operation
mode and the four terminal currents. Every required ID must occur exactly once
with its expected declared data type. Underlying read failures expose only a
fixed allowlist of exception classes, never response bodies or private text.

Turn Fast Headroom Recovery off for the previous gradual mode: increases wait
for 30 seconds of stable headroom and then rise at most 1 A per 15 seconds.
Physical/report response can make gradual recovery slower than this minimum
interval. Recovery after a
zero cap requires 60 seconds of stable buffered headroom. Starting a waiting
vehicle uses **Startup Charge** (default 7 A) as the minimum buffered headroom
required to start, bounded by the configured minimum and maximum. After the
stable observation period, the offer can use the safe available headroom up
to the configured maximum even while the vehicle is waiting. Continuing a
session can still use 6 A. The full normal buffer must fit a startup offer;
idle or unverified old EV readings are not subtracted to manufacture spare
capacity. There is no short 7-to-6 A pulse. `starting`, `waiting_for_vehicle`,
`start_headroom_wait` and `waiting_for_vehicle_stalled` distinguish the observed
waiting conditions; the stalled state is diagnostic and does not trigger
repeated stop/start commands.

With the import-only raw fallback enabled, a connected vehicle that draws no
power can also complete normal measurement recovery. For the existing
30-second recovery period, fresh advancing grid
reports must leave room for the **entire** previous, accepted and prospective
normal offer, without subtracting a cached vehicle current. A full vehicle
does not have to start charging to release an obsolete recovery ceiling.
Restart and retry guards still apply, and the next higher limit requires its
ordinary durable submission and actual acknowledgement. This recovery does
not reset overload exposure, automatic-cycle budget or historical faults.
When whole-raw headroom independently qualifies a waiting offer, the ordinary
calculation can increase it and renew its finite lease before expiry. A
no-write continuity hold cannot suppress those commands indefinitely; active
response observations keep their existing deadlines.

Takeover of an already charging session can use the verified current and
buffered headroom; an old EV report without current evidence receives no
subtraction credit for a new increase. A submitted reduction must be observed
before a rise is allowed; renewing its TTL does not reset ramp timers.

When observed EV reduction, including a partial response that has not yet reached
the requested cap, arrives before a new grid report, the regulator can
briefly retain the already accepted lower cap while waiting for alignment
(default 10 seconds, configurable). Any new grid report is evaluated immediately;
invalid/stale telemetry still requests zero. This is a report-alignment window,
not permission to overload a fuse. Verify real meter cadence and vehicle response
before takeover. Positive starts/increases also retain the preceding coherent
house estimate while current reports catch up. Their alignment window starts
at the first qualifying report mismatch. Coherent partial response pauses the
cumulative mismatch budget; a later increase uses only the remaining time.
Identical values and TTL submissions cannot replenish that budget. Alignment
cannot permit another increase or a resume. Invalid data or an exceeded nominal
budget still requests zero. After the window expires, one strictly conservative
hold is possible: **raw grid current plus the entire accepted cap** must fit the
full normal buffer, without subtracting any EV reading. This hold retains only
that accepted cap and its spent budget; it permits no increase or resume. New
coherent feedback releases the hold. If this conservative budget does not fit,
timeout still requests zero.
An obsolete positive house anchor may be replaced after a verified charger
snapshot and a subsequent coherent grid report. This preserves the accepted
cap and delayed-start context, including any spent alignment time. Receipt
renewals alone never release an anchor. `house_load_basis` and
`house_load_anchor_l1` distinguish the control estimate from raw readings;
`current_refresh_required` and the Source Health snapshot attributes expose
why a newer read is needed and which current/status pair was used.
Experimental timed overload is a separate, disabled-by-default
option described below; the report-alignment window alone does not enable it.

During that positive-response wait, a newer grid report and a delayed low EV
report can make the accepted offer exceed the buffered estimate. If the car
is actually charging and source receipts are fresh, the regulator can first
lower the cap using **raw grid current plus the entire new cap** within the
full normal buffer. It never subtracts the uncertain EV reading for this
fallback. With a 25 A fuse, 2 A buffer and 15.5 A raw grid current, at most 7 A
fits this conservative check. If even the configured minimum cannot fit, it
still requests zero. This path permits only a reduction, never a start,
increase or resume, and an accepted reduction carries only the original
alignment time that remains. It cannot bypass faults, stale data, excessive EV
current, alignment timeout or overload/soft/pause protection.

The opposite report order can briefly pair a lower grid current with the EV
current from before an accepted reduction. The established unchanged-EV path
requires **grid current plus the entire accepted cap** to fit the nominal
budget and minimum reserve. A later EV receipt can also contain the preceding
high value: this narrower extension requires a known preceding accepted offer,
current within that offer's tolerance, and the **full normal buffer** for raw
grid current plus the entire lower cap. It never subtracts the inconsistent EV
reading to create headroom or grants a higher cap or resume.

An unchanged value well below the new cap cannot prove that a startup reduction
has happened. A new near-cap report, or a distinct fall from an observed high
current, needs a subsequent coherent grid report to confirm the response.
Repeated identical receipts do not move that first response time. Confirmation
releases the active wait but retains the accepted lower-cap context and its
remaining alignment budget for delayed high reports. An actually accepted
higher offer replaces that context with positive-response tracking; a displayed
proposal or failed submission does not. Report-order changes, coherent partial
feedback, lease renewals and further reductions in the same response chain
cannot refund its spent alignment budget. Once the earlier response is
confirmed and its guard has ended, a later reduction uses the latest accepted
cap as its own preceding offer; it cannot reuse the older, higher offer.
A timeout, missing or stale timestamps, an implausible EV value or an exceeded
budget requests zero. Source faults and the overload/soft/pause protections
retain priority. A completed/disconnected session does not receive a zero
command merely because its final current reports arrive out of order.

These checks do not synchronize physical measurements. Numerically consistent
grid/EV values can still refer to different moments. While a submitted reduction
is unobserved, the normal estimator retains at least the preceding house-load
estimate and inhibits increases. A coincident house-load increase and EV-current
decrease can nevertheless escape that estimate briefly. This remains best-effort
cloud control, not a guarantee against transient overload; verify report cadence
and response in dry run and a supervised charging test before takeover.

The legacy `pause_limit_amps` / `pause_for_seconds` settings are an additional
sustained-overload Pause fallback, for example when a submitted limit does not
produce the expected physical response. They do **not** grant an overload grace
period: the normal headroom calculation still requests zero immediately when
even minimum charging does not fit. Increasing the default 120-second pause
delay therefore does not implement timed minimum-current overload operation.

### Experimental timed overload

Version **0.4.0b4** adds an explicit **Experimental Timed Overload** opt-in.
It is off for both new and existing entries. Before enabling it, verify the
actual fuse, installation limits, current-report cadence and charger fallback.
The operational limits below are not a fuse time/current curve or certification
that a particular installation may run overloaded. Leave physical protection
in place.

The default/manual experimental profile with `peak_only_response=False` and
a configured 25 A main fuse is shown below. Its legacy 6 A example is separate
from the selected 7 A peak-triggered/automatic profile described above.

| Setting | Default | Behavior |
| --- | --- | --- |
| Sustained overload margin | 4 A | Continue at the configured minimum EV current only if projected L1 fits at most 29 A |
| Cumulative overload budget | 1800 s | Count all observed time above 25 A, including the response window; brief dips do not replenish it |
| Transition peak limit | 38 A | Bound the short response phase while requesting minimum EV current |
| Response window | 10 s | Wait for a fresh grid report and observed EV reduction, then require total L1 at most 29 A |

For example, house load of 22 A plus EV current of 16 A produces a 38 A
report. The regulator may request 6 A and briefly wait for fresh feedback;
the resulting 28 A total can then use the remaining overload budget. Repeated
reports or TTL renewals do not extend the original response deadline. A peak
above the transition limit, an infeasible projected minimum current, a response
timeout, untrustworthy telemetry or exhausted budget requests zero rather than
starting another grace window. It never increases EV current or starts an
idle car merely to use the experimental overload allowance.

A high grid report from an intermediate EV response cannot prove that the final
minimum-current response exceeded the ceiling. If the EV reaches its minimum
after that grid report, the controller waits for matching grid feedback within
the same original response deadline. A grid value already at or below the
ceiling is conservative while the EV is still reducing; a subsequent grid report
above the ceiling after the minimum response still stops immediately.

From **0.4.0b14**, reducing a higher accepted offer to the experimental minimum
uses the same bounded response transition even if the latest EV reading is
already low. That reading may precede the higher offer. Completion requires an
accepted minimum offer, a later minimum-current EV report and then a coherent
grid report strictly after that response. Mixed feedback cannot hand an
unconfirmed reduction to ordinary headroom logic. The original response deadline
still includes API latency; missing confirmation still requests zero and latches
the failure, even if other measurements suggest that the vehicle reduced its
draw. Updating or restarting does not clear an existing safety latch.

Exposure is cumulative rather than a thermal cooling estimate. The controller
persists its budget and unresolved exposure across unload/restart and setting
changes; a short dip below the fuse rating, toggling the switch or restarting HA
does not grant another 30 minutes. Under the default/manual policy, B23's
explicit-period start records a new
baseline without resetting the lifetime counter; it is not a budget-reset
button. Automatic cycles have a separate, explicit qualification/re-arm policy
described above; they also retain lifetime exposure and historical faults.
Unknown elapsed exposure is handled conservatively. Diagnostic sensors
show used/remaining time, the response window and any latched failure.

A blocked timed-overload profile does not by itself forbid a proven normal
reduction within the full configured fuse buffer. This exception is limited
to an already accepted positive offer, fresh valid current/grid evidence,
and a bounded, nonrenewable response context. It cannot increase an offer,
resume a vehicle, refund exposure or clear the saved overload failure. A
load that genuinely needs the blocked overload allowance still stops.

An enabled switch does not mean this allowance is usable. A saved failure or
exhausted cumulative budget remains blocked after an upgrade. Version b16
exposes the saved failure separately and records why an otherwise possible
normal reduction was rejected, including input ages and receipt skew. This
does not clear the latch, refund time, grant a new transition, or change any
fuse/current limit.

During a known accepted startup, a newer grid report can precede the EV
report that explains it. A narrowly bounded normal alignment hold may keep
the existing accepted offer using its prior buffered household anchor only
while raw grid remains within the normal envelope. A later low EV report,
new excessive load, source failure or the original alignment deadline ends
that hold. Repeated reports and lease renewals do not restart its clock.

Checkpoint writes are atomic, serialized and independently read back with a
unique revision. Unchanged exposure state does not cause another disk write;
running exposure and changed safety flags still require new checkpoints.
An unresolved save failure blocks timed control and requests zero current.
After a verified checkpoint of the latest conservative state, a later fresh
evaluation may recover from that save failure without refunding exposure or
resetting guard timers. Failed restores and overload safety latches remain
blocked. Storage error type and verification time are available in diagnostics.

The initial ledger read has a separate 10-second deadline to accommodate busy
startup I/O; runtime checkpoint writes retain their 2-second deadline. A slow
initial read delays controller setup and its first command. A read that still
times out or fails validation remains blocked, and is never replaced by a new
empty allowance. This does not add automatic retries or change how saved
exposure and interrupted transitions are restored.

Easee accepts dynamic current leases in whole minutes. The controller therefore
ends an overload allowance early when less than 90 seconds remain for a further
positive lease and its response reserve. The 1800-second value is a maximum
software allowance, not a promise of exactly 30 minutes of charging, an exact
physical stop deadline or a guarantee that a cloud command will reach the car.
The ordinary soft/pause thresholds do not override a valid experimental hold;
the experimental limits and failure handling still apply.

Before active takeover, verify that the charger's offline fallback cannot
increase EV current after communication loss. A retained 10 A offline limit
can defeat a 6 A request when a dynamic lease expires. Do not enable this
experimental profile until fallback behavior and installation limits have
been checked. Settings are capped at a 4 A margin, 1800 s budget, 38 A peak
and 10 s response window; lowering them does not erase prior exposure.

Missing, invalid, restored or stale grid data resets the guard timers and
requests 0 A when control is enabled. For a verified Currently One MQTT device,
the controller validates L1 directly from received device frames and requires
an advancing source timestamp. Repeated identical L1 values can therefore renew
freshness and provide report alignment even when HA suppresses unchanged sensor
state writes. Retained messages, invalid L1 values, replayed/stale timestamps and
connection loss cannot renew this source. This adapter only supports the verified
vendor identity and protocol; it does not infer L1 freshness from another phase,
power sensor or broker connection alone. Other grid sources continue to use
`State.last_reported`.
Easee status/current are event driven, so their unchanged state timestamps are
not used as heartbeats. The adapter checks the charger's online state, SignalR
connection and latest local ProductUpdate receipt (`latestPulse`). This receipt
is neither a transport ping nor the physical acquisition time of each value.
The default age limit is 120 seconds for configurable grid age and the adapter's
fixed product-update age. Missing recent product data conservatively requests
0 A and blocks positive commands; it does not prove that the websocket itself
disconnected. A read-only GET also returns last-reported observations, so its
local receipt alone cannot repair that freshness gap. Observe the actual
cadence while charging before takeover.

All positive offers, including reductions and identical renewals, are checked
against the latest input revision again after exposure-checkpoint I/O. Source
changes during a successful API submission schedule another coalesced
evaluation; they do not turn HTTP acceptance into proof of physical response.
Source-health diagnostics retain `last_stop_context`: the first zero/pause
attempt in an incident, with its original reason, input clocks,
budget/latch and command result. Later inconsistent feedback or repeated zero
requests do not overwrite it. A new accepted positive offer arms the next
incident. This context is memory-only and does not authorize control.

A pending zero offer is not physical confirmation that current reached zero.
It also must not indefinitely prevent a waiting vehicle from restarting when
the entire new offer fits the fresh raw-grid budget through the normal stable
observation period. This restart subtracts no EV current and does not loosen
the zero-response tolerance. A genuinely accepted positive restart replaces
the pending context and starts new raw-house response tracking, even when its
numeric cap happens to match an older offer whose zero request failed.
An already qualified raw-budget observation can retain its history through
an aged EV receipt while fresh grid and product health remain valid. It
requests only zero until fresh current/status data again confirms that the
vehicle may start. This does not refresh the old receipt; source faults,
fresh contradictory feedback and manual/scheduled waits still stop recovery.

The integration only resumes a pause that it requested during the current
runtime. It does not infer ownership from a charger's paused status after an
HA restart. User settings and experimental overload exposure persist; pause
ownership and ordinary ramp/guard observation timers do not.

## BMW X5 xDrive45e and CarData

BMW's original August 2019 technical data specifies 3.7 kW AC at 230 V / 16 A,
24 kWh gross battery energy and about 6.8 hours for 0–100% at maximum AC power.
The project's default 6–16 A single-phase range matches that vehicle's maximum
and Easee's normal minimum current allocation. A 6 A limit offers about 1.38 kW
at 230 V; the measured draw may be lower if the vehicle requests less. Stability
at exactly 6 A and stop/restart behavior remain supervised vehicle tests.

An optional CarData device adds advisory BMW charging state, genuine reported
SoC, AC current and phase-count sensors with source timestamps/freshness.
Plug state, voltage, end reason and last result are related diagnostic context.
Entity matching uses the selected device and descriptor keys, so renaming an
entity does not break it. Predicted SoC and learned battery capacity are not
used as genuine vehicle measurements. A phase count retained after disconnect
is explicitly marked as unrelated to an active charging session.

The installed CarData interface is read only. Its cloud values have variable
cadence, so they never replace the meter/Easee feedback or decide a protective
current increase, pause or resume. This integration makes no new BMW API polls.

Sources: [BMW 2019 technical data](https://www.press.bmwgroup.com/global/article/attachment/T0300315EN/440691),
[Easee current control](https://developer.easee.com/docs/current-limits-and-control),
[BMW CarData documentation](https://bmw-cardata.bmwgroup.com/customer/public/api-documentation).

## Easee compatibility

The service wrapper in Easee 0.9.74 skips an identical circuit limit, even when
its TTL needs renewal. It also logs some API failures without raising them.
Calling those services repeatedly is therefore insufficient for this regulator.

The version-gated adapter uses the existing Easee integration's authenticated
charger/circuit objects and their public pyeasee methods. It sends identical
limit renewals, converts internal TTL seconds to rounded-up whole **minutes**,
and requires a successful HTTP response before recording a submitted command.
It does not create, store or read a separate API token, or modify Easee files.
An unknown version or missing expected object blocks control with a diagnostic
reason. Dry Run remains available for observation.

`Command Result` describes the latest evaluation and may be `not_sent` while no
write is needed. **Last Command Attempt Result** retains the last actual
attempt, accepted-command and accepted-limit receipts for the current runtime,
including their UTC times and sanitized error class. **Reported Circuit P1**
reads explicit phase attributes from the selected Easee device; Easee's generic
Dynamic Circuit Limit is the maximum across phases, not necessarily P1.
**Source Health** exposes required-source validity and HA receipt times. Compute
ages from these timestamps; a stable event-driven value can be older than the
healthy stream pulse. BMW source time is separate and advisory.

API acceptance, matching cached limit readback and measured EV current are
different evidence. A matching report does not prove physical current response
or device TTL renewal. Failed submissions remain eligible for retry; decisions
and writes are serialized, and disabling control cancels queued decisions.

This remains a cloud dependent soft regulator. If HA, the network or Easee is
unreachable, a 0 A request may not reach the charger. A finite dynamic limit can
expire and restore the charger's configured default. Keep the physical fuse
protection and verify the charger's fallback configuration before takeover.

## First safe test

1. Back up affected files and record the enabled state of the old charging automations.
2. Install the integration folder in `/config/custom_components/1fuseloader` or install it through HACS as a custom repository, then restart Home Assistant with the operator's approval.
3. Add integration: **1FuseLoader**.
4. Leave **Dry Run = ON** and **Active Control = OFF**.
5. Keep old Easee automations ON only during dry-run comparison.
6. Observe an actual charging session: compare grid L1, charger current, estimated house load, target current and source health. Exercise missing data, overload, pause/resume and API failure behavior in the test environment first.
7. After review, disable only the replaced load balancing/guard automations, then set **Active Control = ON**, **Dry Run = OFF**. Verify the physical/current feedback and TTL renewals during supervised charging.

The first six steps keep **Experimental Timed Overload = OFF**. Normal live
control and experimental overload are separate opt-ins; enabling Active Control
does not enable overload operation.

Do not run the old controller and the new controller with writes enabled at
the same time. Keep unrelated alerts and charge schedules as appropriate.
Rollback starts by disabling **Active Control**, then restoring the old
automation enabled states. Check the current dynamic limits and TTL before
leaving the charger unattended; disabling an integration does not itself clear
limits already accepted by Easee. Change settings through HA's UI, never by
manually editing `.storage`.

## HACS prerelease installation

Add `https://github.com/haxxel1337/1FuseLoader` as a custom **Integration**
repository in HACS. For a **0.4.0 prerelease**, open the
repository's download/redownload dialog, enable beta/prerelease versions and
select that exact version. Confirm HACS reports the installed version, then
restart Home Assistant with the operator's approval. Existing UI configuration
is retained. The integration is classified as a **service**, so it appears in
the normal Integrations list instead of Helpers. Downloaded files and loaded
runtime can have different versions until restart. GitHub's stable **Latest**
badge can still refer to v0.3.3 while a newer beta is available as a prerelease. Existing
overload opt-in is preserved; this update never enables it automatically.

The startup default addresses a possible Easee 6 A waiting condition; it is not
proof of the cause of every vehicle stop. Easee recommends 7 A in certain phase
allocation cases. A short phase-switch 7-to-6 A workaround elsewhere caused
regressions and was reverted; this integration does not implement that pulse.
Verify actual current, source alignment and vehicle warnings in supervised
charging before treating this prerelease as physically validated.

References: [Easee load balancing](https://developer.easee.com/docs/load-balancing),
[reported short-pulse regression](https://github.com/evcc-io/evcc/issues/33963),
[revert](https://github.com/evcc-io/evcc/pull/33964).

For an upgrade from a manually copied pilot, back up the component folder first
and let HACS replace the managed files. Confirm the loaded integration version
after the restart and inspect diagnostics before enabling writes. HACS owning
the files does not itself transfer control from the old automations.

## Local test

```powershell
Get-ChildItem test_1fuseloader_*.py | ForEach-Object { python $_.FullName; if ($LASTEXITCODE) { throw "Tests failed" } }
python -m compileall -q .\custom_components\1fuseloader
```

The standalone suites use simulated inputs and adapters. CI additionally
installs Home Assistant 2026.10.1, 2026.10.2, 2026.10.3 and 2026.10.4 on Python 3.14 with
both supported pyeasee versions to test actual imports, selectors, entity
construction and config/options flows. Passing these tests does not establish
physical charging acceptance.
