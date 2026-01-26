# blehome/__init__.py
"""The BLEHome integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN, 
    DEFAULT_PREFIX, 
    PLATFORMS, 
    CONF_MAC, 
    CONF_SERVICE_UUID, 
    CONF_CHAR_UUID, 
    CONF_SUBDEVICES
)
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BLEHome from a config entry."""
    mac: str = entry.data[CONF_MAC]
    
    # Update entry title if needed (e.g., BLE ABCD)
    mac_suffix = mac.replace(":", "").replace("-", "")[-4:].upper()
    current_prefix = DEFAULT_PREFIX
    if entry.title and " " in entry.title:
        current_prefix = entry.title.split()[0]
            
    new_title = f"{current_prefix} {mac_suffix}"
    if entry.title != new_title:
        hass.config_entries.async_update_entry(entry, title=new_title)
    
    name = entry.data.get("name", new_title)
    service_uuid: str = entry.data[CONF_SERVICE_UUID]
    char_uuid: str = entry.data[CONF_CHAR_UUID]
    device_type = entry.data.get("device_type", "tled")
    mac_suffix = entry.data.get("mac_suffix", mac.replace(":", "").replace("-", "")[-4:].lower())
    
    # Create BLEHome controller instance
    controller = BLEHomeController(hass, mac, service_uuid, char_uuid)
    controller.name = name
    controller.device_type = device_type
    controller.mac_suffix = mac_suffix
    controller.config_entry = entry
    
    # Load subdevices from options
    raw_subdevices: dict[str, Any] = entry.options.get(CONF_SUBDEVICES, {})
    subdevices: dict[int, Any] = {}
    for k, v in raw_subdevices.items():
        try:
            subdevices[int(k)] = v
        except (ValueError, TypeError):
             _LOGGER.warning("Ignored invalid subdevice address: %s", k)
    controller.subdevices = subdevices
    
    # Identify gateway's own mesh address
    if subdevices:
        # Priority for devices containing "Gateway" or "网关" in their name
        gateway_addr = next(
            (addr for addr, info in subdevices.items() if any(kw in info["name"] for kw in ["Gateway", "网关"])), 
            next(iter(subdevices))
        )
        controller.gateway_address = gateway_addr
        _LOGGER.info("Set Mesh address 0x%04X as BLEHome gateway proxy", gateway_addr)
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = controller
    
    # Connect to device
    connected = await controller.connect()
    if not connected:
        _LOGGER.warning("Failed to connect to %s at %s, will retry", name, mac)
        raise ConfigEntryNotReady(f"Could not connect to {mac}")
    
    # Load platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update without reloading the integration."""
    controller: BLEHomeController = hass.data[DOMAIN][entry.entry_id]
    old_subdevices = set(controller.subdevices.keys())
    
    raw_subdevices: dict[str, Any] = entry.options.get(CONF_SUBDEVICES, {})
    subdevices: dict[int, Any] = {}
    
    for k, v in raw_subdevices.items():
        try:
            addr = int(k)
            subdevices[addr] = v
            # If it's a new device, fire event to create entity
            if addr not in old_subdevices:
                hass.bus.async_fire(
                    f"{DOMAIN}_new_subdevice_found",
                    {
                        "controller_mac": controller.mac_address,
                        "address": addr, 
                        "name": v["name"],
                        "state": v.get("state", {"on": False, "brightness": 0})
                    }
                )
        except (ValueError, TypeError):
            continue
    
    controller.subdevices = subdevices
    _LOGGER.info("Subdevice configuration updated for %s", controller.mac_address)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller: BLEHomeController = hass.data[DOMAIN].pop(entry.entry_id)
    
    # Disconnect
    if controller.connected:
        await controller.disconnect()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    return unload_ok
