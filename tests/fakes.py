"""Minimal fake homeassistant / bleak modules so ble_controller.py runs without either installed.

Install with install(), then import the integration through load_module(). BLEHOME_SRC may point at
another checkout (for example an export of an older commit) to run the same suite against it.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "blehome"


class BleakError(Exception):
    """Stand-in for bleak.exc.BleakError."""


class BLEDevice:
    def __init__(self, address: str, name: str | None = None, details: Any = None, **_kw: Any) -> None:
        self.address = address
        self.name = name
        self.details = details


class AdvertisementData:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class BluetoothServiceInfoBleak:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Env:
    """Per-test state shared by the fake BLE stack; reset by the env fixture."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.connect_ok = True
        self.device_visible = True
        self.gate: asyncio.Event | None = None  # when set, BleakClient.connect() waits on it
        self.clients: list[FakeBleakClient] = []
        self.writes: list[bytes] = []
        self.on_write: Any = None
        self.scanners = 0
        self.registered: list[Any] = []


ENV = _Env()


class _Char:
    properties = ("notify", "write")
    uuid = "0000ffe1-0000-1000-8000-00805f9b34fb"


class _Services:
    def get_service(self, _uuid: str) -> None:
        return None

    def get_characteristic(self, _uuid: str) -> _Char:
        return _Char()


class FakeBleakClient:
    def __init__(self, device: Any, *_a: Any, **_k: Any) -> None:
        self.device = device
        self._connected = False
        self.cb: Any = None
        self.services = _Services()
        ENV.clients.append(self)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, timeout: float = 10) -> None:
        if ENV.gate is not None:
            await ENV.gate.wait()
        await asyncio.sleep(0)
        if not ENV.connect_ok:
            raise BleakError("connect failed")
        self._connected = True

    def set_disconnected_callback(self, cb: Any) -> None:
        self.cb = cb

    async def start_notify(self, _uuid: str, _handler: Any) -> None:
        return None

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def write_gatt_char(self, _uuid: str, data: Any, response: bool = False) -> None:
        if not self._connected:
            raise BleakError("not connected")
        ENV.writes.append(bytes(data))
        if ENV.on_write is not None:
            ENV.on_write(bytes(data))

    async def disconnect(self) -> None:
        self._connected = False

    def drop(self) -> None:
        """Simulate the radio link dropping: fires the disconnect callback like bleak does."""
        self._connected = False
        if self.cb:
            self.cb(self)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.listeners: list[Any] = []

    def async_fire(self, event: str, data: Any = None) -> None:
        self.events.append((event, data))
        for listener in list(self.listeners):
            listener(event, data)


class FakeConfig:
    def __init__(self, config_dir: str) -> None:
        self.config_dir = config_dir
        self.allowlist: list[str] = []

    def path(self, *parts: str) -> str:
        return os.path.join(self.config_dir, *parts)

    def is_allowed_path(self, path: str) -> bool:
        real = os.path.realpath(path)
        return any(os.path.commonpath([real, os.path.realpath(d)]) == os.path.realpath(d) for d in self.allowlist)


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def async_call(self, *args: Any, **_kw: Any) -> None:
        self.calls.append(args)


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    def async_update_entry(self, entry: Any, options: Any = None) -> None:
        entry.options = options
        self.updates.append(options)


class FakeHass:
    """HA-like: tasks start eagerly, is_running / is_stopping are plain flags."""

    def __init__(self, config_dir: str) -> None:
        self.bus = FakeBus()
        self.data: dict[str, Any] = {}
        self.is_running = True
        self.is_stopping = False
        self.config = FakeConfig(config_dir)
        self.services = FakeServices()
        self.config_entries = FakeConfigEntries()
        self.tasks: list[asyncio.Task] = []

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def _spawn(self, coro: Any, name: str | None = None) -> asyncio.Task:
        task = asyncio.Task(coro, loop=self.loop, name=name, eager_start=True)
        self.tasks.append(task)
        return task

    def async_create_task(self, coro: Any, name: str | None = None, eager_start: bool = True) -> asyncio.Task:
        return self._spawn(coro, name)

    def async_create_background_task(self, coro: Any, name: str, eager_start: bool = True) -> asyncio.Task:
        return self._spawn(coro, name)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return await self.loop.run_in_executor(None, func, *args)


class FakeConfigEntry:
    def __init__(self) -> None:
        self.entry_id = "entry1"
        self.options: dict[str, Any] = {}

    def async_create_background_task(self, hass: FakeHass, coro: Any, name: str, eager_start: bool = True) -> asyncio.Task:
        return hass.async_create_background_task(coro, name)


