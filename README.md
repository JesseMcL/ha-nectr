# Nectr for Home Assistant

Imports the hourly electricity usage and cost that [Nectr](https://nectr.com.au/) bills you for into Home Assistant's long-term statistics, so you can show it in the Energy dashboard next to your own real-time measurements.

This is an unofficial integration, not affiliated with or endorsed by Nectr; the Nectr name and logo belong to their owner and are used here only to identify the service. It uses the same API as the Nectr My Account website, which is undocumented and may change.

## What you get

Nectr publishes smart meter data a day or more after the fact, and often fills a day in over several updates. The usage is therefore imported as **statistics**, not as sensor states. For each channel your premises has, there are two statistics:

| Statistic | Unit |
| --- | --- |
| `nectr:<account>_grid_import`, `nectr:<account>_grid_import_cost` | kWh, $ |
| `nectr:<account>_export`, `nectr:<account>_export_credit` | kWh, $ |
| `nectr:<account>_controlled_load`, `nectr:<account>_controlled_load_cost` | kWh, $ |

Channels are discovered per premises: one is imported when Nectr says your account has that register, or when your data contains it (for example, export appears once solar is installed). Costs are the amounts Nectr calculated for each hour on your plan.

On first setup the integration backfills as much history as Nectr has (up to three years), a month at a time, starting with the most recent month and working back, so recent usage shows up straight away. It fetches the next month every few minutes (the **History poll interval** option, default 5 minutes) and remembers its progress across restarts. After that it checks every three hours and rewrites the last four days, so partly published days and corrections are picked up. Older days are not checked again automatically; use the [re-import action](#re-importing-older-days) for those.

Because older months are added before the existing statistics, the running total (`sum`) of the earliest hours can be negative. The Energy dashboard and statistics cards only use differences, so this doesn't affect what they show.

It also adds diagnostic sensors for the newest hour Nectr has published (with the discovered channels as an attribute) and the current billing period.

## Requirements

- A Nectr electricity account with a smart meter (accounts without one have no hourly data).
- Home Assistant's time zone set to the time zone of the premises, because Nectr reports hours in local time.

## Installation

### HACS

1. In HACS, open the menu, choose **Custom repositories** and add this repository's URL as an **Integration**.
2. Install **Nectr** and restart Home Assistant.

### Manual

Copy `custom_components/nectr` into your Home Assistant `custom_components` folder and restart.

## Setup

1. Go to **Settings → Devices & services → Add integration → Nectr**.
2. Enter the email and password you use for Nectr My Account.
3. Nectr emails you a 6-digit code. Enter it. If it doesn't arrive, tick **Send me a new code** and submit.
4. If your login has more than one account, choose one. Add the integration again for each other account.

Your password is not stored. The integration keeps itself signed in with the token Nectr issues, which Nectr renews each time it is used. If Nectr ever rejects it, Home Assistant asks you to sign in again with your password and a new code.

To change the history poll interval, open the Nectr integration and choose **Configure**.

## Energy dashboard

In **Settings → Dashboards → Energy**:

- **Grid consumption**: add *Nectr … grid import* and choose *Use an entity tracking the total costs* with *Nectr … grid import cost*.
- If you have controlled load (for example off-peak hot water), add *Nectr … controlled load* as a second grid consumption source with *Nectr … controlled load cost*.
- **Return to grid**: add *Nectr … export* with *Nectr … export credit* as the compensation.

To compare with your own real-time meter or CT clamp, keep those on the dashboard as well, or use a statistics graph card with both.

## Re-importing older days

Only the last four days are refreshed automatically. If Nectr corrects older data, or a day was missing when it was first imported, run the **Nectr: Re-import usage** action (`nectr.reimport`) from **Developer tools → Actions**:

```yaml
action: nectr.reimport
data:
  config_entry_id: <your Nectr entry>  # pick it from the list in the UI
  start_date: "2026-10-01"
  end_date: "2026-10-31"  # optional, defaults to today
```

The action fetches every day in the range again and rewrites its hourly statistics. The running totals of all later hours are then shifted by however much the range changed, so the Energy dashboard doesn't show a jump at the end of the range.

- Days Nectr returns no data for are left as they were. The action's response lists them under `days_without_data`, along with the number of `hours` imported.
- Each day is a separate request to Nectr (four at a time), so a long range can take a while. The action returns once the statistics have been written.
- The regular update waits while a re-import runs, and the other way round.

## Limitations

- Nectr only gives one combined figure per channel, so two controlled load circuits or separate time-of-use registers can't be told apart.
- Data is usually a day or more behind. The *Latest data* sensor shows how current it is.
- Solar generation and battery data from Nectr's VPP products are not imported.

## Development

```sh
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt ruff
ruff check . && ruff format --check .
pytest
```

Tests run against an in-memory fake of the Nectr API (`tests/conftest.py`). Notes on the API are in the docstrings of `custom_components/nectr/api.py`.
