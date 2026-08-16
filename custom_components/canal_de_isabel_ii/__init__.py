"""Canal de Isabel II integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import CookieJar
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_change

from .client import CanalClient, CanalCredentials
from .const import (
    CONF_CAPTCHA_API_KEY,
    CONF_CAPTCHA_ATTEMPTS,
    CONF_PASSWORD,
    CONF_SYNC_HOUR,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CAPTCHA_ATTEMPTS,
    DEFAULT_SYNC_HOUR,
    DOMAIN,
)
from .coordinator import CanalConfigEntry, CanalCoordinator, CanalRuntimeData
from .storage import CanalHistoryStore
from .tariff_storage import CanalTariffProfileStore

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_PLATFORMS = (Platform.SENSOR,)
_CREDENTIAL_KEYS = {CONF_USERNAME, CONF_PASSWORD, CONF_CAPTCHA_API_KEY}
_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate legacy session-based entries to unattended authentication."""
    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate Canal config entry %s from unsupported version %s",
            entry.entry_id,
            entry.version,
        )
        return False

    if entry.version < CONFIG_ENTRY_VERSION:
        hass.config_entries.async_update_entry(
            entry,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
            unique_id=entry.unique_id or DOMAIN,
        )
        _LOGGER.info(
            "Migrated Canal config entry %s to version %s.%s",
            entry.entry_id,
            CONFIG_ENTRY_VERSION,
            CONFIG_ENTRY_MINOR_VERSION,
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CanalConfigEntry,
) -> bool:
    """Set up one Canal account and its daily synchronization."""
    _LOGGER.info("Starting setup for Canal config entry %s", entry.entry_id)
    if not _CREDENTIAL_KEYS.issubset(entry.data):
        _LOGGER.warning(
            "Canal config entry %s is missing credentials and requires "
            "reauthentication",
            entry.entry_id,
        )
        msg = "Account credentials are required after upgrading"
        raise ConfigEntryAuthFailed(msg)

    credentials = CanalCredentials(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        captcha_api_key=entry.data[CONF_CAPTCHA_API_KEY],
    )
    session = aiohttp_client.async_create_clientsession(
        hass,
        cookie_jar=CookieJar(),
    )
    client = CanalClient(
        session,
        credentials,
        captcha_attempts=int(
            entry.options.get(CONF_CAPTCHA_ATTEMPTS, DEFAULT_CAPTCHA_ATTEMPTS)
        ),
    )
    store = CanalHistoryStore(hass, entry.entry_id)
    tariff_store = CanalTariffProfileStore(hass, entry.entry_id)
    tariff_profiles = await tariff_store.async_load()
    coordinator = CanalCoordinator(hass, entry, client, store)
    await coordinator.async_initialize()
    entry.runtime_data = CanalRuntimeData(
        client=client,
        coordinator=coordinator,
        tariff_profiles=tariff_profiles,
    )

    async def async_scheduled_refresh(_: datetime) -> None:
        await coordinator.async_request_refresh()

    sync_hour = int(entry.options.get(CONF_SYNC_HOUR, DEFAULT_SYNC_HOUR))
    entry.async_on_unload(
        async_track_time_change(
            hass,
            async_scheduled_refresh,
            hour=sync_hour,
            minute=0,
            second=0,
        )
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_create_background_task(
        hass,
        coordinator.async_request_refresh(),
        "Canal initial synchronization",
    )
    _LOGGER.info(
        "Finished setup for Canal config entry %s; initial synchronization is "
        "running in the background and daily synchronization is scheduled for "
        "%02d:00",
        entry.entry_id,
        sync_hour,
    )
    return True


async def _async_reload_on_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload after credentials or synchronization options change."""
    _LOGGER.info("Reloading Canal config entry %s after an update", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: CanalConfigEntry,
) -> bool:
    """Unload the config entry and its platforms."""
    _LOGGER.info("Unloading Canal config entry %s", entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    _LOGGER.info(
        "Finished unloading Canal config entry %s (success: %s)",
        entry.entry_id,
        unloaded,
    )
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant,
    entry: CanalConfigEntry,
) -> None:
    """Delete private consumption history with its account entry."""
    _LOGGER.info(
        "Removing private history and tariff profiles for Canal config entry %s",
        entry.entry_id,
    )
    await CanalHistoryStore(hass, entry.entry_id).async_remove()
    await CanalTariffProfileStore(hass, entry.entry_id).async_remove()
