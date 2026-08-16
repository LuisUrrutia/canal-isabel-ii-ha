"""Privacy-safe diagnostics for Canal de Isabel II."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tariffs import TARIFF_VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import CanalConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: CanalConfigEntry,
) -> dict[str, Any]:
    """Return health and data-shape information without personal data."""
    del hass
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    contracts = []
    snapshot_contracts = snapshot.contracts if snapshot is not None else {}
    for contract_id in sorted(snapshot_contracts):
        contract = snapshot_contracts[contract_id]
        hourly = contract.hourly_readings
        daily = contract.daily_readings
        contracts.append(
            {
                "has_meter_id": contract.meter_id is not None,
                "has_meter_reading": contract.meter_reading_m3 is not None,
                "hourly_reading_count": len(hourly),
                "first_hourly_reading": (
                    hourly[0].start.isoformat() if hourly else None
                ),
                "last_hourly_reading": (
                    hourly[-1].start.isoformat() if hourly else None
                ),
                "daily_reading_count": len(daily),
                "first_daily_reading": daily[0].day.isoformat() if daily else None,
                "last_daily_reading": daily[-1].day.isoformat() if daily else None,
            }
        )

    return {
        "config_entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": (
                str(coordinator.last_exception)
                if coordinator.last_exception is not None
                else None
            ),
        },
        "tariffs": {
            "catalog_version": TARIFF_VERSION,
            "configured_contract_count": len(entry.runtime_data.tariff_profiles),
        },
        "snapshot": {
            "fetched_at": (
                snapshot.fetched_at.isoformat() if snapshot is not None else None
            ),
            "contract_count": len(snapshot_contracts),
            "contracts": contracts,
        },
    }
