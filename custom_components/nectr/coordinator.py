"""Coordinator that imports Nectr hourly usage into long-term statistics.

Nectr only publishes interval data the next day, often in pieces, so the data
can't be a normal sensor state. Instead each hour is written as an external
statistic (``nectr:<account>_<series>``) which the Energy dashboard can use
directly, the same approach Home Assistant's Opower integration takes.

Every update re-fetches the last few days and rewrites their statistics,
continuing the running sums from the last statistic before that window. Rows
with the same start time are replaced, so partly published days and later
corrections are filled in automatically.

History is backfilled newest first: the first update imports the most recent
month, and each later update imports the month before the oldest day imported
so far, until Nectr has nothing older. An older chunk is written so that its
running sum ends exactly where the newer statistics begin, which leaves the
newer rows untouched. The oldest sums are therefore negative; that is harmless,
because the Energy dashboard only uses differences between sums.

The ``nectr.reimport`` action rewrites any range of days on request. The sums
of all later statistics are then shifted by however much the range's total
changed, so the running sums stay continuous.
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
from homeassistant.helpers.storage import Store
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
    CONF_ACCOUNT_NUMBER,
    CONF_HISTORY_POLL_INTERVAL,
    DEFAULT_HISTORY_POLL_INTERVAL,
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
class Channel:
    """One metering channel Nectr reports usage and cost for.

    Nectr's hourly usage carries three channels: general (grid) usage,
    export and controlled load. Which of them a premises has varies, so
    nothing is assumed: a channel is imported once the account says it has
    that register, its statistics already exist, or Nectr reports a non-zero
    value for it.
    """

    key: str
    name: str
    energy: Callable[[HourlyUsage], float]
    cost: Callable[[HourlyUsage], float]
    cost_name: str
    account_flag: str | None = None

    @property
    def energy_key(self) -> str:
        """Key of the energy statistic."""
        return self.key

    @property
    def cost_key(self) -> str:
        """Key of the cost statistic."""
        return f"{self.key}_{self.cost_name}"


CHANNELS: tuple[Channel, ...] = (
    Channel(
        "grid_import",
        "grid import",
        lambda u: u.grid_kwh,
        lambda u: u.grid_cost,
        "cost",
    ),
    Channel(
        "export",
        "export",
        lambda u: u.export_kwh,
        lambda u: u.export_credit,
        "credit",
        "has_export",
    ),
    Channel(
        "controlled_load",
        "controlled load",
        lambda u: u.controlled_load_kwh,
        lambda u: u.controlled_load_cost,
        "cost",
        "has_controlled_load",
    ),
)


def statistic_id(account: str, key: str) -> str:
    """Return the external statistic id for an account's series."""
    slug = re.sub(r"[^a-z0-9_]", "_", account.lower())
    return f"{DOMAIN}:{slug}_{key}"


STORAGE_VERSION = 1


def history_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store that records how far the history backfill has got."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.history")


@dataclass(frozen=True, slots=True)
class ReimportResult:
    """What a re-import did."""

    hours: int
    days_without_data: list[date]


