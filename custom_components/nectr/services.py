"""Actions for Nectr."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import NectrAuthError, NectrError
from .const import ATTR_END_DATE, ATTR_START_DATE, DOMAIN, SERVICE_REIMPORT
from .coordinator import NectrConfigEntry

REIMPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
    }
)


def _get_entry(hass: HomeAssistant, entry_id: str) -> NectrConfigEntry:
    entry: NectrConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


async def _async_reimport(call: ServiceCall) -> ServiceResponse:
    """Fetch a range of days from Nectr again and rewrite their statistics."""
    entry = _get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = entry.runtime_data
    today = dt_util.now(coordinator.tz).date()
    start = call.data[ATTR_START_DATE]
    end = min(call.data.get(ATTR_END_DATE, today), today)
    if start > end:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_date_range"
        )

    try:
        result = await coordinator.async_reimport(start, end)
    except NectrAuthError as err:
        entry.async_start_reauth(call.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except NectrError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="update_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hours": result.hours,
        "days_without_data": [d.isoformat() for d in result.days_without_data],
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Nectr actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_REIMPORT,
        _async_reimport,
        schema=REIMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
