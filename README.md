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

### Command delivery and low-grid alignment (b19)

From b20, event age or skew alone cannot open a new low-grid alignment
episode. An old, unchanged EV event follows the existing freshness policy:
it provides no new credit for an increase, but does not manufacture a new
ten-second response obligation. A genuine numeric conflict still uses the
bounded alignment path and its original deadline, including after reentry.

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
one fixed window of at most ten seconds. The whole 7 A offer must fit below
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
renew it. A newer post-acceptance EV observation at the lower current and a
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
cooling model. This version introduces no automatic thermal-budget reset.

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

The default experimental profile for a configured 25 A main fuse is:

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
does not grant another 30 minutes. There is no user budget-reset button in this
release. Unknown elapsed exposure is handled conservatively. Diagnostic sensors
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
installs Home Assistant 2026.10.1 and 2026.10.2 on Python 3.14 to test actual imports,
selectors, entity construction and config/options flows. Passing these tests
does not establish physical charging acceptance.
