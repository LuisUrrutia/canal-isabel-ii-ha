"""Long-term water statistics owned by the Canal integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .models import ContractConsumption

_LOGGER = logging.getLogger(__name__)


def water_statistic_id(contract_id: str) -> str:
    """Return the stable external statistic ID for one water contract."""
    return f"{DOMAIN}:water_meter_{slugify(contract_id)}"


class CanalWaterStatisticsImporter:
    """Import portal history without sharing Recorder's entity statistic."""

    def __init__(self, hass: HomeAssistant, contract_id: str) -> None:
        """Initialize one contract-owned external statistic."""
        self._hass = hass
        self._statistic_id = water_statistic_id(contract_id)
        self._last_imported_at: datetime | None = None

    @callback
    def async_import(self, contract: ContractConsumption, *, name: str) -> None:
        """Anchor hourly portal history to the physical meter reading."""
        meter_value = contract.meter_reading_m3
        meter_reading_at = contract.meter_reading_at
        if (
            "recorder" not in self._hass.config.components
            or meter_value is None
            or meter_reading_at is None
        ):
            return

        eligible = tuple(
            reading
            for reading in contract.hourly_readings
            if reading.start <= meter_reading_at
        )
        running = meter_value - sum(
            reading.volume_liters / 1000 for reading in eligible
        )
        cutoff = (
            self._last_imported_at - timedelta(days=2)
            if self._last_imported_at is not None
            else None
        )
        statistics: list[StatisticData] = []
        for reading in eligible:
            running += reading.volume_liters / 1000
            if cutoff is None or reading.start >= cutoff:
                statistics.append(
                    StatisticData(
                        start=reading.start,
                        state=running,
                        sum=running,
                    )
                )

        if (cutoff is None or meter_reading_at >= cutoff) and (
            not eligible or eligible[-1].start != meter_reading_at
        ):
            statistics.append(
                StatisticData(
                    start=meter_reading_at,
                    state=meter_value,
                    sum=meter_value,
                )
            )
        if not statistics:
            return

        _LOGGER.debug(
            "Importing %d external historical water statistic point(s)",
            len(statistics),
        )
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Canal de Isabel II · {name}",
            source=DOMAIN,
            statistic_id=self._statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        async_add_external_statistics(self._hass, metadata, statistics)
        self._last_imported_at = meter_reading_at
