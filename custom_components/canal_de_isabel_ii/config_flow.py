"""Configuration and scheduling flows for Canal de Isabel II."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .client import CanalCredentials
from .const import (
    CONF_BILLING_CYCLE_DAYS,
    CONF_BILLING_PERIOD_START,
    CONF_CAPTCHA_API_KEY,
    CONF_METER_DIAMETER_MM,
    CONF_MUNICIPAL_SEWER_RATE,
    CONF_PASSWORD,
    CONF_SEWER_PROVIDER,
    CONF_SUPPLIED_USES,
    CONF_SUPPLY_TYPE,
    CONF_SYNC_HOUR,
    CONF_TARIFF_CONTRACT,
    CONF_TARIFF_REVISION,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_NAME,
    DEFAULT_SYNC_HOUR,
    DOMAIN,
)
from .tariff_storage import CanalTariffProfileStore
from .tariffs import SewerProvider, SupplyType, TariffProfile

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)


def _credentials_schema(
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    defaults = defaults or {}
    username_marker = (
        vol.Required(CONF_USERNAME, default=defaults[CONF_USERNAME])
        if CONF_USERNAME in defaults
        else vol.Required(CONF_USERNAME)
    )
    return vol.Schema(
        {
            username_marker: selector.TextSelector(),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_CAPTCHA_API_KEY): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class CanalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure unattended authenticated access to one portal account."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    @staticmethod
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> CanalOptionsFlow:
        """Return the daily synchronization options flow."""
        del config_entry
        return CanalOptionsFlow()

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create one entry per Canal account."""
        if user_input is None:
            return self._show_credentials_form("user")

        error = _credentials_error(user_input)
        if error is not None:
            return self._show_credentials_form("user", error, user_input)

        credentials = _credentials_from_input(user_input)
        await self.async_set_unique_id(credentials.normalized_username)
        self._abort_if_unique_id_configured()
        _LOGGER.info(
            "Saving Canal account configuration before starting background "
            "authentication and synchronization"
        )
        return self.async_create_entry(title=DEFAULT_NAME, data=user_input)

    @override
    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start reauthentication for changed account credentials."""
        return self._show_credentials_form("reauth_confirm", defaults=entry_data)

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and store replacement account credentials."""
        entry = self._get_reauth_entry()
        if user_input is None:
            return self._show_credentials_form(
                "reauth_confirm",
                defaults=entry.data,
            )

        error = _credentials_error(user_input)
        if error is not None:
            return self._show_credentials_form(
                "reauth_confirm",
                error,
                user_input,
            )

        credentials = _credentials_from_input(user_input)
        await self.async_set_unique_id(credentials.normalized_username)
        if entry.unique_id not in {None, DOMAIN}:
            self._abort_if_unique_id_mismatch()
        configured = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN,
            credentials.normalized_username,
        )
        if configured is not None and configured.entry_id != entry.entry_id:
            return self.async_abort(reason="already_configured")
        _LOGGER.info(
            "Saving updated Canal account credentials; authentication will be "
            "checked by the background synchronization"
        )
        return self._update_credentials_and_abort(
            entry,
            user_input,
            credentials.normalized_username,
            "reauth_successful",
        )

    @override
    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow proactive replacement of account credentials."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self._show_credentials_form("reconfigure", defaults=entry.data)

        error = _credentials_error(user_input)
        if error is not None:
            return self._show_credentials_form("reconfigure", error, user_input)

        credentials = _credentials_from_input(user_input)
        await self.async_set_unique_id(credentials.normalized_username)
        self._abort_if_unique_id_mismatch()
        _LOGGER.info(
            "Saving reconfigured Canal account credentials; authentication will "
            "be checked by the background synchronization"
        )
        return self._update_credentials_and_abort(
            entry,
            user_input,
            credentials.normalized_username,
            "reconfigure_successful",
        )

    def _update_credentials_and_abort(
        self,
        entry: ConfigEntry,
        data: Mapping[str, Any],
        unique_id: str,
        reason: str,
    ) -> ConfigFlowResult:
        """Update once and let the entry listener perform any required reload."""
        self.hass.config_entries.async_update_entry(
            entry,
            data=data,
            unique_id=unique_id,
        )
        if not entry.update_listeners:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason)

    def _show_credentials_form(
        self,
        step_id: str,
        error: str | None = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the shared credential form without echoing secret defaults."""
        safe_defaults = (
            {CONF_USERNAME: defaults.get(CONF_USERNAME, "")} if defaults else None
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=_credentials_schema(safe_defaults),
            errors={"base": error} if error else {},
        )


class CanalOptionsFlow(OptionsFlow):
    """Configure scheduling and private per-contract tariff profiles."""

    def __init__(self) -> None:
        """Initialize transient options-flow state."""
        self._tariff_contract_id: str | None = None
        self._tariff_profiles: dict[str, TariffProfile] = {}

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose between scheduling and contract pricing."""
        del user_input
        return self.async_show_menu(
            step_id="init",
            menu_options=("schedule", "tariff"),
        )

    async def async_step_schedule(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Store the preferred synchronization hour."""
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_SYNC_HOUR] = int(user_input[CONF_SYNC_HOUR])
            return self.async_create_entry(
                data=options,
            )

        current_hour = self.config_entry.options.get(
            CONF_SYNC_HOUR,
            DEFAULT_SYNC_HOUR,
        )
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYNC_HOUR,
                        default=current_hour,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=23,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )

    async def async_step_tariff(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose the discovered contract whose bill settings should change."""
        contracts = self._available_contracts()
        if not contracts:
            return self.async_abort(reason="no_contracts")
        if user_input is not None:
            self._tariff_contract_id = str(user_input[CONF_TARIFF_CONTRACT])
            self._tariff_profiles = await CanalTariffProfileStore(
                self.hass,
                self.config_entry.entry_id,
            ).async_load()
            return await self.async_step_tariff_profile()

        contract_options = [
            selector.SelectOptionDict(value=contract_id, label=label)
            for contract_id, label in contracts
        ]
        return self.async_show_form(
            step_id="tariff",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARIFF_CONTRACT): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=contract_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_tariff_profile(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate and persist the selected contract's billing facts."""
        contract_id = self._tariff_contract_id
        if contract_id is None:
            return await self.async_step_tariff()
        if user_input is None:
            return self._show_tariff_profile_form(
                self._tariff_profiles.get(contract_id)
            )

        try:
            profile = _tariff_profile_from_input(user_input)
        except TypeError, ValueError:
            return self._show_tariff_profile_form(
                None,
                error="invalid_tariff_profile",
                defaults=user_input,
            )

        profiles = dict(self._tariff_profiles)
        profiles[contract_id] = profile
        await CanalTariffProfileStore(
            self.hass,
            self.config_entry.entry_id,
        ).async_save(profiles)
        options = dict(self.config_entry.options)
        options[CONF_TARIFF_REVISION] = int(options.get(CONF_TARIFF_REVISION, 0)) + 1
        return self.async_create_entry(data=options)

    def _available_contracts(self) -> tuple[tuple[str, str], ...]:
        """Return private UI labels for contracts discovered by the coordinator."""
        try:
            snapshot = self.config_entry.runtime_data.coordinator.data
        except AttributeError:
            return ()
        if snapshot is None:
            return ()
        return tuple(
            (
                contract_id,
                contract.address or f"Contract {index}",
            )
            for index, (contract_id, contract) in enumerate(
                sorted(snapshot.contracts.items()),
                start=1,
            )
        )

    def _show_tariff_profile_form(
        self,
        profile: TariffProfile | None,
        *,
        error: str | None = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show all non-portal contract facts in one explicit form."""
        values: Mapping[str, Any]
        if defaults is not None:
            values = defaults
        elif profile is not None:
            values = {
                CONF_SUPPLY_TYPE: profile.supply_type,
                CONF_SEWER_PROVIDER: profile.sewer_provider,
                CONF_METER_DIAMETER_MM: profile.meter_diameter_mm,
                CONF_SUPPLIED_USES: profile.supplied_uses,
                CONF_BILLING_PERIOD_START: (profile.billing_period_start.isoformat()),
                CONF_BILLING_CYCLE_DAYS: profile.billing_cycle_days,
                CONF_MUNICIPAL_SEWER_RATE: float(profile.municipal_sewer_rate_eur_m3),
            }
        else:
            values = {
                CONF_SUPPLY_TYPE: SupplyType.SINGLE_DWELLING,
                CONF_SEWER_PROVIDER: SewerProvider.CANAL,
                CONF_METER_DIAMETER_MM: 15,
                CONF_SUPPLIED_USES: 1,
                CONF_BILLING_PERIOD_START: dt_util.now().date().isoformat(),
                CONF_BILLING_CYCLE_DAYS: 60,
                CONF_MUNICIPAL_SEWER_RATE: 0,
            }
        return self.async_show_form(
            step_id="tariff_profile",
            data_schema=_tariff_profile_schema(values),
            errors={"base": error} if error else {},
        )


def _credentials_from_input(user_input: Mapping[str, Any]) -> CanalCredentials:
    return CanalCredentials(
        username=str(user_input[CONF_USERNAME]),
        password=str(user_input[CONF_PASSWORD]),
        captcha_api_key=str(user_input[CONF_CAPTCHA_API_KEY]),
    )


def _credentials_error(user_input: Mapping[str, Any]) -> str | None:
    """Validate only local fields so configuration never waits on the portal."""
    try:
        _credentials_from_input(user_input)
    except KeyError, ValueError:
        return "invalid_auth"
    return None


def _tariff_profile_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return selectors for the contract facts absent from the portal."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SUPPLY_TYPE,
                default=defaults[CONF_SUPPLY_TYPE],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[item.value for item in SupplyType],
                    translation_key="supply_type",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_SEWER_PROVIDER,
                default=defaults[CONF_SEWER_PROVIDER],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[item.value for item in SewerProvider],
                    translation_key="sewer_provider",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_METER_DIAMETER_MM,
                default=defaults[CONF_METER_DIAMETER_MM],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=500,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SUPPLIED_USES,
                default=defaults[CONF_SUPPLIED_USES],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=10_000,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_BILLING_PERIOD_START,
                default=defaults[CONF_BILLING_PERIOD_START],
            ): selector.DateSelector(),
            vol.Required(
                CONF_BILLING_CYCLE_DAYS,
                default=defaults[CONF_BILLING_CYCLE_DAYS],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=30,
                    max=90,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MUNICIPAL_SEWER_RATE,
                default=defaults[CONF_MUNICIPAL_SEWER_RATE],
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=10,
                    step="any",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _tariff_profile_from_input(user_input: Mapping[str, Any]) -> TariffProfile:
    """Normalize options-flow primitives into one validated domain profile."""
    return TariffProfile(
        supply_type=SupplyType(user_input[CONF_SUPPLY_TYPE]),
        sewer_provider=SewerProvider(user_input[CONF_SEWER_PROVIDER]),
        meter_diameter_mm=int(user_input[CONF_METER_DIAMETER_MM]),
        supplied_uses=int(user_input[CONF_SUPPLIED_USES]),
        billing_period_start=date.fromisoformat(
            str(user_input[CONF_BILLING_PERIOD_START])
        ),
        billing_cycle_days=int(user_input[CONF_BILLING_CYCLE_DAYS]),
        municipal_sewer_rate_eur_m3=Decimal(str(user_input[CONF_MUNICIPAL_SEWER_RATE])),
    )
