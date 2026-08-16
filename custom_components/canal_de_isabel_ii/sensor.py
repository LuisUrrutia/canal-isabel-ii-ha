"""Water meter and interval-consumption sensors."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, override
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CanalConfigEntry, CanalCoordinator
from .statistics import CanalCostStatisticsImporter, CanalWaterStatisticsImporter
from .tariffs import (
    TARIFF_SOURCE_URL,
    TARIFF_VERSION,
    BillEstimate,
    TariffProfile,
    billing_period_for,
    calculate_accrued_bill,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import ContractConsumption

PARALLEL_UPDATES = 0
_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")
_ONE_DAY = timedelta(days=1)
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
        entities: list[CanalContractSensor] = []
        for contract_id in sorted(new_contracts):
            entities.extend(
                (
                    CanalMeterReadingSensor(coordinator, contract_id),
                    CanalHourlyConsumptionSensor(coordinator, contract_id),
                    CanalDailyConsumptionSensor(coordinator, contract_id),
                )
            )
            profile = entry.runtime_data.tariff_profiles.get(contract_id)
            if profile is not None:
                entities.append(
                    CanalEstimatedBillSensor(coordinator, contract_id, profile)
                )
        _LOGGER.info(
            "Adding %d newly discovered Canal contract(s) as %d sensor entities",
            len(new_contracts),
            len(entities),
        )
        async_add_entities(entities)
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


class CanalEstimatedBillSensor(CanalContractSensor):
    """Live invoice estimate using the configured tariff profile."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "estimated_bill"

    def __init__(
        self,
        coordinator: CanalCoordinator,
        contract_id: str,
        profile: TariffProfile,
    ) -> None:
        """Initialize a monetary sensor for one configured contract."""
        super().__init__(coordinator, contract_id)
        self._profile = profile
        self._attr_unique_id = f"canal_ii_estimated_bill_{contract_id}"
        self._statistics = CanalCostStatisticsImporter(
            coordinator.hass,
            contract_id,
            profile,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Import available daily costs after Home Assistant adds the entity."""
        await super().async_added_to_hass()
        self._import_historical_costs()

    @override
    def _handle_coordinator_update(self) -> None:
        """Refresh the estimate and reimport corrected daily costs."""
        self._import_historical_costs()
        super()._handle_coordinator_update()

    @property
    @override
    def available(self) -> bool:
        """Require both portal history and a supported tariff period."""
        return super().available and self._estimate() is not None

    @property
    @override
    def native_value(self) -> Decimal | None:
        """Return the estimated current invoice total in euros."""
        estimate = self._estimate()
        return estimate[0].total_eur if estimate is not None else None

    @property
    @override
    def last_reset(self) -> datetime | None:
        """Reset statistics at the configured nominal billing-cycle boundary."""
        estimate = self._estimate()
        if estimate is None:
            return None
        return datetime.combine(
            estimate[0].period_start,
            time.min,
            tzinfo=_PORTAL_TIME_ZONE,
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose an auditable invoice-shaped breakdown without private IDs."""
        estimate = self._estimate()
        if estimate is None:
            return {
                "tariff_version": TARIFF_VERSION,
                "tariff_source": TARIFF_SOURCE_URL,
                "is_estimate": True,
            }
        bill, observed_days = estimate
        elapsed_days = (bill.period_end - bill.period_start).days
        return {
            "billing_period_start": bill.period_start.isoformat(),
            "calculated_through": bill.period_end.isoformat(),
            "volume_m3": float(bill.volume_m3),
            "variable_cost": float(bill.variable_eur),
            "fixed_cost": float(bill.fixed_eur),
            "taxable_base": float(bill.taxable_eur),
            "vat": float(bill.vat_eur),
            "non_taxable_cost": float(bill.non_taxable_eur),
            "observed_days": observed_days,
            "elapsed_days": elapsed_days,
            "history_complete": observed_days == elapsed_days,
            "tariff_version": TARIFF_VERSION,
            "tariff_source": TARIFF_SOURCE_URL,
            "is_estimate": True,
        }

    def _estimate(self) -> tuple[BillEstimate, int] | None:
        """Calculate from daily portal readings in the current nominal cycle."""
        latest = self.contract.latest_daily
        if latest is None:
            return None
        period_start, period_limit = billing_period_for(self._profile, latest.day)
        period_end = min(latest.day + _ONE_DAY, period_limit)
        readings = tuple(
            reading
            for reading in self.contract.daily_readings
            if period_start <= reading.day < period_end
        )
        volume_m3 = (
            sum(
                (Decimal(str(reading.volume_liters)) for reading in readings),
                start=Decimal(0),
            )
            / 1000
        )
        try:
            bill = calculate_accrued_bill(
                volume_m3,
                period_start,
                period_limit,
                period_end,
                self._profile,
            )
        except ValueError:
            return None
        return bill, len({reading.day for reading in readings})

    def _import_historical_costs(self) -> None:
        """Import the portal history into its dedicated cost statistic."""
        self._statistics.async_import(self.contract, name=self.name)
