from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, DEFAULT_OPTIONS


@dataclass(frozen=True, kw_only=True)
class OneFuseSwitchDescription(SwitchEntityDescription):
    option_key: str


SWITCH_DESCRIPTIONS = [
    OneFuseSwitchDescription(key="active_control", option_key="active_control", name="Active Control", icon="mdi:power"),
    OneFuseSwitchDescription(key="dry_run", option_key="dry_run", name="Dry Run", icon="mdi:test-tube"),
    OneFuseSwitchDescription(key="enable_dynamic_limit", option_key="enable_dynamic_limit", name="Enable Dynamic Limit", icon="mdi:ev-station"),
    OneFuseSwitchDescription(key="enable_soft_guard", option_key="enable_soft_guard", name="Enable Soft Guard", icon="mdi:shield-half-full"),
    OneFuseSwitchDescription(key="enable_pause_resume", option_key="enable_pause_resume", name="Enable Pause Resume", icon="mdi:pause-play"),
    OneFuseSwitchDescription(key="force_min_when_available_too_low", option_key="force_min_when_available_too_low", name="Force Min When Available Too Low", icon="mdi:battery-heart"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseSwitch(controller, entry, desc) for desc in SWITCH_DESCRIPTIONS])


class OneFuseSwitch(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, controller, entry: ConfigEntry, description: OneFuseSwitchDescription) -> None:
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
    def is_on(self) -> bool:
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self.entry.options or {})
        return bool(opts.get(self.entity_description.option_key))

    async def async_turn_on(self, **kwargs) -> None:
        await self.controller.async_set_switch(self.entity_description.option_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.controller.async_set_switch(self.entity_description.option_key, False)
