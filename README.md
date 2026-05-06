# De Lijn — Home Assistant Integration

> [!WARNING]
> **This integration is currently in active development and is not ready for use.**
> Functionality may be incomplete, unstable or subject to breaking changes at any time.
> Do not use in a production Home Assistant environment.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.0.5-blue.svg)](https://github.com/arszagi/hacs-delijn/releases)

A Home Assistant custom integration for **De Lijn** — the public transport operator for buses and trams in Flanders, Belgium.

Provides real-time departure times, delays and service alerts for any De Lijn stop, directly in your Home Assistant dashboard.

---

## Features

- **Real-time departures** — live arrival/departure times with delays from the GTFS-RT feed
- **Per-line sensors** — one sensor per bus/tram line per stop, grouped by stop as a HA device
- **Service alerts** — active disruptions, cancellations and diversions per stop
- **Multi-stop support** — monitor as many stops as you need
- **Smart GTFS caching** — schedule data downloaded once, refreshed only when changed
- **Configurable refresh interval** — default 30 seconds, minimum 30 seconds
- **Full options flow** — add/remove stops and change settings without reinstalling

---

## Requirements

- Home Assistant **2024.4.0** or later
- A free API key from the Belgian Mobility Open Data portal

### Getting your API key

1. Go to [api-management-opendata-production.developer.azure-api.net](https://api-management-opendata-production.developer.azure-api.net)
2. Log in or create a free account
3. Go to your **Profile** page
4. Subscribe to the **Standard** product
5. Copy your primary or secondary key — you will need it during setup

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → click the three-dot menu → **Custom repositories**
3. Add `https://github.com/arszagi/hacs-delijn` with category **Integration**
4. Search for **De Lijn** and install
5. Restart Home Assistant

### Manual

1. Download the latest release from [GitHub](https://github.com/arszagi/hacs-delijn/releases)
2. Copy the `custom_components/delijn/` folder into your HA `custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **De Lijn**
3. Enter your API key
4. Set the refresh interval (default: 30 seconds)
5. Search for a stop (see below) and select it
6. Add as many stops as needed, then finish

### Searching for a stop

You can search either by **name** or by **stop number** — the behaviour differs:

| Search type | Example | Result |
|---|---|---|
| **By name** | `Clovis` | All platforms sharing that name are grouped into one entry — e.g. *Sint-Josse Clovis (2 platforms)*. Selecting it monitors all platforms at once. |
| **By number** | `304660` | Only the specific platform with that number is returned. Useful when you want to monitor a single direction. |

> Stop numbers are printed on the physical stop signs and on the De Lijn website.

### Options (post-installation)

Click **Configure** on the integration card to:
- Add or remove monitored stops
- Change the API key
- Adjust the refresh interval
- Force a schedule data refresh

---

## Entities

For each configured stop, the integration creates a **device** grouping all related sensors.

### Departure sensors

One sensor per bus/tram line and direction at the stop.

| Attribute | Description |
|---|---|
| State | Minutes until next departure |
| `line` | Line number (e.g. `R70`) |
| `headsign` | Destination displayed on the vehicle |
| `realtime_departure` | Real-time departure time (HH:MM) |
| `delay_minutes` | Current delay in minutes (negative = early) |
| `vehicle_id` | Internal vehicle identifier |
| `next_departures` | List of upcoming departures with time and delay |

**Example entity ID:** `sensor.delijn_line_r70_sint_pieters_leeuw_e_ghijsstraat_to_bruxelles_midi`

### Alert sensor

One sensor per stop showing active service disruptions.

| Attribute | Description |
|---|---|
| State | Number of active alerts |
| `alerts` | List of alerts with header, description, URL and expiry time |

**Example entity ID:** `sensor.delijn_alerts_sint_pieters_leeuw_e_ghijsstraat`

---

## Data sources

This integration uses the **Belgian Mobility Open Data** GTFS feeds:

| Feed | Endpoint |
|---|---|
| Static schedule | `/api/gtfs/feed/delijn/static/` |
| Real-time trip updates | `/api/gtfs/feed/delijn/rt/trip-update/` |
| Real-time alerts | `/api/gtfs/feed/delijn/rt/alert/` |

Schedule data (~200 MB) is downloaded once and cached locally. It is refreshed automatically when the remote file changes.

---

## License

This project is licensed under the [MIT License](LICENSE).
