# blehome/ble_controller.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bleak import BleakClient, BleakError
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    HEADER,
    CONTROL_CMD,
    QUERY_CMD,
    CONF_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)

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
        self.device_address = device_address
        self.mac_address = device_address
        self.service_uuid = service_uuid
        self.char_uuid = char_uuid
        self.notify_uuid: Optional[str] = None
        self.name = ""
        self.client: Optional[BleakClient] = None
        self.connected = False
        self.max_retries = 3
        self.base_timeout = 15.0
        self.subdevices: dict[int, dict[str, Any]] = {}
        self.gateway_address = 0x0001
        self.device_type = "blehome.tled"
        self.mac_suffix = ""
        self.config_entry: Optional[ConfigEntry] = None
        self._connection_lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self.keep_alive_interval = 30

    async def connect(
        self, 
        timeout: Optional[float] = None, 
        retries: Optional[int] = None
    ) -> bool:
        """Connect to the BLE device."""
        async with self._connection_lock:
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
                    if ble_device:
                        self.client = BleakClient(ble_device)
                        _LOGGER.debug("Connecting via HA bluetooth manager: %s", ble_device)
                    else:
                        self.client = BleakClient(self.device_address)
                        _LOGGER.warning(
                            "Device not in HA cache, attempting direct connection: %s", 
                            self.device_address
                        )
                    
                    await self.client.connect(timeout=conn_timeout)
                    
                    if self.client.is_connected:
                        self.connected = True
                        _LOGGER.info("Successfully connected to %s", self.device_address)
                        
                        await self._setup_notifications()
                        self._start_heartbeat()
                        self.client.set_disconnected_callback(self._on_disconnected)
                        
                        # Query initial states
                        for addr in self.subdevices:
                            self.hass.async_create_task(self.send_query_command(addr))
                            await asyncio.sleep(0.1)

                        # Trigger Mesh scan after delay
                        self.hass.loop.call_later(
                            3.0, 
                            lambda: self.hass.async_create_task(self.async_scan_mesh(20))
                        )

                        self.hass.bus.async_fire(
                            f"{DOMAIN}_availability_changed", {"connected": True}
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
        self._stop_heartbeat()
        current_task = asyncio.current_task()
        if self._reconnect_task and self._reconnect_task != current_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error during client cleanup: %s", e)
            finally:
                self.client = None
        
        self.connected = False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        async with self._connection_lock:
            _LOGGER.info("Disconnecting from %s", self.device_address)
            await self._cleanup_client()

    def _on_disconnected(self, client: BleakClient) -> None:
        """Handle disconnection."""
        if self.connected:
            _LOGGER.debug("Disconnected unexpectedly from %s", self.device_address)
            self.connected = False
            self.hass.bus.async_fire(f"{DOMAIN}_availability_changed", {"connected": False})
            self._stop_heartbeat()
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = self.hass.async_create_task(self._persistent_reconnect())

    def _notification_handler(self, sender: int, data: bytearray) -> None:
        """Handle notification data."""
        if len(data) < 7 or data[0] != HEADER:
            return
            
        address = data[1] + (data[2] << 8)
        is_on = data[5] != 0
        brightness = data[6]
        
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
            {"address": address, "state": self.subdevices[address]["state"]}
        )

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

    async def async_scan_mesh(self, scan_range: int = 16) -> None:
        """Scan the mesh network for devices."""
        _LOGGER.info("Scanning mesh network (first %s addresses)...", scan_range)
        for addr in range(1, scan_range + 1):
            if not self.connected:
                break
            if addr in self.subdevices:
                continue
            await self.send_query_command(addr)
            await asyncio.sleep(0.6)

    async def _persistent_reconnect(self) -> None:
        """Retry connection with exponential backoff."""
        attempt = 0
        await asyncio.sleep(5.0)
        
        while not self.connected and self.hass.is_running:
            attempt += 1
            timeout = min(self.base_timeout + (attempt * 5), 60.0)
            wait_time = min(2 ** attempt, 60)

            _LOGGER.info(
                "Persistent reconnect attempt %s for %s, timeout %ss, next in %ss",
                attempt, self.device_address, timeout, wait_time
            )

            if await self.connect(timeout=timeout, retries=1):
                _LOGGER.info("Persistent reconnect successful for %s", self.device_address)
                return

            await asyncio.sleep(wait_time)

    def _start_heartbeat(self) -> None:
        """Start heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        async def heartbeat_loop() -> None:
            while self.connected and self.hass.is_running:
                try:
                    heartbeat_cmd = bytearray([HEADER, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                    await self.send_command(heartbeat_cmd)

                    if self.client and self.client.is_connected:
                        try:
                            rssi = await self.client.get_rssi()
                            self.hass.bus.async_fire(
                                f"{DOMAIN}_rssi_updated",
                                {"address": self.mac_address, "rssi": rssi}
                            )
                        except Exception:
                            pass
                except Exception as e:
                    _LOGGER.warning("Heartbeat failed for %s: %s", self.device_address, e)
                    self.connected = False
                    self._stop_heartbeat()
                    if not self._reconnect_task or self._reconnect_task.done():
                        self._reconnect_task = self.hass.async_create_task(self._persistent_reconnect())
                    break

                await asyncio.sleep(self.keep_alive_interval)

        self._heartbeat_task = self.hass.async_create_task(heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        """Stop heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def send_command(self, command: bytes) -> bool:
        """Send a command to the device."""
        async with self._connection_lock:
            if not self.connected or not self.client or not self.client.is_connected:
                return False
            
            try:
                await self.client.write_gatt_char(self.char_uuid, command, response=False)
                _LOGGER.debug("Command sent to %s: %s", self.device_address, command.hex())
                return True
            except Exception as e:
                _LOGGER.error("Error sending command to %s: %s", self.device_address, e)
                self.connected = False
                self._stop_heartbeat()
                if not self._reconnect_task or self._reconnect_task.done():
                    self._reconnect_task = self.hass.async_create_task(self._persistent_reconnect())
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

    async def send_control_command(self, address: int, is_on: bool, brightness: int) -> bool:
        """Send a light control command."""
        normalized_brightness = brightness if brightness else 0
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
                "brightness": normalized_brightness
            }
            self.hass.bus.async_fire(
                f"{DOMAIN}_subdevice_updated",
                {"address": address, "state": self.subdevices[address]["state"]}
            )
        return success

    async def __aenter__(self) -> BLEHomeController:
        """Async context manager enter."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()