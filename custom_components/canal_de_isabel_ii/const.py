"""Constants for the Canal de Isabel II integration."""

DOMAIN = "canal_de_isabel_ii"

CONFIG_ENTRY_VERSION = 3
CONFIG_ENTRY_MINOR_VERSION = 0

CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105 - Home Assistant config field name
CONF_CAPTCHA_API_KEY = "captcha_api_key"
CONF_SYNC_HOUR = "sync_hour"

DEFAULT_SYNC_HOUR = 3

DEFAULT_NAME = "Canal de Isabel II"

BASE_URL = "https://oficinavirtual.canaldeisabelsegunda.es"
CONSUMPTION_URL = f"{BASE_URL}/group/ovir/consumo"
