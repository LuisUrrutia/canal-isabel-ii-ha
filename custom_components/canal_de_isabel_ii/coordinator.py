"""Persistent daily synchronization for Canal de Isabel II."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
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
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from .tariffs import TariffProfile

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
        """Load cached history without blocking setup on remote work."""
        _LOGGER.info("Loading cached Canal consumption history")
        self._previous = await self._store.async_load()
        if self._previous is None:
            _LOGGER.info(
                "No cached Canal history is available; entities will be created "
                "after the background synchronization discovers contracts"
            )
            return

        self.async_set_updated_data(self._previous)
        daily_count, hourly_count = _reading_counts(self._previous)
        _LOGGER.info(
            "Loaded cached Canal history for %d contract(s): %d daily and %d "
            "hourly readings",
            len(self._previous.contracts),
            daily_count,
            hourly_count,
        )

    @override
    async def _async_update_data(self) -> ConsumptionSnapshot:
        """Fetch, merge and persist one atomic portal snapshot."""
        sync_type = "incremental" if self._previous is not None else "initial"
        started = monotonic()
        _LOGGER.info("Starting %s Canal consumption synchronization", sync_type)
        try:
            snapshot = await self.client.async_fetch_consumption(self._previous)
        except CanalAuthenticationError as err:
            _LOGGER.error(  # noqa: TRY400 - portal tracebacks may contain secrets
                "Canal authentication failed during %s synchronization after "
                "%.1f seconds: %s",
                sync_type,
                monotonic() - started,
                err,
            )
            raise ConfigEntryAuthFailed(str(err)) from None
        except CanalCaptchaError as err:
            _LOGGER.error(  # noqa: TRY400 - solver tracebacks may contain secrets
                "Canal CAPTCHA solving failed during %s synchronization after "
                "%.1f seconds: %s",
                sync_type,
                monotonic() - started,
                err,
            )
            raise ConfigEntryAuthFailed(str(err)) from None
        except CanalConnectionError as err:
            _LOGGER.warning(
                "Canal portal communication failed during %s synchronization "
                "after %.1f seconds; retrying in %d seconds: %s",
                sync_type,
                monotonic() - started,
                _CONNECTION_RETRY_SECONDS,
                err,
            )
            raise UpdateFailed(
                str(err),
                retry_after=_CONNECTION_RETRY_SECONDS,
            ) from None
        except CanalInvalidResponseError as err:
            _LOGGER.error(  # noqa: TRY400 - portal tracebacks may contain secrets
                "Canal portal response could not be parsed during %s "
                "synchronization after %.1f seconds: %s",
                sync_type,
                monotonic() - started,
                err,
            )
            raise UpdateFailed(str(err)) from None
        _LOGGER.debug("Portal data fetched; saving the complete snapshot atomically")
        await self._store.async_save(snapshot)
        self._previous = snapshot
        daily_count, hourly_count = _reading_counts(snapshot)
        _LOGGER.info(
            "Completed %s Canal synchronization in %.1f seconds for %d "
            "contract(s): %d daily and %d hourly readings",
            sync_type,
            monotonic() - started,
            len(snapshot.contracts),
            daily_count,
            hourly_count,
        )
        return snapshot


@dataclass(slots=True)
class CanalRuntimeData:
    """Runtime modules owned by one config entry."""

    client: CanalClient
    coordinator: CanalCoordinator
    tariff_profiles: Mapping[str, TariffProfile]


type CanalConfigEntry = ConfigEntry[CanalRuntimeData]


def _reading_counts(snapshot: ConsumptionSnapshot) -> tuple[int, int]:
    """Return privacy-safe aggregate daily and hourly counts."""
    return (
        sum(len(contract.daily_readings) for contract in snapshot.contracts.values()),
        sum(len(contract.hourly_readings) for contract in snapshot.contracts.values()),
    )
