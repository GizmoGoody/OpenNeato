"""Tests for drawing a replay in the same frame as the floorplan (issue #22).

A clean is recorded in the robot's frame of the day. Its replay is drawn in the
map's frame with the clean's alignment onto the map, which LidarMapRunner.resolve
decides in one place. Skipped on a branch without it.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

lidar_runner = importlib.import_module("custom_components.openneato.lidar_runner")
lidar_mapper = importlib.import_module("custom_components.openneato.lidar_mapper")
websocket = importlib.import_module("custom_components.openneato.websocket")

if not hasattr(lidar_runner.LidarMapRunner, "resolve"):
    pytest.skip("This branch has no single alignment decision", allow_module_level=True)

DOMAIN = "openneato"
NAME = "1791675925.jsonl"

# An L-shaped room's walls, so each quarter turn of it is different
ROOM = {
    **{(x, 0): 3 for x in range(30)},
    **{(0, y): 3 for y in range(20)},
    **{(x, 19): 3 for x in range(15)},
    **{(29, y): 3 for y in range(10)},
    **{(15, y): 3 for y in range(10, 20)},
    **{(x, 10): 3 for x in range(15, 30)},
}
# The room as the robot saw it in a frame a quarter turn round: turning it a
# quarter (alignment (1, 0, 0)) puts it back on the map
TURNED = {cell: 1 for cell in lidar_mapper._rotate_cells(ROOM, 3)}


def _runner(hass: HomeAssistant, alignments: dict | None = None) -> Any:
    runner = lidar_runner.LidarMapRunner(
        hass, "entry", None, SimpleNamespace(async_add_listener=lambda *_: None, data={})
    )
    runner._map = lidar_mapper.AccumulatedMap({"alignments": alignments or {}})
    runner._map.walls = dict(ROOM)
    runner._store = SimpleNamespace(async_save=AsyncMock())
    return runner


def _recording(runner: Any, name: str = NAME) -> None:
    runner._start()
    runner._stop_timer()
    runner._session_name = name


# ── the stored map ──────────────────────────────────────────────────────


def test_the_name_with_or_without_its_suffix_is_the_same_clean() -> None:
    for name in ("1791675925.jsonl", "1791675925.jsonl.hs", "1791675925"):
        assert lidar_mapper.run_key(name) == "1791675925"


def test_alignments_stored_by_earlier_versions_read_as_merged() -> None:
    stored = lidar_mapper.AccumulatedMap({"alignments": {"99.jsonl": [0, 0, 0], "4030.jsonl": [1, -2, -1]}})
    assert stored.alignment_of("4030.jsonl.hs") == ((1, -2, -1), "merged")
    assert stored.latest_merged() == (1, -2, -1)
    saved = stored.as_dict()["alignments"]
    assert saved["4030"] == {"align": [1, -2, -1], "kind": "merged"}
    assert lidar_mapper.AccumulatedMap({"alignments": saved}).alignment_of("4030") == ((1, -2, -1), "merged")


def test_the_latest_merged_alignment_skips_a_fitted_one() -> None:
    stored = lidar_mapper.AccumulatedMap({})
    stored.record("1.jsonl", (1, -6, -3), "merged")
    stored.record("2.jsonl", (2, 0, 0), "fitted")
    assert stored.latest_merged() == (1, -6, -3)
    stored.record("3.jsonl.hs", (1, 3, 2), "merged")
    assert stored.latest_merged() == (1, 3, 2)


# ── fitting ─────────────────────────────────────────────────────────────


def test_a_fit_near_the_guess_wins_over_a_false_whole_map_match() -> None:
    """One real clean's first scans matched at 0 degrees, (17, 39) cells, 99%."""
    assert lidar_mapper.align_near(TURNED, ROOM, (1, -6, -3)) == (1, 0, 0, 1.0)
    with patch.object(lidar_mapper, "align_to_reference", lambda new, ref: (0, 17, 39, 0.99)):
        assert lidar_mapper.best_alignment(TURNED, ROOM, (1, -6, -3))[:3] == (1, 0, 0)
        assert lidar_mapper.best_alignment(TURNED, ROOM, None)[:3] == (0, 17, 39), "no guess, the whole-map fit"


def test_a_merge_prefers_the_fit_near_the_last_merged_alignment() -> None:
    stored = lidar_mapper.AccumulatedMap({})
    stored.walls = dict(ROOM)
    stored.sessions = 1
    with patch.object(lidar_mapper, "align_to_reference", lambda new, ref: (0, 17, 39, 0.99)):
        stored.merge_session(TURNED, set(), NAME, (1, -6, -3))
    assert stored.alignment_of(NAME + ".hs") == ((1, 0, 0), "merged")


async def test_a_recording_clean_is_fitted_by_the_mappers_own_timer(hass: HomeAssistant) -> None:
    runner = _runner(hass, {"1791669270.jsonl": [1, -6, -3]})
    _recording(runner)
    runner._captures = [(0.0, 0.0, 0.0, [], 5.0)] * (lidar_runner.LIVE_ALIGN_MIN_CAPTURES - 1)
    with patch.object(lidar_runner, "build_session_grids", lambda captures: (TURNED, set())):
        await runner._async_fit()
        assert runner._fit is None, "fitted before enough scans"
        runner._captures.append((0.0, 0.0, 0.0, [], 5.0))
        await runner._async_fit()
    assert runner._fit == (1, 0, 0)


