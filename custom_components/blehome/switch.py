"""Switch entities for BLEHome."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ble_controller import BLEHomeController
from .const import DOMAIN, MANUFACTURER, CONF_BTHOME_MOCK


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLEHome switch entities."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BLEHomeBthomeMockSwitch(controller, entry)])


class BLEHomeBthomeMockSwitch(SwitchEntity):
    """Toggle BTHome mock injection."""

    _attr_has_entity_name = True
    _attr_translation_key = "bthome_mock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bug"

    def __init__(self, controller: BLEHomeController, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        self.controller = controller
        self.entry = entry
        self._mac = controller.mac_address
        self._attr_unique_id = f"{self._mac}_bthome_mock"

    @property
    def is_on(self) -> bool:
        return bool(self.controller.bthome_mock_enabled)

    @property
    def available(self) -> bool:
        return self.controller.connected

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name="BLE Mesh Gateway",
            manufacturer=MANUFACTURER,
            model=f"{self.controller.device_type}.{self.controller.mac_suffix}.gateway",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_availability_changed", self._handle_availability_update
            )
        )

    @callback
    def _handle_availability_update(self, event: Any) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        self.controller.bthome_mock_enabled = enabled
        new_options = dict(self.entry.options)
        new_options[CONF_BTHOME_MOCK] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        self.async_write_ha_state()
