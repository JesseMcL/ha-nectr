"""Tests for the Nectr API client."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.nectr.api import (
    NectrAuthError,
    NectrClient,
    NectrConnectionError,
    NectrInvalidCredentialsError,
    NectrInvalidOtpError,
    NectrLoginSessionExpiredError,
    NectrOtpExpiredError,
    NectrTooManyAttemptsError,
    hour_starts_for_labels,
)

from .conftest import ACCOUNT, DEVICE_ID, EMAIL, OTP, PASSWORD, FakeNectr, full_day, hour_row

SYD = ZoneInfo("Australia/Sydney")
BNE = ZoneInfo("Australia/Brisbane")
ADL = ZoneInfo("Australia/Adelaide")


def _labels(hours: list[int]) -> list[str]:
    return [f"{h}:00" for h in hours]


def test_hour_starts_normal_day() -> None:
    """A normal day maps 0:00..23:00 to consecutive hours."""
    starts = hour_starts_for_labels(date(2026, 11, 15), _labels(list(range(24))), SYD)
    assert len(starts) == 24
    # 15 Nov is AEDT (UTC+11): local midnight is 13:00 UTC the day before.
    assert starts[0] == datetime(2026, 11, 14, 13, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 11, 15, 12, tzinfo=UTC)


def test_hour_starts_dst_start() -> None:
    """The day DST starts has no 2:00 and only 23 hours."""
    hours = [h for h in range(24) if h != 2]
    starts = hour_starts_for_labels(date(2026, 10, 4), _labels(hours), SYD)
    assert len(starts) == 23
    assert all((b - a).total_seconds() == 3600 for a, b in zip(starts, starts[1:]))


def test_hour_starts_dst_end_repeated_hour() -> None:
    """The day DST ends has 2:00 twice; they map to consecutive real hours."""
    hours = [0, 1, 2, 2, *range(3, 24)]
    starts = hour_starts_for_labels(date(2026, 4, 5), _labels(hours), SYD)
    assert len(starts) == 25
    assert starts[3] - starts[2] == starts[1] - starts[0]
    assert starts[2].astimezone(SYD).utcoffset() != starts[3].astimezone(SYD).utcoffset()


def test_hour_starts_partial_and_other_zones() -> None:
    """Partial days skip missing hours; non-DST and half-hour zones work."""
    starts = hour_starts_for_labels(date(2026, 11, 20), _labels([0, 5, 6]), SYD)
    assert [s.astimezone(SYD).hour for s in starts] == [0, 5, 6]
    starts = hour_starts_for_labels(date(2026, 10, 4), _labels(list(range(24))), BNE)
    assert len(starts) == 24
    starts = hour_starts_for_labels(date(2026, 11, 15), _labels([0]), ADL)
    assert starts[0] == datetime(2026, 11, 14, 13, 30, tzinfo=UTC)


def test_hour_starts_rejects_impossible_labels() -> None:
    """Labels that don't fit the day raise."""
    with pytest.raises(NectrConnectionError):
        hour_starts_for_labels(date(2026, 11, 15), _labels([5, 4]), SYD)
    with pytest.raises(NectrConnectionError):
        hour_starts_for_labels(date(2026, 11, 15), ["late"], SYD)


