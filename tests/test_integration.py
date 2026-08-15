"""Tests through the Home Assistant config-entry seam."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.canal_de_isabel_ii import async_migrate_entry, async_remove_entry
from custom_components.canal_de_isabel_ii.client import (
    CanalAuthenticationError,
    CanalConnectionError,
)
from custom_components.canal_de_isabel_ii.const import (
    CONF_CAPTCHA_API_KEY,
    CONF_PASSWORD,
    CONF_SYNC_HOUR,
    CONF_USERNAME,
    DOMAIN,
)

from .factories import make_snapshot

VALID_DATA = {
    CONF_USERNAME: "X1234567L",
    CONF_PASSWORD: "secret",
    CONF_CAPTCHA_API_KEY: "captcha-key",
}


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare Recorder storage before Home Assistant is created."""


def _entry(*, options: dict[str, int] | None = None) -> MockConfigEntry:
    """Create one current-version account entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data=VALID_DATA,
        options=options,
        unique_id="X1234567L",
        version=3,
    )


async def test_setup_creates_three_water_sensors(hass: HomeAssistant) -> None:
    """One contract exposes its meter, latest hour and latest day."""
    entry = _entry()
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

    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    meter_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "canal_ii_meter_contract-123"
    )
    hourly_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "canal_ii_consumption_contract-123"
    )
    daily_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "canal_ii_daily_contract-123"
    )
    assert meter_id is not None
    assert hourly_id is not None
    assert daily_id is not None

    meter = hass.states.get(meter_id)
    hourly = hass.states.get(hourly_id)
    daily = hass.states.get(daily_id)
    assert meter is not None
    assert hourly is not None
    assert daily is not None
    assert meter.state == "125.5"
    assert meter.attributes["unit_of_measurement"] == "m³"
    assert meter.attributes["state_class"] == "total_increasing"
    assert hourly.state == "3.0"
    assert hourly.attributes["reading_start"] == "2026-08-14T09:00:00+00:00"
    assert daily.state == "15.5"
    assert daily.attributes["reading_day"] == "2026-08-14"


async def test_setup_backfills_meter_long_term_statistics(
    hass: HomeAssistant,
    recorder_mock: Recorder,
) -> None:
    """Hourly history is anchored to the physical meter in Recorder."""
    entry = _entry()
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

    registry = er.async_get(hass)
    meter_id = registry.async_get_entity_id(
        "sensor", DOMAIN, "canal_ii_meter_contract-123"
    )
    assert meter_id is not None
    await async_wait_recording_done(hass)

    snapshot = make_snapshot()
    statistics = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        snapshot.contracts["contract-123"].hourly_readings[0].start,
        None,
        {meter_id},
        "hour",
        None,
        {"state", "sum"},
    )

    rows = statistics[meter_id]
    assert rows[0]["state"] == pytest.approx(125.497)
    assert rows[-1]["state"] == pytest.approx(125.5)
    assert rows[-1]["sum"] == pytest.approx(125.5)


async def test_cached_snapshot_is_used_for_incremental_refresh(
    hass: HomeAssistant,
) -> None:
    """Startup loads private history and passes it to the deep client."""
    cached = make_snapshot()
    refreshed = make_snapshot()
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            return_value=refreshed,
        ) as fetch,
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=cached,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_save",
            return_value=None,
        ) as save,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    fetch.assert_awaited_once_with(cached)
    save.assert_awaited_once_with(refreshed)


async def test_configured_hour_triggers_one_daily_refresh(
    hass: HomeAssistant,
) -> None:
    """The configured local hour drives synchronization without polling."""
    entry = _entry(options={CONF_SYNC_HOUR: 5})
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=make_snapshot())

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            fetch,
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
        fetch.reset_mock()

        now = dt_util.now()
        next_sync = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if next_sync <= now:
            next_sync += timedelta(days=1)
        async_fire_time_changed(hass, next_sync)
        await hass.async_block_till_done()

    fetch.assert_awaited_once_with(make_snapshot())


async def test_legacy_entry_migrates_then_requests_reauthentication(
    hass: HomeAssistant,
) -> None:
    """Legacy sessions migrate safely and request the new credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data={"jsessionid": "obsolete-session"},
        unique_id=None,
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert entry.unique_id == DOMAIN
    assert entry.data == {"jsessionid": "obsolete-session"}

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_authentication_failure_starts_reauthentication(
    hass: HomeAssistant,
) -> None:
    """A rejected automated login starts Home Assistant reauthentication."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            side_effect=CanalAuthenticationError("rejected"),
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=None,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_temporary_portal_failure_remains_retryable(hass: HomeAssistant) -> None:
    """Connectivity errors do not incorrectly ask users for new credentials."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            side_effect=CanalConnectionError("offline"),
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=None,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_entry_updates_use_the_registered_reload_listener(
    hass: HomeAssistant,
) -> None:
    """Credential and schedule changes reload one already loaded account."""
    entry = _entry()
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
        with patch.object(
            hass.config_entries,
            "async_reload",
            return_value=True,
        ) as reload_entry:
            hass.config_entries.async_update_entry(
                entry,
                options={CONF_SYNC_HOUR: 4},
            )
            await hass.async_block_till_done()

    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_removing_entry_deletes_private_history(hass: HomeAssistant) -> None:
    """Account deletion also removes the integration's private cache."""
    entry = _entry()
    with patch(
        "custom_components.canal_de_isabel_ii.CanalHistoryStore.async_remove",
        return_value=None,
    ) as remove:
        await async_remove_entry(hass, entry)

    remove.assert_awaited_once_with()
