"""Tests for the public Home Assistant configuration flow."""

from collections.abc import Iterator
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canal_de_isabel_ii.const import (
    CONF_BILLING_CYCLE_DAYS,
    CONF_BILLING_PERIOD_START,
    CONF_CAPTCHA_API_KEY,
    CONF_CAPTCHA_ATTEMPTS,
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
    DOMAIN,
)
from custom_components.canal_de_isabel_ii.tariffs import SewerProvider, SupplyType

from .factories import make_snapshot

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


async def test_user_flow_saves_before_any_remote_work(hass: HomeAssistant) -> None:
    """Submitting credentials must not wait for CAPTCHA or portal scraping."""
    with patch(
        "custom_components.canal_de_isabel_ii.client."
        "CanalClient.async_validate_credentials",
        side_effect=AssertionError("The config flow contacted the portal"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=VALID_INPUT,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


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
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "schedule"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "schedule"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SYNC_HOUR: 5, CONF_CAPTCHA_ATTEMPTS: 5},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SYNC_HOUR: 5,
        CONF_CAPTCHA_ATTEMPTS: 5,
    }


async def test_options_flow_stores_captcha_attempts(hass: HomeAssistant) -> None:
    """Users can tune the bounded CAPTCHA retry count from Configure."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT, version=3)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "schedule"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SYNC_HOUR: 5, CONF_CAPTCHA_ATTEMPTS: 7},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SYNC_HOUR: 5,
        CONF_CAPTCHA_ATTEMPTS: 7,
    }


async def test_options_flow_schedules_full_resynchronization(
    hass: HomeAssistant,
) -> None:
    """The Configure menu exposes a confirmed background full resync action."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT, version=3)
    entry.add_to_hass(hass)
    schedule_full_resync = Mock()
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            async_schedule_full_resync=schedule_full_resync,
        )
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "resync"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "resync"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "resync_started"
    schedule_full_resync.assert_called_once_with()


async def test_options_flow_saves_tariff_profile_per_contract(
    hass: HomeAssistant,
) -> None:
    """Users can provide only the billing facts absent from the portal."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=VALID_INPUT,
        options={CONF_SYNC_HOUR: 5},
        version=3,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=make_snapshot())
    )
    profile_input = {
        CONF_SUPPLY_TYPE: SupplyType.SINGLE_DWELLING,
        CONF_SEWER_PROVIDER: SewerProvider.MUNICIPALITY,
        CONF_METER_DIAMETER_MM: 15,
        CONF_SUPPLIED_USES: 1,
        CONF_BILLING_PERIOD_START: date(2026, 7, 8).isoformat(),
        CONF_BILLING_CYCLE_DAYS: 60,
        CONF_MUNICIPAL_SEWER_RATE: 0.22,
    }

    with (
        patch(
            "custom_components.canal_de_isabel_ii.config_flow."
            "CanalTariffProfileStore.async_load",
            return_value={},
        ),
        patch(
            "custom_components.canal_de_isabel_ii.config_flow."
            "CanalTariffProfileStore.async_save",
            return_value=None,
        ) as save,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "tariff"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "tariff"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_TARIFF_CONTRACT: "contract-123"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "tariff_profile"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            profile_input,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SYNC_HOUR: 5,
        CONF_TARIFF_REVISION: 1,
    }
    saved_profiles = save.await_args.args[0]
    assert set(saved_profiles) == {"contract-123"}
    profile = saved_profiles["contract-123"]
    assert profile.billing_period_start == date(2026, 7, 8)
    assert profile.municipal_sewer_rate_eur_m3.as_tuple().exponent == -2


async def test_options_flow_rejects_incomplete_municipal_tariff(
    hass: HomeAssistant,
) -> None:
    """A zero municipal rate stays in the form instead of publishing zero cost."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT, version=3)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=make_snapshot())
    )

    with patch(
        "custom_components.canal_de_isabel_ii.config_flow."
        "CanalTariffProfileStore.async_load",
        return_value={},
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "tariff"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_TARIFF_CONTRACT: "contract-123"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SUPPLY_TYPE: SupplyType.SINGLE_DWELLING,
                CONF_SEWER_PROVIDER: SewerProvider.MUNICIPALITY,
                CONF_METER_DIAMETER_MM: 15,
                CONF_SUPPLIED_USES: 1,
                CONF_BILLING_PERIOD_START: "2026-07-08",
                CONF_BILLING_CYCLE_DAYS: 60,
                CONF_MUNICIPAL_SEWER_RATE: 0,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_tariff_profile"}


async def test_options_flow_waits_for_contract_discovery(
    hass: HomeAssistant,
) -> None:
    """Pricing configuration explains when no synchronized contract exists."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT, version=3)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "tariff"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_contracts"
