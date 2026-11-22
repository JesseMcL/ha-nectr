"""Config flow for Nectr.

Nectr logins need a one-time code that Nectr emails on each login. The code is
only needed here, during setup and re-authentication: afterwards the
integration keeps itself signed in with the refresh token, which Nectr renews
on every use. The password is never stored.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
import uuid

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    Account,
    NectrClient,
    NectrConnectionError,
    NectrError,
    NectrInvalidCredentialsError,
    NectrInvalidOtpError,
    NectrLoginSessionExpiredError,
    NectrOtpExpiredError,
    NectrTooManyAttemptsError,
)
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_DEVICE_ID,
    CONF_OTP,
    CONF_REFRESH_TOKEN,
    CONF_RESEND,
    DOMAIN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class NectrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nectr."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._email: str | None = None
        self._device_id: str | None = None
        self._client: NectrClient | None = None
        self._login_session: str | None = None
        self._accounts: list[Account] = []

    def _make_client(self) -> NectrClient:
        assert self._device_id
        return NectrClient(
            async_get_clientsession(self.hass), device_id=self._device_id
        )

    async def _async_request_code(self, password: str) -> str | None:
        """Ask Nectr to email a code. Returns an error key or None."""
        self._client = self._make_client()
        assert self._email
        try:
            self._login_session = await self._client.async_request_login_otp(
                self._email, password
            )
        except NectrInvalidCredentialsError:
            return "invalid_auth"
        except NectrTooManyAttemptsError:
            return "too_many_attempts"
        except NectrConnectionError:
            return "cannot_connect"
        except NectrError:
            _LOGGER.exception("Unexpected error requesting a Nectr login code")
            return "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            # A stable id Nectr uses to recognise this "device" (one per
            # config entry), like the id the web app keeps in the browser.
            self._device_id = str(uuid.uuid4())
            if not (error := await self._async_request_code(user_input[CONF_PASSWORD])):
                return await self.async_step_otp()
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=self._email or ""): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the code Nectr emailed."""
        assert self._client and self._login_session and self._email
        errors: dict[str, str] = {}
        status = "sent"

        if user_input is not None:
            code = (user_input.get(CONF_OTP) or "").strip()
            if user_input.get(CONF_RESEND):
                try:
                    self._login_session = await self._client.async_resend_login_otp(
                        self._login_session
                    )
                    status = "resent"
                except NectrLoginSessionExpiredError:
                    errors["base"] = "login_expired"
                except NectrError:
                    errors["base"] = "cannot_connect"
            elif not code:
                errors[CONF_OTP] = "invalid_otp"
            else:
                try:
                    await self._client.async_verify_login_otp(self._login_session, code)
                    self._accounts = [
                        a
                        for a in await self._client.async_get_accounts()
                        if a.is_active
                    ]
                except NectrInvalidOtpError:
                    errors[CONF_OTP] = "invalid_otp"
                except NectrOtpExpiredError:
                    errors[CONF_OTP] = "otp_expired"
                except NectrTooManyAttemptsError:
                    errors["base"] = "too_many_otp_attempts"
                except NectrLoginSessionExpiredError:
                    errors["base"] = "login_expired"
                except NectrConnectionError:
                    errors["base"] = "cannot_connect"
                except NectrError:
                    _LOGGER.exception("Unexpected error verifying the Nectr login code")
                    errors["base"] = "unknown"
                else:
                    return await self._async_logged_in()

        schema = vol.Schema(
            {
                vol.Optional(CONF_OTP): str,
                vol.Optional(CONF_RESEND, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="otp",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "email": self._email,
                "status": "A new code has been sent." if status == "resent" else "",
            },
        )

    async def _async_logged_in(self) -> ConfigFlowResult:
        """Continue once the login succeeded."""
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            account = entry.data[CONF_ACCOUNT_NUMBER]
            if account not in {a.number for a in self._accounts}:
                return self.async_abort(reason="wrong_account")
            assert self._client
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_EMAIL: self._email,
                    CONF_DEVICE_ID: self._device_id,
                    CONF_REFRESH_TOKEN: self._client.refresh_token,
                },
            )

        if not self._accounts:
            return self.async_abort(reason="no_accounts")
        if len(self._accounts) == 1:
            return await self._async_create(self._accounts[0])
        return await self.async_step_account()

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which account to add when the login has several."""
        if user_input is not None:
            account = next(
                a for a in self._accounts if a.number == user_input[CONF_ACCOUNT_NUMBER]
            )
            return await self._async_create(account)

        options = [
            SelectOptionDict(
                value=a.number,
                label=f"{a.number} - {a.address}" if a.address else a.number,
            )
            for a in self._accounts
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_NUMBER): SelectSelector(
                    SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(step_id="account", data_schema=schema)

    async def _async_create(self, account: Account) -> ConfigFlowResult:
        await self.async_set_unique_id(account.number)
        self._abort_if_unique_id_configured()
        assert self._client
        return self.async_create_entry(
            title=f"{NAME} {account.address or account.number}",
            data={
                CONF_EMAIL: self._email,
                CONF_DEVICE_ID: self._device_id,
                CONF_ACCOUNT_NUMBER: account.number,
                CONF_REFRESH_TOKEN: self._client.refresh_token,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the refresh token stops working."""
        self._email = entry_data[CONF_EMAIL]
        self._device_id = entry_data.get(CONF_DEVICE_ID) or str(uuid.uuid4())
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again, then for a new code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (error := await self._async_request_code(user_input[CONF_PASSWORD])):
                return await self.async_step_otp()
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )
