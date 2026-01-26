"""Text entity for BLEHome debug."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from .const import DOMAIN, MANUFACTURER
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLEHome text entity."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BLEHomeDebugWrite(controller)])

class BLEHomeDebugWrite(TextEntity):
    """Debug text entity."""
    
    _attr_has_entity_name = True
    _attr_translation_key = "debug_write"
    _attr_pattern = "^[0-9a-fA-F]*$"
    _attr_icon = "mdi:console"

    def __init__(self, controller: BLEHomeController) -> None:
        """Initialize the text entity."""
        self.controller = controller
        self._mac = controller.mac_address
        self._attr_native_value = ""
        self._attr_unique_id = f"{self._mac}_debug_write"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name="BLE Gateway",
            manufacturer=MANUFACTURER,
            model="BLEHome Gateway",
        )

    @property
    def available(self) -> bool:
        return self.controller.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_availability_changed", self._handle_availability_update
            )
        )

    @callback
    def _handle_availability_update(self, event: Any) -> None:
        self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        """Send raw hex command."""
        try:
            data = bytes.fromhex(value)
            if await self.controller.send_command(data):
                self._attr_native_value = value
            self.async_write_ha_state()
        except ValueError:
            _LOGGER.error("Invalid hex format: %s", value)
