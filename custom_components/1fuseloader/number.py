from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, DEFAULT_OPTIONS


@dataclass(frozen=True, kw_only=True)
class OneFuseNumberDescription(NumberEntityDescription):
    option_key: str


NUMBER_DESCRIPTIONS = [
    OneFuseNumberDescription(key="main_fuse_amps", option_key="main_fuse_amps", name="Main Fuse", native_min_value=10, native_max_value=63, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:fuse"),
    OneFuseNumberDescription(key="buffer_amps", option_key="buffer_amps", name="Buffer", native_min_value=0, native_max_value=10, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:shield-half-full"),
    OneFuseNumberDescription(key="min_charge_amps", option_key="min_charge_amps", name="Min Charge", native_min_value=0, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:battery-arrow-down"),
    OneFuseNumberDescription(key="max_charge_amps", option_key="max_charge_amps", name="Max Charge", native_min_value=6, native_max_value=32, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:battery-arrow-up"),
    OneFuseNumberDescription(key="safe_l1_amps", option_key="safe_l1_amps", name="Safe L1", native_min_value=10, native_max_value=35, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:shield-check"),
    OneFuseNumberDescription(key="soft_limit_amps", option_key="soft_limit_amps", name="Soft Guard Limit", native_min_value=10, native_max_value=40, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:speedometer-slow"),
    OneFuseNumberDescription(key="soft_for_seconds", option_key="soft_for_seconds", name="Soft Guard Delay", native_min_value=0, native_max_value=300, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-sand"),
    OneFuseNumberDescription(key="soft_current_amps", option_key="soft_current_amps", name="Soft Current", native_min_value=0, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:ev-plug-type2"),
    OneFuseNumberDescription(key="pause_limit_amps", option_key="pause_limit_amps", name="Pause Limit", native_min_value=10, native_max_value=40, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:pause-octagon"),
    OneFuseNumberDescription(key="pause_for_seconds", option_key="pause_for_seconds", name="Pause Delay", native_min_value=0, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-alert"),
    OneFuseNumberDescription(key="resume_below_amps", option_key="resume_below_amps", name="Resume Below", native_min_value=5, native_max_value=35, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:play-circle"),
    OneFuseNumberDescription(key="resume_for_seconds", option_key="resume_for_seconds", name="Resume Delay", native_min_value=0, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-check"),
    OneFuseNumberDescription(key="normal_ttl_seconds", option_key="normal_ttl_seconds", name="Normal TTL", native_min_value=10, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-cog"),
    OneFuseNumberDescription(key="soft_ttl_seconds", option_key="soft_ttl_seconds", name="Soft TTL", native_min_value=10, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-cog-outline"),
    OneFuseNumberDescription(key="resend_limit_every_seconds", option_key="resend_limit_every_seconds", name="Resend Limit Every", native_min_value=5, native_max_value=300, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:repeat"),
    OneFuseNumberDescription(key="min_seconds_between_actions", option_key="min_seconds_between_actions", name="Min Seconds Between Actions", native_min_value=0, native_max_value=60, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-lock"),
    OneFuseNumberDescription(key="unused_phase_current", option_key="unused_phase_current", name="Unused Phase Current", native_min_value=0, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:transmission-tower-off"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseNumber(controller, entry, desc) for desc in NUMBER_DESCRIPTIONS])


class OneFuseNumber(NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, controller, entry: ConfigEntry, description: OneFuseNumberDescription) -> None:
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
    def native_value(self) -> float | int | None:
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self.entry.options or {})
        return opts.get(self.entity_description.option_key)

    async def async_set_native_value(self, value: float) -> None:
        key = self.entity_description.option_key
        # Preserve integer knobs as integers.
        current = DEFAULT_OPTIONS.get(key)
        new_value: Any = int(value) if isinstance(current, int) and not isinstance(current, bool) else float(value)
        await self.controller.async_set_option(key, new_value)
