"""Sensor setup for our Integration.

Here we use a different method to define some of our entity classes.
As, in our example, so much is common, we use our base entity class to define
many properties, then our base sensor class to define the property to get the
value of the sensor.

As such, for all our other sensor types, we can just set the _attr_ value to
keep our code small and easily readable.  You can do this for all entity properties(attributes)
if you so wish, or mix and match to suit.
"""

from dataclasses import dataclass
import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MyConfigEntry
from .base import SolemBaseEntity
from .const import DOMAIN
from .coordinator import SolemCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class SensorTypeClass:
    """Class for holding sensor type to sensor class."""

    device_type: str
    state_field: str
    sensor_class: object


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MyConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the Sensors."""
    coordinator: SolemCoordinator = config_entry.runtime_data.coordinator

    sensor_types = [
        SensorTypeClass("STATE_SENSOR", "state", StateSensor),
        SensorTypeClass("LAST_SPRINKLE_SENSOR", "state", LastSprinkleSensor),
        SensorTypeClass("LAST_RAIN_SENSOR", "state", LastRainSensor),
        SensorTypeClass("TOTAL_WATER_CONSUMPTION_SENSOR", "state", TotalWaterConsumptionSensor),
        SensorTypeClass("TOTAL_AMOUNT_RAIN_TODAY", "state", TotalAmountRainSensor),
        SensorTypeClass("TOTAL_FORECASTED_RAIN_TODAY", "state", TotalForecastedRainSensor),
        SensorTypeClass("STATION_REMAINING_TIME_SENSOR", "state", StationRemainingTimeSensor),
    ]

    sensors = []

    for sensor_type in sensor_types:
        sensors.extend(
            [
                sensor_type.sensor_class(coordinator, device, sensor_type.state_field)
                for device in coordinator.data
                if device.get("device_type") == sensor_type.device_type
            ]
        )

    sensors.append(LatestStatusSensor(coordinator))
    async_add_entities(sensors)


class StateSensor(SolemBaseEntity, SensorEntity):
    @property
    def native_value(self) -> int | float | str:
        return self.coordinator.get_device_parameter(self.device_id, self.parameter)

    @property
    def extra_state_attributes(self):
        attrs = {}
        if self.coordinator.controller.device_name == "Controller Status":
            attrs["num_stations"] = self.coordinator.num_stations
        return attrs


class LastSprinkleSensor(SolemBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        last_sprinkle = self.coordinator.last_sprinkle

        if last_sprinkle:
            try:
                if isinstance(last_sprinkle, str):
                    last_sprinkle = datetime.fromisoformat(last_sprinkle)

                if last_sprinkle.tzinfo is None:
                    last_sprinkle = dt_util.as_local(last_sprinkle)

                return last_sprinkle
            except Exception as e:
                _LOGGER.warning(f"Invalid format for last_sprinkle: {last_sprinkle} - {e}")

        return None


class LastRainSensor(SolemBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        last_rain = self.coordinator.last_rain

        if last_rain:
            try:
                if isinstance(last_rain, str):
                    last_rain = datetime.fromisoformat(last_rain)

                if last_rain.tzinfo is None:
                    last_rain = dt_util.as_local(last_rain)

                return last_rain
            except Exception as e:
                _LOGGER.warning(f"Invalid format for last_rain: {last_rain} - {e}")

        return None


class TotalAmountRainSensor(SolemBaseEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.PRECIPITATION

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfPrecipitationDepth.MILLIMETERS

    @property
    def native_value(self) -> int:
        return round(self.coordinator.rain_total_amount_today, 2)


class TotalForecastedRainSensor(SolemBaseEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.PRECIPITATION

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfPrecipitationDepth.MILLIMETERS

    @property
    def native_value(self) -> int:
        return round(self.coordinator.rain_total_amount_forecasted_today, 2)


class TotalWaterConsumptionSensor(SolemBaseEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = "L"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.total_water_consumption, 2)


class StationRemainingTimeSensor(SolemBaseEntity, SensorEntity):
    """Live countdown of remaining sprinkling time (seconds) for a single station.

    Starts from the value reported by the latest `3b 00` status read, then
    ticks down locally once per second without polling the controller.
    Resets to 0 as soon as sprinkling is stopped.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, coordinator: SolemCoordinator, device, parameter: str) -> None:
        super().__init__(coordinator, device, parameter)
        self._station_id = int(self.device_id.rsplit("_", 1)[-1])
        self._unsub_tick = None

    @property
    def native_value(self) -> int:
        return self.coordinator.get_station_remaining_seconds(self._station_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._sync_timer()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_timer()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timer()
        await super().async_will_remove_from_hass()

    def _sync_timer(self) -> None:
        """Start/stop the 1-second local tick based on whether a timer is running."""
        if self.coordinator.station_timer_end[self._station_id - 1] is not None:
            if self._unsub_tick is None:
                self._unsub_tick = async_track_time_interval(self.hass, self._tick, timedelta(seconds=1))
        else:
            self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    @callback
    def _tick(self, _now: datetime) -> None:
        if self.coordinator.station_timer_end[self._station_id - 1] is None:
            self._cancel_timer()
        self.async_write_ha_state()


class LatestStatusSensor(CoordinatorEntity, SensorEntity):
    """Reports the latest decoded BLE device status.

    State is "off" when no station is active, otherwise it names the
    active station(s) (e.g. "Station 2 active" or "All stations active").
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: SolemCoordinator) -> None:
        super().__init__(coordinator)
        self._mac = coordinator.controller_mac_address

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}-{self._mac}-latest_status"

    @property
    def name(self) -> str:
        return "Latest status"

    @property
    def native_value(self) -> str:
        ble_status = self.coordinator.ble_connection_status
        if ble_status == "connecting":
            return "Connecting..."
        if ble_status == "error":
            return "BLE error"
        active = self.coordinator.active_stations
        if not active:
            return "off"
        if len(active) > 1 and len(active) == self.coordinator.num_stations:
            return "All stations active"
        if len(active) == 1:
            return f"Station {active[0]} active"
        return "Stations " + ", ".join(str(s) for s in active) + " active"

    @property
    def extra_state_attributes(self) -> dict:
        s = self.coordinator._last_status
        attrs = {
            "active_stations": self.coordinator.active_stations,
            "ble_connection_status": self.coordinator.ble_connection_status,
        }
        if not s:
            return attrs
        attrs.update({
            "status": s.get("status"),
            "mode": s.get("mode"),
            "station_active": s.get("station_active"),
            "remaining_seconds": s.get("remaining_seconds"),
            "packet_hex": s.get("packet_hex"),
        })
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            name=self._mac,
            manufacturer="Solem",
            identifiers={(DOMAIN, self._mac)},
        )
