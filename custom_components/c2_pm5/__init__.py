from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import memory_probe
from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_TYPE,
    DOMAIN,
    EVENT_PM5_AVAILABILITY_CHANGED,
    PLATFORMS,
)
from .memory_record import decode_stored_workout
from .pm5_ble import PM5BleSession, PM5Data
from .schema_compat import vol
from .status_panel import async_setup_panel
from .workout_manager import WorkoutError, WorkoutManager
from .workout_notifications import WorkoutNotifications
from .workout_recorder import WorkoutRecorder

_LOGGER = logging.getLogger(__name__)

SERVICE_CONNECT_HRM = "connect_hrm"
SERVICE_UPLOAD_WORKOUT = "upload_workout"
SERVICE_KEEP_WORKOUT = "keep_workout"
SERVICE_READ_LATEST_STORED_WORKOUT = "read_latest_stored_workout"
DATA_WORKOUT_MANAGER = "workout_manager"
DATA_WORKOUT_NOTIFICATIONS = "workout_notifications"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA_CONNECT_HRM = vol.Schema(
    {
        vol.Required("address"): str,
        vol.Required("manufacturer_id"): vol.Coerce(int),
        vol.Required("device_type"): str,
        vol.Required("belt_id"): vol.Coerce(int),
    }
)


@dataclass
class PM5State:
    available: bool = False
    values: dict = field(default_factory=dict)


