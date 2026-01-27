# blehome/ble_controller.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Callable

from bleak import BleakClient, BleakError, BLEDevice, AdvertisementData
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    BluetoothServiceInfoBleak,
)

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
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    HEADER,
    CONTROL_CMD,
    QUERY_CMD,
    BTHOME_PROXY_CMD,
    CONF_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)

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
        self._discovered_devices[service_info.address] = (
            service_info.device,
            service_info.advertisement,
        )
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
        self._scan_task: Optional[asyncio.Task] = None
        self.keep_alive_interval = 30
        self._bthome_dedup: dict[str, int] = {}  # {mac: last_packet_id}
        self.scanner: Optional[BLEHomeScanner] = None
        self._unregister_scanner: Optional[Callable] = None
        self._mock_packet_id = 0
        self._logged_bt_manager_methods = False
        self.bthome_mock_enabled = False

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
                        if self._scan_task and not self._scan_task.done():
                            self._scan_task.cancel()
                        
                        def start_scan():
                            self._scan_task = self.hass.async_create_task(self.async_scan_mesh(20))

                        self.hass.loop.call_later(10.0, start_scan)

                        self.hass.bus.async_fire(
                            f"{DOMAIN}_availability_changed", {"connected": True}
                        )

                        # --- Bluetooth Proxy Registration ---
                        if HAS_BLUETOOTH_SCANNER and not self.scanner:
                            try:
                                source_mac = self.device_address.upper()
                                from homeassistant.helpers import device_registry as dr
                                registry = dr.async_get(self.hass)
                                
                                # Ensure the gateway device exists in registry
                                entry_id = self.config_entry.entry_id if self.config_entry else None

                                try:
                                    device = registry.async_get_or_create(
                                        config_entry_id=entry_id,
                                        identifiers={(DOMAIN, self.device_address)},
                                        manufacturer="BLEHome",
                                        name=self.name or f"BLEHome Mesh Gateway",
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
        
        # 强制停止扫描任务
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            self._scan_task = None

        current_task = asyncio.current_task()
        if self._reconnect_task and self._reconnect_task != current_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._unregister_scanner:
            self._unregister_scanner()
            self._unregister_scanner = None
            self.scanner = None

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
        cmd = (data[3] << 8) + data[4]
        
        if cmd == BTHOME_PROXY_CMD:
            self._handle_bthome_proxy_packet(data)
            return

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

    def _handle_bthome_proxy_packet(self, data: bytearray) -> None:
        """Process proxied BTHome advertisement data."""
        try:
            if len(data) < 13:
                _LOGGER.warning("Proxy packet too short: %d bytes", len(data))
                return

            rssi = data[5]
            if rssi > 127:
                rssi -= 256
                
            mac_bytes = data[6:12]
            mac = ":".join(f"{b:02X}" for b in mac_bytes)
            adv_len = data[12]
            adv_payload = data[13:13+adv_len]

            payload_hash = hash(bytes(adv_payload))
            if self._bthome_dedup.get(mac) == payload_hash:
                return
            self._bthome_dedup[mac] = payload_hash

            _LOGGER.info("Proxying BTHome packet from %s, RSSI: %d, Data: %s", mac, rssi, adv_payload.hex())

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

                    if self.client and self.client.is_connected and self.bthome_mock_enabled:
                        # --- 调试：模拟 BTHome 广播注入 ---
                        # 模拟一个 MAC 为 AA:BB:CC:DD:EE:01 的设备，上报温度 25.00 C
                        # BTHome V2 格式
                        # Byte 0: Device Info (0x40 = No encryption, V2)
                        # Byte 1: Packet ID (0x00)
                        # Byte 2: Counter value
                        # Byte 3: Object ID (0x02 = Temperature)
                        # Byte 4-5: Value (0.01 factor, little endian)
                        
                        import random
                        # 使用一个新的 MAC 地址以触发重新发现
                        mock_mac_bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
                        
                        # 随机生成 20.00 - 30.00 C
                        temp_val = random.randint(2000, 3000)
                        
                        # 生成一个递增的 Packet ID (0-255)
                        if not hasattr(self, "_mock_packet_id"):
                            self._mock_packet_id = 0
                        self._mock_packet_id = (self._mock_packet_id + 1) % 256
                        
                        mock_payload = bytes([
                            0x40,           # Device Info: No encryption, BTHome v2
                            0x00,           # Packet ID type
                            self._mock_packet_id, # Value
                            0x02,           # Temperature type
                            temp_val & 0xFF, (temp_val >> 8) & 0xFF # Value
                        ])
                        
                        mock_pkg = bytearray([HEADER, 0x00, 0x00, 0x82, 0x03, 0xE2])
                        mock_pkg.extend(mock_mac_bytes)
                        mock_pkg.append(len(mock_payload))
                        mock_pkg.extend(mock_payload)
                        
                        _LOGGER.info("DEBUG: Triggering mock BTHome injection (Temp: %s C, Cnt: %s)", temp_val/100.0, self._mock_packet_id)
                        self._handle_bthome_proxy_packet(mock_pkg)


                    rssi = None
                    try:
                        rssi = await self.client.get_rssi()
                    except Exception:
                        pass
                    
                    # 如果 client.get_rssi() 失败，尝试从 HA 蓝牙缓存获取
                    if rssi is None:
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
                    self.connected = False
                    self._stop_heartbeat()
                    if not self._reconnect_task or self._reconnect_task.done():
                        self._reconnect_task = self.hass.async_create_task(self._persistent_reconnect())
                    break

                await asyncio.sleep(self.keep_alive_interval)

        self._heartbeat_task = self.hass.async_create_task(heartbeat_loop())

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
                _LOGGER.info(
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
                _LOGGER.info(
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

    def debug_inject_mock_bthome(self, mac: Optional[str] = None, temp_c: Optional[float] = None) -> None:
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
