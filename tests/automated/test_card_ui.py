"""Card UI tests: the replay card in Home Assistant's real frontend.

Home Assistant runs here with its real frontend, a vacuum entity, and fake
versions of the card's two WebSocket commands (the robot is not needed). A
headless Chrome, driven by ui/driver.mjs, opens it, and ui/page_tests.js
exercises the card with Home Assistant's own components, including the tile
card. Screenshots are saved for review.

The card file is served by the test itself, so the tests work on any branch,
whatever way that branch registers the card. They run in their own CI job
(OPENNEATO_UI_TESTS=1), which has Chrome and Node.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.core import ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

UI = Path(__file__).parent / "ui"
CARD_FILE = Path("custom_components/openneato/www/openneato-replay-card.js").resolve()
CARD_PATH = "/openneato_test/openneato-replay-card.js"
VACUUM = "vacuum.test_robot"

pytestmark = [
    pytest.mark.skipif(not CARD_FILE.is_file(), reason="This branch has no replay card"),
    pytest.mark.skipif(
        not os.environ.get("OPENNEATO_UI_TESTS"), reason="UI tests run in their own job"
    ),
    pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed"),
]

NOW = int(time.time())
# Newest first, as the integration sorts them. The last two were stamped
# before the bridge had real time: seconds since it booted, not a date.
SESSIONS = [
    {"name": f"{NOW - 3600}.jsonl", "time": NOW - 3600, "mode": "house", "area": 25.7},
    {"name": "20580.jsonl", "time": 20580, "mode": "house", "area": 19.9},
    {"name": "4020.jsonl", "time": 4020, "mode": "spot", "area": 3.2},
]


def _listing(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item["name"],
        "size": 1000,
        "recording": False,
        "session": {"time": item["time"], "mode": item["mode"], "battery": 90},
        "summary": {"areaCovered": item["area"]},
    }


def _session(item: dict[str, Any]) -> dict[str, Any]:
    """A small session: the robot cleans a 3 m by 2 m room in lines."""
    cell = 0.05
    path: list[float] = []
    coverage: list[float] = []
    ts = 0.0
    for row in range(5):
        y = 0.3 + row * 0.35
        xs = [0.3 + i * 0.1 for i in range(27)]
        for x in xs if row % 2 == 0 else reversed(xs):
            path += [round(x, 3), round(y, 3), 0.0 if row % 2 == 0 else 180.0, round(ts, 2)]
            for dx in (-2, 0, 2):
                for dy in (-2, 0, 2):
                    coverage += [round(x / cell) + dx, round(y / cell) + dy, round(ts, 2)]
            ts += 0.8
    return {
        "name": item["name"],
        "session": {"time": item["time"], "mode": item["mode"], "battery": 90},
        "summary": {
            "areaCovered": item["area"],
            "distanceTraveled": 142.0,
            "duration": int(ts),
            "batteryStart": 90,
            "batteryEnd": 84,
            "recharges": 0,
        },
        "bounds": {"minX": -0.2, "maxX": 3.4, "minY": -0.2, "maxY": 2.2},
        "cellSize": cell,
        "duration": ts,
        "recharges": [],
        "recording": False,
        "floorplan": None,
        "path": path,
        "coverage": coverage,
    }


@websocket_api.websocket_command(
    {vol.Required("type"): "openneato/sessions", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def _fake_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    calls = hass.data.setdefault("openneato_ws_calls", [])
    calls.append("sessions")
    connection.send_result(
        msg["id"],
        {"entry_id": "fake", "sessions": [_listing(i) for i in SESSIONS], "floorplan": None},
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "openneato/session", vol.Required("name"): str, vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def _fake_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    hass.data.setdefault("openneato_ws_calls", []).append("session")
    item = next((i for i in SESSIONS if i["name"] == msg["name"]), None)
    if item is None:
        connection.send_error(msg["id"], "not_found", "No such session")
        return
    connection.send_result(msg["id"], _session(item))


def _set_up_robot(hass: HomeAssistant) -> None:
    """The robot as the integration makes it: one device with a vacuum, sensors,
    switches and a select, and services that change them."""
    entry = MockConfigEntry(domain="openneato", data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("openneato", "test")}, name="Test Robot"
    )
    registry = er.async_get(hass)

    def add(domain: str, key: str, state: str, name: str, **attributes: Any) -> str:
        entity = registry.async_get_or_create(
            domain,
            "openneato",
            f"test_{key}",
            suggested_object_id=f"test_robot_{key}" if domain != "vacuum" else "test_robot",
            device_id=device.id,
            translation_key=key if domain != "vacuum" else None,
            config_entry=entry,
        )
        hass.states.async_set(
            entity.entity_id, state, {"friendly_name": f"Test Robot {name}".strip(), **attributes}
        )
        return entity.entity_id

    add(
        "vacuum", "robot", "docked", "",
        fan_speed="normal", fan_speed_list=["eco", "normal", "intense"],
        # The raw states the card leaves out of State content
        robot_state="ST_C_Standby", ui_state="UIMGR_STATE_STANDBY",
        supported_features=14140,
    )
    add("sensor", "battery_level", "85", "Battery level", unit_of_measurement="%", device_class="battery")
    add("binary_sensor", "charging", "on", "Charging", device_class="battery_charging")
    add("sensor", "wifi_signal", "-55", "WiFi signal", unit_of_measurement="dBm", device_class="signal_strength")
    add("sensor", "uptime", "29726", "Uptime", unit_of_measurement="s")
    # No error: the robot's error message is empty
    add("sensor", "error_message", "", "Error message")
    add("switch", "eco_mode", "off", "Eco mode")
    add("switch", "stealth_led", "on", "Stealth LED")
    add("switch", "schedule_monday_1_enabled", "off", "Schedule Monday 1 enabled")
    add("select", "navigation_mode", "normal", "Navigation mode", options=["normal", "gentle", "deep"])

    def change(entity_id: str, state: str | None = None, **attributes: Any) -> None:
        current = hass.states.get(entity_id)
        hass.states.async_set(
            entity_id, state if state is not None else current.state, {**current.attributes, **attributes}
        )

    def vacuum(state: str):
        async def handler(call: ServiceCall) -> None:
            change(VACUUM, state)

        return handler

    async def set_fan_speed(call: ServiceCall) -> None:
        change(VACUUM, fan_speed=call.data["fan_speed"])

    async def switch(call: ServiceCall) -> None:
        for entity_id in _entities(call):
            change(entity_id, "on" if call.service == "turn_on" else "off")

    async def select(call: ServiceCall) -> None:
        for entity_id in _entities(call):
            change(entity_id, call.data["option"])

    for service, state in (("start", "cleaning"), ("pause", "paused"), ("stop", "idle"),
                           ("return_to_base", "returning"), ("clean_spot", "cleaning"), ("locate", None)):
        hass.services.async_register("vacuum", service, vacuum(state) if state else _ignore)
    hass.services.async_register("vacuum", "set_fan_speed", set_fan_speed)
    hass.services.async_register("switch", "turn_on", switch)
    hass.services.async_register("switch", "turn_off", switch)
    hass.services.async_register("select", "select_option", select)


async def _ignore(call: ServiceCall) -> None:
    """A command with nothing to show for it."""


def _entities(call: ServiceCall) -> list[str]:
    ids = call.data.get("entity_id") or []
    return [ids] if isinstance(ids, str) else list(ids)


async def test_card_ui(
    hass: HomeAssistant,
    hass_storage: dict,
    hass_admin_user,
    hass_client,
) -> None:
    """The card works in Home Assistant's real frontend."""
    # Past onboarding, so the frontend opens the dashboard
    hass_storage["onboarding"] = {
        "version": 4,
        "minor_version": 1,
        "key": "onboarding",
        "data": {"done": ["user", "core_config", "analytics", "integration"]},
    }
    # A stored dashboard: only a dashboard page provides the card helpers
    # that load the tile card
    hass_storage["lovelace"] = {
        "version": 1,
        "minor_version": 1,
        "key": "lovelace",
        "data": {"config": {"views": [{"title": "Test", "path": "test", "cards": []}]}},
    }
    assert await async_setup_component(hass, "frontend", {})
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_PATH, str(CARD_FILE), False)])
    websocket_api.async_register_command(hass, _fake_sessions)
    websocket_api.async_register_command(hass, _fake_session)
    _set_up_robot(hass)
    await hass.async_block_till_done()

    client = await hass_client()
    url = str(client.make_url("/"))
    refresh = await hass.auth.async_create_refresh_token(
        hass_admin_user,
        client_name="OpenNeato card UI tests",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=1),
    )
    token = hass.auth.async_create_access_token(refresh)

    out = Path(os.environ.get("OPENNEATO_UI_OUT", "ui-results"))
    out.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(UI / "driver.mjs"),
        url,
        token,
        str(out),
        str(UI / "page_tests.js"),
        CARD_PATH,
        env={**os.environ, "OPENNEATO_CARD_FILE": str(CARD_FILE)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 300)
    print(stdout.decode(), stderr.decode())

    report = json.loads((out / "report.json").read_text())
    failed = [r for r in report if not r["ok"]]
    assert not failed, "\n\n".join(f"{r['name']}:\n{r['detail']}" for r in failed)