class PM5Coordinator(DataUpdateCoordinator[PM5State]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        device_type: str,
        workout_manager: WorkoutManager,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=device_type)
        self.address = address
        self.device_type = device_type
        self.entry_name = entry.title
        self.state = PM5State()
        self._closed = False
        self._memory_read_lock = asyncio.Lock()
        self._memory_check: asyncio.Task[None] | None = None

        def on_complete(workout: dict) -> None:
            workout_manager.record(workout)
            # A recorder held across a link drop can finish on a timer, after
            # the idle status that would normally trigger the memory check.
            self._update_memory_check()

        self.recorder = WorkoutRecorder(
            hass.loop,
            address,
            entry.title,
            on_complete,
            time_zone=hass.config.time_zone,
            has_saved_summary=workout_manager.has_saved_summary,
        )
        self.async_set_updated_data(self.state)

        self.session = PM5BleSession(
            hass,
            device_type,
            address,
            self._handle_data,
            self._handle_available,
            config_entry=entry,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            name=self.device_type,
            manufacturer="Concept2",
            model="PM5",
        )

    def _handle_available(self, available: bool) -> None:
        self.recorder.set_connected(available)
        if available and available is not self.state.available:
            # Clear values so we don't expose stale state
            self.state.values = {}

        if available == self.state.available:
            return

        self.state.available = available
        if available:
            self._update_memory_check()
        else:
            self._cancel_memory_check()
        self.async_set_updated_data(self.state)

        # Fire HA event
        self.hass.bus.async_fire(
            EVENT_PM5_AVAILABILITY_CHANGED,
            {
                "address": self.address,
                "device_type": self.device_type,
                "available": available,
            },
        )

    def _handle_data(self, data: PM5Data) -> None:
        self.state.values.update(data.values)
        self.recorder.receive(data.values, time())
        if "0031_workout_state_raw" in data.values:
            self._update_memory_check()
        self.async_set_updated_data(self.state)

    async def async_read_latest(self) -> dict:
        """Serialize automatic and administrator reads of the same monitor."""
        async with self._memory_read_lock:
            return await memory_probe.read_latest(self)

    def _cancel_memory_check(self) -> None:
        if self._memory_check is not None:
            self._memory_check.cancel()
            self._memory_check = None

    def _update_memory_check(self) -> None:
        if (
            self._closed
            or not self.session.is_connected
            or not self.session.supports_memory_reads
        ):
            return
        # Keep the completed task until disconnect, including when the PM
        # reports an empty log or an unsupported record.
        if self._memory_check is not None and self._memory_check.done():
            return
        if not memory_probe.is_idle(self):
            self._cancel_memory_check()
        elif self._memory_check is None:
            self._memory_check = self.hass.async_create_task(
                self._recover_latest(),
                f"Check PM5 workout memory {self.address}",
                eager_start=False,
            )

    async def _recover_latest(self) -> None:
        try:
            response = await self.async_read_latest()
            workout = decode_stored_workout(response)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("Skipping PM5 memory recovery (%s): %s", self.address, err)
            return
        self.recorder.recover_memory(workout, time())

    def close(self) -> None:
        """Stop memory recovery and finalize any open recording."""
        self._closed = True
        self._cancel_memory_check()
        self.recorder.close()

    def status(self) -> dict:
        return {
            "address": self.address,
            "name": self.entry_name,
            "connected": self.session.is_connected,
            "available": self.session.is_discoverable,
            "recording": self.recorder.recording,
            "values": dict(self.state.values) if self.state.available else {},
        }


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the panel and workout actions once for all PM5 monitors."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = WorkoutManager(hass)
    await manager.async_initialize()
    domain_data[DATA_WORKOUT_MANAGER] = manager
    notifications = WorkoutNotifications(hass, manager)
    domain_data[DATA_WORKOUT_NOTIFICATIONS] = notifications
    active_memory_reads: set[str] = set()

    def coordinators() -> list[PM5Coordinator]:
        return [
            value for value in domain_data.values() if isinstance(value, PM5Coordinator)
        ]

    def get_devices() -> list[dict]:
        return [coordinator.status() for coordinator in coordinators()]

    async def _handle_connect_hrm(call: ServiceCall) -> None:
        address = call.data["address"].strip().upper()
        for coordinator in coordinators():
            if coordinator.address.upper() == address:
                await coordinator.session.async_set_hrbelt_info(
                    call.data["manufacturer_id"],
                    call.data["device_type"],
                    call.data["belt_id"],
                )
                return
        raise HomeAssistantError(f"No configured PM5 matches address {address}")

    async def _handle_upload_workout(call: ServiceCall) -> None:
        try:
            await manager.async_upload(
                call.data["workout_id"],
                call.data.get("garmin_entity_id"),
                call.data["retry"],
                call.data.get("upload_attempt"),
            )
        except WorkoutError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_keep_workout(call: ServiceCall) -> None:
        try:
            await manager.async_keep_workout(call.data["workout_id"])
        except WorkoutError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_read_latest(call: ServiceCall) -> dict:
        entry_id = call.data["config_entry_id"]
        coordinator = domain_data.get(entry_id)
        if not isinstance(coordinator, PM5Coordinator):
            raise HomeAssistantError("Choose a loaded Concept2 PM5 config entry")
        if entry_id in active_memory_reads:
            raise HomeAssistantError("A probe is already running for this PM5")
        active_memory_reads.add(entry_id)
        try:
            return await coordinator.async_read_latest()
        finally:
            active_memory_reads.remove(entry_id)

    async def _finish_recordings(_event) -> None:
        for coordinator in coordinators():
            coordinator.close()
        await manager.async_flush()

    hass.services.async_register(
        DOMAIN,
        SERVICE_CONNECT_HRM,
        _handle_connect_hrm,
        schema=SERVICE_SCHEMA_CONNECT_HRM,
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_UPLOAD_WORKOUT,
        _handle_upload_workout,
        schema=vol.Schema(
            {
                vol.Required("workout_id"): cv.string,
                vol.Optional("garmin_entity_id"): cv.entity_id,
                vol.Optional("retry", default=False): cv.boolean,
                vol.Optional("upload_attempt"): cv.positive_int,
            }
        ),
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_KEEP_WORKOUT,
        _handle_keep_workout,
        schema=vol.Schema({vol.Required("workout_id"): cv.string}),
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_READ_LATEST_STORED_WORKOUT,
        _handle_read_latest,
        schema=vol.Schema({vol.Required("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    await async_setup_panel(hass, get_devices, manager)
    notifications.async_listen()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _finish_recordings)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data[DOMAIN]
    address = entry.data[CONF_ADDRESS]
    device_type = entry.data.get(CONF_DEVICE_TYPE) or f"PM5 {address}"
    coordinator = PM5Coordinator(
        hass, entry, address, device_type, domain_data[DATA_WORKOUT_MANAGER]
    )
    domain_data[entry.entry_id] = coordinator
    try:
        await coordinator.session.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        domain_data.pop(entry.entry_id)
        coordinator.close()
        await coordinator.session.stop()
        raise
    notifications: WorkoutNotifications = domain_data[DATA_WORKOUT_NOTIFICATIONS]
    entry.async_on_unload(
        entry.add_update_listener(notifications.async_options_updated)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    domain_data = hass.data[DOMAIN]
    coordinator: PM5Coordinator = domain_data.pop(entry.entry_id)
    try:
        await coordinator.session.stop()
    finally:
        coordinator.close()
        await domain_data[DATA_WORKOUT_MANAGER].async_flush()
    return True