@dataclass(frozen=True, slots=True)
class NectrData:
    """Data exposed to entities."""

    account_info: AccountInfo
    latest_data: datetime | None
    backfill_complete: bool
    channels: tuple[str, ...]


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
        self.history_poll_interval = timedelta(
            minutes=entry.options.get(
                CONF_HISTORY_POLL_INTERVAL, DEFAULT_HISTORY_POLL_INTERVAL
            )
        )
        # Where the next catch-up chunk starts after a gap (for example Home
        # Assistant being off for weeks). Needed so a stretch of days with no
        # data at all can't stall the catch-up.
        self._resume_day: date | None = None
        # History backfill progress, kept across restarts: the oldest day
        # imported so far, and whether there is nothing older to import.
        self._store = history_store(hass, entry.entry_id)
        self._history_loaded = False
        self._oldest_day: date | None = None
        self._history_done = False
        self._first_day: date | None = None
        # Serialises updates and re-imports, which both read and write sums.
        self._lock = asyncio.Lock()

    @property
    def tz(self) -> tzinfo:
        """Time zone Nectr's local hours are interpreted in.

        Nectr labels hours in the meter's local time; Home Assistant's own time
        zone is assumed to match the property.
        """
        return dt_util.get_default_time_zone()

    async def _async_update_data(self) -> NectrData:
        async with self._lock:
            return await self._async_update()

    async def _async_update(self) -> NectrData:
        try:
            info = await self.client.async_get_account_info(self.account)
            if info.is_smart_meter:
                latest, complete, channels = await self._async_import_statistics(info)
            else:
                # Without interval data there is nothing to import.
                _LOGGER.warning("Nectr account %s has no smart meter", self.account)
                latest, complete, channels = None, True, ()
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

        self.update_interval = (
            UPDATE_INTERVAL if complete else self.history_poll_interval
        )
        return NectrData(
            account_info=info,
            latest_data=latest,
            backfill_complete=complete,
            channels=channels,
        )

    def _energy_metadata(self, channel: Channel) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Nectr {self.account} {channel.name}",
            source=DOMAIN,
            statistic_id=statistic_id(self.account, channel.energy_key),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    def _cost_metadata(self, channel: Channel) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Nectr {self.account} {channel.name} {channel.cost_name}",
            source=DOMAIN,
            statistic_id=statistic_id(self.account, channel.cost_key),
            unit_class=None,
            unit_of_measurement=None,
        )

    def _last_starts(self) -> dict[str, datetime]:
        """Return the latest statistic start of each channel that has any."""
        result: dict[str, datetime] = {}
        for channel in CHANNELS:
            sid = statistic_id(self.account, channel.energy_key)
            last = get_last_statistics(self.hass, 1, sid, True, set())
            if last and last.get(sid):
                result[channel.key] = dt_util.utc_from_timestamp(last[sid][0]["start"])
        return result

    async def _async_first_day(self, today: date) -> date:
        """Return the oldest day the history backfill goes back to."""
        if self._first_day is None:
            floor = today - timedelta(days=MAX_BACKFILL_DAYS)
            periods = await self.client.async_get_bill_periods(self.account)
            if periods:
                self._first_day = max(floor, min(p.start for p in periods))
            else:
                self._first_day = today - timedelta(days=LOOKBACK_DAYS)
        return self._first_day

    async def _async_load_history(self, has_statistics: bool) -> None:
        """Load the backfill progress saved by an earlier run."""
        if self._history_loaded:
            return
        self._history_loaded = True
        stored = await self._store.async_load()
        if stored is None:
            # Statistics without saved progress were imported by a version that
            # backfilled oldest first, so the history is already there.
            self._history_done = has_statistics
            if has_statistics:
                await self._async_save_history()
            return
        oldest = stored.get("oldest_day")
        self._oldest_day = date.fromisoformat(oldest) if oldest else None
        self._history_done = bool(stored.get("done"))

    async def _async_save_history(self) -> None:
        oldest = self._oldest_day
        await self._store.async_save(
            {
                "oldest_day": oldest.isoformat() if oldest else None,
                "done": self._history_done,
            }
        )

    async def _async_import_statistics(
        self, info: AccountInfo
    ) -> tuple[datetime | None, bool, tuple[str, ...]]:
        """Fetch recent days and a chunk of history, and write statistics.

        Returns the start of the newest hour Nectr has published, whether the
        import has caught up with today and finished the history, and the
        channels being imported.
        """
        tz = self.tz
        today = dt_util.now(tz).date()
        recorder = get_instance(self.hass)

        existing = await recorder.async_add_executor_job(self._last_starts)
        await self._async_load_history(bool(existing))
        # Progress is only committed once everything has been fetched, so a
        # failed update is simply repeated.
        oldest_day, history_done = self._oldest_day, self._history_done

        # Recent days: rewrite the lookback window, or catch up after a gap.
        last_start: datetime | None = None
        first_chunk = False
        if existing:
            last_start = max(existing.values())
            start_day = min(
                last_start.astimezone(tz).date(), today - timedelta(days=LOOKBACK_DAYS)
            )
        elif oldest_day is None and not history_done:
            # Very first update: start the history with the most recent month,
            # and leave older months to the following updates so that setup
            # doesn't wait on them.
            first_chunk = True
            first_day = await self._async_first_day(today)
            start_day = max(first_day, today - timedelta(days=MAX_DAYS_PER_UPDATE - 1))
            oldest_day = start_day
            history_done = start_day <= first_day
        else:
            start_day = today - timedelta(days=LOOKBACK_DAYS)
        if self._resume_day and self._resume_day > start_day:
            start_day = self._resume_day
        end_day = min(today, start_day + timedelta(days=MAX_DAYS_PER_UPDATE - 1))
        caught_up = end_day >= today
        self._resume_day = None if caught_up else end_day

        _LOGGER.debug("Fetching Nectr usage for %s to %s", start_day, end_day)
        usage = await self._async_fetch_days(_day_range(start_day, end_day), tz)

        # Older history: one chunk before the oldest day imported so far, once
        # the recent days are up to date.
        history: list[HourlyUsage] = []
        history_end = oldest_day
        if (
            caught_up
            and not first_chunk
            and not history_done
            and history_end is not None
        ):
            first_day = await self._async_first_day(today)
            history_start = max(
                first_day, history_end - timedelta(days=MAX_DAYS_PER_UPDATE)
            )
            if history_start < history_end:
                _LOGGER.debug(
                    "Fetching Nectr history for %s to %s",
                    history_start,
                    history_end - timedelta(days=1),
                )
                history = await self._async_fetch_days(
                    _day_range(history_start, history_end - timedelta(days=1)), tz
                )
            # Nectr has nothing older once a whole chunk comes back empty
            # (for example before the smart meter was installed).
            history_done = history_start <= first_day or not history
            if history:
                oldest_day = history_start

        channels = _select_channels(info, existing, [*usage, *history])
        ids = self._statistic_ids(channels)

        if usage:
            window_start = datetime.combine(start_day, time(), tz).astimezone(UTC)
            sums = await recorder.async_add_executor_job(
                self._sums_before, window_start, ids
            )
            self._write_statistics(channels, usage, sums, ends_at=False)
        if history:
            assert history_end is not None
            window_end = datetime.combine(history_end, time(), tz).astimezone(UTC)
            sums = await recorder.async_add_executor_job(
                self._sums_from, window_end, ids
            )
            self._write_statistics(channels, history, sums, ends_at=True)
        if (oldest_day, history_done) != (self._oldest_day, self._history_done):
            self._oldest_day, self._history_done = oldest_day, history_done
            await self._async_save_history()

        complete = caught_up and self._history_done
        latest = usage[-1].start if usage else last_start
        _LOGGER.debug(
            "Imported %d recent and %d older hours of %s for %s; latest data %s, "
            "history back to %s%s",
            len(usage),
            len(history),
            [c.key for c in channels],
            self.account,
            latest,
            self._oldest_day,
            " (complete)" if self._history_done else "",
        )
        return latest, complete, tuple(c.key for c in channels)

    def _statistic_ids(self, channels: list[Channel]) -> set[str]:
        return {
            statistic_id(self.account, key)
            for c in channels
            for key in (c.energy_key, c.cost_key)
        }

    def _write_statistics(
        self,
        channels: list[Channel],
        usage: list[HourlyUsage],
        sums: dict[str, float],
        *,
        ends_at: bool,
    ) -> dict[str, float]:
        """Queue statistics for consecutive hours.

        ``sums`` holds each statistic's running sum just before the first hour
        or, with ``ends_at``, just after the last hour. Returns each
        statistic's sum after the last hour.
        """
        end_sums: dict[str, float] = {}
        for channel in channels:
            for metadata, value in (
                (self._energy_metadata(channel), channel.energy),
                (self._cost_metadata(channel), channel.cost),
            ):
                states = [value(hour) for hour in usage]
                running = sums.get(metadata["statistic_id"], 0.0)
                if ends_at:
                    running -= sum(states)
                rows: list[StatisticData] = []
                for hour, state in zip(usage, states, strict=True):
                    running += state
                    rows.append(
                        StatisticData(start=hour.start, state=state, sum=running)
                    )
                async_add_external_statistics(self.hass, metadata, rows)
                end_sums[metadata["statistic_id"]] = running
        return end_sums

    async def async_reimport(self, start: date, end: date) -> ReimportResult:
        """Fetch the days from start to end again and rewrite their statistics.

        Days Nectr returns nothing for are left as they are. After each run of
        days that was rewritten, the sums of all later statistics are shifted
        so they carry on from the new values.
        """
        async with self._lock:
            tz = self.tz
            recorder = get_instance(self.hass)
            info = await self.client.async_get_account_info(self.account)
            days = _day_range(start, end)
            _LOGGER.debug("Re-importing Nectr usage for %s to %s", start, end)
            usage = await self._async_fetch_days(days, tz)

            existing = await recorder.async_add_executor_job(self._last_starts)
            channels = _select_channels(info, existing, usage)
            ids = self._statistic_ids(channels)
            units = {
                metadata["statistic_id"]: metadata["unit_of_measurement"]
                for c in channels
                for metadata in (self._energy_metadata(c), self._cost_metadata(c))
            }

            by_day: dict[date, list[HourlyUsage]] = {}
            for hour in usage:
                by_day.setdefault(hour.start.astimezone(tz).date(), []).append(hour)

            for run in _consecutive_runs(sorted(by_day)):
                hours = [h for day in run for h in by_day[day]]
                run_start = datetime.combine(run[0], time(), tz).astimezone(UTC)
                after = hours[-1].start + timedelta(hours=1)
                sums = await recorder.async_add_executor_job(
                    self._sums_before, run_start, ids
                )
                # Read before writing: what the statistics after this run
                # currently carry on from.
                following = await recorder.async_add_executor_job(
                    self._sums_from, after, ids
                )
                end_sums = self._write_statistics(channels, hours, sums, ends_at=False)
                for sid, previous in following.items():
                    if (adjustment := end_sums.get(sid, previous) - previous) != 0:
                        recorder.async_adjust_statistics(
                            sid, after, adjustment, units[sid]
                        )
                # The next run reads sums this one wrote.
                await recorder.async_block_till_done()

        return ReimportResult(
            hours=len(usage),
            days_without_data=[d for d in days if d not in by_day],
        )

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

    def _sums_from(self, window_end: datetime, ids: set[str]) -> dict[str, float]:
        """Return each statistic's sum before its first hour from window_end on.

        This is where an older chunk written before window_end must end. Looks
        progressively further ahead, since a statistic may start later.
        """
        result: dict[str, float] = {}
        remaining = set(ids)
        for lookahead in (
            timedelta(days=1),
            timedelta(days=7),
            timedelta(days=60),
            None,
        ):
            if not remaining:
                break
            stats: dict[str, list[Any]] = statistics_during_period(
                self.hass,
                window_end,
                window_end + lookahead if lookahead else None,
                remaining,
                "hour",
                None,
                {"state", "sum"},
            )
            for sid, rows in stats.items():
                if rows and rows[0].get("sum") is not None:
                    result[sid] = float(rows[0]["sum"]) - float(rows[0]["state"] or 0)
                    remaining.discard(sid)
        return result


def _select_channels(
    info: AccountInfo, existing: dict[str, datetime], usage: list[HourlyUsage]
) -> list[Channel]:
    """Return the channels to import (see Channel)."""
    return [
        c
        for c in CHANNELS
        if c.key in existing
        or (c.account_flag is not None and getattr(info, c.account_flag))
        or any(c.energy(h) or c.cost(h) for h in usage)
    ]


def _consecutive_runs(days: list[date]) -> list[list[date]]:
    """Split sorted days into runs of consecutive days."""
    runs: list[list[date]] = []
    for day in days:
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _day_range(first: date, last: date) -> list[date]:
    """Return the days from first to last inclusive."""
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]
