from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME


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
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseSensor(controller, entry, desc) for desc in SENSOR_DESCRIPTIONS])


class OneFuseSensor(SensorEntity):
    _attr_has_entity_name = True

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
        d = self.controller.last_decision
        key = self.entity_description.key
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
            return d.command or "none"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        d = self.controller.last_decision
        if d is None:
            return None
        return asdict(d)
