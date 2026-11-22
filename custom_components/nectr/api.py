"""Async client for the Nectr customer GraphQL API.

This module only depends on aiohttp so it can later be split out into a
standalone library. The API is the one used by the Nectr "My Account" web app
(account.nectr.com.au); it is undocumented and may change without notice.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import json
import logging
import time as time_mod
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_URL = "https://mobile.nectr.com.au/graphql"
APP_VERSION = "2.9.0"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Refresh the access token when it has less than this many seconds left.
TOKEN_REFRESH_MARGIN = 120

DATE_FORMAT = "%d/%m/%Y"


class NectrError(Exception):
    """Base error for the Nectr API."""


class NectrConnectionError(NectrError):
    """The API could not be reached or returned an unexpected response."""


class NectrAuthError(NectrError):
    """The session is no longer valid and the user must log in again."""


class NectrInvalidCredentialsError(NectrError):
    """Email or password was rejected."""


class NectrTooManyAttemptsError(NectrError):
    """Too many failed login or OTP attempts."""


class NectrInvalidOtpError(NectrError):
    """The OTP code was wrong."""


class NectrOtpExpiredError(NectrError):
    """The OTP code has expired; a new one must be requested."""


class NectrLoginSessionExpiredError(NectrError):
    """The login session has expired; the login must start again."""


class NectrGraphQLError(NectrError):
    """The API returned GraphQL errors."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        """Initialise with the raw GraphQL error list."""
        self.errors = errors
        super().__init__("; ".join(str(e.get("message")) for e in errors))

    @property
    def codes(self) -> set[str]:
        """Return the extension codes of the errors."""
        return {
            str((e.get("extensions") or {}).get("code"))
            for e in self.errors
            if (e.get("extensions") or {}).get("code")
        }

    @property
    def is_unauthorized(self) -> bool:
        """Return True if any error carries an HTTP 401 status."""
        for err in self.errors:
            response = (err.get("extensions") or {}).get("response") or {}
            if response.get("statusCode") == 401:
                return True
        return False

    def message_contains(self, text: str) -> bool:
        """Return True if any error message contains text (case insensitive)."""
        text = text.lower()
        return any(text in str(e.get("message", "")).lower() for e in self.errors)


@dataclass(frozen=True, slots=True)
class Account:
    """A Nectr account belonging to the logged in user."""

    number: str
    status: str
    address: str | None

    @property
    def is_active(self) -> bool:
        """Return True for accounts the web app treats as usable."""
        return self.status.lower() in ("active", "pending")


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """Details of one account."""

    is_smart_meter: bool
    has_export: bool
    has_controlled_load: bool
    plan_name: str | None
    billing_period_start: date | None
    billing_period_end: date | None


@dataclass(frozen=True, slots=True)
class BillPeriod:
    """A closed billing period."""

    start: date
    end: date


@dataclass(frozen=True, slots=True)
class HourlyUsage:
    """Usage for one hour, as billed by Nectr."""

    start: datetime
    grid_kwh: float
    grid_cost: float
    export_kwh: float
    export_credit: float
    controlled_load_kwh: float
    controlled_load_cost: float


TokenCallback = Callable[[str], Awaitable[None] | None]


_ACCOUNTS_QUERY = """
query getUserBrief {
  userBrief { accounts { number status address } }
}"""

_ACCOUNT_INFO_QUERY = """
query getAccountInfo($accountNumber: String!) {
  accountInfo(accountNumber: $accountNumber) {
    isSmartMeterUser
    isExportRegister
    isControlledLoad
    planName
    currentBillingPeriodStartDate
    currentBillingPeriodEndDate
  }
}"""

_USAGE_QUERY = """
query getUsageInfo($accountNumber: String!, $isSmartMeterUser: Boolean!,
    $pageNumber: Int!, $granularity: GRANUALRITY, $fromDate: String!,
    $toDate: String!) {
  getUsageInfo(accountNumber: $accountNumber, isSmartMeterUser: $isSmartMeterUser,
      pageNumber: $pageNumber, granularity: $granularity, fromDate: $fromDate,
      toDate: $toDate) {
    message
    hasNextPage
    allUsage {
      period
      fromDate
      toDate
      gridUsage
      gridCost
      exportUsage
      exportCost
      controlLoadUsage
      controlLoadCost
    }
  }
}"""

_REQUEST_OTP_MUTATION = """
mutation requestLoginOtp($email: String!, $password: String!, $deviceId: String!) {
  requestLoginOtp(email: $email, password: $password, deviceId: $deviceId) {
    loginSessionId
  }
}"""

_VERIFY_OTP_MUTATION = """
mutation verifyLoginOtp($loginSessionId: String!, $otp: String!, $deviceId: String!) {
  verifyLoginOtp(loginSessionId: $loginSessionId, otp: $otp, deviceId: $deviceId) {
    token
    refreshToken
  }
}"""

_RESEND_OTP_MUTATION = """
mutation resendLoginOtp($loginSessionId: String!, $deviceId: String!) {
  resendLoginOtp(loginSessionId: $loginSessionId, deviceId: $deviceId) {
    loginSessionId
  }
}"""


def _jwt_expiry(token: str) -> float | None:
    """Return the exp claim of a JWT, or None if it can't be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO (yyyy-mm-dd) or Nectr (dd/mm/yyyy) date."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", DATE_FORMAT):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def hour_starts_for_labels(day: date, labels: list[str], tz: tzinfo) -> list[datetime]:
    """Map Nectr hour labels for one day to UTC hour-start datetimes.

    Nectr labels hourly rows only with the local hour ("0:00" ... "23:00").
    On the day daylight saving starts that day has 23 hours, and on the day it
    ends the repeated hour appears twice. The labels must be given in
    chronological order; each one is matched to the next real hour of the day
    with that local hour, so repeated hours resolve in order and missing hours
    (days Nectr has only partly published) are simply skipped.
    """
    start = datetime.combine(day, time(), tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tz).astimezone(UTC)
    instants: list[datetime] = []
    current = start
    while current < end:
        instants.append(current)
        current += timedelta(hours=1)
    local_hours = [i.astimezone(tz).hour for i in instants]

    result: list[datetime] = []
    idx = 0
    for label in labels:
        try:
            hour = int(label.split(":", 1)[0])
        except ValueError as err:
            raise NectrConnectionError(f"Unexpected hour label {label!r}") from err
        while idx < len(instants) and local_hours[idx] != hour:
            idx += 1
        if idx >= len(instants):
            raise NectrConnectionError(
                f"Hour labels for {day} do not fit the local day: {labels}"
            )
        result.append(instants[idx])
        idx += 1
    return result


