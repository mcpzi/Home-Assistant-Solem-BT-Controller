"""Solem BLE API and OpenWeatherMap API wrapper."""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError
from habluetooth import HaBleakClientWrapper
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_discovered_service_info,
)
from bleak_retry_connector import BleakOutOfConnectionSlotsError, establish_connection
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import as_local
from tenacity import retry, stop_after_attempt, wait_exponential

from .const import (
    BLUETOOTH_DEFAULT_TIMEOUT,
    CHARACTERISTIC_UUID,
    NOTIFY_UUID,
    OPEN_WEATHER_MAP_CURRENT_URL,
    OPEN_WEATHER_MAP_FORECAST_URL,
    START_VERIFY_MAX_ATTEMPTS,
    STATUS_COMMIT_PACKET,
    STATUS_RETRY_DELAY,
)

_LOGGER = logging.getLogger(__name__)

_MODE_MAP = {
    0x40: "idle",
    0x41: "active_all",
    0x42: "active_single",
    0x02: "programmed_off",
    0x60: "sleep",
}


@dataclass
class SolemStatus:
    """Decoded first-packet status from a `3b 00` status read."""

    status: str          # idle | active_single | active_all | programmed_off | unknown
    mode: int
    station_active: bool
    remaining_seconds: int | None
    packet_hex: str
    received_at: datetime | None


def parse_status_packet(data: bytes) -> SolemStatus:
    """Parse the first status packet (opcode 3c, index 02).

    Byte layout:
      [0]  opcode  0x3c
      [1]  0x10
      [2]  packet index  0x02
      [3]  mode
      [9]  active station number (0x00 = none)
      [13] remaining_seconds high byte
      [14] remaining_seconds low byte
    """
    received_at = dt_util.now()
    hex_str = data.hex()

    if len(data) < 15 or data[0] != 0x3C or data[2] != 0x02:
        return SolemStatus(
            status="unknown", mode=0, station_active=False,
            remaining_seconds=None, packet_hex=hex_str, received_at=received_at,
        )

    mode = data[3]
    station_active = data[9] != 0x00  # byte 9 = active station number; 0 = none
    raw_timer = (data[13] << 8) | data[14]
    remaining = raw_timer if raw_timer > 0 else None
    status_str = _MODE_MAP.get(mode, "unknown")

    return SolemStatus(
        status=status_str,
        mode=mode,
        station_active=station_active,
        remaining_seconds=remaining,
        packet_hex=hex_str,
        received_at=received_at,
    )


class APIConnectionError(Exception):
    """Exception raised when a BLE connection or write fails."""


