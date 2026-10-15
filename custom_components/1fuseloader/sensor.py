from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_BMW_DEVICE_ID, DOMAIN, NAME


SENSOR_DESCRIPTIONS = [
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
        if key.startswith("bmw_"):
            field = self.controller.bmw_context.get(key.removeprefix("bmw_"), {})
            return field.get("value") if field.get("available") else None
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
            return d.reason
        if key == "last_command":
            return self.controller.regulator.last_command or "none"
        if key == "service_result":
            return self.controller.last_service_result
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
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
            return attributes
        d = self.controller.last_decision
        if d is None:
            return None
        return {
            **asdict(d),
            "proposed_command": d.command,
            "command_result": self.controller.last_service_result,
            "command_error": self.controller.last_service_error,
            "control_block_reason": self.controller.control_block_reason,
        }
