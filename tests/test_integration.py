"""Tests through the Home Assistant config-entry seam."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CURRENCY_EURO
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
    CanalCaptchaError,
    CanalConnectionError,
)
from custom_components.canal_de_isabel_ii.const import (
    CONF_CAPTCHA_API_KEY,
    CONF_PASSWORD,
    CONF_SYNC_HOUR,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.canal_de_isabel_ii.models import DailyConsumption
from custom_components.canal_de_isabel_ii.tariffs import (
    SewerProvider,
    SupplyType,
    TariffProfile,
)

from .factories import make_snapshot

VALID_DATA = {
    CONF_USERNAME: "X1234567L",
    CONF_PASSWORD: "secret",
    CONF_CAPTCHA_API_KEY: "captcha-key",
}
EXTERNAL_WATER_STATISTIC_ID = f"{DOMAIN}:water_meter_contract_123"
EXTERNAL_COST_STATISTIC_ID = f"{DOMAIN}:water_cost_contract_123"


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


async def test_configured_tariff_creates_estimated_bill_sensor(
    hass: HomeAssistant,
) -> None:
    """A private contract profile exposes an auditable monetary total."""
    entry = _entry()
    entry.add_to_hass(hass)
    profile = TariffProfile(
        supply_type=SupplyType.SINGLE_DWELLING,
        sewer_provider=SewerProvider.MUNICIPALITY,
        meter_diameter_mm=15,
        supplied_uses=1,
        billing_period_start=date(2026, 7, 8),
        billing_cycle_days=60,
        municipal_sewer_rate_eur_m3=Decimal("0.2200"),
    )
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
        patch(
            "custom_components.canal_de_isabel_ii.CanalTariffProfileStore.async_load",
            return_value={"contract-123": profile},
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    cost_id = registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        "canal_ii_estimated_bill_contract-123",
    )
    assert cost_id is not None
    cost = hass.states.get(cost_id)
    assert cost is not None
    assert cost.attributes["device_class"] == "monetary"
    assert cost.attributes["state_class"] == "total"
    assert cost.attributes["unit_of_measurement"] == "€"
    assert cost.attributes["billing_period_start"] == "2026-07-08"
    assert cost.attributes["calculated_through"] == "2026-08-15"
    assert cost.attributes["volume_m3"] == pytest.approx(0.0245)
    assert cost.attributes["observed_days"] == 2
    assert not cost.attributes["history_complete"]
    assert cost.attributes["is_estimate"]


async def test_setup_finishes_while_initial_sync_runs_in_background(
    hass: HomeAssistant,
) -> None:
    """Slow initial portal history must not block config-entry setup."""
    entry = _entry()
    entry.add_to_hass(hass)
    fetch_started = asyncio.Event()
    allow_fetch_to_finish = asyncio.Event()

    async def slow_fetch(_previous: object) -> object:
        fetch_started.set()
        await allow_fetch_to_finish.wait()
        return make_snapshot()

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            side_effect=slow_fetch,
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
        setup_task = hass.async_create_task(
            hass.config_entries.async_setup(entry.entry_id),
            "test non-blocking Canal setup",
        )
        await asyncio.wait_for(fetch_started.wait(), timeout=1)
        try:
            await asyncio.wait_for(asyncio.shield(setup_task), timeout=0.25)
            setup_finished_before_sync = True
        except TimeoutError:
            setup_finished_before_sync = False
        finally:
            allow_fetch_to_finish.set()
            assert await setup_task
            await hass.async_block_till_done()

    assert setup_finished_before_sync
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_backfills_dedicated_external_water_statistics(
    hass: HomeAssistant,
    recorder_mock: Recorder,
) -> None:
    """Portal history never shares Recorder's automatic entity statistic."""
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
        {EXTERNAL_WATER_STATISTIC_ID},
        "hour",
        None,
        {"state", "sum"},
    )

    assert meter_id != EXTERNAL_WATER_STATISTIC_ID
    rows = statistics[EXTERNAL_WATER_STATISTIC_ID]
    assert rows[0]["state"] == pytest.approx(125.497)
    assert rows[-1]["state"] == pytest.approx(125.5)
    assert rows[-1]["sum"] == pytest.approx(125.5)
    assert all(
        current["sum"] <= following["sum"] for current, following in pairwise(rows)
    )


