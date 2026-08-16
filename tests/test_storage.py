"""Tests for private normalized history persistence."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.canal_de_isabel_ii.storage import CanalHistoryStore
from custom_components.canal_de_isabel_ii.tariff_storage import (
    CanalTariffProfileStore,
)
from custom_components.canal_de_isabel_ii.tariffs import (
    SewerProvider,
    SupplyType,
    TariffProfile,
)

from .factories import make_snapshot


async def test_history_round_trip(hass: HomeAssistant) -> None:
    """All normalized meter, daily and hourly values survive persistence."""
    store = CanalHistoryStore(hass, "round-trip-entry")
    snapshot = make_snapshot()

    await store.async_save(snapshot)

    assert await store.async_load() == snapshot

    await store.async_remove()

    assert await CanalHistoryStore(hass, "round-trip-entry").async_load() is None


async def test_malformed_history_is_ignored(hass: HomeAssistant) -> None:
    """A partial or obsolete cache never prevents integration startup."""
    store = CanalHistoryStore(hass, "malformed-entry")
    with patch(
        "custom_components.canal_de_isabel_ii.storage.Store.async_load",
        return_value={"fetched_at": "not-a-date", "contracts": [{}]},
    ):
        assert await store.async_load() is None


async def test_tariff_profiles_round_trip(hass: HomeAssistant) -> None:
    """Private billing facts survive persistence without joining credentials."""
    store = CanalTariffProfileStore(hass, "tariff-round-trip-entry")
    profile = TariffProfile(
        supply_type=SupplyType.SINGLE_DWELLING,
        sewer_provider=SewerProvider.MUNICIPALITY,
        meter_diameter_mm=15,
        supplied_uses=1,
        billing_period_start=date(2026, 7, 8),
        billing_cycle_days=60,
        municipal_sewer_rate_eur_m3=Decimal("0.2200"),
    )

    await store.async_save({"contract-private": profile})

    assert await store.async_load() == {"contract-private": profile}

    await store.async_remove()

    assert (
        await CanalTariffProfileStore(
            hass,
            "tariff-round-trip-entry",
        ).async_load()
        == {}
    )


async def test_malformed_tariff_profiles_are_ignored(hass: HomeAssistant) -> None:
    """Invalid private pricing configuration never prevents account startup."""
    store = CanalTariffProfileStore(hass, "malformed-tariff-entry")
    with patch(
        "custom_components.canal_de_isabel_ii.tariff_storage.Store.async_load",
        return_value={"profiles": [{"contract_id": "private"}]},
    ):
        assert await store.async_load() == {}