class FakeDeviceRegistry:
    def async_get_or_create(self, **_kw: Any) -> types.SimpleNamespace:
        return types.SimpleNamespace(id="dev1")

    def async_get_device(self, identifiers: Any = None) -> None:
        return None

    def async_update_device(self, *_a: Any, **_kw: Any) -> None:
        return None


def _mod(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def install() -> None:
    """Register the fake third-party modules in sys.modules (idempotent)."""
    if "bleak" in sys.modules and getattr(sys.modules["bleak"], "_blehome_fake", False):
        return

    def async_register_scanner(_hass: Any, scanner: Any) -> Any:
        ENV.scanners += 1
        ENV.registered.append(scanner)

        def unregister() -> None:
            ENV.scanners -= 1

        return unregister

    class BaseScanner:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            return None

    class Platform:
        LIGHT = "light"
        TEXT = "text"
        SENSOR = "sensor"
        SWITCH = "switch"
        BUTTON = "button"

    # Force the optional imports onto their fallback branches
    sys.modules["habluetooth"] = None  # type: ignore[assignment]
    sys.modules["bleak_retry_connector"] = None  # type: ignore[assignment]

    _mod(
        "bleak", _blehome_fake=True, BleakError=BleakError, BLEDevice=BLEDevice,
        AdvertisementData=AdvertisementData, BleakClient=FakeBleakClient,
    )
    _mod("homeassistant")
    _mod("homeassistant.components")
    _mod(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=BluetoothServiceInfoBleak,
        async_ble_device_from_address=lambda _h, addr, connectable=True: BLEDevice(addr) if ENV.device_visible else None,
        async_register_scanner=async_register_scanner,
        BaseScanner=BaseScanner,
        async_last_service_info=lambda *_a, **_k: None,
    )
    _mod("homeassistant.const", Platform=Platform)
    _mod("homeassistant.config_entries", ConfigEntry=object)
    _mod("homeassistant.core", HomeAssistant=object)
    helpers = _mod("homeassistant.helpers")
    helpers.device_registry = _mod("homeassistant.helpers.device_registry", async_get=lambda _h: FakeDeviceRegistry())
    helpers.aiohttp_client = _mod("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda _h: None)


def source_root() -> Path:
    return Path(os.environ.get("BLEHOME_SRC") or ROOT)


def load_module(name: str) -> types.ModuleType:
    """Import custom_components/blehome/<name>.py as blehome.<name> (without running the package __init__)."""
    install()
    pkg_dir = source_root() / "custom_components" / "blehome"
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
        sys.modules[PKG_NAME] = pkg
    return importlib.import_module(f"{PKG_NAME}.{name}")


def bundled_dir() -> Path:
    return source_root() / "custom_components" / "blehome" / "bin"


class FakeNode:
    """Mesh node behind the gateway: answers OTA commands the way the firmware does (PROTOCOL.md).

    update / verify modes: ok, bad (status 1), silent (no ACK), short (5-byte ACK).
    """

    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.update = "ok"
        self.verify = "ok"
        self.foreign_first = False  # emit an ACK for another node before every real one
        self.image_size = 0x10000
        self.frames: list[bytes] = []

    def commands(self, cmd: int) -> list[bytes]:
        return [f for f in self.frames if f[0] == cmd]

    def _deliver(self, ack: bytes) -> None:
        asyncio.get_running_loop().call_soon(self.controller._notification_handler, None, bytearray(ack))

    def on_write(self, data: bytes) -> None:
        cmd = data[0]
        if cmd not in (0xA6, 0xA7, 0xA8, 0xA9):
            return
        self.frames.append(data)
        if cmd == 0xA9:
            return
        addr = data[1:3]
        if self.foreign_first:
            other = bytes([data[1] ^ 0x01, data[2]])
            self._deliver(bytes([cmd - 0x20]) + other + data[3:5] + b"\x00")
        if cmd == 0xA6:
            size = self.image_size.to_bytes(4, "little")
            self._deliver(bytes([0x86]) + addr + size + b"\x00\x01" + b"\x01\x82" + b"\x00")
            return
        mode = self.update if cmd == 0xA7 else self.verify
        if mode == "silent":
            return
        head = bytes([cmd - 0x20]) + addr + data[3:5]
        if mode == "short":
            self._deliver(head)
        else:
            self._deliver(head + (b"\x00" if mode == "ok" else b"\x01"))


def availability(hass: FakeHass) -> list[bool]:
    """The `connected` values of every blehome_availability_changed event, in order."""
    return [d["connected"] for e, d in hass.bus.events if e == "blehome_availability_changed"]
