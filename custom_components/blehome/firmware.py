"""Firmware file discovery for BLEHome OTA."""
from __future__ import annotations

import os
import re

FIRMWARE_EXTENSIONS = (".bin", ".hex")
_VERSION_RE = re.compile(r"v?(\d+)[._](\d+)", re.IGNORECASE)


def firmware_version_key(path: str) -> tuple[int, ...]:
    """Return a sortable version key parsed from a firmware file name.

    "TLED_V3.10.hex" sorts after "TLED_V3.4.hex"; names without a version sort first.
    """
    match = _VERSION_RE.search(os.path.basename(path))
    if not match:
        return (-1,)
    return (int(match.group(1)), int(match.group(2)))


def scan_firmware_files(bin_dir: str) -> list[str]:
    """Return firmware files under bin_dir, oldest version first.

    Blocking: call from an executor.
    """
    found: list[str] = []
    if not os.path.isdir(bin_dir):
        return found
    for root, _dirs, filenames in os.walk(bin_dir):
        for name in filenames:
            if name.lower().endswith(FIRMWARE_EXTENSIONS):
                found.append(os.path.join(root, name))
    found.sort(key=lambda p: (firmware_version_key(p), p))
    return found
