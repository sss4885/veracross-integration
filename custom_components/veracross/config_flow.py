"""Discover accounts and configure student subentries."""
from __future__ import annotations
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigSubentryFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector, entity_registry as er

from .api import VeracrossAPI, make_session, VeracrossAuthError, VeracrossCaptchaError, VeracrossError
from .const import DOMAIN
from .parse import default_display_name
from .class_names import class_entity_id, effective_name

_PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


async def discover(hass, data):
    api = VeracrossAPI(hass, make_session(), data["username"], data["password"], data["school"])
    try:
        await api.login()
        return await api.discover()
    finally:
        await api.async_close()


def error_key(err):
    if isinstance(err, VeracrossCaptchaError):
        return "captcha"
    if isinstance(err, VeracrossAuthError):
        return "invalid_auth"
    return "cannot_connect"


def selection(options, multiple=True):
    return selector.SelectSelector(selector.SelectSelectorConfig(options=options, multiple=multiple,
                                                                 mode=selector.SelectSelectorMode.LIST))


def classes_schema(child):
    return vol.Schema({vol.Required("classes", default=[c["id"] for c in child["classes"]
        if child.get("graded", {}).get(c["id"], False)]): selection([
            {"value": c["id"], "label": c["portal_name"]} for c in child["classes"]])})


def student_data(child, selected):
    return {"student_id": child["student_id"], "student_name": child["name"],
            "classes": [{"id": c["id"], "portal_name": c["portal_name"],
                         "name": default_display_name(c["portal_name"]), "show": c["id"] in selected}
                        for c in child["classes"]],
            "late_mode": "separate", "pin": None, "upcoming_days": 7}


def pin_error(pin, entry, exclude=None, parent=False):
    if not pin:
        return None
    if not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 8:
        return "invalid_pin"
    pins = [s.data.get("pin") for s in entry.subentries.values() if s.subentry_id != exclude]
    if not parent:
        pins.append(entry.options.get("parent_pin"))
    return "duplicate_pin" if pin in pins else None


def class_key(cls):
    """Stable class ID plus a readable portal label for dynamic HA form fields."""
    return f"{cls['id']}: {cls['portal_name']}"


class VeracrossConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self.children = await discover(self.hass, user_input)
            except VeracrossError as err:
                errors["base"] = error_key(err)
            else:
                if not self.children:
                    errors["base"] = "no_students"
                else:
                    await self.async_set_unique_id(f"{user_input['school']}_{user_input['username']}")
                    self._abort_if_unique_id_configured()
                    self.account = user_input
                    return await self.async_step_students()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("username"): str, vol.Required("password"): _PASSWORD,
            vol.Required("school"): vol.All(str, vol.Length(min=1), vol.Match(r"^[A-Za-z0-9_-]+$"))}))

    async def async_step_students(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.pending = [c for c in self.children if c["student_id"] in user_input["students"]]
            if self.pending:
                self.subentries = []
                return await self.async_step_classes()
            errors["base"] = "select_student"
        return self.async_show_form(step_id="students", errors=errors, data_schema=vol.Schema({
            vol.Required("students"): selection([{"value": c["student_id"], "label": c["name"]}
                                                  for c in self.children])}))

    async def async_step_classes(self, user_input=None):
        child = self.pending[0]
        if user_input is not None:
            data = student_data(child, user_input["classes"])
            self.subentries.append({"subentry_type": "student", "title": child["name"],
                                    "unique_id": child["student_id"], "data": data})
            self.pending.pop(0)
            if self.pending:
                return await self.async_step_classes()
            return self.async_create_entry(title=f"Veracross {self.account['username']}", data=self.account,
                options={"parent_pin": None, "pin_lockout": True}, subentries=self.subentries)
        return self.async_show_form(step_id="classes", data_schema=classes_schema(child),
                                    description_placeholders={"student": child["name"]})

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            try:
                await discover(self.hass, {**entry.data, **user_input})
            except VeracrossError as err:
                errors["base"] = error_key(err)
            else:
                if entry.update_listeners:
                    return self.async_update_and_abort(entry, data_updates=user_input)
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(step_id="reauth_confirm", errors=errors,
                                    data_schema=vol.Schema({vol.Required("password"): _PASSWORD}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return VeracrossOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"student": StudentSubentryFlow}


class StudentSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        errors = {}
        if not hasattr(self, "children"):
            try:
                children = await discover(self.hass, self._get_entry().data)
            except VeracrossError as err:
                return self.async_show_form(step_id="user", data_schema=vol.Schema({}),
                                            errors={"base": error_key(err)})
            added = {s.unique_id for s in self._get_entry().subentries.values()}
            self.children = [c for c in children if c["student_id"] not in added]
            if not self.children and not errors:
                return self.async_abort(reason="no_students")
        if user_input and "student_id" in user_input:
            self.child = next(c for c in self.children if c["student_id"] == user_input["student_id"])
            return await self.async_step_classes()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("student_id"): selection([{"value": c["student_id"], "label": c["name"]}
                                                   for c in self.children], multiple=False)}))

    async def async_step_classes(self, user_input=None):
        if user_input is not None:
            self.settings = student_data(self.child, user_input["classes"])
            return self.async_create_entry(title=self.settings["student_name"],
                unique_id=self.settings["student_id"], data=self.settings)
        return self.async_show_form(step_id="classes", data_schema=classes_schema(self.child),
                                    description_placeholders={"student": self.child["name"]})

    async def async_step_reconfigure(self, user_input=None):
        self.settings = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_settings(user_input)

    async def async_step_settings(self, user_input=None):
        errors = {}
        cfg = self.settings
        entry = self._get_entry()
        entities = {entity_id: cls for cls in cfg["classes"]
                    if (entity_id := class_entity_id(self.hass, entry, cfg["student_id"], cls))}
        if user_input is not None:
            pin = user_input.get("pin") or None
            selected = user_input["classes_order"]
            if error := pin_error(pin, entry, self._get_reconfigure_subentry().subentry_id):
                errors["pin"] = error
            elif not selected:
                errors["classes_order"] = "select_class"
            elif len(set(selected)) != len(selected) or any(e not in entities for e in selected):
                errors["classes_order"] = "invalid_class"
            else:
                chosen = [entities[e] for e in selected]
                ordered = chosen + [c for c in cfg["classes"] if c not in chosen]
                self.settings = {**cfg, "pin": pin, "late_mode": user_input["late_mode"],
                    "upcoming_days": user_input["upcoming_days"],
                    "classes": [{**c, "show": c in chosen} for c in ordered]}
                return await self.async_step_names()
        schema = {
            vol.Required("classes_order", default=[e for e, c in entities.items() if c.get("show", True)]):
                selector.EntitySelector(selector.EntitySelectorConfig(
                    multiple=True, reorder=True, include_entities=list(entities),
                    domain="sensor", integration=DOMAIN)),
            vol.Required("late_mode", default=cfg["late_mode"]): selection(["separate", "listed", "counted"], False),
            vol.Optional("pin", default=cfg.get("pin") or ""): _PASSWORD,
            vol.Required("upcoming_days", default=cfg.get("upcoming_days", 7)):
                vol.All(vol.Coerce(int), vol.Range(min=0))}
        return self.async_show_form(step_id="settings", data_schema=self.add_suggested_values_to_schema(
            vol.Schema(schema), user_input), errors=errors, description_placeholders={"student": cfg["student_name"]})

    async def async_step_names(self, user_input=None):
        cfg = self.settings
        entry = self._get_entry()
        shown = [c for c in cfg["classes"] if c["show"]]
        if user_input is not None:
            registry = er.async_get(self.hass)
            for cls in shown:
                default = default_display_name(cls["portal_name"])
                name = user_input[class_key(cls)].strip()
                entity_id = class_entity_id(self.hass, entry, cfg["student_id"], cls)
                registry.async_update_entity(entity_id, name=None if not name or name == default else name)
                # Retire the legacy override so clearing the registry name uses the default.
                cls["name"] = default
            return self.async_update_and_abort(entry, self._get_reconfigure_subentry(), data=cfg)
        return self.async_show_form(step_id="names", data_schema=vol.Schema({
            vol.Required(class_key(c), default=effective_name(self.hass, entry, cfg["student_id"], c)):
                selector.TextSelector() for c in shown}),
            description_placeholders={"student": cfg["student_name"]})


class VeracrossOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        entry = self.config_entry
        if user_input is not None:
            pin = user_input.get("parent_pin") or None
            if error := pin_error(pin, entry, parent=True):
                errors["parent_pin"] = error
            else:
                try:
                    if user_input.get("rediscover"):
                        children = {c["student_id"]: c for c in await discover(self.hass, entry.data)}
                        for sub in entry.subentries.values():
                            if sub.unique_id not in children:
                                continue
                            classes = list(sub.data["classes"])
                            existing = {c["id"] for c in classes}
                            for cls in children[sub.unique_id]["classes"]:
                                if cls["id"] not in existing:
                                    classes.append({"id": cls["id"], "portal_name": cls["portal_name"],
                                                    "name": default_display_name(cls["portal_name"]), "show": False})
                            self.hass.config_entries.async_update_subentry(
                                entry, sub, data={**sub.data, "classes": classes})
                except VeracrossError as err:
                    errors["base"] = error_key(err)
                else:
                    return self.async_create_entry(title="", data={"parent_pin": pin, "pin_lockout": user_input["pin_lockout"]})
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Optional("parent_pin", default=entry.options.get("parent_pin") or ""): _PASSWORD,
            vol.Required("pin_lockout", default=entry.options.get("pin_lockout", True)): bool,
            vol.Required("rediscover", default=False): bool}))
