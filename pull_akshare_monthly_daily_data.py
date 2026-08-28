#!/usr/bin/env python3
"""Pull recent daily A-share market data through AKShare.

The script intentionally avoids minute-level data. It stores raw-enough daily
tables in SQLite and mirrors each table to CSV for quick inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd


PROJECT_DIR = Path(os.environ.get("MONEY_FLOW_PROJECT_DIR", Path(__file__).resolve().parent))
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("MONEY_FLOW_DATA_ROOT", PROJECT_DIR / "akshare_data"))
INDEX_SYMBOLS = {
    "上证指数": ("000001", "sh000001"),
    "深证成指": ("399001", "sz399001"),
    "创业板指": ("399006", "sz399006"),
    "沪深300": ("000300", "sh000300"),
    "上证50": ("000016", "sh000016"),
    "中证500": ("000905", "sh000905"),
    "中证1000": ("000852", "sh000852"),
}


@dataclass
class FetchResult:
    label: str
    ok: bool
    rows: int = 0
    error: str = ""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_table_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)


def with_retries(label: str, fn: Callable[[], pd.DataFrame], retries: int, sleep_seconds: float) -> pd.DataFrame:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = fn()
            if df is None:
                raise RuntimeError("returned None")
            return df
        except Exception as exc:
            last_exc = exc
            print(f"[{now_text()}] {label} attempt {attempt}/{retries} failed: {type(exc).__name__}: {exc}")
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"{label} failed after {retries} attempts: {last_exc}")


def write_table(conn: sqlite3.Connection, out_dir: Path, table: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    table = clean_table_name(table)
    df.to_sql(table, conn, if_exists="replace", index=False)
    df.to_csv(out_dir / f"{table}.csv", index=False)
    return int(len(df))


def market_prefix(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("0", "2", "3")):
        return "sz"
    if symbol.startswith(("4", "8")):
        return "bj"
    return ""


def normalize_kline_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
    }
    df = df.rename(columns={key: val for key, val in rename_map.items() if key in df.columns}).copy()
    return df


def pull_stock_list(ak, conn: sqlite3.Connection, out_dir: Path, retries: int) -> pd.DataFrame:
    df = with_retries("stock_info_a_code_name", ak.stock_info_a_code_name, retries, 2.0)
    df = df.rename(columns={"code": "symbol", "name": "name"})
    write_table(conn, out_dir, "stock_list", df)
    return df


def pull_one_stock_kline(ak, symbol: str, name: str, start_date: str, end_date: str, retries: int) -> tuple[pd.DataFrame, str]:
    prefix = market_prefix(symbol)
    prefixed = f"{prefix}{symbol}" if prefix in {"sh", "sz"} else symbol
    candidates: list[tuple[str, Callable[[], pd.DataFrame]]] = []
    if prefix in {"sh", "sz"}:
        candidates.extend(
            [
                (
                    f"stock_zh_a_hist_tx {symbol}",
                    lambda prefixed=prefixed: ak.stock_zh_a_hist_tx(
                        symbol=prefixed,
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                        timeout=10,
                    ),
                ),
                (
                    f"stock_zh_a_daily {symbol}",
                    lambda prefixed=prefixed: ak.stock_zh_a_daily(
                        symbol=prefixed,
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                    ),
                ),
            ]
        )
    candidates.append(
        (
            f"stock_zh_a_hist {symbol}",
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
                timeout=10,
            ),
        )
    )

    errors: list[str] = []
    df = pd.DataFrame()
    source = ""
    for label, fn in candidates:
        try:
            df = with_retries(label, fn, retries, 1.0)
            source = label.split()[0]
            break
        except Exception as exc:
            errors.append(str(exc))
    if df.empty and errors:
        raise RuntimeError("; ".join(errors))
    if df.empty:
        return df, ""
    df = normalize_kline_columns(df)
    df.insert(0, "name", name)
    df.insert(0, "symbol", symbol)
    df.insert(2, "source", source)
    return df, ""


def pull_stock_klines(
    ak,
    conn: sqlite3.Connection,
    out_dir: Path,
    stock_list: pd.DataFrame,
    start_date: str,
    end_date: str,
    retries: int,
    workers: int,
    max_symbols: int,
) -> FetchResult:
    rows: list[pd.DataFrame] = []
    errors: list[dict] = []
    records = stock_list[["symbol", "name"]].dropna().astype(str).to_dict(orient="records")
    if max_symbols > 0:
        records = records[:max_symbols]

    print(f"[{now_text()}] pulling stock daily K lines: symbols={len(records)}, workers={workers}")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                pull_one_stock_kline,
                ak,
                item["symbol"],
                item["name"],
                start_date,
                end_date,
                retries,
            ): item
            for item in records
        }
        for idx, future in enumerate(as_completed(future_map), start=1):
            item = future_map[future]
            try:
                df, _ = future.result()
                if not df.empty:
                    rows.append(df)
            except Exception as exc:
                errors.append({"symbol": item["symbol"], "name": item["name"], "error": str(exc)})
            if idx % 250 == 0 or idx == len(records):
                print(f"[{now_text()}] stock K progress {idx}/{len(records)} ok_tables={len(rows)} errors={len(errors)}")

    if rows:
        all_df = pd.concat(rows, ignore_index=True)
        count = write_table(conn, out_dir, "stock_daily_kline_1m", all_df)
    else:
        count = 0

    if errors:
        write_table(conn, out_dir, "stock_daily_kline_errors", pd.DataFrame(errors))
    return FetchResult("stock_daily_kline_1m", ok=count > 0, rows=count, error=f"errors={len(errors)}")


def pull_index_klines(ak, conn: sqlite3.Connection, out_dir: Path, start_date: str, end_date: str, retries: int) -> FetchResult:
    rows: list[pd.DataFrame] = []
    errors: list[dict] = []
    for name, (plain_symbol, prefixed_symbol) in INDEX_SYMBOLS.items():
        try:
            candidates = [
                (
                    f"stock_zh_index_daily_tx {name}",
                    lambda prefixed_symbol=prefixed_symbol: ak.stock_zh_index_daily_tx(
                        symbol=prefixed_symbol,
                        start_date=start_date,
                        end_date=end_date,
                    ),
                ),
                (
                    f"stock_zh_index_daily {name}",
                    lambda prefixed_symbol=prefixed_symbol: ak.stock_zh_index_daily(symbol=prefixed_symbol),
                ),
                (
                    f"index_zh_a_hist {name}",
                    lambda plain_symbol=plain_symbol: ak.index_zh_a_hist(
                        symbol=plain_symbol,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                    ),
                ),
            ]
            df = pd.DataFrame()
            source = ""
            errors_for_index: list[str] = []
            for label, fn in candidates:
                try:
                    df = with_retries(label, fn, retries, 1.0)
                    source = label.split()[0]
                    break
                except Exception as exc:
                    errors_for_index.append(str(exc))
            if df.empty and errors_for_index:
                raise RuntimeError("; ".join(errors_for_index))
            if not df.empty:
                df = normalize_kline_columns(df)
                if "date" in df.columns:
                    df = df[df["date"].astype(str).str.replace("-", "") >= start_date].copy()
                    df = df[df["date"].astype(str).str.replace("-", "") <= end_date].copy()
                df.insert(0, "name", name)
                df.insert(0, "symbol", plain_symbol)
                df.insert(2, "source", source)
                rows.append(df)
        except Exception as exc:
            errors.append({"symbol": plain_symbol, "name": name, "error": str(exc)})
    count = write_table(conn, out_dir, "index_daily_kline_1m", pd.concat(rows, ignore_index=True)) if rows else 0
    if errors:
        write_table(conn, out_dir, "index_daily_kline_errors", pd.DataFrame(errors))
    return FetchResult("index_daily_kline_1m", ok=count > 0, rows=count, error=f"errors={len(errors)}")


def pull_optional_table(
    label: str,
    conn: sqlite3.Connection,
    out_dir: Path,
    fn: Callable[[], pd.DataFrame],
    retries: int,
) -> FetchResult:
    try:
        df = with_retries(label, fn, retries, 2.0)
        count = write_table(conn, out_dir, label, df)
        return FetchResult(label, ok=count > 0, rows=count)
    except Exception as exc:
        err_df = pd.DataFrame([{"label": label, "error": str(exc), "created_at": now_text()}])
        write_table(conn, out_dir, f"{label}_errors", err_df)
        return FetchResult(label, ok=False, error=str(exc))


def pull_fund_flow_rankings(ak, conn: sqlite3.Connection, out_dir: Path, retries: int) -> list[FetchResult]:
    results: list[FetchResult] = []
    for symbol, label in [
        ("即时", "stock_fund_flow_individual_now"),
        ("3日排行", "stock_fund_flow_individual_3d"),
        ("5日排行", "stock_fund_flow_individual_5d"),
    ]:
        results.append(
            pull_optional_table(
                label,
                conn,
                out_dir,
                lambda symbol=symbol: ak.stock_fund_flow_individual(symbol=symbol),
                retries,
            )
        )
    results.append(
        pull_optional_table(
            "hsgt_fund_flow_summary",
            conn,
            out_dir,
            ak.stock_hsgt_fund_flow_summary_em,
            retries,
        )
    )
    return results


def pull_industry_fund_flow_now(ak, conn: sqlite3.Connection, out_dir: Path, retries: int) -> FetchResult:
    try:
        flow_df = with_retries(
            "industry_fund_flow_now",
            lambda: ak.stock_fund_flow_industry(symbol="即时"),
            retries,
            2.0,
        )
        try:
            summary_df = with_retries(
                "stock_board_industry_summary_ths",
                ak.stock_board_industry_summary_ths,
                retries,
                1.0,
            )
            if not summary_df.empty:
                summary_df = summary_df.rename(columns={"板块": "行业"})
                keep_cols = [col for col in ["行业", "上涨家数", "下跌家数"] if col in summary_df.columns]
                flow_df = flow_df.merge(summary_df[keep_cols], on="行业", how="left")
        except Exception as exc:
            print(f"[{now_text()}] industry up/down summary skipped: {type(exc).__name__}: {exc}")
        count = write_table(conn, out_dir, "industry_fund_flow_now", flow_df)
        return FetchResult("industry_fund_flow_now", ok=count > 0, rows=count)
    except Exception as exc:
        err_df = pd.DataFrame([{"label": "industry_fund_flow_now", "error": str(exc), "created_at": now_text()}])
        write_table(conn, out_dir, "industry_fund_flow_now_errors", err_df)
        return FetchResult("industry_fund_flow_now", ok=False, error=str(exc))


def pull_sector_fund_flows(ak, conn: sqlite3.Connection, out_dir: Path, retries: int) -> list[FetchResult]:
    results: list[FetchResult] = []
    results.append(pull_industry_fund_flow_now(ak, conn, out_dir, retries))
    for symbol, window in [
        ("3日排行", "3d"),
        ("5日排行", "5d"),
    ]:
        results.append(
            pull_optional_table(
                f"industry_fund_flow_{window}",
                conn,
                out_dir,
                lambda symbol=symbol: ak.stock_fund_flow_industry(symbol=symbol),
                retries,
            )
        )
    for symbol, window in [
        ("即时", "now"),
        ("3日排行", "3d"),
        ("5日排行", "5d"),
    ]:
        results.append(
            pull_optional_table(
                f"concept_fund_flow_{window}",
                conn,
                out_dir,
                lambda symbol=symbol: ak.stock_fund_flow_concept(symbol=symbol),
                retries,
            )
        )
    return results


def pull_industry_flow_hist(ak, conn: sqlite3.Connection, out_dir: Path, industries: list[str], start_date: str, retries: int) -> FetchResult:
    rows: list[pd.DataFrame] = []
    errors: list[dict] = []
    for idx, industry in enumerate(industries, start=1):
        try:
            df = with_retries(
                f"stock_sector_fund_flow_hist {industry}",
                lambda industry=industry: ak.stock_sector_fund_flow_hist(symbol=industry),
                retries,
                1.0,
            )
            if not df.empty:
                date_col = "日期" if "日期" in df.columns else df.columns[0]
                filtered = df[df[date_col].astype(str).str.replace("-", "") >= start_date].copy()
                if not filtered.empty:
                    filtered.insert(0, "industry", industry)
                    rows.append(filtered)
        except Exception as exc:
            errors.append({"industry": industry, "error": str(exc)})
        if idx % 20 == 0 or idx == len(industries):
            print(f"[{now_text()}] industry flow hist progress {idx}/{len(industries)} ok_tables={len(rows)} errors={len(errors)}")
    count = write_table(conn, out_dir, "industry_fund_flow_hist_1m", pd.concat(rows, ignore_index=True)) if rows else 0
    if errors:
        write_table(conn, out_dir, "industry_fund_flow_hist_errors", pd.DataFrame(errors))
    return FetchResult("industry_fund_flow_hist_1m", ok=count > 0, rows=count, error=f"errors={len(errors)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45, help="Calendar days to request; 45 covers about one trading month.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Limit stock count for testing; 0 means all A-share symbols.")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--include-industry-flow-hist", action="store_true", help="Slow and currently flaky Eastmoney route; disabled by default.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args()

    import akshare as ak

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    start_date = start_dt.strftime("%Y%m%d")
    end_date = end_dt.strftime("%Y%m%d")
    run_id = end_dt.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_root) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "akshare_daily.sqlite"

    print(f"[{now_text()}] output={out_dir}")
    print(f"[{now_text()}] date_range={start_date}..{end_date}")

    conn = sqlite3.connect(db_path)
    results: list[FetchResult] = []
    try:
        meta = pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "created_at": now_text(),
                    "start_date": start_date,
                    "end_date": end_date,
                    "days": args.days,
                    "max_symbols": args.max_symbols,
                    "workers": args.workers,
                    "retries": args.retries,
                    "include_industry_flow_hist": args.include_industry_flow_hist,
                    "akshare_version": getattr(ak, "__version__", "unknown"),
                }
            ]
        )
        write_table(conn, out_dir, "run_meta", meta)

        stock_list = pull_stock_list(ak, conn, out_dir, args.retries)
        results.append(FetchResult("stock_list", True, len(stock_list)))

        results.append(pull_index_klines(ak, conn, out_dir, start_date, end_date, args.retries))
        results.extend(pull_sector_fund_flows(ak, conn, out_dir, args.retries))
        results.extend(pull_fund_flow_rankings(ak, conn, out_dir, args.retries))
        industries: list[str] = []
        try:
            now_df = pd.read_sql_query("select 行业 from industry_fund_flow_now", conn)
            industries = now_df["行业"].dropna().astype(str).tolist()
        except Exception:
            industries = []
        if industries and args.include_industry_flow_hist:
            results.append(pull_industry_flow_hist(ak, conn, out_dir, industries, start_date, args.retries))

        results.append(
            pull_optional_table(
                "market_fund_flow_hist",
                conn,
                out_dir,
                ak.stock_market_fund_flow,
                args.retries,
            )
        )
        results.append(
            pull_stock_klines(
                ak,
                conn,
                out_dir,
                stock_list,
                start_date,
                end_date,
                args.retries,
                args.workers,
                args.max_symbols,
            )
        )

        summary = pd.DataFrame([result.__dict__ for result in results])
        write_table(conn, out_dir, "pull_summary", summary)
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "output_dir": str(out_dir),
                    "sqlite": str(db_path),
                    "date_range": [start_date, end_date],
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
