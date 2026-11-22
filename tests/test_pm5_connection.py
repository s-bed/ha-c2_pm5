"""Exercise wake-up, retry and cooldown behavior without a Bluetooth adapter."""

import asyncio
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from custom_components.c2_pm5 import pm5_ble
from custom_components.c2_pm5.const import DEVICE_STALE_SECONDS


class PM5ConnectionTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = pm5_ble.PM5BleSession(
            Mock(), "RowErg", "00:11:22:33:44:55", Mock(), Mock()
        )
        self.ad = SimpleNamespace(time=time.monotonic(), source="proxy", rssi=-60)
        self.cache = self.enterContext(
            patch.object(
                pm5_ble.bluetooth, "async_last_service_info", return_value=self.ad
            )
        )
        self.addAsyncCleanup(self.session.stop)

    async def test_old_replay_is_ignored_then_fresh_advertisement_wakes_immediately(self):
        self.ad.time -= DEVICE_STALE_SECONDS + 1
        self.session._async_discovered_device(self.ad, None)
        self.assertFalse(self.session.is_discoverable)
        attempted = asyncio.Event()

        async def connect():
            attempted.set()
            await self.session._stop.wait()

        with patch.object(
            self.session, "_connect_and_listen", side_effect=connect
        ) as connect_mock:
            self.session._task = asyncio.create_task(self.session._run())
            await asyncio.sleep(0)
            connect_mock.assert_not_awaited()
            self.ad.time = time.monotonic()
            self.session._async_discovered_device(self.ad, None)
            async with asyncio.timeout(0.5):
                await attempted.wait()
            self.assertTrue(self.session.is_discoverable)
            connect_mock.assert_awaited_once()

    async def test_watchdog_can_recognize_an_unchanged_advertisement(self):
        # HA's cache timestamp advances even if the payload is the same and its
        # change-only registration callback was never called.
        self.ad.time -= DEVICE_STALE_SECONDS + 1
        self.session._refresh_availability()
        self.assertFalse(self.session.is_discoverable)
        self.ad.time = time.monotonic()
        self.session._avail_task = asyncio.create_task(
            self.session._availability_watchdog()
        )
        await asyncio.sleep(0)
        self.assertTrue(self.session.is_discoverable)
        self.cache.return_value = None
        self.session._refresh_availability()
        self.assertFalse(self.session.is_discoverable)

    async def test_cooldown_requires_an_advertisement_observed_after_expiry(self):
        now = time.monotonic()
        self.session._reconnect_after = now + 60
        self.session._async_discovered_device(self.ad, None)
        self.assertFalse(self.session.is_discoverable)
        self.session._reconnect_after = now - 1
        self.ad.time = now - 2
        self.session._refresh_availability()
        self.assertFalse(self.session.is_discoverable)
        self.ad.time = now
        self.session._refresh_availability()
        self.assertTrue(self.session.is_discoverable)

    async def test_repeated_failures_back_off_until_true_rediscovery(self):
        delays = []
        at_maximum = asyncio.Event()
        restarted = asyncio.Event()

        async def wait_for_change(waiter, timeout):
            delays.append(timeout)
            if len(delays) <= 6:
                waiter.close()
                raise TimeoutError
            if len(delays) == 7:
                at_maximum.set()
            else:
                restarted.set()
            await waiter

        connect_mock = AsyncMock(
            side_effect=pm5_ble.BleakOutOfConnectionSlotsError("busy")
        )
        with (
            patch.object(self.session, "_connect_and_listen", connect_mock),
            patch.object(pm5_ble.asyncio, "wait_for", side_effect=wait_for_change),
            self.assertLogs(pm5_ble.__name__, "DEBUG") as log,
        ):
            self.session._task = asyncio.create_task(self.session._run())
            async with asyncio.timeout(1):
                await at_maximum.wait()
                self.session._async_discovered_device(self.ad, None)
                await asyncio.sleep(0)
                self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30])

                # Both transitions can arrive before the retry loop is scheduled.
                self.ad.time -= DEVICE_STALE_SECONDS + 1
                self.session._refresh_availability()
                self.ad.time = time.monotonic()
                self.session._async_discovered_device(self.ad, None)
                await restarted.wait()
                await self.session.stop()
        self.assertEqual(delays[-1], 1)
        self.assertEqual(connect_mock.await_count, 8)
        self.assertTrue(
            any("BleakOutOfConnectionSlotsError: busy" in line for line in log.output)
        )

    async def test_status_messages_only_log_screen_and_workout_state_changes(self):
        status = bytearray(19)
        screen = bytearray.fromhex("3e020165000000")
        with self.assertLogs(pm5_ble.__name__, "DEBUG") as log:
            self.session._handle_0080("", screen)
            self.session._handle_0080("", screen)
            screen[3] += 1
            self.session._handle_0080("", screen)
            self.session._handle_0031("", status)
            self.session._handle_0031("", status)
            status[8] = 1
            self.session._handle_0031("", status)
        self.assertEqual(sum("PM5 operational state" in line for line in log.output), 2)
        self.assertEqual(
            sum("First PM5 general status" in line for line in log.output), 1
        )
        self.assertEqual(
            sum("PM5 workout state changed" in line for line in log.output), 1
        )

    async def test_late_notification_from_old_link_is_ignored(self):
        client = self.session._client = AsyncMock()
        handler = Mock()
        await self.session._start_notify(client, set(), "test", handler)
        _, forward = client.start_notify.await_args.args
        packet = bytearray([1, 2])
        forward(None, packet)
        self.session._client = Mock()
        forward(None, packet)
        handler.assert_called_once_with("test", packet)
