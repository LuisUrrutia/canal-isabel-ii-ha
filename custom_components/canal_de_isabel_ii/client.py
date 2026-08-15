"""Authenticated asynchronous client for the Canal customer portal."""

from __future__ import annotations

import asyncio
import logging
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from http import HTTPStatus
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientSession, ClientTimeout, FormData
from twocaptcha import TwoCaptcha

from .const import BASE_URL, CONSUMPTION_URL
from .models import (
    ConsumptionReading,
    ConsumptionSnapshot,
    ContractConsumption,
    DailyConsumption,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_PORTAL_TIME_ZONE = ZoneInfo("Europe/Madrid")
_REQUEST_TIMEOUT = ClientTimeout(total=45)
_USER_AGENT = "Canal-Isabel-II-Home-Assistant/3.1.1"
_LOGIN_PATH = "/web/ovir"
_DEFAULT_HISTORY_DAYS = 183
_DEFAULT_CORRECTION_DAYS = 2
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_LOGGER = logging.getLogger(__name__)

_SITE_KEY_RE = re.compile(
    r"['\"]sitekey['\"]\s*:\s*['\"](?P<sitekey>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_ACTIVE_CONTRACT_RE = re.compile(
    r"Contrato\s+n\.?[ºo]\s*(?P<contract>[A-Za-z0-9-]+)",
    re.IGNORECASE,
)
_GRAPH_ROW_RE = re.compile(
    r"\{c:\s*\[\s*\{v:\s*'(?P<label>(?:\\.|[^'])*)'\}\s*,\s*"
    r"\{v:\s*(?P<value>-?\d+(?:\.\d+)?)\}",
    re.DOTALL,
)
_DATE_RE = re.compile(r"(?P<date>\d{2}/\d{2}/\d{4})")
_HOUR_RE = re.compile(r"(?P<hour>\d{2})h")
_METADATA_RE = re.compile(
    r"DIRECCI[ÓO]N\s+SUMINISTRO\s+(?P<address>.*?)\s+"
    r"CONTADOR\s+(?P<meter>.*?)\s+"
    r"[ÚU]LTIMA\s+LECTURA\s+(?P<reading>.*?)\s+"
    r"FECHA\s+Y\s+HORA\s+LECTURA\s+"
    r"(?P<read_at>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})",
    re.IGNORECASE | re.DOTALL,
)


class CanalError(Exception):
    """Base exception for stable portal failures."""


class CanalAuthenticationError(CanalError):
    """The portal rejected the configured account credentials."""


class CanalCaptchaError(CanalError):
    """2Captcha could not produce a usable portal token."""


class CanalConnectionError(CanalError):
    """The portal could not be reached successfully."""


class CanalInvalidResponseError(CanalError):
    """The portal returned data that cannot be understood safely."""


class _CanalSessionExpiredError(CanalError):
    """The authenticated portal session expired during a synchronization."""


@dataclass(frozen=True, slots=True)
class CanalCredentials:
    """Account credentials required for unattended portal login."""

    username: str
    password: str
    captcha_api_key: str

    def __post_init__(self) -> None:
        """Reject unusable credentials before making remote requests."""
        if not all(value.strip() for value in vars_from_slots(self)):
            msg = "Username, password and 2Captcha API key are required"
            raise ValueError(msg)

    @property
    def normalized_username(self) -> str:
        """Return the document identifier in the portal's expected format."""
        return self.username.strip().upper()

    @property
    def document_type(self) -> str:
        """Infer the only two document types exposed by the login form."""
        return "NIE" if self.normalized_username.startswith(("X", "Y", "Z")) else "NIF"


def vars_from_slots(credentials: CanalCredentials) -> tuple[str, str, str]:
    """Return credential values without creating a diagnostics-friendly mapping."""
    return credentials.username, credentials.password, credentials.captcha_api_key


class CaptchaSolver(Protocol):
    """Internal port implemented by 2Captcha and test adapters."""

    async def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Resolve one reCAPTCHA challenge."""


class _BlockingCaptchaSolver(Protocol):
    """Synchronous 2Captcha-compatible solver executed in a worker thread."""

    def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Resolve one reCAPTCHA challenge synchronously."""


class _ThreadedTwoCaptcha:
    """Keep the blocking 2Captcha SDK outside Home Assistant's event loop."""

    def __init__(
        self,
        api_key: str,
        *,
        solver: _BlockingCaptchaSolver | None = None,
    ) -> None:
        """Initialize the synchronous SDK behind an async adapter."""
        self._solver = solver or TwoCaptcha(api_key)

    async def recaptcha(
        self,
        *,
        sitekey: str,
        url: str,
        enterprise: int,
        invisible: int,
        userAgent: str,  # noqa: N803 - 2Captcha's public argument name
    ) -> dict[str, object]:
        """Solve the challenge in a worker thread and return its result."""
        return await asyncio.to_thread(
            self._solver.recaptcha,
            sitekey=sitekey,
            url=url,
            enterprise=enterprise,
            invisible=invisible,
            userAgent=userAgent,
        )


@dataclass(slots=True)
class _Option:
    value: str
    selected: bool
    label: str = ""


@dataclass(slots=True)
class _Select:
    element_id: str
    name: str
    options: list[_Option] = field(default_factory=list)

    @property
    def selected_value(self) -> str:
        selected = next((option for option in self.options if option.selected), None)
        if selected is not None:
            return selected.value
        return self.options[0].value if self.options else ""


@dataclass(slots=True)
class _Form:
    action: str
    values: dict[str, str] = field(default_factory=dict)
    selects: list[_Select] = field(default_factory=list)
    has_password: bool = False

    def field_name(self, suffix: str) -> str | None:
        """Find an input or select name by stable Liferay suffix."""
        return next(
            (
                name
                for name in (*self.values, *(select.name for select in self.selects))
                if name.endswith(suffix)
            ),
            None,
        )


@dataclass(slots=True)
class _Link:
    href: str
    label: str = ""


class _PortalPageParser(HTMLParser):
    """Extract the small stable interface exposed by the portal HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.links: list[_Link] = []
        self.text_parts: list[str] = []
        self._form: _Form | None = None
        self._select: _Select | None = None
        self._option: _Option | None = None
        self._link: _Link | None = None

    @property
    def text(self) -> str:
        """Return visible text normalized for label-driven parsing."""
        return " ".join(" ".join(self.text_parts).split())

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """Capture relevant form and link attributes."""
        attributes = {key: value or "" for key, value in attrs}
        if tag == "form":
            action = "".join(attributes.get("action", "").split())
            self._form = _Form(action=action)
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            name = attributes.get("name")
            input_type = attributes.get("type", "text").casefold()
            if input_type == "password":
                self._form.has_password = True
            if (
                name
                and input_type not in {"submit", "button"}
                and (input_type not in {"radio", "checkbox"} or "checked" in attributes)
            ):
                self._form.values[name] = attributes.get("value", "")
        elif tag == "select" and self._form is not None:
            self._select = _Select(
                element_id=attributes.get("id", ""),
                name=attributes.get("name", ""),
            )
            self._form.selects.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = _Option(
                value=attributes.get("value", ""),
                selected="selected" in attributes,
            )
            self._select.options.append(self._option)
        elif tag == "a":
            self._link = _Link(href=attributes.get("href", ""))
            self.links.append(self._link)

    def handle_endtag(self, tag: str) -> None:
        """Close the current parser context."""
        if tag == "option":
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "form":
            self._form = None
        elif tag == "a":
            self._link = None

    def handle_data(self, data: str) -> None:
        """Capture labels and normalized page text."""
        stripped = data.strip()
        if not stripped:
            return
        self.text_parts.append(stripped)
        if self._option is not None:
            self._option.label += stripped
        if self._link is not None:
            self._link.label += stripped


@dataclass(frozen=True, slots=True)
class _ContractReference:
    contract_id: str
    switch_href: str


@dataclass(frozen=True, slots=True)
class _ContractMetadata:
    meter_id: str | None
    address: str | None
    meter_reading_m3: float | None
    meter_reading_at: datetime | None


@dataclass(frozen=True, slots=True)
class _HttpResult:
    status: int
    text: str
    location: str | None


class CanalClient:
    """Hide the complete portal workflow behind two asynchronous methods."""

    def __init__(  # noqa: PLR0913 - optional arguments are testable adapters
        self,
        session: ClientSession,
        credentials: CanalCredentials,
        *,
        captcha_solver: CaptchaSolver | None = None,
        base_url: str = BASE_URL,
        history_days: int = _DEFAULT_HISTORY_DAYS,
        correction_days: int = _DEFAULT_CORRECTION_DAYS,
        hourly_history_days: int | None = None,
    ) -> None:
        """Initialize the portal client with injected remote adapters."""
        if (
            history_days < 1
            or correction_days < 1
            or (hourly_history_days is not None and hourly_history_days < 1)
        ):
            msg = "History and correction windows must be positive"
            raise ValueError(msg)
        self._session = session
        self._credentials = credentials
        self._captcha_solver = captcha_solver or _ThreadedTwoCaptcha(
            credentials.captcha_api_key
        )
        self._base_url = base_url.rstrip("/")
        self._login_url = urljoin(f"{self._base_url}/", _LOGIN_PATH.lstrip("/"))
        self._consumption_url = urljoin(
            f"{self._base_url}/",
            CONSUMPTION_URL.removeprefix(BASE_URL).lstrip("/"),
        )
        self._history_days = history_days
        self._correction_days = correction_days
        self._hourly_history_days = hourly_history_days

    @property
    def login_url(self) -> str:
        """Expose the public challenge URL required by the CAPTCHA adapter."""
        return self._login_url

    async def async_validate_credentials(self) -> None:
        """Validate login without starting the expensive historical sync."""
        started = monotonic()
        _LOGGER.info("Starting standalone Canal credential validation")
        await self._authenticated_consumption_page()
        _LOGGER.info(
            "Completed standalone Canal credential validation in %.1f seconds",
            monotonic() - started,
        )

    async def async_fetch_consumption(
        self,
        previous: ConsumptionSnapshot | None = None,
    ) -> ConsumptionSnapshot:
        """Return a normalized, merged history for every account contract."""
        started = monotonic()
        sync_type = "incremental" if previous is not None else "initial"
        _LOGGER.info(
            "Starting %s portal scrape with a %d-day history window and a "
            "%d-day correction window",
            sync_type,
            self._history_days,
            self._correction_days,
        )
        for attempt in range(2):
            try:
                snapshot = await self._async_fetch_consumption_once(previous)
            except _CanalSessionExpiredError:
                if attempt == 1:
                    _LOGGER.exception(
                        "The Canal portal session repeatedly expired during the "
                        "%s scrape",
                        sync_type,
                    )
                    msg = (
                        "The Canal portal session repeatedly expired during "
                        "synchronization"
                    )
                    raise CanalConnectionError(msg) from None
                _LOGGER.warning(
                    "The Canal portal session expired during the %s scrape; "
                    "restarting once with a fresh login and CAPTCHA",
                    sync_type,
                )
                continue

            _LOGGER.info(
                "Completed %s portal scrape in %.1f seconds",
                sync_type,
                monotonic() - started,
            )
            return snapshot

        msg = "The Canal portal synchronization retry loop ended unexpectedly"
        raise CanalConnectionError(msg)

    async def _async_fetch_consumption_once(
        self,
        previous: ConsumptionSnapshot | None,
    ) -> ConsumptionSnapshot:
        """Fetch one complete snapshot using the current portal session."""
        initial_page = await self._authenticated_consumption_page()
        initial_parser = self._parse_page(initial_page)
        references, original_contract = self._contract_references(initial_parser)
        _LOGGER.info("The Canal portal reported %d contract(s)", len(references))
        contracts: dict[str, ContractConsumption] = {}
        current_contract = original_contract

        try:
            for contract_number, reference in enumerate(references, start=1):
                _LOGGER.info(
                    "Synchronizing Canal contract %d of %d",
                    contract_number,
                    len(references),
                )
                page = initial_page
                if reference.contract_id != current_contract:
                    _LOGGER.debug(
                        "Switching the active portal contract for contract %d",
                        contract_number,
                    )
                    await self._switch_contract(reference)
                    current_contract = reference.contract_id
                    page = await self._authenticated_consumption_page()

                prior = (
                    previous.contracts.get(reference.contract_id)
                    if previous is not None
                    else None
                )
                contracts[reference.contract_id] = await self._collect_contract(
                    page,
                    reference.contract_id,
                    prior,
                )
                contract = contracts[reference.contract_id]
                _LOGGER.info(
                    "Completed Canal contract %d of %d: %d daily and %d hourly "
                    "readings",
                    contract_number,
                    len(references),
                    len(contract.daily_readings),
                    len(contract.hourly_readings),
                )
        finally:
            if current_contract != original_contract:
                _LOGGER.debug("Restoring the original active Canal contract")
                original = next(
                    ref for ref in references if ref.contract_id == original_contract
                )
                await self._switch_contract(original)

        return ConsumptionSnapshot(
            contracts=contracts,
            fetched_at=datetime.now(UTC),
        )

    async def _collect_contract(
        self,
        page: str,
        contract_id: str,
        previous: ContractConsumption | None,
    ) -> ContractConsumption:
        parser = self._parse_page(page)
        form = self._consumption_form(parser)
        max_day = self._maximum_available_day(form)
        history_start = max_day - timedelta(days=self._history_days - 1)
        refresh_start = self._refresh_start(previous, history_start)

        daily: list[DailyConsumption] = []
        month_ranges = tuple(_month_ranges(refresh_start, max_day))
        _LOGGER.debug(
            "Querying daily consumption from %s through %s in %d monthly request(s)",
            refresh_start,
            max_day,
            len(month_ranges),
        )
        for range_number, (range_start, range_end) in enumerate(
            month_ranges,
            start=1,
        ):
            _LOGGER.debug(
                "Querying daily range %d of %d: %s through %s",
                range_number,
                len(month_ranges),
                range_start,
                range_end,
            )
            response = await self._query_consumption(
                form,
                contract_id,
                range_start,
                range_end,
                "Diaria",
            )
            daily.extend(self._parse_daily_graph(response))

        hourly: list[ConsumptionReading] = []
        hourly_days = sorted(
            reading.day for reading in daily if refresh_start <= reading.day <= max_day
        )
        if self._hourly_history_days is not None:
            hourly_days = hourly_days[-self._hourly_history_days :]
        _LOGGER.info(
            "Daily history returned %d day(s); querying %d day(s) of hourly "
            "consumption",
            len(daily),
            len(hourly_days),
        )
        for day_number, day in enumerate(hourly_days, start=1):
            _LOGGER.debug(
                "Querying hourly consumption for day %d of %d: %s",
                day_number,
                len(hourly_days),
                day,
            )
            response = await self._query_consumption(
                form,
                contract_id,
                day,
                day,
                "Horaria",
            )
            hourly.extend(self._parse_hourly_graph(response, day))

        merged_daily = _merge_daily(
            previous.daily_readings if previous is not None else (),
            daily,
            replace_from=refresh_start,
            keep_from=history_start,
        )
        merged_hourly = _merge_hourly(
            previous.hourly_readings if previous is not None else (),
            hourly,
            replace_from=refresh_start,
            keep_from=history_start,
        )
        metadata = self._contract_metadata(parser)
        return ContractConsumption(
            contract_id=contract_id,
            meter_id=metadata.meter_id,
            address=metadata.address,
            meter_reading_m3=metadata.meter_reading_m3,
            meter_reading_at=metadata.meter_reading_at,
            daily_readings=merged_daily,
            hourly_readings=merged_hourly,
        )

    def _refresh_start(
        self,
        previous: ContractConsumption | None,
        history_start: date,
    ) -> date:
        if previous is None or not previous.hourly_readings:
            return history_start
        latest_local_day = (
            previous.hourly_readings[-1].start.astimezone(_PORTAL_TIME_ZONE).date()
        )
        return max(
            history_start,
            latest_local_day - timedelta(days=self._correction_days - 1),
        )

    async def _authenticated_consumption_page(self) -> str:
        result = await self._request_text("GET", self._consumption_url)
        if result.status in _REDIRECT_STATUSES or self._is_login_page(result.text):
            _LOGGER.info(
                "The Canal portal session is not authenticated; starting automatic "
                "login"
            )
            await self._login()
            result = await self._request_text("GET", self._consumption_url)
        if result.status in _REDIRECT_STATUSES or self._is_login_page(result.text):
            _LOGGER.error("The Canal portal rejected the automatic login")
            msg = "The Canal de Isabel II portal rejected the account credentials"
            raise CanalAuthenticationError(msg)
        _LOGGER.debug("An authenticated Canal consumption page is available")
        return result.text

    async def _login(self) -> None:
        started = monotonic()
        _LOGGER.debug("Loading the Canal login form")
        login_page = await self._request_text("GET", self._login_url)
        parser = self._parse_page(login_page.text)
        form = next(
            (candidate for candidate in parser.forms if candidate.has_password), None
        )
        if form is None or not form.action:
            msg = "The portal login page has no recognizable credential form"
            raise CanalInvalidResponseError(msg)
        match = _SITE_KEY_RE.search(login_page.text)
        if match is None:
            msg = "The portal login page has no reCAPTCHA site key"
            raise CanalInvalidResponseError(msg)

        captcha_started = monotonic()
        _LOGGER.info("Requesting an invisible enterprise reCAPTCHA solution")
        try:
            solved = await self._captcha_solver.recaptcha(
                sitekey=match.group("sitekey"),
                url=self._login_url,
                enterprise=1,
                invisible=1,
                userAgent=_USER_AGENT,
            )
            token = str(solved["code"]).strip()
        except Exception as err:  # noqa: BLE001 - third-party solver exceptions vary
            _LOGGER.error(  # noqa: TRY400 - solver tracebacks may contain secrets
                "The reCAPTCHA solver failed after %.1f seconds (%s)",
                monotonic() - captcha_started,
                type(err).__name__,
            )
            msg = "2Captcha could not solve the Canal login challenge"
            raise CanalCaptchaError(msg) from None
        if not token:
            _LOGGER.error(
                "The reCAPTCHA solver returned an empty solution after %.1f seconds",
                monotonic() - captcha_started,
            )
            msg = "2Captcha returned an empty Canal login token"
            raise CanalCaptchaError(msg)
        _LOGGER.info(
            "Received a reCAPTCHA solution in %.1f seconds",
            monotonic() - captcha_started,
        )

        password_name = next(
            (name for name in form.values if name.endswith("password")),
            None,
        )
        if password_name is None:
            msg = "The portal login form has no named password field"
            raise CanalInvalidResponseError(msg)
        namespace = password_name.removesuffix("password")
        payload = dict(form.values)
        payload.update(
            {
                f"{namespace}messageDispositivo": "",
                f"{namespace}errorDispositivo": "",
                f"{namespace}tipoUsuario": "PARTICULAR",
                f"{namespace}tipoUsuarioDesktop": "PARTICULAR",
                f"{namespace}tipoDocumento": self._credentials.document_type,
                f"{namespace}numeroDocumento": self._credentials.normalized_username,
                password_name: self._credentials.password,
                f"{namespace}idThemeLogin": "ovir",
                "g-recaptcha-response": token,
            }
        )
        result = await self._request_text(
            "POST",
            urljoin(self._base_url, form.action),
            data=payload,
            headers={
                "Origin": self._base_url,
                "Referer": self._login_url,
            },
        )
        if result.status not in _REDIRECT_STATUSES and self._is_login_page(result.text):
            _LOGGER.error("The Canal portal rejected the submitted credentials")
            msg = "The Canal de Isabel II portal rejected the account credentials"
            raise CanalAuthenticationError(msg)
        _LOGGER.info(
            "Completed automatic Canal login in %.1f seconds",
            monotonic() - started,
        )

    async def _switch_contract(self, reference: _ContractReference) -> None:
        _LOGGER.debug("Submitting a Canal portal contract switch request")
        split = urlsplit(urljoin(self._base_url, reference.switch_href))
        query = parse_qsl(split.query, keep_blank_values=True)
        contract_key = next(key for key, _ in query if key.endswith("contratoId"))
        action_key = next(
            key for key, _ in query if key.endswith("javax.portlet.action")
        )
        filtered = [
            (key, value)
            for key, value in query
            if not key.endswith(("favorito", "contratoId"))
        ]
        filtered = [
            (
                key,
                "/listadoContratos/contratoPorDefecto" if key == action_key else value,
            )
            for key, value in filtered
        ]
        switch_url = urlunsplit((*split[:3], urlencode(filtered), split.fragment))
        payload_name = f"{contract_key.removesuffix('contratoId')}contractOption"
        result = await self._request_text(
            "POST",
            switch_url,
            data={payload_name: reference.contract_id},
            headers={"Origin": self._base_url, "Referer": self._consumption_url},
        )
        redirects_to_login = result.status in _REDIRECT_STATUSES and (
            result.location is None
            or urlsplit(urljoin(self._base_url, result.location)).path == _LOGIN_PATH
        )
        if redirects_to_login or self._is_login_page(result.text):
            msg = "The Canal portal session expired while switching contracts"
            raise _CanalSessionExpiredError(msg)
        _LOGGER.debug("The Canal portal contract switch completed")

    async def _query_consumption(
        self,
        form: _Form,
        contract_id: str,
        start: date,
        end: date,
        frequency: str,
    ) -> str:
        frequency_label = "daily" if frequency == "Diaria" else "hourly"
        _LOGGER.debug(
            "Preparing a %s consumption query from %s through %s",
            frequency_label,
            start,
            end,
        )
        payload = dict(form.values)
        payload.update(
            {
                select.name: select.selected_value
                for select in form.selects
                if select.name
            }
        )
        overrides = {
            "fechaDesde": start.isoformat(),
            "fechaHasta": end.isoformat(),
            "periodicidad": frequency,
            "contratosFiltro": contract_id,
        }
        for suffix, value in overrides.items():
            name = form.field_name(suffix)
            if name is None:
                msg = f"The consumption form has no {suffix} field"
                raise CanalInvalidResponseError(msg)
            payload[name] = value

        form_data = FormData(default_to_multipart=True)
        for name, value in payload.items():
            form_data.add_field(name, value)
        action_url = urljoin(self._base_url, form.action)
        csrf_token = parse_qs(urlsplit(action_url).query).get("p_auth", [""])[0]
        result = await self._request_text(
            "POST",
            action_url,
            data=form_data,
            headers={
                "Origin": self._base_url,
                "Referer": self._consumption_url,
                "x-csrf-token": csrf_token,
                "x-pjax": "true",
                "x-requested-with": "XMLHttpRequest",
            },
        )
        if result.status in _REDIRECT_STATUSES or self._is_login_page(result.text):
            msg = "The Canal portal session expired during synchronization"
            raise _CanalSessionExpiredError(msg)
        _LOGGER.debug(
            "Completed the %s consumption query from %s through %s",
            frequency_label,
            start,
            end,
        )
        return result.text

    async def _request_text(
        self,
        method: str,
        url: str,
        *,
        data: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> _HttpResult:
        request_started = monotonic()
        safe_path = urlsplit(url).path
        _LOGGER.debug("Starting portal request: %s %s", method, safe_path)
        request_headers = {"User-Agent": _USER_AGENT, **(headers or {})}
        try:
            response = await self._session.request(
                method,
                url,
                allow_redirects=False,
                headers=request_headers,
                timeout=_REQUEST_TIMEOUT,
                data=data,
            )
        except (TimeoutError, ClientError) as err:
            _LOGGER.warning(
                "Portal request failed after %.1f seconds: %s %s (%s)",
                monotonic() - request_started,
                method,
                safe_path,
                type(err).__name__,
            )
            msg = "Unable to communicate with the Canal de Isabel II portal"
            raise CanalConnectionError(msg) from None
        async with response:
            text = await response.text()
            if response.status >= HTTPStatus.BAD_REQUEST:
                _LOGGER.warning(
                    "Portal request returned HTTP %d after %.1f seconds: %s %s",
                    response.status,
                    monotonic() - request_started,
                    method,
                    safe_path,
                )
                msg = f"The Canal de Isabel II portal returned HTTP {response.status}"
                raise CanalConnectionError(msg)
            _LOGGER.debug(
                "Completed portal request in %.1f seconds: %s %s returned HTTP %d "
                "with %d response characters",
                monotonic() - request_started,
                method,
                safe_path,
                response.status,
                len(text),
            )
            return _HttpResult(
                status=response.status,
                text=text,
                location=response.headers.get("Location"),
            )

    @staticmethod
    def _parse_page(page: str) -> _PortalPageParser:
        parser = _PortalPageParser()
        parser.feed(page)
        parser.close()
        return parser

    @classmethod
    def _is_login_page(cls, page: str) -> bool:
        return any(form.has_password for form in cls._parse_page(page).forms)

    def _contract_references(
        self,
        parser: _PortalPageParser,
    ) -> tuple[tuple[_ContractReference, ...], str]:
        active_match = _ACTIVE_CONTRACT_RE.search(parser.text)
        references: list[_ContractReference] = []
        for link in parser.links:
            query = parse_qs(urlsplit(link.href).query)
            contract_id = next(
                (
                    values[0]
                    for key, values in query.items()
                    if key.endswith("contratoId") and values
                ),
                None,
            )
            if contract_id:
                references.append(_ContractReference(contract_id, link.href))
        references = list(
            {reference.contract_id: reference for reference in references}.values()
        )
        if active_match is None or not references:
            msg = "The portal response has no recognizable contract selector"
            raise CanalInvalidResponseError(msg)
        active = active_match.group("contract")
        if active not in {reference.contract_id for reference in references}:
            msg = "The active contract is missing from the contract selector"
            raise CanalInvalidResponseError(msg)
        references.sort(key=lambda reference: reference.contract_id != active)
        return tuple(references), active

    @staticmethod
    def _consumption_form(parser: _PortalPageParser) -> _Form:
        form = next(
            (
                candidate
                for candidate in parser.forms
                if candidate.field_name("fechaDesde")
                and candidate.field_name("fechaHasta")
                and candidate.field_name("periodicidad")
                and candidate.field_name("contratosFiltro")
            ),
            None,
        )
        if form is None or not form.action:
            msg = "The portal response has no consumption form"
            raise CanalInvalidResponseError(msg)
        return form

    @staticmethod
    def _maximum_available_day(form: _Form) -> date:
        name = form.field_name("fechaHasta")
        try:
            return date.fromisoformat(form.values[name or ""])
        except (KeyError, ValueError) as err:
            msg = "The consumption form has no valid maximum date"
            raise CanalInvalidResponseError(msg) from err

    @staticmethod
    def _contract_metadata(parser: _PortalPageParser) -> _ContractMetadata:
        match = _METADATA_RE.search(parser.text)
        if match is None:
            return _ContractMetadata(None, None, None, None)
        reading_match = re.search(r"-?\d[\d.,]*", match.group("reading"))
        if reading_match is None:
            msg = "The portal returned invalid meter metadata"
            raise CanalInvalidResponseError(msg)
        try:
            meter_reading = _parse_decimal(reading_match.group())
            local_reading_at = datetime.strptime(
                match.group("read_at"),
                "%d/%m/%Y %H:%M",
            ).replace(tzinfo=_PORTAL_TIME_ZONE)
        except ValueError as err:
            msg = "The portal returned invalid meter metadata"
            raise CanalInvalidResponseError(msg) from err
        return _ContractMetadata(
            meter_id=match.group("meter").strip() or None,
            address=match.group("address").strip() or None,
            meter_reading_m3=meter_reading,
            meter_reading_at=local_reading_at.astimezone(UTC),
        )

    @classmethod
    def _parse_daily_graph(cls, page: str) -> list[DailyConsumption]:
        readings: list[DailyConsumption] = []
        for label, value in cls._graph_rows(page):
            match = _DATE_RE.search(label)
            if match is None:
                msg = f"Unsupported daily consumption label: {label!r}"
                raise CanalInvalidResponseError(msg)
            readings.append(
                DailyConsumption(
                    day=_parse_portal_date(match.group("date")),
                    volume_liters=value,
                )
            )
        return readings

    @classmethod
    def _parse_hourly_graph(
        cls,
        page: str,
        requested_day: date,
    ) -> list[ConsumptionReading]:
        readings: list[ConsumptionReading] = []
        for label, value in cls._graph_rows(page):
            hour_match = _HOUR_RE.search(label)
            date_match = _DATE_RE.search(label)
            if hour_match is None:
                msg = f"Unsupported hourly consumption label: {label!r}"
                raise CanalInvalidResponseError(msg)
            row_day = (
                _parse_portal_date(date_match.group("date"))
                if date_match is not None
                else requested_day
            )
            local = datetime.combine(
                row_day,
                datetime.min.time(),
                tzinfo=_PORTAL_TIME_ZONE,
            ).replace(hour=int(hour_match.group("hour")))
            readings.append(
                ConsumptionReading(start=local.astimezone(UTC), volume_liters=value)
            )
        return readings

    @staticmethod
    def _graph_rows(page: str) -> list[tuple[str, float]]:
        if "dataJsonConsumo" not in page:
            msg = "The portal response has no consumption graph"
            raise CanalInvalidResponseError(msg)
        rows = [
            (match.group("label").replace("\\'", "'"), float(match.group("value")))
            for match in _GRAPH_ROW_RE.finditer(page)
        ]
        if not rows and not re.search(r"rows\s*:\s*\[\s*\]", page):
            msg = "The portal returned an invalid consumption graph"
            raise CanalInvalidResponseError(msg)
        if any(value < 0 for _, value in rows):
            msg = "The portal returned negative consumption"
            raise CanalInvalidResponseError(msg)
        return rows


def _month_ranges(start: date, end: date) -> Iterable[tuple[date, date]]:
    """Yield inclusive query ranges that never cross a calendar month."""
    current = start
    while current <= end:
        last = date(
            current.year, current.month, monthrange(current.year, current.month)[1]
        )
        range_end = min(last, end)
        yield current, range_end
        current = range_end + timedelta(days=1)


def _parse_portal_date(value: str) -> date:
    """Parse the portal's fixed day/month/year representation."""
    day, month, year = (int(part) for part in value.split("/"))
    return date(year, month, day)


def _merge_daily(
    existing: tuple[DailyConsumption, ...],
    fresh: list[DailyConsumption],
    *,
    replace_from: date,
    keep_from: date,
) -> tuple[DailyConsumption, ...]:
    merged = {
        reading.day: reading
        for reading in existing
        if keep_from <= reading.day < replace_from
    }
    merged.update({reading.day: reading for reading in fresh})
    return tuple(merged[day] for day in sorted(merged) if day >= keep_from)


def _merge_hourly(
    existing: tuple[ConsumptionReading, ...],
    fresh: list[ConsumptionReading],
    *,
    replace_from: date,
    keep_from: date,
) -> tuple[ConsumptionReading, ...]:
    merged = {
        reading.start: reading
        for reading in existing
        if keep_from
        <= reading.start.astimezone(_PORTAL_TIME_ZONE).date()
        < replace_from
    }
    merged.update({reading.start: reading for reading in fresh})
    return tuple(merged[start] for start in sorted(merged))


def _parse_decimal(value: str) -> float:
    normalized = value.strip().replace("\N{NO-BREAK SPACE}", "").replace(" ", "")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    result = float(normalized)
    if result < 0:
        msg = f"Consumption cannot be negative: {value!r}"
        raise CanalInvalidResponseError(msg)
    return result
