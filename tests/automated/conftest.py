"""Shared fixtures for the OpenNeato tests."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Load integrations from custom_components in every test."""
