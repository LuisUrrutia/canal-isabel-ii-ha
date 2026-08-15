"""Tests for the deep Canal portal client interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from custom_components.canal_de_isabel_ii.client import (
    CanalAuthenticationError,
    CanalCaptchaError,
    CanalClient,
    CanalConnectionError,
    CanalCredentials,
    CanalInvalidResponseError,
)
from custom_components.canal_de_isabel_ii.models import (
    ConsumptionReading,
    ConsumptionSnapshot,
    ContractConsumption,
    DailyConsumption,
)

CONTRACTS = {
    "contract-a": ("meter-a", "Address A", 1234.5),
    "contract-b": ("meter-b", "Address B", 2345.6),
}


class FakeCaptchaSolver:
    """Deterministic adapter for the external 2Captcha seam."""

    def __init__(self) -> None:
        """Initialize the retained call list."""
        self.calls: list[dict[str, object]] = []

    async def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Return a stable token and retain the observable request."""
        self.calls.append(
            {
                "sitekey": sitekey,
                "url": url,
                "enterprise": enterprise,
                "invisible": invisible,
                "userAgent": userAgent,
            }
        )
        return {"captchaId": "captcha-id", "code": "captcha-token"}


class FailingCaptchaSolver:
    """Adapter that represents an unavailable or unfunded solver account."""

    async def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Fail every solve request."""
        del sitekey, url, enterprise, invisible, userAgent
        msg = "ERROR_ZERO_BALANCE"
        raise RuntimeError(msg)


class EmptyCaptchaSolver:
    """Adapter that returns a structurally valid but empty solver response."""

    async def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Return an empty token after accepting the public solver inputs."""
        del sitekey, url, enterprise, invisible, userAgent
        return {"code": ""}


@dataclass
class PortalState:
    """Mutable state owned by the local portal adapter."""

    max_day: date = date(2026, 8, 14)
    contracts: dict[str, tuple[str, str, float]] = field(
        default_factory=lambda: dict(CONTRACTS)
    )
    authenticated: bool = False
    accept_credentials: bool = True
    malformed_consumption: bool = False
    active_contract: str = "contract-a"
    login_payload: dict[str, str] = field(default_factory=dict)
    queries: list[dict[str, str]] = field(default_factory=list)
    switches: list[str] = field(default_factory=list)
    login_override: str | None = None
    consumption_override: str | None = None
    daily_chart_override: str | None = None
    hourly_chart_override: str | None = None
    redirect_queries: bool = False


def _login_page() -> str:
    return """
        <html>
          <form id="login" action="/inicio?p_auth=login-token" method="post">
            <input type="hidden" name="_login_messageDispositivo" value="">
            <input type="hidden" name="_login_errorDispositivo" value="">
            <input type="hidden" name="_login_tipoUsuario" value="">
            <input name="_login_tipoDocumento" value="NIF">
            <input name="_login_numeroDocumento" value="">
            <input name="_login_password" type="password">
            <input name="_login_idThemeLogin" value="ovir">
          </form>
          <script>
            grecaptcha.enterprise.render('recaptcha-login', {
              'sitekey': 'portal-site-key', 'size': 'invisible'
            });
          </script>
        </html>
    """


def _consumption_page(state: PortalState) -> str:
    meter_id, address, meter_reading = state.contracts[state.active_contract]
    links = "".join(
        (
            '<a class="dropdown-item" href="/group/ovir/consumo?'
            "p_p_id=contracts&p_p_lifecycle=1&"
            "_contracts_javax.portlet.action=%2FlistadoContratos%2Ffavorito&"
            f"_contracts_favorito=true&_contracts_contratoId={contract_id}&"
            f'p_auth=switch-token">{contract_id} - {values[1]}</a>'
        )
        for contract_id, values in state.contracts.items()
    )
    namespace = "_telelectura_"
    return f"""
        <html>
          <div>Contrato n.º {state.active_contract}</div>
          {links}
          <form action="/group/ovir/consumo?p_p_id=telelectura&amp;
              p_p_lifecycle=1&amp;{namespace}javax.portlet.action=
              %2Ftelelectura%2FbuscarForm&amp;p_auth=query-token" method="post">
            <input id="fechaDesde1" name="{namespace}fechaDesde"
                   value="{state.max_day - timedelta(days=59)}">
            <input id="fechaHasta1" name="{namespace}fechaHasta"
                   value="{state.max_day}">
            <select id="periodicidad" name="{namespace}periodicidad">
              <option value="Diaria" selected>Diaria</option>
              <option value="Horaria">Horaria</option>
            </select>
            <select id="contratosFiltro" name="{namespace}contratosFiltro">
              <option value="{state.active_contract}" selected>
                {state.active_contract} - {address}
              </option>
            </select>
            <input name="{namespace}fechaDesde2" value="">
            <input name="{namespace}fechaHasta2" value="">
            <input name="{namespace}back" value="">
            <input name="{namespace}prorrateado" value="">
            <input name="{namespace}valor-prorrateado" value="">
          </form>
          <section>
            <h5>DIRECCIÓN SUMINISTRO</h5><h5>{address}</h5>
            <h5>CONTADOR</h5><h5>{meter_id}</h5>
            <h5>ÚLTIMA LECTURA</h5><h5>{str(meter_reading).replace(".", ",")}m3</h5>
            <h5>FECHA Y HORA LECTURA</h5><h5>15/08/2026 05:00</h5>
          </section>
        </html>
    """


