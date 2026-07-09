# 1FuseLoader

Home Assistant custom integration for Axel's Easee/BMW single-phase fuse guard.

This is a **soft load balancer** for an Easee charger where a BMW is effectively charged on one phase. It monitors grid current, estimates house load, and then dynamically lowers/raises Easee's circuit limit. It can also soft-limit, pause, and resume charging when L1 gets too high.

> Safety note: this is not a certified hardware fuse guard. Keep proper electrical protection in place.

## Target

- Home Assistant Core: **2026.7+**
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

Old guard behavior is included:

- Force L1 dynamic circuit limit
- Soft guard: high L1 for short duration → set low current
- Pause guard: high L1 for longer duration → pause charging
- Resume guard: low L1 for stable duration → resume charging

## First safe test

1. Install the integration folder in `/config/custom_components/1fuseloader` or install it through HACS as a custom repository.
2. Restart Home Assistant.
3. Add integration: **1FuseLoader**.
4. Leave **Dry Run = ON** and **Active Control = OFF**.
5. Keep old Easee automations ON only during dry-run comparison.
6. When sensors look correct: disable old automations, set **Active Control = ON**, **Dry Run = OFF**.

## Local test

```powershell
python .\test_1fuseloader_logic.py
python -m compileall .\custom_components\1fuseloader .\test_1fuseloader_logic.py
```
