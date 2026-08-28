#!/usr/bin/env python3
"""Add reliable AKShare fund-flow tables to an existing daily data run."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

from pull_akshare_monthly_daily_data import FetchResult, pull_fund_flow_rankings, write_table


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", help="Existing akshare_data/<run_id> directory.")
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    import akshare as ak

    run_dir = Path(args.run_dir).expanduser().resolve()
    db_path = run_dir / "akshare_daily.sqlite"
    if not db_path.exists():
        raise SystemExit(f"SQLite file not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        results = pull_fund_flow_rankings(ak, conn, run_dir, args.retries)
        existing = pd.read_sql_query("select * from pull_summary", conn)
        labels = {result.label for result in results}
        existing = existing[~existing["label"].isin(labels)].copy()
        updated = pd.concat([existing, pd.DataFrame([result.__dict__ for result in results])], ignore_index=True)
        write_table(conn, run_dir, "pull_summary", updated)

        summary_path = run_dir / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            summary = {"output_dir": str(run_dir), "sqlite": str(db_path), "results": []}
        previous = [item for item in summary.get("results", []) if item.get("label") not in labels]
        summary["results"] = previous + [result.__dict__ for result in results]
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(pd.DataFrame([result.__dict__ for result in results]).to_string(index=False))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
