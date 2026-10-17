# blehome/config_flow.py
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import voluptuous as vol
from bleak import BleakClient, BleakError, BleakScanner
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CHAR_UUID,
    CONF_FIRMWARE_PATH,
    CONF_SERVICE_UUID,
    CONF_SUBDEVICES,
    DEFAULT_CHAR_UUID,
    DEFAULT_PREFIX,
    DEFAULT_SERVICE_UUID,
    DOMAIN,
    FIRMWARE_BIN_DIR,
    MESH_ADDR_MAX,
    MESH_ADDR_MIN,
    MESH_STORE_KEY,
    MESH_STORE_VERSION,
)
from .firmware import scan_firmware_files

_FIRMWARE_AUTO = "__auto__"
_NEW_MESH = "new_mesh"

_LOGGER = logging.getLogger(__name__)


try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None


@asynccontextmanager
async def _connect(hass: HomeAssistant, address: str) -> AsyncIterator[BleakClient]:
    """Connect through the adapter HA picks for the address, with retries.

    A bare BleakClient(address) tries one adapter once; a flaky proxy then fails the flow.
    """
    ble_device = async_ble_device_from_address(hass, address.upper(), connectable=True)
    if ble_device is None:
        raise BleakError(f"{address} is not seen by any connectable Bluetooth adapter")
    if establish_connection is not None:
        client = await establish_connection(BleakClient, ble_device, address, max_attempts=3)
    else:
        client = BleakClient(ble_device)
        await client.connect(timeout=15.0)
    try:
        yield client
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _provision_device(
    hass: HomeAssistant,
    device_address: str,
    service_uuid: str,
    char_uuid: str,
    net_key: bytes,
    node_address: int,
) -> tuple[bool, str]:
    """Connect to an unprovisioned WCH BLE device and provision it.

    Connects through HA's Bluetooth stack so the best connectable adapter is used.
    Returns (success, message).
    """
    if len(net_key) != 16:
        return False, "Network key must be exactly 16 bytes"

    try:
        success = False
        message = ""
        async with _connect(hass, device_address) as client:
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
            except TimeoutError:
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
            except TimeoutError:
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

            success = True
            message = f"Provisioned at 0x{node_address:04X}"

        # Brief pause after disconnect to let BlueZ release the device
        await asyncio.sleep(0.5)
        return success, message

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
        # Gateway entry whose mesh the device joins, or _NEW_MESH; None until chosen
        self._provision_mesh: str | None = None

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
        if user_input is not None and user_input.get("device"):
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
            )

        except Exception as e:
            _LOGGER.error("Error scanning for BLEHome devices: %s", e)
            return self.async_show_form(step_id="scan", errors={"base": "scan_failed"})

    def _mesh_store(self) -> Store:
        return Store(self.hass, MESH_STORE_VERSION, MESH_STORE_KEY)

    def _mesh_entry(self) -> ConfigEntry | None:
        """Return the gateway entry of the chosen mesh, or None for a new mesh."""
        if self._provision_mesh in (None, _NEW_MESH):
            return None
        return self.hass.config_entries.async_get_entry(self._provision_mesh)

    def _next_node_address(self) -> int | None:
        """Return one above the highest address in the chosen mesh, starting at 0x0001.

        Gaps are not reused: a gap may belong to a node that is offline or unknown to HA.
        """
        known = [MESH_ADDR_MIN - 1]
        entry = self._mesh_entry()
        if entry:
            for addr in entry.options.get(CONF_SUBDEVICES, {}):
                try:
                    known.append(int(addr))
                except (TypeError, ValueError):
                    continue
        candidate = max(known) + 1
        return candidate if candidate <= MESH_ADDR_MAX else None

    async def _default_net_key(self) -> str | None:
        """Return the chosen mesh's remembered key, a fresh one for a new mesh, or None.

        An existing mesh without a remembered key needs its key entered once: a random key
        would put the device in a different mesh.
        """
        entry = self._mesh_entry()
        if entry is None:
            return secrets.token_hex(16)
        stored = await self._mesh_store().async_load() or {}
        return stored.get("keys", {}).get(entry.data[CONF_MAC].upper())

    async def _remember_net_key(self) -> None:
        """Store the key under the gateway MAC of its mesh."""
        entry = self._mesh_entry()
        gateway_mac = entry.data[CONF_MAC] if entry else self._provision_device.address
        store = self._mesh_store()
        stored = await store.async_load() or {}
        keys = dict(stored.get("keys", {}))
        keys[gateway_mac.upper()] = self._provision_net_key.hex()
        await store.async_save({"keys": keys})

    async def async_step_provision_mesh(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose the mesh the device joins: an existing gateway's mesh or a new one."""
        if user_input is not None:
            self._provision_mesh = user_input["mesh"]
            return await self.async_step_provision_configure()

        options = [
            {"value": entry.entry_id, "label": entry.title}
            for entry in self._async_current_entries()
        ]
        options.append({"value": _NEW_MESH, "label": "New mesh"})
        return self.async_show_form(
            step_id="provision_mesh",
            data_schema=vol.Schema({
                vol.Required("mesh", default=options[0]["value"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="mesh",
                    )
                ),
            }),
            description_placeholders={
                "device": self._provision_device.address if self._provision_device else "?"
            },
        )

    async def async_step_provision_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure provisioning parameters."""
        errors: dict[str, str] = {}

        if self._provision_mesh is None:
            if self._async_current_entries():
                return await self.async_step_provision_mesh()
            self._provision_mesh = _NEW_MESH

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

        if user_input is not None:
            default_address = user_input["node_address"]
            default_key = user_input["net_key"]
        else:
            next_address = self._next_node_address()
            default_address = f"0x{next_address:04X}" if next_address else ""
            default_key = await self._default_net_key()

        address_field = (
            vol.Required("node_address", default=default_address)
            if default_address else vol.Required("node_address")
        )
        key_field = (
            vol.Required("net_key", default=default_key)
            if default_key else vol.Required("net_key")
        )
        return self.async_show_form(
            step_id="provision_configure",
            data_schema=vol.Schema({
                address_field: str,
                key_field: selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
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
            self.hass,
            device.address,
            DEFAULT_SERVICE_UUID,
            DEFAULT_CHAR_UUID,
            self._provision_net_key,
            self._provision_address,
        )

        if result[0]:
            await self._remember_net_key()
            # Joined an existing mesh: its gateway discovers the device on its own
            if self._mesh_entry() is not None:
                return self.async_abort(
                    reason="provision_success",
                    description_placeholders={
                        "address": f"0x{self._provision_address:04X}",
                    },
                )
            # New mesh: this device becomes its gateway, route to gateway setup
            self._selected_device = getattr(device, "device", device)
            await asyncio.sleep(8)  # Wait for device to reboot and start advertising
            return await self.async_step_select_service()
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
            name_parts = (self._selected_device.name or "").split()
            if name_parts:
                prefix = name_parts[0].split('_')[0] or DEFAULT_PREFIX

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
            address = getattr(self._selected_device, "address", self._selected_device)
            async with _connect(self.hass, address) as client:
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

    async def _scan_firmware_files(self) -> list[dict[str, str]]:
        """Scan custom_components/blehome/bin/ for .bin/.hex files, oldest version first."""
        bin_dir = self.hass.config.path(FIRMWARE_BIN_DIR)
        paths = await self.hass.async_add_executor_job(scan_firmware_files, bin_dir)
        return [{"value": p, "label": os.path.relpath(p, bin_dir)} for p in paths]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage options - firmware path and sub-devices."""
        if user_input is not None:
            new_options = dict(self.config_entry.options)

            # Save selected firmware file path; "auto" stores nothing so OTA uses the latest bundled file
            fw_choice = user_input.get("firmware_choice", _FIRMWARE_AUTO)
            new_options[CONF_FIRMWARE_PATH] = "" if fw_choice == _FIRMWARE_AUTO else fw_choice

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
        firmware_files = await self._scan_firmware_files()

        schema: dict = {}

        # Show firmware file picker; "auto" follows the newest bundled file across updates
        fw_options = []
        default_fw = _FIRMWARE_AUTO
        if firmware_files:
            fw_options.append({
                "value": _FIRMWARE_AUTO,
                "label": f"Auto ({firmware_files[-1]['label']})",
            })
        for f in firmware_files:
            fw_options.append({"value": f["value"], "label": f["label"]})
            if current_fw and os.path.normpath(current_fw) == os.path.normpath(f["value"]):
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
        )
