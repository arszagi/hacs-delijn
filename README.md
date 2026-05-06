# De Lijn — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Version](https://img.shields.io/badge/version-1.2.5-blue.svg)](https://github.com/arszagi/hacs-delijn/releases)
[![HACS Action](https://github.com/arszagi/hacs-delijn/actions/workflows/hacs.yml/badge.svg)](https://github.com/arszagi/hacs-delijn/actions/workflows/hacs.yml)
[![Hassfest](https://github.com/arszagi/hacs-delijn/actions/workflows/hassfest.yml/badge.svg)](https://github.com/arszagi/hacs-delijn/actions/workflows/hassfest.yml)

A Home Assistant custom integration for **De Lijn** — the public transport operator for buses and trams in Flanders, Belgium.

Provides real-time departure times, delays, service alerts and line badge colors for any De Lijn stop, directly in your Home Assistant dashboard.

---

## Features

- **Real-time departures** — live times and delays via the De Lijn V1 Core API
- **Scheduled times** — always shown even when no real-time data is available
- **Per-line sensors** — one sensor per bus/tram line per stop, grouped by stop as a HA device
- **Line badge colors** — background, text, border and text-border colors for each line (for custom Lovelace cards)
- **Bilingual destinations** — display stop destinations in Dutch or French
- **Service alerts** — active disruptions and diversions per stop
- **Temporary stop detection** — warns during installation if a stop is temporary (TIJDELIJK) or on-demand (FLEX)
- **Multi-stop support** — monitor as many stops as you need
- **Configurable refresh interval** — default 30 seconds, minimum 30 seconds
- **Full options flow** — add/remove stops and change settings without reinstalling

---

## Requirements

- Home Assistant **2024.4.0** or later
- A free API key from the De Lijn Open Data portal

### Getting your API key

1. Go to [portal.delijn.be](https://portal.delijn.be)
2. Log in or create a free account
3. Go to **Products** and choose **Open Data Free — Subscribe Here**
4. In **Your subscriptions**, enter a name of your choice (e.g. `HASSIO`) and click **Subscribe**
5. Go to the **Profile** tab — you will find your **Primary key** and **Secondary key**
6. Either key works — copy one and use it during setup

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
5. Choose your display language: **Nederlands** or **Français**
6. Search for a stop and select it
7. Add as many stops as needed, then finish

### Searching for a stop

You can search either by **name** or by **stop number** — the behaviour differs:

| Search type | Example | Result |
|---|---|---|
| **By name** | `E. Ghijsstraat` | All platforms sharing that name are grouped — e.g. *Sint-Pieters-Leeuw E. Ghijsstraat (304660, 304661, 354661)*. Selecting it monitors all platforms at once. |
| **By number** | `304660` | Only that specific platform is returned — e.g. *Sint-Pieters-Leeuw E. Ghijsstraat (304660)*. Useful to monitor a single direction. |

> Stop numbers are printed on the physical stop signs and on the De Lijn website.

### Stop types

De Lijn classifies each stop with one of four types:

| Type | NL | FR | Meaning |
|---|---|---|---|
| `REGULIER` | Regulier | Régulier | Standard stop with fixed schedules |
| `TIJDELIJK` ⚠️ | Tijdelijk | Temporaire | Temporary stop (e.g. due to roadworks) — may be removed when works end |
| `FLEX` ℹ️ | Flexbus | Flexbus (à la demande) | On-demand stop — reservation required via **015 40 88 88** or the **De Lijn Flex** app. No real-time data available. |
| `COMBI` | Regulier + Flexbus | Régulier + Flexbus | Mixed stop — served by both regular buses and a Flexbus |

A warning is shown during installation for `TIJDELIJK` and `FLEX` stops. The stop type is also visible as an attribute on every sensor.

### Options (post-installation)

Click **Configure** on the integration card to:
- Add or remove monitored stops
- Change the API key
- Change the display language (NL / FR)
- Adjust the refresh interval
- Force a stop data refresh

---

## Entities

For each configured stop, the integration creates a **device** grouping all related sensors. The device model reflects the stop type in the configured language.

### Departure sensors

One sensor per bus/tram line and direction at the stop.

| Attribute | Description |
|---|---|
| State | Minutes until next departure (unavailable = no service) |
| `line` | Public line number (e.g. `R70`) |
| `direction` | Direction (`HEEN` or `TERUG`) |
| `destination` | Destination in the configured language |
| `destination_fr` | Destination in French (always stored) |
| `scheduled` | Scheduled departure time (HH:MM) |
| `realtime` | Real-time departure time (HH:MM) when available |
| `delay_minutes` | Delay in minutes (negative = early) |
| `prediction` | `REALTIME` — live data · `GEENREALTIME` — scheduled timetable (no live data) · `GESCHRAPT` — cancelled · `VERSTREKEN` — passed |
| `vehicle_id` | Vehicle number |
| `next_departures` | List of upcoming departures (scheduled, realtime, delay, cancelled) |
| `badge_background` | Badge background color hex (e.g. `#BBDD00`) |
| `badge_text` | Badge text color hex (e.g. `#000000`) |
| `badge_border` | Badge border color hex |
| `badge_text_border` | Badge text outline color hex |
| `stop_number` | Stop number (e.g. `354661`) |
| `stop_type` | Stop type in the configured language (Regulier, Tijdelijk, Flexbus, Regulier + Flexbus) |

**Timetable fallback** — when real-time data is unavailable (service outage, night hours), the sensor automatically falls back to the scheduled timetable. Times are still shown and the state still indicates minutes until the next departure. The `prediction` attribute will be `GEENREALTIME` to indicate the data is scheduled, not live. `realtime` and `delay_minutes` will be empty.

**Example entity ID:** `sensor.delijn_line_r70_sint_pieters_leeuw_e_ghijsstraat_to_brussel_zuid`

### Alert sensor

One sensor per stop showing active disruptions and diversions.

| Attribute | Description |
|---|---|
| State | Number of active alerts |
| `alerts` | List of alerts with type, title, description, start/end date and affected lines |
| `stop_type` | Stop classification |

**Example entity ID:** `sensor.delijn_alerts_sint_pieters_leeuw_e_ghijsstraat_354661`

---

## Data source

This integration uses the **De Lijn Open Data V1 Core API** (`api.delijn.be`):

| Endpoint | Used for |
|---|---|
| `/entiteiten/{id}/haltes` | Stop list download and search (cached locally) |
| `/haltes/{e}/{h}/real-time` | Real-time departures per stop |
| `/haltes/{e}/{h}/storingen` | Disruptions and diversions per stop |
| `/lijnen/{e}/{lijn}/lijnkleuren` | Badge colors per line |
| `/kleuren/{code}` | Color code to hex conversion |

Stop data (~30K stops, ~17 MB) is downloaded once at installation and cached locally. It can be refreshed manually from the integration options.

---

## License

This project is licensed under the [MIT License](LICENSE).
