"""Tests for private normalized history persistence."""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.canal_de_isabel_ii.storage import CanalHistoryStore

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
