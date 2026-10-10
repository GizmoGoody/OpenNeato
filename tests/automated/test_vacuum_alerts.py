"""The vacuum shows Error only for the robot's errors, not its alerts.

The bridge reports alerts (UI_ALERT 201-242: returning to base, recovering
location...) with kind "warning" and errors with kind "error". Skipped on a
branch that still treats every alert as an error.
"""

from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace

import pytest
from homeassistant.components.vacuum import VacuumActivity

vacuum = importlib.import_module("custom_components.openneato.vacuum")

if '"kind"' not in inspect.getsource(vacuum.OpenNeatoVacuum.activity.fget):
    pytest.skip("This branch treats every alert as an error", allow_module_level=True)


def _activity(error: dict, ui_state: str = "UIMGR_STATE_HOUSECLEANINGRUNNING") -> VacuumActivity:
    entity = object.__new__(vacuum.OpenNeatoVacuum)
    entity.coordinator = SimpleNamespace(
        data={"state": {"uiState": ui_state}, "charger": {}, "error": error}
    )
    return entity.activity


def test_an_alert_is_not_an_error() -> None:
    """Sent home, the robot says Returning to base: it is returning, not in error."""
    alert = {"hasError": True, "kind": "warning", "errorCode": 201, "displayMessage": "Returning to base"}
    assert _activity(alert, "UIMGR_STATE_DOCKINGRUNNING") == VacuumActivity.RETURNING


def test_an_error_is_an_error() -> None:
    error = {"hasError": True, "kind": "error", "errorCode": 249, "displayMessage": "Please empty the dust bin"}
    assert _activity(error) == VacuumActivity.ERROR


def test_firmware_without_kind_keeps_the_old_behaviour() -> None:
    assert _activity({"hasError": True}) == VacuumActivity.ERROR
