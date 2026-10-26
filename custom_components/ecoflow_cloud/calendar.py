from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ECOFLOW_DOMAIN
from .api import EcoflowApiClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    client: EcoflowApiClient = hass.data[ECOFLOW_DOMAIN][entry.entry_id]
    for device in client.devices.values():
        async_add_entities(device.calendars(client))

    entity_platform.async_get_current_platform().async_register_entity_service(
        "get_schedule", None, "async_get_schedule", supports_response=SupportsResponse.ONLY
    )
