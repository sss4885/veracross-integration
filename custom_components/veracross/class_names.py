"""Class identity and display names shared by forms and entities."""
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .parse import default_display_name


def class_entity_id(hass, entry, sid, cls):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{sid}_{cls['id']}")


def effective_name(hass, entry, sid, cls):
    entity_id = class_entity_id(hass, entry, sid, cls)
    entity = er.async_get(hass).async_get(entity_id) if entity_id else None
    if entity is not None and entity.name is not None:
        return entity.name
    return cls.get("name") or default_display_name(cls["portal_name"])
