from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_BMW_DEVICE_ID, DOMAIN, NAME


SENSOR_DESCRIPTIONS = [
    SensorEntityDescription(
        key="overload_trial_state", name="Overload Trial State", icon="mdi:timer-outline",
    ),
    SensorEntityDescription(
        key="overload_trial_used_seconds", name="Overload Trial Used Seconds",
        native_unit_of_measurement="s", icon="mdi:timer-sand",
    ),
    SensorEntityDescription(
        key="overload_trial_remaining_seconds", name="Overload Trial Remaining Seconds",
        native_unit_of_measurement="s", icon="mdi:timer-sand-complete",
    ),
    SensorEntityDescription(
        key="mode",
        name="Mode",
        icon="mdi:ev-station",
    ),
    SensorEntityDescription(
        key="target_p1",
        name="Target P1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
    ),
    SensorEntityDescription(
        key="target_p2",
        name="Target P2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
    ),
    SensorEntityDescription(
        key="target_p3",
        name="Target P3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
    ),
    SensorEntityDescription(
        key="available_l1",
        name="Available L1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
    ),
    SensorEntityDescription(
        key="house_load_l1",
        name="House Load L1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-lightning-bolt",
    ),
    SensorEntityDescription(
        key="reason",
        name="Reason",
        icon="mdi:text-box-search-outline",
    ),
    SensorEntityDescription(
        key="last_command",
        name="Last Command",
        icon="mdi:console",
    ),
    SensorEntityDescription(
        key="service_result",
        name="Command Result",
        icon="mdi:list-status",
    ),
    SensorEntityDescription(
        key="last_attempt_result",
        name="Last Command Attempt Result",
        icon="mdi:history",
    ),
    SensorEntityDescription(
        key="reported_circuit_p1",
        name="Reported Circuit P1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
    ),
    SensorEntityDescription(
        key="source_health",
        name="Source Health",
        icon="mdi:heart-pulse",
    ),
]

