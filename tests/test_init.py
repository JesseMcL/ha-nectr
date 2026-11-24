"""Tests for Nectr setup, statistics import and sensors."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nectr.const import (
    CONF_HISTORY_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SERVICE_REIMPORT,
    UPDATE_INTERVAL,
)
from custom_components.nectr.coordinator import statistic_id

from .conftest import ACCOUNT, DEVICE_ID, EMAIL, FakeNectr, full_day, hour_row

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=SYD)


@pytest.fixture(autouse=True)
async def sydney(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Run in Sydney time on 21 Nov 2026."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(NOW)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _requested(fake: FakeNectr) -> set[date]:
    """Return the days requested since the last clear."""
    return {datetime.strptime(d, "%d/%m/%Y").date() for d in fake.hourly_requests}


def _total(rows: list[dict[str, Any]]) -> float:
    """Return what a statistic's rows add up to, from their sums."""
    return rows[-1]["sum"] - rows[0]["sum"] + rows[0]["state"]


def _assert_consistent(rows: list[dict[str, Any]]) -> None:
    """Each row's sum is the previous sum plus its own state."""
    for prev, row in pairwise(rows):
        assert row["sum"] == pytest.approx(prev["sum"] + row["state"])


async def _stats(hass: HomeAssistant, key: str) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    sid = statistic_id(ACCOUNT, key)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {sid},
        "hour",
        None,
        {"state", "sum"},
    )
    return result.get(sid, [])


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # Let the previous update's statistics reach the database first, as they
    # would in the minutes between real updates.
    await async_wait_recording_done(hass)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_backfill_newest_first_then_rolling_updates(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Backfill from the newest month back, then fill in partly published days."""
    fake_nectr.bill_periods = [("19/10/2026", "18/11/2026")]
    for day in _days(date(2026, 10, 19), date(2026, 11, 19)):
        fake_nectr.hourly[day] = full_day(grid=1.0, export=0.5, cl=0.25)
    fake_nectr.hourly[date(2026, 11, 20)] = [hour_row(0, grid=1.0, export=0.5, cl=0.25)]

    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    coordinator = config_entry.runtime_data

    # First chunk: the most recent 31 days.
    assert _requested(fake_nectr) == set(_days(date(2026, 10, 22), date(2026, 11, 21)))
    assert not coordinator.data.backfill_complete
    assert coordinator.update_interval == timedelta(minutes=5)
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 29 * 24 + 1
    assert _total(grid) == pytest.approx(29 * 24 + 1)
    assert coordinator.data.latest_data == datetime(2026, 11, 20, 0, tzinfo=SYD)

    # Next: the recent days again, plus the older days back to the first bill.
    fake_nectr.hourly_requests.clear()
    await _refresh(hass, config_entry)
    assert _requested(fake_nectr) == {
        *_days(date(2026, 11, 17), date(2026, 11, 21)),
        *_days(date(2026, 10, 19), date(2026, 10, 21)),
    }
    assert coordinator.data.backfill_complete
    assert coordinator.update_interval == UPDATE_INTERVAL
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 32 * 24 + 1
    assert grid[0]["start"] == datetime(2026, 10, 19, tzinfo=SYD).timestamp()
    assert _total(grid) == pytest.approx(32 * 24 + 1)
    _assert_consistent(grid)
    assert coordinator.data.latest_data == datetime(2026, 11, 20, 0, tzinfo=SYD)

    # Nectr fills in the rest of 20 Nov and corrects an hour on 19 Nov.
    fake_nectr.hourly[date(2026, 11, 20)] = full_day(grid=1.0, export=0.5, cl=0.25)
    fake_nectr.hourly[date(2026, 11, 19)][5] = hour_row(
        5, grid=11.0, export=0.5, cl=0.25
    )
    fake_nectr.hourly_requests.clear()
    await _refresh(hass, config_entry)
    # Lookback window only: today minus 4 days.
    assert _requested(fake_nectr) == set(_days(date(2026, 11, 17), date(2026, 11, 21)))
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 33 * 24
    assert _total(grid) == pytest.approx(33 * 24 + 10)
    _assert_consistent(grid)

    for key, per_hour in (
        ("grid_import_cost", 0.3),
        ("export", 0.5),
        ("export_credit", 0.025),
        ("controlled_load", 0.25),
        ("controlled_load_cost", 0.0425),
    ):
        rows = await _stats(hass, key)
        assert len(rows) == 33 * 24, key
        expected = 33 * 24 * per_hour + (3.0 if key == "grid_import_cost" else 0)
        assert _total(rows) == pytest.approx(expected), key
        _assert_consistent(rows)

    # Sensors.
    latest = hass.states.get("sensor.nectr_1000123_latest_data")
    assert latest is not None
    assert datetime.fromisoformat(latest.state) == datetime(
        2026, 11, 20, 23, tzinfo=SYD
    )
    assert latest.attributes["backfill_complete"] is True
    start = hass.states.get("sensor.nectr_1000123_billing_period_start")
    assert start.state == "2026-11-19"
    assert start.attributes["plan"] == "Test Plan"
    assert (
        hass.states.get("sensor.nectr_1000123_billing_period_end").state == "2026-12-18"
    )

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_backfill_skips_days_without_data(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """A long stretch with no data at the start doesn't stall the backfill."""
    fake_nectr.bill_periods = [("01/08/2026", "31/08/2026")]
    for day in _days(date(2026, 10, 1), date(2026, 11, 19)):
        fake_nectr.hourly[day] = full_day(day=day)
    # An unreadable day is skipped, not fatal.
    fake_nectr.hourly[date(2026, 10, 10)] = [hour_row(5), hour_row(4)]

    await _setup(hass, config_entry)
    await _refresh(hass, config_entry)
    assert not config_entry.runtime_data.data.backfill_complete
    # A whole month with no data ends the history, before the first bill.
    fake_nectr.hourly_requests.clear()
    await _refresh(hass, config_entry)
    assert date(2026, 8, 21) in _requested(fake_nectr)
    assert config_entry.runtime_data.data.backfill_complete
    grid = await _stats(hass, "grid_import")
    # 50 days, less the DST-start hour and the skipped day.
    assert len(grid) == 49 * 24 - 1
    assert _total(grid) == pytest.approx(49 * 24 - 1)
    _assert_consistent(grid)


async def test_backfill_resumes_after_reload(
    hass: HomeAssistant, fake_nectr: FakeNectr
) -> None:
    """Backfill progress survives a reload; the poll period is an option."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data={
            "email": EMAIL,
            "device_id": DEVICE_ID,
            "account_number": ACCOUNT,
            CONF_REFRESH_TOKEN: "rt-initial",
        },
        options={CONF_HISTORY_POLL_INTERVAL: 2},
    )
    fake_nectr.bill_periods = [("01/09/2026", "30/09/2026")]
    for day in _days(date(2026, 9, 1), date(2026, 11, 20)):
        fake_nectr.hourly[day] = full_day(day=day)

    await _setup(hass, entry)
    assert entry.runtime_data.update_interval == timedelta(minutes=2)
    await async_wait_recording_done(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # The reload's update continued with the month before the first chunk.
    requested = _requested(fake_nectr)
    assert date(2026, 9, 21) in requested
    assert date(2026, 9, 20) not in requested
    await _refresh(hass, entry)
    assert entry.runtime_data.data.backfill_complete
    grid = await _stats(hass, "grid_import")
    assert grid[0]["start"] == datetime(2026, 9, 1, tzinfo=SYD).timestamp()
    # 81 days, less the DST-start hour.
    assert _total(grid) == pytest.approx(81 * 24 - 1)
    _assert_consistent(grid)


async def test_statistics_without_saved_progress_are_complete(
    hass: HomeAssistant,
    fake_nectr: FakeNectr,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Statistics from a version that backfilled oldest first aren't redone."""
    fake_nectr.bill_periods = [("01/09/2026", "30/09/2026")]
    for day in _days(date(2026, 9, 1), date(2026, 11, 20)):
        fake_nectr.hourly[day] = full_day(day=day)
    await _setup(hass, config_entry)
    await async_wait_recording_done(hass)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    hass_storage.pop(f"{DOMAIN}.{config_entry.entry_id}.history")

    fake_nectr.hourly_requests.clear()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _requested(fake_nectr) == set(_days(date(2026, 11, 17), date(2026, 11, 21)))
    assert config_entry.runtime_data.data.backfill_complete


async def test_reimport_action(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Re-importing a range picks up changes and keeps later sums continuous."""
    fake_nectr.bill_periods = [("01/10/2026", "31/10/2026")]
    for day in _days(date(2026, 10, 1), date(2026, 11, 20)):
        if day != date(2026, 10, 10):
            fake_nectr.hourly[day] = full_day(day=day)
    await _setup(hass, config_entry)
    await _refresh(hass, config_entry)
    assert config_entry.runtime_data.data.backfill_complete
    hours = 50 * 24 - 1  # less the missing day and the DST-start hour
    assert len(await _stats(hass, "grid_import")) == hours

    # Nectr later publishes the missing day and corrects an hour. Another day
    # in the range now comes back empty; its statistics are left alone.
    fake_nectr.hourly[date(2026, 10, 10)] = full_day()
    fake_nectr.hourly[date(2026, 10, 15)][3] = hour_row(3, grid=5.0)
    del fake_nectr.hourly[date(2026, 10, 12)]
    fake_nectr.hourly_requests.clear()
    await async_wait_recording_done(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_REIMPORT,
        {
            "config_entry_id": config_entry.entry_id,
            "start_date": "2026-10-09",
            "end_date": "2026-10-16",
        },
        blocking=True,
        return_response=True,
    )
    assert response == {
        "start_date": "2026-10-09",
        "end_date": "2026-10-16",
        "hours": 7 * 24,
        "days_without_data": ["2026-10-12"],
    }
    assert _requested(fake_nectr) == set(_days(date(2026, 10, 9), date(2026, 10, 16)))

    for key, total in (
        ("grid_import", hours + 24 + 4),
        ("grid_import_cost", (hours + 24) * 0.3 + 1.2),
    ):
        rows = await _stats(hass, key)
        assert len(rows) == hours + 24, key
        assert _total(rows) == pytest.approx(total), key
        # Includes the hours after the range and around the empty day.
        _assert_consistent(rows)


async def test_reimport_validation(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Bad dates and unknown entries are rejected."""
    await _setup(hass, config_entry)
    for data in (
        {
            "config_entry_id": config_entry.entry_id,
            "start_date": "2026-11-10",
            "end_date": "2026-11-09",
        },
        {"config_entry_id": "missing", "start_date": "2026-11-10"},
    ):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, SERVICE_REIMPORT, data, blocking=True, return_response=True
            )


async def test_without_bill_history_and_optional_registers(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """New accounts start a few days back; absent registers aren't imported."""
    fake_nectr.account_info["isExportRegister"] = False
    fake_nectr.account_info["isControlledLoad"] = False
    for day in _days(date(2026, 11, 17), date(2026, 11, 20)):
        fake_nectr.hourly[day] = full_day()

    await _setup(hass, config_entry)
    assert fake_nectr.hourly_requests[0] == "17/11/2026"
    assert config_entry.runtime_data.data.backfill_complete
    assert len(await _stats(hass, "grid_import")) == 4 * 24
    assert await _stats(hass, "export") == []
    assert await _stats(hass, "controlled_load") == []


async def test_channels_discovered_from_data(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Channels appear when Nectr reports them, even without the account flag."""
    fake_nectr.account_info["isExportRegister"] = False
    fake_nectr.account_info["isControlledLoad"] = False
    days = _days(date(2026, 11, 17), date(2026, 11, 20))
    for day in days:
        fake_nectr.hourly[day] = full_day()

    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.data.channels == ("grid_import",)

    # Solar export starts showing up; it is imported from then on.
    fake_nectr.hourly[date(2026, 11, 20)] = full_day(export=0.5)
    await _refresh(hass, config_entry)
    assert coordinator.data.channels == ("grid_import", "export")
    export = await _stats(hass, "export")
    assert len(export) == 4 * 24
    assert export[-1]["sum"] == pytest.approx(24 * 0.5)
    assert len(await _stats(hass, "export_credit")) == 4 * 24

    # Once it has statistics it stays, even when the latest days are zero.
    fake_nectr.hourly[date(2026, 11, 20)] = full_day()
    await _refresh(hass, config_entry)
    assert coordinator.data.channels == ("grid_import", "export")
    latest = hass.states.get("sensor.nectr_1000123_latest_data")
    assert latest.attributes["channels"] == ["grid_import", "export"]


async def test_account_flag_enables_channel_without_data(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """A register the account reports is imported even while it reads zero."""
    fake_nectr.account_info["isExportRegister"] = False
    for day in _days(date(2026, 11, 17), date(2026, 11, 20)):
        fake_nectr.hourly[day] = full_day()
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.data.channels == ("grid_import", "controlled_load")
    assert len(await _stats(hass, "controlled_load_cost")) == 4 * 24


async def test_basic_meter_imports_nothing(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Accounts without a smart meter load but don't request hourly data."""
    fake_nectr.account_info["isSmartMeterUser"] = False
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert fake_nectr.hourly_requests == []
    assert config_entry.runtime_data.data.channels == ()


async def test_refresh_token_rotation_is_saved(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Each new refresh token is written to the config entry."""
    await _setup(hass, config_entry)
    assert config_entry.data[CONF_REFRESH_TOKEN] == "rt-1"
    fake_nectr.expire_access_tokens()
    await _refresh(hass, config_entry)
    assert config_entry.data[CONF_REFRESH_TOKEN] == "rt-2"


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """A rejected refresh token starts re-authentication."""
    fake_nectr.refresh_fails = True
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_update_failure_retries(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Unexpected API errors make setup retry later."""
    fake_nectr.usage_http_error = True
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
