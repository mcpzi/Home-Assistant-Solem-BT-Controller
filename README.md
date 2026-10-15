# Home Assistant Solem Bluetooth Watering Controller Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)

Integrate Solem Bluetooth Watering Controllers (tested on BL-IP) into Home Assistant. Control irrigation per station, monitor rain and water consumption, and automate watering based on weather data.

- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
  - [Switches](#switches)
  - [Sensors](#sensors)
  - [Binary Sensors](#binary-sensors)
  - [Numbers](#numbers)
  - [Buttons](#buttons)
- [Services](#services)
- [FAQ](#faq)

## Installation

Install via HACS as a custom repository, then add the integration in Home Assistant:
**Settings → Devices & Services → Add Integration → Solem Bluetooth Watering Controller**

No other integration or `configuration.yaml` entry is required.

## Configuration

Each physical controller is a separate config entry. The setup flow asks for:

- **Bluetooth device** — discovered automatically from nearby BLE devices
- **Number of stations** — how many valves your controller has
- **Controller location** — a HA zone used for weather lookups (OpenWeatherMap)
- **OpenWeatherMap API key** *(optional)* — enables rain detection and forecasting; create a free key at [openweathermap.org](https://home.openweathermap.org/users/sign_up)
- **Sprinkle even when raining** — if false, active irrigation is stopped automatically when rain is detected
- **Lawn area per station** *(m²)* — used for water consumption calculations

After setup, configure per-station run durations and flow rates from the entity cards or the Options flow.

## Entities

All entities are grouped under a single device per controller.

### Switches

One switch per station. This is the primary way to start and stop irrigation.

| Entity | Description |
|---|---|
| `switch.station_1` … `switch.station_N` | On = start irrigation for this station using its configured duration; off = stop immediately |

Turning a switch on sends a timed start command directly to the BL-IP; the device closes the valve itself when the duration expires. Turning off sends an immediate stop command.

### Sensors

| Entity | Description |
|---|---|
| `sensor.state` | Controller state (On / Off) |
| `sensor.latest_status` | Latest decoded BLE status packet; attributes include active stations, mode, remaining seconds, raw packet hex |
| `sensor.station_N_remaining_time` | Countdown in seconds for station N |
| `sensor.last_sprinkle` | Timestamp of the most recent irrigation |
| `sensor.last_rain` | Timestamp of the most recent detected rain |
| `sensor.total_water_consumption` | Cumulative water use (L), based on flow rate × run time |
| `sensor.total_amount_of_rain_today` | Rain depth measured today (mm) |
| `sensor.total_forecasted_rain_today` | Total rain forecast for today including what has already fallen (mm) |

### Binary Sensors

| Entity | Description |
|---|---|
| `binary_sensor.will_it_rain_today` | True if rain is forecast from now until midnight |
| `binary_sensor.has_rained_today` | True if rain has been detected today |
| `binary_sensor.is_it_raining_now` | True if it is currently raining |

### Numbers

| Entity | Range | Description |
|---|---|---|
| `number.station_N_duration` | 0–59 min | Run duration for station N. 0 = open-ended (60 min command sent to device) |
| `number.water_flow_rate_N` | 1–30 L/min | Water flow rate for station N, used for consumption accounting (use of a water meter is needed) |

### Buttons

| Entity | Description |
|---|---|
| `button.stop_all` | Stop all active irrigation immediately |
| `button.turn_on_controller` | Re-enable the controller |
| `button.turn_off_controller` | Disable the controller |

## Services

All services are available under the `solem_bluetooth_watering_controller` domain.

| Service | Key fields | Description |
|---|---|---|
| `sprinkle_station_x_for_y_minutes` | `device_mac`, `station`, `minutes` | Start one station for N minutes |
| `sprinkle_all_stations_for_y_minutes` | `device_mac`, `minutes` | Start all stations for N minutes each |
| `stop_manual_sprinkle` | `device_mac` | Stop immediately |
| `turn_on` | `device_mac` | Re-enable the controller |
| `turn_off_permanent` | `device_mac` | Permanently disable watering |
| `turn_off_x_days` | `device_mac`, `days` | Suspend watering for N days |
| `run_program_x` | `device_mac`, `program` | Run a stored controller program (1–3) |
| `request_status` | `device_mac` | Read and return the BLE status packet |
| `list_characteristics` | `device_mac` | List GATT services/characteristics (debug) |

## FAQ

### Can I configure other controller models?

Only the BL4-IP has been tested so far.

### Do I still need the Solem Toolkit integration?

No. The BLE layer is now built directly into this integration. You can remove `solem_toolkit` from HACS and any `solem_toolkit:` entry from `configuration.yaml`.

### What does a station duration of 0 mean?

A duration of 0 sends a 60-minute timed command to the BL-IP. The device counts down internally and closes the valve at the end, without requiring HA to send a stop command. Use this when you don't know how long the run will last and use an other sensor in an automation (for example if you want to reach a specific target for soil humidity).