async def test_configured_tariff_backfills_external_cost_statistics(
    hass: HomeAssistant,
    recorder_mock: Recorder,
) -> None:
    """Daily tariff cost is historical, monetary and monotonic across Recorder."""
    entry = _entry()
    entry.add_to_hass(hass)
    profile = TariffProfile(
        supply_type=SupplyType.SINGLE_DWELLING,
        sewer_provider=SewerProvider.MUNICIPALITY,
        meter_diameter_mm=15,
        supplied_uses=1,
        billing_period_start=date(2026, 7, 8),
        billing_cycle_days=60,
        municipal_sewer_rate_eur_m3=Decimal("0.2200"),
    )
    original_snapshot = make_snapshot()
    original_contract = original_snapshot.contracts["contract-123"]
    snapshot = replace(
        original_snapshot,
        contracts={
            "contract-123": replace(
                original_contract,
                daily_readings=(
                    DailyConsumption(day=date(2026, 7, 7), volume_liters=1500),
                    DailyConsumption(day=date(2026, 7, 8), volume_liters=1000),
                ),
            )
        },
    )

    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Stale shifted water cost",
            source=DOMAIN,
            statistic_id=EXTERNAL_COST_STATISTIC_ID,
            unit_class=None,
            unit_of_measurement=CURRENCY_EURO,
        ),
        [
            StatisticData(
                start=datetime(2026, 7, 8, 22, tzinfo=UTC),
                state=999,
                sum=999,
            )
        ],
    )
    await async_wait_recording_done(hass)

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            return_value=snapshot,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=None,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_save",
            return_value=None,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.CanalTariffProfileStore.async_load",
            return_value={"contract-123": profile},
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    await async_wait_recording_done(hass)
    statistics = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 6, tzinfo=UTC),
        None,
        {EXTERNAL_COST_STATISTIC_ID},
        "hour",
        None,
        {"state", "sum"},
    )

    rows = statistics[EXTERNAL_COST_STATISTIC_ID]
    assert len(rows) == 2
    assert [row["start"] for row in rows] == [
        datetime(2026, 7, 6, 22, tzinfo=UTC).timestamp(),
        datetime(2026, 7, 7, 22, tzinfo=UTC).timestamp(),
    ]
    assert rows[-1]["state"] < rows[0]["state"]
    assert rows[-1]["sum"] > rows[0]["sum"] > 0


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


@pytest.mark.parametrize(
    "failure",
    [
        CanalAuthenticationError("rejected"),
        CanalCaptchaError("solver failed"),
    ],
)
async def test_authentication_failure_starts_reauthentication(
    hass: HomeAssistant,
    failure: Exception,
) -> None:
    """A credential or CAPTCHA failure starts Home Assistant reauthentication."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.canal_de_isabel_ii.client."
            "CanalClient.async_fetch_consumption",
            side_effect=failure,
        ),
        patch(
            "custom_components.canal_de_isabel_ii.storage.CanalHistoryStore.async_load",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
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
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
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
    with (
        patch(
            "custom_components.canal_de_isabel_ii.CanalHistoryStore.async_remove",
            return_value=None,
        ) as remove_history,
        patch(
            "custom_components.canal_de_isabel_ii.CanalTariffProfileStore.async_remove",
            return_value=None,
        ) as remove_tariffs,
    ):
        await async_remove_entry(hass, entry)

    remove_history.assert_awaited_once_with()
    remove_tariffs.assert_awaited_once_with()