async def test_login_and_fetch(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """Full login with OTP, then fetch accounts and usage."""
    tokens: list[str] = []
    client = NectrClient(
        async_get_clientsession(hass),
        device_id=DEVICE_ID,
        token_callback=tokens.append,
    )
    session = await client.async_request_login_otp(EMAIL, PASSWORD)
    await client.async_verify_login_otp(session, f" {OTP} ")
    assert tokens == [client.refresh_token]

    accounts = await client.async_get_accounts()
    assert [a.number for a in accounts] == [ACCOUNT]
    assert accounts[0].is_active

    info = await client.async_get_account_info(ACCOUNT)
    assert info.has_export and info.has_controlled_load
    assert info.billing_period_start == date(2026, 11, 19)

    fake_nectr.hourly[date(2026, 11, 15)] = full_day(grid=2.0, export=0.5)
    usage = await client.async_get_hourly_usage(ACCOUNT, date(2026, 11, 15), SYD)
    assert len(usage) == 24
    assert usage[0].start == datetime(2026, 11, 14, 13, tzinfo=UTC)
    assert usage[0].grid_kwh == 2.0
    assert usage[0].export_kwh == 0.5
    assert fake_nectr.hourly_requests == ["15/11/2026"]

    assert await client.async_get_hourly_usage(ACCOUNT, date(2026, 11, 16), SYD) == []


async def test_bill_periods_paginate(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """Bill periods are collected across pages."""
    fake_nectr.bill_periods = [(f"19/{m:02d}/2026", f"18/{m + 1:02d}/2026") for m in range(10, 0, -1)]
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID, refresh_token="rt-initial")
    periods = await client.async_get_bill_periods(ACCOUNT)
    assert len(periods) == 10
    assert periods[-1].start == date(2026, 1, 19)


@pytest.mark.parametrize(
    ("password", "error"),
    [("wrong", NectrInvalidCredentialsError), ("too-many", NectrTooManyAttemptsError)],
)
async def test_request_otp_errors(hass, fake_nectr, password, error) -> None:
    """Login errors map to specific exceptions."""
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID)
    with pytest.raises(error):
        await client.async_request_login_otp(EMAIL, password)


@pytest.mark.parametrize(
    ("session", "otp", "error"),
    [
        ("sess-1", "000000", NectrInvalidOtpError),
        ("sess-1", "expired", NectrOtpExpiredError),
        ("dead", OTP, NectrLoginSessionExpiredError),
    ],
)
async def test_verify_otp_errors(hass, fake_nectr, session, otp, error) -> None:
    """OTP errors map to specific exceptions."""
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID)
    with pytest.raises(error):
        await client.async_verify_login_otp(session, otp)


async def test_resend_otp(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """Resending returns the new session id, which then verifies."""
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID)
    session = await client.async_request_login_otp(EMAIL, PASSWORD)
    session = await client.async_resend_login_otp(session)
    assert session == "sess-2"
    await client.async_verify_login_otp(session, OTP)


async def test_refresh_rotates_and_retries_on_401(hass, fake_nectr: FakeNectr) -> None:
    """Expired access tokens are refreshed transparently and rotation persisted."""
    tokens: list[str] = []
    client = NectrClient(
        async_get_clientsession(hass),
        device_id=DEVICE_ID,
        refresh_token="rt-initial",
        token_callback=tokens.append,
    )
    await client.async_get_accounts()
    assert tokens == ["rt-1"]
    # Second call reuses the still valid access token.
    await client.async_get_accounts()
    assert fake_nectr.calls.count("refreshToken") == 1

    fake_nectr.expire_access_tokens()
    await client.async_get_accounts()
    assert tokens == ["rt-1", "rt-2"]


async def test_refresh_soon_expiring_token(hass, fake_nectr: FakeNectr) -> None:
    """A token close to expiry is refreshed before use."""
    fake_nectr.access_lifetime = 30
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID, refresh_token="rt-initial")
    await client.async_get_accounts()
    await client.async_get_accounts()
    assert fake_nectr.calls.count("refreshToken") == 2


async def test_refresh_failure_is_auth_error(hass, fake_nectr: FakeNectr) -> None:
    """A rejected refresh token means the user must log in again."""
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID, refresh_token="stale")
    with pytest.raises(NectrAuthError):
        await client.async_get_accounts()
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID)
    with pytest.raises(NectrAuthError):
        await client.async_get_accounts()


async def test_dst_end_day_usage(hass, fake_nectr: FakeNectr) -> None:
    """A 25-hour day comes back with 25 distinct hour starts."""
    rows = [hour_row(h) for h in (0, 1, 2, 2, *range(3, 24))]
    fake_nectr.hourly[date(2026, 4, 5)] = rows
    client = NectrClient(async_get_clientsession(hass), device_id=DEVICE_ID, refresh_token="rt-initial")
    usage = await client.async_get_hourly_usage(ACCOUNT, date(2026, 4, 5), SYD)
    assert len({u.start for u in usage}) == 25
