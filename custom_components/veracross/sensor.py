"""Small state sensors; assignment data is served over websocket only."""
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .parse import default_display_name
from .class_names import effective_name


async def async_setup_entry(hass, entry, async_add_entities):
    coord = entry.runtime_data
    async_add_entities([ActiveStudentSensor(coord)])
    for sub in coord.students:
        entities = [ClassSensor(coord, sub.data, cls) for cls in sub.data["classes"]]
        entities.append(SummarySensor(coord, sub.data))
        async_add_entities(entities, config_subentry_id=sub.subentry_id)


class BaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = False
    _unrecorded_attributes = frozenset({"classes", "data_version", "throttled_until"})

    @property
    def available(self):
        # Persisted values remain useful when the portal is temporarily unavailable.
        return True


class StudentSensor(BaseSensor):
    def __init__(self, coord, student, suffix, name):
        super().__init__(coord)
        self.sid = student["student_id"]
        self._attr_unique_id = f"{coord.entry.entry_id}_{self.sid}_{suffix}"
        self._attr_name = f"Veracross {student['student_name']} {name}"

    @property
    def view(self):
        return self.coordinator.views[self.sid]


class ClassSensor(StudentSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, student, cls):
        super().__init__(coord, student, cls["id"], default_display_name(cls["portal_name"]))
        self.cls = cls
        self.shown = cls.get("show", True)
        self.cid = cls["id"]

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        registry = er.async_get(self.hass)
        entity = registry.async_get(self.entity_id)
        updates = {}
        if not self.shown and entity.hidden_by is None:
            updates["hidden_by"] = er.RegistryEntryHider.INTEGRATION
        elif self.shown and entity.hidden_by is er.RegistryEntryHider.INTEGRATION:
            updates["hidden_by"] = None
        key = f"name_migrated:{self.sid}:{self.cid}"
        if not await self.coordinator.db("get_meta", key):
            default = default_display_name(self.cls["portal_name"])
            if entity.name is None and self.cls.get("name") and self.cls["name"] != default:
                updates["name"] = self.cls["name"]
            await self.coordinator.db("set_meta", key, "1")
        if updates:
            registry.async_update_entity(self.entity_id, **updates)
        self.coordinator.update_display_names()

    @property
    def row(self):
        if not self.shown:
            return self.coordinator.hidden_classes[self.sid][self.cid]
        return next(c for c in self.view["classes"] if c["id"] == self.cid)

    @property
    def native_value(self):
        return self.row["grade"]

    @property
    def extra_state_attributes(self):
        row = self.row
        return {"class_id": self.cid, "class_name": effective_name(self.hass, self.coordinator.entry, self.sid, self.cls),
                "shown": self.shown, "data_version": self.view["data_version"],
                **{k: row[k] for k in ("teacher", "letter", "grade_source", "period", "weighting",
                                      "needs_attention_count", "late_count", "upcoming_count")}}


class SummarySensor(StudentSensor):
    def __init__(self, coord, student):
        super().__init__(coord, student, "summary", "Summary")

    @property
    def native_value(self):
        return sum(c["needs_attention_count"] for c in self.view["classes"])

    @property
    def extra_state_attributes(self):
        self.coordinator.update_display_names()
        view = self.view
        registry = er.async_get(self.hass)
        until = self.coordinator.throttled_until
        return {"student": view["name"], "student_id": self.sid, "late_mode": view["late_mode"],
                "last_success": view["last_success"], "refreshing": self.coordinator.refreshing,
                "throttled_until": until.isoformat() if until else None,
                "data_version": view["data_version"],
                "upcoming_total": sum(c["upcoming_count"] for c in view["classes"]),
                "late_total": sum(c["late_count"] for c in view["classes"]),
                "classes": [{"id": c["id"], "name": c["name"], "entity_id": registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self.coordinator.entry.entry_id}_{self.sid}_{c['id']}")}
                    for c in view["classes"]]}


class ActiveStudentSensor(BaseSensor):
    def __init__(self, coord):
        super().__init__(coord)
        self._attr_name = "Veracross Active Student"
        self._attr_unique_id = f"{coord.entry.entry_id}_active_student"

    @property
    def native_value(self):
        return self.coordinator.active

    @property
    def extra_state_attributes(self):
        coord = self.coordinator
        return {"student_name": coord.student_name, "unlocked_at": coord.unlocked_at,
                "locked_out_until": coord.locked_out_until.isoformat() if coord.locked_out_until else None}
