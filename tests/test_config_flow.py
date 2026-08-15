"""Tests for the public Home Assistant configuration flow."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canal_de_isabel_ii.client import (
    CanalAuthenticationError,
    CanalCaptchaError,
    CanalConnectionError,
    CanalInvalidResponseError,
)
from custom_components.canal_de_isabel_ii.const import (
    CONF_CAPTCHA_API_KEY,
    CONF_PASSWORD,
    CONF_SYNC_HOUR,
    CONF_USERNAME,
    DOMAIN,
)

VALID_INPUT = {
    CONF_USERNAME: "x1234567l",
    CONF_PASSWORD: "secret",
    CONF_CAPTCHA_API_KEY: "captcha-key",
}


@pytest.fixture(autouse=True)
def mock_entry_setup() -> Iterator[None]:
    """Prevent config-flow entries from starting real portal synchronization."""
    with patch(
        "custom_components.canal_de_isabel_ii.async_setup_entry",
        return_value=True,
    ):
        yield


async def test_user_flow_creates_account_entry(hass: HomeAssistant) -> None:
    """Valid unattended credentials create one normalized account entry."""
    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            VALID_INPUT,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Canal de Isabel II"
    assert result["data"] == VALID_INPUT
    assert result["result"].unique_id == "X1234567L"


async def test_reconfigure_replaces_credentials(hass: HomeAssistant) -> None:
    """The user can proactively replace all portal credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data=VALID_INPUT,
        unique_id="X1234567L",
        version=3,
    )
    entry.add_to_hass(hass)
    replacement = {**VALID_INPUT, CONF_PASSWORD: "new-secret"}

    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"
        assert "new-secret" not in str(result["data_schema"])

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            replacement,
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == replacement


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (CanalAuthenticationError, "invalid_auth"),
        (CanalCaptchaError, "captcha_failed"),
        (CanalConnectionError, "cannot_connect"),
        (CanalInvalidResponseError, "invalid_response"),
    ],
)
async def test_user_flow_reports_remote_errors(
    hass: HomeAssistant,
    exception: type[Exception],
    expected_error: str,
) -> None:
    """Known remote failures produce actionable form errors."""
    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        side_effect=exception,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=VALID_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


async def test_reauth_updates_expired_credentials(hass: HomeAssistant) -> None:
    """An authentication flow updates credentials on the same entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data=VALID_INPUT,
        unique_id="X1234567L",
        version=3,
    )
    entry.add_to_hass(hass)
    replacement = {**VALID_INPUT, CONF_CAPTCHA_API_KEY: "replacement-key"}

    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert "replacement-key" not in str(result["data_schema"])

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            replacement,
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == replacement


async def test_reauth_rejects_credentials_for_another_account(
    hass: HomeAssistant,
) -> None:
    """Reauthentication cannot silently turn an entry into another account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Canal de Isabel II",
        data=VALID_INPUT,
        unique_id="X1234567L",
        version=3,
    )
    entry.add_to_hass(hass)
    other_account = {**VALID_INPUT, CONF_USERNAME: "12345678Z"}

    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            other_account,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data == VALID_INPUT


async def test_user_flow_rejects_duplicate_account(hass: HomeAssistant) -> None:
    """The same portal account cannot be configured twice."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="X1234567L",
        data=VALID_INPUT,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalClient.async_validate_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=VALID_INPUT,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_rejects_empty_credentials(hass: HomeAssistant) -> None:
    """Empty credentials remain a form error instead of crashing the flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={**VALID_INPUT, CONF_PASSWORD: ""},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_stores_daily_sync_hour(hass: HomeAssistant) -> None:
    """The synchronization hour can be changed without editing credentials."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT, version=3)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SYNC_HOUR: 5},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SYNC_HOUR: 5}
