"""Domain object factories used through public integration seams."""

from datetime import UTC, date, datetime

from custom_components.canal_de_isabel_ii.consumption import (
    ConsumptionReading,
    ConsumptionSnapshot,
    ContractConsumption,
    DailyConsumption,
)


def make_snapshot() -> ConsumptionSnapshot:
    """Create representative, deterministic portal data."""
    contract = ContractConsumption(
        contract_id="contract-123",
        meter_id="meter-456",
        address="Calle de Alcalá 1",
        meter_reading_m3=125.5,
        meter_reading_at=datetime(2026, 8, 15, 5, tzinfo=UTC),
        daily_readings=(
            DailyConsumption(
                day=date(2026, 8, 13),
                volume_liters=9.0,
            ),
            DailyConsumption(
                day=date(2026, 8, 14),
                volume_liters=15.5,
            ),
        ),
        hourly_readings=(
            ConsumptionReading(
                start=datetime(2026, 8, 14, 8, tzinfo=UTC),
                volume_liters=12.5,
            ),
            ConsumptionReading(
                start=datetime(2026, 8, 14, 9, tzinfo=UTC),
                volume_liters=3.0,
            ),
        ),
    )
    return ConsumptionSnapshot(
        contracts={contract.contract_id: contract},
        fetched_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )
