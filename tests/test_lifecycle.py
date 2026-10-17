"""Connection lifecycle: reconnect, disconnect callbacks, shutdown, background loops, fail-fast writes."""
from __future__ import annotations

import asyncio

import fakes
import pytest
from conftest import HEARTBEAT
from fakes import availability


@pytest.mark.parametrize("with_entry", [False, True])
async def test_link_drop_right_after_connected_still_reconnects(
    make_ctrl, hass, env, wait_until, with_entry
):
    """The link drops inside connect() right after connected=True, while running in the reconnect task."""
    ctrl = make_ctrl(with_entry=with_entry)
    assert await ctrl.connect()

    armed = True

    def drop_once_on_reconnect(event, data):
        nonlocal armed
        if armed and event == "blehome_availability_changed" and data["connected"]:
            armed = False
            env.clients[-1].drop()

    hass.bus.listeners.append(drop_once_on_reconnect)
    env.clients[-1].drop()  # first loss -> _persistent_reconnect

    assert await wait_until(lambda: not armed), "reconnect never reached connect()"
    recovered = await wait_until(
        lambda: ctrl.connected and ctrl.client is not None and ctrl.client.is_connected
        and (ctrl._reconnect_task is None or ctrl._reconnect_task.done())
    )
    assert recovered, "controller stayed offline after a drop inside connect()"
    assert len(env.clients) >= 3
    assert env.scanners == 1
    assert ctrl._heartbeat_task is not None and not ctrl._heartbeat_task.done()
    assert availability(hass)[-1] is True


async def test_late_disconnect_callback_from_old_client_is_ignored(make_ctrl, hass, env):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    old_client = ctrl.client
    await ctrl.disconnect()
    assert await ctrl.connect()
    assert ctrl.client is not old_client
    before = len(hass.bus.events)

    ctrl._on_disconnected(old_client)  # bleak reports the old link going away after the new one is up

    assert ctrl.connected
    assert ctrl.client.is_connected
    assert ctrl._reconnect_task is None or ctrl._reconnect_task.done()
    assert False not in availability(hass)
    assert len(hass.bus.events) == before


async def test_shutdown_while_offline_stops_reconnect(make_ctrl, env, real_sleep, wait_until):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    env.connect_ok = False
    env.clients[-1].drop()
    assert await wait_until(lambda: len(env.clients) >= 3), "reconnect loop never retried"
    assert ctrl._reconnect_task is not None and not ctrl._reconnect_task.done()

    if hasattr(ctrl, "async_shutdown"):
        await ctrl.async_shutdown()
    elif ctrl.connected:  # the old unload path: only disconnects a live link
        await ctrl.disconnect()

    clients_at_shutdown = len(env.clients)
    env.connect_ok = True  # the gateway comes back after the entry is gone
    await real_sleep(0.3)

    assert len(env.clients) == clients_at_shutdown, "reconnect kept running after unload"
    assert not ctrl.connected
    assert env.scanners == 0
    assert ctrl._reconnect_task is None or ctrl._reconnect_task.done()
    assert not ctrl._running


async def test_shutdown_closes_link_and_blocks_connect(make_ctrl, env):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    await ctrl.async_shutdown()
    assert not ctrl.connected
    assert env.scanners == 0
    assert not env.clients[-1].is_connected
    assert not ctrl._running
    assert await ctrl.connect() is False
    assert len(env.clients) == 1


async def test_loops_keep_running_while_hass_is_still_starting(make_ctrl, hass, env, real_sleep):
    """is_running is False during startup; only is_stopping or an unloaded entry may end the loops."""
    hass.is_running = False
    hass.is_stopping = False
    ctrl = make_ctrl()
    assert await ctrl.connect()

    await real_sleep(0.2)

    assert ctrl.connected
    assert ctrl._heartbeat_task is not None and not ctrl._heartbeat_task.done()
    assert ctrl._scan_task is not None and not ctrl._scan_task.done()
    assert env.writes.count(HEARTBEAT) >= 2
    assert any(w[0] == 0xA5 and w[3:5] == b"\x82\x01" for w in env.writes), "mesh scan never sent a query"


