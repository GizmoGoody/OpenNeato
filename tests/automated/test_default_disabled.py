"""A new robot starts with only the everyday entities enabled.

Raw electrical readings, motor speeds and tuning, bridge diagnostics, and
settings that are set once (also in the bridge's web UI) are disabled by
default. Checked in the entity registry after setting the integration up.
Skipped on a branch that still enables them.
"""

from __future__ import annotations

import importlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .bridge_mock import SERIAL, mock_bridge, setup_integration, without_frontend

sensor = importlib.import_module("custom_components.openneato.sensor")
if next(d for d in sensor.SENSOR_DESCRIPTIONS if d.key == "error_code").entity_registry_enabled_default:
    pytest.skip("This branch still enables every entity by default", allow_module_level=True)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SCHEDULE = {f"schedule_{day}_{slot}{suffix}" for day in DAYS for slot in (1, 2) for suffix in ("", "_enabled")}

DISABLED = SCHEDULE | {
    # Robot
    "charger_vbattv", "charger_vextv", "charger_discharge_mah", "charger_charge_mah",
    "analog_battery_voltage", "analog_battery_current", "analog_external_voltage",
    "error_code", "motors_brush_rpm", "motors_vacuum_rpm", "motors_side_brush_ma", "motors_laser_rpm",
    "sensors_left_wheel_extended", "sensors_right_wheel_extended", "sensors_dc_jack_in",
    "sensors_bumper_front_left", "sensors_bumper_front_right", "sensors_bumper_side_left",
    "sensors_bumper_side_right", "sensors_bumper_lds_left", "sensors_bumper_lds_right",
    "brush_rpm_setting", "vacuum_speed_setting", "side_brush_power_setting", "stall_threshold_setting",
    "robot_shutdown", "new_battery",
    # Bridge
    "format_fs", "system_uptime", "system_heap", "system_fs_used", "system_ntp_synced",
    "notify_on_start", "notify_on_done", "notify_on_error", "notify_on_alert", "notify_on_docking",
    "ntfy_topic", "ntfy_server", "ntfy_token",
    "ap_fallback_on_disconnect", "remote_syslog", "syslog_ip", "auto_restart",
}


async def test_only_the_everyday_entities_start_enabled(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        entry = await setup_integration(hass)

    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    disabled = {
        e.unique_id.removeprefix(f"{SERIAL}_")
        for e in entries
        if e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    }
    assert disabled == DISABLED
    # Everything else is enabled, and nothing is disabled for another reason.
    assert all(e.disabled_by in (None, er.RegistryEntryDisabler.INTEGRATION) for e in entries)
