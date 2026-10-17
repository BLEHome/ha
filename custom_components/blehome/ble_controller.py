# blehome/ble_controller.py
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable, Coroutine
from typing import Any

from bleak import AdvertisementData, BleakClient, BleakError, BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)

try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None

# --- 核心修复：精准导入最新的蓝牙基类以通过类型检查 ---
try:
    # Home Assistant 2024.x/2025.x 强类型检查要求的基类
    from habluetooth import BaseHaScanner as BaseScanner
    from homeassistant.components.bluetooth import async_register_scanner
    HAS_BLUETOOTH_SCANNER = True
except ImportError:
    try:
        # 兼容旧版本
        from homeassistant.components.bluetooth import BaseScanner, async_register_scanner
        HAS_BLUETOOTH_SCANNER = True
    except ImportError:
        BaseScanner = object
        async_register_scanner = None
        HAS_BLUETOOTH_SCANNER = False

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BTHOME_PROXY_CMD,
    CMD_DELETE_NODE,
    CMD_DELETE_NODE_ACK,
    CONTROL_CMD,
    DOMAIN,
    FIRMWARE_BIN_DIR,
    FIRMWARE_MAX_FILE_SIZE,
    FIRMWARE_MAX_IMAGE_SIZE,
    HEADER,
    MESH_ADDR_MAX,
    MESH_ADDR_MIN,
    QUERY_CMD,
    STATE_REPORT_CMDS,
)

_LOGGER = logging.getLogger(__name__)

# Scanner cache cap; relayed BTHome devices beyond this evict the oldest entry
_SCANNER_CACHE_MAX = 512
_WRITE_TIMEOUT = 10.0


def async_get_registry_device(
    registry: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str | None
) -> dr.DeviceEntry | None:
    """Look up a device by identifier.

    async_get_device_by_identifier exists from HA 2026.8; older cores use async_get_device.
    """
    if config_entry_id and hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier(identifier, config_entry_id)
    return registry.async_get_device(identifiers={identifier})


def _parse_hex_file(filepath: str) -> tuple[bytes, int] | None:
    """Parse an Intel HEX file into contiguous bytes.

    Returns (data_bytes, min_address) or None on error.
    Handles record types 0x00, 0x01, 0x02, 0x04 and verifies record checksums.
    Raises ValueError on malformed records or an image larger than FIRMWARE_MAX_IMAGE_SIZE.
    """
    elements: list[tuple[int, bytes]] = []
    upper_addr = 0
    seg_base = 0
    address_mode = 0  # 0=16bit, 2=segment, 4=linear
    min_addr: int | None = None
    max_addr = 0

    try:
        with open(filepath) as f:
            lines = f.readlines()
    except OSError as e:
        _LOGGER.error("Cannot read HEX file %s: %s", filepath, e)
        return None

    for line in lines:
        line = line.strip()
        if not line or not line.startswith(':'):
            continue

        record = bytes.fromhex(line[1:])
        if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xFF:
            raise ValueError(f"Bad HEX record: {line[:16]}")

        byte_count = record[0]
        address = int(line[3:7], 16)
        record_type = int(line[7:9], 16)
        data_str = line[9:9 + byte_count * 2]

        if record_type == 0x00:  # Data
            data = bytes.fromhex(data_str)
            if len(data) != byte_count:
                continue
            if address_mode == 0x04:
                full_addr = (upper_addr << 16) | address
            elif address_mode == 0x02:
                full_addr = (seg_base << 4) + address
            else:
                full_addr = address
            if min_addr is None or full_addr < min_addr:
                min_addr = full_addr
            if full_addr + len(data) > max_addr:
                max_addr = full_addr + len(data)
            elements.append((full_addr, data))

        elif record_type == 0x01:  # EOF
            break
        elif record_type == 0x02:  # Segment
            if byte_count == 2:
                seg_base = int(data_str, 16)
                address_mode = 0x02
        elif record_type == 0x04:  # Linear
            if byte_count == 2:
                upper_addr = int(data_str, 16)
                address_mode = 0x04
        # 0x03, 0x05 ignored

    if not elements or min_addr is None:
        _LOGGER.error("No data records found in HEX file")
        return None

    buffer_size = max_addr - min_addr
    if buffer_size > FIRMWARE_MAX_IMAGE_SIZE:
        raise ValueError(f"HEX image spans {buffer_size} bytes, limit {FIRMWARE_MAX_IMAGE_SIZE}")
    buffer = bytearray(buffer_size)
    for addr, data in elements:
        offset = addr - min_addr
        buffer[offset:offset + len(data)] = data

    return bytes(buffer), min_addr

class BLEHomeScanner(BaseScanner):
    """Bluetooth scanner for BLEHome Proxy."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str | None,
        source: str,
        name: str,
        connector: Any,
        device_id: str | None = None
    ) -> None:
        """Initialize the scanner."""
        self.hass = hass
        self._source = source.upper()
        self._name = name
        self._connector = connector
        self._config_entry_id = entry_id
        self._device_id = device_id
        self._scanning = True
        self._discovered_devices: dict[str, tuple[Any, Any]] = {}
        self._callback: Callable[[BluetoothServiceInfoBleak], None] | None = None

        # 核心修复：根据 BaseScanner 实际定义的参数进行调用
        # BaseHaScanner 在新版本中通常只需要 source 和 name
        if HAS_BLUETOOTH_SCANNER and BaseScanner is not object:
            try:
                # 优先尝试新版 habluetooth 推荐的 (source, name) 方式
                super().__init__(self._source, self._name)
            except TypeError as e:
                _LOGGER.debug("Falling back to old BaseScanner signature: %s", e)
                try:
                    # 尝试旧版 (hass, source, name) 方式
                    super().__init__(hass, self._source, self._name)
                except TypeError:
                    # 如果还是不行，就尝试只传 source
                    super().__init__(self._source)

    def inject_proxy_advertisement(self, service_info: Any) -> None:
        """Inject a proxied advertisement into the manager."""
        # Re-insert to keep the dict ordered by last seen, then evict the oldest
        self._discovered_devices.pop(service_info.address, None)
        self._discovered_devices[service_info.address] = (
            service_info.device,
            service_info.advertisement,
        )
        while len(self._discovered_devices) > _SCANNER_CACHE_MAX:
            del self._discovered_devices[next(iter(self._discovered_devices))]
        _LOGGER.debug(
            "Proxy inject: address=%s service_uuids=%s service_data_keys=%s source=%s",
            service_info.address,
            service_info.service_uuids,
            list(service_info.service_data.keys()),
            service_info.source,
        )

        # 优先使用标准回调机制
        if hasattr(self, "async_on_advertisement"):
            self.async_on_advertisement(service_info)
        elif hasattr(self, "_async_on_advertisement"):
            # 某些旧版本 HA 可能使用带下划线的方法
            self._async_on_advertisement(service_info)
        elif self._callback:
            # 如果没有辅助方法但有回调，直接调用
            self._callback(service_info)
        else:
            _LOGGER.warning("No callback registered for scanner, dropping packet from %s", service_info.address)


    @property
    def source(self) -> str:
        """Return the source of the scanner."""
        return self._source

    @property
    def name(self) -> str:
        """Return the name of the scanner."""
        return self._name

    @property
    def config_entry_id(self) -> str | None:
        """Return the config entry ID."""
        return self._config_entry_id

    @property
    def device_id(self) -> str | None:
        """Return the device ID."""
        return self._device_id

    @property
    def scanning(self) -> bool:
        """Return if the scanner is scanning."""
        return self._scanning

    @property
    def adapter(self) -> str:
        """Return the adapter name."""
        return f"blehome_{self._source.replace(':', '').lower()}"

    @property
    def connector(self) -> Any:
        """Return the connector."""
        return self._connector

    @property
    def manufacturer(self) -> str:
        """Return the manufacturer."""
        return "BLEHome"

    @property
    def model(self) -> str:
        """Return the model."""
        return "Mesh Gateway Proxy"

    @property
    def discovered_addresses(self) -> list[str]:
        """Return a list of discovered addresses."""
        return list(self._discovered_devices.keys())

    @property
    def discovered_devices(self) -> list[Any]:
        """Return a list of discovered devices."""
        return [device for device, _ in self._discovered_devices.values()]

    @property
    def discovered_devices_and_advertisement_data(self) -> dict[str, tuple[Any, Any]]:
        """Return a dict of discovered devices and advertisement data."""
        return self._discovered_devices

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[Any, Any] | None:
        """Return the advertisement data for a discovered device."""
        return self._discovered_devices.get(address)

    async def async_poll(self, now: float) -> None:
        """Poll for new devices."""
        return

    def async_register_callback(
        self, callback: Callable[[BluetoothServiceInfoBleak], None]
    ) -> Callable[[], None]:
        """Register a callback."""
        self._callback = callback

        def _cancel() -> None:
            self._callback = None

        return _cancel

    def async_on_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Call the registered callback."""
        if self._callback:
            self._callback(service_info)


