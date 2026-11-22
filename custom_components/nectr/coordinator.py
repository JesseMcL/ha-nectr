"""Coordinator that imports Nectr hourly usage into long-term statistics.

Nectr only publishes interval data the next day, often in pieces, so the data
can't be a normal sensor state. Instead each hour is written as an external
statistic (``nectr:<account>_<series>``) which the Energy dashboard can use
directly, the same approach Home Assistant's Opower integration takes.

Every update re-fetches the last few days and rewrites their statistics,
continuing the running sums from the last statistic before that window. Rows
with the same start time are replaced, so partly published days and later
corrections are filled in automatically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    AccountInfo,
    HourlyUsage,
    NectrAuthError,
    NectrClient,
    NectrDataError,
    NectrError,
)
from .const import (
    BACKFILL_UPDATE_INTERVAL,
    CONF_ACCOUNT_NUMBER,
    DOMAIN,
    FETCH_CONCURRENCY,
    LOOKBACK_DAYS,
    MAX_BACKFILL_DAYS,
    MAX_DAYS_PER_UPDATE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type NectrConfigEntry = ConfigEntry[NectrCoordinator]


@dataclass(frozen=True, slots=True)
class Series:
    """One imported statistic."""

    key: str
    name: str
    value: Callable[[HourlyUsage], float]
    is_energy: bool
    needs: str | None = None  # AccountInfo flag that must be set


SERIES: tuple[Series, ...] = (
    Series("grid_import", "grid import", lambda u: u.grid_kwh, True),
    Series("grid_import_cost", "grid import cost", lambda u: u.grid_cost, False),
    Series("export", "export", lambda u: u.export_kwh, True, "has_export"),
    Series(
        "export_credit", "export credit", lambda u: u.export_credit, False, "has_export"
    ),
    Series(
        "controlled_load",
        "controlled load",
        lambda u: u.controlled_load_kwh,
        True,
        "has_controlled_load",
    ),
    Series(
        "controlled_load_cost",
        "controlled load cost",
        lambda u: u.controlled_load_cost,
        False,
        "has_controlled_load",
    ),
)
# The series used to decide how far the import has got.
REFERENCE_SERIES = "grid_import"


def statistic_id(account: str, key: str) -> str:
    """Return the external statistic id for an account's series."""
    slug = re.sub(r"[^a-z0-9_]", "_", account.lower())
    return f"{DOMAIN}:{slug}_{key}"


@dataclass(frozen=True, slots=True)
class NectrData:
    """Data exposed to entities."""

    account_info: AccountInfo
    latest_data: datetime | None
    backfill_complete: bool


