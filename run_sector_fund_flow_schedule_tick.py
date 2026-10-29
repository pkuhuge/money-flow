#!/usr/bin/env python3
"""Run sector fund-flow pull only during A-share trading windows.

This wrapper is intended for launchd/cron. It exits quietly outside trading
time, so the scheduler can call it frequently without pulling data all day.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from datetime import datetime, time
from pathlib import Path


PROJECT_DIR = Path(os.environ.get("MONEY_FLOW_PROJECT_DIR", Path(__file__).resolve().parent))
PYTHON = Path("/opt/anaconda3/bin/python")
PULL_SCRIPT = PROJECT_DIR / "pull_akshare_sector_fund_flow.py"
DASHBOARD_SCRIPT = PROJECT_DIR / "build_sector_fund_flow_dashboard.py"
BIG_DEAL_SCRIPT = PROJECT_DIR / "pull_stock_big_deal_into_snapshot.py"
LOG_DIR = PROJECT_DIR / "akshare_data" / "logs"
LOCK_FILE = Path("/tmp/akshare_sector_fund_flow.lock")
TRADING_WINDOWS = [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]
STATUS_LOG = LOG_DIR / "sector_fund_flow_status.csv"
REQUIRED_TABLES = {
    "industry_fund_flow_now": 80,
    "concept_fund_flow_now": 250,
}


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (LOG_DIR / "sector_fund_flow_schedule.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def append_status(**fields: object) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    columns = [
        "timestamp",
        "event",
        "status",
        "attempt",
        "output_dir",
        "returncode",
        "industry_rows",
        "concept_rows",
        "big_deal_rows",
        "reason",
        "dashboard_built",
        "dashboard_returncode",
    ]
    row = {key: "" for key in columns}
    row.update({key: value for key, value in fields.items() if key in row})
    row["timestamp"] = row["timestamp"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    exists = STATUS_LOG.exists()
    with STATUS_LOG.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def is_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return any(start <= current <= end for start, end in TRADING_WINDOWS)


def is_trading_day_by_akshare(now: datetime) -> bool:
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        today = now.strftime("%Y-%m-%d")
        values = {str(value)[:10] for value in df["trade_date"].dropna().tolist()}
        return today in values
    except Exception as exc:
        log(f"calendar check failed, fallback to weekday: {type(exc).__name__}: {exc}")
        return now.weekday() < 5


def acquire_lock() -> int | None:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return fd
    except FileExistsError:
        try:
            pid_text = LOCK_FILE.read_text(encoding="utf-8").strip()
            pid = int(pid_text)
            os.kill(pid, 0)
            return None
        except (FileNotFoundError, ValueError, ProcessLookupError):
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            log("recovered stale lock")
            return fd
        except PermissionError:
            return None


def release_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def extract_output_dir(stdout: str) -> Path | None:
    match = re.search(r"output=(/Users/[^\n]+?_sector_fund_flow)", stdout)
    return Path(match.group(1)) if match else None


def csv_row_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            names = set()
            for row in csv.DictReader(handle):
                name = (row.get("行业") or "").strip()
                try:
                    net = float(row.get("净额", ""))
                except (TypeError, ValueError):
                    continue
                if name and net == net and abs(net) != float("inf"):
                    names.add(name)
            return len(names)
    except FileNotFoundError:
        return 0


def snapshot_counts(out_dir: Path | None) -> dict[str, int]:
    if out_dir is None:
        return {table: 0 for table in REQUIRED_TABLES}
    return {table: csv_row_count(out_dir / f"{table}.csv") for table in REQUIRED_TABLES}


def snapshot_complete(out_dir: Path | None) -> tuple[bool, str]:
    if out_dir is None:
        return False, "missing output dir"
    missing: list[str] = []
    counts = snapshot_counts(out_dir)
    for table, minimum_rows in REQUIRED_TABLES.items():
        rows = counts.get(table, 0)
        if rows < minimum_rows:
            missing.append(f"{table} rows={rows} min={minimum_rows}")
    if missing:
        return False, "; ".join(missing)
    return True, "ok"


def log_missing_sectors(out_dir: Path) -> None:
    path = out_dir / "pull_summary.csv"
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("label") in REQUIRED_TABLES and row.get("error"):
                reason = f"{row['label']}: {row['error']}"
                log(reason)
                append_status(event="sector_coverage", status="incomplete", output_dir=str(out_dir), reason=reason)


def run_pull_once(retries: int, timeout_seconds: int) -> tuple[subprocess.CompletedProcess[str] | None, Path | None, str]:
    cmd = [str(PYTHON), str(PULL_SCRIPT), "--retries", str(retries)]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if stdout:
            log("stdout before timeout:\n" + str(stdout).rstrip())
        if stderr:
            log("stderr before timeout:\n" + str(stderr).rstrip())
        return None, extract_output_dir(str(stdout)), f"timeout after {timeout_seconds}s"
    if completed.returncode != 0:
        if completed.stdout:
            log("stdout:\n" + completed.stdout.rstrip())
        if completed.stderr:
            log("stderr:\n" + completed.stderr.rstrip())
        log(f"exit={completed.returncode}")
    return completed, extract_output_dir(completed.stdout), f"exit={completed.returncode}"


def extract_rows(stdout: str) -> int:
    match = re.search(r"rows=(\d+)", stdout)
    return int(match.group(1)) if match else 0


def run_big_deal_once(snapshot_dir: Path, timeout_seconds: int) -> int:
    cmd = [str(PYTHON), str(BIG_DEAL_SCRIPT), "--snapshot-dir", str(snapshot_dir)]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        if stdout:
            log("big-deal stdout before timeout:\n" + stdout.rstrip())
        if stderr:
            log("big-deal stderr before timeout:\n" + stderr.rstrip())
        append_status(
            event="big_deal",
            status="timeout",
            output_dir=str(snapshot_dir),
            returncode=124,
            big_deal_rows=extract_rows(stdout),
            reason=f"timeout after {timeout_seconds}s",
        )
        return 124

    rows = extract_rows(completed.stdout or "")
    if completed.returncode != 0 or rows <= 0:
        if completed.stdout:
            log("big-deal stdout:\n" + completed.stdout.rstrip())
        if completed.stderr:
            log("big-deal stderr:\n" + completed.stderr.rstrip())
        append_status(
            event="big_deal",
            status="failed" if completed.returncode != 0 else "empty",
            output_dir=str(snapshot_dir),
            returncode=completed.returncode,
            big_deal_rows=rows,
            reason=(completed.stderr or completed.stdout or "no rows").strip()[-300:],
        )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run even outside trading time.")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--pull-attempts", type=int, default=2)
    parser.add_argument("--pull-timeout", type=int, default=240)
    parser.add_argument("--big-deal-timeout", type=int, default=75)
    parser.add_argument("--skip-big-deal", action="store_true", help="Skip optional stock big-deal pull.")
    parser.add_argument("--skip-calendar", action="store_true", help="Only check weekday/time, not exchange calendar.")
    args = parser.parse_args()

    now = datetime.now()
    if not args.force:
        if not is_trading_time(now):
            return 0
        if not args.skip_calendar and not is_trading_day_by_akshare(now):
            log("skip: not an exchange trading day")
            return 0

    fd = acquire_lock()
    if fd is None:
        log("skip: previous run still active")
        append_status(event="skip", status="previous_run_active", reason=str(LOCK_FILE))
        return 0

    try:
        last_returncode = 1
        complete_dir: Path | None = None
        for attempt in range(1, max(1, args.pull_attempts) + 1):
            completed, out_dir, status = run_pull_once(args.retries, args.pull_timeout)
            last_returncode = completed.returncode if completed is not None else 124
            complete, reason = snapshot_complete(out_dir)
            counts = snapshot_counts(out_dir)
            if completed is not None and completed.returncode == 0 and complete:
                complete_dir = out_dir
                break
            append_status(
                event="pull_attempt",
                status="incomplete",
                attempt=attempt,
                output_dir=str(out_dir or ""),
                returncode=last_returncode,
                industry_rows=counts["industry_fund_flow_now"],
                concept_rows=counts["concept_fund_flow_now"],
                reason=f"{status}; {reason}",
            )
            log(f"incomplete snapshot attempt {attempt}/{args.pull_attempts}: {status}; {reason}")
        if complete_dir is None:
            log("skip dashboard build: no complete industry/concept snapshot")
            append_status(
                event="dashboard",
                status="skipped",
                returncode=last_returncode or 1,
                reason="no complete industry/concept snapshot",
                dashboard_built=False,
            )
            return last_returncode or 1

        if not args.skip_big_deal:
            run_big_deal_once(complete_dir, args.big_deal_timeout)

        log_missing_sectors(complete_dir)
        dashboard_cmd = [str(PYTHON), str(DASHBOARD_SCRIPT)]
        dashboard = subprocess.run(dashboard_cmd, cwd=str(PROJECT_DIR), text=True, capture_output=True, check=False)
        if dashboard.returncode != 0:
            if dashboard.stdout:
                log("dashboard stdout:\n" + dashboard.stdout.rstrip())
            if dashboard.stderr:
                log("dashboard stderr:\n" + dashboard.stderr.rstrip())
            log(f"dashboard exit={dashboard.returncode}")
            append_status(
                event="dashboard",
                status="failed",
                output_dir=str(complete_dir),
                dashboard_built=False,
                dashboard_returncode=dashboard.returncode,
            )
        return dashboard.returncode
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
