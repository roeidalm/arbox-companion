"""Constants for the Arbox integration (thin client of arbox-server)."""

DOMAIN = "arbox"

CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"

DEFAULT_BASE_URL = "http://arbox-server:8000"
DEFAULT_SCAN_INTERVAL = 5  # minutes — reads the server's local DB, not Arbox

# Services
SERVICE_BOOK_CLASS = "book_class"
SERVICE_CANCEL_BOOKING = "cancel_booking"
SERVICE_JOIN_STANDBY = "join_standby"
