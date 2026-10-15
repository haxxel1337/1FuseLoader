# 1FuseLoader

Home Assistant custom integration for Axel's Easee/BMW single-phase fuse guard.

This is a **soft load balancer** for an Easee charger where a BMW is effectively charged on one phase. It monitors grid current, estimates house load, and then dynamically lowers/raises Easee's circuit limit. It can also soft-limit, pause, and resume charging when L1 gets too high.

> Safety note: this is not a certified hardware fuse guard. Keep proper electrical protection in place.

## Target

- Home Assistant Core: **2026.10.1** (minimum and CI target)
- Compatibility adapter: **Easee 0.9.74 / pyeasee 0.8.17**; unsupported versions block writes
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

Current never exceeds the calculated headroom. In particular, the old forced
6 A fallback is removed: if less than the minimum charging current is available,
the target is 0 A. The SOFT guard can only lower the target. L1 is the only
supported controlled phase, and L2/L3 limits stay at 0 A.

Missing, invalid, restored or stale grid data resets the guard timers and
requests 0 A when control is enabled. Grid freshness uses `State.last_reported`.
Easee status/current are event driven, so their unchanged state timestamps are
not used as heartbeats; the adapter checks the charger's online state, SignalR
connection and latest received stream pulse instead. The default age limit is
120 seconds for the configurable grid age and the adapter's fixed stream-pulse
age. An idle, disconnected charger can legitimately be quiet for longer; this
conservatively inhibits increases until new telemetry arrives. Observe the
actual cadence while charging before takeover.

The integration only resumes a pause that it requested during the current
runtime. It does not infer ownership from a charger's paused status after an
HA restart. User settings persist, but pause ownership and timers do not.

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

`Command Result` and `Last Command` distinguish a proposed action from an API
accepted submission. API acceptance is not confirmation of physical charger
current. Failed submissions remain eligible for retry; decisions and writes
are serialized, and disabling control cancels queued decisions.

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

Do not run the old controller and the new controller with writes enabled at
the same time. Keep unrelated alerts and charge schedules as appropriate.
Rollback starts by disabling **Active Control**, then restoring the old
automation enabled states. Check the current dynamic limits and TTL before
leaving the charger unattended; disabling an integration does not itself clear
limits already accepted by Easee. Change settings through HA's UI, never by
manually editing `.storage`.

## Local test

```powershell
Get-ChildItem test_1fuseloader_*.py | ForEach-Object { python $_.FullName; if ($LASTEXITCODE) { throw "Tests failed" } }
python -m compileall -q .\custom_components\1fuseloader
```

The standalone suites use simulated inputs and adapters. CI additionally
installs Home Assistant 2026.10.1 on Python 3.14 to test actual imports,
selectors, entity construction and config/options flows. Passing these tests
does not establish physical charging acceptance.
