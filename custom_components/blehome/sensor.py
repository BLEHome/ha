"""Sensor entities for BLEHome."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_last_service_info,
    async_register_callback,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLEHome sensors."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BLEHomeRSSISensor(controller)])

class BLEHomeRSSISensor(SensorEntity):
    """Representation of an RSSI sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "rssi"

    def __init__(self, controller: BLEHomeController) -> None:
        """Initialize the sensor."""
        self.controller = controller
        self._mac = controller.mac_address
        self._attr_unique_id = f"{self._mac}_rssi"
        self._rssi: int | None = None

    @property
    def native_value(self) -> int | None:
        return self._rssi

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name="BLE Mesh Gateway",
            manufacturer=MANUFACTURER,
            model=f"{self.controller.device_type}.{self.controller.mac_suffix}.gateway",
        )

    @property
    def available(self) -> bool:
        return self.controller.connected

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        service_info = async_last_service_info(self.hass, self._mac, connectable=True)
        if service_info:
            self._rssi = service_info.rssi

        self.async_on_remove(
            async_register_callback(
                self.hass,
                self._handle_bluetooth_event,
                {"address": self._mac},
                BluetoothScanningMode.ACTIVE,
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_rssi_updated", self._handle_rssi_event)
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_availability_changed", self._handle_availability_update
            )
        )

    @callback
    def _handle_availability_update(self, event: Any) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_rssi_event(self, event: Any) -> None:
        if event.data.get("address") == self._mac:
            self._rssi = event.data.get("rssi")
            self.async_write_ha_state()

    @callback
    def _handle_bluetooth_event(self, service_info: Any, change: Any) -> None:
        self._rssi = service_info.rssi
        self.async_write_ha_state()