async def test_loops_stop_when_hass_is_stopping(make_ctrl, hass, env, real_sleep):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    heartbeat = ctrl._heartbeat_task
    await real_sleep(0.05)
    hass.is_stopping = True

    await real_sleep(0.2)

    assert heartbeat.done()
    assert ctrl._scan_task.done()
    # A link loss during shutdown must not start a new reconnect task
    env.clients[-1].drop()
    assert ctrl._reconnect_task is None


async def test_send_command_fails_fast_while_connect_is_running(make_ctrl, env, wait_until):
    ctrl = make_ctrl()
    env.gate = asyncio.Event()
    connect_task = asyncio.ensure_future(ctrl.connect())
    assert await wait_until(ctrl._connection_lock.locked)
    assert not ctrl.connected

    try:
        result = await asyncio.wait_for(ctrl.send_command(HEARTBEAT), timeout=0.5)
    except TimeoutError:
        pytest.fail("send_command waited on the connection lock held by connect()")
    finally:
        env.gate.set()
        await connect_task

    assert result is False


async def test_link_drop_fires_unavailable_once_and_wakes_pending_command(make_ctrl, hass, env, real_sleep):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    hass.bus.events.clear()

    waiter = asyncio.ensure_future(ctrl._send_and_wait(bytes([0xAB, 2, 0]), bytes([0x8B, 2, 0]), timeout=30.0))
    await real_sleep(0.02)
    assert not waiter.done()

    env.clients[-1].drop()

    try:
        result = await asyncio.wait_for(waiter, timeout=1.0)
    except TimeoutError:
        pytest.fail("pending command was not woken when the link dropped")
    assert result is None
    assert availability(hass) == [False]


async def test_handle_link_lost_is_idempotent(make_ctrl, hass, env):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    hass.bus.events.clear()

    ctrl._handle_link_lost()
    ctrl._handle_link_lost()

    assert not ctrl.connected
    assert availability(hass) == [False]
    assert ctrl._reconnect_task is not None


async def test_failed_write_marks_link_lost_once(make_ctrl, hass, env):
    ctrl = make_ctrl()
    assert await ctrl.connect()
    hass.bus.events.clear()

    def boom(_data):
        raise fakes.BleakError("write failed")

    env.on_write = boom
    assert await ctrl.send_command(HEARTBEAT) is False
    env.on_write = None

    assert not ctrl.connected
    assert availability(hass) == [False]
    assert await ctrl.send_command(HEARTBEAT) is False


async def test_connect_without_visible_device_fails_without_direct_client(make_ctrl, env):
    ctrl = make_ctrl()
    env.device_visible = False
    assert await ctrl.connect(retries=1) is False
    assert env.clients == []


async def test_connect_uses_establish_connection_when_available(bc, make_ctrl, hass, env, monkeypatch):
    calls = []

    async def fake_establish(client_cls, device, name, disconnected_callback=None, **kwargs):
        client = client_cls(device)
        await client.connect()
        client.set_disconnected_callback(disconnected_callback)
        calls.append((device.address, kwargs["max_attempts"]))
        return client

    monkeypatch.setattr(bc, "establish_connection", fake_establish)
    ctrl = make_ctrl()
    assert await ctrl.connect()
    assert calls == [(ctrl.device_address, 2)]

    env.clients[-1].drop()  # the callback passed to establish_connection reaches the controller
    assert not ctrl.connected
    assert availability(hass)[-1] is False


def test_registry_lookup_uses_entry_scoped_api_when_present(bc):
    class New:
        def async_get_device_by_identifier(self, identifier, entry_id):
            return ("new", identifier, entry_id)

        def async_get_device(self, identifiers):
            return ("old", identifiers)

    ident = ("blehome", "AA_0002")
    assert bc.async_get_registry_device(New(), ident, "e1") == ("new", ident, "e1")
    assert bc.async_get_registry_device(New(), ident, None) == ("old", {ident})


def test_registry_lookup_falls_back_on_older_cores(bc):
    class Old:
        def async_get_device(self, identifiers):
            return ("old", identifiers)

    ident = ("blehome", "AA_0002")
    assert bc.async_get_registry_device(Old(), ident, "e1") == ("old", {ident})
