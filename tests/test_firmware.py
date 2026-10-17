"""Intel HEX parsing limits and firmware file discovery."""
from __future__ import annotations

import os

import fakes
import pytest

BUNDLED_HEX = sorted(
    os.path.join(root, name)
    for root, _dirs, names in os.walk(fakes.bundled_dir())
    for name in names
    if name.lower().endswith(".hex")
)


def record(addr: int, rtype: int, data: bytes = b"") -> str:
    body = bytes([len(data), addr >> 8, addr & 0xFF, rtype]) + data
    return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()


def write_hex(path, *lines: str) -> str:
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_valid_hex_with_linear_address_parses(bc, tmp_path):
    path = write_hex(
        tmp_path / "ok.hex",
        record(0, 4, b"\x00\x01"),
        record(0x0000, 0, b"\x01\x02\x03\x04"),
        record(0x0008, 0, b"\x05\x06"),
        record(0, 1),
    )
    data, start = bc._parse_hex_file(path)
    assert start == 0x10000
    assert data == b"\x01\x02\x03\x04\x00\x00\x00\x00\x05\x06"


def test_bad_checksum_is_rejected(bc, tmp_path):
    good = record(0x1000, 0, b"\xde\xad\xbe\xef")
    bad = good[:-2] + f"{(int(good[-2:], 16) + 1) & 0xFF:02X}"
    path = write_hex(tmp_path / "bad.hex", bad, record(0, 1))
    with pytest.raises(ValueError):
        bc._parse_hex_file(path)
    assert bc.BLEHomeController._parse_ota_firmware(path) is None


def test_truncated_record_is_rejected(bc, tmp_path):
    good = record(0x1000, 0, b"\xde\xad\xbe\xef")
    path = write_hex(tmp_path / "cut.hex", good[:-4], record(0, 1))
    with pytest.raises(ValueError):
        bc._parse_hex_file(path)
    assert bc.BLEHomeController._parse_ota_firmware(path) is None


def test_oversized_image_is_rejected_before_allocation(bc, tmp_path):
    # Two tiny records 64 MiB apart: the file is a few bytes, the image would be 64 MiB
    path = write_hex(
        tmp_path / "huge.hex",
        record(0, 4, b"\x00\x00"),
        record(0x0000, 0, b"\x01\x02\x03\x04"),
        record(0, 4, b"\x04\x00"),
        record(0x0000, 0, b"\x05\x06\x07\x08"),
        record(0, 1),
    )
    assert os.path.getsize(path) < 1024
    with pytest.raises(ValueError):
        bc._parse_hex_file(path)
    assert bc.BLEHomeController._parse_ota_firmware(path) is None


def test_image_span_at_the_limit_is_accepted_and_one_more_byte_is_not(bc, const, tmp_path):
    limit = getattr(const, "FIRMWARE_MAX_IMAGE_SIZE", 512 * 1024)
    last = limit - 1  # the highest address of an image that is exactly `limit` bytes
    ok = write_hex(
        tmp_path / "edge.hex",
        record(0, 4, b"\x00\x00"),
        record(0x0000, 0, b"\x01"),
        record(0, 4, bytes([last >> 24, (last >> 16) & 0xFF])),
        record(last & 0xFFFF, 0, b"\x02"),
        record(0, 1),
    )
    data, start = bc._parse_hex_file(ok)
    assert (start, len(data)) == (0, limit)
    over = write_hex(
        tmp_path / "over.hex",
        record(0, 4, b"\x00\x00"),
        record(0x0000, 0, b"\x01"),
        record(0, 4, bytes([limit >> 24, (limit >> 16) & 0xFF])),
        record(limit & 0xFFFF, 0, b"\x02"),
        record(0, 1),
    )
    with pytest.raises(ValueError):
        bc._parse_hex_file(over)


def test_oversized_bin_file_is_rejected(bc, const, tmp_path):
    limit = getattr(const, "FIRMWARE_MAX_IMAGE_SIZE", 512 * 1024)
    path = tmp_path / "big.bin"
    with open(path, "wb") as f:
        f.truncate(limit + 1)
    assert bc.BLEHomeController._parse_ota_firmware(str(path)) is None


def test_bin_file_starts_at_0x1000(bc, tmp_path):
    path = tmp_path / "ok.bin"
    path.write_bytes(b"\x01\x02\x03")
    assert bc.BLEHomeController._parse_ota_firmware(str(path)) == (b"\x01\x02\x03", 0x1000)


def test_unsupported_extension_is_rejected(bc, tmp_path):
    path = tmp_path / "fw.txt"
    path.write_text("x")
    assert bc.BLEHomeController._parse_ota_firmware(str(path)) is None


def test_bundled_firmware_is_present():
    assert any(p.endswith("TLED_V3.4.hex") for p in BUNDLED_HEX)


@pytest.mark.parametrize("path", BUNDLED_HEX, ids=os.path.basename)
def test_bundled_hex_files_still_parse(bc, path):
    parsed = bc.BLEHomeController._parse_ota_firmware(path)
    assert parsed is not None
    data, start = parsed
    assert start == 0x1000
    assert 0 < len(data) <= 512 * 1024


# --- firmware discovery (case 10) ---


def load_firmware():
    try:
        return fakes.load_module("firmware")
    except ModuleNotFoundError:
        pytest.fail("custom_components/blehome/firmware.py does not exist")


def test_version_key_orders_numerically():
    firmware = load_firmware()
    key = firmware.firmware_version_key
    assert key("TLED_V3.10.hex") > key("TLED_V3.4.hex")
    assert key("TLED_V3.4.hex") > key("TLED_V3.1.hex")
    assert key("TLED_V10.0.hex") > key("TLED_V9.9.hex")
    assert key("fw_v2_5.bin") == (2, 5)
    assert key("noversion.bin") < key("TLED_V0.1.hex")


def test_version_key_uses_file_name_only():
    firmware = load_firmware()
    assert firmware.firmware_version_key("/data/v9.9/TLED_V3.4.hex") == (3, 4)


def test_scan_sorts_oldest_first_and_filters_extensions(tmp_path):
    firmware = load_firmware()
    sub = tmp_path / "CH582"
    sub.mkdir()
    for name in ("TLED_V3.10.hex", "TLED_V3.4.hex", "TLED_V3.9.bin", "noversion.bin", "readme.txt", "TLED_V3.5.hex.bak"):
        (sub / name).write_text("x")
    found = [os.path.basename(p) for p in firmware.scan_firmware_files(str(tmp_path))]
    assert found == ["noversion.bin", "TLED_V3.4.hex", "TLED_V3.9.bin", "TLED_V3.10.hex"]


def test_scan_missing_directory_returns_empty(tmp_path):
    firmware = load_firmware()
    assert firmware.scan_firmware_files(str(tmp_path / "nope")) == []


def test_scan_bundled_dir_puts_newest_last():
    firmware = load_firmware()
    found = firmware.scan_firmware_files(str(fakes.bundled_dir()))
    names = [os.path.basename(p) for p in found]
    assert names
    assert names == sorted(names, key=firmware.firmware_version_key)
    assert names[-1] == max(names, key=firmware.firmware_version_key)
