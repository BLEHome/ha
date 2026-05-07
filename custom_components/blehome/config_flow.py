# blehome/config_flow.py
from __future__ import annotations

import os
import asyncio
import logging
from typing import Any

import voluptuous as vol
from bleak import BleakClient, BleakScanner
from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.const import CONF_NAME, CONF_MAC
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    DEFAULT_PREFIX,
    DEFAULT_SERVICE_UUID,
    DEFAULT_CHAR_UUID,
    CONF_SERVICE_UUID,
    CONF_CHAR_UUID,
    CONF_SUBDEVICES,
    CONF_FIRMWARE_PATH,
)

_LOGGER = logging.getLogger(__name__)


async def _provision_device(
    device_address: str,
    service_uuid: str,
    char_uuid: str,
    net_key: bytes,
    node_address: int,
) -> tuple[bool, str]:
    """Connect to an unprovisioned WCH BLE device and provision it.

    Uses a standalone BleakClient (no HA dependencies beyond bleak).
    Returns (success, message).
    """
    if len(net_key) != 16:
        return False, "Network key must be exactly 16 bytes"

    try:
        async with BleakClient(device_address, timeout=15.0) as client:
            if not client.is_connected:
                return False, "Failed to connect to device"

            _LOGGER.info("Connected to %s for provisioning", device_address)

            # Find the notify characteristic in the target service
            notify_uuid = None
            for service in client.services:
                if service.uuid.lower() == service_uuid.lower():
                    for char in service.characteristics:
                        if "notify" in char.properties:
                            notify_uuid = char.uuid
                            break
                    break

            if not notify_uuid:
                # Fallback: look for any notification char in any service
                for service in client.services:
                    for char in service.characteristics:
                        if "notify" in char.properties:
                            notify_uuid = char.uuid
                            break
                    if notify_uuid:
                        break

            if not notify_uuid:
                return False, "No notification characteristic found on device"

            # Set up event-based ACK waiting
            event = asyncio.Event()
            ack_data: list[bytes | None] = [None]
            ack_expected: list[int | None] = [None]

            def _handler(_sender: int, data: bytearray) -> None:
                if (
                    ack_expected[0] is not None
                    and len(data) >= 1
                    and data[0] == ack_expected[0]
                ):
                    ack_data[0] = bytes(data)
                    event.set()

            await client.start_notify(notify_uuid, _handler)

            # --- Step 1: Set network info (IV index = 1, flag = 0) ---
            iv_index = 1
            flag = 0
            cmd1 = bytes([
                0xA0, 0x01,
                iv_index & 0xFF, (iv_index >> 8) & 0xFF,
                (iv_index >> 16) & 0xFF, (iv_index >> 24) & 0xFF,
                flag,
            ])

            ack_expected[0] = 0x80
            event.clear()
            await client.write_gatt_char(char_uuid, cmd1, response=False)

            try:
                await asyncio.wait_for(event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                return False, "No response from device (set network info timeout)"

            ack = ack_data[0]
            if ack is None or len(ack) < 2 or ack[1] != 0:
                status = ack[1] if ack and len(ack) >= 2 else -1
                return False, f"Set network info failed (status={status})"

            _LOGGER.info("Network info set successfully on %s", device_address)

            # --- Step 2: Provision the device ---
            cmd2 = bytearray()
            cmd2.append(0xA1)
            cmd2.extend(net_key)
            cmd2.append(node_address & 0xFF)
            cmd2.append((node_address >> 8) & 0xFF)

            ack_expected[0] = 0x81
            event.clear()
            await client.write_gatt_char(char_uuid, bytes(cmd2), response=False)

            try:
                await asyncio.wait_for(event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                return False, "No ACK for provision command (timeout)"

            ack = ack_data[0]
            if ack is None or len(ack) < 4:
                return False, "Provision ACK too short"
            if ack[3] != 0:
                return False, f"Provision failed (status={ack[3]})"

            _LOGGER.info(
                "Device %s successfully provisioned at 0x%04X",
                device_address, node_address,
            )

            # Device will restart as a mesh node after provisioning
            return True, f"Provisioned at 0x{node_address:04X}"

    except Exception as e:
        _LOGGER.error("Provisioning error for %s: %s", device_address, e)
        return False, str(e)


class BLEHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BLEHome."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_devices: dict[str, Any] = {}
        self._selected_device: Any = None
        self._device_services: dict[str, list[dict[str, str]]] = {}
        self._device_info: dict[str, dict[str, bool]] = {}
        # Provisioning state
        self._provision_device: Any = None
        self._provision_net_key: bytes = b""
        self._provision_address: int = 0

    async def async_step_bluetooth(
        self, discovery_info: Any
    ) -> FlowResult:
        """Handle Bluetooth-discovered WCH device.

        Only unprovisioned devices are handled here (auto → provisioning).
        Already-provisioned devices should use the scan flow instead.
        """
        address = discovery_info.address.upper()
        mac_suffix = address.replace(":", "")[-4:]

        # Override the discovered item title to include MAC suffix
        self.context["title_placeholders"] = {"name": f"BLEHome ({mac_suffix})"}

        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        # Only handle unprovisioned devices (mfr[0] == 0x00)
        mfr = discovery_info.manufacturer_data.get(2007, b"")
        if len(mfr) >= 1 and mfr[0] == 0x00:
            self._provision_device = discovery_info
            return await self.async_step_provision_configure()

        # Provisioned device: show the address so user can identify it
        return self.async_abort(
            reason="provisioned_device",
            description_placeholders={"address": address},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step — directly start scanning."""
        return await self.async_step_scan()

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Scan for BLEHome devices."""
        if user_input is not None:
            selected_mac = user_input["device"]
            self._selected_device = self._discovered_devices.get(selected_mac)
            if self._selected_device:
                # If device is unprovisioned, route to configure step instead
                dev_info = self._device_info.get(selected_mac, {})
                if dev_info.get("unprovisioned"):
                    self._provision_device = self._selected_device
                    return await self.async_step_provision_configure()
                return await self.async_step_select_service()

        self._discovered_devices = {}
        self._device_info = {}
        try:
            devices = await BleakScanner.discover(timeout=10.0, return_adv=True)

            device_options = {}
            for device, adv_data in devices.values():
                # 0x07D7 is WCH (BLEHome core manufacturer)
                if 2007 in adv_data.manufacturer_data:
                    mfr = adv_data.manufacturer_data[2007]
                    self._discovered_devices[device.address] = device
                    rssi = adv_data.rssi

                    # Check if provisioned or unprovisioned
                    # mfr[0]==0x00 = unprovisioned, mfr[0]==0x01 = provisioned
                    unprovisioned = len(mfr) >= 1 and mfr[0] == 0x00
                    self._device_info[device.address] = {
                        "unprovisioned": unprovisioned,
                    }

                    status = " [unprovisioned]" if unprovisioned else ""
                    label = f"{device.name or 'Unknown WCH'}{status} ({device.address}) [{rssi} dBm]"
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

    async def async_step_provision_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure provisioning parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            key_str = user_input["net_key"].replace(" ", "").replace("-", "")
            addr_str = user_input["node_address"].strip()

            # Validate: 16 bytes = 32 hex chars
            if len(key_str) != 32:
                errors["net_key"] = "invalid_key_length"
            else:
                try:
                    net_key = bytes.fromhex(key_str)
                except ValueError:
                    errors["net_key"] = "invalid_key_hex"

            # Parse address: support hex (0x64) and decimal (100)
            address = None
            try:
                if addr_str.lower().startswith("0x"):
                    address = int(addr_str, 16)
                else:
                    address = int(addr_str)
            except ValueError:
                errors["node_address"] = "invalid_address"

            if address is not None and (address < 1 or address > 0x7FFF):
                errors["node_address"] = "invalid_address"

            if not errors:
                self._provision_net_key = net_key
                self._provision_address = address
                return await self.async_step_provision_progress()

        return self.async_show_form(
            step_id="provision_configure",
            data_schema=vol.Schema({
                vol.Required("node_address", default="0x64"): str,
                vol.Required("net_key"): str,
            }),
            errors=errors,
            description_placeholders={
                "device": self._provision_device.address if self._provision_device else "?"
            },
        )

    async def async_step_provision_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Connect to the unprovisioned device and provision it."""
        device = self._provision_device
        if not device:
            return self.async_abort(reason="no_device")

        result = await _provision_device(
            device.address,
            DEFAULT_SERVICE_UUID,
            DEFAULT_CHAR_UUID,
            self._provision_net_key,
            self._provision_address,
        )

        if result[0]:
            return self.async_abort(
                reason="provision_success",
                description_placeholders={
                    "address": f"0x{self._provision_address:04X}",
                },
            )
        else:
            return self.async_abort(
                reason="provision_failed",
                description_placeholders={"error": result[1]},
            )

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return BLEHomeOptionsFlowHandler()


class BLEHomeOptionsFlowHandler(OptionsFlow):
    """Handle BLEHome options."""

    def _scan_firmware_files(self) -> list[dict[str, str]]:
        """Scan custom_components/blehome/bin/ for .bin/.hex files."""
        files = []
        bin_dir = self.hass.config.path("custom_components/blehome/bin")
        if os.path.isdir(bin_dir):
            for f in sorted(os.listdir(bin_dir)):
                if f.lower().endswith((".bin", ".hex")):
                    files.append({"value": os.path.join(bin_dir, f), "label": f})
        return files

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage options - firmware path and sub-devices."""
        if user_input is not None:
            new_options = dict(self.config_entry.options)

            # Save selected firmware file path
            fw_choice = user_input.get("firmware_choice", "")
            new_options[CONF_FIRMWARE_PATH] = fw_choice

            # Handle removed sub-devices
            removed = set(user_input.get("remove_subdevices", []))
            existing = new_options.get(CONF_SUBDEVICES, {})
            new_options[CONF_SUBDEVICES] = {
                addr: info
                for addr, info in existing.items()
                if addr not in removed
            }
            return self.async_create_entry(title="", data=new_options)

        subdevices = self.config_entry.options.get(CONF_SUBDEVICES, {})
        current_fw = self.config_entry.options.get(CONF_FIRMWARE_PATH, "")
        firmware_files = self._scan_firmware_files()

        schema: dict = {}

        # Show firmware file picker
        fw_options = []
        default_fw = "__none__"
        for f in firmware_files:
            fw_options.append({"value": f["value"], "label": f["label"]})
            if os.path.normpath(current_fw) == os.path.normpath(f["value"]):
                default_fw = f["value"]
        if fw_options:
            schema[vol.Required("firmware_choice", default=default_fw)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=fw_options),
            )

        # Sub-device removal
        choices = []
        for addr_str, info in subdevices.items():
            try:
                addr_int = int(addr_str)
                name = info.get("name", "Sub-device")
                choices.append({"value": addr_str, "label": f"{name} (0x{addr_int:04X})"})
            except (ValueError, TypeError):
                continue

        if choices:
            schema[vol.Optional("remove_subdevices", default=[])] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=choices, multiple=True),
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": str(len(choices))},
        )
