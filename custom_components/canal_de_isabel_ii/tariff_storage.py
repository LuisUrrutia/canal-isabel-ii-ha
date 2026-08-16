"""Private persistent tariff profiles configured per Canal contract."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .tariffs import SewerProvider, SupplyType, TariffProfile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_STORAGE_VERSION = 1
_LOGGER = logging.getLogger(__name__)


class CanalTariffProfileStore:
    """Keep billing facts separate from credentials and portal history."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize one private store per account entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _STORAGE_VERSION,
            f"{DOMAIN}.tariffs.{entry_id}",
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> dict[str, TariffProfile]:
        """Load every valid per-contract tariff profile."""
        payload = await self._store.async_load()
        if payload is None:
            _LOGGER.debug("No private Canal tariff profiles were found")
            return {}
        try:
            profiles = {
                item["contract_id"]: _decode_profile(item)
                for item in payload["profiles"]
            }
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Ignoring malformed private Canal tariff profiles; configure "
                "pricing again from the integration options"
            )
            _LOGGER.debug("Tariff profile decoding failure: %s", err, exc_info=True)
            return {}
        _LOGGER.info("Loaded %d private Canal tariff profile(s)", len(profiles))
        return profiles

    async def async_save(self, profiles: Mapping[str, TariffProfile]) -> None:
        """Persist all profiles atomically without logging contract identifiers."""
        await self._store.async_save(
            {
                "profiles": [
                    _encode_profile(contract_id, profiles[contract_id])
                    for contract_id in sorted(profiles)
                ]
            }
        )
        _LOGGER.info("Saved %d private Canal tariff profile(s)", len(profiles))

    async def async_remove(self) -> None:
        """Remove private profiles when the account entry is deleted."""
        await self._store.async_remove()
        _LOGGER.info("Private Canal tariff profiles removed")


def _encode_profile(contract_id: str, profile: TariffProfile) -> dict[str, Any]:
    """Encode one profile using stable JSON primitives."""
    return {
        "contract_id": contract_id,
        "supply_type": profile.supply_type,
        "sewer_provider": profile.sewer_provider,
        "meter_diameter_mm": profile.meter_diameter_mm,
        "supplied_uses": profile.supplied_uses,
        "billing_period_start": profile.billing_period_start.isoformat(),
        "billing_cycle_days": profile.billing_cycle_days,
        "municipal_sewer_rate_eur_m3": str(profile.municipal_sewer_rate_eur_m3),
    }


def _decode_profile(payload: dict[str, Any]) -> TariffProfile:
    """Decode and validate one profile from private JSON storage."""
    return TariffProfile(
        supply_type=SupplyType(payload["supply_type"]),
        sewer_provider=SewerProvider(payload["sewer_provider"]),
        meter_diameter_mm=int(payload["meter_diameter_mm"]),
        supplied_uses=int(payload["supplied_uses"]),
        billing_period_start=date.fromisoformat(payload["billing_period_start"]),
        billing_cycle_days=int(payload["billing_cycle_days"]),
        municipal_sewer_rate_eur_m3=Decimal(payload["municipal_sewer_rate_eur_m3"]),
    )
