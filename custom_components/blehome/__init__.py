# blehome/__init__.py
"""The BLEHome integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN, 
    DEFAULT_PREFIX, 
    PLATFORMS, 
    CONF_MAC, 
    CONF_SERVICE_UUID, 
    CONF_CHAR_UUID, 
    CONF_SUBDEVICES,
    CONF_BTHOME_MOCK,
)
from .ble_controller import BLEHomeController

_LOGGER = logging.getLogger(__name__)
_SERVICES_REGISTERED_KEY = "_services_registered"
SERVICE_DEBUG_STATUS = "debug_status"
SERVICE_DEBUG_INJECT = "debug_inject_bthome"
SERVICE_REMOVE_SUBDEVICE = "remove_subdevice"

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
    device_type = entry.data.get("device_type", "blehome.tled")
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
    controller.bthome_mock_enabled = bool(entry.options.get(CONF_BTHOME_MOCK, False))
    
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
    _LOGGER.info("BTHome integration loaded: %s", "bthome" in hass.config.components)

    # Register debug services once
    if not hass.data[DOMAIN].get(_SERVICES_REGISTERED_KEY):
        hass.data[DOMAIN][_SERVICES_REGISTERED_KEY] = True

        async def _handle_debug_status(call) -> None:
            entry_id = call.data.get("entry_id")
            mac = call.data.get("mac")
            controllers = [
                c for k, c in hass.data[DOMAIN].items()
                if k != _SERVICES_REGISTERED_KEY
            ]
            if entry_id:
                controllers = [hass.data[DOMAIN].get(entry_id)] if entry_id in hass.data[DOMAIN] else []
            elif mac:
                controllers = [c for c in controllers if c.mac_address.upper() == mac.upper()]

            for c in controllers:
                if c:
                    c.debug_dump_bluetooth_state()

        async def _handle_debug_inject(call) -> None:
            entry_id = call.data.get("entry_id")
            mac = call.data.get("mac")
            temp_c = call.data.get("temperature")
            target_mac = call.data.get("target_mac")
            controllers = [
                c for k, c in hass.data[DOMAIN].items()
                if k != _SERVICES_REGISTERED_KEY
            ]
            if entry_id:
                controllers = [hass.data[DOMAIN].get(entry_id)] if entry_id in hass.data[DOMAIN] else []
            elif mac:
                controllers = [c for c in controllers if c.mac_address.upper() == mac.upper()]

            for c in controllers:
                if c:
                    c.debug_dump_bluetooth_state()
                    c.debug_inject_mock_bthome(mac=target_mac, temp_c=temp_c)

        hass.services.async_register(
            DOMAIN,
            SERVICE_DEBUG_STATUS,
            _handle_debug_status,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Optional("mac"): str,
            }),
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_DEBUG_INJECT,
            _handle_debug_inject,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Optional("mac"): str,
                vol.Optional("target_mac"): str,
                vol.Optional("temperature"): vol.Coerce(float),
            }),
        )

        async def _handle_remove_subdevice(call) -> None:
            """Remove a sub-device from the gateway and clean up registries."""
            entry_id = call.data.get("entry_id")
            mac = call.data.get("mac")
            mesh_address = call.data.get("mesh_address")

            controllers = [
                c for k, c in hass.data[DOMAIN].items()
                if k != _SERVICES_REGISTERED_KEY
            ]
            if entry_id:
                controllers = [hass.data[DOMAIN].get(entry_id)] if entry_id in hass.data[DOMAIN] else []
            elif mac:
                controllers = [c for c in controllers if c.mac_address.upper() == mac.upper()]

            for controller in controllers:
                if not controller:
                    continue
                if mesh_address not in controller.subdevices:
                    _LOGGER.warning(
                        "Sub-device 0x%04X not found for %s",
                        mesh_address, controller.mac_address
                    )
                    continue

                entry = controller.config_entry
                new_options = dict(entry.options)
                subdevices_config = new_options.get(CONF_SUBDEVICES, {}).copy()
                subdevices_config.pop(str(mesh_address), None)
                new_options[CONF_SUBDEVICES] = subdevices_config
                hass.config_entries.async_update_entry(entry, options=new_options)
                _LOGGER.info(
                    "Removed sub-device 0x%04X from %s",
                    mesh_address, controller.mac_address
                )

        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_SUBDEVICE,
            _handle_remove_subdevice,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Optional("mac"): str,
                vol.Required("mesh_address"): vol.Coerce(int),
            }),
        )

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

    # Clean up removed sub-devices from entity & device registries
    removed = old_subdevices - set(subdevices.keys())
    if removed:
        er_registry = er.async_get(hass)
        dr_registry = dr.async_get(hass)
        for addr in removed:
            unique_id = f"{controller.mac_address}_{addr}"
            entity_id = er_registry.async_get_entity_id(Platform.LIGHT, DOMAIN, unique_id)
            if entity_id:
                er_registry.async_remove(entity_id)
            device_identifier = (DOMAIN, f"{controller.mac_address}_{addr:04X}")
            device = dr_registry.async_get_device(identifiers={device_identifier})
            if device:
                dr_registry.async_remove_device(device.id)
                _LOGGER.info("Cleaned up removed sub-device 0x%04X (%s)", addr, unique_id)

    controller.subdevices = subdevices
    controller.bthome_mock_enabled = bool(entry.options.get(CONF_BTHOME_MOCK, False))
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