# ── what a replay is drawn with ─────────────────────────────────────────


async def test_what_a_clean_is_drawn_with_in_every_case(hass: HomeAssistant) -> None:
    """LidarMapRunner.resolve, the one place it is decided."""
    runner = _runner(hass, {"1791669270.jsonl": [1, -6, -3], "1791670000.jsonl": [2, 0, 0]})
    runner._map.alignments["1791670000"]["kind"] = "fitted"
    # A merged clean, under either name
    assert runner.resolve("1791669270.jsonl.hs") == ((1, -6, -3), "merged")
    assert runner.resolve("1791669270.jsonl") == ((1, -6, -3), "merged")
    # A clean that was not merged: its own fit
    assert runner.resolve("1791670000.jsonl.hs") == ((2, 0, 0), "fitted")
    # A clean with no record: the last merged clean's
    assert runner.resolve("4030.jsonl.hs") == ((1, -6, -3), "previous")
    # Recording, before its own fit: the last merged clean's
    _recording(runner)
    assert runner.resolve(NAME, recording=True) == ((1, -6, -3), "previous")
    # Recording, with its own fit
    runner._fit = (1, 3, 2)
    assert runner.resolve(NAME, recording=True) == ((1, 3, 2), "live")
    # No map at all: the raw frame
    empty = lidar_runner.LidarMapRunner(hass, "entry", None, SimpleNamespace(async_add_listener=lambda *_: None))
    assert empty.resolve(NAME) == (None, "none")


async def test_a_clean_too_short_to_merge_keeps_its_own_fit_across_a_restart(hass: HomeAssistant) -> None:
    runner = _runner(hass, {"1791669270.jsonl": [1, -6, -3]})
    _recording(runner)
    runner._fit = (1, 3, 2)
    runner._captures = []
    await runner._finish()
    runner._store.async_save.assert_awaited()
    saved = runner._store.async_save.await_args.args[0]
    assert saved["alignments"]["1791675925"] == {"align": [1, 3, 2], "kind": "fitted"}
    restarted = lidar_mapper.AccumulatedMap(saved)
    assert restarted.alignment_of(NAME + ".hs") == ((1, 3, 2), "fitted")


async def test_a_merged_clean_replaces_its_fit_with_the_merge(hass: HomeAssistant) -> None:
    runner = _runner(hass, {"1791669270.jsonl": [1, -6, -3]})
    runner._map.sessions = 1
    _recording(runner)
    runner._fit = (1, 2, 2)
    runner._captures = [(0.0, 0.0, 0.0, [], 5.0)] * lidar_runner.MIN_CAPTURES
    with patch.object(lidar_runner, "build_session_grids", lambda captures: (TURNED, set())):
        await runner._finish()
    assert runner.resolve(NAME + ".hs") == ((1, 0, 0), "merged")


# ── the replay's WebSocket command ──────────────────────────────────────


class FakeRunner:
    """The map runner, answering resolve with what a test sets."""

    def __init__(self) -> None:
        self.answer: tuple[Any, str] = (None, "none")
        self.asked: list[tuple[str, bool]] = []

    def resolve(self, name: str, recording: bool = False) -> tuple[Any, str]:
        self.asked.append((name, recording))
        return self.answer


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
    return runner, []


def _fake_build(built: list):
    def build(raw: str, name: str, align: Any) -> dict[str, Any]:
        built.append(align)
        return {"name": name, "path": [0.0, 0.0, 0.0, 0.0], "coverage": [], "bounds": None}

    return build


async def test_a_cached_clean_is_parsed_again_when_its_alignment_changes(hass: HomeAssistant) -> None:
    """Fetched in the minute before its merge, a clean is not kept on the old alignment."""
    runner, built = _set_up(hass, recording=False)
    runner.answer = ((1, -6, -3), "previous")
    connection = FakeConnection()
    with patch.object(websocket, "build_replay_session", _fake_build(built)):
        await _get_session(hass, connection)
        runner.answer = ((1, 3, 2), "merged")
        await _get_session(hass, connection)
        await _get_session(hass, connection)
    assert built == [(1, -6, -3), (1, 3, 2)], "parsed again once, then cached"
    assert [r["alignment_source"] for r in connection.results] == ["previous", "merged", "merged"]


async def test_a_recording_clean_is_resolved_as_recording(hass: HomeAssistant) -> None:
    runner, built = _set_up(hass, recording=True)
    runner.answer = ((1, 3, 2), "live")
    connection = FakeConnection()
    with patch.object(websocket, "build_replay_session", _fake_build(built)):
        await _get_session(hass, connection)
    assert runner.asked[-1] == (NAME, True)
    assert built == [(1, 3, 2)]
    assert connection.results[-1]["alignment_source"] == "live"
