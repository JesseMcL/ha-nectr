"""Diagnostic sensors for Nectr.

The usage itself is imported as statistics (see coordinator.py) rather than
exposed as sensor states, because Nectr's data is a day or more old. These
sensors show what the integration is doing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import NectrConfigEntry, NectrCoordinator, NectrData


@dataclass(frozen=True, kw_only=True)
class NectrSensorDescription(SensorEntityDescription):
    """Describes a Nectr sensor."""

    value: Callable[[NectrData], date | datetime | None]
    attributes: Callable[[NectrData], dict[str, object]] | None = None


SENSORS: tuple[NectrSensorDescription, ...] = (
    NectrSensorDescription(
        key="latest_data",
        translation_key="latest_data",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.latest_data,
        attributes=lambda d: {"backfill_complete": d.backfill_complete},
    ),
    NectrSensorDescription(
        key="billing_period_start",
        translation_key="billing_period_start",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.account_info.billing_period_start,
        attributes=lambda d: {"plan": d.account_info.plan_name},
    ),
    NectrSensorDescription(
        key="billing_period_end",
        translation_key="billing_period_end",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.account_info.billing_period_end,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NectrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Nectr sensors."""
    coordinator = entry.runtime_data
    async_add_entities(NectrSensor(coordinator, d) for d in SENSORS)


class NectrSensor(CoordinatorEntity[NectrCoordinator], SensorEntity):
    """A Nectr diagnostic sensor."""

    _attr_has_entity_name = True
    entity_description: NectrSensorDescription

    def __init__(
        self, coordinator: NectrCoordinator, description: NectrSensorDescription
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.account}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account)},
            name=f"{NAME} {coordinator.account}",
            manufacturer=NAME,
            model=coordinator.data.account_info.plan_name,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://account.nectr.com.au/",
        )

    @property
    def native_value(self) -> date | datetime | None:
        """Return the sensor value."""
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return extra attributes."""
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)
