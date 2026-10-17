"""Pytest setup: fake HA / bleak, async test runner without plugins, fast sleeps."""
from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable
from typing import Any

import fakes
import pytest

sys.dont_write_bytecode = True
fakes.install()

REAL_SLEEP = asyncio.sleep
SLEEP_SCALE = 0.001  # asyncio.sleep(30) waits 30 ms
TEST_TIMEOUT = 30.0
MAC = "70:19:88:C9:27:8B"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
HEARTBEAT = bytes([0xA5, 0, 0, 0, 0, 0, 0])


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run `async def` tests on a fresh event loop, so no asyncio plugin is needed."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}

    async def run() -> None:
        async with asyncio.timeout(TEST_TIMEOUT):
            await pyfuncitem.obj(**kwargs)

    asyncio.run(run())
    return True


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> fakes._Env:
    """Fresh fake BLE state and scaled-down sleeps for every test."""
    fakes.ENV.reset()

    async def fast_sleep(delay: float, result: Any = None) -> Any:
        return await REAL_SLEEP(delay * SLEEP_SCALE, result)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return fakes.ENV


@pytest.fixture
def real_sleep() -> Callable[[float], Any]:
    return REAL_SLEEP


@pytest.fixture
def wait_until() -> Callable[..., Any]:
    async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                return False
            await REAL_SLEEP(0.005)
        return True

    return wait_until


@pytest.fixture(scope="session")
def bc() -> Any:
    """The ble_controller module under test."""
    return fakes.load_module("ble_controller")


@pytest.fixture(scope="session")
def const() -> Any:
    return fakes.load_module("const")


@pytest.fixture
def hass(tmp_path: Any) -> fakes.FakeHass:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return fakes.FakeHass(str(config_dir))


@pytest.fixture
def make_ctrl(bc: Any, hass: fakes.FakeHass) -> Callable[..., Any]:
    """Build a controller; with_entry routes background tasks through a fake ConfigEntry."""

    def make(with_entry: bool = False, subdevices: int = 0) -> Any:
        ctrl = bc.BLEHomeController(hass, MAC, "0000ffe0-0000-1000-8000-00805f9b34fb", CHAR_UUID)
        ctrl.mac_suffix = "278b"
        if with_entry:
            ctrl.config_entry = fakes.FakeConfigEntry()
        ctrl.subdevices = {i + 2: {"name": f"sub{i}", "state": {}} for i in range(subdevices)}

        async def no_version(_addr: int) -> None:
            return None

        ctrl.async_query_version = no_version  # keep lifecycle tests off the 8 s ACK wait
        return ctrl

    return make


@pytest.fixture
def attach() -> Callable[[Any], fakes.FakeBleakClient]:
    """Put a connected fake client on a controller without running connect() or any background task."""

    def attach(ctrl: Any) -> fakes.FakeBleakClient:
        client = fakes.FakeBleakClient(fakes.BLEDevice(MAC))
        client._connected = True
        client.set_disconnected_callback(ctrl._on_disconnected)
        ctrl.client = client
        ctrl.connected = True
        return client

    return attach

