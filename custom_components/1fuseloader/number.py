from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, DEFAULT_OPTIONS, validate_option_value, validate_options


@dataclass(frozen=True, kw_only=True)
class OneFuseNumberDescription(NumberEntityDescription):
    option_key: str


NUMBER_DESCRIPTIONS = [
    OneFuseNumberDescription(key="main_fuse_amps", option_key="main_fuse_amps", name="Main Fuse", native_min_value=10, native_max_value=63, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:fuse"),
    OneFuseNumberDescription(key="buffer_amps", option_key="buffer_amps", name="Buffer", native_min_value=0, native_max_value=10, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:shield-half-full"),
    OneFuseNumberDescription(key="minimum_reserve_amps", option_key="minimum_reserve_amps", name="Reserve While Keeping Minimum Charge", native_min_value=0, native_max_value=5, native_step=0.1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:shield-outline"),
    OneFuseNumberDescription(key="min_charge_amps", option_key="min_charge_amps", name="Min Charge", native_min_value=6, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:battery-arrow-down"),
    OneFuseNumberDescription(key="startup_charge_amps", option_key="startup_charge_amps", name="Startup Charge", native_min_value=6, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:ev-plug-type2"),
    OneFuseNumberDescription(key="max_charge_amps", option_key="max_charge_amps", name="Max Charge", native_min_value=6, native_max_value=32, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:battery-arrow-up"),
    OneFuseNumberDescription(key="soft_limit_amps", option_key="soft_limit_amps", name="Soft Guard Limit", native_min_value=10, native_max_value=80, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:speedometer-slow"),
    OneFuseNumberDescription(key="soft_for_seconds", option_key="soft_for_seconds", name="Soft Guard Delay", native_min_value=0, native_max_value=300, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-sand"),
    OneFuseNumberDescription(key="soft_current_amps", option_key="soft_current_amps", name="Soft Current", native_min_value=0, native_max_value=16, native_step=1, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:ev-plug-type2"),
    OneFuseNumberDescription(key="pause_limit_amps", option_key="pause_limit_amps", name="Pause Limit", native_min_value=10, native_max_value=80, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:pause-octagon"),
    OneFuseNumberDescription(key="pause_for_seconds", option_key="pause_for_seconds", name="Pause Delay", native_min_value=0, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-alert"),
    OneFuseNumberDescription(key="resume_below_amps", option_key="resume_below_amps", name="Resume Below", native_min_value=5, native_max_value=63, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:play-circle"),
    OneFuseNumberDescription(key="resume_for_seconds", option_key="resume_for_seconds", name="Resume Delay", native_min_value=0, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-check"),
    OneFuseNumberDescription(key="normal_ttl_seconds", option_key="normal_ttl_seconds", name="Normal TTL", native_min_value=60, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-cog"),
    OneFuseNumberDescription(key="soft_ttl_seconds", option_key="soft_ttl_seconds", name="Soft TTL", native_min_value=60, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-cog-outline"),
    OneFuseNumberDescription(key="resend_limit_every_seconds", option_key="resend_limit_every_seconds", name="Resend Limit Every", native_min_value=5, native_max_value=300, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:repeat"),
    OneFuseNumberDescription(key="min_seconds_between_actions", option_key="min_seconds_between_actions", name="Min Seconds Between Actions", native_min_value=1, native_max_value=60, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-lock"),
    OneFuseNumberDescription(key="max_sensor_age_seconds", option_key="max_sensor_age_seconds", name="Maximum Sensor Age", native_min_value=15, native_max_value=3600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-alert-outline"),
    OneFuseNumberDescription(key="increase_stable_seconds", option_key="increase_stable_seconds", name="Stable Load Before Increasing", native_min_value=5, native_max_value=300, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-sand"),
    OneFuseNumberDescription(key="increase_interval_seconds", option_key="increase_interval_seconds", name="Seconds Between 1 A Increases", native_min_value=5, native_max_value=120, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:stairs-up"),
    OneFuseNumberDescription(key="restart_stable_seconds", option_key="restart_stable_seconds", name="Stable Headroom Before Restart", native_min_value=15, native_max_value=600, native_step=5, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:play-pause"),
    OneFuseNumberDescription(key="measurement_settle_seconds", option_key="measurement_settle_seconds", name="Current Report Alignment Window", native_min_value=1, native_max_value=30, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:sync-clock"),
    OneFuseNumberDescription(key="overload_margin_amps", option_key="overload_margin_amps", name="Experimental Overload Margin", native_min_value=0.5, native_max_value=4, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:speedometer-slow"),
    OneFuseNumberDescription(key="overload_budget_seconds", option_key="overload_budget_seconds", name="Experimental Cumulative Overload Budget", native_min_value=60, native_max_value=1800, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-sand"),
    OneFuseNumberDescription(key="overload_transition_limit_amps", option_key="overload_transition_limit_amps", name="Experimental Overload Transition Limit", native_min_value=10, native_max_value=38, native_step=0.5, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, icon="mdi:speedometer"),
    OneFuseNumberDescription(key="overload_response_seconds", option_key="overload_response_seconds", name="Experimental Overload Response Window", native_min_value=1, native_max_value=10, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, icon="mdi:timer-alert"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneFuseNumber(controller, entry, desc) for desc in NUMBER_DESCRIPTIONS])


class OneFuseNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller, entry: ConfigEntry, description: OneFuseNumberDescription) -> None:
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
    def native_value(self) -> float | int | None:
        opts = validate_options(self.entry.options or {})
        return opts.get(self.entity_description.option_key)

    async def async_set_native_value(self, value: float) -> None:
        key = self.entity_description.option_key
        try:
            new_value = validate_option_value(key, value)
            validate_options({**self.entry.options, key: new_value})
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        await self.controller.async_set_option(key, new_value)
