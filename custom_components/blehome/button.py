"""Button entities for BLEHome integration."""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ble_controller import BLEHomeController
from .const import (
    CONF_FIRMWARE_PATH,
    DOMAIN,
    FIRMWARE_BIN_DIR,
)
from .firmware import scan_firmware_files

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
    _attr_entity_category = EntityCategory.CONFIG

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

        hass = self.controller.hass
        firmware_path = entry.options.get(CONF_FIRMWARE_PATH, "")
        if firmware_path and not await hass.async_add_executor_job(os.path.isfile, firmware_path):
            _LOGGER.warning("Configured firmware %s no longer exists, using the latest bundled one", firmware_path)
            firmware_path = ""
        if not firmware_path:
            # Auto-detect highest version firmware in bin/ directory
            found = await hass.async_add_executor_job(
                scan_firmware_files, hass.config.path(FIRMWARE_BIN_DIR)
            )
            if not found:
                raise HomeAssistantError(
                    "No firmware file in custom_components/blehome/bin/; "
                    "place a .bin/.hex file there or set one in the gateway options"
                )
            firmware_path = found[-1]
            _LOGGER.info("Auto-selected firmware: %s", firmware_path)

        _LOGGER.info(
            "Starting OTA update for 0x%04X with firmware: %s",
            self._address, firmware_path,
        )

        result = await self.controller.async_ota_update_node(
            self._address, firmware_path
        )

        if not result["success"]:
            raise HomeAssistantError(f"OTA update failed for 0x{self._address:04X}: {result['message']}")
        _LOGGER.info(
            "OTA update successful for 0x%04X: %s",
            self._address, result["message"],
        )
