"""The firmware update entity finds, verifies and installs the bridge firmware.

Releases are read from the fork's GitHub releases; the board's file is picked
by the board ID the bridge reports, checked against checksums.txt, and sent to
the bridge's upload endpoint. The entity tests set the integration up through
its config entry and install through Home Assistant's update.install service.
Skipped on a branch without the update platform.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import importlib

import pytest

try:
    update = importlib.import_module("custom_components.openneato.update")
    firmware_release = importlib.import_module("custom_components.openneato.firmware_release")
except ModuleNotFoundError:
    pytest.skip("This branch has no firmware update entity", allow_module_level=True)

from homeassistant.components.update import ATTR_LATEST_VERSION, UpdateEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .bridge_mock import (
    DOMAIN,
    DOWNLOAD,
    FIRMWARE,
    FIRMWARE_INFO,
    OLD_FIRMWARE_INFO,
    RELEASES,
    SERIAL,
    XIAO,
    checksums,
    mock_bridge,
    release,
    setup_integration,
    without_frontend,
)

api_module = importlib.import_module("custom_components.openneato.api")
UPLOAD = "http://bridge/api/firmware/update"


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll for the rebooted bridge without the real five-second pauses."""
    monkeypatch.setattr(update, "FIRMWARE_REBOOT_POLL", 0)
    monkeypatch.setattr(update, "FIRMWARE_REBOOT_TIMEOUT", 0.5)


def _entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, f"{SERIAL}_firmware")
    assert entity_id
    return entity_id


def _mock_install(aioclient_mock, firmware: bytes = FIRMWARE, version_after: str = "1.16.0", **upload) -> None:
    """Answer the install: downloads, the upload, and the rebooted bridge's version."""
    aioclient_mock.clear_requests()
    mock_bridge(aioclient_mock, firmware_info={**FIRMWARE_INFO, "version": version_after})
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/{XIAO}", content=firmware)
    aioclient_mock.get(f"{DOWNLOAD}/v1.16.0/checksums.txt", content=checksums())
    aioclient_mock.post(UPLOAD, **(upload or {"text": "OK"}))


def _uploads(aioclient_mock) -> list:
    return [call for call in aioclient_mock.mock_calls if call[0] == "POST" and str(call[1]).startswith(UPLOAD)]


async def _install(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)


# -- Picking the release -------------------------------------------------------


def test_the_newest_published_release_with_firmware_is_picked() -> None:
    picked = firmware_release.pick_release(RELEASES, "seeed-xiao-esp32c3")
    assert picked.version == "1.16.0"
    assert picked.firmware_url == f"{DOWNLOAD}/v1.16.0/{XIAO}"
    assert picked.checksums_url == f"{DOWNLOAD}/v1.16.0/checksums.txt"


def test_a_board_missing_from_the_release_cannot_install() -> None:
    picked = firmware_release.pick_release(RELEASES, "esp32-s3")
    assert picked.version == "1.16.0"
    assert picked.firmware_url is None


def test_no_release_with_firmware() -> None:
    assert firmware_release.pick_release([release("v1.17.0")], "esp32-c3") is None


def test_checksums_are_parsed_by_file_name() -> None:
    parsed = firmware_release.parse_checksums(checksums().decode())
    assert parsed[XIAO] == hashlib.sha256(FIRMWARE).hexdigest()
    assert len(parsed) == 2


# -- The entity ------------------------------------------------------------------


async def test_a_newer_release_is_offered(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
    state = hass.states.get(_entity_id(hass))
    assert state.state == "on"
    assert state.attributes["installed_version"] == "1.15.0"
    assert state.attributes[ATTR_LATEST_VERSION] == "1.16.0"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & UpdateEntityFeature.INSTALL


async def test_without_board_install_is_not_offered(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock, firmware_info=OLD_FIRMWARE_INFO)
    with without_frontend(hass):
        await setup_integration(hass)
    state = hass.states.get(_entity_id(hass))
    assert state.attributes[ATTR_LATEST_VERSION] == "1.16.0"
    assert not state.attributes[ATTR_SUPPORTED_FEATURES] & UpdateEntityFeature.INSTALL


async def test_github_unreachable_keeps_the_last_answer(hass: HomeAssistant, aioclient_mock) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
        aioclient_mock.clear_requests()
        mock_bridge(aioclient_mock, releases_status=503)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=update.FIRMWARE_CHECK_INTERVAL + 1))
        await hass.async_block_till_done()
    assert any(str(call[1]).startswith("https://api.github.com") for call in aioclient_mock.mock_calls)
    assert hass.states.get(_entity_id(hass)).attributes[ATTR_LATEST_VERSION] == "1.16.0"


async def test_install_flashes_the_verified_board_file(hass: HomeAssistant, aioclient_mock, no_wait) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
        entity_id = _entity_id(hass)
        _mock_install(aioclient_mock)
        await _install(hass, entity_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    uploads = _uploads(aioclient_mock)
    assert len(uploads) == 1
    assert uploads[0][1].query["hash"] == hashlib.md5(FIRMWARE).hexdigest()
    # The entry reloaded and now reads the new firmware.
    state = hass.states.get(entity_id)
    assert state.attributes["installed_version"] == "1.16.0"
    assert state.state == "off"


async def test_a_file_that_fails_the_checksum_is_not_flashed(
    hass: HomeAssistant, aioclient_mock, no_wait
) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
        entity_id = _entity_id(hass)
        _mock_install(aioclient_mock, firmware=FIRMWARE[:-1])
        with pytest.raises(HomeAssistantError, match="does not match checksums.txt"):
            await _install(hass, entity_id)
    assert _uploads(aioclient_mock) == []
    assert hass.states.get(entity_id).attributes["in_progress"] is False


async def test_a_refusal_from_the_bridge_is_reported(hass: HomeAssistant, aioclient_mock, no_wait) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
        entity_id = _entity_id(hass)
        _mock_install(aioclient_mock, status=400, json={"error": "Firmware chip mismatch"})
        with pytest.raises(HomeAssistantError, match="chip mismatch"):
            await _install(hass, entity_id)
    assert hass.states.get(entity_id).attributes["in_progress"] is False


async def test_a_bridge_that_does_not_come_back_updated_is_an_error(
    hass: HomeAssistant, aioclient_mock, no_wait
) -> None:
    mock_bridge(aioclient_mock)
    with without_frontend(hass):
        await setup_integration(hass)
        entity_id = _entity_id(hass)
        _mock_install(aioclient_mock, version_after="1.15.0")
        with pytest.raises(HomeAssistantError, match="did not come back"):
            await _install(hass, entity_id)
    assert hass.states.get(entity_id).attributes["installed_version"] == "1.15.0"


# -- The upload request ---------------------------------------------------------


async def test_the_upload_sends_the_md5_like_the_web_ui(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(UPLOAD, text="OK")
    client = api_module.OpenNeatoApiClient("bridge", async_get_clientsession(hass))
    await client.upload_firmware(FIRMWARE, XIAO, "abc123")
    method, url, _data, _headers = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert url.path == "/api/firmware/update"
    assert url.query["hash"] == "abc123"


async def test_the_upload_reports_the_bridge_refusal(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(UPLOAD, status=400, json={"error": "MD5 parameter invalid"})
    client = api_module.OpenNeatoApiClient("bridge", async_get_clientsession(hass))
    with pytest.raises(api_module.OpenNeatoApiError, match="MD5 parameter invalid"):
        await client.upload_firmware(FIRMWARE, XIAO, "bad")
