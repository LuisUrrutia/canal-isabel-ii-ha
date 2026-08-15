"""Water meter and interval-consumption sensors."""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, override
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CanalConfigEntry, CanalCoordinator
from .statistics import CanalWaterStatisticsImporter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import ContractConsumption

PARALLEL_UPDATES = 0
_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")
_LOGGER = logging.getLogger(__name__)


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
        snapshot = coordinator.data
        if snapshot is None:
            _LOGGER.debug(
                "No Canal snapshot is available yet; waiting to discover contracts"
            )
            return
        new_contracts = set(snapshot.contracts) - known_contracts
        if not new_contracts:
            return
        _LOGGER.info(
            "Adding %d newly discovered Canal contract(s) as %d sensor entities",
            len(new_contracts),
            len(new_contracts) * 3,
        )
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
        self._statistics = CanalWaterStatisticsImporter(
            coordinator.hass,
            contract_id,
        )

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
        """Import portal history into its dedicated external statistic."""
        self._statistics.async_import(self.contract, name=self.name)


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
