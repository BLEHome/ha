# blehome/config_flow.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from bleak import BleakClient, BleakScanner
from homeassistant.config_entries import ConfigFlow, ConfigEntry
from homeassistant.const import CONF_NAME, CONF_MAC
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN, 
    DEFAULT_PREFIX, 
    DEFAULT_SERVICE_UUID, 
    DEFAULT_CHAR_UUID,
    CONF_SERVICE_UUID,
    CONF_CHAR_UUID
)

_LOGGER = logging.getLogger(__name__)

class BLEHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BLEHome."""
    
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_devices: dict[str, Any] = {}
        self._selected_device: Any = None
        self._device_services: dict[str, list[dict[str, str]]] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if user_input["setup_method"] == "scan":
                return await self.async_step_scan()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("setup_method", default="scan"): vol.In({
                    "scan": "scan",
                    "manual": "manual"
                })
            })
        )

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Scan for BLEHome devices."""
        if user_input is not None:
            selected_mac = user_input["device"]
            self._selected_device = self._discovered_devices.get(selected_mac)
            if self._selected_device:
                return await self.async_step_select_service()

        self._discovered_devices = {}
        try:
            devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
            
            device_options = {}
            for device, adv_data in devices.values():
                # 0x07D7 is WCH (BLEHome core manufacturer)
                if 2007 in adv_data.manufacturer_data:
                    self._discovered_devices[device.address] = device
                    rssi = adv_data.rssi
                    label = f"{device.name or 'Unknown BLEHome'} ({device.address}) [{rssi} dBm]"
                    device_options[device.address] = label

            if not self._discovered_devices:
                return self.async_show_form(
                    step_id="scan",
                    errors={"base": "no_devices_found"}
                )

            return self.async_show_form(
                step_id="scan",
                data_schema=vol.Schema({
                    vol.Required("device"): vol.In(device_options)
                }),
                description_placeholders={"count": len(self._discovered_devices)}
            )

        except Exception as e:
            _LOGGER.error("Error scanning for BLEHome devices: %s", e)
            return self.async_show_form(step_id="scan", errors={"base": "scan_failed"})

    async def async_step_select_service(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select GATT service and characteristic."""
        if user_input is not None and "char_uuid" in user_input:
            await self.async_set_unique_id(self._selected_device.address)
            self._abort_if_unique_id_configured()
            
            mac_suffix = self._selected_device.address.replace(":", "").replace("-", "")[-4:].upper()
            prefix = DEFAULT_PREFIX
            if self._selected_device.name:
                prefix = self._selected_device.name.split()[0].split('_')[0]
            
            device_name = f"{prefix} {mac_suffix}"
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_MAC: self._selected_device.address,
                    CONF_NAME: device_name,
                    "device_type": f"blehome.{prefix.lower()}",
                    "mac_suffix": mac_suffix.lower(),
                    CONF_SERVICE_UUID: user_input["service_uuid"],
                    CONF_CHAR_UUID: user_input["char_uuid"]
                }
            )

        try:
            async with BleakClient(self._selected_device, timeout=15.0) as client:
                services = client.services
                self._device_services = {}
                for service in services:
                    chars = []
                    for char in service.characteristics:
                        props = ",".join(char.properties)
                        chars.append({"uuid": char.uuid, "properties": props})
                    if chars:
                        self._device_services[service.uuid] = chars
            
            # Auto-match if possible
            found_service = None
            found_char = None
            for s_uuid in self._device_services:
                if s_uuid.lower() == DEFAULT_SERVICE_UUID.lower() or "ffe0" in s_uuid.lower():
                    found_service = s_uuid
                    for char in self._device_services[s_uuid]:
                        if char["uuid"].lower() == DEFAULT_CHAR_UUID.lower() or "ffe1" in char["uuid"].lower():
                            found_char = char["uuid"]
                            break
                    break
            
            if found_service and found_char:
                return await self.async_step_select_service(
                    {"service_uuid": found_service, "char_uuid": found_char}
                )

            return self.async_show_form(
                step_id="select_service",
                data_schema=vol.Schema({
                    vol.Required("service_uuid"): vol.In(list(self._device_services.keys())),
                    vol.Required("char_uuid"): str
                }),
                description_placeholders={"device": self._selected_device.address}
            )
            
        except Exception as e:
            _LOGGER.error("Error getting services: %s", e)
            return self.async_show_form(
                step_id="select_service", 
                errors={"base": "service_scan_failed"}
            )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle manual configuration."""
        if user_input is not None:
            mac = user_input[CONF_MAC].upper()
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            
            # Extract prefix from name for manual entry as well
            prefix = user_input[CONF_NAME].split()[0].lower()
            mac_suffix = mac.replace(":", "").replace("-", "")[-4:].lower()
            user_input["device_type"] = f"blehome.{prefix}"
            user_input["mac_suffix"] = mac_suffix

            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input
            )
            
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_MAC): str,
                vol.Required(CONF_SERVICE_UUID, default=DEFAULT_SERVICE_UUID): str,
                vol.Required(CONF_CHAR_UUID, default=DEFAULT_CHAR_UUID): str
            })
        )
