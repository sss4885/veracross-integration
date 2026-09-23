"""Account setup, migration, actions and authenticated websocket views."""
from datetime import timedelta
import hmac
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.components import frontend as ha_frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.loader import async_get_integration
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .api import VeracrossAPI, make_session
from .const import DOMAIN, PLATFORMS
from .coordinator import VeracrossCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def loaded(hass):
    return [e.runtime_data for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED]


def lock(coord):
    coord.active = "none"
    coord.student_name = coord.unlocked_at = None
    coord.async_update_listeners()


async def async_setup(hass, config):
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("card_registered"):
        integration = await async_get_integration(hass, DOMAIN)
        url = "/veracross/veracross-card.js"
        await hass.http.async_register_static_paths([
            StaticPathConfig(url, str(Path(__file__).parent / "frontend" / "veracross-card.js"), True)
        ])
        ha_frontend.add_extra_js_url(hass, f"{url}?v={integration.version}")
        data["card_registered"] = True

    async def enter_pin(call):
        coords = loaded(hass)
        now = dt_util.utcnow()
        eligible = [c for c in coords if not c.entry.options.get("pin_lockout", True)
                    or not c.locked_out_until or now >= c.locked_out_until]
        if coords and not eligible:
            raise ServiceValidationError("locked_out", translation_domain=DOMAIN, translation_key="locked_out")
        pin = call.data["pin"]
        for coord in eligible:
            match = None
            if coord.entry.options.get("parent_pin") and hmac.compare_digest(pin.encode(), coord.entry.options["parent_pin"].encode()):
                match = ("all", None)
            for sub in coord.students:
                if sub.data.get("pin") and hmac.compare_digest(pin.encode(), sub.data["pin"].encode()):
                    match = (sub.data["student_id"], sub.data["student_name"])
            if match:
                for other in coords:
                    lock(other)
                coord.active, coord.student_name = match
                coord.unlocked_at = now.isoformat()
                coord.locked_out_until = None
                coord.wrong_pins.clear()
                coord.async_update_listeners()
                return {"active": coord.active, "student_name": coord.student_name}
        for coord in eligible:
            coord.wrong_pins = [t for t in coord.wrong_pins if now - t < timedelta(seconds=60)]
            coord.wrong_pins.append(now)
            if coord.entry.options.get("pin_lockout", True) and len(coord.wrong_pins) >= 5:
                coord.locked_out_until = now + timedelta(seconds=60)
                lock(coord)
            coord.async_update_listeners()
        raise ServiceValidationError("wrong_pin", translation_domain=DOMAIN, translation_key="wrong_pin")

    async def action(call):
        for coord in loaded(hass):
            if call.service == "lock":
                lock(coord)
            elif call.service == "refresh":
                if not call.data.get("student_id") or call.data["student_id"] in coord.views:
                    await coord.async_manual_refresh()
            elif call.service == "purge":
                await coord.db("purge_history", call.data["before"])
                await coord.async_build_views()
                coord.async_update_listeners()

    hass.services.async_register(DOMAIN, "enter_pin", enter_pin,
        schema=vol.Schema({vol.Required("pin"): str}), supports_response=SupportsResponse.OPTIONAL)
    for name, schema in (("lock", {}), ("refresh", {vol.Optional("student_id"): str}),
                         ("purge", {vol.Required("before"): vol.All(cv.date, lambda d: d.isoformat())})):
        hass.services.async_register(DOMAIN, name, action, schema=vol.Schema(schema))
    websocket_api.async_register_command(hass, websocket_student)
    websocket_api.async_register_command(hass, websocket_students)
    return True


@websocket_api.websocket_command({vol.Required("type"): "veracross/student", vol.Required("student_id"): str})
@callback
def websocket_student(hass, connection, msg):
    sid = msg["student_id"]
    for coord in loaded(hass):
        if sid in coord.views and coord.active in (sid, "all"):
            coord.update_display_names()
            connection.send_result(msg["id"], coord.views[sid])
            return
    connection.send_error(msg["id"], "unauthorized", "Student is locked or unavailable")


@websocket_api.websocket_command({vol.Required("type"): "veracross/students"})
@callback
def websocket_students(hass, connection, msg):
    connection.send_result(msg["id"], [{"student_id": sid, "name": view["name"]}
        for coord in loaded(hass) if coord.active == "all" for sid, view in coord.views.items()])


async def async_setup_entry(hass, entry):
    api = VeracrossAPI(hass, make_session(), entry.data["username"], entry.data["password"], entry.data["school"])
    coordinator = VeracrossCoordinator(hass, entry, api)
    try:
        needs_refresh = await coordinator.async_load_cache()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await api.async_close()
        if hasattr(coordinator, "store"):
            await coordinator.db("close")
        raise
    @callback
    def registry_updated(event):
        if "name" not in event.data.get("changes", {}):
            return
        entity = er.async_get(hass).async_get(event.data["entity_id"])
        if entity and entity.config_entry_id == entry.entry_id:
            coordinator.update_display_names()
            coordinator.async_update_listeners()

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated))
    coordinator.update_display_names()
    coordinator.async_update_listeners()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if needs_refresh:
        entry.async_create_background_task(hass, coordinator.async_request_refresh(), "Veracross startup refresh")
    return True


async def _async_update_listener(hass, entry):
    # Rediscovery can update several subentries in the same event-loop turn.
    coord = entry.runtime_data
    if getattr(coord, "reload_pending", False):
        return
    coord.reload_pending = True
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_close()
    return True


async def async_migrate_entry(hass, entry):
    """Public entries start at version one; reject unknown future formats."""
    return entry.version <= 1


async def async_remove_entry(hass, entry):
    """Stop any open account and remove only its SQLite files in the executor."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and not coordinator._closing:
        await coordinator.async_close()

    def remove_files():
        path = hass.config.path("veracross", f"{entry.entry_id}.db")
        for suffix in ("", "-wal", "-shm"):
            Path(path + suffix).unlink(missing_ok=True)

    await hass.async_add_executor_job(remove_files)
