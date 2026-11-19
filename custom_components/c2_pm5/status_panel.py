"""Authenticated Home Assistant sidebar for rowing status and workout files."""

from base64 import b64encode
from pathlib import Path
from uuid import UUID

from .const import CONF_GARMIN_ACCOUNT, DOMAIN
from .garmin_upload import GARMIN_UPLOAD_ACTION, list_garmin_accounts
from .schema_compat import vol
from .workout_manager import WorkoutError

PANEL_PATH = "concept2-pm5"
PANEL_ELEMENT = "c2-pm5-panel"


def _workout_id(value: str) -> str:
    """Accept the canonical UUID used by a saved workout."""
    try:
        identifier = UUID(value)
    except (AttributeError, TypeError, ValueError) as err:
        raise vol.Invalid("Invalid workout ID") from err
    if identifier.version != 4 or str(identifier) != value:
        raise vol.Invalid("Invalid workout ID")
    return value


async def async_setup_panel(hass, get_devices, manager) -> None:
    """Register the status page and its administrator-only data commands."""
    from homeassistant.components import frontend, panel_custom, websocket_api
    from homeassistant.components.http import StaticPathConfig

    if frontend.async_panel_exists(hass, PANEL_PATH):
        return

    @websocket_api.websocket_command({"type": f"{DOMAIN}/status"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def status(hass, connection, message):
        connection.send_result(message["id"], {"devices": get_devices()})

    @websocket_api.websocket_command({"type": f"{DOMAIN}/workouts/list"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def list_workouts(hass, connection, message):
        workouts = manager.list_workouts()
        for workout in workouts:
            options = manager.workout_options(workout["address"])
            workout["default_garmin_account"] = options.get(CONF_GARMIN_ACCOUNT)
        connection.send_result(
            message["id"],
            {
                "workouts": workouts,
                "storage_error": manager.last_error,
                "garmin_accounts": list_garmin_accounts(hass),
                "garmin_upload_available": hass.services.has_service(
                    *GARMIN_UPLOAD_ACTION
                ),
            },
        )

    @websocket_api.websocket_command(
        {"type": f"{DOMAIN}/workouts/download", vol.Required("workout_id"): _workout_id}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def download_workout(hass, connection, message):
        workout_id = message["workout_id"]
        try:
            activity = await manager.async_export(workout_id)
        except WorkoutError as err:
            connection.send_error(message["id"], "file_unavailable", str(err))
        else:
            connection.send_result(
                message["id"],
                {
                    "filename": f"concept2-{workout_id}.fit",
                    "content": b64encode(activity).decode("ascii"),
                },
            )

    url = "/c2_pm5_static/c2-pm5-panel.js"
    path = Path(__file__).parent / "frontend" / "c2-pm5-panel.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(url, str(path), False)]
    )
    for command in (status, list_workouts, download_workout):
        websocket_api.async_register_command(hass, command)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="Concept2 PM5",
        sidebar_icon="mdi:rowing",
        module_url=url,
        embed_iframe=False,
        trust_external=False,
        require_admin=True,
    )
