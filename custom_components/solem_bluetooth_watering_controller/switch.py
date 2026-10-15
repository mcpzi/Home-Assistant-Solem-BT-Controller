"""Switch platform — one switch per irrigation station."""

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MyConfigEntry
from .base import SolemBaseEntity
from .coordinator import SolemCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-station switches."""
    coordinator: SolemCoordinator = config_entry.runtime_data.coordinator

    switches = [
        StationSwitch(coordinator, device)
        for device in coordinator.data
        if device.get("device_type") == "STATION_SWITCH"
    ]
    async_add_entities(switches)


class StationSwitch(SolemBaseEntity, SwitchEntity):
    """Switch that starts/stops a single irrigation station."""

    def __init__(self, coordinator: SolemCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator, device, None)
        self._station_id = int(self.device_id.rsplit("_", 1)[-1])

    @property
    def is_on(self) -> bool:
        return self._station_id in self.coordinator.active_stations

    @property
    def icon(self) -> str:
        return "mdi:sprinkler" if self.is_on else "mdi:sprinkler-variant"

    async def async_turn_on(self, **kwargs: Any) -> None:
        asyncio.create_task(self.coordinator.start_irrigation(self._station_id))

    async def async_turn_off(self, **kwargs: Any) -> None:
        asyncio.create_task(self.coordinator.stop_irrigation())
