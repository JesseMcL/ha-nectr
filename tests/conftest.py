"""Fixtures for Nectr tests, including a fake Nectr GraphQL server."""

from __future__ import annotations

import base64
from collections.abc import Generator
from datetime import date
import json
import re
import time
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.nectr.api import API_URL
from custom_components.nectr.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

ACCOUNT = "1000123"
EMAIL = "user@example.com"
PASSWORD = "good-password"
OTP = "123456"
DEVICE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return


def make_jwt(exp: float) -> str:
    """Build an unsigned JWT with the given expiry."""

    def b64(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'sub': 'x'})}.sig"


def hour_row(hour: int, grid: float = 1.0, export: float = 0.0, cl: float = 0.0):
    """Build one Nectr hourly row."""
    return {
        "period": f"{hour}:00",
        "fromDate": None,
        "toDate": None,
        "gridUsage": grid,
        "gridCost": round(grid * 0.3, 4),
        "exportUsage": export,
        "exportCost": round(export * 0.05, 4),
        "controlLoadUsage": cl,
        "controlLoadCost": round(cl * 0.17, 4),
    }


def full_day(grid: float = 1.0, export: float = 0.0, cl: float = 0.0):
    """Build 24 chronological hourly rows."""
    return [hour_row(h, grid, export, cl) for h in range(24)]


class FakeNectr:
    """A small in-memory imitation of the Nectr GraphQL API."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        """Register with the aiohttp mocker."""
        self._mocker = mocker
        self.accounts = [{"number": ACCOUNT, "status": "ACTIVE", "address": "1 Test St"}]
        self.account_info = {
            "isSmartMeterUser": True,
            "isExportRegister": True,
            "isControlledLoad": True,
            "planName": "Test Plan",
            "currentBillingPeriodStartDate": "2026-11-19",
            "currentBillingPeriodEndDate": "2026-12-18",
        }
        self.bill_periods: list[tuple[str, str]] = []
        # date -> chronological hourly rows
        self.hourly: dict[date, list[dict[str, Any]]] = {}
        self.valid_refresh_tokens: set[str] = {"rt-initial"}
        self.access_tokens: set[str] = set()
        self.token_counter = 0
        self.access_lifetime = 1800
        self.refresh_fails = False
        self.calls: list[str] = []
        self.hourly_requests: list[str] = []
        self.otp_session = "sess-1"
        mocker.post(API_URL, side_effect=self._handle)

    def _issue(self) -> tuple[str, str]:
        self.token_counter += 1
        access = make_jwt(time.time() + self.access_lifetime) + str(self.token_counter)
        refresh = f"rt-{self.token_counter}"
        self.access_tokens = {access}
        self.valid_refresh_tokens = {refresh}
        return access, refresh

    def expire_access_tokens(self) -> None:
        """Make the server reject all current access tokens."""
        self.access_tokens = set()

    @staticmethod
    def _resp(body: dict[str, Any], status: int = 200) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse("post", API_URL, status=status, json=body)

    @staticmethod
    def _error(message: str, **ext: Any) -> dict[str, Any]:
        return {"errors": [{"message": message, "extensions": ext}], "data": None}

    async def _handle(self, method, url, data):
        op = data.get("operationName")
        self.calls.append(op)
        headers = self._mocker.mock_calls[-1][3] or {}
        variables = data.get("variables") or {}

        if op == "requestLoginOtp":
            if variables["password"] == "too-many":
                return self._resp(self._error("Too many login attempts"))
            if variables["password"] != PASSWORD:
                return self._resp(self._error("Invalid email or password"))
            return self._resp({"data": {"requestLoginOtp": {"loginSessionId": self.otp_session}}})
        if op == "resendLoginOtp":
            self.otp_session = "sess-2"
            return self._resp({"data": {"resendLoginOtp": {"loginSessionId": "sess-2"}}})
        if op == "verifyLoginOtp":
            if variables["loginSessionId"] == "dead":
                return self._resp(self._error("Session expired", code="OTP_EXPIRED"))
            if variables["loginSessionId"] != self.otp_session:
                return self._resp(self._error("Invalid session"))
            if variables["otp"] == "expired":
                return self._resp(self._error("OTP has expired"))
            if variables["otp"] != OTP:
                return self._resp(self._error("Invalid OTP"))
            access, refresh = self._issue()
            return self._resp({"data": {"verifyLoginOtp": {"token": access, "refreshToken": refresh}}})
        if op == "refreshToken":
            match = re.search(r'refreshToken: "([^"]+)"', data["query"])
            token = match.group(1) if match else None
            if self.refresh_fails or token not in self.valid_refresh_tokens:
                return self._resp(self._error("Invalid refresh token"))
            access, refresh = self._issue()
            return self._resp({"data": {"refreshTokens": {"accessToken": access, "refreshToken": refresh}}})

        auth = headers.get("Authorization", "")
        if auth.removeprefix("bearer ") not in self.access_tokens:
            return self._resp(self._error("Unauthorized", response={"statusCode": 401}))

        if op == "getUserBrief":
            return self._resp({"data": {"userBrief": {"accounts": self.accounts}}})
        if op == "getAccountInfo":
            return self._resp({"data": {"accountInfo": self.account_info}})
        if op == "getUsageInfo":
            return self._resp({"data": {"getUsageInfo": self._usage(variables)}})
        raise AssertionError(f"Unexpected operation {op}")

    def _usage(self, v: dict[str, Any]) -> dict[str, Any]:
        if v["granularity"] == "PERIOD":
            page = v["pageNumber"]
            rows = self.bill_periods[(page - 1) * 7 : page * 7]
            return {
                "message": "",
                "hasNextPage": len(self.bill_periods) > page * 7,
                "allUsage": [{"fromDate": f, "toDate": t, "period": ""} for f, t in rows],
            }
        assert v["granularity"] == "HOURLY"
        self.hourly_requests.append(v["fromDate"])
        day, _ = v["fromDate"].split("/", 1)
        d = date(int(v["fromDate"][6:]), int(v["fromDate"][3:5]), int(day))
        rows = self.hourly.get(d, [])
        if not rows:
            return {"message": "Oops! No Data Found.", "hasNextPage": False, "allUsage": []}
        return {"message": "", "hasNextPage": False, "allUsage": list(reversed(rows))}


@pytest.fixture
def fake_nectr(aioclient_mock: AiohttpClientMocker) -> FakeNectr:
    """Return a fake Nectr server."""
    return FakeNectr(aioclient_mock)


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Skip setting up the entry during config flow tests."""
    with patch("custom_components.nectr.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Nectr {ACCOUNT}",
        unique_id=ACCOUNT,
        data={
            "email": EMAIL,
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_ACCOUNT_NUMBER: ACCOUNT,
            CONF_REFRESH_TOKEN: "rt-initial",
        },
    )
