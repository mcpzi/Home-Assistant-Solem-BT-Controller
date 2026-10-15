"""Constants for our integration."""

DOMAIN = "solem_bluetooth_watering_controller"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10
CONTROLLER_MAC_ADDRESS = "controller_mac_address"
NUM_STATIONS = "num_stations"
SPRINKLE_WITH_RAIN = "sprinkle_with_rain"
OPEN_WEATHER_MAP_API_KEY = "open_weather_map_api_key"

CHARACTERISTIC_UUID = "108b0002-eab5-bc09-d0ea-0b8f467ce8ee"
NOTIFY_UUID = "108b0003-eab5-bc09-d0ea-0b8f467ce8ee"
STATUS_COMMIT_PACKET = b"\x3b\x00"
STATUS_RETRY_DELAY = 3        # delay between start-verification BLE attempts
START_VERIFY_MAX_ATTEMPTS = 3

BLUETOOTH_TIMEOUT = "bluetooth_timeout"
BLUETOOTH_MIN_TIMEOUT = 5
BLUETOOTH_DEFAULT_TIMEOUT = 15

# Delay before requesting a status refresh (3b 00) after a start/stop command
STATUS_REFRESH_DELAY = 3

OPEN_WEATHER_MAP_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast?units=metric&"
OPEN_WEATHER_MAP_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather?"
OPEN_WEATHER_MAP_API_CACHE_TIMEOUT = "openweathermap_api_cache_timeout"
OPEN_WEATHER_MAP_API_CACHE_MIN_TIMEOUT = 1
OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT = 5
SOLEM_API_MOCK = "solem_api_mock"