class NectrCoordinator(DataUpdateCoordinator[NectrData]):
    """Fetch Nectr data and keep the statistics current."""

    config_entry: NectrConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: NectrConfigEntry, client: NectrClient
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ACCOUNT_NUMBER]}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.account: str = entry.data[CONF_ACCOUNT_NUMBER]
        # Where the next backfill chunk starts. Needed so a stretch of days
        # with no data at all can't stall the backfill.
        self._resume_day: date | None = None

    @property
    def tz(self) -> tzinfo:
        """Time zone Nectr's local hours are interpreted in.

        Nectr labels hours in the meter's local time; Home Assistant's own time
        zone is assumed to match the property.
        """
        return dt_util.get_default_time_zone()

    async def _async_update_data(self) -> NectrData:
        try:
            info = await self.client.async_get_account_info(self.account)
            latest, complete = await self._async_import_statistics(info)
        except NectrAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except NectrError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        self.update_interval = UPDATE_INTERVAL if complete else BACKFILL_UPDATE_INTERVAL
        return NectrData(
            account_info=info, latest_data=latest, backfill_complete=complete
        )

    def _active_series(self, info: AccountInfo) -> list[Series]:
        return [s for s in SERIES if s.needs is None or getattr(info, s.needs)]

    def _metadata(self, series: Series) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Nectr {self.account} {series.name}",
            source=DOMAIN,
            statistic_id=statistic_id(self.account, series.key),
            unit_class=EnergyConverter.UNIT_CLASS if series.is_energy else None,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
            if series.is_energy
            else None,
        )

    async def _async_first_day(self) -> date:
        """Return the first day to import when there are no statistics yet."""
        today = dt_util.now(self.tz).date()
        floor = today - timedelta(days=MAX_BACKFILL_DAYS)
        periods = await self.client.async_get_bill_periods(self.account)
        if not periods:
            return today - timedelta(days=LOOKBACK_DAYS)
        return max(floor, min(p.start for p in periods))

    async def _async_import_statistics(
        self, info: AccountInfo
    ) -> tuple[datetime | None, bool]:
        """Fetch recent days and write statistics.

        Returns the start of the newest hour Nectr has published and whether
        the import has caught up with today.
        """
        tz = self.tz
        today = dt_util.now(tz).date()
        recorder = get_instance(self.hass)
        ref_id = statistic_id(self.account, REFERENCE_SERIES)

        last = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, ref_id, True, {"sum"}
        )
        if last and last.get(ref_id):
            last_start = dt_util.utc_from_timestamp(last[ref_id][0]["start"])
            start_day = min(
                last_start.astimezone(tz).date(), today - timedelta(days=LOOKBACK_DAYS)
            )
        else:
            last_start = None
            start_day = await self._async_first_day()
        if self._resume_day and self._resume_day > start_day:
            start_day = self._resume_day

        end_day = min(today, start_day + timedelta(days=MAX_DAYS_PER_UPDATE - 1))
        complete = end_day >= today

        days = [
            start_day + timedelta(days=n) for n in range((end_day - start_day).days + 1)
        ]
        _LOGGER.debug("Fetching Nectr usage for %s to %s", start_day, end_day)
        usage = await self._async_fetch_days(days, tz)

        series = self._active_series(info)
        window_start = datetime.combine(start_day, time(), tz).astimezone(UTC)
        sums = await recorder.async_add_executor_job(
            self._sums_before,
            window_start,
            {statistic_id(self.account, s.key) for s in series},
        )

        for s in series:
            sid = statistic_id(self.account, s.key)
            running = sums.get(sid, 0.0)
            rows: list[StatisticData] = []
            for hour in usage:
                value = s.value(hour)
                running += value
                rows.append(StatisticData(start=hour.start, state=value, sum=running))
            if rows:
                async_add_external_statistics(self.hass, self._metadata(s), rows)

        self._resume_day = None if complete else end_day
        latest = usage[-1].start if usage else last_start
        _LOGGER.debug(
            "Imported %d hours for %s; latest data %s", len(usage), self.account, latest
        )
        return latest, complete

    async def _async_fetch_days(
        self, days: list[date], tz: tzinfo
    ) -> list[HourlyUsage]:
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch(day: date) -> list[HourlyUsage]:
            async with semaphore:
                try:
                    return await self.client.async_get_hourly_usage(
                        self.account, day, tz
                    )
                except NectrDataError as err:
                    # Skip a day we can't interpret rather than stalling the
                    # whole import on it.
                    _LOGGER.warning("Skipping Nectr data for %s: %s", day, err)
                    return []

        results = await asyncio.gather(*(fetch(d) for d in days))
        hours = [h for day_hours in results for h in day_hours]
        hours.sort(key=lambda h: h.start)
        return hours

    def _sums_before(self, window_start: datetime, ids: set[str]) -> dict[str, float]:
        """Return each statistic's sum at the last hour before window_start.

        Looks back progressively further, since there can be gaps (for example
        if Home Assistant was offline or Nectr had no data for a while).
        """
        result: dict[str, float] = {}
        remaining = set(ids)
        for lookback in (
            timedelta(hours=1),
            timedelta(days=7),
            timedelta(days=60),
            None,
        ):
            if not remaining:
                break
            start = (
                window_start - lookback
                if lookback
                else datetime(2000, 1, 1, tzinfo=UTC)
            )
            stats: dict[str, list[Any]] = statistics_during_period(
                self.hass, start, window_start, remaining, "hour", None, {"sum"}
            )
            for sid, rows in stats.items():
                if rows and rows[-1].get("sum") is not None:
                    result[sid] = float(rows[-1]["sum"])
                    remaining.discard(sid)
        return result
