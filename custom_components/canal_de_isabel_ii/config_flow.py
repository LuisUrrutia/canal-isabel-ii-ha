"""Configuration and scheduling flows for Canal de Isabel II."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

import voluptuous as vol
from aiohttp import CookieJar
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import aiohttp_client, selector

from .client import (
    CanalAuthenticationError,
    CanalCaptchaError,
    CanalClient,
    CanalConnectionError,
    CanalCredentials,
    CanalInvalidResponseError,
)
from .const import (
    CONF_CAPTCHA_API_KEY,
    CONF_PASSWORD,
    CONF_SYNC_HOUR,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_NAME,
    DEFAULT_SYNC_HOUR,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


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

        error = await self._async_credentials_error(user_input)
        if error is not None:
            return self._show_credentials_form("user", error, user_input)

        credentials = _credentials_from_input(user_input)
        await self.async_set_unique_id(credentials.normalized_username)
        self._abort_if_unique_id_configured()
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

        error = await self._async_credentials_error(user_input)
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

        error = await self._async_credentials_error(user_input)
        if error is not None:
            return self._show_credentials_form("reconfigure", error, user_input)

        credentials = _credentials_from_input(user_input)
        await self.async_set_unique_id(credentials.normalized_username)
        self._abort_if_unique_id_mismatch()
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

    async def _async_credentials_error(
        self,
        user_input: Mapping[str, Any],
    ) -> str | None:
        """Return a flow error key, or None when unattended login works."""
        try:
            credentials = _credentials_from_input(user_input)
        except KeyError, ValueError:
            return "invalid_auth"

        session = aiohttp_client.async_create_clientsession(
            self.hass,
            auto_cleanup=False,
            cookie_jar=CookieJar(),
        )
        try:
            await CanalClient(session, credentials).async_validate_credentials()
        except CanalAuthenticationError:
            return "invalid_auth"
        except CanalCaptchaError:
            return "captcha_failed"
        except CanalConnectionError:
            return "cannot_connect"
        except CanalInvalidResponseError:
            return "invalid_response"
        finally:
            session.detach()
        return None

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
    """Configure the local hour used for the daily incremental sync."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Store the preferred synchronization hour."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SYNC_HOUR: int(user_input[CONF_SYNC_HOUR])}
            )

        current_hour = self.config_entry.options.get(
            CONF_SYNC_HOUR,
            DEFAULT_SYNC_HOUR,
        )
        return self.async_show_form(
            step_id="init",
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


def _credentials_from_input(user_input: Mapping[str, Any]) -> CanalCredentials:
    return CanalCredentials(
        username=str(user_input[CONF_USERNAME]),
        password=str(user_input[CONF_PASSWORD]),
        captcha_api_key=str(user_input[CONF_CAPTCHA_API_KEY]),
    )
