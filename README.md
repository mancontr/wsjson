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

  history:
    - temperature
    - humidity
    - wind_speed
    - precipitation_rate

  # Optional: today's tides from an IHM port (Spain).
  tides:
    station_id: "<IHM port id>"
    # Optional: mean spring range at that port, in metres. Estimated if omitted.
    spring_range: <metres>
```

## Setup

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mancontr&repository=wsjson&category=integration)

1. Click the button above, or in HACS open the ⋮ menu → **Custom repositories**
   and add `https://github.com/mancontr/wsjson` as an **Integration**.
2. Download **Weather Station JSON** in HACS.
3. Add the config above to your `configuration.yaml` and restart Home Assistant.

To install manually instead, put this repo's folder at `config/custom_components/wsjson`.
