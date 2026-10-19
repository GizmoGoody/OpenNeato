"""The firmware update entity finds, verifies and installs the bridge firmware.

Releases are read from the fork's GitHub releases; the board's file is picked
by the releaseId the bridge reports, checked against checksums.txt, and sent
to the bridge's upload endpoint. Skipped on a branch without the update
platform.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from typing import Any
from unittest.mock import Mock

import pytest

try:
    update = importlib.import_module("custom_components.openneato.update")
    firmware_release = importlib.import_module("custom_components.openneato.firmware_release")
except ModuleNotFoundError:
    pytest.skip("This branch has no firmware update entity", allow_module_level=True)

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

api_module = importlib.import_module("custom_components.openneato.api")
const = importlib.import_module("custom_components.openneato.const")

DOWNLOAD = "https://github.com/Leicas/OpenNeato/releases/download"
XIAO = "openneato-seeed-xiao-esp32c3-firmware.bin"
FIRMWARE = b"\xe9\x05\x02\x20" + bytes(range(256)) * 8


def _asset(tag: str, name: str) -> dict[str, str]:
    return {"name": name, "browser_download_url": f"{DOWNLOAD}/{tag}/{name}"}


def _release(tag: str, *names: str, **extra: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/Leicas/OpenNeato/releases/tag/{tag}",
        "body": f"Notes for {tag}",
        "draft": False,
        "prerelease": False,
        "assets": [_asset(tag, name) for name in names],
        **extra,
    }


# Newest first, as GitHub lists them: a prerelease, an integration-only
# release, then the newest release with firmware for every board.
RELEASES = [
    _release("v1.18.0-rc.1", XIAO, "checksums.txt", prerelease=True),
    _release("v1.17.0"),
    _release("v1.16.0", "openneato-esp32-c3-firmware.bin", XIAO, "checksums.txt"),
    _release("v1.15.0", "openneato-esp32-c3-firmware.bin", XIAO, "checksums.txt"),
]


def _checksums(firmware: bytes = FIRMWARE) -> bytes:
    other = hashlib.sha256(b"generic c3").hexdigest()
    mine = hashlib.sha256(firmware).hexdigest()
    return f"{other}  openneato-esp32-c3-firmware.bin\n{mine}  {XIAO}\n".encode()


# -- Picking the release -------------------------------------------------------


def test_the_newest_published_release_with_firmware_is_picked() -> None:
    release = firmware_release.pick_release(RELEASES, "seeed-xiao-esp32c3")
    assert release.version == "1.16.0"
    assert release.firmware_url == f"{DOWNLOAD}/v1.16.0/{XIAO}"
    assert release.checksums_url == f"{DOWNLOAD}/v1.16.0/checksums.txt"
    assert release.notes == "Notes for v1.16.0"


def test_a_bridge_without_release_id_is_told_but_cannot_install() -> None:
    release = firmware_release.pick_release(RELEASES, None)
    assert release.version == "1.16.0"
    assert release.firmware_url is None


def test_a_board_missing_from_the_release_cannot_install() -> None:
    release = firmware_release.pick_release(RELEASES, "esp32-s3")
    assert release.version == "1.16.0"
    assert release.firmware_url is None


def test_no_release_with_firmware() -> None:
    assert firmware_release.pick_release([_release("v1.17.0")], "esp32-c3") is None


def test_checksums_are_parsed_by_file_name() -> None:
    parsed = firmware_release.parse_checksums(_checksums().decode())
    assert parsed[XIAO] == hashlib.sha256(FIRMWARE).hexdigest()
    assert len(parsed) == 2


# -- The entity ------------------------------------------------------------------


class FakeBridge:
    """Stands in for OpenNeatoApiClient: records the upload, then reports a version."""

    base_url = "http://bridge"

    def __init__(self, version_after: str = "1.16.0", refuse: str | None = None) -> None:
        self.version_after = version_after
        self.refuse = refuse
        self.uploads: list[tuple[bytes, str, str]] = []

    async def upload_firmware(self, firmware: bytes, filename: str, md5: str) -> None:
        if self.refuse:
            raise api_module.OpenNeatoApiError(f"The bridge refused the firmware: {self.refuse}", status=400)
        self.uploads.append((firmware, filename, md5))

    async def get_firmware_version(self) -> dict[str, Any]:
        return {"version": self.version_after}


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "FIRMWARE_REBOOT_POLL", 0)
    monkeypatch.setattr(update, "FIRMWARE_REBOOT_TIMEOUT", 0.2)


def _entity(hass: HomeAssistant, bridge: FakeBridge, release_id: str | None = "seeed-xiao-esp32c3"):
    entry = MockConfigEntry(domain=const.DOMAIN, data={"host": "bridge"})
    entry.add_to_hass(hass)
    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), config_entry=entry, name="test"
    )
    info = {"version": "1.15.0", "chip": "ESP32-C3"}
    if release_id:
        info["releaseId"] = release_id
    entity = update.OpenNeatoFirmwareUpdate(coordinator, entry, bridge, "SERIAL", info)
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    hass.config_entries.async_schedule_reload = Mock()
    return entity, entry


async def test_releases_are_read_from_github(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    entity, _ = _entity(hass, FakeBridge())
    await entity._async_check_releases()
    assert entity.installed_version == "1.15.0"
    assert entity.latest_version == "1.16.0"
    assert entity.release_url.endswith("/v1.16.0")
    assert entity.supported_features & UpdateEntityFeature.INSTALL
    assert await entity.async_release_notes() == "Notes for v1.16.0"


async def test_without_release_id_install_is_not_offered(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    entity, _ = _entity(hass, FakeBridge(), release_id=None)
    await entity._async_check_releases()
    assert entity.latest_version == "1.16.0"
    assert not entity.supported_features & UpdateEntityFeature.INSTALL
    with pytest.raises(HomeAssistantError):
        await entity.async_install(None, False)


async def test_github_unreachable_keeps_the_last_answer(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    entity, _ = _entity(hass, FakeBridge())
    await entity._async_check_releases()
    aioclient_mock.clear_requests()
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, status=503)
    await entity._async_check_releases()
    assert entity.latest_version == "1.16.0"


async def test_install_flashes_the_verified_board_file(hass: HomeAssistant, aioclient_mock, no_wait) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/{XIAO}", content=FIRMWARE)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/checksums.txt", content=_checksums())
    bridge = FakeBridge()
    entity, entry = _entity(hass, bridge)
    await entity._async_check_releases()

    await entity.async_install("1.16.0", False)

    assert bridge.uploads == [(FIRMWARE, XIAO, hashlib.md5(FIRMWARE).hexdigest())]
    assert entity.in_progress is False
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)


async def test_a_file_that_fails_the_checksum_is_not_flashed(hass: HomeAssistant, aioclient_mock, no_wait) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/{XIAO}", content=FIRMWARE[:-1])
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/checksums.txt", content=_checksums())
    bridge = FakeBridge()
    entity, _ = _entity(hass, bridge)
    await entity._async_check_releases()

    with pytest.raises(HomeAssistantError, match="does not match checksums.txt"):
        await entity.async_install(None, False)
    assert bridge.uploads == []
    assert entity.in_progress is False
    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_a_refusal_from_the_bridge_is_reported(hass: HomeAssistant, aioclient_mock, no_wait) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/{XIAO}", content=FIRMWARE)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/checksums.txt", content=_checksums())
    entity, _ = _entity(hass, FakeBridge(refuse="Firmware chip mismatch"))
    await entity._async_check_releases()

    with pytest.raises(HomeAssistantError, match="chip mismatch"):
        await entity.async_install(None, False)
    assert entity.in_progress is False


async def test_a_bridge_that_does_not_come_back_updated_is_an_error(
    hass: HomeAssistant, aioclient_mock, no_wait
) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/{XIAO}", content=FIRMWARE)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/checksums.txt", content=_checksums())
    entity, _ = _entity(hass, FakeBridge(version_after="1.15.0"))
    await entity._async_check_releases()

    with pytest.raises(HomeAssistantError, match="did not come back"):
        await entity.async_install(None, False)
    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_only_the_newest_version_can_be_installed(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(const.FIRMWARE_RELEASES_API, json=RELEASES)
    entity, _ = _entity(hass, FakeBridge())
    await entity._async_check_releases()
    with pytest.raises(HomeAssistantError, match="Only the newest"):
        await entity.async_install("1.15.0", False)


# -- The upload request ---------------------------------------------------------


async def test_the_upload_sends_the_md5_like_the_web_ui(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post("http://bridge/api/firmware/update", text="OK")
    client = api_module.OpenNeatoApiClient("bridge", async_get_clientsession(hass))
    await client.upload_firmware(FIRMWARE, XIAO, "abc123")
    method, url, _data, _headers = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert url.path == "/api/firmware/update"
    assert url.query["hash"] == "abc123"


async def test_the_upload_reports_the_bridge_refusal(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(
        "http://bridge/api/firmware/update", status=400, json={"error": "MD5 parameter invalid"}
    )
    client = api_module.OpenNeatoApiClient("bridge", async_get_clientsession(hass))
    with pytest.raises(api_module.OpenNeatoApiError, match="MD5 parameter invalid"):
        await client.upload_firmware(FIRMWARE, XIAO, "bad")