def _chart_page(
    state: PortalState,
    frequency: str,
    start: date,
    end: date,
) -> str:
    if state.malformed_consumption:
        return "<script>dataJsonConsumo = definitely-not-data;</script>"

    rows: list[str] = []
    if frequency == "Diaria":
        current = start
        while current <= end:
            volume = float(current.day * 10)
            rows.append(
                "{c:[{v: 'X "
                f"{current.strftime('%d/%m/%Y')} "
                f"'}}, {{v: {volume}}}, {{v: 'tooltip'}}, {{v: ''}}]}}"
            )
            current += timedelta(days=1)
    else:
        for hour in range(24):
            label = (
                f"X {start.strftime('%d/%m/%Y')} {hour:02d}h "
                if hour == 0
                else f"{hour:02d}h "
            )
            rows.append(
                f"{{c:[{{v: '{label}'}}, {{v: {hour + 0.5}}}, {{v: 'tooltip'}}]}}"
            )

    return (
        "<script>dataJsonConsumo = {cols: [],rows: [" + ",".join(rows) + "]};</script>"
    )


async def make_client(  # noqa: PLR0913 - explicit test-adapter controls
    aiohttp_client: AiohttpClient,
    state: PortalState,
    *,
    captcha_solver: FakeCaptchaSolver
    | FailingCaptchaSolver
    | EmptyCaptchaSolver
    | None = None,
    history_days: int = 2,
    correction_days: int = 2,
    hourly_history_days: int | None = None,
) -> tuple[
    CanalClient,
    FakeCaptchaSolver | FailingCaptchaSolver | EmptyCaptchaSolver,
]:
    """Create a client backed by a stateful local portal adapter."""

    async def landing(_: web.Request) -> web.Response:
        return web.Response(text=state.login_override or _login_page())

    async def login(request: web.Request) -> web.Response:
        payload = await request.post()
        state.login_payload = {key: str(value) for key, value in payload.items()}
        state.authenticated = (
            state.accept_credentials
            and state.login_payload.get("_login_numeroDocumento") == "X1234567L"
            and state.login_payload.get("_login_password") == "secret"
            and state.login_payload.get("g-recaptcha-response") == "captcha-token"
        )
        if not state.authenticated:
            return web.Response(text=_login_page())
        location = "/group/ovir/consumo"
        raise web.HTTPFound(location)

    async def consumption(request: web.Request) -> web.Response:
        if not state.authenticated:
            location = "/web/ovir"
            raise web.HTTPFound(location)

        action = next(
            (
                value
                for key, value in request.query.items()
                if key.endswith("javax.portlet.action")
            ),
            "",
        )
        if request.method == "POST" and action.endswith("contratoPorDefecto"):
            payload = await request.post()
            contract_id = next(
                str(value)
                for key, value in payload.items()
                if key.endswith("contractOption")
            )
            state.active_contract = contract_id
            state.switches.append(contract_id)
            location = "/group/ovir/consumo"
            raise web.HTTPFound(location)

        if request.method == "POST" and action.endswith("buscarForm"):
            if state.redirect_queries:
                location = "/web/ovir"
                raise web.HTTPFound(location)
            payload = await request.post()
            normalized = {
                key.rsplit("_", maxsplit=1)[-1]: str(value)
                for key, value in payload.items()
            }
            normalized["contract"] = state.active_contract
            state.queries.append(normalized)
            frequency = normalized["periodicidad"]
            override = (
                state.daily_chart_override
                if frequency == "Diaria"
                else state.hourly_chart_override
            )
            return web.Response(
                text=override
                or _chart_page(
                    state,
                    frequency,
                    date.fromisoformat(normalized["fechaDesde"]),
                    date.fromisoformat(normalized["fechaHasta"]),
                )
            )

        return web.Response(text=state.consumption_override or _consumption_page(state))

    app = web.Application()
    app.router.add_get("/web/ovir", landing)
    app.router.add_post("/inicio", login)
    app.router.add_route("*", "/group/ovir/consumo", consumption)
    http = await aiohttp_client(app)
    solver = captcha_solver or FakeCaptchaSolver()
    client = CanalClient(
        http.session,
        CanalCredentials(
            username="x1234567l",
            password="secret",
            captcha_api_key="api-key",
        ),
        captcha_solver=solver,
        base_url=str(http.make_url("/")),
        history_days=history_days,
        correction_days=correction_days,
        hourly_history_days=hourly_history_days,
    )
    return client, solver


