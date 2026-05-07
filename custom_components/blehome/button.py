"""Button entities for BLEHome integration."""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_FIRMWARE_PATH,
)
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLEHome button entities."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = []

    # OTA button for each sub-device
    for addr in controller.subdevices:
        entities.append(BLEHomeOTAButton(controller, addr))

    async_add_entities(entities)

    # Listen for new sub-devices to add OTA buttons
    @callback
    def async_discover_new_device(event: Any) -> None:
        if event.data.get("controller_mac") == controller.mac_address:
            address = event.data["address"]
            async_add_entities([BLEHomeOTAButton(controller, address)])

    entry.async_on_unload(
        hass.bus.async_listen(
            f"{DOMAIN}_new_subdevice_found", async_discover_new_device
        )
    )


class BLEHomeOTAButton(ButtonEntity):
    """Button to trigger OTA firmware update on a sub-device."""

    _attr_has_entity_name = True
    _attr_translation_key = "ota_update"
    _attr_icon = "mdi:cellphone-arrow-down"

    def __init__(self, controller: BLEHomeController, address: int) -> None:
        """Initialize the button."""
        self.controller = controller
        self._address = address
        self._attr_unique_id = f"{controller.mac_address}_{address:04X}_ota"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self.controller.mac_address}_{self._address:04X}")
            },
        )

    @property
    def available(self) -> bool:
        return self.controller.connected

    async def async_press(self) -> None:
        """Start OTA update on this sub-device."""
        entry = self.controller.config_entry
        if not entry:
            _LOGGER.error("No config entry for controller")
            return

        firmware_path = entry.options.get(CONF_FIRMWARE_PATH, "")
        if not firmware_path:
            # Auto-detect highest version firmware in bin/ directory
            bin_dir = self.controller.hass.config.path("custom_components/blehome/bin")
            found = []
            if os.path.isdir(bin_dir):
                for f in sorted(os.listdir(bin_dir), reverse=True):
                    if f.lower().endswith((".bin", ".hex")):
                        found.append(os.path.join(bin_dir, f))
            if found:
                firmware_path = found[0]
                _LOGGER.info("Auto-selected firmware: %s", firmware_path)
            else:
                _LOGGER.error(
                    "Firmware path not configured and no firmware files found "
                    "in custom_components/blehome/bin/. "
                    "Place a .bin/.hex file there or set firmware path in gateway options."
                )
                return

        _LOGGER.info(
            "Starting OTA update for 0x%04X with firmware: %s",
            self._address, firmware_path,
        )

        result = await self.controller.async_ota_update_node(
            self._address, firmware_path
        )

        if result["success"]:
            _LOGGER.info(
                "OTA update successful for 0x%04X: %s",
                self._address, result["message"],
            )
        else:
            _LOGGER.error(
                "OTA update failed for 0x%04X: %s",
                self._address, result["message"],
            )
