from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .const import (
    CONF_CHARGER_CURRENT_ENTITY,
    CONF_CONTROLLED_PHASE,
    CONF_DEVICE_ID,
    CONF_L1_ENTITY,
    CONF_L2_ENTITY,
    CONF_L3_ENTITY,
    CONF_NAME,
    CONF_STATUS_ENTITY,
    DEFAULT_CHARGER_CURRENT_ENTITY,
    DEFAULT_CONTROLLED_PHASE,
    DEFAULT_DEVICE_ID,
    DEFAULT_L1_ENTITY,
    DEFAULT_L2_ENTITY,
    DEFAULT_L3_ENTITY,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    DEFAULT_STATUS_ENTITY,
    DOMAIN,
    OPTION_NUMBER_RANGES,
    validate_options,
)


def _sensor_selector(*, current: bool = False, integration: str | None = None):
    """Place new filtering features under filter, as required by HA 2026.10."""
    entity_filter = selector.EntityFilterSelectorConfig(domain="sensor")
    if current:
        entity_filter["unit_of_measurement"] = ["A"]
    if integration:
        entity_filter["integration"] = integration
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=entity_filter))


def _config_schema(defaults: dict[str, Any] | None = None, *, include_name: bool = True) -> vol.Schema:
    d = defaults or {}
    fields = {
            vol.Required(CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID)): selector.DeviceSelector(selector.DeviceSelectorConfig(integration="easee")),
            vol.Required(CONF_STATUS_ENTITY, default=d.get(CONF_STATUS_ENTITY, DEFAULT_STATUS_ENTITY)): _sensor_selector(integration="easee"),
            vol.Required(CONF_L1_ENTITY, default=d.get(CONF_L1_ENTITY, DEFAULT_L1_ENTITY)): _sensor_selector(current=True),
            vol.Required(CONF_L2_ENTITY, default=d.get(CONF_L2_ENTITY, DEFAULT_L2_ENTITY)): _sensor_selector(current=True),
            vol.Required(CONF_L3_ENTITY, default=d.get(CONF_L3_ENTITY, DEFAULT_L3_ENTITY)): _sensor_selector(current=True),
            vol.Required(CONF_CHARGER_CURRENT_ENTITY, default=d.get(CONF_CHARGER_CURRENT_ENTITY, DEFAULT_CHARGER_CURRENT_ENTITY)): _sensor_selector(current=True, integration="easee"),
            vol.Required(CONF_CONTROLLED_PHASE, default=d.get(CONF_CONTROLLED_PHASE, DEFAULT_CONTROLLED_PHASE)): vol.In(["L1"]),
        }
    if include_name:
        fields = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, DEFAULT_NAME)): str, **fields}
    return vol.Schema(fields)


class OneFuseLoaderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _input_errors(self, data: dict[str, Any]) -> dict[str, str]:
        """Check registry references and units without controlling the charger."""
        errors: dict[str, str] = {}
        if data.get(CONF_CONTROLLED_PHASE) != "L1":
            errors[CONF_CONTROLLED_PHASE] = "unsupported_phase"
        device, easee_entry = dr.async_get_device_and_config_entry_for_domain(
            self.hass, data.get(CONF_DEVICE_ID, ""), domain="easee",
        )
        if device is None or easee_entry is None:
            errors[CONF_DEVICE_ID] = "device_not_easee"
        grid_keys = (CONF_L1_ENTITY, CONF_L2_ENTITY, CONF_L3_ENTITY)
        grid_ids = [data.get(key) for key in grid_keys]
        for key in grid_keys:
            if grid_ids.count(data.get(key)) > 1:
                errors[key] = "duplicate_current_entity"
        if data.get(CONF_CHARGER_CURRENT_ENTITY) in grid_ids:
            errors[CONF_CHARGER_CURRENT_ENTITY] = "duplicate_current_entity"
        current_keys = (*grid_keys, CONF_CHARGER_CURRENT_ENTITY)
        for key in (CONF_STATUS_ENTITY, *current_keys):
            entity_id = data.get(key, "")
            state = self.hass.states.get(entity_id)
            if state is None or not entity_id.startswith("sensor."):
                errors[key] = "entity_not_found"
            elif key in current_keys and state.attributes.get("unit_of_measurement") != "A":
                errors[key] = "current_unit_required"
        # The source heartbeat belongs to the selected charger. Subtracting a
        # different car's current would underestimate this house's base load.
        if device is not None and easee_entry is not None:
            registry = er.async_get(self.hass)
            for key in (CONF_STATUS_ENTITY, CONF_CHARGER_CURRENT_ENTITY):
                source = registry.async_get(data.get(key, ""))
                if key not in errors and (
                    source is None or source.platform != "easee" or source.device_id != device.id
                ):
                    errors[key] = "charger_sensor_mismatch"
        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_DEVICE_ID] = str(user_input.get(CONF_DEVICE_ID, "")).strip()

            if not user_input[CONF_DEVICE_ID]:
                errors[CONF_DEVICE_ID] = "device_id_required"
            else:
                errors.update(self._input_errors(user_input))
            if not errors:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                title = str(user_input.get(CONF_NAME, DEFAULT_NAME)).strip() or DEFAULT_NAME
                data = dict(user_input)
                data.pop(CONF_NAME, None)
                return self.async_create_entry(title=title, data=data, options=dict(DEFAULT_OPTIONS))

        return self.async_show_form(step_id="user", data_schema=_config_schema(user_input), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Allow entity changes through HA instead of editing its storage."""
        entry = self._get_reconfigure_entry()
        defaults = {**entry.data, CONF_NAME: entry.title}
        errors: dict[str, str] = {}
        if user_input is not None:
            defaults.update(user_input)
            errors = self._input_errors(user_input)
            if not errors:
                await self.async_set_unique_id(str(user_input[CONF_DEVICE_ID]))
                self._abort_if_unique_id_mismatch()
                data = dict(user_input)
                data.pop(CONF_NAME, None)
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(step_id="reconfigure", data_schema=_config_schema(defaults, include_name=False), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the options flow.

        HA 2026.7 exposes the current config entry as self.config_entry
        inside OptionsFlow; do not pass/store it manually.
        """
        return OneFuseLoaderOptionsFlow()


class OneFuseLoaderOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self.config_entry.options or {})

        errors: dict[str, str] = {}
        error_detail = ""
        if user_input is not None:
            try:
                validated = validate_options({**opts, **user_input})
            except ValueError as err:
                errors["base"] = "invalid_settings"
                error_detail = str(err)
                opts.update(user_input)
            else:
                return self.async_create_entry(title="", data=validated)
        fields = {}
        for key, default in DEFAULT_OPTIONS.items():
            if key in {"unused_phase_current", "force_min_when_available_too_low"}:
                continue
            marker = vol.Required(key, default=opts[key])
            if isinstance(default, bool):
                fields[marker] = bool
            else:
                minimum, maximum, step = OPTION_NUMBER_RANGES[key]
                fields[marker] = selector.NumberSelector(selector.NumberSelectorConfig(
                    min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX,
                ))
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"error": error_detail},
        )
