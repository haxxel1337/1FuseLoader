from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME

BUTTON_DESCRIPTIONS = [
    ButtonEntityDescription(key="recalculate_now", name="Recalculate Now", icon="mdi:calculator"),
    ButtonEntityDescription(key="reset_state", name="Reset Internal State", icon="mdi:restart"),
    ButtonEntityDescription(key="start_overload_trial", name="Start New Overload Trial (7 A)", icon="mdi:timer-play-outline"),
    ButtonEntityDescription(key="end_overload_trial", name="End Overload Trial", icon="mdi:timer-stop-outline"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseButton(controller, entry, desc) for desc in BUTTON_DESCRIPTIONS])


class OneFuseButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller, entry: ConfigEntry, description: ButtonEntityDescription) -> None:
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
    def available(self) -> bool:
        return not (
            self.entity_description.key in {"start_overload_trial", "end_overload_trial"}
            and (self.controller.settings.automatic_overload_cycles
                 or getattr(self.controller.settings, "meter_envelope_control", False))
        )

    async def async_press(self) -> None:
        if not self.available:
            raise ServiceValidationError(
                "Manual overload trial controls are unavailable in automatic or standalone meter control."
            )
        if self.entity_description.key == "reset_state":
            await self.controller.async_reset_state()
        elif self.entity_description.key == "start_overload_trial":
            await self.controller.async_start_overload_trial()
        elif self.entity_description.key == "end_overload_trial":
            await self.controller.async_end_overload_trial()
        else:
            await self.controller.async_tick()
