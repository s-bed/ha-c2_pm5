"""Store recorded workouts and guard explicit Garmin upload attempts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_ADDRESS, CONF_GARMIN_ACCOUNT, DOMAIN, SIGNAL_WORKOUT_UPDATED
from .fit_export import encode_workout
from .garmin_upload import (
    GARMIN_UPLOAD_ACTION,
    async_upload_workout,
    garmin_account,
    garmin_account_ready,
)

_LOGGER = logging.getLogger(__name__)
_UNCERTAIN = "The upload outcome is uncertain. Check Garmin before retrying."
REVIEW_STATUSES = frozenset({"ready", "needs_review"})
_RESOLVED_STATUSES = ("uploaded", "kept_local")


class WorkoutError(Exception):
    """A workout could not be read or its upload could not be authorized."""


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace a private file only after the new data has been written."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(temp.name)
    try:
        with temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class WorkoutManager:
    """Keep completed recordings independently of Bluetooth and Garmin."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._directory = Path(hass.config.path(".storage", f"{DOMAIN}_workouts"))
        self._workouts: dict[str, dict] = {}
        self._unsaved: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def async_initialize(self) -> None:
        self._workouts, self.last_error = await self.hass.async_add_executor_job(
            self._load_summaries
        )

    def _load_summaries(self) -> tuple[dict[str, dict], str | None]:
        workouts = {}
        error = None
        for path in self._directory.glob("*.jsonl"):
            try:
                workout_id = path.stem
                if str(UUID(workout_id, version=4)) != workout_id:
                    continue
                with path.open(encoding="utf-8") as recording:
                    summary = json.loads(recording.readline())
                if (
                    not isinstance(summary, dict)
                    or summary.get("workout_id") != workout_id
                ):
                    raise ValueError("Invalid workout summary")

                summary.update(status="ready", error=None, upload_attempt=0)
                try:
                    upload = json.loads(
                        self._upload_path(workout_id).read_text("utf-8")
                    )
                except FileNotFoundError:
                    pass
                except (ValueError, OSError):
                    # An unreadable upload record may stand for a completed send.
                    summary.update(status="needs_review", error=_UNCERTAIN)
                    _LOGGER.exception(
                        "Could not read upload status for PM5 workout %s", workout_id
                    )
                else:
                    if (
                        isinstance(upload, dict)
                        and upload.get("status") in _RESOLVED_STATUSES
                    ):
                        summary.update(upload)
                    else:
                        if isinstance(upload, dict):
                            summary.update(upload)
                        summary.update(status="needs_review", error=_UNCERTAIN)

                workouts[workout_id] = summary
            except (OSError, ValueError):
                error = "One or more saved workouts could not be read."
                _LOGGER.exception("Could not read PM5 workout %s", path.name)
        return workouts, error

    def _upload_path(self, workout_id: str) -> Path:
        return self._directory / f"{workout_id}.upload.json"

    def _get_summary(self, workout_id: str) -> dict:
        try:
            return self._workouts[workout_id]
        except KeyError as err:
            raise WorkoutError("Workout not found.") from err

    def get_summary(self, workout_id: str) -> dict:
        """Return the current saved review state for one workout."""
        return dict(self._get_summary(workout_id))

    def workout_options(self, address: str) -> Mapping:
        """Read current review settings for the monitor that recorded a workout."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.disabled_by is None and entry.data[CONF_ADDRESS] == address:
                return entry.options
        return {}

    def list_workouts(self) -> list[dict]:
        """Return compact history for the status panel."""
        return [
            dict(summary)
            for summary in sorted(
                self._workouts.values(),
                key=lambda workout: workout["start_time"],
                reverse=True,
            )
        ]

    def record(self, workout: dict) -> None:
        """Queue a new recording for an immediate private write."""
        workout_id = workout["workout_id"]
        self._unsaved[workout_id] = dict(workout, time_zone=self.hass.config.time_zone)
        self.hass.async_create_task(
            self.async_flush(), f"Save PM5 workout {workout_id}"
        )

    async def async_flush(self) -> None:
        async with self._lock:
            for workout_id, workout in list(self._unsaved.items()):
                if workout_id not in self._workouts:
                    summary = {
                        key: value for key, value in workout.items() if key != "samples"
                    }
                    try:
                        await self.hass.async_add_executor_job(
                            self._write_recording,
                            workout_id,
                            summary,
                            workout["samples"],
                        )
                    except (OSError, ValueError):
                        _LOGGER.exception("Could not save PM5 workout %s", workout_id)
                        continue
                    self._workouts[workout_id] = dict(
                        summary, status="ready", error=None, upload_attempt=0
                    )
                    async_dispatcher_send(self.hass, SIGNAL_WORKOUT_UPDATED, workout_id)
                del self._unsaved[workout_id]
            if self._unsaved:
                self.last_error = "A completed workout could not be saved."
            elif self.last_error == "A completed workout could not be saved.":
                self.last_error = None

    def _write_recording(
        self, workout_id: str, summary: dict, samples: list[dict]
    ) -> None:
        # Read only the first line when loading the history at startup.
        header = json.dumps(summary, separators=(",", ":"), allow_nan=False)
        track = json.dumps(samples, separators=(",", ":"), allow_nan=False)
        _atomic_write(
            self._directory / f"{workout_id}.jsonl", f"{header}\n{track}\n".encode()
        )

    def _read_samples(self, workout_id: str) -> list[dict]:
        with (self._directory / f"{workout_id}.jsonl").open(
            encoding="utf-8"
        ) as recording:
            recording.readline()
            return json.load(recording)

    async def async_get_workout(self, workout_id: str) -> dict:
        summary = self.get_summary(workout_id)
        try:
            samples = await self.hass.async_add_executor_job(
                self._read_samples, workout_id
            )
        except (OSError, ValueError) as err:
            raise WorkoutError("The saved workout samples could not be read.") from err
        return dict(summary, samples=samples)

    async def async_export(self, workout_id: str) -> bytes:
        workout = await self.async_get_workout(workout_id)
        try:
            return await self.hass.async_add_executor_job(
                encode_workout, workout, workout["time_zone"]
            )
        except (ValueError, OverflowError) as err:
            raise WorkoutError("The workout could not be encoded as FIT.") from err

    async def _save_upload_state(self, workout_id: str, state: dict) -> None:
        # Store.async_save may log write errors without raising them. Here a
        # confirmed write is needed before starting the external service call.
        await self.hass.async_add_executor_job(
            _atomic_write, self._upload_path(workout_id), json.dumps(state).encode()
        )
        self._workouts[workout_id].update(state)

    async def async_upload(
        self,
        workout_id: str,
        entity_id: str | None = None,
        retry: bool = False,
        upload_attempt: int | None = None,
    ) -> dict:
        """Send one approved FIT file to the selected Garmin account."""
        async with self._lock:
            summary = self._get_summary(workout_id)
            if summary["status"] in ("uploading", "uploaded"):
                return dict(summary)
            if summary["status"] == "kept_local":
                raise WorkoutError("This workout was marked to be kept locally.")
            if summary["status"] == "needs_review" and (
                not retry or upload_attempt != summary["upload_attempt"]
            ):
                raise WorkoutError(_UNCERTAIN)
            if not self.hass.services.has_service(*GARMIN_UPLOAD_ACTION):
                raise WorkoutError("The Garmin Connect upload action is unavailable.")
            if entity_id is None:
                if summary["status"] == "needs_review":
                    # A retry without an override stays on the original destination.
                    entity_id = summary.get("garmin_entity_id")
                else:
                    options = self.workout_options(summary["address"])
                    account = garmin_account(
                        self.hass, options.get(CONF_GARMIN_ACCOUNT)
                    )
                    entity_id = account["entity_id"] if account else None
            if entity_id is None:
                raise WorkoutError(
                    "Select a Garmin Connect account or configure a default."
                )
            if not garmin_account_ready(self.hass, entity_id):
                raise WorkoutError("Select an available Garmin Connect account.")

            fit = await self.async_export(workout_id)
            path = self._directory / f"{workout_id}.fit"
            attempt = {
                "status": "uploading",
                "error": None,
                "upload_attempt": summary["upload_attempt"] + 1,
                "garmin_entity_id": entity_id,
            }
            try:
                await self.hass.async_add_executor_job(_atomic_write, path, fit)
                await self._save_upload_state(workout_id, attempt)
            except OSError as err:
                raise WorkoutError(
                    "The upload could not be prepared in local storage."
                ) from err

        try:
            await async_upload_workout(self.hass, path, entity_id)
        except asyncio.CancelledError:
            await self._finish_upload(
                workout_id, dict(attempt, status="needs_review", error=_UNCERTAIN)
            )
            raise
        except Exception as err:
            _LOGGER.warning(
                "Garmin upload failed for PM5 workout %s", workout_id, exc_info=True
            )
            await self._finish_upload(
                workout_id, dict(attempt, status="needs_review", error=_UNCERTAIN)
            )
            raise WorkoutError(_UNCERTAIN) from err

        await self._finish_upload(workout_id, dict(attempt, status="uploaded"))
        return dict(self._get_summary(workout_id))

    async def async_keep_workout(self, workout_id: str) -> None:
        """Decline a Garmin upload and retain the recording and its FIT export."""
        async with self._lock:
            summary = self._get_summary(workout_id)
            if summary["status"] == "kept_local":
                return
            if summary["status"] not in REVIEW_STATUSES:
                raise WorkoutError(
                    "A workout already uploading or sent cannot be skipped."
                )
            state = {
                "status": "kept_local",
                "error": None,
                "upload_attempt": summary["upload_attempt"],
            }
            if "garmin_entity_id" in summary:
                state["garmin_entity_id"] = summary["garmin_entity_id"]
            try:
                await self._save_upload_state(workout_id, state)
            except OSError as err:
                raise WorkoutError("The review decision could not be saved.") from err
            async_dispatcher_send(self.hass, SIGNAL_WORKOUT_UPDATED, workout_id)

    async def _finish_upload(self, workout_id: str, state: dict) -> None:
        async with self._lock:
            try:
                await self._save_upload_state(workout_id, state)
            except OSError as err:
                _LOGGER.exception(
                    "Could not save upload status for PM5 workout %s", workout_id
                )
                self._workouts[workout_id].update(
                    status="needs_review", error=_UNCERTAIN
                )
                raise WorkoutError(_UNCERTAIN) from err
            finally:
                async_dispatcher_send(self.hass, SIGNAL_WORKOUT_UPDATED, workout_id)
