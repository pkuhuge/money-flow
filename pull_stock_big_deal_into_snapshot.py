#!/usr/bin/env python3
"""Pull Tonghuashun stock big-deal data into an existing snapshot directory."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from pull_akshare_monthly_daily_data import write_table


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", required=True)
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)
    if not snapshot_dir.exists():
        print(f"[{now_text()}] snapshot dir not found: {snapshot_dir}")
        return 2

    import akshare as ak

    db_path = snapshot_dir / "akshare_sector_fund_flow.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        df = ak.stock_fund_flow_big_deal()
        rows = write_table(conn, snapshot_dir, "stock_fund_flow_big_deal", df)
    finally:
        conn.close()

    print(f"[{now_text()}] rows={rows}")
    return 0 if rows > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
