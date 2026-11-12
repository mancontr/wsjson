# WSJSON

This integration serves your Weather Station data in a unauthenticated endpoint
in JSON format, ready to use in your own meteo page.

## Config

You need something like this at your `configuration.yaml`:

```yaml
wsjson:
  entities:
    temperature: sensor.gw2000a_outdoor_temperature
    dewpoint: sensor.gw2000a_dewpoint
    humidity: sensor.gw2000a_humidity
    wind_speed: sensor.gw2000a_wind_speed
    wind_gust: sensor.gw2000a_wind_gust
    wind_bearing: sensor.gw2000a_wind_direction
    precipitation_rate: sensor.gw2000a_rain_rate_piezo
    precipitation_today: sensor.gw2000a_daily_rain_piezo
    pressure: sensor.gw2000a_relative_pressure
    uv: sensor.gw2000a_uv_index
    solar_radiation: sensor.gw2000a_solar_radiation

  # Optional: today's tides from an IHM port (Spain).
  tides:
    station_id: "<IHM port id>"
    # Optional: mean spring range at that port, in metres. Estimated if omitted.
    spring_range: <metres>
```

The keys under `entities` are fixed (the ones above); include the ones your
station has. Every key except `wind_bearing` also gets history and daily
summaries, taken from Home Assistant's statistics, so those sensors need a
`state_class`.

## Endpoints

All are public, read-only and cacheable.

| Path | What | Refreshed |
| --- | --- | --- |
| `/api/wsjson/data.json` | Current values, sun, moon, tides | Live |
| `/api/wsjson/history.json` | The last 24 hours, in 5-minute buckets | Every 5 minutes |
| `/api/wsjson/daily.json` | Which days and years the daily archive covers | Daily, after midnight |
| `/api/wsjson/daily/<year>.json` | One year of daily summaries | Daily, after midnight |

### Series

History and the yearly files share one shape: a start, an interval, units,
and one array per field, one value per bucket, `null` where there is no data.

```json
{
  "start": "2026-01-01",
  "interval": "P1D",
  "units": {"temperature": "°C", "precipitation_today": "mm"},
  "temperature": {"min": [6.1, 5.9, null], "mean": [9.8, 9.1, null], "max": [14.2, 13.8, null]},
  "precipitation_today": {"change": [0.0, 12.4, null]}
}
```

- History uses `"interval": "PT5M"`; the yearly files use `"P1D"` (local days).
- Measurements get `min`, `mean` and `max`. Counters (`total_increasing`
  sensors, typically rain) get `change` instead: the amount accumulated in that
  bucket.

The daily archive is built from Home Assistant's hourly statistics, which are
never purged. On first start it backfills as far back as they go; after that
it adds each finished day shortly after midnight. It is kept in its own file,
so it survives replacing the station or wiping the recorder database.

## Setup

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mancontr&repository=wsjson&category=integration)

1. Click the button above, or in HACS open the ⋮ menu → **Custom repositories**
   and add `https://github.com/mancontr/wsjson` as an **Integration**.
2. Download **Weather Station JSON** in HACS.
3. Add the config above to your `configuration.yaml` and restart Home Assistant.

To install manually instead, put this repo's folder at `config/custom_components/wsjson`.
