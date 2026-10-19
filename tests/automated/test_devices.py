"""Each robot is two devices: the robot, and the bridge it is connected via.

The robot's device carries Neato's firmware and the robot's own controls;
the bridge's device carries the OpenNeato firmware, the board, and the
bridge's own settings. Skipped on a branch that still has one device.
"""

from __future__ import annotations

import importlib
import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

entity = importlib.import_module("custom_components.openneato.entity")
if not hasattr(entity, "build_devices"):
    pytest.skip("This branch has one device per robot", allow_module_level=True)

DOMAIN = "openneato"
SERIAL = "OPS18218,74e18290a4f4,P"

ROBOT_INFO = {"serialNumber": SERIAL, "modelName": "BotVacD7Connected", "softwareVersion": "4.5.3.189.0"}
FIRMWARE_INFO = {
    "version": "1.16.0",
    "chip": "ESP32-C3",
    "board": "seeed-xiao-esp32c3",
    "boardName": "XIAO ESP32C3",
    "boardMaker": "Seeed Studio",
    "mac": "AA:BB:CC:DD:EE:FF",
    "hostname": "Alfred",
}
# Firmware 1.15.0 and older report none of the board fields.
OLD_FIRMWARE_INFO = {"version": "1.15.0", "chip": "ESP32-C3", "hostname": "Alfred"}

BRIDGE_KEYS = {
    "binary_sensor": {"system_ntp_synced"},
    "button": {"restart", "format_fs"},
    "sensor": {"system_rssi", "system_uptime", "system_heap", "system_fs_used"},
    "switch": {
        "notifications_enabled", "notify_on_start", "notify_on_done", "notify_on_error",
        "notify_on_alert", "notify_on_docking", "ap_fallback_on_disconnect", "remote_syslog", "auto_restart",
    },
    "text": {"syslog_ip", "ntfy_topic", "ntfy_server", "ntfy_token"},
}


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


def test_the_robot_device() -> None:
    robot = entity.build_devices(SERIAL, ROBOT_INFO, FIRMWARE_INFO, "192.168.20.142").robot
    assert robot["identifiers"] == {(DOMAIN, SERIAL)}
    assert robot["name"] == "Alfred"
    assert robot["manufacturer"] == "Neato Robotics"
    assert robot["model"] == "Botvac D7 Connected"
    assert robot["model_id"] == "BotVacD7Connected"
    assert robot["sw_version"] == "4.5.3.189.0"
    assert robot["serial_number"] == SERIAL
    assert robot["via_device"] == (DOMAIN, f"{SERIAL}_bridge")
    assert robot["configuration_url"] == "http://192.168.20.142"


def test_the_bridge_device() -> None:
    bridge = entity.build_devices(SERIAL, ROBOT_INFO, FIRMWARE_INFO, "192.168.20.142").bridge
    assert bridge["identifiers"] == {(DOMAIN, f"{SERIAL}_bridge")}
    assert bridge["name"] == "Alfred bridge"
    assert bridge["manufacturer"] == "Seeed Studio"
    assert bridge["model"] == "XIAO ESP32C3"
    assert bridge["model_id"] == "seeed-xiao-esp32c3"
    assert bridge["hw_version"] == "ESP32-C3"
    assert bridge["sw_version"] == "1.16.0"
    assert bridge["connections"] == {(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")}
    assert bridge["configuration_url"] == "http://192.168.20.142"


def test_older_firmware_leaves_the_board_fields_empty() -> None:
    bridge = entity.build_devices(SERIAL, ROBOT_INFO, OLD_FIRMWARE_INFO, "bridge").bridge
    assert bridge["sw_version"] == "1.15.0"
    assert bridge["manufacturer"] is None
    assert bridge["model"] is None
    assert bridge["model_id"] is None
    assert "connections" not in bridge


def test_without_a_hostname_the_names_fall_back() -> None:
    devices = entity.build_devices(SERIAL, ROBOT_INFO, {"version": "1.16.0"}, "bridge")
    assert devices.robot["name"] == "Botvac D7 Connected"
    assert devices.bridge["name"] == "OpenNeato bridge"


async def test_the_robot_is_connected_via_the_bridge(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "bridge"})
    entry.add_to_hass(hass)
    devices = entity.build_devices(SERIAL, ROBOT_INFO, FIRMWARE_INFO, "bridge")
    registry = dr.async_get(hass)
    # Setup registers the bridge's device; the entities create the robot's,
    # and Home Assistant resolves its via_device to the bridge's device.
    bridge = registry.async_get_or_create(config_entry_id=entry.entry_id, **devices.bridge)
    assert devices.robot["via_device"] in bridge.identifiers
    assert bridge.via_device_id is None


@pytest.mark.parametrize(
    "platform",
    ["binary_sensor", "button", "number", "select", "sensor", "switch", "text", "time", "vacuum"],
)
async def test_each_entity_sits_on_the_device_it_acts_on(hass: HomeAssistant, platform: str) -> None:
    """Run the platform's own setup and check every entity's device."""
    module = importlib.import_module(f"custom_components.openneato.{platform}")
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "bridge"})
    entry.add_to_hass(hass)
    coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__), config_entry=entry, name="test")
    coordinator.data = {}
    devices = entity.build_devices(SERIAL, ROBOT_INFO, FIRMWARE_INFO, "bridge")
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "serial": SERIAL,
            "devices": devices,
            "api": object(),
            "coordinator": coordinator,
            "host": "bridge",
        }
    }
    added: list = []
    await module.async_setup_entry(hass, entry, lambda entities, *args, **kwargs: added.extend(entities))
    assert added

    bridge_keys = BRIDGE_KEYS.get(platform, set())
    seen_bridge = set()
    for item in added:
        key = item.entity_description.key if hasattr(item, "entity_description") else item.unique_id
        if item.device_info is devices.bridge:
            seen_bridge.add(key)
        else:
            assert item.device_info is devices.robot, key
    assert seen_bridge == bridge_keys
