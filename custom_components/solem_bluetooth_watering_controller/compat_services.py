"""BLE service registrations for solem_bluetooth_watering_controller."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .api import APIConnectionError, SolemAPI
from .const import BLUETOOTH_DEFAULT_TIMEOUT, BLUETOOTH_MIN_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _find_coordinator(hass: HomeAssistant, mac_address: str | None):
    """Return the SolemCoordinator managing mac_address, or None."""
    from .coordinator import SolemCoordinator
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, SolemCoordinator) and coordinator.controller_mac_address == mac_address:
            return coordinator
    return None


def _get_timeout(call: ServiceCall) -> int:
    timeout = call.data.get("bluetooth_timeout", BLUETOOTH_DEFAULT_TIMEOUT)
    try:
        timeout_int = int(timeout)
    except (TypeError, ValueError) as exc:
        raise HomeAssistantError("Invalid bluetooth_timeout") from exc
    return max(BLUETOOTH_MIN_TIMEOUT, timeout_int)


def async_register_compat_services(hass: HomeAssistant) -> None:
    """Register BLE services under the main domain (idempotent)."""
    if hass.services.has_service(DOMAIN, "stop_manual_sprinkle"):
        return

    async def _list_characteristics(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            result = await api.list_characteristics()
            for svc_uuid, chars in result.items():
                _LOGGER.info("Service: %s", svc_uuid)
                for c in chars:
                    _LOGGER.info("  Characteristic: %s (properties=%s)", c["uuid"], c["properties"])
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _turn_off_permanent(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.turn_off_permanent()
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _turn_off_x_days(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.turn_off_x_days(int(call.data.get("days", 1)))
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _turn_on(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.turn_on()
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _sprinkle_station(call: ServiceCall) -> None:
        device_mac = call.data.get("device_mac")
        station = int(call.data.get("station", 1))
        minutes = int(call.data.get("minutes", 1))
        coordinator = _find_coordinator(hass, device_mac)
        if coordinator is not None:
            await coordinator.start_irrigation(station, minutes)
            return
        # No coordinator found for this MAC — fall back to standalone API
        api = SolemAPI(hass, device_mac, bluetooth_timeout=_get_timeout(call))
        try:
            await api.sprinkle_station_x_for_y_minutes(station, minutes)
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _sprinkle_all(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.sprinkle_all_stations_for_y_minutes(int(call.data.get("minutes", 1)))
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _run_program(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.run_program_x(int(call.data.get("program", 1)))
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _stop_manual_sprinkle(call: ServiceCall) -> None:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            await api.stop_manual_sprinkle()
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _request_status(call: ServiceCall) -> dict:
        api = SolemAPI(hass, call.data.get("device_mac"), bluetooth_timeout=_get_timeout(call))
        try:
            status = await api.async_request_status()
            result = {
                "status": status.status,
                "mode": status.mode,
                "station_active": status.station_active,
                "remaining_seconds": status.remaining_seconds,
                "packet_hex": status.packet_hex,
            }
            _LOGGER.info("Device %s status: %s", call.data.get("device_mac"), result)
            return result
        except APIConnectionError as exc:
            raise HomeAssistantError(str(exc)) from exc

    hass.services.async_register(DOMAIN, "list_characteristics", _list_characteristics)
    hass.services.async_register(DOMAIN, "turn_off_permanent", _turn_off_permanent)
    hass.services.async_register(DOMAIN, "turn_off_x_days", _turn_off_x_days)
    hass.services.async_register(DOMAIN, "turn_on", _turn_on)
    hass.services.async_register(DOMAIN, "sprinkle_station_x_for_y_minutes", _sprinkle_station)
    hass.services.async_register(DOMAIN, "sprinkle_all_stations_for_y_minutes", _sprinkle_all)
    hass.services.async_register(DOMAIN, "run_program_x", _run_program)
    hass.services.async_register(DOMAIN, "stop_manual_sprinkle", _stop_manual_sprinkle)
    hass.services.async_register(
        DOMAIN, "request_status", _request_status,
        supports_response=SupportsResponse.OPTIONAL,
    )
    _LOGGER.info("BLE services registered under '%s'", DOMAIN)
