"""The requests Nectr's API is sent: endpoint, headers' app version and GraphQL documents.

Nectr's web portal API is undocumented, so these mirror what the My Account
website sends. This module is deliberately standalone: it uses only the
standard library and imports nothing from the rest of the integration (or from
Home Assistant), so ``scripts/check_api_schema.py`` can load it by path with no
packages installed, and so the Nectr API code can move to its own repository
later if that is ever worthwhile.
"""

import json

API_URL = "https://mobile.nectr.com.au/graphql"
# Sent as the App-Version header, as the web app does.
APP_VERSION = "2.9.0"

ACCOUNTS_QUERY = """
query getUserBrief {
  userBrief { accounts { number status address } }
}"""

ACCOUNT_INFO_QUERY = """
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

USAGE_QUERY = """
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

REQUEST_OTP_MUTATION = """
mutation requestLoginOtp($email: String!, $password: String!, $deviceId: String!) {
  requestLoginOtp(email: $email, password: $password, deviceId: $deviceId) {
    loginSessionId
  }
}"""

VERIFY_OTP_MUTATION = """
mutation verifyLoginOtp($loginSessionId: String!, $otp: String!, $deviceId: String!) {
  verifyLoginOtp(loginSessionId: $loginSessionId, otp: $otp, deviceId: $deviceId) {
    token
    refreshToken
  }
}"""

RESEND_OTP_MUTATION = """
mutation resendLoginOtp($loginSessionId: String!, $deviceId: String!) {
  resendLoginOtp(loginSessionId: $loginSessionId, deviceId: $deviceId) {
    loginSessionId
  }
}"""


def refresh_mutation(refresh_token: str) -> str:
    """Return the token refresh mutation.

    The web app inlines the refresh token in the query rather than passing a
    variable. json.dumps gives a safely quoted literal.
    """
    return (
        "mutation refreshToken { refreshTokens(refreshToken: "
        f"{json.dumps(refresh_token)}) {{ accessToken refreshToken }} }}"
    )