@pytest.mark.asyncio
async def test_fetch_consumption_owns_login_contracts_and_history(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """One call hides CAPTCHA, contract switching, ranges and parsing."""
    state = PortalState()
    client, solver = await make_client(aiohttp_client, state)

    snapshot = await client.async_fetch_consumption()

    assert isinstance(solver, FakeCaptchaSolver)
    assert solver.calls == [
        {
            "sitekey": "portal-site-key",
            "url": str(client.login_url),
            "enterprise": 1,
            "invisible": 1,
            "userAgent": "Canal-Isabel-II-Home-Assistant/3.1",
        }
    ]
    assert state.login_payload["_login_tipoUsuario"] == "PARTICULAR"
    assert state.login_payload["_login_tipoUsuarioDesktop"] == "PARTICULAR"
    assert state.login_payload["_login_tipoDocumento"] == "NIE"
    assert state.login_payload["_login_numeroDocumento"] == "X1234567L"
    assert state.login_payload["_login_password"] == "secret"
    assert state.switches == ["contract-b", "contract-a"]
    assert set(snapshot.contracts) == set(CONTRACTS)

    contract = snapshot.contracts["contract-a"]
    assert contract.address == "Address A"
    assert contract.meter_id == "meter-a"
    assert contract.meter_reading_m3 == 1234.5
    assert contract.meter_reading_at == datetime(2026, 8, 15, 3, tzinfo=UTC)
    assert [reading.day for reading in contract.daily_readings] == [
        date(2026, 8, 13),
        date(2026, 8, 14),
    ]
    assert len(contract.hourly_readings) == 48
    assert contract.hourly_readings[0].start == datetime(2026, 8, 12, 22, tzinfo=UTC)
    assert contract.hourly_readings[-1].volume_liters == 23.5

    assert len(state.queries) == 6
    assert [query["periodicidad"] for query in state.queries].count("Diaria") == 2
    assert [query["periodicidad"] for query in state.queries].count("Horaria") == 4


@pytest.mark.asyncio
async def test_debug_logs_report_progress_without_secrets(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Detailed scrape logs remain useful and safe to share after review."""
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.canal_de_isabel_ii.client",
    )
    state = PortalState(contracts={"contract-a": CONTRACTS["contract-a"]})
    client, _ = await make_client(aiohttp_client, state, history_days=1)

    await client.async_fetch_consumption()

    messages = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.canal_de_isabel_ii.client"
    )
    assert "Starting initial portal scrape" in messages
    assert "Requesting an invisible enterprise reCAPTCHA solution" in messages
    assert "Synchronizing Canal contract 1 of 1" in messages
    assert "Querying hourly consumption for day 1 of 1" in messages
    assert "Completed initial portal scrape" in messages
    for secret in (
        "X1234567L",
        "secret",
        "api-key",
        "captcha-token",
        "contract-a",
    ):
        assert secret not in messages


@pytest.mark.asyncio
async def test_fetch_consumption_splits_daily_queries_at_month_boundaries(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """The portal's broken cross-month daily filter stays behind the interface."""
    state = PortalState(
        max_day=date(2026, 3, 1),
        contracts={"contract-a": CONTRACTS["contract-a"]},
    )
    client, _ = await make_client(aiohttp_client, state, history_days=3)

    await client.async_fetch_consumption()

    daily_queries = [
        query for query in state.queries if query["periodicidad"] == "Diaria"
    ]
    assert [(query["fechaDesde"], query["fechaHasta"]) for query in daily_queries] == [
        ("2026-02-27", "2026-02-28"),
        ("2026-03-01", "2026-03-01"),
    ]


@pytest.mark.asyncio
async def test_fetch_consumption_refreshes_only_recent_history(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """Existing history is retained while recent portal corrections are replaced."""
    state = PortalState(contracts={"contract-a": CONTRACTS["contract-a"]})
    client, _ = await make_client(
        aiohttp_client,
        state,
        history_days=30,
        correction_days=2,
    )
    previous_contract = ContractConsumption(
        contract_id="contract-a",
        meter_id="old-meter",
        address="Old address",
        meter_reading_m3=1200.0,
        meter_reading_at=datetime(2026, 8, 13, 3, tzinfo=UTC),
        daily_readings=(DailyConsumption(date(2026, 8, 12), 1.0),),
        hourly_readings=(
            ConsumptionReading(datetime(2026, 8, 12, tzinfo=UTC), 99.0),
            ConsumptionReading(datetime(2026, 8, 13, tzinfo=UTC), 99.0),
        ),
    )
    previous = ConsumptionSnapshot(
        contracts={"contract-a": previous_contract},
        fetched_at=datetime(2026, 8, 13, 6, tzinfo=UTC),
    )

    snapshot = await client.async_fetch_consumption(previous)

    hourly_days = {
        query["fechaDesde"]
        for query in state.queries
        if query["periodicidad"] == "Horaria"
    }
    assert hourly_days == {"2026-08-12", "2026-08-13", "2026-08-14"}
    readings = snapshot.contracts["contract-a"].hourly_readings
    assert readings[0].volume_liters == 0.5
    assert readings[-1].start == datetime(2026, 8, 14, 21, tzinfo=UTC)


@pytest.mark.asyncio
async def test_standalone_validation_stops_before_consumption_backfill(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """Standalone validation can check login without the expensive sync."""
    state = PortalState()
    client, _ = await make_client(aiohttp_client, state)

    await client.async_validate_credentials()

    assert state.authenticated
    assert state.queries == []


@pytest.mark.asyncio
async def test_reduced_read_scope_fetches_only_latest_hourly_day(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """A live CLI probe can verify a month without 31 hourly requests."""
    state = PortalState(contracts={"contract-a": CONTRACTS["contract-a"]})
    client, _ = await make_client(
        aiohttp_client,
        state,
        history_days=31,
        hourly_history_days=1,
    )

    snapshot = await client.async_fetch_consumption()

    hourly_queries = [
        query for query in state.queries if query["periodicidad"] == "Horaria"
    ]
    assert [query["fechaDesde"] for query in hourly_queries] == ["2026-08-14"]
    assert len(snapshot.contracts["contract-a"].daily_readings) == 31
    assert len(snapshot.contracts["contract-a"].hourly_readings) == 24


@pytest.mark.asyncio
async def test_invalid_credentials_are_distinct_from_captcha_failure(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """Users receive actionable errors for the two credential destinations."""
    rejected = PortalState(accept_credentials=False)
    rejected_client, _ = await make_client(aiohttp_client, rejected)
    with pytest.raises(CanalAuthenticationError):
        await rejected_client.async_validate_credentials()

    captcha_client, _ = await make_client(
        aiohttp_client,
        PortalState(),
        captcha_solver=FailingCaptchaSolver(),
    )
    with pytest.raises(CanalCaptchaError, match="2Captcha"):
        await captcha_client.async_validate_credentials()


@pytest.mark.asyncio
async def test_malformed_portal_data_fails_explicitly(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """A changed graph response never silently publishes empty consumption."""
    state = PortalState(
        malformed_consumption=True,
        contracts={"contract-a": CONTRACTS["contract-a"]},
    )
    client, _ = await make_client(aiohttp_client, state)

    with pytest.raises(CanalInvalidResponseError, match="consumption graph"):
        await client.async_fetch_consumption()


@pytest.mark.asyncio
async def test_portal_http_failures_keep_a_stable_exception(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """Remote availability problems do not masquerade as bad credentials."""

    async def unavailable(_: web.Request) -> web.Response:
        return web.Response(status=503)

    app = web.Application()
    app.router.add_get("/group/ovir/consumo", unavailable)
    app.router.add_get("/web/ovir", unavailable)
    http = await aiohttp_client(app)
    client = CanalClient(
        http.session,
        CanalCredentials("X1234567L", "secret", "api-key"),
        captcha_solver=FakeCaptchaSolver(),
        base_url=str(http.make_url("/")),
    )

    with pytest.raises(CanalConnectionError, match="HTTP 503"):
        await client.async_fetch_consumption()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "login_page",
    [
        "<html>No credential form</html>",
        """
        <form action="/inicio">
          <input name="_login_password" type="password">
        </form>
        """,
        """
        <form action="/inicio">
          <input type="password">
        </form>
        <script>const captcha = {'sitekey': 'portal-site-key'};</script>
        """,
    ],
)
async def test_changed_login_page_fails_explicitly(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
    login_page: str,
) -> None:
    """Missing login primitives are reported as incompatible portal HTML."""
    client, _ = await make_client(
        aiohttp_client,
        PortalState(login_override=login_page),
    )

    with pytest.raises(CanalInvalidResponseError):
        await client.async_validate_credentials()


@pytest.mark.asyncio
async def test_empty_captcha_token_is_rejected(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """A solver success without a token cannot reach the credential endpoint."""
    client, _ = await make_client(
        aiohttp_client,
        PortalState(),
        captcha_solver=EmptyCaptchaSolver(),
    )

    with pytest.raises(CanalCaptchaError, match="empty"):
        await client.async_validate_credentials()


@pytest.mark.asyncio
async def test_changed_contract_and_date_controls_fail_explicitly(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """Contract discovery and maximum-date controls are mandatory seams."""
    no_contracts = PortalState(
        authenticated=True,
        consumption_override="<html>No contract selector</html>",
    )
    client, _ = await make_client(aiohttp_client, no_contracts)
    with pytest.raises(CanalInvalidResponseError, match="contract selector"):
        await client.async_fetch_consumption()

    invalid_date = PortalState(authenticated=True)
    invalid_date.consumption_override = _consumption_page(invalid_date).replace(
        f'value="{invalid_date.max_day}"',
        'value="not-a-date"',
    )
    client, _ = await make_client(aiohttp_client, invalid_date)
    with pytest.raises(CanalInvalidResponseError, match="maximum date"):
        await client.async_fetch_consumption()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daily_chart", "hourly_chart", "message"),
    [
        (
            "dataJsonConsumo = {rows: [{c:[{v: 'not-a-day'}, {v: 1}]}]};",
            None,
            "daily consumption label",
        ),
        (
            "dataJsonConsumo = {rows: [{c:[{v: 'X 14/08/2026'}, {v: 1}]}]};",
            "dataJsonConsumo = {rows: [{c:[{v: 'no-hour'}, {v: 1}]}]};",
            "hourly consumption label",
        ),
        ("<html>No graph variable</html>", None, "consumption graph"),
    ],
)
async def test_changed_graph_labels_fail_explicitly(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
    daily_chart: str,
    hourly_chart: str | None,
    message: str,
) -> None:
    """Portal graph changes cannot silently produce incorrect history."""
    state = PortalState(
        authenticated=True,
        contracts={"contract-a": CONTRACTS["contract-a"]},
        daily_chart_override=daily_chart,
        hourly_chart_override=hourly_chart,
    )
    client, _ = await make_client(aiohttp_client, state)

    with pytest.raises(CanalInvalidResponseError, match=message):
        await client.async_fetch_consumption()


@pytest.mark.asyncio
async def test_session_expiry_during_query_requests_reauthentication(
    aiohttp_client: AiohttpClient,
    socket_enabled: None,
) -> None:
    """A mid-sync redirect remains an authentication failure."""
    state = PortalState(
        authenticated=True,
        contracts={"contract-a": CONTRACTS["contract-a"]},
        redirect_queries=True,
    )
    client, _ = await make_client(aiohttp_client, state)

    with pytest.raises(CanalAuthenticationError, match="expired"):
        await client.async_fetch_consumption()
