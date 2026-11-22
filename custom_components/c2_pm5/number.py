from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PM5Coordinator
from .const import CONF_IDLE_DISCONNECT_MINUTES, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PM5Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IdleDisconnectTimeout(coordinator, entry)])


class IdleDisconnectTimeout(NumberEntity):
    """Configure how long the PM5 link stays open when idle."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_should_poll = False
    _attr_translation_key = CONF_IDLE_DISCONNECT_MINUTES

    def __init__(self, coordinator: PM5Coordinator, entry: ConfigEntry) -> None:
        self._entry = entry
        self._session = coordinator.session
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{coordinator.address}_{CONF_IDLE_DISCONNECT_MINUTES}"

    @property
    def native_value(self) -> int:
        return self._session.idle_disconnect_minutes

    async def async_set_native_value(self, value: float) -> None:
        if not self.native_min_value <= value <= self.native_max_value or value % 1:
            raise ServiceValidationError(
                "Idle disconnect timeout must be a whole number from 1 to 60 minutes."
            )

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_IDLE_DISCONNECT_MINUTES: int(value)},
        )
        self.async_write_ha_state()
