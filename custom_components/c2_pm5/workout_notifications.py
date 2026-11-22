"""Send Companion App prompts for saved workouts and handle explicit decisions."""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from uuid import uuid4

from homeassistant.components.device_automation import (
    DeviceAutomationType,
    InvalidDeviceAutomationConfig,
    async_get_device_automation_platform,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import Event, EventOrigin, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_ADDRESS,
    CONF_GARMIN_ACCOUNT,
    CONF_NOTIFICATION_DEVICE,
    DOMAIN,
    SIGNAL_WORKOUT_UPDATED,
)
from .garmin_upload import garmin_account
from .status_panel import PANEL_PATH
from .workout_manager import REVIEW_STATUSES, WorkoutError, WorkoutManager

_LOGGER = logging.getLogger(__name__)
_MOBILE_APP = "mobile_app"


@dataclass(frozen=True)
class _Prompt:
    device_id: str
    garmin_entry_id: str | None
    status: str
    attempt: int
    token: str = field(default="", compare=False)


class WorkoutNotifications:
    """Keep each prompt tied to a saved workout and the configured destination."""

    def __init__(self, hass: HomeAssistant, manager: WorkoutManager) -> None:
        self.hass = hass
        self._manager = manager
        self._prompts: dict[str, _Prompt] = {}
        self._deciding: set[str] = set()
        self._lock = asyncio.Lock()

    @callback
    def async_listen(self) -> None:
        async_dispatcher_connect(self.hass, SIGNAL_WORKOUT_UPDATED, self._async_update)
        self.hass.bus.async_listen("mobile_app_notification_action", self._async_decide)
        self.hass.bus.async_listen(
            EVENT_SERVICE_REGISTERED, self._async_notify_registered
        )
        async_at_started(self.hass, self._async_started)

    async def _async_started(self, _hass: HomeAssistant) -> None:
        await self._async_refresh()

    async def _async_notify_registered(self, event: Event) -> None:
        if event.data["domain"] == "notify" and event.data["service"].startswith(
            "mobile_app_"
        ):
            await self._async_refresh()

    async def async_options_updated(
        self, _hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        await self._async_refresh(entry.data[CONF_ADDRESS])

    async def _async_refresh(self, address: str | None = None) -> None:
        for workout in self._manager.list_workouts():
            if address is None or workout["address"] == address:
                await self._async_update(workout["workout_id"])

    def _prompt_for(self, workout: dict) -> _Prompt | None:
        options = self._manager.workout_options(workout["address"])
        if workout["status"] in REVIEW_STATUSES and (
            phone := options.get(CONF_NOTIFICATION_DEVICE)
        ):
            return _Prompt(
                phone,
                options.get(CONF_GARMIN_ACCOUNT),
                workout["status"],
                workout["upload_attempt"],
            )
        return None

    async def _async_update(self, workout_id: str, error: str | None = None) -> None:
        async with self._lock:
            workout = self._manager.get_summary(workout_id)
            prompt = self._prompt_for(workout)
            previous = self._prompts.get(workout_id)
            tag = f"{DOMAIN}_{workout_id}"
            if previous and (prompt is None or prompt.device_id != previous.device_id):
                self._prompts.pop(workout_id, None)
                await self._async_send(
                    previous.device_id, "clear_notification", {"tag": tag}
                )
            if prompt is None or (
                prompt == previous and (error is None or error == workout["error"])
            ):
                return

            prompt = replace(prompt, token=uuid4().hex)
            account = garmin_account(self.hass, prompt.garmin_entry_id)
            minutes, seconds = divmod(round(workout["elapsed_seconds"]), 60)
            message = (
                f"{minutes}:{seconds:02d} elapsed, {workout['distance_m']:,.0f} m."
            )
            if workout.get("summary_only"):
                message += " Partial recording · PM5 summary only."
            elif workout["partial"]:
                message += " Partial recording."
            path = f"/{PANEL_PATH}?workout={workout_id}"
            prefix = f"{DOMAIN}|{workout_id}|{prompt.token}|"
            actions = []
            if prompt.status == "needs_review":
                message += " Check Garmin for this workout before retrying from Home Assistant."
            elif account:
                message += f" Upload to {account['name']}?"
                actions.append(
                    {
                        "action": prefix + "upload",
                        "title": "Upload to Garmin",
                        "authenticationRequired": True,
                    }
                )
            else:
                message += " Choose a default Garmin account in the PM5 settings to upload from your phone."
            actions.append(
                {
                    "action": prefix + "keep",
                    "title": "Keep locally",
                    "authenticationRequired": True,
                }
            )
            if error and error != workout["error"]:
                message += f" {error}"

            self._prompts[workout_id] = prompt
            sent = await self._async_send(
                prompt.device_id,
                message,
                {"tag": tag, "actions": actions, "url": path, "clickAction": path},
                title=f"Concept2 row · {workout['name']}",
            )
            if not sent:
                self._prompts.pop(workout_id, None)

    async def _async_decide(self, event: Event) -> None:
        action = event.data.get("action")
        if event.origin is not EventOrigin.remote or not isinstance(action, str):
            return
        parts = action.split("|")
        if len(parts) != 4 or parts[0] != DOMAIN:
            return
        _, workout_id, token, decision = parts
        prompt = self._prompts.get(workout_id)
        if prompt is None or prompt.token != token or workout_id in self._deciding:
            return
        workout = self._manager.get_summary(workout_id)
        if prompt != self._prompt_for(workout) or not self._from_phone(
            event, prompt.device_id
        ):
            return
        if decision not in ("upload", "keep") or (
            decision == "upload" and prompt.status != "ready"
        ):
            return

        self._deciding.add(workout_id)
        try:
            if decision == "keep":
                await self._manager.async_keep_workout(workout_id)
            else:
                account = garmin_account(self.hass, prompt.garmin_entry_id)
                if account is None:
                    raise WorkoutError("The selected Garmin account is unavailable.")
                await self._manager.async_upload(workout_id, account["entity_id"])
        except WorkoutError as err:
            await self._async_update(workout_id, str(err))
        finally:
            self._deciding.discard(workout_id)

    def _from_phone(self, event: Event, device_id: str) -> bool:
        """Check the HA user attached to the Companion App webhook."""
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or event.context.user_id is None:
            return False
        return any(
            entry is not None
            and entry.domain == _MOBILE_APP
            and entry.data.get("user_id") == event.context.user_id
            for entry in (
                self.hass.config_entries.async_get_entry(entry_id)
                for entry_id in device.config_entries
            )
        )

    async def _async_send(
        self, device_id: str, message: str, data: dict, *, title: str | None = None
    ) -> bool:
        config = {
            "domain": _MOBILE_APP,
            "device_id": device_id,
            "type": "notify",
            "message": message,
            "data": data,
        }
        if title:
            config["title"] = title
        try:
            platform = await async_get_device_automation_platform(
                self.hass, _MOBILE_APP, DeviceAutomationType.ACTION
            )
            await platform.async_call_action_from_config(self.hass, config, {}, None)
        except (HomeAssistantError, InvalidDeviceAutomationConfig) as err:
            _LOGGER.warning("Could not send Concept2 PM5 workout notification: %s", err)
            return False
        return True
