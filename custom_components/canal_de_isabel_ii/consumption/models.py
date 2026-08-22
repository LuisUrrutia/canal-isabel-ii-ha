"""Immutable domain models for Canal de Isabel II data."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ConsumptionReading:
    """Water consumed during one hourly interval."""

    start: datetime
    volume_liters: float
    is_estimated: bool = False


@dataclass(frozen=True, slots=True)
class DailyConsumption:
    """Water consumed during one local calendar day."""

    day: date
    volume_liters: float
    is_estimated: bool = False


@dataclass(frozen=True, slots=True)
class ContractConsumption:
    """Normalized portal history and meter state for one contract."""

    contract_id: str
    meter_id: str | None
    address: str | None
    meter_reading_m3: float | None
    meter_reading_at: datetime | None
    daily_readings: tuple[DailyConsumption, ...]
    hourly_readings: tuple[ConsumptionReading, ...]

    @property
    def latest_hourly(self) -> ConsumptionReading | None:
        """Return the most recent hourly interval, if available."""
        return self.hourly_readings[-1] if self.hourly_readings else None

    @property
    def latest_daily(self) -> DailyConsumption | None:
        """Return the most recent daily interval, if available."""
        return self.daily_readings[-1] if self.daily_readings else None


@dataclass(frozen=True, slots=True)
class ConsumptionSnapshot:
    """One atomic view of every contract available to an account."""

    contracts: Mapping[str, ContractConsumption]
    fetched_at: datetime

    def __post_init__(self) -> None:
        """Prevent callers from mutating coordinator data in place."""
        object.__setattr__(self, "contracts", MappingProxyType(dict(self.contracts)))
