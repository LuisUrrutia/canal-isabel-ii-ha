"""Water meter and interval-consumption sensors."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, override
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN
from .coordinator import CanalConfigEntry, CanalCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import ContractConsumption

PARALLEL_UPDATES = 0
_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CanalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors and add contracts discovered on later refreshes."""
    del hass
    coordinator = entry.runtime_data.coordinator
    known_contracts: set[str] = set()

    @callback
    def async_add_new_contracts() -> None:
        new_contracts = set(coordinator.data.contracts) - known_contracts
        if not new_contracts:
            return
        async_add_entities(
            entity
            for contract_id in sorted(new_contracts)
            for entity in (
                CanalMeterReadingSensor(coordinator, contract_id),
                CanalHourlyConsumptionSensor(coordinator, contract_id),
                CanalDailyConsumptionSensor(coordinator, contract_id),
            )
        )
        known_contracts.update(new_contracts)

    async_add_new_contracts()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_contracts))


class CanalContractSensor(CoordinatorEntity[CanalCoordinator], SensorEntity):
    """Base class for a sensor belonging to one Canal contract."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.WATER

    def __init__(self, coordinator: CanalCoordinator, contract_id: str) -> None:
        """Initialize the contract sensor."""
        super().__init__(coordinator)
        self._contract_id = contract_id

    @property
    def contract(self) -> ContractConsumption:
        """Return current in-memory data for this sensor's contract."""
        return self.coordinator.data.contracts[self._contract_id]

    @property
    @override
    def available(self) -> bool:
        """Return whether the coordinator still contains this contract."""
        return (
            super().available and self._contract_id in self.coordinator.data.contracts
        )

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Represent every water contract as one Home Assistant device."""
        contract = self.contract
        return DeviceInfo(
            identifiers={(DOMAIN, self._contract_id)},
            name=contract.address or f"Contrato {self._contract_id}",
            manufacturer="Canal de Isabel II",
            model="Contador de agua",
            serial_number=contract.meter_id,
        )


class CanalMeterReadingSensor(CanalContractSensor):
    """Cumulative physical water-meter reading."""

    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "meter_reading"

    def __init__(self, coordinator: CanalCoordinator, contract_id: str) -> None:
        """Initialize the cumulative meter sensor."""
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"canal_ii_meter_{contract_id}"
        self._last_imported_at: datetime | None = None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the latest physical meter reading in cubic meters."""
        return self.contract.meter_reading_m3

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the timestamp attached to the portal meter reading."""
        reading_at = self.contract.meter_reading_at
        return {"reading_at": reading_at.isoformat() if reading_at else None}

    @override
    async def async_added_to_hass(self) -> None:
        """Import available history after Home Assistant assigns an entity ID."""
        await super().async_added_to_hass()
        self._import_historical_statistics()

    @override
    def _handle_coordinator_update(self) -> None:
        """Refresh state and import new or recently corrected readings."""
        self._import_historical_statistics()
        super()._handle_coordinator_update()

    def _import_historical_statistics(self) -> None:
        """Anchor hourly history to the portal's physical meter reading."""
        contract = self.contract
        meter_value = contract.meter_reading_m3
        meter_reading_at = contract.meter_reading_at
        if (
            "recorder" not in self.hass.config.components
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

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self.name,
            source="recorder",
            statistic_id=self.entity_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        async_import_statistics(self.hass, metadata, statistics)
        self._last_imported_at = meter_reading_at


class CanalHourlyConsumptionSensor(CanalContractSensor):
    """Water consumed during the latest hourly interval."""

    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "hourly_consumption"

    def __init__(self, coordinator: CanalCoordinator, contract_id: str) -> None:
        """Initialize the latest hourly sensor."""
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"canal_ii_consumption_{contract_id}"

    @property
    @override
    def native_value(self) -> float | None:
        """Return the latest hourly consumption in liters."""
        reading = self.contract.latest_hourly
        return reading.volume_liters if reading is not None else None

    @property
    @override
    def last_reset(self) -> datetime | None:
        """Return the start of the latest consumption interval."""
        reading = self.contract.latest_hourly
        return reading.start if reading is not None else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether the latest portal interval was estimated."""
        reading = self.contract.latest_hourly
        return {
            "reading_start": reading.start.isoformat() if reading else None,
            "is_estimated": reading.is_estimated if reading else None,
        }


class CanalDailyConsumptionSensor(CanalContractSensor):
    """Water consumed during the latest local calendar day."""

    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "daily_consumption"

    def __init__(self, coordinator: CanalCoordinator, contract_id: str) -> None:
        """Initialize the latest daily sensor."""
        super().__init__(coordinator, contract_id)
        self._attr_unique_id = f"canal_ii_daily_{contract_id}"

    @property
    @override
    def native_value(self) -> float | None:
        """Return the latest daily consumption in liters."""
        reading = self.contract.latest_daily
        return reading.volume_liters if reading is not None else None

    @property
    @override
    def last_reset(self) -> datetime | None:
        """Return local midnight for the latest daily value."""
        reading = self.contract.latest_daily
        if reading is None:
            return None
        return datetime.combine(reading.day, time.min, tzinfo=_PORTAL_TIME_ZONE)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether the latest portal day was estimated."""
        reading = self.contract.latest_daily
        return {
            "reading_day": reading.day.isoformat() if reading else None,
            "is_estimated": reading.is_estimated if reading else None,
        }
