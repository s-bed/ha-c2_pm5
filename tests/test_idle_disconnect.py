"""Exercise the PM5 idle disconnect setting without a Bluetooth adapter."""

import asyncio
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry

from custom_components.c2_pm5 import PM5Coordinator, config_flow, number, pm5_ble, sensor
from custom_components.c2_pm5.const import (
    CONF_ADDRESS,
    CONF_DEVICE_TYPE,
    CONF_GARMIN_ACCOUNT,
    CONF_IDLE_DISCONNECT_MINUTES,
    CONF_NOTIFICATION_DEVICE,
    DOMAIN,
)


class IdleDisconnectTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = self.enterContext(TemporaryDirectory())
        self.hass = HomeAssistant(self.config_dir)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        await issue_registry.async_load(self.hass, load_empty=True)
        self.address = "00:11:22:33:44:55"
        self.entry = ConfigEntry(
            data={CONF_ADDRESS: self.address, CONF_DEVICE_TYPE: "RowErg"},
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=1,
            options={CONF_NOTIFICATION_DEVICE: "phone"},
            source="user",
            subentries_data=None,
            title="RowErg",
            unique_id=self.address,
            version=1,
        )
        with patch.object(
            self.hass.config_entries, "async_setup", new_callable=AsyncMock
        ):
            await self.hass.config_entries.async_add(self.entry)

        coordinator = PM5Coordinator(
            self.hass, self.entry, self.address, "RowErg", Mock()
        )
        self.addCleanup(coordinator.close)
        self.hass.data[DOMAIN] = {self.entry.entry_id: coordinator}
        self.session = coordinator.session
        add_entities = Mock()
        await number.async_setup_entry(self.hass, self.entry, add_entities)
        [self.control] = add_entities.call_args.args[0]
        self.control.hass = self.hass
        self.control.async_write_ha_state = Mock()
        self.monitor_sensor = sensor.PM5Sensor(coordinator, sensor.SENSORS[0])

    async def test_control_uses_same_device_and_persists_whole_minutes(self):
        self.assertEqual(self.control.device_info, self.monitor_sensor.device_info)
        self.assertEqual(self.control.entity_category, EntityCategory.CONFIG)
        self.assertEqual(self.control.native_unit_of_measurement, UnitOfTime.MINUTES)
        self.assertEqual(self.control.native_step, 1)
        self.assertTrue(self.control.available)
        self.assertEqual(self.control.native_value, 5)

        for value in (0, 2.5, 61):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await self.control.async_set_native_value(value)
        self.assertNotIn(CONF_IDLE_DISCONNECT_MINUTES, self.entry.options)

        await self.control.async_set_native_value(8.0)
        self.control.async_write_ha_state.assert_called_once_with()
        self.assertEqual(self.control.native_value, 8)
        self.assertEqual(self.entry.options[CONF_NOTIFICATION_DEVICE], "phone")

        self.hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await self.hass.async_block_till_done()
        restarted = HomeAssistant(self.config_dir)
        restarted.config_entries = ConfigEntries(restarted, {})
        await restarted.config_entries.async_initialize()
        saved_entry = restarted.config_entries.async_get_known_entry(self.entry.entry_id)
        self.assertEqual(saved_entry.options[CONF_IDLE_DISCONNECT_MINUTES], 8)

    async def test_watchdog_disconnects_at_five_idle_minutes_by_default(self):
        idle = asyncio.Event()
        self.session._last_active_monotonic = 60
        clock = SimpleNamespace(monotonic=Mock(side_effect=[359, 360]))

        with (
            patch.object(pm5_ble, "time", clock),
            patch.object(pm5_ble.asyncio, "sleep", new_callable=AsyncMock) as tick,
            self.assertLogs(pm5_ble.__name__, "WARNING"),
        ):
            await self.session._idle_watchdog(idle)

        self.assertTrue(idle.is_set())
        self.assertEqual(tick.await_count, 2)

    async def test_watchdog_applies_setting_change_to_existing_connection(self):
        idle = asyncio.Event()
        self.session._last_active_monotonic = 60

        async def check_again(_delay):
            self.assertLessEqual(tick.await_count, 2)
            if tick.await_count == 2:
                self.assertFalse(idle.is_set())
                await self.control.async_set_native_value(2)

        with (
            patch.object(pm5_ble, "time", SimpleNamespace(monotonic=lambda: 200)),
            patch.object(pm5_ble.asyncio, "sleep", side_effect=check_again) as tick,
            self.assertLogs(pm5_ble.__name__, "WARNING"),
        ):
            await self.session._idle_watchdog(idle)

        self.assertTrue(idle.is_set())
        self.assertEqual(tick.await_count, 2)

    async def test_saving_workout_options_preserves_latest_timeout(self):
        await self.control.async_set_native_value(3)
        flow = config_flow.ConfigFlow.async_get_options_flow(self.entry)
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        with patch.object(config_flow, "list_garmin_accounts", return_value=[]):
            form = await flow.async_step_init()

        await self.control.async_set_native_value(4)
        selection = form["data_schema"]({CONF_GARMIN_ACCOUNT: "none"})
        saved = await flow.async_step_init(selection)
        self.assertEqual(saved["data"], {CONF_IDLE_DISCONNECT_MINUTES: 4})
