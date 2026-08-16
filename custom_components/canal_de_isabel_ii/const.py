"""Constants for the Canal de Isabel II integration."""

DOMAIN = "canal_de_isabel_ii"

CONFIG_ENTRY_VERSION = 3
CONFIG_ENTRY_MINOR_VERSION = 0

CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105 - Home Assistant config field name
CONF_CAPTCHA_API_KEY = "captcha_api_key"
CONF_CAPTCHA_ATTEMPTS = "captcha_attempts"
CONF_SYNC_HOUR = "sync_hour"
CONF_TARIFF_CONTRACT = "tariff_contract"
CONF_SUPPLY_TYPE = "supply_type"
CONF_SEWER_PROVIDER = "sewer_provider"
CONF_METER_DIAMETER_MM = "meter_diameter_mm"
CONF_SUPPLIED_USES = "supplied_uses"
CONF_BILLING_PERIOD_START = "billing_period_start"
CONF_BILLING_CYCLE_DAYS = "billing_cycle_days"
CONF_MUNICIPAL_SEWER_RATE = "municipal_sewer_rate_eur_m3"
CONF_TARIFF_REVISION = "tariff_revision"

DEFAULT_SYNC_HOUR = 3
DEFAULT_CAPTCHA_ATTEMPTS = 5
MAX_CAPTCHA_ATTEMPTS = 10

DEFAULT_NAME = "Canal de Isabel II"

BASE_URL = "https://oficinavirtual.canaldeisabelsegunda.es"
CONSUMPTION_URL = f"{BASE_URL}/group/ovir/consumo"
