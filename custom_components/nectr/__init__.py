"""The Nectr integration.

Imports the hourly usage and cost Nectr bills you for into Home Assistant's
long-term statistics so it can be shown in the Energy dashboard.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NectrClient
from .const import CONF_DEVICE_ID, CONF_REFRESH_TOKEN
from .coordinator import NectrConfigEntry, NectrCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: NectrConfigEntry) -> bool:
    """Set up Nectr from a config entry."""

    def _save_refresh_token(token: str) -> None:
        # Nectr issues a new refresh token on every refresh and the old one
        # stops working, so persist it straight away.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = NectrClient(
        async_get_clientsession(hass),
        device_id=entry.data[CONF_DEVICE_ID],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        token_callback=_save_refresh_token,
    )
    coordinator = NectrCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NectrConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
