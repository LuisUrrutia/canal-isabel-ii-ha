"""Tests for the dependency-free live CLI renderer."""

import pytest

from scripts.canal_live_test import render_snapshot

from .factories import make_snapshot


def test_live_renderer_shows_month_and_latest_hourly_day(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live result is readable and never needs account credentials."""
    render_snapshot(make_snapshot())

    output = capsys.readouterr().out
    assert "Contrato contract-123" in output
    assert "Contador: 125.500 m³" in output
    assert "Consumo diario · 2026-08" in output
    assert "2026-08-14" in output
    assert "Consumo horario · 2026-08-14" in output
    assert "11:00" in output
    assert "Total horario: 15.50 L" in output
