"""Update platform for the OpenNeato integration: the bridge firmware.

Reads the fork's GitHub releases and installs the newest firmware for this
bridge's board through the same upload endpoint the bridge's web UI uses, so
the bridge can be updated from Home Assistant in one step.

Installing needs the board's release file name, which the bridge reports as
`board` in /api/firmware/version. Older firmware does not report it; the
entity then still announces new releases but cannot install them, because
boards that share a chip (generic ESP32-C3, Seeed XIAO ESP32C3) need
different files.
"""

from __future__ import annotations

import asyncio
from asyncio import timeout
from datetime import datetime, timedelta
import hashlib
import logging
import time
from typing import Any

import aiohttp

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .api import OpenNeatoApiClient, OpenNeatoApiError, OpenNeatoConnectionError
from .const import (
    DOMAIN,
    FIRMWARE_CHECK_INTERVAL,
    FIRMWARE_REBOOT_POLL,
    FIRMWARE_REBOOT_TIMEOUT,
    FIRMWARE_RELEASES_API,
)
from .entity import OpenNeatoEntity
from .firmware_check import parse_version
from .firmware_release import FirmwareRelease, parse_checksums, pick_release

_LOGGER = logging.getLogger(__name__)

# GitHub answers in well under a second; the firmware file is about 1.4 MB.
DOWNLOAD_TIMEOUT = 60  # seconds


class OpenNeatoFirmwareUpdate(OpenNeatoEntity, UpdateEntity):
    """The bridge firmware, compared with the newest fork release."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "firmware"
    _attr_name = "Firmware"

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        api: OpenNeatoApiClient,
        serial: str,
        firmware_info: dict[str, Any],
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, serial, device_info)
        self._attr_unique_id = f"{serial}_firmware"
        self._entry = entry
        self._api = api
        self._board: str | None = firmware_info.get("board") or None
        self._attr_installed_version = str(firmware_info.get("version") or "") or None
        self._release: FirmwareRelease | None = None

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Offer Install only when the newest release has this board's file."""
        features = UpdateEntityFeature.RELEASE_NOTES
        if self._release is not None and self._release.firmware_url is not None:
            features |= UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        return features

    @property
    def latest_version(self) -> str | None:
        """Return the newest release that carries firmware, once known."""
        return self._release.version if self._release else None

    @property
    def release_url(self) -> str | None:
        """Return the newest firmware release's page."""
        return self._release.release_url if self._release else None

    async def async_added_to_hass(self) -> None:
        """Check the releases now and every few hours."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_check_releases, timedelta(seconds=FIRMWARE_CHECK_INTERVAL)
            )
        )
        self._entry.async_create_background_task(
            self.hass, self._async_check_releases(), f"{DOMAIN} firmware release check"
        )

    async def _async_check_releases(self, _now: datetime | None = None) -> None:
        """Read the fork's releases and keep the newest one with firmware."""
        session = async_get_clientsession(self.hass)
        try:
            async with timeout(DOWNLOAD_TIMEOUT):
                async with session.get(
                    FIRMWARE_RELEASES_API, headers={"Accept": "application/vnd.github+json"}
                ) as response:
                    response.raise_for_status()
                    releases = await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            # Keep the last answer; GitHub being unreachable is not a fault
            # of the bridge.
            _LOGGER.debug("Could not read the firmware releases: %s", err)
            return
        self._release = pick_release(releases if isinstance(releases, list) else [], self._board)
        self.async_write_ha_state()

    async def async_release_notes(self) -> str | None:
        """Return the newest firmware release's notes."""
        return self._release.notes if self._release else None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download, verify and flash the newest firmware, then wait for the reboot."""
        release = self._release
        if release is None or release.firmware_url is None or release.checksums_url is None:
            raise HomeAssistantError("The newest release has no firmware file for this bridge's board")
        if version is not None and version.lstrip("v") != release.version:
            raise HomeAssistantError(f"Only the newest firmware, {release.version}, can be installed")

        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            firmware = await self._async_download(release.firmware_url)
            checksums = parse_checksums((await self._async_download(release.checksums_url)).decode())
            if hashlib.sha256(firmware).hexdigest() != checksums.get(release.firmware_name or ""):
                raise HomeAssistantError(
                    f"{release.firmware_name} does not match checksums.txt; nothing was installed"
                )
            _LOGGER.info("Installing firmware %s on %s", release.version, self._api.base_url)
            await self._api.upload_firmware(
                firmware,
                release.firmware_name or "firmware.bin",
                hashlib.md5(firmware, usedforsecurity=False).hexdigest(),
            )
            await self._async_wait_for_version(release.version)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

        # The device, the coordinator's endpoint list and this entity all
        # describe the old firmware; reloading picks up the new one.
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    async def _async_download(self, url: str) -> bytes:
        """Download one release file."""
        session = async_get_clientsession(self.hass)
        try:
            async with timeout(DOWNLOAD_TIMEOUT):
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Could not download {url}: {err}") from err

    async def _async_wait_for_version(self, version: str) -> None:
        """Wait for the rebooted bridge to report the new version."""
        target = parse_version(version)
        deadline = time.monotonic() + FIRMWARE_REBOOT_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(FIRMWARE_REBOOT_POLL)
            try:
                info = await self._api.get_firmware_version()
            except (OpenNeatoConnectionError, OpenNeatoApiError):
                continue  # still rebooting
            if parse_version(info.get("version")) == target:
                return
        raise HomeAssistantError(
            f"The bridge did not come back with firmware {version} within {FIRMWARE_REBOOT_TIMEOUT} s"
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the OpenNeato firmware update entity from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            OpenNeatoFirmwareUpdate(
                data["coordinator"],
                entry,
                data["api"],
                data["serial"],
                data["firmware_info"],
                data["devices"].bridge,
            )
        ]
    )
