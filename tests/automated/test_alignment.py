"""Tests for drawing a replay in the same frame as the floorplan (issue #22).

A run is recorded in the robot's frame of the day. The replay is corrected onto
the accumulated map with the run's alignment, which is only stored once the run
has been merged. These check that a live run gets its fit so far, and that a
run cached before its merge is parsed again once its alignment is known.

Skipped on a branch without the live alignment.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

lidar_runner = importlib.import_module("custom_components.openneato.lidar_runner")
lidar_mapper = importlib.import_module("custom_components.openneato.lidar_mapper")
websocket = importlib.import_module("custom_components.openneato.websocket")

if not hasattr(lidar_runner.LidarMapRunner, "async_live_alignment"):
    pytest.skip("This branch has no live alignment", allow_module_level=True)

DOMAIN = "openneato"
NAME = "1760051700.jsonl"

# An L-shaped room's walls, so each quarter turn of it is different
ROOM = {
    **{(x, 0): 3 for x in range(30)},
    **{(0, y): 3 for y in range(20)},
    **{(x, 19): 3 for x in range(15)},
    **{(29, y): 3 for y in range(10)},
    **{(15, y): 3 for y in range(10, 20)},
    **{(x, 10): 3 for x in range(15, 30)},
}


class FakeRunner:
    """The map runner, with the alignments a test sets."""

    def __init__(self) -> None:
        self.merged: tuple[int, int, int] | None = None
        self.live: tuple[int, int, int] | None = None

    def alignment(self, name: str) -> tuple[int, int, int] | None:
        return self.merged

    async def async_live_alignment(self, name: str) -> tuple[int, int, int] | None:
        return self.live


class FakeConnection:
    """Collects what the command sends back."""

    def __init__(self) -> None:
        self.results: list[Any] = []
        self.errors: list[Any] = []

    def send_result(self, msg_id: int, result: Any) -> None:
        self.results.append(result)

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        self.errors.append((code, message))

    def async_handle_exception(self, msg: dict, err: Exception) -> None:
        raise err


async def _get_session(hass: HomeAssistant, connection: FakeConnection) -> None:
    websocket.ws_get_session(hass, connection, {"id": 1, "type": "openneato/session", "name": NAME})
    await hass.async_block_till_done(wait_background_tasks=True)


def _set_up(hass: HomeAssistant, recording: bool) -> tuple[FakeRunner, list]:
    async def history(name: str) -> str:
        return "{}"

    runner = FakeRunner()
    hass.data[DOMAIN] = {
        "entry": {
            "api": SimpleNamespace(get_history_session=history),
            "coordinator": SimpleNamespace(data={"history": [{"name": NAME, "recording": recording}]}),
            "mapper": runner,
        }
    }
    built: list = []
    return runner, built


def _fake_build(built: list):
    def build(raw: str, name: str, align: Any) -> dict[str, Any]:
        built.append(align)
        return {"name": name, "path": [0.0, 0.0, 0.0, 0.0], "coverage": [], "bounds": None}

    return build


async def test_cached_session_parsed_again_once_its_alignment_is_known(hass: HomeAssistant) -> None:
    """A run fetched before its merge is not served unaligned from the cache."""
    runner, built = _set_up(hass, recording=False)
    connection = FakeConnection()
    with patch.object(websocket, "build_replay_session", _fake_build(built)):
        await _get_session(hass, connection)
        runner.merged = (1, 2, -3)
        await _get_session(hass, connection)
        await _get_session(hass, connection)
    assert built == [None, (1, 2, -3)], "parsed again once, with the alignment, then cached"
    assert connection.errors == []
    assert len(connection.results) == 3


async def test_recording_session_drawn_with_its_live_fit(hass: HomeAssistant) -> None:
    """A run still recording has not been merged, so it uses its fit so far."""
    runner, built = _set_up(hass, recording=True)
    runner.live = (3, 0, 1)
    connection = FakeConnection()
    with patch.object(websocket, "build_replay_session", _fake_build(built)):
        await _get_session(hass, connection)
    assert built == [(3, 0, 1)]


async def test_live_fit_finds_a_quarter_turned_run(hass: HomeAssistant) -> None:
    """A run recorded a quarter turn round is fitted back onto the map."""
    runner = lidar_runner.LidarMapRunner(hass, "entry", None, SimpleNamespace(async_add_listener=lambda *_: None))
    runner._map = lidar_mapper.AccumulatedMap({})
    runner._map.walls = dict(ROOM)
    runner._collecting = True
    runner._session_name = NAME
    runner._captures = [(0.0, 0.0, 0.0, [], 5.0)] * lidar_runner.LIVE_ALIGN_MIN_CAPTURES
    # The run's walls as the robot saw them: the room turned three quarters
    turned = {cell: 1 for cell in lidar_mapper._rotate_cells(ROOM, 3)}
    fits: list = []

    def grids(captures: list) -> tuple[dict, set]:
        fits.append(len(captures))
        return turned, set()

    with patch.object(lidar_runner, "build_session_grids", grids):
        first = await runner.async_live_alignment(NAME)
        again = await runner.async_live_alignment(NAME)
    assert first == (1, 0, 0), "turned back a quarter, with no shift"
    assert again == first and len(fits) == 1, "worked out at most once a minute"
    assert await runner.async_live_alignment("another.jsonl") is None, "only for the run being recorded"


async def test_no_live_fit_before_enough_scans(hass: HomeAssistant) -> None:
    """Too few scans to fit yet: the run is drawn as recorded."""
    runner = lidar_runner.LidarMapRunner(hass, "entry", None, SimpleNamespace(async_add_listener=lambda *_: None))
    runner._map = lidar_mapper.AccumulatedMap({})
    runner._map.walls = dict(ROOM)
    runner._collecting = True
    runner._session_name = NAME
    runner._captures = [(0.0, 0.0, 0.0, [], 5.0)] * (lidar_runner.LIVE_ALIGN_MIN_CAPTURES - 1)
    assert await runner.async_live_alignment(NAME) is None


async def test_finished_run_keeps_its_live_fit_until_merged(hass: HomeAssistant) -> None:
    """Between the end of a clean and its merge, the run keeps its live fit."""
    runner = lidar_runner.LidarMapRunner(hass, "entry", None, SimpleNamespace(async_add_listener=lambda *_: None))
    runner._map = lidar_mapper.AccumulatedMap({})
    runner._map.walls = dict(ROOM)
    runner._collecting = True
    runner._session_name = NAME
    runner._live_align = (1, -6, -3)
    # Too few captures to merge: the run ends unmerged, as it is in the gap before a merge
    runner._captures = []
    await runner._finish()
    assert runner.alignment(NAME) == (1, -6, -3)
