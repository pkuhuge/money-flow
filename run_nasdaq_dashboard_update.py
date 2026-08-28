#!/usr/bin/env python3
"""Refresh dashboard Nasdaq futures data once before A-share open."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(os.environ.get("MONEY_FLOW_PROJECT_DIR", Path(__file__).resolve().parent))
PYTHON = Path("/opt/anaconda3/bin/python")
DASHBOARD_SCRIPT = PROJECT_DIR / "build_sector_fund_flow_dashboard.py"
LOG_DIR = PROJECT_DIR / "akshare_data" / "logs"


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (LOG_DIR / "nasdaq_dashboard_update.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def is_trading_day(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        today = now.strftime("%Y-%m-%d")
        values = {str(value)[:10] for value in df["trade_date"].dropna().tolist()}
        return today in values
    except Exception as exc:
        log(f"calendar check failed, fallback to weekday: {type(exc).__name__}: {exc}")
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run even if today is not an exchange trading day.")
    args = parser.parse_args()

    now = datetime.now()
    if not args.force and not is_trading_day(now):
        return 0

    cmd = [str(PYTHON), str(DASHBOARD_SCRIPT)]
    completed = subprocess.run(cmd, cwd=str(PROJECT_DIR), text=True, capture_output=True, check=False, timeout=180)
    if completed.returncode != 0:
        log(f"dashboard update failed exit={completed.returncode}")
        if completed.stdout:
            log("stdout:\n" + completed.stdout.rstrip())
        if completed.stderr:
            log("stderr:\n" + completed.stderr.rstrip())
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
