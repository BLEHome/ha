"""ACK matching, notification handling, brightness mapping and the BTHome proxy path."""
from __future__ import annotations

import asyncio
import inspect

import pytest

NODE2_FLASH_0200 = bytes([0x87, 0x02, 0x00, 0x00, 0x02])  # cmd, node 0x0002, flash 0x0200


def start_wait(ctrl, prefix: bytes, timeout: float = 5.0) -> asyncio.Task:
    """Start _send_and_wait for an ACK with this prefix (HEAD took only the command byte)."""
    param = list(inspect.signature(ctrl._send_and_wait).parameters)[1]
    arg = prefix if param == "ack_prefix" else prefix[0]
    return asyncio.ensure_future(ctrl._send_and_wait(b"\xa7" + prefix[1:], arg, timeout=timeout))


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --- ACK matching (case 7) ---


@pytest.mark.parametrize(
    "foreign",
    [
        bytes([0x87, 0x03, 0x00, 0x00, 0x02, 0x00]),  # another node
        bytes([0x87, 0x02, 0x00, 0x01, 0x02, 0x00]),  # another flash address
        bytes([0x87, 0x02, 0x00, 0x00, 0x03, 0x00]),
        bytes([0x88, 0x02, 0x00, 0x00, 0x02, 0x00]),  # another command
    ],
)
async def test_ack_for_other_node_or_flash_address_does_not_satisfy_wait(make_ctrl, attach, foreign):
    ctrl = make_ctrl()
    attach(ctrl)
    waiter = start_wait(ctrl, NODE2_FLASH_0200)
    await settle()

    ctrl._notification_handler(None, bytearray(foreign))
    await settle()
    assert not waiter.done(), "foreign ACK was taken as the answer"

    match = bytes([0x87, 0x02, 0x00, 0x00, 0x02, 0x00])
    ctrl._notification_handler(None, bytearray(match))
    assert await asyncio.wait_for(waiter, 1.0) == match


async def test_second_ack_after_match_is_ignored(make_ctrl, attach):
    ctrl = make_ctrl()
    attach(ctrl)
    waiter = start_wait(ctrl, NODE2_FLASH_0200)
    await settle()
    first = bytes([0x87, 0x02, 0x00, 0x00, 0x02, 0x00])
    ctrl._notification_handler(None, bytearray(first))
    ctrl._notification_handler(None, bytearray([0x87, 0x02, 0x00, 0x00, 0x02, 0x05]))
    assert await asyncio.wait_for(waiter, 1.0) == first


async def test_version_query_matches_only_its_own_node(make_ctrl, attach):
    ctrl = make_ctrl()
    attach(ctrl)
    del ctrl.async_query_version  # use the real method, not the lifecycle stub
    query = asyncio.ensure_future(ctrl.async_query_version(5))
    await settle()
    ctrl._notification_handler(None, bytearray([0x8B, 0x06, 0x00, 9, 9, 0x00]))  # node 6
    await settle()
    assert not query.done()
    ctrl._notification_handler(None, bytearray([0x8B, 0x05, 0x00, 3, 4, 0x00]))
    assert await asyncio.wait_for(query, 1.0) == (3, 4)


async def test_version_query_with_error_status_returns_none(make_ctrl, attach):
    ctrl = make_ctrl()
    attach(ctrl)
    del ctrl.async_query_version
    query = asyncio.ensure_future(ctrl.async_query_version(5))
    await settle()
    ctrl._notification_handler(None, bytearray([0x8B, 0x05, 0x00, 3, 4, 0x01]))
    assert await asyncio.wait_for(query, 1.0) is None


# --- notification handler (case 11) ---


def state_frame(addr: int, cmd: int = 0x8202, on: int = 1, duty: int = 100) -> bytearray:
    return bytearray([0xA5, addr & 0xFF, addr >> 8, cmd >> 8, cmd & 0xFF, on, duty])


