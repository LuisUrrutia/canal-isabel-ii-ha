"""Long-term water statistics owned by the Canal integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import CURRENCY_EURO, UnitOfVolume
from homeassistant.core import callback
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import VolumeConverter

from custom_components.canal_de_isabel_ii.billing import (
    TariffProfile,
    billing_period_for,
    calculate_accrued_bill,
)
from custom_components.canal_de_isabel_ii.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .models import ContractConsumption

_LOGGER = logging.getLogger(__name__)
_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")
_ONE_DAY = timedelta(days=1)
_LITERS_PER_CUBIC_METER = Decimal(1000)


def water_statistic_id(contract_id: str) -> str:
    """Return the stable external statistic ID for one water contract."""
    return f"{DOMAIN}:water_meter_{slugify(contract_id)}"


def cost_statistic_id(contract_id: str) -> str:
    """Return the stable external cost statistic ID for one water contract."""
    return f"{DOMAIN}:water_cost_{slugify(contract_id)}"


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
        meter_value_decimal = Decimal(str(meter_value))
        running = meter_value_decimal - sum(
            (
                Decimal(str(reading.volume_liters)) / _LITERS_PER_CUBIC_METER
                for reading in eligible
            ),
            start=Decimal(0),
        )
        cutoff = (
            self._last_imported_at - timedelta(days=2)
            if self._last_imported_at is not None
            else None
        )
        statistics: list[StatisticData] = []
        for reading in eligible:
            running += Decimal(str(reading.volume_liters)) / _LITERS_PER_CUBIC_METER
            if cutoff is None or reading.start >= cutoff:
                value = float(running)
                statistics.append(
                    StatisticData(
                        start=reading.start,
                        state=value,
                        sum=value,
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


class CanalCostStatisticsImporter:
    """Import daily tariff accrual as a monotonic external statistic."""

    def __init__(
        self,
        hass: HomeAssistant,
        contract_id: str,
        profile: TariffProfile,
    ) -> None:
        """Initialize one contract-owned external cost statistic."""
        self._hass = hass
        self._profile = profile
        self._statistic_id = cost_statistic_id(contract_id)
        self._history_rebuilt = False

    @callback
    def async_import(self, contract: ContractConsumption, *, name: str) -> None:
        """Rebuild cost history from the stored daily portal readings."""
        if "recorder" not in self._hass.config.components:
            return

        daily_volumes: dict[date, Decimal] = {}
        for reading in contract.daily_readings:
            daily_volumes[reading.day] = (
                daily_volumes.get(reading.day, Decimal(0))
                + Decimal(str(reading.volume_liters)) / 1000
            )
        if not daily_volumes:
            return

        completed_cost = Decimal(0)
        period: tuple[date, date] | None = None
        period_volume = Decimal(0)
        last_period_cost = Decimal(0)
        statistics: list[StatisticData] = []
        skipped_points = 0

        for day, daily_volume in sorted(daily_volumes.items()):
            current_period = billing_period_for(self._profile, day)
            if period != current_period:
                if period is not None:
                    completed_cost += last_period_cost
                period = current_period
                period_volume = Decimal(0)
                last_period_cost = Decimal(0)

            period_volume += daily_volume
            period_start, period_end = current_period
            calculated_through = min(day + _ONE_DAY, period_end)
            try:
                last_period_cost = calculate_accrued_bill(
                    period_volume,
                    period_start,
                    period_end,
                    calculated_through,
                    self._profile,
                ).total_eur
            except ValueError:
                skipped_points += 1
                continue

            statistics.append(
                StatisticData(
                    start=datetime.combine(
                        day,
                        time.min,
                        tzinfo=_PORTAL_TIME_ZONE,
                    ),
                    state=float(last_period_cost),
                    sum=float(completed_cost + last_period_cost),
                )
            )

        if not statistics:
            return
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Canal de Isabel II · {name} cost estimate",
            source=DOMAIN,
            statistic_id=self._statistic_id,
            unit_class=None,
            unit_of_measurement=CURRENCY_EURO,
        )
        self._async_publish(metadata, statistics, skipped_points=skipped_points)

    @callback
    def _async_publish(
        self,
        metadata: StatisticMetaData,
        statistics: list[StatisticData],
        *,
        skipped_points: int,
    ) -> None:
        """Publish corrected cost history, replacing legacy timestamps once."""
        if self._history_rebuilt:
            _LOGGER.debug(
                "Importing %d day-aligned historical water cost point(s); "
                "skipped %d point(s) outside the tariff catalog",
                len(statistics),
                skipped_points,
            )
            async_add_external_statistics(self._hass, metadata, statistics)
            return

        self._history_rebuilt = True
        _LOGGER.info(
            "Rebuilding the external water cost statistic with %d day-aligned "
            "point(s); skipped %d point(s) outside the tariff catalog",
            len(statistics),
            skipped_points,
        )

        def import_after_clear() -> None:
            """Import corrected timestamps after Recorder removes stale points."""
            self._hass.add_job(
                async_add_external_statistics,
                self._hass,
                metadata,
                statistics,
            )

        get_instance(self._hass).async_clear_statistics(
            [self._statistic_id],
            on_done=import_after_clear,
        )
