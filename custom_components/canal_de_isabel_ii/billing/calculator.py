"""Versioned Canal tariff catalog and domestic bill calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_ZERO = Decimal(0)
_DAYS_PER_BILLING_PERIOD = Decimal(60)
_BASE_BLOCK_VOLUME_M3 = Decimal(20)
_CENT = Decimal("0.01")
_WHOLE_CUBIC_METER = Decimal(1)
_MIN_METER_DIAMETER_MM = 10
_MAX_METER_DIAMETER_MM = 500
_MAX_SUPPLIED_USES = 10_000
_MIN_BILLING_CYCLE_DAYS = 30
_MAX_BILLING_CYCLE_DAYS = 90
_TARIFF_YEAR = 2026
_TARIFF_END = date(2027, 1, 1)

TARIFF_VERSION = "2026"
TARIFF_SOURCE_URL = "https://www.canaldeisabelsegunda.es/documents/d/site/tarifas-1"

_ADDUCTION_RATES = {
    "winter": (
        Decimal("0.3146"),
        Decimal("0.6286"),
        Decimal("1.6199"),
        Decimal("1.8629"),
    ),
    "summer": (
        Decimal("0.3146"),
        Decimal("0.7854"),
        Decimal("2.4300"),
        Decimal("2.7945"),
    ),
}
_DISTRIBUTION_RATES = (
    Decimal("0.1416"),
    Decimal("0.2409"),
    Decimal("0.6174"),
    Decimal("0.7100"),
)
_CANAL_SEWER_RATES = (
    Decimal("0.1161"),
    Decimal("0.1378"),
    Decimal("0.1812"),
    Decimal("0.2084"),
)
_PURIFICATION_RATES = (
    Decimal("0.3304"),
    Decimal("0.4074"),
    Decimal("0.6684"),
    Decimal("0.7686"),
)
_ADDUCTION_FIXED_RATE = Decimal("0.0188")
_DISTRIBUTION_FIXED_RATE = Decimal("0.0085")
_CANAL_SEWER_FIXED_RATE = Decimal("1.1353")
_PURIFICATION_FIXED_RATE = Decimal("3.3281")
_CANAL_VAT_RATE = Decimal("0.10")


class SupplyType(StrEnum):
    """Domestic supply shapes supported by the published tariff."""

    SINGLE_DWELLING = "single_dwelling"
    MULTI_DWELLING = "multi_dwelling"
    DOMESTIC_EQUIVALENT = "domestic_equivalent"


class SewerProvider(StrEnum):
    """Party whose sewer tariff applies to the contract."""

    CANAL = "canal"
    MUNICIPALITY = "municipality"


@dataclass(frozen=True, slots=True)
class TariffProfile:
    """Contract facts that cannot be obtained from the tariff catalog."""

    supply_type: SupplyType
    sewer_provider: SewerProvider
    meter_diameter_mm: int
    supplied_uses: int
    billing_period_start: date
    billing_cycle_days: int = 60
    municipal_sewer_rate_eur_m3: Decimal = _ZERO

    def __post_init__(self) -> None:
        """Reject profiles that would silently produce invalid bills."""
        if not (
            _MIN_METER_DIAMETER_MM <= self.meter_diameter_mm <= _MAX_METER_DIAMETER_MM
        ):
            msg = "Meter diameter must be between 10 and 500 millimeters"
            raise ValueError(msg)
        if not 1 <= self.supplied_uses <= _MAX_SUPPLIED_USES:
            msg = "Supplied uses must be between 1 and 10000"
            raise ValueError(msg)
        if not (
            _MIN_BILLING_CYCLE_DAYS
            <= self.billing_cycle_days
            <= _MAX_BILLING_CYCLE_DAYS
        ):
            msg = "Billing cycle must be between 30 and 90 days"
            raise ValueError(msg)
        if self.municipal_sewer_rate_eur_m3 < 0:
            msg = "Municipal sewer rate cannot be negative"
            raise ValueError(msg)
        if (
            self.sewer_provider is SewerProvider.MUNICIPALITY
            and self.municipal_sewer_rate_eur_m3 == 0
        ):
            msg = "Municipal sewer rate is required for municipal service"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BillEstimate:
    """Invoice-shaped result with every rounding decision already applied."""

    period_start: date
    period_end: date
    volume_m3: Decimal
    adduction_variable_eur: Decimal
    distribution_variable_eur: Decimal
    sewer_variable_eur: Decimal
    purification_variable_eur: Decimal
    adduction_fixed_eur: Decimal
    distribution_fixed_eur: Decimal
    sewer_fixed_eur: Decimal
    purification_fixed_eur: Decimal
    taxable_eur: Decimal
    vat_eur: Decimal
    non_taxable_eur: Decimal
    total_eur: Decimal
    finalized: bool

    @property
    def variable_eur(self) -> Decimal:
        """Return every volume-dependent charge, including municipal sewer."""
        return (
            self.adduction_variable_eur
            + self.distribution_variable_eur
            + self.sewer_variable_eur
            + self.purification_variable_eur
        )

    @property
    def fixed_eur(self) -> Decimal:
        """Return every availability charge."""
        return (
            self.adduction_fixed_eur
            + self.distribution_fixed_eur
            + self.sewer_fixed_eur
            + self.purification_fixed_eur
        )


def billing_period_for(profile: TariffProfile, day: date) -> tuple[date, date]:
    """Return the configured nominal billing cycle containing one local day."""
    start = profile.billing_period_start
    step = timedelta(days=profile.billing_cycle_days)
    if day >= start:
        elapsed_cycles = (day - start).days // profile.billing_cycle_days
        start += step * elapsed_cycles
    else:
        elapsed_cycles = ((start - day).days - 1) // profile.billing_cycle_days + 1
        start -= step * elapsed_cycles
    return start, start + step


def calculate_bill(
    volume_m3: Decimal,
    period_start: date,
    period_end: date,
    profile: TariffProfile,
    *,
    finalized: bool,
) -> BillEstimate:
    """Calculate one domestic bill or a live partial-period estimate."""
    if volume_m3 < 0:
        msg = "Consumption volume cannot be negative"
        raise ValueError(msg)
    period_days = (period_end - period_start).days
    if period_days <= 0:
        msg = "Billing period end must be after its start"
        raise ValueError(msg)
    if period_start.year != _TARIFF_YEAR or period_end > _TARIFF_END:
        msg = "The bundled tariff catalog only supports 2026 billing periods"
        raise ValueError(msg)

    seasonal_segments = _seasonal_segments(period_start, period_end)
    seasonal_volumes = _allocate_volume(
        volume_m3,
        tuple(days for _, days in seasonal_segments),
        finalized=finalized,
    )
    adduction_variable = _ZERO
    adduction_fixed = _ZERO
    for (season, days), segment_volume in zip(
        seasonal_segments,
        seasonal_volumes,
        strict=True,
    ):
        adduction_variable += _tiered_charge(
            segment_volume,
            days,
            _ADDUCTION_RATES[season],
            finalized=finalized,
        )
        adduction_fixed += _money(
            _ADDUCTION_FIXED_RATE
            * _fixed_basis(profile)
            * Decimal(days)
            / _DAYS_PER_BILLING_PERIOD
        )

    distribution_variable = _tiered_charge(
        volume_m3,
        period_days,
        _DISTRIBUTION_RATES,
        finalized=finalized,
    )
    distribution_fixed = _money(
        _DISTRIBUTION_FIXED_RATE
        * _fixed_basis(profile)
        * Decimal(period_days)
        / _DAYS_PER_BILLING_PERIOD
    )
    purification_variable = _tiered_charge(
        volume_m3,
        period_days,
        _PURIFICATION_RATES,
        finalized=finalized,
    )
    purification_fixed = _money(
        _PURIFICATION_FIXED_RATE
        * Decimal(profile.supplied_uses)
        * Decimal(period_days)
        / _DAYS_PER_BILLING_PERIOD
    )

    sewer_fixed = _ZERO
    non_taxable = _ZERO
    if profile.sewer_provider is SewerProvider.MUNICIPALITY:
        sewer_variable = _money(volume_m3 * profile.municipal_sewer_rate_eur_m3)
        non_taxable = sewer_variable
    else:
        sewer_variable = _tiered_charge(
            volume_m3,
            period_days,
            _CANAL_SEWER_RATES,
            finalized=finalized,
        )
        sewer_fixed = _money(
            _canal_sewer_fixed_basis(profile)
            * Decimal(period_days)
            / _DAYS_PER_BILLING_PERIOD
        )

    taxable = (
        adduction_variable
        + distribution_variable
        + purification_variable
        + adduction_fixed
        + distribution_fixed
        + purification_fixed
    )
    if profile.sewer_provider is SewerProvider.CANAL:
        taxable += sewer_variable + sewer_fixed
    vat = _money(taxable * _CANAL_VAT_RATE)
    total = taxable + vat + non_taxable

    return BillEstimate(
        period_start=period_start,
        period_end=period_end,
        volume_m3=volume_m3,
        adduction_variable_eur=adduction_variable,
        distribution_variable_eur=distribution_variable,
        sewer_variable_eur=sewer_variable,
        purification_variable_eur=purification_variable,
        adduction_fixed_eur=adduction_fixed,
        distribution_fixed_eur=distribution_fixed,
        sewer_fixed_eur=sewer_fixed,
        purification_fixed_eur=purification_fixed,
        taxable_eur=taxable,
        vat_eur=vat,
        non_taxable_eur=non_taxable,
        total_eur=total,
        finalized=finalized,
    )


def calculate_accrued_bill(
    volume_m3: Decimal,
    period_start: date,
    period_end: date,
    calculated_through: date,
    profile: TariffProfile,
) -> BillEstimate:
    """Return a monotonic bill accrued within one nominal billing period.

    Variable charges use the thresholds of the complete billing period, while
    availability charges accrue through the last observed day. This prevents
    progressive-block thresholds from making Energy's cost total decrease as
    an unfinished period grows, and converges to the invoice total at period
    end.
    """
    if not period_start < calculated_through <= period_end:
        msg = "Calculated-through date must fall inside the billing period"
        raise ValueError(msg)

    full_bill = calculate_bill(
        volume_m3,
        period_start,
        period_end,
        profile,
        finalized=False,
    )
    full_zero_volume = calculate_bill(
        _ZERO,
        period_start,
        period_end,
        profile,
        finalized=False,
    )
    accrued_fixed = calculate_bill(
        _ZERO,
        period_start,
        calculated_through,
        profile,
        finalized=False,
    )

    def accrued(field: str) -> Decimal:
        """Combine full-period variable and elapsed-period fixed charges."""
        return (
            getattr(full_bill, field)
            - getattr(full_zero_volume, field)
            + getattr(accrued_fixed, field)
        )

    return BillEstimate(
        period_start=period_start,
        period_end=calculated_through,
        volume_m3=volume_m3,
        adduction_variable_eur=accrued("adduction_variable_eur"),
        distribution_variable_eur=accrued("distribution_variable_eur"),
        sewer_variable_eur=accrued("sewer_variable_eur"),
        purification_variable_eur=accrued("purification_variable_eur"),
        adduction_fixed_eur=accrued("adduction_fixed_eur"),
        distribution_fixed_eur=accrued("distribution_fixed_eur"),
        sewer_fixed_eur=accrued("sewer_fixed_eur"),
        purification_fixed_eur=accrued("purification_fixed_eur"),
        taxable_eur=accrued("taxable_eur"),
        vat_eur=accrued("vat_eur"),
        non_taxable_eur=accrued("non_taxable_eur"),
        total_eur=accrued("total_eur"),
        finalized=False,
    )


def _seasonal_segments(start: date, end: date) -> tuple[tuple[str, int], ...]:
    """Split an invoice whenever the adduction season changes."""
    segments: list[tuple[str, int]] = []
    cursor = start
    while cursor < end:
        summer_start = date(cursor.year, 6, 1)
        winter_start = date(cursor.year, 10, 1)
        if summer_start <= cursor < winter_start:
            season = "summer"
            boundary = winter_start
        elif cursor < summer_start:
            season = "winter"
            boundary = summer_start
        else:
            season = "winter"
            boundary = date(cursor.year + 1, 6, 1)
        segment_end = min(boundary, end)
        segments.append((season, (segment_end - cursor).days))
        cursor = segment_end
    return tuple(segments)


def _allocate_volume(
    volume_m3: Decimal,
    day_counts: tuple[int, ...],
    *,
    finalized: bool,
) -> tuple[Decimal, ...]:
    """Allocate register consumption proportionally across seasonal segments."""
    total_days = sum(day_counts)
    cumulative_days = 0
    previous_volume = _ZERO
    allocated: list[Decimal] = []
    for index, days in enumerate(day_counts):
        cumulative_days += days
        if index == len(day_counts) - 1:
            cumulative_volume = volume_m3
        else:
            cumulative_volume = (
                volume_m3 * Decimal(cumulative_days) / Decimal(total_days)
            )
            if finalized:
                cumulative_volume = cumulative_volume.quantize(
                    _WHOLE_CUBIC_METER,
                    rounding=ROUND_HALF_UP,
                )
        allocated.append(cumulative_volume - previous_volume)
        previous_volume = cumulative_volume
    return tuple(allocated)


def _tiered_charge(
    volume_m3: Decimal,
    days: int,
    rates: tuple[Decimal, Decimal, Decimal, Decimal],
    *,
    finalized: bool,
) -> Decimal:
    """Apply the four prorated bimonthly blocks and round each invoice line."""
    upper_bounds: list[Decimal] = []
    for block in range(1, 4):
        upper = (
            _BASE_BLOCK_VOLUME_M3
            * Decimal(days)
            * Decimal(block)
            / _DAYS_PER_BILLING_PERIOD
        )
        if finalized:
            upper = upper.quantize(_WHOLE_CUBIC_METER, rounding=ROUND_HALF_UP)
        upper_bounds.append(upper)

    remaining = volume_m3
    previous_upper = _ZERO
    charge = _ZERO
    for rate, upper in zip(rates[:3], upper_bounds, strict=True):
        block_volume = min(remaining, max(_ZERO, upper - previous_upper))
        charge += _money(block_volume * rate)
        remaining -= block_volume
        previous_upper = upper
    charge += _money(max(_ZERO, remaining) * rates[3])
    return charge


def _fixed_basis(profile: TariffProfile) -> Decimal:
    """Return the published D/N basis for adduction and distribution."""
    uses = Decimal(profile.supplied_uses)
    if profile.supply_type is SupplyType.MULTI_DWELLING:
        return uses * Decimal(15**2 + 225)
    return Decimal(profile.meter_diameter_mm**2) + Decimal(225) * uses


def _canal_sewer_fixed_basis(profile: TariffProfile) -> Decimal:
    """Return the published domestic or domestic-equivalent sewer basis."""
    if profile.supply_type is SupplyType.DOMESTIC_EQUIVALENT:
        return _CANAL_SEWER_FIXED_RATE * Decimal(profile.meter_diameter_mm**2) / 100
    return _CANAL_SEWER_FIXED_RATE * Decimal(profile.supplied_uses)


def _money(value: Decimal) -> Decimal:
    """Round one invoice line exactly to cents."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)
