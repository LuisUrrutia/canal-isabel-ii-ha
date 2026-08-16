"""Tests through the versioned tariff-calculation interface."""

from datetime import date
from decimal import Decimal

import pytest

from custom_components.canal_de_isabel_ii.tariffs import (
    SewerProvider,
    SupplyType,
    TariffProfile,
    billing_period_for,
    calculate_accrued_bill,
    calculate_bill,
)


def _majadahonda_profile(**overrides: object) -> TariffProfile:
    """Return the anonymized contract facts visible on the reference bill."""
    values = {
        "supply_type": SupplyType.SINGLE_DWELLING,
        "sewer_provider": SewerProvider.MUNICIPALITY,
        "meter_diameter_mm": 15,
        "supplied_uses": 1,
        "billing_period_start": date(2026, 7, 8),
        "billing_cycle_days": 60,
        "municipal_sewer_rate_eur_m3": Decimal("0.2200"),
    }
    values.update(overrides)
    return TariffProfile(**values)


def test_reference_invoice_matches_every_published_total() -> None:
    """The anonymized May-July invoice is reproduced exactly to the cent."""
    bill = calculate_bill(
        Decimal(144),
        date(2026, 5, 11),
        date(2026, 7, 8),
        _majadahonda_profile(),
        finalized=True,
    )

    assert bill.adduction_variable_eur == Decimal("272.54")
    assert bill.adduction_fixed_eur == Decimal("8.18")
    assert bill.distribution_variable_eur == Decimal("80.30")
    assert bill.distribution_fixed_eur == Decimal("3.70")
    assert bill.sewer_variable_eur == Decimal("31.68")
    assert bill.sewer_fixed_eur == 0
    assert bill.purification_variable_eur == Decimal("93.23")
    assert bill.purification_fixed_eur == Decimal("3.22")
    assert bill.variable_eur == Decimal("477.75")
    assert bill.fixed_eur == Decimal("15.10")
    assert bill.taxable_eur == Decimal("461.17")
    assert bill.vat_eur == Decimal("46.12")
    assert bill.non_taxable_eur == Decimal("31.68")
    assert bill.total_eur == Decimal("538.97")
    assert bill.finalized


def test_live_estimate_keeps_fractional_telemetering_volume() -> None:
    """Partial periods price liters without pretending to be a final invoice."""
    bill = calculate_bill(
        Decimal("12.345"),
        date(2026, 7, 8),
        date(2026, 8, 15),
        _majadahonda_profile(),
        finalized=False,
    )

    assert bill.volume_m3 == Decimal("12.345")
    assert bill.total_eur > 0
    assert not bill.finalized


def test_accrued_cost_is_monotonic_and_converges_to_period_total() -> None:
    """Energy cost never falls as thresholds expand through a live period."""
    profile = _majadahonda_profile()
    period_start = date(2026, 7, 8)
    period_end = date(2026, 9, 6)
    bills = (
        calculate_accrued_bill(
            Decimal(60), period_start, period_end, date(2026, 7, 9), profile
        ),
        calculate_accrued_bill(
            Decimal(60), period_start, period_end, date(2026, 8, 7), profile
        ),
        calculate_accrued_bill(
            Decimal(61), period_start, period_end, period_end, profile
        ),
    )
    accrued = tuple(bill.total_eur for bill in bills)

    assert accrued == tuple(sorted(accrued))
    assert all(
        bill.total_eur == bill.taxable_eur + bill.vat_eur + bill.non_taxable_eur
        for bill in bills
    )
    assert bills[-1] == calculate_bill(
        Decimal(61),
        period_start,
        period_end,
        profile,
        finalized=False,
    )


def test_accrued_cost_requires_a_date_inside_the_period() -> None:
    """Invalid accrual boundaries cannot silently create bad statistics."""
    with pytest.raises(ValueError, match="inside the billing period"):
        calculate_accrued_bill(
            Decimal(1),
            date(2026, 7, 8),
            date(2026, 9, 6),
            date(2026, 7, 8),
            _majadahonda_profile(),
        )


def test_canal_sewer_and_domestic_equivalent_use_catalog_rates() -> None:
    """Canal-operated sewer service stays taxable and includes its fixed fee."""
    bill = calculate_bill(
        Decimal(20),
        date(2026, 1, 1),
        date(2026, 3, 2),
        _majadahonda_profile(
            supply_type=SupplyType.DOMESTIC_EQUIVALENT,
            sewer_provider=SewerProvider.CANAL,
            meter_diameter_mm=20,
            municipal_sewer_rate_eur_m3=Decimal(0),
        ),
        finalized=True,
    )

    assert bill.sewer_variable_eur == Decimal("2.32")
    assert bill.sewer_fixed_eur == Decimal("4.54")
    assert bill.non_taxable_eur == 0
    assert bill.taxable_eur + bill.vat_eur == bill.total_eur


def test_multi_dwelling_profile_uses_published_fixed_basis() -> None:
    """A shared domestic meter uses N times the standard 15 mm basis."""
    single = calculate_bill(
        Decimal(0),
        date(2026, 1, 1),
        date(2026, 3, 2),
        _majadahonda_profile(),
        finalized=True,
    )
    shared = calculate_bill(
        Decimal(0),
        date(2026, 1, 1),
        date(2026, 3, 2),
        _majadahonda_profile(
            supply_type=SupplyType.MULTI_DWELLING,
            meter_diameter_mm=40,
            supplied_uses=3,
        ),
        finalized=True,
    )

    assert shared.adduction_fixed_eur == single.adduction_fixed_eur * 3
    assert shared.distribution_fixed_eur == Decimal("11.48")


def test_billing_period_rolls_both_sides_of_anchor() -> None:
    """A configured anchor yields stable nominal cycles before and after it."""
    profile = _majadahonda_profile()

    assert billing_period_for(profile, date(2026, 8, 15)) == (
        date(2026, 7, 8),
        date(2026, 9, 6),
    )
    assert billing_period_for(profile, date(2026, 7, 7)) == (
        date(2026, 5, 9),
        date(2026, 7, 8),
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"meter_diameter_mm": 9}, "Meter diameter"),
        ({"supplied_uses": 0}, "Supplied uses"),
        ({"billing_cycle_days": 29}, "Billing cycle"),
        (
            {"municipal_sewer_rate_eur_m3": Decimal("-0.1")},
            "cannot be negative",
        ),
        (
            {"municipal_sewer_rate_eur_m3": Decimal(0)},
            "is required",
        ),
    ],
)
def test_invalid_profiles_are_rejected(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Missing contract facts fail before any misleading cost is published."""
    with pytest.raises(ValueError, match=message):
        _majadahonda_profile(**overrides)


@pytest.mark.parametrize(
    ("volume", "start", "end", "message"),
    [
        (Decimal(-1), date(2026, 1, 1), date(2026, 2, 1), "negative"),
        (Decimal(1), date(2026, 1, 1), date(2026, 1, 1), "must be after"),
        (Decimal(1), date(2025, 12, 1), date(2026, 1, 1), "only supports"),
    ],
)
def test_unsupported_bill_inputs_are_rejected(
    volume: Decimal,
    start: date,
    end: date,
    message: str,
) -> None:
    """The calculator never guesses outside its supported tariff catalog."""
    with pytest.raises(ValueError, match=message):
        calculate_bill(
            volume,
            start,
            end,
            _majadahonda_profile(),
            finalized=False,
        )
