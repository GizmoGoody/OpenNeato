"""A mocked OpenNeato bridge and fork releases, for setting up the whole integration.

Tests set the integration up through its config entry, as Home Assistant's
testing guidelines ask, with every HTTP call answered by aioclient_mock. The
dashboard card and the replay card's WebSocket API are patched out: they need
Home Assistant's frontend package, which the Tests job does not install (the
Card UI job covers them).
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "openneato"
HOST = "bridge"
SERIAL = "OPS18218,74e18290a4f4,P"
RELEASES_API = "https://api.github.com/repos/Leicas/OpenNeato/releases?per_page=10"
DOWNLOAD = "https://github.com/Leicas/OpenNeato/releases/download"
XIAO = "openneato-seeed-xiao-esp32c3-firmware.bin"
FIRMWARE = b"\xe9\x05\x02\x20" + bytes(range(256)) * 8

ROBOT_INFO = {"serialNumber": SERIAL, "modelName": "BotVacD7Connected", "softwareVersion": "4.5.3.189.0"}
FIRMWARE_INFO = {
    "name": "OpenNeato",
    "version": "1.15.0",
    "chip": "ESP32-C3",
    "board": "seeed-xiao-esp32c3",
    "boardName": "XIAO ESP32C3",
    "boardMaker": "Seeed Studio",
    "mac": "AA:BB:CC:DD:EE:FF",
    "hostname": "Alfred",
    "repositoryUrl": "https://github.com/Leicas/OpenNeato",
}
# What firmware 1.15.0 and older report: none of the board fields.
OLD_FIRMWARE_INFO = {
    key: FIRMWARE_INFO[key] for key in ("name", "version", "chip", "hostname", "repositoryUrl")
}

# Sections the coordinator polls; empty answers are enough for setup.
SECTIONS = (
    "state", "charger", "error", "user-settings", "system", "settings",
    "motors", "sensors", "analog", "warranty", "schedule/next",
)


def release(tag: str, *names: str, **extra: Any) -> dict[str, Any]:
    """A GitHub release as the releases API lists it."""
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/Leicas/OpenNeato/releases/tag/{tag}",
        "body": f"Notes for {tag}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "browser_download_url": f"{DOWNLOAD}/{tag}/{name}"} for name in names
        ],
        **extra,
    }


# Newest first, as GitHub lists them: a prerelease, an integration-only
# release, then the newest release with firmware for every board.
RELEASES = [
    release("v1.18.0-rc.1", XIAO, "checksums.txt", prerelease=True),
    release("v1.17.0"),
    release("v1.16.0", "openneato-esp32-c3-firmware.bin", XIAO, "checksums.txt"),
    release("v1.15.0", "openneato-esp32-c3-firmware.bin", XIAO, "checksums.txt"),
]


def checksums(firmware: bytes = FIRMWARE) -> bytes:
    """checksums.txt for v1.16.0, listing the XIAO file with this content's hash."""
    other = hashlib.sha256(b"generic c3").hexdigest()
    mine = hashlib.sha256(firmware).hexdigest()
    return f"{other}  openneato-esp32-c3-firmware.bin\n{mine}  {XIAO}\n".encode()


def mock_bridge(
    aioclient_mock,
    firmware_info: dict[str, Any] | None = None,
    releases: list[dict[str, Any]] | None = None,
    releases_status: int = 200,
) -> None:
    """Answer every request the integration makes to the bridge and to GitHub."""
    base = f"http://{HOST}/api"
    aioclient_mock.get(f"{base}/firmware/version", json=firmware_info or FIRMWARE_INFO)
    aioclient_mock.get(f"{base}/version", json=ROBOT_INFO)
    aioclient_mock.get(f"{base}/history", json=[])
    for section in SECTIONS:
        aioclient_mock.get(f"{base}/{section}", json={})
    if releases_status == 200:
        aioclient_mock.get(RELEASES_API, json=RELEASES if releases is None else releases)
    else:
        aioclient_mock.get(RELEASES_API, status=releases_status)


@contextmanager
def without_frontend(hass: HomeAssistant):
    """Skip the card and its WebSocket API, which need the frontend package."""
    hass.config.components.update({"frontend", "http", "websocket_api"})
    with (
        patch("custom_components.openneato.async_register_card", AsyncMock()),
        patch("custom_components.openneato._async_register_frontend", AsyncMock()),
    ):
        yield


async def setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    """Add the bridge as a config entry and set it up, waiting for background work."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        title="BotVacD7Connected",
        data={"host": HOST, "serial": SERIAL, "model": "BotVacD7Connected"},
        # The LIDAR floor-plan builder is not under test here.
        options={"lidar_map_enabled": False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry
