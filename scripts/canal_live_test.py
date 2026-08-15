"""Run a small, real Canal portal query from the command line."""

from __future__ import annotations

import asyncio
import os
import sys
from getpass import getpass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, CookieJar

from custom_components.canal_de_isabel_ii.client import (
    CanalAuthenticationError,
    CanalCaptchaError,
    CanalClient,
    CanalCredentials,
    CanalError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from custom_components.canal_de_isabel_ii.models import (
        ConsumptionSnapshot,
        ContractConsumption,
    )

_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")
_USERNAME_ENV = "CANAL_TEST_USERNAME"
_PASSWORD_ENV = "CANAL_TEST_PASSWORD"
_CAPTCHA_KEY_ENV = "CANAL_TEST_CAPTCHA_API_KEY"


def _required_value(environment_name: str, prompt: str, *, secret: bool) -> str:
    """Read a required value from the process or an interactive terminal."""
    value = os.environ.get(environment_name)
    if value is None:
        value = getpass(prompt) if secret else input(prompt)
    if not value.strip():
        msg = f"{prompt.rstrip(': ')} is required"
        raise ValueError(msg)
    return value


def _print_table(headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> None:
    """Print one dependency-free, aligned terminal table."""
    materialized = list(rows)
    widths = [
        max(len(header), *(len(row[index]) for row in materialized))
        for index, header in enumerate(headers)
    ]
    pattern = "  ".join(f"{{:<{width}}}" for width in widths)
    print(pattern.format(*headers))
    print(pattern.format(*("─" * width for width in widths)))
    for row in materialized:
        print(pattern.format(*row))


def _render_contract(contract: ContractConsumption) -> None:
    """Render the requested calendar month and latest hourly day."""
    heading = f"Contrato {contract.contract_id}"
    if contract.address:
        heading += f" · {contract.address}"
    print(f"\n{heading}")
    print("─" * len(heading))
    if contract.meter_reading_m3 is not None:
        reading_at = (
            contract.meter_reading_at.astimezone(_PORTAL_TIME_ZONE).isoformat(
                timespec="minutes"
            )
            if contract.meter_reading_at is not None
            else "fecha desconocida"
        )
        print(f"Contador: {contract.meter_reading_m3:.3f} m³ ({reading_at})")

    if not contract.daily_readings:
        print("\nEl portal no devolvió consumos diarios.")
        return

    latest_day = contract.daily_readings[-1].day
    month_start = latest_day.replace(day=1)
    month = tuple(
        reading
        for reading in contract.daily_readings
        if month_start <= reading.day <= latest_day
    )
    print(f"\nConsumo diario · {month_start:%Y-%m}")
    _print_table(
        ("Fecha", "Litros"),
        (
            (reading.day.isoformat(), f"{reading.volume_liters:.2f}")
            for reading in month
        ),
    )
    print(
        f"Total del mes disponible: {sum(item.volume_liters for item in month):.2f} L"
    )

    hourly = tuple(
        reading
        for reading in contract.hourly_readings
        if reading.start.astimezone(_PORTAL_TIME_ZONE).date() == latest_day
    )
    print(f"\nConsumo horario · {latest_day.isoformat()}")
    if not hourly:
        print("El portal no devolvió lecturas horarias para ese día.")
        return
    _print_table(
        ("Hora", "Litros"),
        (
            (
                reading.start.astimezone(_PORTAL_TIME_ZONE).strftime("%H:%M"),
                f"{reading.volume_liters:.2f}",
            )
            for reading in hourly
        ),
    )
    print(f"Total horario: {sum(item.volume_liters for item in hourly):.2f} L")


def render_snapshot(snapshot: ConsumptionSnapshot) -> None:
    """Render every contract without exposing account credentials."""
    print(
        "\nConsulta completada: "
        f"{len(snapshot.contracts)} contrato(s), "
        f"{snapshot.fetched_at.isoformat(timespec='seconds')}"
    )
    for contract_id in sorted(snapshot.contracts):
        _render_contract(snapshot.contracts[contract_id])


async def _async_run() -> None:
    """Authenticate and retrieve the smallest useful live data window."""
    credentials = CanalCredentials(
        username=_required_value(_USERNAME_ENV, "NIF/NIE: ", secret=False),
        password=_required_value(_PASSWORD_ENV, "Contraseña: ", secret=True),
        captcha_api_key=_required_value(
            _CAPTCHA_KEY_ENV,
            "API key de 2Captcha: ",
            secret=True,
        ),
    )
    print(
        "\nResolviendo el CAPTCHA y consultando el portal. "
        "Se hará una consulta mensual y una horaria por contrato…",
        flush=True,
    )
    async with ClientSession(cookie_jar=CookieJar()) as session:
        snapshot = await CanalClient(
            session,
            credentials,
            history_days=31,
            hourly_history_days=1,
        ).async_fetch_consumption()
    render_snapshot(snapshot)


def main() -> int:
    """Return a stable process status without printing secrets or tracebacks."""
    try:
        asyncio.run(_async_run())
    except CanalAuthenticationError as err:
        print(f"\nError de autenticación: {err}", file=sys.stderr)
        return 2
    except CanalCaptchaError as err:
        print(f"\nError de 2Captcha: {err}", file=sys.stderr)
        return 3
    except CanalError as err:
        print(f"\nError del portal: {err}", file=sys.stderr)
        return 4
    except EOFError, KeyboardInterrupt:
        print("\nPrueba cancelada.", file=sys.stderr)
        return 130
    except ValueError as err:
        print(f"\nEntrada no válida: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
