"""Per-student buttons refresh the whole account."""
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    for sub in entry.runtime_data.students:
        async_add_entities([VeracrossRefreshButton(entry.runtime_data, sub.data)],
                           config_subentry_id=sub.subentry_id)


class VeracrossRefreshButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = False

    def __init__(self, coordinator, student):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{student['student_id']}_refresh"
        self._attr_name = f"Veracross {student['student_name']} Refresh"

    @property
    def available(self):
        return True

    async def async_press(self):
        await self.coordinator.async_manual_refresh()
