"""Constants for the De Lijn integration."""

DOMAIN = "delijn"
VERSION = "1.2.5"

# V1 Core API
API_BASE_URL = "https://api.delijn.be/DLKernOpenData/api/v1"
API_AUTH_HEADER = "Ocp-Apim-Subscription-Key"
API_ENTITIES = [1, 2, 3, 4, 5]  # Antwerpen, Oost-Vl, Vlaams-Brabant, Limburg, West-Vl

# Configuration keys
CONF_API_KEY = "api_key"
CONF_STOPS = "stops"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LANGUAGE = "language"

# Languages
LANG_NL = "nl"
LANG_FR = "fr"
DEFAULT_LANGUAGE = LANG_NL

# Defaults
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 30
MAX_DEPARTURES = 10

# Stop classifications
CLASSIFICATIE_REGULIER = "REGULIER"
CLASSIFICATIE_TIJDELIJK = "TIJDELIJK"
CLASSIFICATIE_FLEX = "FLEX"
CLASSIFICATIE_COMBI = "COMBI"

# Prediction statuses
PREDICTION_REALTIME = "REALTIME"
PREDICTION_NO_REALTIME = "GEENREALTIME"
PREDICTION_CANCELLED = "GESCHRAPT"
PREDICTION_PASSED = "VERSTREKEN"

# Cache
CACHE_DIR_NAME = ".delijn_cache"
CACHE_STOPS_FILE = "stops_v1.json.gz"

# Entity attributes
ATTR_LINE = "line"
ATTR_BADGE_BACKGROUND = "badge_background"
ATTR_BADGE_TEXT = "badge_text"
ATTR_BADGE_BORDER = "badge_border"
ATTR_BADGE_TEXT_BORDER = "badge_text_border"
ATTR_DIRECTION = "direction"
ATTR_DESTINATION = "destination"
ATTR_DESTINATION_FR = "destination_fr"
ATTR_STOP_NAME = "stop_name"
ATTR_STOP_NUMBER = "stop_number"
ATTR_STOP_TYPE = "stop_type"
ATTR_SCHEDULED = "scheduled"
ATTR_REALTIME = "realtime"
ATTR_DELAY_MINUTES = "delay_minutes"
ATTR_VEHICLE_ID = "vehicle_id"
ATTR_PREDICTION = "prediction"
ATTR_NEXT_DEPARTURES = "next_departures"
ATTR_ALERTS = "alerts"
ATTR_LAST_UPDATED = "last_updated"
