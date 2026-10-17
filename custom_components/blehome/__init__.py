# blehome/__init__.py
"""The BLEHome integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .ble_controller import BLEHomeController, async_get_registry_device

try:
    # HA validates service data with probatio from 2026.10; mixing libraries breaks error mapping
    import probatio as vol
except ImportError:
    import voluptuous as vol
from .const import (
    CONF_BTHOME_MOCK,
    CONF_CHAR_UUID,
    CONF_MAC,
    CONF_SERVICE_UUID,
    CONF_SUBDEVICES,
    DEFAULT_PREFIX,
    DOMAIN,
    MESH_ADDR_MAX,
    MESH_ADDR_MIN,
    PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)
_SERVICES_REGISTERED_KEY = "_services_registered"
SERVICE_DEBUG_STATUS = "debug_status"
SERVICE_DEBUG_INJECT = "debug_inject_bthome"
SERVICE_REMOVE_SUBDEVICE = "remove_subdevice"
SERVICE_OTA_UPDATE = "ota_update"
_SERVICES = (SERVICE_DEBUG_STATUS, SERVICE_DEBUG_INJECT, SERVICE_REMOVE_SUBDEVICE, SERVICE_OTA_UPDATE)

_NON_EMPTY = vol.All(cv.string, vol.Length(min=1))
_GATEWAY_TARGET = {
    vol.Optional("entry_id"): _NON_EMPTY,
    vol.Optional("mac"): _NON_EMPTY,
}
_MESH_ADDRESS = vol.All(vol.Coerce(int), vol.Range(min=MESH_ADDR_MIN, max=MESH_ADDR_MAX))


def _target_schema(fields: dict) -> vol.All:
    """Service schema that requires entry_id or mac, so a call never hits every gateway."""
    return vol.All(
        vol.Schema({**_GATEWAY_TARGET, **fields}),
        cv.has_at_least_one_key("entry_id", "mac"),
    )


def _get_controllers(hass, entry_id=None, mac=None):
    """Get BLEHome controllers filtered by entry_id or MAC address."""
    controllers = [
        c for k, c in hass.data[DOMAIN].items()
        if k != _SERVICES_REGISTERED_KEY
    ]
    if entry_id:
        return [hass.data[DOMAIN].get(entry_id)] if entry_id in hass.data[DOMAIN] else []
    if mac:
        return [c for c in controllers if c.mac_address.upper() == mac.upper()]
    return controllers


def _require_controllers(hass, call: ServiceCall) -> list[BLEHomeController]:
    """Resolve the target gateways of a service call or raise."""
    controllers = [
        c for c in _get_controllers(hass, call.data.get("entry_id"), call.data.get("mac")) if c
    ]
    if not controllers:
        raise ServiceValidationError("No BLEHome gateway matches the given entry_id or mac")
    return controllers


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

        async def _handle_debug_status(call: ServiceCall) -> None:
            for c in _require_controllers(hass, call):
                c.debug_dump_bluetooth_state()

        async def _handle_debug_inject(call: ServiceCall) -> None:
            for c in _require_controllers(hass, call):
                c.debug_dump_bluetooth_state()
                c.debug_inject_mock_bthome(
                    mac=call.data.get("target_mac"), temp_c=call.data.get("temperature")
                )

        async def _handle_remove_subdevice(call: ServiceCall) -> None:
            """Remove a sub-device; the options listener unprovisions it and cleans registries."""
            mesh_address = call.data["mesh_address"]
            for controller in _require_controllers(hass, call):
                if mesh_address not in controller.subdevices:
                    raise ServiceValidationError(
                        f"Sub-device 0x{mesh_address:04X} not found on {controller.mac_address}"
                    )
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

        async def _handle_ota_update(call: ServiceCall) -> None:
            """Perform OTA firmware update on a mesh node."""
            node_address = call.data["node_address"]
            firmware_path = call.data["firmware_path"]
            for controller in _require_controllers(hass, call):
                result = await controller.async_ota_update_node(node_address, firmware_path)
                _LOGGER.info("OTA result for 0x%04X: %s - %s",
                             node_address, result["success"], result["message"])

        async_register_admin_service(
            hass, DOMAIN, SERVICE_DEBUG_STATUS, _handle_debug_status,
            schema=_target_schema({}),
        )
        async_register_admin_service(
            hass, DOMAIN, SERVICE_DEBUG_INJECT, _handle_debug_inject,
            schema=_target_schema({
                vol.Optional("target_mac"): cv.string,
                vol.Optional("temperature"): vol.Coerce(float),
            }),
        )
        async_register_admin_service(
            hass, DOMAIN, SERVICE_REMOVE_SUBDEVICE, _handle_remove_subdevice,
            schema=_target_schema({vol.Required("mesh_address"): _MESH_ADDRESS}),
        )
        async_register_admin_service(
            hass, DOMAIN, SERVICE_OTA_UPDATE, _handle_ota_update,
            schema=_target_schema({
                vol.Required("node_address"): _MESH_ADDRESS,
                vol.Required("firmware_path"): cv.string,
            }),
        )

    # Connect once; HA retries setup with backoff, so a missing gateway does not hold startup
    connected = await controller.connect(retries=1)
    if not connected:
        _LOGGER.warning("Failed to connect to %s at %s, will retry", name, mac)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await controller.async_shutdown()
        raise ConfigEntryNotReady(f"Could not connect to {mac}")

    async def _async_stop(_event: Event) -> None:
        await controller.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    # Load platforms
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await controller.async_shutdown()
        raise

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
            # Try to unprovision the device via mesh
            _LOGGER.info("Sending delete command to removed sub-device 0x%04X...", addr)
            await controller.async_delete_node(addr)

            unique_id = f"{controller.mac_address}_{addr}"
            entity_id = er_registry.async_get_entity_id(Platform.LIGHT, DOMAIN, unique_id)
            if entity_id:
                er_registry.async_remove(entity_id)
            device_identifier = (DOMAIN, f"{controller.mac_address}_{addr:04X}")
            device = async_get_registry_device(dr_registry, device_identifier, entry.entry_id)
            if device:
                dr_registry.async_remove_device(device.id)
                _LOGGER.info("Cleaned up removed sub-device 0x%04X (%s)", addr, unique_id)

    controller.subdevices = subdevices
    controller.bthome_mock_enabled = bool(entry.options.get(CONF_BTHOME_MOCK, False))
    _LOGGER.info("Subdevice configuration updated for %s", controller.mac_address)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Always shut the controller down: its reconnect loop may still run while offline
    controller: BLEHomeController = hass.data[DOMAIN].pop(entry.entry_id)
    await controller.async_shutdown()

    if not any(k != _SERVICES_REGISTERED_KEY for k in hass.data[DOMAIN]):
        for service in _SERVICES:
            hass.services.async_remove(DOMAIN, service)
        hass.data[DOMAIN].pop(_SERVICES_REGISTERED_KEY, None)

    return unload_ok
