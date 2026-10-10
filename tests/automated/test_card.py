"""Tests for serving the replay card and adding it to the dashboards.

The tests come from the ci branch and run against any branch, so they are
skipped on a branch that does not have the card module yet.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

try:
    card = importlib.import_module("custom_components.openneato.card")
except ModuleNotFoundError:
    pytest.skip("This branch has no card module", allow_module_level=True)

integration = importlib.import_module("custom_components.openneato")
DOMAIN = "openneato"

OTHER = {"id": "other", "type": "module", "url": "/hacsfiles/other-card.js"}


class FakeResources:
    """Behaves like the dashboards' resource collection (storage mode)."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self.loaded = False
        self.items = {item["id"]: dict(item) for item in items or []}
        self.writes = 0

    async def async_load(self) -> None:
        pass

    def async_items(self) -> list[dict]:
        assert self.loaded, "the collection must be loaded first"
        return list(self.items.values())

    async def async_create_item(self, data: dict) -> dict:
        item = {
            "id": f"id{len(self.items)}",
            "type": data["res_type"],
            "url": data["url"],
        }
        self.items[item["id"]] = item
        self.writes += 1
        return item

    async def async_update_item(self, item_id: str, updates: dict) -> dict:
        self.items[item_id].update(type=updates["res_type"], url=updates["url"])
        self.writes += 1
        return self.items[item_id]

    async def async_delete_item(self, item_id: str) -> None:
        del self.items[item_id]
        self.writes += 1


@pytest.fixture
def frontend(hass: HomeAssistant):
    """Dashboards are set up: the card can be served."""
    hass.config.components.add("frontend")
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_url:
        yield add_url


def card_resources(resources: FakeResources) -> list[dict]:
    return [item for item in resources.items.values() if item["url"].startswith(card.CARD_URL)]


async def test_card_added_to_dashboard_resources(hass: HomeAssistant, frontend: MagicMock) -> None:
    """The file is served and added to the dashboard resources with a hash."""
    resources = FakeResources([OTHER])
    hass.data["lovelace"] = SimpleNamespace(resources=resources)
    await card.async_register_card(hass)

    (path,) = hass.http.async_register_static_paths.call_args.args[0]
    assert path.url_path == card.CARD_URL
    assert Path(path.path) == card.CARD_FILE and card.CARD_FILE.is_file()
    (ours,) = card_resources(resources)
    assert ours["type"] == "module"
    assert ours["url"].startswith(f"{card.CARD_URL}?v=")
    assert resources.items["other"] == OTHER
    frontend.assert_not_called()


async def test_card_resource_updated_and_kept_single(hass: HomeAssistant, frontend: MagicMock) -> None:
    """An older version is updated and extra copies removed."""
    resources = FakeResources(
        [
            {"id": "a", "type": "js", "url": f"{card.CARD_URL}?v=old"},
            {"id": "b", "type": "module", "url": f"{card.CARD_URL}?v=older"},
            OTHER,
        ]
    )
    hass.data["lovelace"] = SimpleNamespace(resources=resources)
    await card.async_register_card(hass)
    (ours,) = card_resources(resources)
    assert ours["id"] == "a" and ours["type"] == "module"

    writes = resources.writes
    await card.async_register_card(hass)
    assert resources.writes == writes


async def test_card_on_every_page_when_resources_are_yaml(hass: HomeAssistant, frontend: MagicMock) -> None:
    """Resources in YAML cannot be edited: the card is loaded on every page."""
    hass.data["lovelace"] = SimpleNamespace(resources=SimpleNamespace(loaded=True, async_items=list))
    await card.async_register_card(hass)
    assert frontend.call_args.args[1].startswith(f"{card.CARD_URL}?v=")


async def test_card_on_every_page_when_resources_fail(
    hass: HomeAssistant, frontend: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """If the resource collection fails, setup goes on."""
    resources = FakeResources()
    resources.async_create_item = AsyncMock(side_effect=RuntimeError("changed"))
    hass.data["lovelace"] = SimpleNamespace(resources=resources)
    await card.async_register_card(hass)
    frontend.assert_called_once()
    assert "Could not update the replay card" in caplog.text


async def test_card_skipped_without_frontend(hass: HomeAssistant) -> None:
    """Without dashboards there is nothing to serve, and setup still works."""
    assert "frontend" not in hass.config.components
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_url:
        await card.async_register_card(hass)
    add_url.assert_not_called()


async def test_card_registered_before_any_robot_answers(hass: HomeAssistant, frontend: MagicMock) -> None:
    """The card is on the dashboards even while the robot is offline."""
    resources = FakeResources()
    hass.data["lovelace"] = SimpleNamespace(resources=resources)
    assert await integration.async_setup(hass, {})
    assert len(card_resources(resources)) == 1

    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    error = integration.OpenNeatoConnectionError("offline")
    with (
        patch.object(integration.OpenNeatoApiClient, "get_firmware_version", AsyncMock(side_effect=error)),
        pytest.raises(ConfigEntryNotReady),
    ):
        await integration.async_setup_entry(hass, entry)
    assert len(card_resources(resources)) == 1


async def test_card_resource_removed_with_last_robot(hass: HomeAssistant) -> None:
    """Removing the last robot removes the card's resource, and only it."""
    resources = FakeResources([{"id": "a", "type": "module", "url": f"{card.CARD_URL}?v=1"}, OTHER])
    hass.data["lovelace"] = SimpleNamespace(resources=resources)
    first = MockConfigEntry(domain=DOMAIN, unique_id="first", data={"host": "192.0.2.1"})
    second = MockConfigEntry(domain=DOMAIN, unique_id="second", data={"host": "192.0.2.2"})
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    await integration.async_remove_entry(hass, first)
    assert len(card_resources(resources)) == 1

    await hass.config_entries.async_remove(first.entry_id)
    await integration.async_remove_entry(hass, second)
    assert card_resources(resources) == []
    assert resources.items["other"] == OTHER


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_card_script_parses() -> None:
    """The card's script has no syntax errors."""
    result = subprocess.run(
        ["node", "--check", str(card.CARD_FILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

