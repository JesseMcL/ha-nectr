"""Tests for Nectr setup, statistics import and sensors."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nectr.const import (
    BACKFILL_UPDATE_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.nectr.coordinator import statistic_id

from .conftest import ACCOUNT, FakeNectr, full_day, hour_row

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=SYD)


@pytest.fixture(autouse=True)
async def sydney(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Run in Sydney time on 21 Nov 2026."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(NOW)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


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
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_backfill_then_rolling_updates(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Backfill in chunks, then keep filling in partly published days."""
    fake_nectr.bill_periods = [("19/10/2026", "18/11/2026")]
    for day in _days(date(2026, 10, 19), date(2026, 11, 19)):
        fake_nectr.hourly[day] = full_day(grid=1.0, export=0.5, cl=0.25)
    fake_nectr.hourly[date(2026, 11, 20)] = [hour_row(0, grid=1.0, export=0.5, cl=0.25)]

    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    coordinator = config_entry.runtime_data

    # First chunk: 31 days from the first bill period.
    assert fake_nectr.hourly_requests[0] == "19/10/2026"
    assert len(fake_nectr.hourly_requests) == 31
    assert not coordinator.data.backfill_complete
    assert coordinator.update_interval == BACKFILL_UPDATE_INTERVAL
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 31 * 24
    assert grid[-1]["sum"] == pytest.approx(31 * 24)

    # Second chunk reaches today.
    fake_nectr.hourly_requests.clear()
    await _refresh(hass, config_entry)
    assert fake_nectr.hourly_requests[0] == "18/11/2026"
    assert fake_nectr.hourly_requests[-1] == "21/11/2026"
    assert coordinator.data.backfill_complete
    assert coordinator.update_interval == UPDATE_INTERVAL
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 32 * 24 + 1
    assert grid[-1]["sum"] == pytest.approx(32 * 24 + 1)
    assert coordinator.data.latest_data == datetime(2026, 11, 20, 0, tzinfo=SYD)

    # Nectr fills in the rest of 20 Nov and corrects an hour on 19 Nov.
    fake_nectr.hourly[date(2026, 11, 20)] = full_day(grid=1.0, export=0.5, cl=0.25)
    fake_nectr.hourly[date(2026, 11, 19)][5] = hour_row(
        5, grid=11.0, export=0.5, cl=0.25
    )
    fake_nectr.hourly_requests.clear()
    await _refresh(hass, config_entry)
    # Lookback window: today minus 4 days.
    assert fake_nectr.hourly_requests[0] == "17/11/2026"
    grid = await _stats(hass, "grid_import")
    assert len(grid) == 33 * 24
    assert grid[-1]["sum"] == pytest.approx(33 * 24 + 10)
    sums = [row["sum"] for row in grid]
    assert sums == sorted(sums)

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
        assert rows[-1]["sum"] == pytest.approx(expected), key

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
    assert await _stats(hass, "grid_import") == []
    for _ in range(4):
        await _refresh(hass, config_entry)
    assert config_entry.runtime_data.data.backfill_complete
    grid = await _stats(hass, "grid_import")
    # 50 days, less the DST-start hour and the skipped day.
    assert len(grid) == 49 * 24 - 1
    assert grid[-1]["sum"] == pytest.approx(49 * 24 - 1)


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
