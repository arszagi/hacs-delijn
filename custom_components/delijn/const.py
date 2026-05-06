"""Constants for the De Lijn integration."""

DOMAIN = "delijn"
VERSION = "0.0.5"

# API
API_BASE_URL = "https://api-management-opendata-production.azure-api.net"
API_STATIC_PATH = "/api/gtfs/feed/delijn/static/"
API_TRIP_UPDATES_PATH = "/api/gtfs/feed/delijn/rt/trip-update/"
API_ALERTS_PATH = "/api/gtfs/feed/delijn/rt/alert/"
API_AUTH_HEADER = "bmc-partner-key"

# Configuration keys
CONF_API_KEY = "api_key"
CONF_STOP_IDS = "stop_ids"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 30
MAX_UPCOMING_DEPARTURES = 5

# GTFS-RT scheduleRelationship enum values
SCHEDULE_RELATIONSHIP_SCHEDULED = 0
SCHEDULE_RELATIONSHIP_SKIPPED = 1
SCHEDULE_RELATIONSHIP_NO_DATA = 2
SCHEDULE_RELATIONSHIP_UNSCHEDULED = 3

# Cache
CACHE_DIR_NAME = ".delijn_cache"
CACHE_STOPS_FILE = "stops.json"
CACHE_ROUTES_FILE = "routes.json"
CACHE_TRIPS_FILE = "trips.json.gz"
CACHE_LAST_MODIFIED_FILE = "last_modified.txt"

# Entity attributes
ATTR_LINE = "line"
ATTR_HEADSIGN = "headsign"
ATTR_DIRECTION_ID = "direction_id"
ATTR_STOP_NAME = "stop_name"
ATTR_STOP_ID = "stop_id"
ATTR_ROUTE_ID = "route_id"
ATTR_SCHEDULED_DEPARTURE = "scheduled_departure"
ATTR_REALTIME_DEPARTURE = "realtime_departure"
ATTR_DELAY_MINUTES = "delay_minutes"
ATTR_NEXT_DEPARTURES = "next_departures"
ATTR_LAST_UPDATED = "last_updated"
ATTR_VEHICLE_ID = "vehicle_id"
ATTR_ALERTS = "alerts"

# GTFS route_type
ROUTE_TYPE_TRAM = 0
ROUTE_TYPE_BUS = 3