class NectrClient:
    """Client for the Nectr GraphQL API.

    The client holds an access token (valid about 30 minutes) and a refresh
    token (valid about 40 days, renewed on every refresh). Whenever a new
    refresh token is issued, ``token_callback`` is called with it so the caller
    can persist it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        device_id: str,
        refresh_token: str | None = None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._device_id = device_id
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._access_expiry: float | None = None
        self._token_callback = token_callback
        self._refresh_lock = asyncio.Lock()

    @property
    def device_id(self) -> str:
        """Return the device id used for login."""
        return self._device_id

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    # Transport

    async def _post(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Send one GraphQL request and return its data."""
        headers = {
            "Content-Type": "application/json",
            "X-Client-Type": "web",
            "App-Version": APP_VERSION,
        }
        if token:
            headers["Authorization"] = f"bearer {token}"
        payload: dict[str, Any] = {"operationName": operation, "query": query}
        if variables is not None:
            payload["variables"] = variables

        try:
            async with self._session.post(
                API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status == 401:
                    raise NectrGraphQLError(
                        [{"message": "Unauthorized", "extensions": {"response": {"statusCode": 401}}}]
                    )
                if resp.status >= 400:
                    raise NectrConnectionError(f"{operation}: HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NectrConnectionError(f"{operation}: {err}") from err
        except ValueError as err:
            raise NectrConnectionError(f"{operation}: invalid JSON") from err

        if not isinstance(body, dict):
            raise NectrConnectionError(f"{operation}: unexpected response")
        if body.get("errors"):
            raise NectrGraphQLError(body["errors"])
        data = body.get("data")
        if not isinstance(data, dict):
            raise NectrConnectionError(f"{operation}: response has no data")
        return data

    async def _store_tokens(self, access: str, refresh: str) -> None:
        self._access_token = access
        self._access_expiry = _jwt_expiry(access)
        if refresh != self._refresh_token:
            self._refresh_token = refresh
            if self._token_callback is not None:
                result = self._token_callback(refresh)
                if asyncio.iscoroutine(result):
                    await result

    def _access_token_valid(self) -> bool:
        if not self._access_token:
            return False
        if self._access_expiry is None:
            return True
        return self._access_expiry - TOKEN_REFRESH_MARGIN > time_mod.time()

    async def async_refresh_tokens(self, *, force: bool = False) -> None:
        """Exchange the refresh token for a new access and refresh token."""
        async with self._refresh_lock:
            if not force and self._access_token_valid():
                return
            if not self._refresh_token:
                raise NectrAuthError("No refresh token; log in again")
            # The web app inlines the refresh token in the query rather than
            # passing a variable. json.dumps gives a safely quoted literal.
            query = (
                "mutation refreshToken { refreshTokens(refreshToken: "
                f"{json.dumps(self._refresh_token)}) {{ accessToken refreshToken }} }}"
            )
            try:
                data = await self._post("refreshToken", query)
            except NectrGraphQLError as err:
                # "reset" means Nectr wants a password reset; anything else
                # means the refresh token is no longer valid either way.
                raise NectrAuthError(f"Token refresh rejected: {err}") from err
            tokens = data.get("refreshTokens") or {}
            access, refresh = tokens.get("accessToken"), tokens.get("refreshToken")
            if not access or not refresh:
                raise NectrAuthError("Token refresh returned no tokens")
            await self._store_tokens(access, refresh)

    async def _authed(
        self, operation: str, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send an authenticated request, refreshing the token if needed."""
        await self.async_refresh_tokens()
        try:
            return await self._post(
                operation, query, variables, token=self._access_token
            )
        except NectrGraphQLError as err:
            if not err.is_unauthorized:
                raise
        await self.async_refresh_tokens(force=True)
        try:
            return await self._post(
                operation, query, variables, token=self._access_token
            )
        except NectrGraphQLError as err:
            if err.is_unauthorized:
                raise NectrAuthError(f"{operation}: unauthorized") from err
            raise

    # Login

    async def async_request_login_otp(self, email: str, password: str) -> str:
        """Start a login. Nectr emails a code; returns the login session id."""
        try:
            data = await self._post(
                "requestLoginOtp",
                _REQUEST_OTP_MUTATION,
                {"email": email, "password": password, "deviceId": self._device_id},
            )
        except NectrGraphQLError as err:
            if err.message_contains("too many"):
                raise NectrTooManyAttemptsError(str(err)) from err
            raise NectrInvalidCredentialsError(str(err)) from err
        session_id = (data.get("requestLoginOtp") or {}).get("loginSessionId")
        if not session_id:
            raise NectrConnectionError("requestLoginOtp returned no session id")
        return str(session_id)

    async def async_resend_login_otp(self, login_session_id: str) -> str:
        """Ask Nectr to send a new code; returns the (new) login session id."""
        try:
            data = await self._post(
                "resendLoginOtp",
                _RESEND_OTP_MUTATION,
                {"loginSessionId": login_session_id, "deviceId": self._device_id},
            )
        except NectrGraphQLError as err:
            raise NectrLoginSessionExpiredError(str(err)) from err
        session_id = (data.get("resendLoginOtp") or {}).get("loginSessionId")
        return str(session_id or login_session_id)

    async def async_verify_login_otp(self, login_session_id: str, otp: str) -> None:
        """Finish a login with the emailed code and store the tokens."""
        try:
            data = await self._post(
                "verifyLoginOtp",
                _VERIFY_OTP_MUTATION,
                {
                    "loginSessionId": login_session_id,
                    "otp": otp.strip(),
                    "deviceId": self._device_id,
                },
            )
        except NectrGraphQLError as err:
            # Mirrors how the web app interprets these errors.
            if "OTP_EXPIRED" in err.codes:
                raise NectrLoginSessionExpiredError(str(err)) from err
            if err.message_contains("too many"):
                raise NectrTooManyAttemptsError(str(err)) from err
            if err.message_contains("expired"):
                raise NectrOtpExpiredError(str(err)) from err
            raise NectrInvalidOtpError(str(err)) from err
        tokens = data.get("verifyLoginOtp") or {}
        access, refresh = tokens.get("token"), tokens.get("refreshToken")
        if not access or not refresh:
            raise NectrConnectionError("verifyLoginOtp returned no tokens")
        await self._store_tokens(access, refresh)

    # Data

    async def async_get_accounts(self) -> list[Account]:
        """Return the accounts of the logged in user."""
        data = await self._authed("getUserBrief", _ACCOUNTS_QUERY)
        accounts = ((data.get("userBrief") or {}).get("accounts")) or []
        return [
            Account(
                number=str(a["number"]),
                status=str(a.get("status") or ""),
                address=a.get("address"),
            )
            for a in accounts
            if a.get("number")
        ]

    async def async_get_account_info(self, account_number: str) -> AccountInfo:
        """Return details of one account."""
        data = await self._authed(
            "getAccountInfo", _ACCOUNT_INFO_QUERY, {"accountNumber": account_number}
        )
        info = data.get("accountInfo") or {}
        return AccountInfo(
            is_smart_meter=bool(info.get("isSmartMeterUser", True)),
            has_export=bool(info.get("isExportRegister")),
            has_controlled_load=bool(info.get("isControlledLoad")),
            plan_name=info.get("planName"),
            billing_period_start=_parse_date(info.get("currentBillingPeriodStartDate")),
            billing_period_end=_parse_date(info.get("currentBillingPeriodEndDate")),
        )

    async def _usage(
        self,
        account_number: str,
        granularity: str,
        from_date: str,
        to_date: str,
        page: int = 1,
    ) -> dict[str, Any]:
        data = await self._authed(
            "getUsageInfo",
            _USAGE_QUERY,
            {
                "accountNumber": account_number,
                "isSmartMeterUser": True,
                "pageNumber": page,
                "granularity": granularity,
                "fromDate": from_date,
                "toDate": to_date,
            },
        )
        return data.get("getUsageInfo") or {}

    async def async_get_bill_periods(
        self, account_number: str, *, max_pages: int = 20
    ) -> list[BillPeriod]:
        """Return closed billing periods, newest first.

        The current, still open period is not included; see AccountInfo.
        """
        periods: list[BillPeriod] = []
        for page in range(1, max_pages + 1):
            usage = await self._usage(account_number, "PERIOD", "", "", page)
            for row in usage.get("allUsage") or []:
                start = _parse_date(row.get("fromDate"))
                end = _parse_date(row.get("toDate"))
                if start and end:
                    periods.append(BillPeriod(start, end))
            if not usage.get("hasNextPage"):
                break
        return periods

    async def async_get_hourly_usage(
        self, account_number: str, day: date, tz: tzinfo
    ) -> list[HourlyUsage]:
        """Return the hourly usage Nectr has published for one local day.

        Returns an empty list if Nectr has nothing for the day yet. Days are
        often only partly published at first.
        """
        usage = await self._usage(
            account_number,
            "HOURLY",
            day.strftime(DATE_FORMAT),
            (day + timedelta(days=1)).strftime(DATE_FORMAT),
        )
        # Rows come newest first and carry only the hour; a multi-day request
        # can't be split back into days, so the caller asks for one day.
        rows = list(reversed(usage.get("allUsage") or []))
        if not rows:
            return []
        starts = hour_starts_for_labels(day, [str(r.get("period")) for r in rows], tz)

        def num(row: dict[str, Any], key: str) -> float:
            value = row.get(key)
            return float(value) if value is not None else 0.0

        return [
            HourlyUsage(
                start=start,
                grid_kwh=num(row, "gridUsage"),
                grid_cost=num(row, "gridCost"),
                export_kwh=num(row, "exportUsage"),
                export_credit=num(row, "exportCost"),
                controlled_load_kwh=num(row, "controlLoadUsage"),
                controlled_load_cost=num(row, "controlLoadCost"),
            )
            for start, row in zip(starts, rows, strict=True)
        ]