@pytest.mark.parametrize(
    ("addr", "cmd"),
    [
        (0x0002, 0x8301),  # not a state report
        (0x0002, 0x0000),
        (0x0002, 0x8203),  # BTHome proxy frame, not a state report
        (0x0000, 0x8202),
        (0x0000, 0x8201),
        (0x8000, 0x8202),
        (0x8001, 0x8202),
        (0xC000, 0x8202),
        (0xFFFF, 0x8202),
    ],
)
async def test_invalid_frames_do_not_create_subdevices(make_ctrl, hass, addr, cmd):
    ctrl = make_ctrl()
    ctrl._notification_handler(None, state_frame(addr, cmd))
    await settle()
    assert ctrl.subdevices == {}
    assert not [e for e, _ in hass.bus.events if e == "blehome_new_subdevice_found"]


@pytest.mark.parametrize("addr", [0x0001, 0x0002, 0x1234, 0x7FFF])
@pytest.mark.parametrize("cmd", [0x8202, 0x8201])
async def test_valid_state_report_creates_subdevice(make_ctrl, hass, addr, cmd):
    ctrl = make_ctrl()
    ctrl._notification_handler(None, state_frame(addr, cmd, on=1, duty=100))
    await settle()
    assert addr in ctrl.subdevices
    assert ctrl.subdevices[addr]["state"] == {"on": True, "brightness": 255}
    found = [d for e, d in hass.bus.events if e == "blehome_new_subdevice_found"]
    assert len(found) == 1
    assert found[0]["address"] == addr


async def test_state_report_for_known_subdevice_updates_state(make_ctrl, hass):
    ctrl = make_ctrl(subdevices=1)  # address 2
    ctrl._notification_handler(None, state_frame(2, on=0, duty=0))
    assert ctrl.subdevices[2]["state"] == {"on": False, "brightness": 0}
    updates = [d for e, d in hass.bus.events if e == "blehome_subdevice_updated"]
    assert updates[-1]["address"] == 2
    assert updates[-1]["controller_mac"] == ctrl.mac_address


async def test_short_or_foreign_frames_are_ignored(make_ctrl):
    ctrl = make_ctrl()
    ctrl._notification_handler(None, bytearray([0xA5, 0x02, 0x00, 0x82, 0x02, 0x01]))  # 6 bytes
    ctrl._notification_handler(None, bytearray([0xA6, 0x02, 0x00, 0x82, 0x02, 0x01, 0x10]))  # wrong header
    ctrl._notification_handler(None, bytearray())
    await settle()
    assert ctrl.subdevices == {}


# --- brightness mapping (case 12) ---


def test_gamma_correct_endpoints_and_minimum(bc):
    gamma = bc.BLEHomeController._gamma_correct
    assert gamma(0) == 0
    assert gamma(-5) == 0
    assert gamma(255) == 100
    assert gamma(300) == 100
    assert gamma(1) >= 1


def test_gamma_correct_is_monotonic_and_never_dark_when_on(bc):
    gamma = bc.BLEHomeController._gamma_correct
    values = [gamma(b) for b in range(1, 256)]
    assert all(1 <= v <= 100 for v in values)
    assert values == sorted(values)


async def test_control_command_never_sends_duty_zero_for_on_light(make_ctrl, attach, env):
    ctrl = make_ctrl(subdevices=1)
    attach(ctrl)
    assert await ctrl.send_control_command(2, True, 1)
    frame = env.writes[-1]
    assert frame[:5] == bytes([0xA5, 0x02, 0x00, 0x82, 0x02])
    assert frame[5] == 1
    assert frame[6] >= 1
    assert await ctrl.send_control_command(2, False, 0)
    assert env.writes[-1][5:7] == bytes([0, 0])


# --- BTHome proxy packets (case 13) ---


def proxy_frame(mac: bytes = b"\x11\x22\x33\x44\x55\x66", rssi: int = 0xC8, adv: bytes = b"\x40\x02\x10\x09",
                adv_len: int | None = None) -> bytearray:
    length = len(adv) if adv_len is None else adv_len
    return bytearray([0xA5, 0, 0, 0x82, 0x03, rssi]) + bytearray(mac) + bytearray([length]) + bytearray(adv)