class SolemAPI:
    """Direct BLE API wrapper for the Solem controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str | None,
        bluetooth_timeout: int = BLUETOOTH_DEFAULT_TIMEOUT,
    ) -> None:
        self.hass = hass
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout
        self.characteristic_uuid: str = CHARACTERISTIC_UUID
        self._conn_lock = asyncio.Lock()
        self.mock: bool = False

    async def scan_bluetooth(self) -> list[Any]:
        """Return discovered BLE devices via HA's bluetooth integration."""
        service_infos = async_discovered_service_info(self.hass, connectable=True)
        seen: set[str] = set()
        devices: list[Any] = []
        for info in service_infos:
            if info.address not in seen:
                seen.add(info.address)
                devices.append(info.device)
        return devices

    async def connect(self) -> str:
        """Validate device reachability (used by config flow)."""
        if self.mock:
            return ""
        await self.list_characteristics()
        return ""

    async def _resolve_ble_device(self) -> BLEDevice:
        _LOGGER.info("Resolving BLE device for MAC %s", self.mac_address.upper())
        ble_device = async_ble_device_from_address(
            self.hass, self.mac_address.upper(), connectable=True
        )
        if ble_device is not None:
            return ble_device
        _LOGGER.error("BLE device %s NOT found in HA bluetooth registry", self.mac_address)
        raise APIConnectionError("Device not found! Failed connecting!")

    async def _connect_client(self) -> HaBleakClientWrapper:
        async with self._conn_lock:
            ble_device = await self._resolve_ble_device()
            try:
                client = await establish_connection(
                    HaBleakClientWrapper,
                    ble_device,
                    name=f"Solem - {self.mac_address}",
                    timeout=self.bluetooth_timeout,
                    max_attempts=3,
                    ble_device_callback=lambda: async_ble_device_from_address(
                        self.hass, self.mac_address.upper(), connectable=True
                    ),
                )
                return client
            except BleakOutOfConnectionSlotsError as exc:
                raise APIConnectionError(
                    "Bluetooth adapter/proxy out of connection slots or device busy/unreachable"
                ) from exc
            except (BleakDBusError, TimeoutError, OSError) as exc:
                raise APIConnectionError("Timeout connecting to device") from exc
            except Exception as exc:  # noqa: BLE001
                raise APIConnectionError("Unexpected BLE connection error") from exc

    async def list_characteristics(self) -> dict:
        """Return discovered services/characteristics (debug helper)."""
        client = await self._connect_client()
        try:
            if not client.is_connected:
                raise APIConnectionError("Failed connecting!")
            services = getattr(client, "services", None)
            if services is None:
                inner = getattr(client, "_client", None) or getattr(client, "_bleak_client", None)
                if inner is not None and hasattr(inner, "get_services"):
                    services = await inner.get_services()
                else:
                    raise APIConnectionError("Services not available on this platform/client")
            result: dict = {}
            for svc in services:
                chars = []
                for c in svc.characteristics:
                    chars.append({
                        "uuid": str(c.uuid),
                        "properties": list(c.properties),
                        "descriptors": [str(d.uuid) for d in c.descriptors],
                    })
                result[str(svc.uuid)] = chars
            return result
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.4, min=0.4, max=2))
    async def _write_with_auth_retry(self, client: HaBleakClientWrapper, payload: bytes) -> None:
        if not client.is_connected:
            raise APIConnectionError("Client not connected")
        await client.write_gatt_char(self.characteristic_uuid, payload, response=True)

    async def _write_and_commit(self, command: bytes, send_commit: bool) -> None:
        if self.mock:
            _LOGGER.debug("Mock=True, skipping BLE write")
            return
        client = await self._connect_client()
        try:
            if not client.is_connected:
                raise APIConnectionError("Failed connecting!")
            await self._write_with_auth_retry(client, command)
            await asyncio.sleep(0.3)
            if send_commit:
                commit = struct.pack(">H", 0x3b00)
                await self._write_with_auth_retry(client, commit)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def async_request_status(self) -> SolemStatus:
        """Send `3b 00` and return the decoded first-packet status."""
        if self.mock:
            return SolemStatus(
                status="idle", mode=0x40, station_active=False,
                remaining_seconds=None, packet_hex="", received_at=dt_util.now(),
            )
        received: list[bytes] = []
        event = asyncio.Event()

        def _on_notification(_: object, data: bytearray) -> None:
            if len(data) >= 3 and data[0] == 0x3C and data[2] == 0x02:
                received.append(bytes(data))
                event.set()

        client = await self._connect_client()
        try:
            await client.start_notify(NOTIFY_UUID, _on_notification)
            _LOGGER.debug("Sending status request 3b 00")
            await client.write_gatt_char(CHARACTERISTIC_UUID, STATUS_COMMIT_PACKET, response=True)
            try:
                await asyncio.wait_for(event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("Timed out waiting for status packet")
        finally:
            try:
                await client.stop_notify(NOTIFY_UUID)
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

        if received:
            status = parse_status_packet(received[0])
            _LOGGER.debug(
                "Status packet: mode=%#04x station_active=%s remaining=%s",
                status.mode, status.station_active, status.remaining_seconds,
            )
            return status

        return SolemStatus(
            status="unknown", mode=0, station_active=False,
            remaining_seconds=None, packet_hex="", received_at=dt_util.now(),
        )

    async def turn_on(self) -> None:
        """Turn on controller (enable watering)."""
        command = struct.pack(">HBHH", 0x3105, 0xa0, 0x0000, 0x0000)
        await self._write_and_commit(command, True)

    async def turn_off_permanent(self) -> None:
        """Disable watering permanently."""
        command = struct.pack(">HBHH", 0x3105, 0xc0, 0x00, 0x00, 0x0000)
        await self._write_and_commit(command, False)

    async def turn_off_x_days(self, days: int) -> None:
        """Disable watering for X days."""
        days = max(0, min(days, 365))
        command = struct.pack(">HBHH", 0x3105, 0xc0, days, 0x0000)
        await self._write_and_commit(command, False)

    async def sprinkle_station_x_for_y_minutes(self, station: int, minutes: int) -> None:
        """Manually water a station for Y minutes, with start verification."""
        station = max(1, min(station, 16))
        minutes = max(1, min(minutes, 240))
        secs = minutes * 60
        command = struct.pack(">HBBBH", 0x3105, 0x12, station, 0x00, secs)

        for attempt in range(1, START_VERIFY_MAX_ATTEMPTS + 1):
            _LOGGER.debug("Irrigation start attempt %d/%d", attempt, START_VERIFY_MAX_ATTEMPTS)
            await self._write_and_commit(command, False)
            await asyncio.sleep(STATUS_RETRY_DELAY)
            status = await self.async_request_status()
            if status.station_active or status.status in ("active_single", "active_all"):
                _LOGGER.debug("Irrigation confirmed active after attempt %d", attempt)
                return
            _LOGGER.debug("Station not active (mode=%#04x), retrying...", status.mode)

        raise APIConnectionError(
            f"Station {station} did not become active after {START_VERIFY_MAX_ATTEMPTS} attempts"
        )

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> None:
        """Manually water all stations for Y minutes each."""
        minutes = max(1, min(minutes, 240))
        secs = minutes * 60
        command = struct.pack(">HBHH", 0x3105, 0x11, 0x00, secs)
        await self._write_and_commit(command, False)

    async def run_program_x(self, program: int) -> None:
        """Run a controller program by id (1-3)."""
        program = max(1, min(program, 3))
        command = struct.pack(">HBHH", 0x3105, 0x14, program, 0x0000)
        await self._write_and_commit(command, False)

    async def stop_manual_sprinkle(self) -> None:
        """Stop any running manual watering session."""
        command = struct.pack(">HBHH", 0x3105, 0x15, 0x00ff, 0x0000)
        await self._write_and_commit(command, False)


class OpenWeatherMapAPI:
    """Class for OpenWeatherMap API."""

    def __init__(self, api_key: str, latitude: str, longitude: str, timeout: int) -> None:
        """Initialise."""
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.timeout = timeout
        self._cache_forecast = None
        self._cache_current = None
        self._last_forecast_fetch_time = None
        self.last_forecast_date = datetime.now().date()
        self._last_current_fetch_time = None

    async def get_current_weather(self) -> Any:
        now = dt_util.now()

        if self._cache_current and self._last_current_fetch_time and now - self._last_current_fetch_time < timedelta(minutes=self.timeout):
            _LOGGER.debug("Returning cached data.")
            return self._cache_current

        weather_url = f"{OPEN_WEATHER_MAP_CURRENT_URL}appid={self.api_key}&lat={self.latitude}&lon={self.longitude}"
        _LOGGER.debug("Getting current weather at : %s", weather_url)

        async with aiohttp.ClientSession() as session:
            async with session.get(weather_url) as response:
                try:
                    data = await response.json()
                    _LOGGER.debug("Current Weather Data: %s", data)

                    if "dt" in data:
                        utc_dt = datetime.fromtimestamp(data["dt"], tz=timezone.utc)
                        local_dt = as_local(utc_dt)
                        data["dt_txt"] = local_dt.strftime('%Y-%m-%d %H:%M:%S')

                    self._cache_current = data
                    self._last_current_fetch_time = now
                except Exception:
                    _LOGGER.error("Error processing Current Weather data: JSON format invalid!")
                    raise APIConnectionError("Error processing Current Weather data: JSON format invalid!")

        return self._cache_current

    async def is_raining(self) -> dict:
        current_weather = await self.get_current_weather()
        return {
            "is_raining": "rain" in current_weather,
            "current": current_weather
        }

    async def get_forecast(self) -> list:
        """Obtains and preserves data from 00h till 00h of the next day."""
        now = datetime.now()

        if self._cache_forecast and self._last_forecast_fetch_time and now - self._last_forecast_fetch_time < timedelta(minutes=self.timeout):
            _LOGGER.debug("Returning cached data.")
            return self._cache_forecast

        temp_cache = self._cache_forecast.copy() if self._cache_forecast else []

        if self.last_forecast_date != now.date():
            _LOGGER.debug("Day changed, will get 00h forecast to new day...")
            last_00_03_forecast = None

            if self._cache_forecast:
                for forecast in self._cache_forecast:
                    forecast_time_str = forecast.get("dt_txt")
                    if not forecast_time_str:
                        continue
                    forecast_dt = datetime.strptime(forecast_time_str, "%Y-%m-%d %H:%M:%S")
                    if forecast_dt.hour == 0:
                        last_00_03_forecast = forecast
                        break

            self._cache_forecast = []
            self.last_forecast_date = now.date()

            if last_00_03_forecast:
                self._cache_forecast.append(last_00_03_forecast)

        current_hour = now.hour
        forecast_hours = [h for h in range(0, 21, 3) if h >= current_hour]
        forecast_hours.append(0)
        items = len(forecast_hours)

        weather_url = f"{OPEN_WEATHER_MAP_FORECAST_URL}&appid={self.api_key}&lat={self.latitude}&lon={self.longitude}&cnt={items}"
        _LOGGER.debug("Getting forecast at: %s", weather_url)

        async with aiohttp.ClientSession() as session:
            async with session.get(weather_url) as response:
                try:
                    data = await response.json()

                    for item in data["list"]:
                        forecast_time_str = item["dt_txt"]
                        existing_index = next(
                            (index for index, forecast in enumerate(self._cache_forecast)
                             if forecast["dt_txt"] == forecast_time_str),
                            None
                        )
                        if existing_index is not None:
                            self._cache_forecast[existing_index] = item
                        else:
                            self._cache_forecast.append(item)

                    self._last_forecast_fetch_time = now

                except Exception:
                    _LOGGER.error("Error processing Forecast Weather data: JSON format invalid!", exc_info=True)
                    if not self._cache_forecast:
                        self._cache_forecast = temp_cache
                    raise APIConnectionError("Error processing Forecast Weather data: JSON format invalid!")

        return self._cache_forecast

    async def will_it_rain(self) -> dict:
        """Verifies if it will rain for the rest of the day."""
        forecast = await self.get_forecast()

        now = dt_util.now()
        today_str = now.strftime("%Y-%m-%d")
        current_hour = now.hour

        block_hours = [h for h in range(0, 21, 3)]
        current_block = max([h for h in block_hours if h <= current_hour])

        relevant_forecasts = []
        for item in forecast:
            forecast_time_str = item["dt_txt"]
            forecast_date, forecast_hour_minute = forecast_time_str.split(" ")
            forecast_hour, _, _ = forecast_hour_minute.split(":")
            forecast_hour = int(forecast_hour)

            if forecast_date == today_str and forecast_hour >= current_block:
                relevant_forecasts.append(item)

        will_rain = any(item.get("pop", 0) > 0.50 for item in relevant_forecasts)

        return {
            "will_rain": will_rain,
            "forecast": forecast
        }

    async def get_total_rain_forecast_for_today(self) -> float:
        """Calculates total amount of rain predicted (mm) for the rest of the day."""
        will_it_rain_result = await self.will_it_rain()
        forecasts = will_it_rain_result.get("forecast", [])

        now = dt_util.now()
        current_time = now.hour * 60 + now.minute
        today_str = now.strftime("%Y-%m-%d")
        total_rain_mm = 0.0

        for item in forecasts:
            forecast_time_str = item["dt_txt"]
            forecast_date, forecast_hour_minute = forecast_time_str.split(" ")
            forecast_hour, _, _ = forecast_hour_minute.split(":")
            forecast_hour = int(forecast_hour)

            rain_data = item.get("rain", {})
            rain_mm = rain_data.get("3h", 0.0)

            if forecast_date != today_str:
                continue

            forecast_start_minute = forecast_hour * 60
            forecast_end_minute = forecast_start_minute + 180

            if forecast_end_minute <= current_time:
                continue

            if forecast_start_minute <= current_time < forecast_end_minute:
                remaining_minutes = forecast_end_minute - current_time
                rain_mm = (remaining_minutes / 180) * rain_mm

            total_rain_mm += rain_mm

        return total_rain_mm
