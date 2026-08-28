# Money Flow Dashboard

A local AKShare-based A-share sector fund-flow dashboard. It pulls Tonghuashun
industry/concept fund-flow snapshots, optional stock big-deal data, and renders a
self-refreshing HTML dashboard.

## What It Tracks

- A-share industry and concept fund-flow snapshots
- Intraday sector net inflow/outflow lines
- Date-range cumulative fund-flow lines
- Complete sector table sorted by net inflow
- Stock big-deal net buy/sell radar
- Nasdaq 100 continuous futures reference panel
- Failure-only status logging for scheduled pulls

## Setup

```bash
python -m pip install -r requirements.txt
```

By default, data is written under `./akshare_data`. You can override paths:

```bash
export MONEY_FLOW_PROJECT_DIR=/path/to/money-flow
export MONEY_FLOW_DATA_ROOT=/path/to/money-flow/akshare_data
```

## Run Once

```bash
python pull_akshare_sector_fund_flow.py
python build_sector_fund_flow_dashboard.py
```

Open:

```text
akshare_data/sector_fund_flow_dashboard.html
```

## Scheduled Run

`run_sector_fund_flow_schedule_tick.py` is the launchd/cron-friendly entrypoint.
It only pulls during A-share trading windows unless `--force` is used. Core
sector data is required; stock big-deal data is pulled after the core snapshot
and failures do not block dashboard generation.

```bash
python run_sector_fund_flow_schedule_tick.py --force
```

The plist files in this repo are examples for the original macOS deployment and
may need path edits before loading on another machine.
