"""Light entities for BLEHome integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLEHome light entities."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        BLEHomeLight(controller, addr, info["name"])
        for addr, info in controller.subdevices.items()
    ]
    async_add_entities(entities)

    @callback
    def async_discover_new_device(event: Any) -> None:
        if event.data.get("controller_mac") == controller.mac_address:
            address = event.data["address"]
            name = event.data["name"]
            async_add_entities([BLEHomeLight(controller, address, name)])

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_new_subdevice_found", async_discover_new_device)
    )

class BLEHomeLight(LightEntity):
    """Representation of a BLEHome light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_translation_key = "mesh_light"

    def __init__(self, controller: BLEHomeController, address: int, name: str) -> None:
        """Initialize the light."""
        self.controller = controller
        self.address = address
        self._device_id = name
        self._attr_unique_id = f"{controller.mac_address}_{address}"
        self._is_on = False
        self._brightness = 0
        
        if address in controller.subdevices:
            state = controller.subdevices[address]["state"]
            self._is_on = state["on"]
            self._brightness = state["brightness"]

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_subdevice_updated", self._handle_state_update
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_availability_changed", self._handle_availability_update
            )
        )

    @callback
    def _handle_state_update(self, event: Any) -> None:
        if event.data.get("address") == self.address:
            state = event.data["state"]
            self._is_on = state["on"]
            self._brightness = state["brightness"]
            self.async_write_ha_state()

    @callback
    def _handle_availability_update(self, event: Any) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.controller.connected

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.controller.mac_address}_{self.address:04X}")},
            name=f"Light {self.address:04X}",
            manufacturer=MANUFACTURER,
            model=self._device_id,
            via_device=(DOMAIN, self.controller.mac_address),
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness or 255)
        if await self.controller.send_control_command(self.address, True, brightness):
            self._is_on = True
            self._brightness = brightness
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self.controller.send_control_command(self.address, False, self._brightness):
            self._is_on = False
            self.async_write_ha_state()
