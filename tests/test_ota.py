"""OTA flow against a fake mesh node, and local firmware path resolution."""
from __future__ import annotations

import asyncio
import os

import fakes
import pytest

FW_REL = os.path.join("custom_components", "blehome", "bin", "node.bin")
FW_SIZE = 300  # two UPDATE chunks of up to 216 bytes
SHORT_TIMEOUT = 0.05


@pytest.fixture
def ota(make_ctrl, attach, hass, env, tmp_path):
    """Connected controller wired to a fake node, with a bundled firmware image in the config dir."""
    ctrl = make_ctrl()
    attach(ctrl)
    node = fakes.FakeNode(ctrl)
    bundled = os.path.join(hass.config.config_dir, FW_REL)
    os.makedirs(os.path.dirname(bundled))
    with open(bundled, "wb") as f:
        f.write(bytes(i & 0xFF for i in range(FW_SIZE)))

    # Wait for ACKs briefly instead of the production 120 s
    real_send_and_wait = ctrl._send_and_wait

    async def quick(cmd, ack, timeout=30.0):
        return await real_send_and_wait(cmd, ack, timeout=min(timeout, SHORT_TIMEOUT))

    ctrl._send_and_wait = quick
    env.on_write = None
    return ctrl, node, bundled


def wire(node, env):
    env.on_write = node.on_write


async def run_ota(ctrl, address: int, path: str):
    return await ctrl.async_ota_update_node(address, path)


async def test_ota_success_sends_end_after_clean_verify(ota, env):
    ctrl, node, path = ota
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"], result
    assert "verify incomplete" not in result["message"]
    assert len(node.commands(0xA7)) == 2
    assert len(node.commands(0xA8)) == 2
    assert len(node.commands(0xA9)) == 1
    assert node.commands(0xA9)[0] == bytes([0xA9, 0x02, 0x00])
    # UPDATE chunks carry flash addresses in 8-byte units starting at 0x1000
    assert node.commands(0xA7)[0][3:5] == bytes([0x00, 0x02])
    assert node.commands(0xA7)[1][3:5] == bytes([27, 0x02])  # 216 bytes later: 0x200 + 27


async def test_ota_verify_mismatch_fails_and_does_not_send_end(ota, env):
    ctrl, node, path = ota
    node.verify = "bad"
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"] is False
    assert "mismatch" in result["message"]
    assert node.commands(0xA9) == [], "END was sent although VERIFY reported a mismatch"
    assert len(node.commands(0xA8)) == 5  # initial try plus 4 retries on the first chunk


async def test_ota_verify_timeout_still_sends_end_and_notes_incomplete(ota, env):
    ctrl, node, path = ota
    node.verify = "silent"
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"] is True
    assert "verify incomplete" in result["message"]
    assert len(node.commands(0xA9)) == 1


async def test_ota_short_update_ack_does_not_raise(ota, env):
    ctrl, node, path = ota
    node.update = "short"
    wire(node, env)
    result = await run_ota(ctrl, 2, path)  # must not raise IndexError
    assert result["success"] is False
    assert "N/A" in result["message"]
    assert node.commands(0xA9) == []


async def test_ota_short_verify_ack_does_not_raise(ota, env):
    ctrl, node, path = ota
    node.verify = "short"
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"] is False
    assert node.commands(0xA9) == []


async def test_ota_update_failure_status_stops_before_verify(ota, env):
    ctrl, node, path = ota
    node.update = "bad"
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"] is False
    assert node.commands(0xA8) == []
    assert node.commands(0xA9) == []


async def test_ota_ignores_acks_from_other_nodes(ota, env):
    ctrl, node, path = ota
    node.foreign_first = True
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"], result
    assert len(node.commands(0xA9)) == 1


async def test_second_concurrent_ota_is_rejected(ota, env):
    ctrl, node, path = ota
    wire(node, env)
    first, second = await asyncio.gather(run_ota(ctrl, 2, path), run_ota(ctrl, 3, path))
    assert first["success"], first
    assert second["success"] is False
    assert "Another OTA" in second["message"]
    assert all(f[1:3] == b"\x02\x00" for f in node.frames), "second OTA reached the mesh"
    # The lock is released afterwards
    third = await run_ota(ctrl, 3, path)
    assert third["success"], third


