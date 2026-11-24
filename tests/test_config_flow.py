"""Tests for the Nectr config flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nectr.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_DEVICE_ID,
    CONF_HISTORY_POLL_INTERVAL,
    CONF_OTP,
    CONF_REFRESH_TOKEN,
    CONF_RESEND,
    DOMAIN,
)

from .conftest import ACCOUNT, DEVICE_ID, EMAIL, OTP, PASSWORD, FakeNectr

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


async def _start(hass: HomeAssistant, password: str = PASSWORD):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: password}
    )


async def test_full_flow(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """Email, password and code create an entry without storing the password."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"
    assert result["description_placeholders"]["email"] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nectr 1 Test St"
    assert result["result"].unique_id == ACCOUNT
    data = result["data"]
    assert data[CONF_EMAIL] == EMAIL
    assert data[CONF_ACCOUNT_NUMBER] == ACCOUNT
    assert data[CONF_REFRESH_TOKEN] == "rt-1"
    assert len(data[CONF_DEVICE_ID]) == 36
    assert CONF_PASSWORD not in data


@pytest.mark.parametrize(
    ("password", "error"),
    [("wrong", "invalid_auth"), ("too-many", "too_many_attempts")],
)
async def test_login_errors(hass, fake_nectr, password, error) -> None:
    """Bad logins show an error and can be retried."""
    result = await _start(hass, password)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "otp"


async def test_cannot_connect(hass, aioclient_mock) -> None:
    """Connection errors are reported."""
    from custom_components.nectr.api import API_URL

    aioclient_mock.post(API_URL, exc=TimeoutError())
    result = await _start(hass)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_otp_errors_and_resend(
    hass: HomeAssistant, fake_nectr: FakeNectr
) -> None:
    """Wrong, empty and expired codes show errors; resending gives a new code."""
    result = await _start(hass)
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_OTP: "000000"}
    )
    assert result["errors"] == {CONF_OTP: "invalid_otp"}

    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["errors"] == {CONF_OTP: "invalid_otp"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_OTP: "expired"}
    )
    assert result["errors"] == {CONF_OTP: "otp_expired"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_RESEND: True}
    )
    assert result["step_id"] == "otp"
    assert result["errors"] == {}
    assert result["description_placeholders"]["status"]
    assert fake_nectr.otp_session == "sess-2"

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_OTP: OTP})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_login_session_expired(
    hass: HomeAssistant, fake_nectr: FakeNectr
) -> None:
    """An expired login session tells the user to start again."""
    fake_nectr.otp_session = "dead"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["errors"] == {"base": "login_expired"}


async def test_multiple_accounts(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """With several active accounts the user picks one; closed ones are hidden."""
    fake_nectr.accounts = [
        {"number": "111", "status": "ACTIVE", "address": "1 A St"},
        {"number": "222", "status": "PENDING", "address": None},
        {"number": "333", "status": "CLOSED", "address": "3 C St"},
    ]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["step_id"] == "account"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_NUMBER: "222"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nectr 222"


async def test_no_accounts(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """A login with no active accounts aborts."""
    fake_nectr.accounts = [{"number": "333", "status": "CLOSED", "address": None}]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_accounts"


async def test_not_smart_meter(hass: HomeAssistant, fake_nectr: FakeNectr) -> None:
    """Accounts without a smart meter can't be added."""
    fake_nectr.account_info["isSmartMeterUser"] = False
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_smart_meter"


async def test_already_configured(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """The same account can't be added twice."""
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Re-auth asks for the password and a code, then updates the token."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "otp"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_REFRESH_TOKEN] == "rt-1"
    assert config_entry.data[CONF_DEVICE_ID] == DEVICE_ID


async def test_reauth_wrong_account(
    hass: HomeAssistant, fake_nectr: FakeNectr, config_entry: MockConfigEntry
) -> None:
    """Re-auth with a login that can't see the account aborts."""
    config_entry.add_to_hass(hass)
    fake_nectr.accounts = [{"number": "999", "status": "ACTIVE", "address": None}]
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: OTP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_REFRESH_TOKEN] == "rt-initial"


async def test_options_flow(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The history poll interval defaults to 5 minutes and can be changed."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = result["data_schema"].schema
    default = next(k for k in schema if k == CONF_HISTORY_POLL_INTERVAL).default()
    assert default == 5

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORY_POLL_INTERVAL: 15.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_HISTORY_POLL_INTERVAL: 15}
