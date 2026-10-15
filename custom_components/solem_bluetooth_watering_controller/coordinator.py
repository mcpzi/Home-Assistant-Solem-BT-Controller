"""DataUpdateCoordinator for our integration."""

from datetime import datetime, timedelta
from homeassistant.util import dt as dt_util
import logging
import asyncio
from asyncio import sleep

from typing import Any
from homeassistant.helpers.storage import Store

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_SENSORS,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import DOMAIN, HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_change

from .util import mac_to_uuid, ensure_datetime, ensure_aware
from .models import IrrigationController, IrrigationStation
from .api import SolemAPI, OpenWeatherMapAPI, APIConnectionError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    CONTROLLER_MAC_ADDRESS,
    NUM_STATIONS,
    OPEN_WEATHER_MAP_API_KEY,
    SPRINKLE_WITH_RAIN,
    BLUETOOTH_TIMEOUT,
    BLUETOOTH_MIN_TIMEOUT,
    BLUETOOTH_DEFAULT_TIMEOUT,
    OPEN_WEATHER_MAP_API_CACHE_TIMEOUT,
    OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT,
    SOLEM_API_MOCK,
    STATUS_REFRESH_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class SolemCoordinator(DataUpdateCoordinator):
    """Solem coordinator."""

    data: list[dict[str, Any]]

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Set variables from values entered in config flow setup
        self.controller_mac_address = config_entry.data[CONTROLLER_MAC_ADDRESS].rsplit(' - ', 1)[1]

        _LOGGER.info(f"{self.controller_mac_address} - Starting Coordinator initialization...")
        
        self.sprinkle_with_rain = config_entry.data[SPRINKLE_WITH_RAIN] == "true"
        self.openweathermap_api_key = config_entry.data.get(OPEN_WEATHER_MAP_API_KEY)
        zone_entity_id = config_entry.data[CONF_SENSORS]
        zone_state = hass.states.get(zone_entity_id)

        if zone_state:
            self.latitude = zone_state.attributes.get("latitude")
            self.longitude = zone_state.attributes.get("longitude")

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.bluetooth_timeout = config_entry.options.get(
            BLUETOOTH_TIMEOUT, BLUETOOTH_DEFAULT_TIMEOUT
        )
        self.openweathermap_api_timeout = config_entry.options.get(
            OPEN_WEATHER_MAP_API_CACHE_TIMEOUT, OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT
        )
        self.solem_api_mock = config_entry.options.get(SOLEM_API_MOCK, "false") == "true"

        # Initialise DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            # Method to call on every update interval.
            update_method=self.async_update_data,
            # Polling interval. Will only be polled if you have made your
            # platform entities, CoordinatorEntities.
            # Using config option here but you can just use a fixed value.
            update_interval=timedelta(seconds=self.poll_interval),
        )

        self.num_stations = config_entry.data.get("num_stations", 2)
        self.station_areas = config_entry.data.get("station_areas", [0] * self.num_stations)
        if not isinstance(self.station_areas, list) or len(self.station_areas) != self.num_stations:
            _LOGGER.warning(f"{self.controller_mac_address} - station_areas missing or invalid, setting defaults.")
            self.station_areas = [0] * self.num_stations
            
        self.config_entry = config_entry

        # Create instances of devices
        self.controller = IrrigationController(
            device_id=f"{self.controller_mac_address}_irrigation_controller_status",
            device_name="Controller Status",
            device_uid="",
            software_version="1.0",
            icon = "mdi:state-machine"
        )

        self.stations = [
            IrrigationStation(
                device_id=f"{self.controller_mac_address}_irrigation_station_{station_id}_status",
                device_name=f"Station {station_id} Status",
                device_uid="",
                station_number=station_id,
                software_version="1.0",
                icon = "mdi:state-machine"
            )
            for station_id in range(1, self.num_stations + 1)
        ]
        
        self.api = SolemAPI(self.hass, mac_address=self.controller_mac_address, bluetooth_timeout=self.bluetooth_timeout)
        if self.openweathermap_api_key:
            self.weather_api = OpenWeatherMapAPI(
                self.openweathermap_api_key,
                self.latitude,
                self.longitude,
                self.openweathermap_api_timeout,
            )
        else:
            self.weather_api = None

        self.storage = Store(hass, 1, f"irrigation_{config_entry.unique_id}")
        self.station_stop_events: list[asyncio.Event] = [asyncio.Event() for _ in range(self.num_stations)]
        self._last_status: dict | None = None
        self.station_timer_end: list[datetime | None] = [None] * self.num_stations
        self.active_stations: list[int] = []
        self.ble_connection_status: str = "idle"  # "idle" | "connecting" | "error"

        # ---- Default init for attributes to avoid race conditions ----
        self.last_reset = dt_util.now()
        self.last_rain = dt_util.now()
        self.last_sprinkle = dt_util.now()
        
        self.will_it_rain_today = False
        self.will_it_rain_today_forecast = []
        self.has_rained_today = False
        self.is_raining_now = False
        self.is_raining_now_json = {}
        
        self.rain_total_amount_today = 0
        self.rain_total_amount_forecasted_today = 0
        self.total_water_consumption = 0

        self.station_durations = [10] * self.num_stations
        self.water_flow_rate = [12] * self.num_stations
        
        self.init_task = hass.async_create_task(self.async_init())
    
        _LOGGER.info(f"{self.controller_mac_address} - Coordinator initialization finished!")


    async def update_config(self, new_config: ConfigEntry):
        """Update the coordinator with new configuration."""
        
        _LOGGER.info(f"{self.controller_mac_address} - Updating Coordinator with new config...")
        self.config_entry = new_config  # Atualizar as configurações internas
    
        self.controller_mac_address = self.config_entry.data[CONTROLLER_MAC_ADDRESS].rsplit(' - ', 1)[1]
        self.sprinkle_with_rain = self.config_entry.data.get(SPRINKLE_WITH_RAIN, "False") == "true"
        self.openweathermap_api_key = self.config_entry.data.get(OPEN_WEATHER_MAP_API_KEY)
        zone_entity_id = self.config_entry.data[CONF_SENSORS]
        zone_state = self.hass.states.get(zone_entity_id)
        
        if zone_state:
            self.latitude = zone_state.attributes.get("latitude")
            self.longitude = zone_state.attributes.get("longitude")

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.bluetooth_timeout = self.config_entry.options.get(
            BLUETOOTH_TIMEOUT, BLUETOOTH_DEFAULT_TIMEOUT
        )
        self.openweathermap_api_timeout = self.config_entry.options.get(
            OPEN_WEATHER_MAP_API_CACHE_TIMEOUT, OPEN_WEATHER_MAP_API_CACHE_DEFAULT_TIMEOUT
        )
        self.solem_api_mock = self.config_entry.options.get(SOLEM_API_MOCK, "false") == "true"

        self.api = SolemAPI(self.hass, mac_address=self.controller_mac_address, bluetooth_timeout=self.bluetooth_timeout)
        if self.openweathermap_api_key:
            self.weather_api = OpenWeatherMapAPI(
                self.openweathermap_api_key,
                self.latitude,
                self.longitude,
                self.openweathermap_api_timeout,
            )
        else:
            self.weather_api = None

        self.num_stations = self.config_entry.data.get("num_stations", 2)
        self.station_areas = self.config_entry.data.get("station_areas", [0] * self.num_stations)
        if not isinstance(self.station_areas, list) or len(self.station_areas) != self.num_stations:
            _LOGGER.warning(f"{self.controller_mac_address} - station_areas missing or invalid on update, setting defaults.")
            self.station_areas = [0] * self.num_stations

        self.stations = [
            IrrigationStation(
                device_id=f"{self.controller_mac_address}_irrigation_station_{station_id}_status",
                device_name=f"Station {station_id} Status",
                device_uid="",
                station_number=station_id,
                software_version="1.0",
                icon = "mdi:state-machine"
            )
            for station_id in range(1, self.num_stations + 1)
        ]
        self.station_timer_end = [None] * self.num_stations
        self.active_stations = []
        self.station_stop_events = [asyncio.Event() for _ in range(self.num_stations)]
        self.ble_connection_status = "idle"
        await self.async_request_refresh()
        _LOGGER.info(f"{self.controller_mac_address} - Updated Coordinator with new config.")

    async def load_persistent_data(self):
        """Load persistent data from storage"""
        storage_data = await self.storage.async_load()

        if storage_data:
            self.will_it_rain_today = storage_data.get("will_it_rain_today")
            self.will_it_rain_today_forecast = storage_data.get("will_it_rain_today_forecast") or []
            if self.weather_api:
                self.weather_api._cache_forecast = self.will_it_rain_today_forecast
            self.has_rained_today = storage_data.get("has_rained_today")
            self.is_raining_now = storage_data.get("is_raining_now")
            self.is_raining_now_json = storage_data.get("is_raining_now_json") or {}
            if self.weather_api:
                self.weather_api._cache_current = self.is_raining_now_json
            # Migrate old global duration to per-station list
            if "station_durations" in storage_data:
                self.station_durations = storage_data["station_durations"]
                if not isinstance(self.station_durations, list) or len(self.station_durations) != self.num_stations:
                    self.station_durations = [10] * self.num_stations
            else:
                old = storage_data.get("irrigation_manual_duration", 10) or 10
                self.station_durations = [int(old)] * self.num_stations
            self.rain_total_amount_today = storage_data.get("rain_total_amount_today", 0)
            self.rain_total_amount_forecasted_today = storage_data.get("rain_total_amount_forecasted_today", 0)
            self.total_water_consumption = storage_data.get("total_water_consumption", 0)

            self.water_flow_rate = storage_data.get("water_flow_rate")
            if not isinstance(self.water_flow_rate, list) or len(self.water_flow_rate) != self.num_stations:
                _LOGGER.debug(f"{self.controller_mac_address} - Initializing water_flow_rate with default values.")
                self.water_flow_rate = [12] * self.num_stations

            last_reset = storage_data.get("last_reset")
            if isinstance(last_reset, str):
                try:
                    self.last_reset = datetime.strptime(last_reset, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_reset: {last_reset}")
                    self.last_reset = dt_util.now()
            else:
                self.last_reset = last_reset or dt_util.now()

            last_rain = storage_data.get("last_rain")
            if isinstance(last_rain, str):
                try:
                    self.last_rain = datetime.strptime(last_rain, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_rain: {last_rain}")
                    self.last_rain = dt_util.now()
            else:
                self.last_rain = last_rain or dt_util.now()

            last_sprinkle = storage_data.get("last_sprinkle")
            if isinstance(last_sprinkle, str):
                try:
                    self.last_sprinkle = datetime.strptime(last_sprinkle, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    _LOGGER.error(f"{self.controller_mac_address} - Invalid date format for last_sprinkle: {last_sprinkle}")
                    self.last_sprinkle = dt_util.now()
            else:
                self.last_sprinkle = last_sprinkle or dt_util.now()

            # Normalize all datetimes to be aware
            self.last_reset = ensure_aware(self.last_reset)
            self.last_rain = ensure_aware(self.last_rain)
            self.last_sprinkle = ensure_aware(self.last_sprinkle)

        else:
            self.will_it_rain_today = False
            self.will_it_rain_today_forecast = []
            self.has_rained_today = False
            self.is_raining_now = False
            self.is_raining_now_json = []
            self.last_reset = dt_util.now()
            self.last_sprinkle = dt_util.now()
            self.last_rain = dt_util.now()
            self.rain_time_today = 0
            self.rain_total_amount_today = 0
            self.rain_total_amount_forecasted_today = 0
            self.total_water_consumption = 0
            self.station_durations = [10] * self.num_stations
            self.water_flow_rate = [12] * self.num_stations

        _LOGGER.info(f"{self.controller_mac_address} - Persistent data loaded.")

    async def save_persistent_data(self):
        """Save persistent data on storage."""

        if isinstance(self.last_reset, str):
            self.last_reset = datetime.fromisoformat(self.last_reset)
        if isinstance(self.last_rain, str):
            self.last_rain = datetime.fromisoformat(self.last_rain)
        if isinstance(self.last_sprinkle, str):
            self.last_sprinkle = datetime.fromisoformat(self.last_sprinkle)
        
        storage_data = {
            "will_it_rain_today": self.will_it_rain_today,
            "will_it_rain_today_forecast": self.will_it_rain_today_forecast,
            "has_rained_today": self.has_rained_today,
            "is_raining_now": self.is_raining_now,
            "is_raining_now_json": self.is_raining_now_json,
            "last_reset": ensure_aware(self.last_reset).strftime("%Y-%m-%d %H:%M:%S"),
            "last_sprinkle": ensure_aware(self.last_sprinkle or datetime.min).strftime("%Y-%m-%d %H:%M:%S"),
            "last_rain": ensure_aware(self.last_rain or datetime.min).strftime("%Y-%m-%d %H:%M:%S"),
            "station_durations": self.station_durations,
            "water_flow_rate": self.water_flow_rate,
            "rain_total_amount_today": self.rain_total_amount_today,
            "rain_total_amount_forecasted_today": self.rain_total_amount_forecasted_today,
            "total_water_consumption": self.total_water_consumption,
        }
    
        await self.storage.async_save(storage_data)
        _LOGGER.debug(f"{self.controller_mac_address} - Persistent data saved.")

    async def setup_scheduled_tasks(self):
        """Create scheduled tasks."""
        async_track_time_change(
            self.hass,
            lambda *_: self.hass.create_task(self.reset_rain_sprinkle_indicators()),
            hour=0, minute=0, second=0
        )

    async def async_init(self):
        await self.load_persistent_data()
        
        """Init APIs and schedule tasks."""

        _LOGGER.info(f"{self.controller_mac_address} - Connecting to Solem API...")
        try:
            self.api.mock = self.solem_api_mock
            await self.api.connect()
            _LOGGER.info(f"{self.controller_mac_address} - Connected to Solem API")
        except Exception as ex:
            _LOGGER.warning(f"{self.controller_mac_address} - Failed connecting to Solem device ({self.controller_mac_address})!, ex={ex}")

        await self.setup_scheduled_tasks()
        self.data = await self.async_update_all_sensors()

    async def reset_rain_sprinkle_indicators(self, *_):
        """Reset raind indicators."""
        self.has_rained_today = False
        self.will_it_rain_today = False
        self.rain_total_amount_today = 0
        if self.weather_api:
            self.rain_total_amount_forecasted_today = await self.weather_api.get_total_rain_forecast_for_today()
        else:
            self.rain_total_amount_forecasted_today = 0
        self.last_reset = dt_util.now()
        
        
        _LOGGER.info(f"{self.controller_mac_address} - Resetted rain and sprinkle indicators.")
        
        await self.save_persistent_data()

    async def async_update_all_sensors(self):
        _LOGGER.debug(f"{self.controller_mac_address} - Updating all sensors...")

        if not hasattr(self, 'rain_total_amount_today') or self.rain_total_amount_today is None:
            self.rain_total_amount_today = 0
        if not hasattr(self, 'rain_total_amount_forecasted_today') or self.rain_total_amount_forecasted_today is None:
            self.rain_total_amount_forecasted_today = 0
        if not hasattr(self, 'last_reset'):
            self.last_reset = None
        
        # Verifies if it's after 00:05:00
        now = dt_util.now()
        if now.time() > datetime.strptime("00:05:00", "%H:%M:%S").time():
            try:
                self.last_reset = ensure_datetime(self.last_reset)
                if self.last_reset.date() != now.date():
                    _LOGGER.info(f"{self.controller_mac_address} - Last reset was on {self.last_reset.date()}, performing daily reset.")
                    await self.reset_rain_sprinkle_indicators()
            except Exception as e:
                _LOGGER.warning(f"{self.controller_mac_address} - Could not determine last_reset date. Skipping reset. Error: {e}")
                
        data = []
        
        counter = 1
        station_duration_counter = 601
        water_flow_counter = 701
        stations_counter = 801
        switch_counter = 1101
        station_timer_counter = 1001
        
        if self.weather_api:
            will_it_rain_result = await self.weather_api.will_it_rain()
            self.will_it_rain_today = will_it_rain_result.get("will_rain", False)
            self.will_it_rain_today_forecast = will_it_rain_result.get("forecast", [])
            is_raining_result = await self.weather_api.is_raining()
            self.is_raining_now = is_raining_result["is_raining"]
            self.is_raining_now_json = is_raining_result["current"]
        else:
            self.will_it_rain_today = False
            self.will_it_rain_today_forecast = []
            self.is_raining_now = False
            self.is_raining_now_json = {}

        if self.is_raining_now:
            self.has_rained_today = True
            self.last_rain = dt_util.now()
            self.rain_total_amount_today += await self.calculate_rain_amount()

            if not self.sprinkle_with_rain:
                for station_id in range(1, self.num_stations + 1):
                    if self.stations[station_id - 1].state == "Sprinkling":
                        await self.stop_irrigation()
                        break
        
        if self.weather_api:
            self.rain_total_amount_forecasted_today = (
                await self.weather_api.get_total_rain_forecast_for_today()
            ) + self.rain_total_amount_today
        else:
            self.rain_total_amount_forecasted_today = self.rain_total_amount_today

        # Controller
        data.append({
            "device_id": self.controller.device_id,
            "device_type": "STATE_SENSOR",
            "device_name": self.controller.device_name,
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": self.controller.software_version,
            "state": self.controller.state,
            "icon": self.controller.icon,
            "last_reboot": self.controller.last_reboot,
        })
        counter += 1
    
        # Stations
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": self.stations[station_id - 1].device_id,
                "device_type": "STATE_SENSOR",
                "device_name": self.stations[station_id - 1].device_name,
                "device_uid": mac_to_uuid(self.controller_mac_address, stations_counter),
                "software_version": self.stations[station_id - 1].software_version,
                "state": self.stations[station_id - 1].state,
                "icon": self.stations[station_id - 1].icon,
                "last_reboot": self.stations[station_id - 1].last_reboot,
            })
            stations_counter += 1

        counter += 1  # slot 2 reserved (formerly IRRIGATION_DURATION_NUMBER)

        # Per-station run duration numbers
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_station_duration_{station_id}",
                "device_type": "STATION_DURATION_NUMBER",
                "device_name": f"Station {station_id} Duration",
                "device_uid": mac_to_uuid(self.controller_mac_address, station_duration_counter),
                "software_version": "1.0",
                "value": self.station_durations[station_id - 1],
                "icon": "mdi:clock-time-five-outline",
                "last_reboot": None,
            })
            station_duration_counter += 1

        # Water flow rate numbers
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_water_flow_rate_{station_id}",
                "device_type": "WATER_FLOW_NUMBER",
                "device_name": f"Water Flow Rate {station_id}",
                "device_uid": mac_to_uuid(self.controller_mac_address, water_flow_counter),
                "software_version": "1.0",
                "value": self.water_flow_rate[station_id - 1],
                "icon": "mdi:water-pump",
                "last_reboot": None,
            })
            water_flow_counter += 1

        # Per-station switches
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_station_switch_{station_id}",
                "device_type": "STATION_SWITCH",
                "device_name": f"Station {station_id}",
                "device_uid": mac_to_uuid(self.controller_mac_address, switch_counter),
                "software_version": "1.0",
                "state": station_id in self.active_stations,
                "icon": "mdi:sprinkler",
                "last_reboot": None,
            })
            switch_counter += 1
        
        # Remaining sprinkling time (seconds) per station
        for station_id in range(1, self.num_stations + 1):
            data.append({
                "device_id": f"{self.controller_mac_address}_station_remaining_time_{station_id}",
                "device_type": "STATION_REMAINING_TIME_SENSOR",
                "device_name": f"Station {station_id} Remaining Time",
                "device_uid": mac_to_uuid(self.controller_mac_address, station_timer_counter),
                "software_version": "1.0",
                "state": self.get_station_remaining_seconds(station_id),
                "icon": "mdi:timer-sand",
                "last_reboot": None,
            })
            station_timer_counter += 1

        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_stop",
            "device_type": "STOP_BUTTON",
            "device_name": f"Stop sprinkle",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:water-off",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_controller_on",
            "device_type": "ON_BUTTON",
            "device_name": f"Turn on controller",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:power-on",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_irrigation_controller_off",
            "device_type": "OFF_BUTTON",
            "device_name": f"Turn off controller",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "icon": "mdi:power-off",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_will_rain_today",
            "device_type": "WILL_RAIN_SENSOR",
            "device_name": f"Will it rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.will_it_rain_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_has_rained_today",
            "device_type": "HAS_RAINED_SENSOR",
            "device_name": f"Has rained today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.has_rained_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_is_raining_now",
            "device_type": "IS_RAINING_SENSOR",
            "device_name": f"Is it raining now",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.is_raining_now,
            "icon": "mdi:weather-pouring",
            "last_reboot": None,
        })
        counter += 1
        counter += 1  # slot 9 reserved (formerly NEXT_SCHEDULE_SENSOR)
        data.append({
            "device_id": f"{self.controller_mac_address}_last_sprinkle",
            "device_type": "LAST_SPRINKLE_SENSOR",
            "device_name": f"Last sprinkle",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.last_sprinkle,
            "icon": "mdi:sprinkler",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_last_rain",
            "device_type": "LAST_RAIN_SENSOR",
            "device_name": f"Last rain",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.last_rain,
            "icon": "mdi:weather-pouring",
            "last_reboot": None,
        })
        counter += 1
        counter += 1  # slot 12 reserved (formerly RAIN_TIME_TODAY_SENSOR)
        data.append({
            "device_id": f"{self.controller_mac_address}_total_water_consumption",
            "device_type": "TOTAL_WATER_CONSUMPTION_SENSOR",
            "device_name": f"Total water consumption",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.total_water_consumption,
            "icon": "mdi:water-pump",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_total_amount_rain_today",
            "device_type": "TOTAL_AMOUNT_RAIN_TODAY",
            "device_name": f"Total amount of rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.rain_total_amount_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1
        data.append({
            "device_id": f"{self.controller_mac_address}_total_forecasted_rain_today",
            "device_type": "TOTAL_FORECASTED_RAIN_TODAY",
            "device_name": f"Total forecasted rain today",
            "device_uid": mac_to_uuid(self.controller_mac_address, counter),
            "software_version": "1.0",
            "state": self.rain_total_amount_forecasted_today,
            "icon": "mdi:weather-rainy",
            "last_reboot": None,
        })
        counter += 1

        # Save persistent data
        await self.save_persistent_data()
        _LOGGER.debug(f"{self.controller_mac_address} - Updated sensors.")
        return data

    async def async_update_data(self):
        data = []

        try:
            data = await self.async_update_all_sensors()
        except Exception as err:
            # This will show entities as unavailable by raising UpdateFailed exception
            _LOGGER.error(f"{self.controller_mac_address} - Error: {err}", exc_info=True)

        # What is returned here is stored in self.data by the DataUpdateCoordinator
        return data


    async def calculate_rain_amount(self) -> float:
        if "rain" not in self.is_raining_now_json:
            return 0.0  # No rain
    
        rain_data = self.is_raining_now_json["rain"]
        
        for key in rain_data:
            if key.endswith("h") and key[:-1].isdigit():
                hours = int(key[:-1])  # Extract the number of hours
                rain_amount = rain_data[key]  # Amount of water during this period
                minutes = hours * 60  # Convert from hours to minutes
                return (rain_amount / minutes) * (self.poll_interval / 60)  # Returns the amount of water for the polling frequency

        return 0.0  # In case there are no recognizable keys

    async def start_irrigation(self, station: int, minutes: int | None = None):
        if minutes is not None:
            duration = int(minutes)
        else:
            raw = self.station_durations[station - 1]
            duration = 60 if raw == 0 else int(raw)
        _LOGGER.info(f"{self.controller_mac_address} - Going to start watering on station {station} for {duration} minutes...")

        # Signal any running background task to stop, then send a lightweight BLE
        # stop command.  We deliberately skip the post-stop status refresh and
        # full sensor update here; those run after the new station starts.
        if self.active_stations:
            _LOGGER.info(
                f"{self.controller_mac_address} - Stopping active station(s) {self.active_stations} "
                f"before starting station {station}."
            )
            for event in self.station_stop_events:
                event.set()
            for _sid in range(1, self.num_stations + 1):
                self.stations[_sid - 1].state = "Stopped"
            self.station_timer_end = [None] * self.num_stations
            self.active_stations = []
            try:
                await self.api.stop_manual_sprinkle()
                _LOGGER.debug(f"{self.controller_mac_address} - Pre-stop BLE command sent.")
            except APIConnectionError:
                _LOGGER.warning(f"{self.controller_mac_address} - Pre-stop BLE command failed, proceeding with start.")

        self.station_stop_events[station - 1].clear()
        self.ble_connection_status = "connecting"
        self.async_update_listeners()
        try:
            await self.api.sprinkle_station_x_for_y_minutes(station, duration)
            _LOGGER.debug(f"{self.controller_mac_address} - Irrigation start command sent for station {station}.")
        except APIConnectionError:
            # The BLE write may have reached the device even if the internal
            # start-verification loop timed out.  Fall through to the
            # post-start status read, which is the authoritative decision point.
            _LOGGER.warning(
                f"{self.controller_mac_address} - Start command for station {station} raised an error; "
                "will verify via post-start status read before deciding success or failure."
            )

        # BLE command phase is done (success or error). Stop showing "Connecting..."
        # before the verification delay so the UI stays responsive.
        self.ble_connection_status = "idle"
        self.async_update_listeners()

        # If stop was requested while the start command was in flight, do not run the
        # post-start verify — it would repopulate active_stations after stop_irrigation
        # has already cleared it, making the sensor show "Station X active" after a stop.
        if self.station_stop_events[station - 1].is_set():
            _LOGGER.info(
                f"{self.controller_mac_address} - Stop requested during start sequence for station {station}; "
                "aborting post-start verify."
            )
            self.stations[station - 1].state = "Stopped"
            return

        self.stations[station - 1].state = "Sprinkling"

        await sleep(STATUS_REFRESH_DELAY)
        _LOGGER.debug(f"{self.controller_mac_address} - Sending delayed status refresh (3b 00)...")
        await self.async_request_status_ble(station_hint=station)

        if not self.active_stations:
            _LOGGER.error(
                f"{self.controller_mac_address} - Station {station} not confirmed active after start command; aborting."
            )
            self.stations[station - 1].state = "Stopped"
            self.ble_connection_status = "error"
            self.async_update_listeners()
            return

        # Device confirmed active. If the status packet did not include remaining_seconds
        # (bytes 13-14 appear to be 0 for stations 2+ on some firmware), fall back to the
        # commanded duration so the countdown timer is still functional.
        if self.station_timer_end[station - 1] is None:
            self.station_timer_end[station - 1] = dt_util.now() + timedelta(seconds=duration * 60)
            _LOGGER.debug(
                f"{self.controller_mac_address} - BLE status had no remaining_seconds for station {station}; "
                f"using commanded duration ({duration} min) as timer fallback."
            )

        self.ble_connection_status = "idle"

        data = await self.async_update_all_sensors()
        if data is not None:  # Update only if data is valid
            self.async_set_updated_data(data)
        else:
            _LOGGER.warning(f"{self.controller_mac_address} - async_update_all_sensors() returned None, skipping update.")

        for _ in range(duration * 60):
            # Verify if exit condition is met
            if self.station_stop_events[station - 1].is_set():
                _LOGGER.info(f"{self.controller_mac_address} - Irrigation cancelation triggered.")
                break
            await sleep(1)  # Validate every second
            self.total_water_consumption += (self.water_flow_rate[station - 1] / 60)

        else:  # Só entra aqui se o loop terminar normalmente (sem interrupção)
            self.stations[station - 1].state = "Stopped"
            self.station_timer_end[station - 1] = None
            if station in self.active_stations:
                self.active_stations.remove(station)
            _LOGGER.info(f"{self.controller_mac_address} - Finished watering on station {station}.")
        
        now = dt_util.now()
        self.last_sprinkle = now
    
        data = await self.async_update_all_sensors()
        if data is not None:  # Update only if data is valid
            self.async_set_updated_data(data)
        else:
            _LOGGER.warning(f"{self.controller_mac_address} - async_update_all_sensors() returned None, skipping update.")

    async def stop_irrigation(self):
        _LOGGER.info(f"{self.controller_mac_address} - Stopping watering...")
        self.ble_connection_status = "connecting"
        self.async_update_listeners()
        try:
            await self.api.stop_manual_sprinkle()
            self.ble_connection_status = "idle"
            _LOGGER.debug(f"{self.controller_mac_address} - Irrigation stop command sent.")
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            self.ble_connection_status = "error"
            self.async_update_listeners()
            return

        # Signal every per-station task to stop
        for event in self.station_stop_events:
            event.set()

        for station_id in range(1, self.num_stations + 1):
            self.stations[station_id - 1].state = "Stopped"

        # Reset countdown timers immediately; the delayed status refresh below
        # will reconcile with the controller's actual state shortly after.
        self.station_timer_end = [None] * self.num_stations
        self.active_stations = []

        await sleep(STATUS_REFRESH_DELAY)
        _LOGGER.debug(f"{self.controller_mac_address} - Sending delayed status refresh (3b 00)...")
        await self.async_request_status_ble()

        _LOGGER.info(f"{self.controller_mac_address} - Stopped watering.")
        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
    
    async def turn_controller_on(self):
        _LOGGER.info(f"{self.controller_mac_address} - Turning irrigation controller on...")

        try:
            await self.api.turn_on()
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return
        
        self.controller.state = "On"
        
        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
        _LOGGER.info(f"{self.controller_mac_address} - Irrigation controller turned on.")
    
    async def turn_controller_off(self):
        _LOGGER.info(f"{self.controller_mac_address} - Turning irrigation controller off..")
        try:
            await self.api.turn_off_permanent()
        except APIConnectionError as ex:
            _LOGGER.error(f"{self.controller_mac_address} - Failed due to connection error.")
            return

        self.controller.state = "Off"

        data = await self.async_update_all_sensors()
        self.async_set_updated_data(data)
        _LOGGER.info(f"{self.controller_mac_address} - Irrigation controller turned off.")


    async def async_request_status_ble(self, station_hint: int | None = None) -> dict:
        """Request device status via BLE and cache the decoded result.

        station_hint: when called right after starting a specific station, pass that
        station ID so the active-station detection is not confused by other stations
        that are still in "Sprinkling" state from a concurrent background task.
        """
        try:
            ble_status = await self.api.async_request_status()
            result = {
                "status": ble_status.status,
                "mode": ble_status.mode,
                "station_active": ble_status.station_active,
                "remaining_seconds": ble_status.remaining_seconds,
                "packet_hex": ble_status.packet_hex,
            }
            self._last_status = result
            _LOGGER.debug(
                "%s - Parsed status: mode=%s station_active=%s remaining_seconds=%s",
                self.controller_mac_address,
                self._last_status.get("mode"),
                self._last_status.get("station_active"),
                self._last_status.get("remaining_seconds"),
            )
            if self.ble_connection_status == "error":
                self.ble_connection_status = "idle"
            self._update_station_timers_from_status(self._last_status, station_hint=station_hint)
            _LOGGER.debug(
                "%s - Latest status updated: %s", self.controller_mac_address, self._last_status.get("status")
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("%s - Failed to request status: %s", self.controller_mac_address, exc)
            self._last_status = self._last_status or {}
        return self._last_status

    def get_station_remaining_seconds(self, station_id: int) -> int:
        """Return the live remaining sprinkling time (s) for a station, ticking down locally."""
        end_time = self.station_timer_end[station_id - 1]
        if end_time is None:
            return 0
        return max(0, round((end_time - dt_util.now()).total_seconds()))

    def _update_station_timers_from_status(self, status: dict, station_hint: int | None = None) -> None:
        """Update per-station countdown end times and active stations from a parsed 3b 00 status.

        station_hint: the station that was just commanded to start. When provided it is used
        directly, avoiding a false match against another station whose state is still
        "Sprinkling" from a concurrent background task.
        """
        station_active = status.get("station_active", False)
        remaining = status.get("remaining_seconds") or 0
        mode_status = status.get("status")

        self.station_timer_end = [None] * self.num_stations
        self.active_stations = []

        if station_active or mode_status in ("active_single", "active_all"):
            if mode_status == "active_all":
                self.active_stations = list(range(1, self.num_stations + 1))
                if remaining:
                    end_time = dt_util.now() + timedelta(seconds=remaining)
                    self.station_timer_end = [end_time] * self.num_stations
            else:
                # Prefer the explicit hint (avoids false match when another station's
                # background task still holds "Sprinkling" state).
                if station_hint is not None:
                    target_station = station_hint
                else:
                    target_station = next(
                        (sid for sid in range(1, self.num_stations + 1)
                         if self.stations[sid - 1].state == "Sprinkling"),
                        None,
                    )
                if target_station is not None:
                    self.active_stations = [target_station]
                    if remaining:
                        self.station_timer_end[target_station - 1] = dt_util.now() + timedelta(seconds=remaining)

        _LOGGER.debug(
            "%s - Station timers updated: active_stations=%s, timer_end=%s",
            self.controller_mac_address, self.active_stations, self.station_timer_end,
        )

    # ----------------------------------------------------------------------------
    # Here we add some custom functions on our data coordinator to be called
    # from entity platforms to get access to the specific data they want.
    #
    # These will be specific to your api or yo may not need them at all
    # ----------------------------------------------------------------------------
    def get_device(self, device_id: int) -> dict[str, Any]:
        """Get a device entity from our api data."""
        try:
            return [
                devices for devices in self.data if devices["device_id"] == device_id
            ][0]
        except (TypeError, IndexError):
            # In this case if the device id does not exist you will get an IndexError.
            # If api did not return any data, you will get TypeError.
            return None

    def get_device_parameter(self, device_id: int, parameter: str) -> Any:
        """Get the parameter value of one of our devices from our api data."""
        if device := self.get_device(device_id):
            return device.get(parameter)
