from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME

BUTTON_DESCRIPTIONS = [
    ButtonEntityDescription(key="recalculate_now", name="Recalculate Now", icon="mdi:calculator"),
    ButtonEntityDescription(key="reset_state", name="Reset Internal State", icon="mdi:restart"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseButton(controller, entry, desc) for desc in BUTTON_DESCRIPTIONS])


class OneFuseButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, controller, entry: ConfigEntry, description: ButtonEntityDescription) -> None:
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

    async def async_press(self) -> None:
        if self.entity_description.key == "reset_state":
            await self.controller.async_reset_state()
        else:
            await self.controller.async_tick()
