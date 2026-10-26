from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ECOFLOW_DOMAIN
from .api import EcoflowApiClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    client: EcoflowApiClient = hass.data[ECOFLOW_DOMAIN][entry.entry_id]
    for device in client.devices.values():
        if hasattr(device, "calendars"):
            async_add_entities(device.calendars(client))
