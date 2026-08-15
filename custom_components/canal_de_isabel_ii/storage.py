"""Private persistent history for Canal synchronization."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .models import (
    ConsumptionReading,
    ConsumptionSnapshot,
    ContractConsumption,
    DailyConsumption,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORAGE_VERSION = 1
_LOGGER = logging.getLogger(__name__)


class CanalHistoryStore:
    """Serialize normalized history without exposing portal implementation details."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize one private store per account entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _STORAGE_VERSION,
            f"{DOMAIN}.history.{entry_id}",
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> ConsumptionSnapshot | None:
        """Load normalized history, ignoring obsolete or malformed data."""
        payload = await self._store.async_load()
        if payload is None:
            _LOGGER.debug("No private Canal history cache was found")
            return None
        try:
            contracts = {
                item["contract_id"]: _decode_contract(item)
                for item in payload["contracts"]
            }
            snapshot = ConsumptionSnapshot(
                contracts=contracts,
                fetched_at=datetime.fromisoformat(payload["fetched_at"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Ignoring malformed private Canal history cache; a fresh "
                "background synchronization will rebuild it"
            )
            _LOGGER.debug("Cache decoding failure: %s", err, exc_info=True)
            return None
        _LOGGER.debug(
            "Decoded private Canal history cache containing %d contract(s)",
            len(snapshot.contracts),
        )
        return snapshot

    async def async_save(self, snapshot: ConsumptionSnapshot) -> None:
        """Persist one complete atomic snapshot."""
        _LOGGER.debug(
            "Saving private Canal history cache containing %d contract(s)",
            len(snapshot.contracts),
        )
        await self._store.async_save(
            {
                "fetched_at": snapshot.fetched_at.isoformat(),
                "contracts": [
                    _encode_contract(snapshot.contracts[contract_id])
                    for contract_id in sorted(snapshot.contracts)
                ],
            }
        )
        _LOGGER.debug("Private Canal history cache saved successfully")

    async def async_remove(self) -> None:
        """Remove private history when its config entry is deleted."""
        await self._store.async_remove()
        _LOGGER.info("Private Canal history cache removed")


def _encode_contract(contract: ContractConsumption) -> dict[str, Any]:
    """Encode one normalized contract for private JSON storage."""
    return {
        "contract_id": contract.contract_id,
        "meter_id": contract.meter_id,
        "address": contract.address,
        "meter_reading_m3": contract.meter_reading_m3,
        "meter_reading_at": (
            contract.meter_reading_at.isoformat()
            if contract.meter_reading_at is not None
            else None
        ),
        "daily_readings": [
            {
                "day": reading.day.isoformat(),
                "volume_liters": reading.volume_liters,
                "is_estimated": reading.is_estimated,
            }
            for reading in contract.daily_readings
        ],
        "hourly_readings": [
            {
                "start": reading.start.isoformat(),
                "volume_liters": reading.volume_liters,
                "is_estimated": reading.is_estimated,
            }
            for reading in contract.hourly_readings
        ],
    }


def _decode_contract(payload: dict[str, Any]) -> ContractConsumption:
    """Decode one normalized contract from private JSON storage."""
    meter_reading_at = payload["meter_reading_at"]
    return ContractConsumption(
        contract_id=payload["contract_id"],
        meter_id=payload["meter_id"],
        address=payload["address"],
        meter_reading_m3=payload["meter_reading_m3"],
        meter_reading_at=(
            datetime.fromisoformat(meter_reading_at)
            if meter_reading_at is not None
            else None
        ),
        daily_readings=tuple(
            DailyConsumption(
                day=date.fromisoformat(reading["day"]),
                volume_liters=reading["volume_liters"],
                is_estimated=reading["is_estimated"],
            )
            for reading in payload["daily_readings"]
        ),
        hourly_readings=tuple(
            ConsumptionReading(
                start=datetime.fromisoformat(reading["start"]),
                volume_liters=reading["volume_liters"],
                is_estimated=reading["is_estimated"],
            )
            for reading in payload["hourly_readings"]
        ),
    )
