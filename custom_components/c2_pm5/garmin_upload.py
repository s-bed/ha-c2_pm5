"""Find Garmin Connect destinations and send approved FIT files."""

from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

GARMIN_DOMAIN = "garmin_connect"
GARMIN_UPLOAD_SERVICE = "upload_activity"
GARMIN_UPLOAD_ACTION = (GARMIN_DOMAIN, GARMIN_UPLOAD_SERVICE)


def _account_entry(hass: HomeAssistant, entity: er.RegistryEntry) -> ConfigEntry | None:
    if (
        entity.platform != GARMIN_DOMAIN
        or not entity.entity_id.startswith("sensor.")
        or entity.disabled_by is not None
        or entity.config_entry_id is None
    ):
        return None
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    return entry if entry is not None and entry.domain == GARMIN_DOMAIN else None


@callback
def garmin_account_ready(hass: HomeAssistant, entity_id: str) -> bool:
    """Return whether an enabled sensor belongs to a loaded Garmin account."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None:
        return False
    entry = _account_entry(hass, entity)
    return entry is not None and entry.state is ConfigEntryState.LOADED


@callback
def list_garmin_accounts(hass: HomeAssistant) -> list[dict]:
    """Provide one representative sensor per Garmin Connect config entry."""
    accounts = {}
    for entity in er.async_get(hass).entities.values():
        entry = _account_entry(hass, entity)
        if entry is None:
            continue
        sensor = hass.states.get(entity.entity_id)
        entity_ids = accounts.setdefault(entry.entry_id, (entry, []))[1]
        entity_ids.append(
            (
                sensor is None or sensor.state in {"unavailable", "unknown"},
                entity.entity_id,
            )
        )

    result = []
    for entry, sensors in accounts.values():
        _, entity_id = min(sensors)
        result.append(
            {
                "entry_id": entry.entry_id,
                "entity_id": entity_id,
                "entity_ids": sorted(entity_id for _, entity_id in sensors),
                "name": entry.title or entity_id,
                "available": entry.state is ConfigEntryState.LOADED,
            }
        )
    return sorted(
        result, key=lambda account: (account["name"].casefold(), account["entity_id"])
    )


@callback
def garmin_account(hass: HomeAssistant, entry_id: str | None) -> dict | None:
    """Resolve the current upload sensor for a saved account preference."""
    if entry_id is None:
        return None
    return next(
        (
            account
            for account in list_garmin_accounts(hass)
            if account["entry_id"] == entry_id
        ),
        None,
    )


async def async_upload_workout(
    hass: HomeAssistant, fit_path: str | Path, entity_id: str
) -> None:
    """Use the selected Garmin Connect integration's authenticated upload action."""
    await hass.services.async_call(
        GARMIN_DOMAIN,
        GARMIN_UPLOAD_SERVICE,
        {"file_path": str(fit_path), "entity_id": entity_id},
        blocking=True,
    )