@pytest.mark.parametrize("address", [0x0000, 0x8000, 0xC000, 0xFFFF, -1, 0x10000])
async def test_ota_rejects_non_unicast_addresses(ota, env, address):
    ctrl, node, path = ota
    wire(node, env)
    result = await run_ota(ctrl, address, path)
    assert result["success"] is False
    assert "Invalid node address" in result["message"]
    assert node.frames == []
    assert env.writes == []


@pytest.mark.parametrize("address", [0x0001, 0x7FFF])
async def test_ota_accepts_edge_unicast_addresses(ota, env, address):
    ctrl, node, path = ota
    wire(node, env)
    result = await run_ota(ctrl, address, path)
    assert result["success"], result


async def test_ota_rejects_firmware_larger_than_node_image(ota, env):
    ctrl, node, path = ota
    node.image_size = FW_SIZE - 1
    wire(node, env)
    result = await run_ota(ctrl, 2, path)
    assert result["success"] is False
    assert "too large" in result["message"]
    assert node.commands(0xA7) == []


async def test_ota_refuses_file_outside_bundle_and_allowlist(ota, env, tmp_path):
    ctrl, node, _path = ota
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"\x00" * 64)
    wire(node, env)
    result = await run_ota(ctrl, 2, str(outside))
    assert result["success"] is False
    assert "not allowed" in result["message"]
    assert node.frames == []


# --- local firmware path resolution (case 14) ---


@pytest.fixture
def resolver(make_ctrl, hass, tmp_path):
    ctrl = make_ctrl()
    bundled_dir = os.path.join(hass.config.config_dir, "custom_components", "blehome", "bin")
    os.makedirs(os.path.join(bundled_dir, "CH582"))
    inside = os.path.join(bundled_dir, "CH582", "fw.hex")
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = str(outside_dir / "evil.hex")
    for p in (inside, outside):
        with open(p, "w") as f:
            f.write(":00000001FF\n")
    return ctrl, hass, inside, outside, bundled_dir


def test_resolve_accepts_bundled_file_relative_and_absolute(resolver):
    ctrl, _hass, inside, _outside, _bundled = resolver
    rel = os.path.join("custom_components", "blehome", "bin", "CH582", "fw.hex")
    assert ctrl._resolve_local_firmware(rel) == os.path.realpath(inside)
    assert ctrl._resolve_local_firmware(inside) == os.path.realpath(inside)


def test_resolve_rejects_absolute_path_outside_bundle(resolver):
    ctrl, _hass, _inside, outside, _bundled = resolver
    assert ctrl._resolve_local_firmware(outside) is None
    assert ctrl._resolve_local_firmware("/etc/passwd") is None


def test_resolve_rejects_dotdot_traversal(resolver):
    ctrl, _hass, _inside, outside, _bundled = resolver
    hass_dir = ctrl.hass.config.config_dir
    traversal = os.path.join(
        "custom_components", "blehome", "bin", "..", "..", "..", "..", os.path.relpath(outside, os.path.dirname(hass_dir))
    )
    assert os.path.isfile(os.path.join(hass_dir, traversal)), "test setup: traversal must reach a real file"
    assert ctrl._resolve_local_firmware(traversal) is None
    sibling = os.path.join("custom_components", "blehome", "bin", "..", "__init__.py")
    assert ctrl._resolve_local_firmware(sibling) is None


def test_resolve_rejects_symlink_escaping_bundle(resolver):
    ctrl, _hass, _inside, outside, bundled = resolver
    link = os.path.join(bundled, "link.hex")
    os.symlink(outside, link)
    assert ctrl._resolve_local_firmware(link) is None


def test_resolve_rejects_sibling_directory_with_same_prefix(resolver):
    ctrl, hass, _inside, _outside, bundled = resolver
    lookalike = bundled + "_evil"
    os.makedirs(lookalike)
    path = os.path.join(lookalike, "fw.hex")
    with open(path, "w") as f:
        f.write(":00000001FF\n")
    assert ctrl._resolve_local_firmware(path) is None


def test_resolve_accepts_allowlisted_directory_only(resolver):
    ctrl, hass, _inside, outside, _bundled = resolver
    assert ctrl._resolve_local_firmware(outside) is None
    hass.config.allowlist.append(os.path.dirname(outside))
    assert ctrl._resolve_local_firmware(outside) == os.path.realpath(outside)


def test_resolve_rejects_missing_file(resolver):
    ctrl, _hass, _inside, _outside, bundled = resolver
    assert ctrl._resolve_local_firmware(os.path.join(bundled, "missing.hex")) is None