BMW_SENSOR_DESCRIPTIONS = [
    SensorEntityDescription(key="bmw_charging_state", name="BMW Charging State", icon="mdi:ev-station"),
    SensorEntityDescription(key="bmw_state_of_charge", name="BMW State of Charge", native_unit_of_measurement=PERCENTAGE, device_class=SensorDeviceClass.BATTERY),
    SensorEntityDescription(key="bmw_ac_current", name="BMW Reported AC Current", native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, device_class=SensorDeviceClass.CURRENT),
    SensorEntityDescription(key="bmw_phase_count", name="BMW Reported Charging Phases", icon="mdi:sine-wave"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    descriptions = list(SENSOR_DESCRIPTIONS)
    if entry.data.get(CONF_BMW_DEVICE_ID):
        descriptions.extend(BMW_SENSOR_DESCRIPTIONS)
    async_add_entities([OneFuseSensor(controller, entry, desc) for desc in descriptions])


class OneFuseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller, entry: ConfigEntry, description: SensorEntityDescription) -> None:
        self.controller = controller
        self.entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
            manufacturer="Axel / ChatGPT",
            model=NAME,
        )

    async def async_added_to_hass(self) -> None:
        self.controller.add_listener(self._handle_controller_update)

    async def async_will_remove_from_hass(self) -> None:
        self.controller.remove_listener(self._handle_controller_update)

    @callback
    def _handle_controller_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        key = self.entity_description.key
        if key.startswith("overload_trial_"):
            value = self.controller.regulator.trial_diagnostics().get(key)
            return "not_started" if key == "overload_trial_state" and value is None else value
        if key.startswith("bmw_"):
            field = self.controller.bmw_context.get(key.removeprefix("bmw_"), {})
            return field.get("value") if field.get("available") else None
        if key == "last_attempt_result":
            return (self.controller.last_command_attempt or {}).get("result", "none")
        if key in {"reported_circuit_p1", "source_health"}:
            return self.controller.source_diagnostics.get(key)
        d = self.controller.last_decision
        if d is None:
            return None
        if key == "mode":
            return d.mode
        if key == "target_p1":
            return d.current_p1
        if key == "target_p2":
            return d.current_p2
        if key == "target_p3":
            return d.current_p3
        if key == "available_l1":
            return d.available_l1
        if key == "house_load_l1":
            return round(d.house_load_l1, 2)
        if key == "reason":
            # HA states are limited to 255 characters. Preserve the complete
            # explanation in the existing `reason` state attribute below.
            return d.reason if len(d.reason) <= 255 else d.reason[:252] + "..."
        if key == "last_command":
            return self.controller.regulator.last_command or "none"
        if key == "service_result":
            return self.controller.last_service_result
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key.startswith("overload_trial_"):
            return self.controller.compact_state_attributes({
                **self.controller.regulator.trial_diagnostics(),
                "scope": "explicit_software_period_not_fuse_thermal_model",
            })
        if self.entity_description.key.startswith("bmw_"):
            field = self.controller.bmw_context.get(self.entity_description.key.removeprefix("bmw_"), {})
            # The source timestamp establishes age without recording a changing
            # age_seconds attribute on every 5-second watchdog tick.
            attributes = {**{key: value for key, value in field.items() if key not in {"value", "age_seconds"}}, "role": "advisory_only"}
            if self.entity_description.key == "bmw_charging_state":
                attributes["related_context"] = {
                    key: {name: value for name, value in self.controller.bmw_context.get(key, {}).items() if name != "age_seconds"}
                    for key in ("plug_state", "ac_voltage", "end_reason", "last_result")
                }
            return self.controller.compact_state_attributes(attributes)
        d = self.controller.last_decision
        if d is None:
            return None
        diagnostics = {}
        key = self.entity_description.key
        if key in {"source_health", "reported_circuit_p1", "last_attempt_result", "service_result"}:
            snapshot = self.controller.diagnostic_attributes()
            if key == "source_health":
                diagnostics = snapshot
            elif key == "reported_circuit_p1":
                diagnostics = {
                    name: value for name, value in snapshot.items()
                    if (name.startswith("reported_circuit_") and not name.endswith("_age_seconds"))
                    or name in {"last_accepted_limit", "reported_cap_matches_last_accepted_limit",
                                "reported_cap_report_after_acceptance", "readback_scope",
                                "physical_current_verified", "device_ttl_verified"}
                }
            else:
                diagnostics = {name: snapshot[name] for name in (
                    "last_command_attempt", "last_accepted_command", "last_accepted_limit",
                )}
        meter_sources = {name: self.controller.source_diagnostics[name] for name in (
            "meter_envelope_control", "meter_control_health", "meter_estimate_quality",
            "meter_grid_valid", "meter_actuator_valid", "meter_ev_valid",
            "meter_control_state", "meter_episode_id", "meter_operational_fault",
            "meter_recovery_stage",
            "controlled_l1_observed_current", "controlled_l1_received_at_utc",
            "controlled_l1_source_at_utc", "controlled_l1_output_phase",
            "controlled_l1_mapping_epoch", "controlled_l1_mapping_adopted_at_utc",
            "controlled_l1_sequence", "controlled_l1_error", "controlled_l1_current_error",
            "controlled_l1_committed",
        ) if name in self.controller.source_diagnostics}
        if getattr(d, "meter_envelope", False) and getattr(d, "meter_zero_cause", None) is not None:
            # A known control/delivery/storage block is distinct from the
            # meter or stream being unhealthy. Keep their original classes.
            meter_sources["meter_control_health"] = "blocked"
        return self.controller.compact_state_attributes({
            **asdict(d),
            **diagnostics,
            # Present critical control health separately from uncertain EV
            # estimates on target/reason cards as well as Source Health.
            **meter_sources,
            "proposed_command": d.command,
            "command_result": self.controller.last_service_result,
            "command_error": self.controller.last_service_error,
            "control_block_reason": self.controller.control_block_reason,
        })
