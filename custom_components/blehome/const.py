# blehome/const.py
"""Constants for the BLEHome integration."""
from homeassistant.const import Platform

DOMAIN = "blehome"
MANUFACTURER = "BLEHome"

# Default device prefix
DEFAULT_PREFIX = "BLE"

# Command format constants
HEADER = 0xA5
CONTROL_CMD = 0x8202
QUERY_CMD = 0x8201
BTHOME_PROXY_CMD = 0x8203  # BTHome proxy command

# Default UUIDs
DEFAULT_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
DEFAULT_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.TEXT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Config entry keys
CONF_MAC = "mac"
CONF_SERVICE_UUID = "service_uuid"
CONF_CHAR_UUID = "char_uuid"
CONF_SUBDEVICES = "subdevices"
CONF_BTHOME_MOCK = "bthome_mock"