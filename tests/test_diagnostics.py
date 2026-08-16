"""Tests for privacy-safe integration diagnostics."""

import json
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canal_de_isabel_ii.const import (
    CONF_CAPTCHA_API_KEY,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.canal_de_isabel_ii.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .factories import make_snapshot


async def test_diagnostics_describe_health_without_personal_data(
    hass: HomeAssistant,
) -> None:
    """Diagnostics remain useful without exposing credentials or supply data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data={
            CONF_USERNAME: "X1234567L",
            CONF_PASSWORD: "secret-password",
            CONF_CAPTCHA_API_KEY: "secret-captcha-key",
        },
        unique_id="X1234567L",
        version=3,
        minor_version=0,
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            return_value=make_snapshot(),
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=None,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_save",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config_entry"] == {"version": 3, "minor_version": 0}
    assert diagnostics["coordinator"]["last_update_success"] is True
    assert diagnostics["tariffs"] == {
        "catalog_version": "2026",
        "configured_contract_count": 0,
    }
    assert diagnostics["snapshot"]["contract_count"] == 1
    assert diagnostics["snapshot"]["contracts"] == [
        {
            "has_meter_id": True,
            "has_meter_reading": True,
            "hourly_reading_count": 2,
            "first_hourly_reading": "2026-08-14T08:00:00+00:00",
            "last_hourly_reading": "2026-08-14T09:00:00+00:00",
            "daily_reading_count": 2,
            "first_daily_reading": "2026-08-13",
            "last_daily_reading": "2026-08-14",
        }
    ]
    serialized = json.dumps(diagnostics)
    assert "X1234567L" not in serialized
    assert "secret-password" not in serialized
    assert "secret-captcha-key" not in serialized
    assert "contract-123" not in serialized
    assert "meter-456" not in serialized
    assert "Calle de Alcalá 1" not in serialized
