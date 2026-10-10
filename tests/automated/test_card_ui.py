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
from homeassistant.setup import async_setup_component

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
    hass.states.async_set(VACUUM, "docked", {"friendly_name": "Test Robot", "fan_speed": "normal", "fan_speed_list": ["eco", "normal", "intense"]})
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
