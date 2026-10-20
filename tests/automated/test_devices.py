"""Each robot is two devices: the robot, and the bridge it is connected via.

The robot's device carries Neato's firmware and the robot's own controls;
the bridge's device carries the OpenNeato firmware, the board, and the
bridge's own settings. The integration is set up through its config entry
and checked in the device and entity registries. Skipped on a branch that
still has one device per robot.
"""

from __future__ import annotations

import importlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .bridge_mock import (
    DOMAIN,
    OLD_FIRMWARE_INFO,
    SERIAL,
    mock_bridge,
    setup_integration,
    without_frontend,
)

entity = importlib.import_module("custom_components.openneato.entity")
if not hasattr(entity, "build_devices"):
    pytest.skip("This branch has one device per robot", allow_module_level=True)

BRIDGE_KEYS = {
    "system_ntp_synced",
    "restart", "format_fs",
    "system_rssi", "system_uptime", "system_heap", "system_fs_used",
    "notifications_enabled", "notify_on_start", "notify_on_done", "notify_on_error",
    "notify_on_alert", "notify_on_docking", "ap_fallback_on_disconnect", "remote_syslog", "auto_restart",
    "syslog_ip", "ntfy_topic", "ntfy_server", "ntfy_token",
}
try:
    importlib.import_module("custom_components.openneato.update")
    BRIDGE_KEYS.add("firmware")
except ModuleNotFoundError:
    pass


def _devices(hass: HomeAssistant, entry) -> tuple[dr.DeviceEntry, dr.DeviceEntry]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 2
    robot = next(d for d in devices if (DOMAIN, SERIAL) in d.identifiers)
    bridge = next(d for d in devices if (DOMAIN, f"{SERIAL}_bridge") in d.identifiers)
    return robot, bridge


@pytest.mark.parametrize(
    ("raw", "readable"),
    [
        ("BotVacD7Connected", "Botvac D7 Connected"),
        ("BotVacD5", "Botvac D5"),
        ("BotVacConnected", "Botvac Connected"),
        (None, None),
    ],
)
def test_readable_model(raw: str | None, readable: str | None) -> None:
    assert entity.readable_model(raw) == readable


async def test_the_robot_is_connected_via_the_bridge(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        entry = await setup_integration(hass)
    robot, bridge = _devices(hass, entry)

    assert robot.via_device_id == bridge.id
    assert robot.name == "Alfred"
    assert robot.manufacturer == "Neato Robotics"
    assert robot.model == "Botvac D7 Connected"
    assert robot.model_id == "BotVacD7Connected"
    assert robot.sw_version == "4.5.3.189.0"
    assert robot.serial_number == SERIAL
    assert robot.configuration_url == "http://bridge"

    assert bridge.via_device_id is None
    assert bridge.name == "Alfred bridge"
    assert bridge.manufacturer == "Seeed Studio"
    assert bridge.model == "XIAO ESP32C3"
    assert bridge.model_id == "seeed-xiao-esp32c3"
    assert bridge.hw_version == "ESP32-C3"
    assert bridge.sw_version == "1.15.0"
    assert bridge.connections == {(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")}
    assert bridge.configuration_url == "http://bridge"


async def test_each_entity_sits_on_the_device_it_acts_on(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        entry = await setup_integration(hass)
    robot, bridge = _devices(hass, entry)

    on_bridge = set()
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        key = item.unique_id.removeprefix(f"{SERIAL}_")
        if item.device_id == bridge.id:
            on_bridge.add(key)
        else:
            assert item.device_id == robot.id, key
    assert on_bridge == BRIDGE_KEYS


async def test_older_firmware_leaves_the_board_fields_empty(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock, firmware_info=OLD_FIRMWARE_INFO)
    with without_frontend(hass):
        entry = await setup_integration(hass)
    robot, bridge = _devices(hass, entry)

    assert robot.via_device_id == bridge.id
    assert bridge.sw_version == "1.15.0"
    assert bridge.manufacturer is None
    assert bridge.model is None
    assert bridge.model_id is None
    assert bridge.connections == set()


def test_without_a_hostname_the_names_fall_back() -> None:
    robot_info = {"serialNumber": SERIAL, "modelName": "BotVacD7Connected"}
    devices = entity.build_devices(SERIAL, robot_info, {"version": "1.16.0"}, "bridge")
    assert devices.robot["name"] == "Botvac D7 Connected"
    assert devices.bridge["name"] == "OpenNeato bridge"
