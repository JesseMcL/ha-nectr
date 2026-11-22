"""Constants for the Nectr integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "nectr"
NAME: Final = "Nectr"

CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_DEVICE_ID: Final = "device_id"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_OTP: Final = "otp"
CONF_RESEND: Final = "resend"

# Normal polling once the statistics have caught up with Nectr. Nectr only
# publishes interval data the following day, so there is no point polling often.
UPDATE_INTERVAL: Final = timedelta(hours=3)
# Polling while the initial backfill is still working through history.
BACKFILL_UPDATE_INTERVAL: Final = timedelta(minutes=5)

# Each update re-fetches this many days before today and rewrites their
# statistics, because Nectr publishes days in pieces and fills them in later.
LOOKBACK_DAYS: Final = 4
# Maximum days fetched in a single update, so the initial backfill is spread
# over several updates instead of blocking setup for minutes.
MAX_DAYS_PER_UPDATE: Final = 31
# Hard limit on how far back the initial backfill goes.
MAX_BACKFILL_DAYS: Final = 3 * 365
# Concurrent day requests while fetching.
FETCH_CONCURRENCY: Final = 4