@pytest.fixture
def injected(make_ctrl):
    ctrl = make_ctrl()
    seen = []
    ctrl._inject_to_bluetooth_manager = seen.append
    return ctrl, seen


async def test_valid_proxy_packet_is_injected(injected):
    ctrl, seen = injected
    ctrl._notification_handler(None, proxy_frame())
    assert len(seen) == 1
    assert seen[0].address == "11:22:33:44:55:66"
    assert seen[0].rssi == -56
    assert seen[0].service_data["0000fcd2-0000-1000-8000-00805f9b34fb"] == b"\x40\x02\x10\x09"


@pytest.mark.parametrize(
    "frame",
    [
        proxy_frame(adv=b"\x40\x02\x10\x09", adv_len=20),  # adv_len runs past the frame
        proxy_frame(adv=b"", adv_len=0),
        proxy_frame(mac=b"\x00" * 6),
        proxy_frame(mac=b"\xff" * 6),
        bytearray([0xA5, 0, 0, 0x82, 0x03, 0xC8, 1, 2, 3]),  # too short
    ],
    ids=["adv_len_overrun", "adv_len_zero", "zero_mac", "broadcast_mac", "too_short"],
)
async def test_malformed_proxy_packet_is_rejected(injected, frame):
    ctrl, seen = injected
    ctrl._notification_handler(None, frame)
    assert seen == []


@pytest.mark.parametrize("raw", [100, 127, 0])
async def test_forged_positive_rssi_is_clamped_or_rejected(injected, raw):
    ctrl, seen = injected
    ctrl._notification_handler(None, proxy_frame(rssi=raw))
    assert all(-127 <= info.rssi <= -1 for info in seen)


async def test_scanner_cache_is_capped(bc, make_ctrl, hass):
    ctrl = make_ctrl()
    ctrl._inject_to_bluetooth_manager = lambda _info: None
    ctrl.scanner = bc.BLEHomeScanner(hass, "entry1", ctrl.device_address, "proxy", ctrl)
    cap = getattr(bc, "_SCANNER_CACHE_MAX", 512)
    total = cap + 150
    for i in range(1, total + 1):
        mac = bytes([0x10, 0x20, 0x30, 0x40, i >> 8, i & 0xFF])
        ctrl._notification_handler(None, proxy_frame(mac=mac, adv=b"\x40\x00" + bytes([i & 0xFF])))
    cache = ctrl.scanner._discovered_devices
    assert len(cache) <= cap
    newest = ":".join(f"{b:02X}" for b in bytes([0x10, 0x20, 0x30, 0x40, total >> 8, total & 0xFF]))
    oldest = "10:20:30:40:00:01"
    assert newest in cache
    assert oldest not in cache


async def test_scanner_refresh_keeps_active_device(bc, hass, make_ctrl):
    scanner = bc.BLEHomeScanner(hass, "entry1", "AA:BB:CC:DD:EE:FF", "proxy", make_ctrl())
    cap = getattr(bc, "_SCANNER_CACHE_MAX", 512)

    class Info:
        service_uuids = ()
        service_data = {}
        source = "AA:BB:CC:DD:EE:FF"

        def __init__(self, address):
            self.address = address
            self.device = address
            self.advertisement = address

    scanner.inject_proxy_advertisement(Info("keep"))
    for i in range(cap - 1):
        scanner.inject_proxy_advertisement(Info(f"d{i}"))
    scanner.inject_proxy_advertisement(Info("keep"))  # seen again: now the newest
    scanner.inject_proxy_advertisement(Info("extra"))  # evicts the oldest, which is d0
    assert "keep" in scanner._discovered_devices
    assert "d0" not in scanner._discovered_devices
    assert len(scanner._discovered_devices) == cap