class BLEHomeController:
    """Control connection and communication with BLEHome devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_address: str,
        service_uuid: str,
        char_uuid: str
    ) -> None:
        """Initialize the controller."""
        self.hass = hass
        self.device_address = device_address.upper()
        self.mac_address = device_address.upper()
        self.service_uuid = service_uuid
        self.char_uuid = char_uuid
        self.notify_uuid: str | None = None
        self.name = ""
        self.client: BleakClient | None = None
        self.connected = False
        self.max_retries = 3
        self.base_timeout = 15.0
        self.subdevices: dict[int, dict[str, Any]] = {}
        self.gateway_address = 0x0001
        self.device_type = "blehome.tled"
        self.mac_suffix = ""
        self.config_entry: ConfigEntry | None = None
        self._connection_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._ota_lock = asyncio.Lock()
        self._closing = False  # set once by async_shutdown; stops all loops and reconnects
        self._disconnecting = False  # guard to prevent _on_disconnected from firing during cleanup/connect
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._scan_task: asyncio.Task | None = None
        self.keep_alive_interval = 30
        self._bthome_dedup: dict[str, int] = {}  # {mac: last_packet_hash}
        self._BTHOME_DEDUP_MAX = 100
        self.scanner: BLEHomeScanner | None = None
        self._unregister_scanner: Callable | None = None
        self._mock_packet_id = 0
        self._logged_bt_manager_methods = False
        self.bthome_mock_enabled = False
        # Generic command/ACK mechanism for provisioning, OTA, version queries
        self._cmd_lock = asyncio.Lock()
        self._cmd_event: asyncio.Event | None = None
        self._cmd_ack_prefix: bytes | None = None
        self._cmd_ack_data: bytes | None = None
        # Debug: track last notification for timeout diagnostics
        self._last_notification: bytes | None = None

    @property
    def _running(self) -> bool:
        """Return True while loops and reconnects may keep going."""
        return not self._closing and not self.hass.is_stopping

    def _create_task(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Start a background task that is cancelled when the entry unloads."""
        if self.config_entry:
            return self.config_entry.async_create_background_task(self.hass, coro, name)
        return self.hass.async_create_background_task(coro, name)

    def _abort_pending_command(self) -> None:
        """Wake a pending _send_and_wait so it returns None immediately."""
        if self._cmd_event is not None and not self._cmd_event.is_set():
            self._cmd_event.set()

    def _handle_link_lost(self) -> None:
        """Mark the link down, notify entities and start the reconnect loop."""
        if self._disconnecting:
            return
        was_connected = self.connected
        self.connected = False
        self._stop_heartbeat()
        self._abort_pending_command()
        if was_connected:
            self.hass.bus.async_fire(f"{DOMAIN}_availability_changed", {"connected": False})
        if self._running and (not self._reconnect_task or self._reconnect_task.done()):
            self._reconnect_task = self._create_task(
                self._persistent_reconnect(), f"blehome reconnect {self.device_address}"
            )

    async def async_shutdown(self) -> None:
        """Stop every loop and close the link. The controller is not reused afterwards."""
        self._closing = True
        self._abort_pending_command()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await asyncio.wait_for(self._reconnect_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._reconnect_task = None
        await self.disconnect()

    async def connect(
        self,
        timeout: float | None = None,
        retries: int | None = None
    ) -> bool:
        """Connect to the BLE device."""
        async with self._connection_lock:
            if self._closing:
                return False
            if self.connected and self.client and self.client.is_connected:
                return True

            conn_timeout = timeout or self.base_timeout
            conn_retries = retries or self.max_retries

            for attempt in range(conn_retries):
                try:
                    _LOGGER.info(
                        "Attempting to connect to %s (attempt %s/%s), timeout: %ss",
                        self.device_address, attempt + 1, conn_retries, conn_timeout
                    )

                    await self._cleanup_client()

                    ble_device = async_ble_device_from_address(
                        self.hass, self.device_address, connectable=True
                    )
                    if not ble_device:
                        # HA's bleak wrapper resolves addresses from the same cache, so a
                        # direct BleakClient(address) cannot succeed either; wait for an advert.
                        raise BleakError(
                            f"{self.device_address} not seen by any connectable Bluetooth adapter"
                        )

                    if establish_connection is not None:
                        self.client = await establish_connection(
                            BleakClient,
                            ble_device,
                            self.name or self.device_address,
                            disconnected_callback=self._on_disconnected,
                            max_attempts=2,
                            ble_device_callback=lambda: async_ble_device_from_address(
                                self.hass, self.device_address, connectable=True
                            ) or ble_device,
                        )
                    else:
                        self.client = BleakClient(ble_device)
                        await self.client.connect(timeout=conn_timeout)
                        self.client.set_disconnected_callback(self._on_disconnected)

                    # Double-check connection right after connect() returns
                    if not self.client.is_connected:
                        _LOGGER.warning("Connection lost immediately after connect to %s", self.device_address)
                        self.connected = False
                        continue

                    _LOGGER.info("Successfully connected to %s", self.device_address)

                    await self._setup_notifications()

                    # Re-check connection after notification setup
                    if not self.client.is_connected:
                        _LOGGER.warning("Connection lost during notification setup for %s", self.device_address)
                        self.connected = False
                        continue

                    self.connected = True
                    self._start_heartbeat()

                    # Query initial states and firmware versions without holding the lock
                    if self.subdevices:
                        self._create_task(
                            self._query_initial_state(), f"blehome initial query {self.device_address}"
                        )

                    # Start periodic mesh scan loop
                    if self._scan_task and not self._scan_task.done():
                        self._scan_task.cancel()
                    self._scan_task = self._create_task(
                        self._scan_loop(), f"blehome mesh scan {self.device_address}"
                    )

                    self.hass.bus.async_fire(
                        f"{DOMAIN}_availability_changed", {"connected": True}
                    )

                    # --- Bluetooth Proxy Registration ---
                    if HAS_BLUETOOTH_SCANNER and not self.scanner:
                        try:
                            source_mac = self.device_address.upper()
                            registry = dr.async_get(self.hass)

                            # Ensure the gateway device exists in registry
                            entry_id = self.config_entry.entry_id if self.config_entry else None

                            try:
                                device = registry.async_get_or_create(
                                    config_entry_id=entry_id,
                                    identifiers={(DOMAIN, self.device_address)},
                                    manufacturer="BLEHome",
                                    name=self.name or "BLEHome Mesh Gateway",
                                    model="Mesh Gateway Proxy",
                                )
                                dev_id = device.id
                            except Exception as dr_err:
                                _LOGGER.warning("Could not link device to registry (expected during reload): %s", dr_err)
                                dev_id = None

                            self.scanner = BLEHomeScanner(
                                self.hass,
                                entry_id,
                                source_mac,
                                f"BLEHome Mesh Proxy ({self.mac_suffix.upper()})",
                                self,
                                device_id=dev_id
                            )
                            self._unregister_scanner = async_register_scanner(self.hass, self.scanner)
                            _LOGGER.info("SUCCESS: Registered BLEHome Mesh Gateway [%s] as Bluetooth Proxy", source_mac)
                        except Exception as e:
                            _LOGGER.error("CRITICAL: Failed to register Bluetooth Proxy: %s", e, exc_info=True)
                    elif not HAS_BLUETOOTH_SCANNER:
                        _LOGGER.warning(
                            "Bluetooth proxy not registered: HAS_BLUETOOTH_SCANNER=False "
                            "(bluetooth integration or habluetooth missing)"
                        )

                    return True

                except (TimeoutError, BleakError, Exception) as e:
                    _LOGGER.debug(
                        "Connection attempt %s failed for %s: %s",
                        attempt + 1, self.device_address, str(e)
                    )
                    if attempt < conn_retries - 1:
                        wait_time = min(5 * (attempt + 1), 30)
                        await asyncio.sleep(wait_time)

            self.connected = False
            return False

    async def _query_initial_state(self) -> None:
        """Query each known sub-device for its state, then its firmware version."""
        for addr in list(self.subdevices):
            if not self.connected:
                return
            await self.send_query_command(addr)
            await asyncio.sleep(0.1)
        # Old firmware does not answer version queries; those devices are skipped
        await self._query_all_versions()

    async def _setup_notifications(self) -> None:
        """Set up GATT notifications."""
        if not self.client:
            return

        try:
            target_notify_uuid = None
            service = self.client.services.get_service(self.service_uuid)
            if service:
                for char in service.characteristics:
                    if "notify" in char.properties:
                        target_notify_uuid = char.uuid
                        if char.uuid == self.char_uuid:
                            break

            if not target_notify_uuid:
                char = self.client.services.get_characteristic(self.char_uuid)
                if char and "notify" in char.properties:
                    target_notify_uuid = self.char_uuid

            if target_notify_uuid:
                await self.client.start_notify(target_notify_uuid, self._notification_handler)
                self.notify_uuid = target_notify_uuid
                _LOGGER.info("Subscribed to notifications on %s", target_notify_uuid)
            else:
                _LOGGER.warning("No notification-capable characteristic found")

        except Exception as e:
            _LOGGER.warning("Failed to subscribe to notifications: %s", e)

    async def _cleanup_client(self) -> None:
        """Clean up client resources."""
        self._disconnecting = True
        self.connected = False
        self._stop_heartbeat()
        self._abort_pending_command()

        # Cancel scan task
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await asyncio.wait_for(self._scan_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._scan_task = None

        current_task = asyncio.current_task()
        if self._reconnect_task and self._reconnect_task != current_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await asyncio.wait_for(self._reconnect_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._reconnect_task = None

        if self._unregister_scanner:
            self._unregister_scanner()
            self._unregister_scanner = None
            self.scanner = None

        if self.client:
            try:
                # Stop notifications first, then disconnect
                if self.notify_uuid:
                    try:
                        await asyncio.wait_for(
                            self.client.stop_notify(self.notify_uuid), timeout=_WRITE_TIMEOUT
                        )
                    except Exception:
                        pass
                await asyncio.wait_for(self.client.disconnect(), timeout=_WRITE_TIMEOUT)
            except Exception as e:
                _LOGGER.debug("Error during client cleanup: %s", e)
            finally:
                self.client = None
                # Give BlueZ time to release the device resource
                await asyncio.sleep(0.3)

        self.connected = False
        self._disconnecting = False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        async with self._connection_lock:
            _LOGGER.info("Disconnecting from %s", self.device_address)
            await self._cleanup_client()

    def _on_disconnected(self, client: BleakClient) -> None:
        """Handle disconnection."""
        if self._disconnecting:
            _LOGGER.debug("Ignoring disconnect callback during intentional cleanup for %s", self.device_address)
            return
        if self.client is not None and client is not self.client:
            _LOGGER.debug("Ignoring late disconnect callback from a previous client for %s", self.device_address)
            return
        if self.connected:
            _LOGGER.info("Disconnected unexpectedly from %s", self.device_address)
            self._handle_link_lost()

    def _notification_handler(self, sender: int, data: bytearray) -> None:
        """Handle notification data."""
        # Track last notification for timeout diagnostics
        self._last_notification = bytes(data)

        # ACK matching for provisioning, OTA, version queries: command byte plus the
        # echoed node (and flash) address, so a late or foreign ACK is not taken
        if (self._cmd_event is not None
                and not self._cmd_event.is_set()
                and self._cmd_ack_prefix is not None
                and bytes(data[:len(self._cmd_ack_prefix)]) == self._cmd_ack_prefix):
            self._cmd_ack_data = bytes(data)
            self._cmd_event.set()
            return

        # Debug: log unmatched OTA-related ACKs that might indicate a routing issue
        if len(data) >= 1 and data[0] in (0x87, 0x88, 0x86):
            _LOGGER.debug("Unmatched OTA ACK: %s (waiting for %s)",
                          bytes(data[:6]).hex(),
                          self._cmd_ack_prefix.hex() if self._cmd_ack_prefix else "none")

        if len(data) < 7 or data[0] != HEADER:
            return

        address = data[1] + (data[2] << 8)
        cmd = (data[3] << 8) + data[4]

        if cmd == BTHOME_PROXY_CMD:
            self._handle_bthome_proxy_packet(data)
            return

        # Only state reports from unicast addresses create or update sub-devices
        if cmd not in STATE_REPORT_CMDS or not MESH_ADDR_MIN <= address <= MESH_ADDR_MAX:
            _LOGGER.debug("Ignoring frame cmd=0x%04X addr=0x%04X", cmd, address)
            return

        is_on = data[5] != 0
        # 逆 Gamma 2.2: 设备 duty(0-100) → HA brightness(0-255)
        duty = data[6]
        if duty <= 0:
            brightness = 0
        elif duty >= 100:
            brightness = 255
        else:
            brightness = int(round(255.0 * ((duty / 100.0) ** (1.0 / 2.2))))

        _LOGGER.debug("Notification received: Addr=0x%04X, On=%s, Bri=%s", address, is_on, brightness)

        if address not in self.subdevices:
            _LOGGER.info("Discovered new mesh device: 0x%04X", address)
            self.hass.async_create_task(
                self._async_add_discovered_subdevice(address, is_on, brightness)
            )
            return

        self.subdevices[address]["state"] = {"on": is_on, "brightness": brightness}
        self.hass.bus.async_fire(
            f"{DOMAIN}_subdevice_updated",
            {
                "controller_mac": self.mac_address,
                "address": address,
                "state": self.subdevices[address]["state"],
            },
        )

    def _handle_bthome_proxy_packet(self, data: bytearray) -> None:
        """Process proxied BTHome advertisement data."""
        try:
            if len(data) < 13:
                _LOGGER.warning("Proxy packet too short: %d bytes", len(data))
                return

            rssi = data[5]
            if rssi > 127:
                rssi -= 256
            # A real RSSI is negative; clamp so a forged value cannot outrank other adapters
            rssi = max(-127, min(rssi, -1))

            mac_bytes = bytes(data[6:12])
            if mac_bytes in (b"\x00" * 6, b"\xff" * 6):
                _LOGGER.debug("Proxy packet with invalid MAC %s", mac_bytes.hex())
                return
            mac = ":".join(f"{b:02X}" for b in mac_bytes)
            adv_len = data[12]
            if adv_len == 0 or 13 + adv_len > len(data):
                _LOGGER.debug("Proxy packet length mismatch: adv_len=%d frame=%d", adv_len, len(data))
                return
            adv_payload = data[13:13+adv_len]

            payload_hash = hash(bytes(adv_payload))
            if self._bthome_dedup.get(mac) == payload_hash:
                return
            # Limit dedup cache size to prevent memory leaks
            if len(self._bthome_dedup) >= self._BTHOME_DEDUP_MAX:
                self._bthome_dedup.clear()
            self._bthome_dedup[mac] = payload_hash

            _LOGGER.debug("Proxying BTHome packet from %s, RSSI: %d, Data: %s", mac, rssi, adv_payload.hex())

            # BTHome integration discovery relies on Service Data UUID matching OR Service UUID matching.
            # BTHome V2 usually puts 0xFCD2 in Service Data AD Type.
            # We MUST ensure that the Service UUIDs list contains the BTHome UUID so the integration matcher sees it.

            device = BLEDevice(address=mac, name=None, details=None)

            advertisement = AdvertisementData(
                local_name=f"BTHome {mac.replace(':', '')[-4:]}",
                manufacturer_data={},
                service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": bytes(adv_payload)},
                service_uuids=["0000fcd2-0000-1000-8000-00805f9b34fb"],
                rssi=rssi,
                tx_power=-127,
                platform_data=(),
            )

            source = self.scanner.source if self.scanner else self.device_address.upper()
            service_info = BluetoothServiceInfoBleak(
                name=f"BTHome {mac.replace(':', '')[-4:]}",
                address=mac,
                rssi=rssi,
                tx_power=-127,
                manufacturer_data={},
                service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": bytes(adv_payload)},
                service_uuids=["0000fcd2-0000-1000-8000-00805f9b34fb"],
                source=source,
                device=device,
                advertisement=advertisement,
                connectable=False,
                time=self.hass.loop.time(),
            )

            self._inject_to_bluetooth_manager(service_info)

            if hasattr(self, "async_on_advertisement"):
                self.async_on_advertisement(service_info)
            elif hasattr(self, "_async_on_advertisement"):
                self._async_on_advertisement(service_info)
            elif self.scanner:
                if hasattr(self.scanner, "inject_proxy_advertisement"):
                    self.scanner.inject_proxy_advertisement(service_info)
                elif hasattr(self.scanner, "async_on_advertisement"):
                    self.scanner.async_on_advertisement(service_info)
                elif hasattr(self.scanner, "_async_on_advertisement"):
                    self.scanner._async_on_advertisement(service_info)
                elif getattr(self.scanner, "_callback", None):
                    self.scanner._callback(service_info)
                else:
                    _LOGGER.warning("Scanner exists but no callback found, dropping packet from %s", service_info.address)
            else:
                _LOGGER.warning("Scanner not initialized, dropping packet from %s", service_info.address)

        except Exception as e:
            _LOGGER.error("Error processing BTHome proxy packet: %s", e, exc_info=True)

    async def _async_add_discovered_subdevice(self, address: int, is_on: bool, brightness: int) -> None:
        """Add a discovered subdevice and update config."""
        if address in self.subdevices:
            return

        name = f"{self.device_type}.{self.mac_suffix}.light.{address:04x}"
        self.subdevices[address] = {
            "name": name,
            "state": {"on": is_on, "brightness": brightness}
        }

        self.hass.bus.async_fire(
            f"{DOMAIN}_new_subdevice_found",
            {
                "controller_mac": self.mac_address,
                "address": address,
                "name": name,
                "state": self.subdevices[address]["state"]
            }
        )

        if self.config_entry:
            new_options = dict(self.config_entry.options)
            subdevices_config = new_options.get("subdevices", {}).copy()
            subdevices_config[str(address)] = {
                "name": name,
                "state": self.subdevices[address]["state"]
            }
            new_options["subdevices"] = subdevices_config
            self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

        # Asynchronously query firmware version (old devices silently skip)
        ver = await self.async_query_version(address)
        if ver:
            self.subdevices[address]["version"] = ver
        else:
            self.subdevices[address].pop("version", None)
        self._update_subdevice_version_registry(address, ver)

    async def async_scan_mesh(self, scan_range: int = 16) -> None:
        """Scan the mesh network for devices."""
        _LOGGER.info("Scanning mesh network (first %s addresses)...", scan_range)
        for addr in range(1, scan_range + 1):
            if not self.connected:
                break
            if addr in self.subdevices:
                continue
            await self.send_query_command(addr)
            await asyncio.sleep(1.0)

    async def _scan_loop(self) -> None:
        """Periodically scan mesh for new devices, with backoff between passes."""
        try:
            # First scan after short delay
            await asyncio.sleep(5.0)
            _LOGGER.info("Starting periodic mesh scan loop")
            while self.connected and self._running:
                _LOGGER.debug("Scanning mesh for new devices...")
                await self.async_scan_mesh(20)
                # Wait 5 minutes before next scan
                for _ in range(300):
                    if not self.connected or not self._running:
                        return
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Mesh scan loop error")

    async def _persistent_reconnect(self) -> None:
        """Retry connection with exponential backoff."""
        attempt = 0
        await asyncio.sleep(5.0)

        while self._running:
            # Re-check the real link every pass: it can drop again while connect() is
            # still finishing, and that disconnect does not start a second reconnect task
            if self.connected and self.client and self.client.is_connected:
                _LOGGER.info("Persistent reconnect successful for %s", self.device_address)
                return

            attempt += 1
            timeout = min(self.base_timeout + (attempt * 5), 60.0)
            wait_time = min(2 ** attempt, 60)

            _LOGGER.info(
                "Persistent reconnect attempt %s for %s, timeout %ss, next in %ss",
                attempt, self.device_address, timeout, wait_time
            )

            if await self.connect(timeout=timeout, retries=1) and self.connected:
                continue

            await asyncio.sleep(wait_time)

    def _start_heartbeat(self) -> None:
        """Start heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        async def heartbeat_loop() -> None:
            while self.connected and self._running:
                try:
                    heartbeat_cmd = bytearray([HEADER, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                    await self.send_command(heartbeat_cmd)

                    if self.client and self.client.is_connected and self.bthome_mock_enabled:
                        self.debug_inject_mock_bthome(mac="AA:BB:CC:DD:EE:01")

                    # The gateway's RSSI comes from the adapter that last heard its advert
                    rssi = None
                    from homeassistant.components.bluetooth import async_last_service_info
                    service_info = async_last_service_info(self.hass, self.mac_address, connectable=True)
                    if service_info:
                        rssi = service_info.rssi

                    if rssi is not None:
                        _LOGGER.debug("Gateway RSSI updated: %s dBm", rssi)
                        self.hass.bus.async_fire(
                            f"{DOMAIN}_rssi_updated",
                            {"address": self.mac_address, "rssi": rssi}
                        )
                    else:
                        _LOGGER.debug("Gateway RSSI unavailable via all methods for %s", self.mac_address)
                except Exception as e:
                    _LOGGER.warning("Heartbeat failed for %s: %s", self.device_address, e)
                    self._handle_link_lost()
                    break

                await asyncio.sleep(self.keep_alive_interval)

        self._heartbeat_task = self._create_task(
            heartbeat_loop(), f"blehome heartbeat {self.device_address}"
        )

    def _inject_to_bluetooth_manager(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Directly inject advertisement into bluetooth manager."""
        try:
            manager = None
            # Try hass.data first (newer cores)
            data_manager = self.hass.data.get("bluetooth_manager")
            if data_manager is None:
                data_manager = self.hass.data.get("bluetooth")
            if isinstance(data_manager, dict):
                data_manager = data_manager.get("manager") or data_manager.get("bluetooth_manager")
            if data_manager is not None:
                manager = data_manager

            try:
                from homeassistant.components import bluetooth as bluetooth_comp
                if hasattr(bluetooth_comp, "async_get_manager"):
                    manager = bluetooth_comp.async_get_manager(self.hass)
            except Exception:
                manager = None

            if manager is None:
                try:
                    from homeassistant.components.bluetooth import manager as bt_manager
                    if hasattr(bt_manager, "async_get_manager"):
                        manager = bt_manager.async_get_manager(self.hass)
                except Exception:
                    manager = None

            if not manager:
                _LOGGER.info("Bluetooth manager not available for injection")
                return

            if not self._logged_bt_manager_methods:
                method_names = [
                    name for name in dir(manager)
                    if "advert" in name.lower() or "service" in name.lower()
                ]
                _LOGGER.debug(
                    "Bluetooth manager type=%s methods=%s",
                    type(manager).__name__,
                    method_names,
                )
                self._logged_bt_manager_methods = True

            for method_name, label in (
                ("async_on_advertisement", "Injected advertisement"),
                ("async_process_service_info", "Injected service_info"),
                ("_discover_service_info", "Injected via _discover_service_info"),
                ("async_discovered_service_info", "Injected via async_discovered_service_info"),
            ):
                method = getattr(manager, method_name, None)
                if not callable(method):
                    continue

                result = method(service_info)
                if asyncio.iscoroutine(result):
                    self.hass.async_create_task(result)
                _LOGGER.debug(
                    "%s into bluetooth manager: %s (%s) service_data=%s",
                    label,
                    service_info.address,
                    service_info.source,
                    list(service_info.service_data.keys()),
                )
                return

            _LOGGER.warning("Bluetooth manager has no injection method")
        except Exception as e:
            _LOGGER.warning("Bluetooth manager injection failed: %s", e)

    def debug_dump_bluetooth_state(self) -> None:
        """Log internal bluetooth/proxy state for debugging."""
        client_connected = bool(self.client and self.client.is_connected)
        scanner_has_cb = bool(getattr(self.scanner, "_callback", None)) if self.scanner else False
        _LOGGER.info(
            "BLEHome debug state: mac=%s connected=%s client_connected=%s "
            "HAS_BLUETOOTH_SCANNER=%s scanner=%s scanner_callback=%s",
            self.mac_address,
            self.connected,
            client_connected,
            HAS_BLUETOOTH_SCANNER,
            bool(self.scanner),
            scanner_has_cb,
        )

    def debug_inject_mock_bthome(self, mac: str | None = None, temp_c: float | None = None) -> None:
        """Inject a mock BTHome advertisement for debugging."""
        import random

        if mac:
            mac_bytes = bytes(int(b, 16) for b in mac.split(":"))
            if len(mac_bytes) != 6:
                raise ValueError("Invalid MAC length for debug injection")
        else:
            mac_bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, random.randint(0, 255)])

        if temp_c is None:
            temp_val = random.randint(2000, 3000)
        else:
            temp_val = int(round(temp_c * 100))

        self._mock_packet_id = (self._mock_packet_id + 1) % 256

        payload = bytes([
            0x40,  # Device Info: No encryption, BTHome v2
            0x00,  # Packet ID type
            self._mock_packet_id,
            0x02,  # Temperature type
            temp_val & 0xFF, (temp_val >> 8) & 0xFF
        ])

        mock_pkg = bytearray([HEADER, 0x00, 0x00, (BTHOME_PROXY_CMD >> 8) & 0xFF, BTHOME_PROXY_CMD & 0xFF, 0xE2])
        mock_pkg.extend(mac_bytes)
        mock_pkg.append(len(payload))
        mock_pkg.extend(payload)

        _LOGGER.info(
            "DEBUG: Manual mock BTHome injection (mac=%s temp=%sC packet_id=%s)",
            ":".join(f"{b:02X}" for b in mac_bytes),
            temp_val / 100.0,
            self._mock_packet_id,
        )
        self._handle_bthome_proxy_packet(mock_pkg)

    def _stop_heartbeat(self) -> None:
        """Stop heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def send_command(self, command: bytes) -> bool:
        """Send a command to the device.

        Fails fast while the link is down instead of waiting for a reconnect to finish.
        """
        if not self.connected:
            return False
        async with self._write_lock:
            client = self.client
            if not self.connected or not client or not client.is_connected:
                return False

            try:
                await asyncio.wait_for(
                    client.write_gatt_char(self.char_uuid, command, response=False),
                    timeout=_WRITE_TIMEOUT,
                )
                _LOGGER.debug("Command sent to %s: %s", self.device_address, command.hex())
                return True
            except Exception as e:
                _LOGGER.error("Error sending command to %s: %s", self.device_address, e)
                # A failed write on a client that a reconnect already replaced says nothing about the new link
                if client is self.client:
                    self._handle_link_lost()
                return False

    async def send_query_command(self, address: int) -> bool:
        """Send a status query command."""
        cmd_frame = bytearray([
            HEADER,
            address & 0xFF,
            (address >> 8) & 0xFF,
            (QUERY_CMD >> 8) & 0xFF,
            QUERY_CMD & 0xFF,
            0x00,
            0x00,
        ])
        return await self.send_command(cmd_frame)

    @staticmethod
    def _gamma_correct(brightness: int) -> int:
        """Gamma 2.2 校正: HA brightness(0-255) → 设备 duty(0-100)"""
        if brightness <= 0:
            return 0
        if brightness >= 255:
            return 100
        fraction = brightness / 255.0
        duty = int(round(100.0 * (fraction ** 2.2)))
        # The lowest brightness levels round to duty 0, which would leave a light "on" but dark
        return max(1, min(100, duty))

    async def send_control_command(self, address: int, is_on: bool, brightness: int) -> bool:
        """Send a light control command."""
        normalized_brightness = self._gamma_correct(brightness) if brightness else 0
        cmd_frame = bytearray([
            HEADER,
            address & 0xFF,
            (address >> 8) & 0xFF,
            (CONTROL_CMD >> 8) & 0xFF,
            CONTROL_CMD & 0xFF,
            0x01 if is_on else 0x00,
            normalized_brightness,
        ])

        success = await self.send_command(cmd_frame)
        if success and address in self.subdevices:
            self.subdevices[address]["state"] = {
                "on": is_on,
                "brightness": brightness if brightness else 0  # 保持 HA 0-255 尺度
            }
            self.hass.bus.async_fire(
                f"{DOMAIN}_subdevice_updated",
                {
                    "controller_mac": self.mac_address,
                    "address": address,
                    "state": self.subdevices[address]["state"],
                },
            )
        return success

    async def _send_and_wait(self, cmd_bytes: bytes, ack_prefix: bytes, timeout: float = 30.0) -> bytes | None:
        """Send command bytes and wait for an ACK starting with ack_prefix.

        Uses _cmd_lock to serialize command/ACK exchanges.
        Returns raw ACK bytes on success, None on timeout, send failure or disconnect.
        """
        async with self._cmd_lock:
            self._cmd_event = asyncio.Event()
            self._cmd_ack_prefix = ack_prefix
            self._cmd_ack_data = None

            try:
                if not await self.send_command(cmd_bytes):
                    return None
                await asyncio.wait_for(self._cmd_event.wait(), timeout=timeout)
                return self._cmd_ack_data
            except TimeoutError:
                return None
            finally:
                self._cmd_event = None
                self._cmd_ack_prefix = None
                self._cmd_ack_data = None

    async def async_query_version(self, address: int) -> tuple[int, int] | None:
        """Query firmware version from a mesh sub-device.

        Returns (major, minor) or None if device doesn't respond (old firmware).
        """
        cmd = bytes([0xAB, address & 0xFF, (address >> 8) & 0xFF])
        ack = await self._send_and_wait(cmd, bytes([0x8B, address & 0xFF, (address >> 8) & 0xFF]), timeout=8.0)
        if ack is None or len(ack) < 5 or (len(ack) >= 6 and ack[5] != 0):
            return None
        return (ack[3], ack[4])

    async def async_delete_node(self, address: int) -> bool:
        """Delete/unprovision a mesh sub-device through the gateway.

        Sends CMD_DELETE_NODE (0xA2) and waits for ACK (0x82).
        Returns True if the device acknowledged, False on timeout/disconnect.
        """
        cmd = bytes([CMD_DELETE_NODE, address & 0xFF, (address >> 8) & 0xFF])
        ack = await self._send_and_wait(cmd, bytes([CMD_DELETE_NODE_ACK]), timeout=8.0)
        if ack is None:
            _LOGGER.warning("Delete node 0x%04X: no ACK (device may be offline)", address)
            return False
        _LOGGER.info("Delete node 0x%04X: acknowledged", address)
        return True

    async def _query_all_versions(self) -> None:
        """Query firmware versions for all known sub-devices."""
        for addr in list(self.subdevices.keys()):
            if not self.connected:
                break
            ver = await self.async_query_version(addr)
            if ver:
                self.subdevices[addr]["version"] = ver
            else:
                self.subdevices[addr].pop("version", None)
            self._update_subdevice_version_registry(addr, ver)
            await asyncio.sleep(0.5)

    def _update_subdevice_version_registry(self, address: int, version: tuple[int, int] | None) -> None:
        """Update or clear device registry sw_version for a sub-device."""
        dr_registry = dr.async_get(self.hass)
        device_id = (DOMAIN, f"{self.mac_address}_{address:04X}")
        device = async_get_registry_device(
            dr_registry, device_id, self.config_entry.entry_id if self.config_entry else None
        )
        if device:
            sw_ver = f"v{version[0]}.{version[1]}" if version else None
            dr_registry.async_update_device(device.id, sw_version=sw_ver)

    # --- Provisioning ---

    # --- OTA Update ---

    @staticmethod
    def _parse_ota_firmware(filepath: str) -> tuple[bytes, int] | None:
        """Parse .bin or .hex firmware file.

        Returns (firmware_bytes, start_address) or None on error.
        For .bin: start_address = 0x1000
        For .hex: start_address = minimum address in file
        """
        try:
            name = os.path.basename(filepath).lower()
            size = os.path.getsize(filepath)
            limit = FIRMWARE_MAX_IMAGE_SIZE if name.endswith('.bin') else FIRMWARE_MAX_FILE_SIZE
            if size > limit:
                raise ValueError(f"file is {size} bytes, limit {limit}")
            if name.endswith('.bin'):
                with open(filepath, 'rb') as f:
                    data = f.read()
                return data, 0x1000
            elif name.endswith('.hex'):
                return _parse_hex_file(filepath)
            else:
                _LOGGER.error("Unsupported firmware file: %s (use .bin or .hex)", filepath)
                return None
        except (OSError, ValueError) as e:
            _LOGGER.error("Failed to parse firmware file %s: %s", filepath, e)
            return None

    async def async_ota_query_image_info(self, node_address: int) -> dict[str, Any] | None:
        """Query OTA image info from a node.

        Sends CMD_IMAGE_INFO (0xA6) + address.
        ACK: [0x86, addrL, addrH, imageSize(4), blockSize(2), chipType(2), status] = 12 bytes
        Returns dict with 'image_size', 'block_size', 'chip_type', 'status_code', or None.
        """
        cmd = bytes([0xA6, node_address & 0xFF, (node_address >> 8) & 0xFF])
        ack = await self._send_and_wait(cmd, bytes([0x86]) + cmd[1:3], timeout=15.0)
        if ack is None or len(ack) < 12:
            return None
        if ack[11] != 0:
            _LOGGER.error("Image info query failed, status=%d", ack[11])
            return None
        image_size = ack[3] | (ack[4] << 8) | (ack[5] << 16) | (ack[6] << 24)
        block_size = ack[7] | (ack[8] << 8)
        chip_type = (ack[9] << 8) | ack[10]  # big-endian from ACK
        return {
            "image_size": image_size,
            "block_size": block_size,
            "chip_type": chip_type,
            "status_code": ack[11],
        }

    async def _download_firmware(self, url: str) -> str | None:
        """Download firmware from HTTP/HTTPS URL to a temporary file.

        Returns the temp file path, or None on failure.
        """
        import aiohttp

        _LOGGER.info("OTA: downloading firmware from %s", url)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    _LOGGER.error("OTA: HTTP %d downloading %s", resp.status, url)
                    return None
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > FIRMWARE_MAX_FILE_SIZE:
                        _LOGGER.error("OTA: firmware download exceeds %d bytes", FIRMWARE_MAX_FILE_SIZE)
                        return None
        except (TimeoutError, aiohttp.ClientError) as e:
            _LOGGER.error("OTA: failed to download firmware: %s", e)
            return None

        # Preserve extension for parsing (e.g. .bin, .hex)
        ext = ".hex" if url.lower().split("?")[0].endswith(".hex") else ".bin"

        def _write() -> str:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(data)
                return tmp.name

        try:
            tmp_path = await self.hass.async_add_executor_job(_write)
        except OSError as e:
            _LOGGER.error("OTA: failed to store downloaded firmware: %s", e)
            return None
        _LOGGER.info("OTA: downloaded %d bytes to %s", len(data), tmp_path)
        return tmp_path

    def _resolve_local_firmware(self, firmware_path: str) -> str | None:
        """Resolve a local firmware path; only bundled files and allowlisted dirs are accepted.

        Blocking: call from an executor.
        """
        path = firmware_path
        if not os.path.isabs(path):
            path = os.path.join(self.hass.config.path(), path)
        path = os.path.realpath(path)
        bundled_dir = os.path.realpath(self.hass.config.path(FIRMWARE_BIN_DIR))
        in_bundle = os.path.commonpath([path, bundled_dir]) == bundled_dir
        if not in_bundle and not self.hass.config.is_allowed_path(path):
            _LOGGER.error(
                "OTA: %s is outside %s and not in allowlist_external_dirs", path, FIRMWARE_BIN_DIR
            )
            return None
        if not os.path.isfile(path):
            _LOGGER.error("OTA: firmware file not found: %s", path)
            return None
        return path

    async def async_ota_update_node(self, node_address: int, firmware_path: str) -> dict[str, Any]:
        """Perform a full OTA firmware update on a mesh node.

        Returns dict with 'success' (bool) and 'message' (str).
        Progress is reported via HA events and persistent notifications.
        """
        result = {"success": False, "message": ""}
        if not MESH_ADDR_MIN <= node_address <= MESH_ADDR_MAX:
            result["message"] = f"Invalid node address 0x{node_address:04X} (unicast 0x0001-0x7FFF only)"
            _LOGGER.error("OTA: %s", result["message"])
            return result
        if self._ota_lock.locked():
            result["message"] = "Another OTA update is running on this gateway"
            _LOGGER.error("OTA: %s", result["message"])
            return result

        async with self._ota_lock:
            return await self._ota_update_locked(node_address, firmware_path, result)

    async def _ota_update_locked(
        self, node_address: int, firmware_path: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one OTA update; the caller holds _ota_lock."""
        _notify_id = f"blehome_ota_{node_address:04X}"

        await self._ota_notify(node_address, 0, "准备中...", _notify_id)

        # 0. Download firmware from URL if needed
        cleanup_path = None
        if firmware_path.lower().startswith(("http://", "https://")):
            downloaded = await self._download_firmware(firmware_path)
            if downloaded is None:
                result["message"] = "Failed to download firmware from URL"
                await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
                return result
            actual_path = downloaded
            cleanup_path = downloaded
        else:
            actual_path = await self.hass.async_add_executor_job(
                self._resolve_local_firmware, firmware_path
            )
            if actual_path is None:
                result["message"] = f"Firmware file not allowed or not found: {firmware_path}"
                await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
                return result

        try:
            return await self._do_ota_update(node_address, actual_path, result, _notify_id)
        finally:
            if cleanup_path:
                try:
                    await self.hass.async_add_executor_job(os.unlink, cleanup_path)
                except OSError:
                    pass
            # Re-subscribe to notifications to clear any stale BLE buffer state
            # accumulated during hundreds of OTA chunks. This improves success
            # rate for subsequent commands without a full reconnect.
            _LOGGER.info("OTA complete, re-subscribing to BLE notifications")
            if self.client and self.client.is_connected:
                try:
                    if self.notify_uuid:
                        await self.client.stop_notify(self.notify_uuid)
                except Exception:
                    pass
                await self._setup_notifications()

    async def _ota_notify(
        self, node_address: int, progress: int, message: str,
        notify_id: str = "", progress_label: str = "OTA 升级"
    ) -> None:
        """Create or update a persistent notification for OTA progress."""
        if not notify_id:
            notify_id = f"blehome_ota_{node_address:04X}"
        title = f"BLEHome OTA — 0x{node_address:04X}"
        body = f"{progress_label}: {progress}%\n{message}"
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {"title": title, "message": body, "notification_id": notify_id},
                blocking=False,
            )
        except Exception:
            _LOGGER.warning("Failed to create OTA notification", exc_info=True)

    async def _do_ota_update(
        self, node_address: int, firmware_path: str, result: dict, _notify_id: str = ""
    ) -> dict:
        """Internal OTA logic, separated for temp file cleanup."""
        import time as _time
        _t_start = _time.monotonic()

        # 1. Parse firmware file (run in executor to avoid blocking event loop)
        parsed = await asyncio.to_thread(self._parse_ota_firmware, firmware_path)
        if parsed is None:
            result["message"] = f"Cannot parse firmware file: {firmware_path}"
            await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
            return result
        firmware_data, start_addr = parsed
        fw_name = os.path.basename(firmware_path)
        # Extract version from filename (e.g. "TLED_V3.3.hex" → "V3.3")
        fw_version = ""
        import re as _re
        _m = _re.search(r'_?(v?\d+[._]\d+)', fw_name, _re.I)
        if _m:
            fw_version = _m.group(1)
        total = len(firmware_data)
        _LOGGER.info("OTA: firmware size=%d bytes, start_addr=0x%04X, version=%s", total, start_addr, fw_version or "?")
        await self._ota_notify(node_address, 0,
            f"固件: {fw_name}\n版本: {fw_version or '?'}\n大小: {total} 字节\n状态: 解析完成", _notify_id)

        # 2. Query current image info
        image_info = await self.async_ota_query_image_info(node_address)
        if image_info is None:
            result["message"] = "Failed to query image info (no response or error)"
            await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
            return result
        _LOGGER.info("OTA: image_size=%d, block_size=%d, chip_type=0x%04X",
                     image_info["image_size"], image_info["block_size"], image_info["chip_type"])

        # Flash addresses go on the wire in 8-byte units as 16 bits
        if (start_addr + total) // 8 > 0xFFFF:
            result["message"] = f"Firmware spans beyond the 16-bit flash address range (start 0x{start_addr:X})"
            await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
            return result

        if image_info["image_size"] < total:
            result["message"] = (
                f"Firmware file too large ({total} bytes) "
                f"> image capacity ({image_info['image_size']} bytes)"
            )
            await self._ota_notify(node_address, 0, f"失败: {result['message']}", _notify_id)
            return result

        chip_name = f"0x{image_info['chip_type']:04X}"
        await self._ota_notify(node_address, 0,
            f"固件: {fw_name}\n版本: {fw_version or '?'}\n大小: {total} 字节\n"
            f"芯片: {chip_name}  容量: {image_info['image_size']} 字节\n"
            f"状态: 设备就绪，开始写入...", _notify_id)

        # 3. Determine packet parameters based on BLE MTU
        address_base = 8
        frame_head = 5

        # On BlueZ, client.mtu_size often returns 23 (default) even on modern
        # adapters because MTU negotiation happens during the HA-managed connection,
        # not on our BleakClient instance.  The original hardcoded value of 216
        # data bytes (MTU=247 equivalent) worked correctly — revert to that.
        mtu = 247
        _LOGGER.info("OTA: using MTU = %d", mtu)

        # Calculate max frame size: cap at 221, align data to 8-byte boundary
        frame_max = min(221, mtu - 3)
        align = (frame_max - frame_head) // address_base
        frame_max_len = align * address_base + frame_head
        max_data_len = frame_max_len - frame_head
        if max_data_len < 8:
            max_data_len = 8
        total_chunks = (total + max_data_len - 1) // max_data_len
        _LOGGER.info("OTA: frame_max_len=%d, max_data_len=%d, total_chunks=%d (MTU %d)",
                     frame_max_len, max_data_len, total_chunks, mtu)

        # 4. Send UPDATE chunks
        offset = 0
        progress_pct = 0
        chunk_index = 0
        _t_phase = _time.monotonic()
        while offset < total:
            if not self.connected:
                result["message"] = f"Connection lost at offset {offset}/{total}"
                await self._ota_notify(node_address, progress_pct, f"失败: 连接断开\n已写入: {offset}/{total} 字节", _notify_id)
                return result

            data_len = min(max_data_len, total - offset)
            flash_addr = start_addr // address_base + offset // address_base

            # Build UPDATE command: [0xA7, addrL, addrH, flashL, flashH, data...]
            cmd = bytearray()
            cmd.append(0xA7)
            cmd.append(node_address & 0xFF)
            cmd.append((node_address >> 8) & 0xFF)
            cmd.append(flash_addr & 0xFF)
            cmd.append((flash_addr >> 8) & 0xFF)
            cmd.extend(firmware_data[offset:offset + data_len])

            # Retry up to 5 times for transient failures (timeout or status!=0)
            ack = None
            for retry in range(5):
                if not self.connected:
                    break
                ack = await self._send_and_wait(bytes(cmd), bytes([0x87]) + bytes(cmd[1:5]), timeout=120.0)
                if ack is None:
                    _LOGGER.warning("OTA UPDATE timeout at offset %d (retry %d/5)", offset, retry + 1)
                    continue
                if len(ack) >= 6 and ack[5] == 0:
                    break  # success
                # status != 0 — may be corrupted mesh packet, retry
                _LOGGER.warning("OTA UPDATE retry at offset %d, status=%s (retry %d/5)",
                                offset, ack[5] if len(ack) >= 6 else "N/A", retry + 1)
            if ack is None and not self.connected:
                result["message"] = f"Connection lost at offset {offset}/{total}"
                await self._ota_notify(node_address, progress_pct, f"失败: 连接断开\n已写入: {offset}/{total} 字节", _notify_id)
                return result
            if ack is None:
                last = self._last_notification.hex() if self._last_notification else "none"
                result["message"] = f"OTA UPDATE timeout at offset {offset}/{total} (last_notify={last})"
                await self._ota_notify(node_address, progress_pct,
                    f"失败: 写入超时\n已写入: {offset}/{total} 字节\n"
                    f"块: {chunk_index}/{total_chunks}", _notify_id)
                return result
            if len(ack) < 6 or ack[5] != 0:
                status = ack[5] if len(ack) >= 6 else "N/A"
                flash_addr = start_addr // address_base + offset // address_base
                _LOGGER.error("OTA UPDATE fail at offset %d (flash_addr=0x%04X), status=%s",
                              offset, flash_addr, status)
                result["message"] = f"OTA UPDATE fail at offset {offset}/{total}, status={status}"
                await self._ota_notify(node_address, progress_pct,
                    f"失败: 写入错误 (status={status})\n"
                    f"已写入: {offset}/{total} 字节\n"
                    f"块: {chunk_index}/{total_chunks}", _notify_id)
                return result

            offset += data_len
            chunk_index += 1
            progress_pct = int(offset * 100 / total)
            self.hass.bus.async_fire(
                f"{DOMAIN}_ota_progress",
                {"node_address": node_address, "progress": progress_pct,
                 "offset": offset, "total": total, "phase": "update"}
            )
            # Update notification every 5% or every 50 chunks (whichever is more frequent)
            if progress_pct % 5 == 0 or offset == total or chunk_index % 50 == 0:
                _elapsed = _time.monotonic() - _t_start
                _speed = offset / _elapsed / 1024 if _elapsed > 0 else 0
                await self._ota_notify(node_address, progress_pct,
                    f"阶段: 写入中 ({chunk_index}/{total_chunks} 块)\n"
                    f"固件: {fw_name}\n"
                    f"进度: {offset}/{total} 字节\n"
                    f"速度: {_speed:.1f} KB/s\n"
                    f"耗时: {_elapsed:.0f} 秒", _notify_id)
            _LOGGER.debug("OTA update: %d/%d (%d%%) chunk %d/%d",
                          offset, total, progress_pct, chunk_index, total_chunks)

        _t_phase = _time.monotonic() - _t_phase
        _LOGGER.info("OTA: UPDATE phase complete in %.1fs", _t_phase)
        await self._ota_notify(node_address, 100,
            f"写入完成 ({total} 字节, {_t_phase:.0f} 秒)\n"
            f"固件: {fw_name}\n"
            f"阶段: 验证中...", _notify_id, progress_label="OTA 验证")

        # 5. Send VERIFY chunks
        _LOGGER.info("OTA: VERIFY phase")
        _t_phase = _time.monotonic()
        offset = 0
        chunk_index = 0
        verify_ok = True
        verify_mismatch = False
        while offset < total and verify_ok:
            if not self.connected:
                _LOGGER.warning("OTA: disconnected during verify, skipping remaining")
                verify_ok = False
                break

            data_len = min(max_data_len, total - offset)
            flash_addr = start_addr // address_base + offset // address_base

            cmd = bytearray()
            cmd.append(0xA8)
            cmd.append(node_address & 0xFF)
            cmd.append((node_address >> 8) & 0xFF)
            cmd.append(flash_addr & 0xFF)
            cmd.append((flash_addr >> 8) & 0xFF)
            cmd.extend(firmware_data[offset:offset + data_len])

            # Retry up to 5 times for transient failures (timeout or status!=0)
            ack = None
            for retry in range(5):
                if not self.connected:
                    break
                ack = await self._send_and_wait(bytes(cmd), bytes([0x88]) + bytes(cmd[1:5]), timeout=120.0)
                if ack is None:
                    _LOGGER.warning("OTA VERIFY timeout at offset %d (retry %d/5)", offset, retry + 1)
                    continue
                if len(ack) >= 6 and ack[5] == 0:
                    break
                _LOGGER.warning("OTA VERIFY retry at offset %d, status=%s (retry %d/5)",
                                offset, ack[5] if len(ack) >= 6 else "N/A", retry + 1)
            if ack is None:
                last = self._last_notification.hex() if self._last_notification else "none"
                _LOGGER.warning(
                    "OTA: VERIFY timeout at offset %d (last_notify=%s), skipping remaining",
                    offset, last
                )
                verify_ok = False
                break
            if len(ack) < 6 or ack[5] != 0:
                status = ack[5] if len(ack) >= 6 else "N/A"
                _LOGGER.error("OTA: VERIFY mismatch at offset %d, status=%s", offset, status)
                verify_ok = False
                verify_mismatch = True
                break

            offset += data_len
            chunk_index += 1
            progress_pct = int(offset * 100 / total)
            self.hass.bus.async_fire(
                f"{DOMAIN}_ota_progress",
                {"node_address": node_address, "progress": progress_pct,
                 "offset": offset, "total": total, "phase": "verify"}
            )
            if progress_pct % 10 == 0 or offset == total:
                _elapsed = _time.monotonic() - _t_start
                await self._ota_notify(node_address, progress_pct,
                    f"阶段: 验证中 ({chunk_index}/{total_chunks} 块)\n"
                    f"固件: {fw_name}\n"
                    f"进度: {offset}/{total} 字节\n"
                    f"总耗时: {_elapsed:.0f} 秒", _notify_id, progress_label="OTA 验证")
            _LOGGER.debug("OTA verify: %d/%d (%d%%)", offset, total, progress_pct)

        _t_verify = _time.monotonic() - _t_phase
        if verify_mismatch:
            # The node reported flash content that differs from the image: do not finalize it
            result["message"] = f"OTA VERIFY mismatch at offset {offset}/{total}; image not finalized"
            await self._ota_notify(node_address, progress_pct,
                f"失败: 校验不一致，未切换固件\n位置: {offset}/{total} 字节", _notify_id,
                progress_label="OTA 验证")
            return result
        if not self.connected:
            result["message"] = f"Connection lost during verify at offset {offset}/{total}"
            await self._ota_notify(node_address, progress_pct,
                f"失败: 验证时连接断开\n已验证: {offset}/{total} 字节", _notify_id,
                progress_label="OTA 验证")
            return result
        if verify_ok:
            _LOGGER.info("OTA: VERIFY phase complete in %.1fs", _t_verify)
        else:
            # Verify is best effort per protocol: a timeout leaves the per-chunk UPDATE ACKs as proof
            _LOGGER.warning("OTA: VERIFY timed out at offset %d; UPDATE chunks were confirmed, proceeding to END", offset)

        # 6. Send END command (desktop app uses 1.0s delay)
        await asyncio.sleep(1.0)
        end_cmd = bytes([0xA9, node_address & 0xFF, (node_address >> 8) & 0xFF])
        await self.send_command(end_cmd)

        self.hass.bus.async_fire(
            f"{DOMAIN}_ota_progress",
            {"node_address": node_address, "progress": 100,
             "offset": total, "total": total, "phase": "complete"}
        )

        _t_total = _time.monotonic() - _t_start
        result["success"] = True
        result["message"] = f"OTA update complete ({total} bytes in {_t_total:.0f}s)"
        if not verify_ok:
            result["message"] += "; verify incomplete"
        await self._ota_notify(node_address, 100,
            ("OTA 升级成功!\n" if verify_ok else "OTA 升级完成（验证未全部完成）\n")
            + f"固件: {fw_name}\n"
            f"版本: {fw_version or '?'}\n"
            f"大小: {total} 字节\n"
            f"耗时: {_t_total:.0f} 秒\n"
            f"速度: {total/_t_total/1024:.1f} KB/s", _notify_id)
        _LOGGER.info("OTA: update complete for 0x%04X in %.1fs", node_address, _t_total)

        return result

