#!/usr/bin/env python3
"""Pull current A-share sector fund-flow snapshots through AKShare."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from pull_akshare_monthly_daily_data import DEFAULT_OUTPUT_ROOT, pull_optional_table, pull_sector_fund_flows, write_table


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--include-big-deal", action="store_true", help="Also pull slower optional stock big-deal data.")
    args = parser.parse_args()

    import akshare as ak

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_sector_fund_flow")
    out_dir = Path(args.output_root) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "akshare_sector_fund_flow.sqlite"

    print(f"[{now_text()}] output={out_dir}")
    conn = sqlite3.connect(db_path)
    try:
        meta = pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "created_at": now_text(),
                    "retries": args.retries,
                    "akshare_version": getattr(ak, "__version__", "unknown"),
                }
            ]
        )
        write_table(conn, out_dir, "run_meta", meta)
        results = pull_sector_fund_flows(ak, conn, out_dir, args.retries)
        if args.include_big_deal:
            results.append(
                pull_optional_table(
                    "stock_fund_flow_big_deal",
                    conn,
                    out_dir,
                    ak.stock_fund_flow_big_deal,
                    args.retries,
                )
            )
        summary = pd.DataFrame([result.__dict__ for result in results])
        write_table(conn, out_dir, "pull_summary", summary)
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "output_dir": str(out_dir),
                    "sqlite": str(db_path),
                    "created_at": now_text(),
                    "results": [result.__dict__ for result in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(summary.to_string(index=False))
        print(f"[{now_text()}] sqlite={db_path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
