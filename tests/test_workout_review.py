"""Check that saved workouts require a decision before Garmin receives them."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, EventOrigin, HomeAssistant

from custom_components.c2_pm5 import async_setup
from custom_components.c2_pm5.config_flow import ConfigFlow
from custom_components.c2_pm5.const import (
    CONF_ADDRESS,
    CONF_GARMIN_ACCOUNT,
    CONF_NOTIFICATION_DEVICE,
    DOMAIN,
)
from custom_components.c2_pm5.workout_manager import WorkoutError, WorkoutManager


class WorkoutReviewTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config_directory = self.enterContext(TemporaryDirectory())
        self.hass = HomeAssistant(config_directory)
        self.hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
        self.address = "00:11:22:33:44:55"
        self.monitor = SimpleNamespace(
            data={CONF_ADDRESS: self.address}, options={}, disabled_by=None
        )
        self.account = SimpleNamespace(
            entry_id="garmin_account",
            title="Garmin account A",
            domain="garmin_connect",
            state=ConfigEntryState.LOADED,
        )
        self.phone = SimpleNamespace(
            domain="mobile_app", data={"user_id": "phone_owner"}
        )
        self.sensor = SimpleNamespace(
            entity_id="sensor.garmin_a",
            platform="garmin_connect",
            disabled_by=None,
            config_entry_id=self.account.entry_id,
        )
        self.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [self.monitor] if domain == DOMAIN else [],
            async_get_known_entry={"pm5_entry": self.monitor}.__getitem__,
            async_get_entry={
                self.account.entry_id: self.account,
                "phone_entry": self.phone,
            }.get,
        )
        entities = {self.sensor.entity_id: self.sensor}
        self.enterContext(
            patch(
                "custom_components.c2_pm5.garmin_upload.er.async_get",
                return_value=SimpleNamespace(entities=entities, async_get=entities.get),
            )
        )
        self.enterContext(
            patch(
                "custom_components.c2_pm5.workout_notifications.dr.async_get",
                return_value=SimpleNamespace(
                    async_get={
                        "phone": SimpleNamespace(config_entries={"phone_entry"})
                    }.get
                ),
            )
        )
        self.notify = AsyncMock()
        self.enterContext(
            patch(
                "custom_components.c2_pm5.workout_notifications.async_get_device_automation_platform",
                return_value=SimpleNamespace(async_call_action_from_config=self.notify),
            )
        )
        self.uploads = []

        async def receive_upload(call):
            self.assertEqual(Path(call.data["file_path"]).read_bytes()[8:12], b".FIT")
            self.uploads.append(dict(call.data))

        self.hass.services.async_register(
            "garmin_connect", "upload_activity", receive_upload
        )
        await async_setup(self.hass, {})
        self.manager = self.hass.data[DOMAIN]["workout_manager"]
        self.notifications = self.hass.data[DOMAIN]["workout_notifications"]

    async def _record(self):
        workout_id = str(uuid4())
        start = 1_700_000_000
        self.manager.record(
            {
                "workout_id": workout_id,
                "address": self.address,
                "name": "RowErg",
                "start_time": start,
                "end_time": start + 120,
                "elapsed_seconds": 120,
                "distance_m": 500,
                "partial": False,
                "samples": [{"timestamp": start + 120, "distance_m": 500}],
            }
        )
        await self.hass.async_block_till_done()
        return workout_id

    async def _enable_review(self):
        self.monitor.options = {
            CONF_GARMIN_ACCOUNT: self.account.entry_id,
            CONF_NOTIFICATION_DEVICE: "phone",
        }
        await self.notifications.async_options_updated(self.hass, self.monitor)

    def _action(self, title):
        message = self.notify.await_args.args[1]
        self.assertEqual(message["device_id"], "phone")
        return next(
            action["action"]
            for action in message["data"]["actions"]
            if action["title"] == title
        )

    async def _tap(self, action, user_id="phone_owner"):
        self.hass.bus.async_fire(
            "mobile_app_notification_action",
            {"action": action},
            EventOrigin.remote,
            context=Context(user_id=user_id),
        )
        await self.hass.async_block_till_done()

    async def test_options_can_set_and_clear_default_account_and_phone(self):
        flow = ConfigFlow.async_get_options_flow(self.monitor)
        flow.hass = self.hass
        flow.handler = "pm5_entry"
        form = await flow.async_step_init()
        selection = {
            CONF_GARMIN_ACCOUNT: self.account.entry_id,
            CONF_NOTIFICATION_DEVICE: "phone",
        }
        saved = await flow.async_step_init(form["data_schema"](selection))
        self.assertEqual(saved["data"], selection)

        self.monitor.options = saved["data"]
        form = await flow.async_step_init()
        cleared = await flow.async_step_init(
            form["data_schema"]({CONF_GARMIN_ACCOUNT: "none"})
        )
        self.assertEqual(cleared["data"], {})

    async def test_pending_workout_can_be_kept_locally_after_enabling_review(self):
        workout_id = await self._record()
        self.notify.assert_not_awaited()
        await self._enable_review()
        await self._tap(self._action("Keep locally"))
        self.assertEqual(
            self.notify.await_args.args[1]["message"], "clear_notification"
        )
        self.assertEqual(self.uploads, [])

        restarted = WorkoutManager(self.hass)
        await restarted.async_initialize()
        self.assertEqual(restarted.get_summary(workout_id)["status"], "kept_local")
        self.assertEqual((await restarted.async_export(workout_id))[8:12], b".FIT")
        with self.assertRaises(WorkoutError):
            await restarted.async_upload(workout_id)

    async def test_only_the_current_phone_prompt_can_upload(self):
        await self._enable_review()
        workout_id = await self._record()
        approval = self._action("Upload to Garmin")
        await self._tap(approval, user_id="other_user")
        self.assertEqual(self.uploads, [])

        self.monitor.options.pop(CONF_GARMIN_ACCOUNT)
        await self._tap(approval)
        self.assertEqual(self.uploads, [])
        await self.notifications.async_options_updated(self.hass, self.monitor)
        await self._enable_review()

        await self._tap(approval)
        self.assertEqual(self.uploads, [])
        approval = self._action("Upload to Garmin")
        await self._tap(approval)
        await self._tap(approval)
        self.assertEqual(self.manager.get_summary(workout_id)["status"], "uploaded")
        self.assertEqual(
            [upload["entity_id"] for upload in self.uploads], [self.sensor.entity_id]
        )

    async def test_upload_action_uses_default_when_account_is_omitted(self):
        await self._enable_review()
        workout_id = await self._record()
        await self.hass.services.async_call(
            DOMAIN, "upload_workout", {"workout_id": workout_id}, blocking=True
        )
        self.assertEqual(
            [upload["entity_id"] for upload in self.uploads], [self.sensor.entity_id]
        )
