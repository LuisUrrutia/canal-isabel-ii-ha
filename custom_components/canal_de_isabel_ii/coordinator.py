"""Persistent daily synchronization for Canal de Isabel II."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    CanalAuthenticationError,
    CanalCaptchaError,
    CanalClient,
    CanalConnectionError,
    CanalInvalidResponseError,
)
from .const import DOMAIN
from .models import ConsumptionSnapshot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_CONNECTION_RETRY_SECONDS = 30 * 60


class HistoryStore(Protocol):
    """Internal persistence port with production and in-memory adapters."""

    async def async_load(self) -> ConsumptionSnapshot | None:
        """Load the last complete snapshot."""

    async def async_save(self, snapshot: ConsumptionSnapshot) -> None:
        """Persist the last complete snapshot."""


class CanalCoordinator(DataUpdateCoordinator[ConsumptionSnapshot]):
    """Merge one daily portal sync into private persistent history."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: CanalClient,
        store: HistoryStore,
    ) -> None:
        """Initialize the coordinator without interval-based polling."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
            always_update=False,
        )
        self.client = client
        self._store = store
        self._previous: ConsumptionSnapshot | None = None

    async def async_initialize(self) -> None:
        """Load cached history before performing the first incremental refresh."""
        self._previous = await self._store.async_load()
        await self.async_config_entry_first_refresh()

    @override
    async def _async_update_data(self) -> ConsumptionSnapshot:
        """Fetch, merge and persist one atomic portal snapshot."""
        try:
            snapshot = await self.client.async_fetch_consumption(self._previous)
        except (CanalAuthenticationError, CanalCaptchaError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CanalConnectionError as err:
            raise UpdateFailed(
                str(err),
                retry_after=_CONNECTION_RETRY_SECONDS,
            ) from err
        except CanalInvalidResponseError as err:
            raise UpdateFailed(str(err)) from err
        await self._store.async_save(snapshot)
        self._previous = snapshot
        return snapshot


@dataclass(slots=True)
class CanalRuntimeData:
    """Runtime modules owned by one config entry."""

    client: CanalClient
    coordinator: CanalCoordinator


type CanalConfigEntry = ConfigEntry[CanalRuntimeData]
