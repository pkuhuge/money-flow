#!/usr/bin/env python3
"""Build a self-refreshing sector fund-flow dashboard from snapshot runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd
import requests


PROJECT_DIR = Path(os.environ.get("MONEY_FLOW_PROJECT_DIR", Path(__file__).resolve().parent))
DATA_ROOT = Path(os.environ.get("MONEY_FLOW_DATA_ROOT", PROJECT_DIR / "akshare_data"))
OUTPUT_HTML = DATA_ROOT / "sector_fund_flow_dashboard.html"


@dataclass
class Snapshot:
    run_dir: Path
    stamp: datetime
    date: str
    time: str


def parse_stamp(run_dir: Path) -> datetime | None:
    match = re.match(r"(\d{8})_(\d{6})_sector_fund_flow$", run_dir.name)
    if not match:
        return None
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")


def find_snapshots(data_root: Path) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    for run_dir in sorted(data_root.glob("*_sector_fund_flow")):
        stamp = parse_stamp(run_dir)
        if stamp is None:
            continue
        if not (run_dir / "industry_fund_flow_now.csv").exists():
            continue
        if not (run_dir / "concept_fund_flow_now.csv").exists():
            continue
        snapshots.append(
            Snapshot(
                run_dir=run_dir,
                stamp=stamp,
                date=stamp.strftime("%Y-%m-%d"),
                time=stamp.strftime("%H:%M"),
            )
        )
    return snapshots


def read_snapshot_table(snapshot: Snapshot, filename: str, kind: str) -> list[dict]:
    path = snapshot.run_dir / filename
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if "行业" in df.columns:
        df["行业"] = df["行业"].astype(str).str.strip()
        df = df[df["行业"] != ""].drop_duplicates(subset=["行业"], keep="last")
    rows: list[dict] = []

    def int_or_none(value: object) -> int | None:
        if pd.isna(value):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    for _, row in df.iterrows():
        name = str(row.get("行业", "")).strip()
        if not name:
            continue
        rows.append(
            {
                "kind": kind,
                "date": snapshot.date,
                "time": snapshot.time,
                "stamp": snapshot.stamp.isoformat(timespec="seconds"),
                "name": name,
                "net": float(row.get("净额", 0) or 0),
                "change": float(row.get("行业-涨跌幅", 0) or 0),
                "leader": str(row.get("领涨股", "") or ""),
                "leaderChange": float(row.get("领涨股-涨跌幅", 0) or 0),
                "companies": int(row.get("公司家数", 0) or 0),
                "upCount": int_or_none(row.get("上涨家数")),
                "downCount": int_or_none(row.get("下跌家数")),
            }
        )
    return rows


def number_from_text(value: object) -> float:
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return 0.0
    text = text.rstrip("%")
    try:
        return float(text)
    except ValueError:
        return 0.0


def money_to_yi(value: object, default_unit: str = "yuan") -> float:
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        multiplier = 0.0001 if default_unit == "wan" else 0.00000001
        return float(value) * multiplier
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return 0.0
    multiplier = 1.0
    if text.endswith("亿"):
        text = text[:-1]
    elif text.endswith("万"):
        text = text[:-1]
        multiplier = 0.0001
    else:
        multiplier = 0.0001 if default_unit == "wan" else 0.00000001
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def read_stock_big_deal_table(snapshot: Snapshot) -> list[dict]:
    path = snapshot.run_dir / "stock_fund_flow_big_deal.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    by_stock: dict[tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        name = str(row.get("股票简称", "")).strip()
        if not name:
            continue
        code = str(row.get("股票代码", "")).strip()
        current = by_stock.setdefault(
            (code, name),
            {
                "date": snapshot.date,
                "time": snapshot.time,
                "stamp": snapshot.stamp.isoformat(timespec="seconds"),
                "code": code,
                "name": name,
                "price": 0.0,
                "change": number_from_text(row.get("涨跌幅")),
                "inflow": 0.0,
                "outflow": 0.0,
                "net": 0.0,
                "amount": 0.0,
                "dealCount": 0,
                "source": "同花顺大单追踪",
            },
        )
        current["price"] = number_from_text(row.get("成交价格")) or current["price"]
        current["change"] = number_from_text(row.get("涨跌幅"))
        amount = money_to_yi(row.get("成交额"), default_unit="wan")
        nature = str(row.get("大单性质", ""))
        if "买" in nature:
            current["inflow"] += amount
        elif "卖" in nature:
            current["outflow"] += amount
        current["amount"] += amount
        current["dealCount"] += 1
    rows = []
    for current in by_stock.values():
        current["net"] = current["inflow"] - current["outflow"]
        rows.append(current)
    return rows


def build_payload(snapshots: list[Snapshot]) -> dict:
    rows: list[dict] = []
    for snapshot in snapshots:
        rows.extend(read_snapshot_table(snapshot, "industry_fund_flow_now.csv", "industry"))
        rows.extend(read_snapshot_table(snapshot, "concept_fund_flow_now.csv", "concept"))

    latest_stock_snapshot_by_date: dict[str, Snapshot] = {}
    for snapshot in snapshots:
        if (snapshot.run_dir / "stock_fund_flow_big_deal.csv").exists():
            latest_stock_snapshot_by_date[snapshot.date] = snapshot
    stocks: list[dict] = []
    for snapshot in latest_stock_snapshot_by_date.values():
        stocks.extend(read_stock_big_deal_table(snapshot))

    dates = sorted({snapshot.date for snapshot in snapshots})
    latest_run = str(snapshots[-1].run_dir) if snapshots else ""
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "availableDates": dates,
        "latestDate": dates[-1] if dates else "",
        "rows": rows,
        "stocks": stocks,
        "latestRun": latest_run,
        "nasdaqFutures": fetch_nasdaq_futures_7d(),
    }


def fetch_nasdaq_futures_7d() -> dict:
    end_ts = int(time.time())
    start_ts = int((datetime.now() - timedelta(days=10)).timestamp())
    url = "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F"
    params = {
        "period1": start_ts,
        "period2": end_ts,
        "interval": "1h",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        rows = []
        for idx, stamp in enumerate(timestamps):
            close = quote.get("close", [None] * len(timestamps))[idx]
            if close is None:
                continue
            point_time = datetime.fromtimestamp(stamp)
            rows.append(
                {
                    "date": point_time.strftime("%Y-%m-%d"),
                    "time": point_time.strftime("%H:%M"),
                    "label": point_time.strftime("%m-%d %H:%M"),
                    "open": quote.get("open", [None] * len(timestamps))[idx],
                    "high": quote.get("high", [None] * len(timestamps))[idx],
                    "low": quote.get("low", [None] * len(timestamps))[idx],
                    "close": close,
                    "volume": quote.get("volume", [None] * len(timestamps))[idx],
                }
            )
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        rows = [row for row in rows if f"{row['date']} {row['time']}" >= cutoff]
        if rows:
            first = rows[0]["close"]
            prev = None
            for row in rows:
                row["changeFromFirstPct"] = (row["close"] / first - 1) * 100 if first else 0
                row["pointChangePct"] = (row["close"] / prev - 1) * 100 if prev else None
                prev = row["close"]
        daily_rows = []
        for date in sorted({row["date"] for row in rows})[-7:]:
            day_points = [row for row in rows if row["date"] == date]
            if not day_points:
                continue
            day_open = day_points[0]["open"] or day_points[0]["close"]
            day_close = day_points[-1]["close"]
            day_high = max((row["high"] or row["close"]) for row in day_points)
            day_low = min((row["low"] or row["close"]) for row in day_points)
            daily_rows.append(
                {
                    "date": date,
                    "open": day_open,
                    "high": day_high,
                    "low": day_low,
                    "close": day_close,
                    "changePct": (day_close / day_open - 1) * 100 if day_open else 0,
                    "points": len(day_points),
                }
            )
        return {
            "ok": bool(rows),
            "symbol": "NQ=F",
            "name": "纳斯达克100连续期指",
            "source": "Yahoo Finance",
            "interval": "1h",
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "rows": rows,
            "dailyRows": daily_rows,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "symbol": "NQ=F",
            "name": "纳斯达克100连续期指",
            "source": "Yahoo Finance",
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "rows": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def render_html(payload: dict) -> str:
    data_json = json.dumps(payload, ensure_ascii=False)
    title_date = payload.get("latestDate") or datetime.now().strftime("%Y-%m-%d")
    latest_run = escape(payload.get("latestRun", ""))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title_date)} 盘中资金流向</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #dfe3ea;
      --text: #20242a;
      --muted: #667085;
      --red: #b72d39;
      --red-soft: #fff1f2;
      --green: #0f766e;
      --green-soft: #ecfdf5;
      --blue: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .page {{ max-width: 1280px; margin: 0 auto; padding: 22px 18px 34px; }}
    header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
    h1 {{ margin: 0; font-size: 22px; line-height: 1.2; font-weight: 760; letter-spacing: 0; }}
    .sub {{ margin-top: 8px; color: var(--muted); max-width: 760px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .toolbar {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .field {{ display: grid; gap: 3px; color: var(--muted); font-size: 11px; }}
    .range-field {{ min-width: 315px; }}
    .date-range {{ display: flex; align-items: center; gap: 6px; }}
    .date-sep {{ color: var(--muted); font-size: 13px; }}
    .field input, .field select, .search {{
      height: 38px; padding: 0 10px; border: 1px solid var(--line); border-radius: 6px;
      background: var(--panel); color: var(--text); outline: none;
    }}
    .field input {{ width: 138px; }}
    .field select {{ width: 145px; }}
    .search {{ width: 190px; }}
    .clock {{ height: 38px; min-width: 74px; padding: 9px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); font-weight: 700; color: #1552b7; text-align: center; align-self: end; }}
    .segments {{ display: inline-grid; grid-template-columns: repeat(2, 1fr); gap: 2px; padding: 3px; border: 1px solid var(--line); border-radius: 7px; background: #eef1f5; align-self: end; }}
    button {{ height: 30px; border: 0; border-radius: 5px; padding: 0 12px; background: transparent; color: var(--muted); cursor: pointer; font-weight: 650; }}
    button.active {{ background: var(--panel); color: var(--text); box-shadow: 0 1px 2px rgba(16, 24, 40, .08); }}
    .chart-wrap {{ position: relative; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    svg {{ display: block; width: 100%; height: min(66vh, 660px); min-height: 500px; }}
    .grid {{ stroke: var(--line); stroke-width: 1; }}
    .axis-label {{ fill: var(--muted); font-size: 12px; }}
    .line {{ fill: none; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }}
    .series-line, .series-guide, .series-label {{ transition: opacity .12s ease, stroke-width .12s ease; }}
    .series-line.is-dim, .series-guide.is-dim, .series-label.is-dim {{ opacity: .14; }}
    .series-line.is-focused {{ stroke-width: 3.6; }}
    .series-hitline {{ fill: none; stroke: transparent; stroke-width: 14; pointer-events: stroke; cursor: crosshair; }}
    .series-label text {{ font-size: 12px; font-weight: 700; }}
    .series-label rect {{ stroke-width: 1; rx: 5; }}
    .hover-point {{ cursor: crosshair; opacity: 0; }}
    .hover-point:hover {{ opacity: .18; }}
    .tooltip {{
      position: absolute;
      z-index: 3;
      display: none;
      min-width: 150px;
      max-width: 240px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, .96);
      box-shadow: 0 8px 20px rgba(16, 24, 40, .14);
      pointer-events: none;
      color: var(--text);
      font-size: 12px;
    }}
    .tooltip-title {{ font-weight: 760; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .tooltip-row {{ display: flex; justify-content: space-between; gap: 16px; color: var(--muted); }}
    .tooltip-row strong {{ color: var(--text); font-weight: 700; }}
    .panel {{ margin-top: 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--line); }}
    .panel h2 {{ margin: 0; font-size: 15px; letter-spacing: 0; }}
    .panel-note {{ color: var(--muted); font-size: 12px; }}
    .table-scroll {{ max-height: 760px; overflow: auto; }}
    .mini-chart-wrap {{ position: relative; padding: 12px 14px 16px; }}
    .mini-chart {{ width: 100%; height: 320px; min-height: 320px; border-bottom: 1px solid #edf0f4; }}
    .total-chart {{ height: 280px; min-height: 280px; }}
    .mini-summary {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 10px; color: var(--muted); flex-wrap: wrap; }}
    .mini-price {{ font-size: 20px; font-weight: 760; color: var(--text); }}
    .mini-bars {{ display: grid; grid-template-columns: repeat(7, minmax(82px, 1fr)); gap: 8px; margin-top: 10px; }}
    .mini-bar {{ border: 1px solid #edf0f4; border-radius: 6px; padding: 7px 8px; background: #fafbfc; }}
    .mini-bar-date {{ color: var(--muted); font-size: 11px; }}
    .mini-bar-value {{ margin-top: 3px; font-weight: 700; }}
    .analysis-body {{ padding: 14px; }}
    .analysis-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .stock-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }}
    .analysis-block {{ border: 1px solid #edf0f4; border-radius: 8px; overflow: hidden; background: #fafbfc; }}
    .stock-block {{ margin-top: 12px; }}
    .analysis-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-bottom: 1px solid #edf0f4; font-weight: 760; }}
    .analysis-list {{ display: grid; gap: 0; }}
    .analysis-item {{ display: grid; grid-template-columns: minmax(118px, 1fr) 2fr; gap: 12px; padding: 10px 12px; border-bottom: 1px solid #edf0f4; }}
    .analysis-item:last-child {{ border-bottom: 0; }}
    .analysis-sector {{ min-width: 0; font-weight: 760; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .analysis-meta {{ display: flex; align-items: center; justify-content: flex-end; gap: 12px; color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .analysis-reason {{ color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .stock-card {{ display: grid; grid-template-columns: minmax(92px, 1fr) auto; gap: 8px; align-items: center; border: 1px solid #edf0f4; border-radius: 7px; padding: 8px 10px; background: #ffffff; }}
    .stock-name {{ min-width: 0; font-weight: 760; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .stock-code {{ margin-left: 4px; color: var(--muted); font-size: 11px; font-weight: 650; }}
    .stock-metrics {{ display: flex; align-items: center; justify-content: flex-end; gap: 10px; color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .analysis-nasdaq {{ margin-top: 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid #edf0f4; border-radius: 8px; background: #fafbfc; color: var(--muted); }}
    .tag {{ display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 999px; background: #eef1f5; color: var(--muted); font-size: 12px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #edf0f4; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    th:first-child, td:first-child {{ text-align: left; width: 64px; }}
    th:nth-child(2), td:nth-child(2) {{ text-align: left; width: 180px; }}
    th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) {{ width: 112px; }}
    th {{ position: sticky; top: 0; background: var(--panel); color: var(--muted); font-weight: 650; font-size: 12px; z-index: 1; }}
    .pos {{ color: var(--red); }}
    .neg {{ color: var(--green); }}
    .empty {{ padding: 80px 20px; color: var(--muted); text-align: center; }}
    @media (max-width: 820px) {{
      header {{ display: block; }}
      .toolbar {{ margin-top: 14px; justify-content: flex-start; }}
      .range-field {{ width: 100%; }}
      .date-range {{ width: 100%; }}
      .date-range input {{ flex: 1; min-width: 0; }}
      .search {{ width: 100%; }}
      .sub {{ white-space: normal; }}
      svg {{ height: 560px; }}
      .mini-chart {{ height: 320px; }}
      .mini-bars {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .analysis-grid {{ grid-template-columns: 1fr; }}
      .stock-grid {{ grid-template-columns: 1fr; }}
      .analysis-item {{ grid-template-columns: 1fr; gap: 4px; }}
      .analysis-meta {{ justify-content: flex-start; flex-wrap: wrap; white-space: normal; }}
      .analysis-nasdaq {{ display: block; }}
      th:nth-child(2), td:nth-child(2) {{ width: 150px; }}
      th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) {{ width: 102px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header>
      <div>
        <h1><span id="titleDate"></span> 资金流向</h1>
        <div class="sub">最后更新：<span id="generatedAt"></span> · 数据模式：<span id="rangeMode"></span> · 最新数据：<span id="latestRun">{latest_run}</span></div>
      </div>
      <div class="toolbar">
        <div class="segments" aria-label="数据类型">
          <button id="industryBtn" class="active" type="button">行业</button>
          <button id="conceptBtn" type="button">概念</button>
        </div>
        <label class="field">有数据日期<select id="datePreset"></select></label>
        <label class="field range-field">日期范围
          <span class="date-range">
            <input id="startDate" type="date">
            <span class="date-sep">至</span>
            <input id="endDate" type="date">
          </span>
        </label>
        <input id="search" class="search" placeholder="搜索板块">
        <div class="clock" id="clock"></div>
      </div>
    </header>
    <section class="chart-wrap">
      <svg id="chart" role="img" aria-label="资金流向折线图"></svg>
      <div id="tooltip" class="tooltip"></div>
      <div id="empty" class="empty" hidden>所选日期没有可展示的数据</div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>完整列表</h2>
        <div class="panel-note" id="tableNote"></div>
      </div>
      <div class="table-scroll">
        <table id="fullTable"></table>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>总资金净流入/净流出</h2>
        <div class="panel-note" id="totalFlowNote"></div>
      </div>
      <div class="mini-chart-wrap">
        <svg id="totalFlowChart" class="mini-chart total-chart" role="img" aria-label="总资金净流入净流出曲线"></svg>
        <div id="totalFlowTooltip" class="tooltip"></div>
        <div class="mini-summary">
          <div><span class="mini-price" id="totalFlowLatest"></span> <span id="totalFlowChange"></span></div>
          <div id="totalFlowSource"></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>纳斯达克连续期指</h2>
        <div class="panel-note" id="nasdaqNote"></div>
      </div>
      <div class="mini-chart-wrap">
        <svg id="nasdaqChart" class="mini-chart" role="img" aria-label="纳斯达克连续期指最近7天走势"></svg>
        <div class="mini-summary">
          <div><span class="mini-price" id="nasdaqLatest"></span> <span id="nasdaqChange"></span></div>
          <div id="nasdaqSource"></div>
        </div>
        <div class="mini-bars" id="nasdaqBars"></div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>盘面研判</h2>
        <div class="panel-note" id="analysisNote"></div>
      </div>
      <div class="analysis-body">
        <div class="analysis-grid">
          <div class="analysis-block">
            <div class="analysis-title"><span>强势板块</span><span class="tag">资金占优</span></div>
            <div class="analysis-list" id="analysisStrong"></div>
          </div>
          <div class="analysis-block">
            <div class="analysis-title"><span>大跌风险与转弱</span><span class="tag">注意回撤</span></div>
            <div class="analysis-list" id="analysisRisk"></div>
          </div>
        </div>
        <div class="analysis-block stock-block">
          <div class="analysis-title"><span>个股大单雷达</span><span class="tag">大单净买 + 涨跌</span></div>
          <div class="stock-grid" id="analysisStocks"></div>
        </div>
        <div class="analysis-nasdaq" id="analysisNasdaq"></div>
      </div>
    </section>
  </main>
  <script id="payload" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById('payload').textContent);
    let mode = 'industry';
    const palette = ['#b72d39', '#c2410c', '#be123c', '#9f1239', '#dc2626', '#ea580c', '#0f766e', '#0f766e', '#0d9488', '#115e59', '#134e4a', '#166534'];

    function fmt(value) {{
      return `${{Number(value || 0).toFixed(2)}}`;
    }}

    function fmtMoney(value) {{
      const n = Number(value || 0);
      if (Math.abs(n) < 0.1) return `${{(n * 10000).toFixed(2)}}万`;
      return `${{n.toFixed(2)}}亿`;
    }}

    function escAttr(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    }}

    function escHtml(value) {{
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    }}

    function dateRange() {{
      const dates = payload.availableDates || [];
      const latest = payload.latestDate || dates[dates.length - 1] || '';
      let start = document.getElementById('startDate').value || latest;
      let end = document.getElementById('endDate').value || start;
      if (start > end) [start, end] = [end, start];
      return {{ start, end, isSingle: start === end }};
    }}

    function rowsInRange(kind) {{
      const {{ start, end }} = dateRange();
      return payload.rows.filter(r => r.kind === kind && r.date >= start && r.date <= end);
    }}

    function latestRowsForDate(kind, date) {{
      const rows = payload.rows.filter(r => r.kind === kind && r.date === date);
      if (!rows.length) return [];
      const byName = new Map();
      rows.forEach(row => {{
        const current = byName.get(row.name);
        if (!current || row.stamp > current.stamp) byName.set(row.name, row);
      }});
      return [...byName.values()];
    }}

    function latestStocksForDate(date) {{
      const rows = (payload.stocks || []).filter(r => r.date === date);
      if (!rows.length) return [];
      const latestStamp = rows.map(r => r.stamp).sort().at(-1);
      return rows.filter(r => r.stamp === latestStamp);
    }}

    function lastSnapshotRows(kind) {{
      const {{ start, end, isSingle }} = dateRange();
      if (isSingle) return latestRowsForDate(kind, start);
      const byName = new Map();
      const dates = (payload.availableDates || []).filter(d => d >= start && d <= end);
      dates.forEach(date => {{
        latestRowsForDate(kind, date).forEach(row => {{
          const current = byName.get(row.name) || {{ ...row, net: 0, days: 0 }};
          current.net += row.net;
          current.days += 1;
          current.change = row.change;
          current.leader = row.leader;
          current.leaderChange = row.leaderChange;
          current.companies = row.companies;
          current.upCount = row.upCount;
          current.downCount = row.downCount;
          byName.set(row.name, current);
        }});
      }});
      return [...byName.values()];
    }}

    function halfHourNet(row) {{
      if (!row?.stamp) return row?.net ?? 0;
      const current = new Date(row.stamp);
      if (Number.isNaN(current.getTime())) return row.net;
      const marketOpen = new Date(`${{row.date}}T09:30:00`);
      const target = new Date(current.getTime() - 30 * 60 * 1000);
      if (target <= marketOpen) return row.net;
      const candidates = payload.rows
        .filter(r => r.kind === row.kind && r.date === row.date && r.name === row.name && new Date(r.stamp) <= target)
        .sort((a, b) => new Date(a.stamp) - new Date(b.stamp));
      const baseline = candidates.at(-1);
      return baseline ? row.net - baseline.net : row.net;
    }}

    function filteredRows(rows) {{
      const q = document.getElementById('search').value.trim().toLowerCase();
      return q ? rows.filter(r => r.name.toLowerCase().includes(q)) : rows;
    }}

    function chartPoints(kind, name) {{
      const {{ start, end, isSingle }} = dateRange();
      if (isSingle) {{
        const rows = payload.rows.filter(r => r.kind === kind && r.date === start && r.name === name);
        const byTime = new Map(rows.map(r => [r.time, r]));
        const ticks = [...new Set(payload.rows.filter(r => r.kind === kind && r.date === start).map(r => r.time))].sort();
        return {{ ticks, points: ticks.map(t => byTime.get(t) || {{ time: t, label: t, net: null }}) }};
      }}
      const dates = (payload.availableDates || []).filter(d => d >= start && d <= end);
      let cumulative = 0;
      const points = dates.map(date => {{
        const rows = latestRowsForDate(kind, date).filter(r => r.name === name);
        if (rows[0]) {{
          cumulative += rows[0].net;
          return {{ ...rows[0], label: date.slice(5), dailyNet: rows[0].net, net: cumulative }};
        }}
        return {{ date, label: date.slice(5), dailyNet: null, net: cumulative }};
      }});
      return {{ ticks: dates.map(d => d.slice(5)), points }};
    }}

    function totalFlowPoints(kind) {{
      const {{ start, end, isSingle }} = dateRange();
      if (isSingle) {{
        const dayRows = payload.rows.filter(r => r.kind === kind && r.date === start);
        const ticks = [...new Set(dayRows.map(r => r.time))].sort();
        const carried = new Map();
        const points = ticks.map(t => {{
          dayRows.filter(r => r.time === t).forEach(row => carried.set(row.name, row.net));
          const net = [...carried.values()].reduce((sum, value) => sum + value, 0);
          return {{ label: t, net, count: carried.size }};
        }});
        return {{ ticks, points, modeText: '盘中快照合计' }};
      }}
      const dates = (payload.availableDates || []).filter(d => d >= start && d <= end);
      let cumulative = 0;
      const points = dates.map(date => {{
        const rows = latestRowsForDate(kind, date);
        const dailyNet = rows.reduce((sum, row) => sum + row.net, 0);
        cumulative += dailyNet;
        return {{ label: date.slice(5), date, net: cumulative, dailyNet, count: rows.length }};
      }});
      return {{ ticks: dates.map(d => d.slice(5)), points, modeText: '按天累计合计' }};
    }}

    function selectedNames(kind) {{
      const {{ isSingle }} = dateRange();
      if (isSingle) {{
        const rows = filteredRows(lastSnapshotRows(kind));
        const positive = rows.filter(r => r.net >= 0).sort((a, b) => b.net - a.net).slice(0, 8);
        const negative = rows.filter(r => r.net < 0).sort((a, b) => a.net - b.net).slice(0, 8);
        return [...positive, ...negative].map(r => r.name);
      }}
      const rows = filteredRows(lastSnapshotRows(kind)).sort((a, b) => Math.abs(b.net) - Math.abs(a.net));
      const positive = rows.filter(r => r.net >= 0).sort((a, b) => b.net - a.net).slice(0, 6);
      const negative = rows.filter(r => r.net < 0).sort((a, b) => a.net - b.net).slice(0, 6);
      return [...positive, ...negative].map(r => r.name);
    }}

    function labelLayout(items, minY, maxY, gap) {{
      const sorted = items.slice().sort((a, b) => a.rawY - b.rawY);
      sorted.forEach((item, idx) => {{
        item.labelY = Math.max(minY, idx === 0 ? item.rawY : sorted[idx - 1].labelY + gap);
      }});
      for (let i = sorted.length - 1; i >= 0; i--) {{
        const limit = i === sorted.length - 1 ? maxY : sorted[i + 1].labelY - gap;
        sorted[i].labelY = Math.min(sorted[i].labelY, limit);
      }}
      sorted.forEach((item, idx) => {{
        item.labelY = Math.max(minY, idx === 0 ? item.labelY : sorted[idx - 1].labelY + gap);
      }});
      return sorted;
    }}

    function drawChart() {{
      const {{ start, end, isSingle }} = dateRange();
      document.getElementById('titleDate').textContent = isSingle ? `${{start}} 盘中` : `${{start}} 至 ${{end}}`;
      document.getElementById('generatedAt').textContent = (payload.generatedAt || '').replace('T', ' ');
      document.getElementById('rangeMode').textContent = isSingle ? '单日分时' : '按天累计折线，列表净额求和';
      const svg = document.getElementById('chart');
      const tooltip = document.getElementById('tooltip');
      const empty = document.getElementById('empty');
      const names = selectedNames(mode);
      svg.innerHTML = '';
      tooltip.style.display = 'none';
      if (!names.length) {{
        svg.style.display = 'none';
        empty.hidden = false;
        return;
      }}
      svg.style.display = 'block';
      empty.hidden = true;

      const width = 1180, height = 640;
      const margin = {{ left: 62, right: 240, top: 26, bottom: 46 }};
      svg.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);

      const packed = names.map(name => ({{ name, ...chartPoints(mode, name) }}));
      const ticks = packed[0]?.ticks || [];
      const values = [];
      packed.forEach(s => s.points.forEach(p => {{ if (p.net !== null) values.push(p.net); }}));
      let min = Math.min(0, ...values), max = Math.max(0, ...values);
      const pad = Math.max(5, (max - min) * 0.12);
      min -= pad; max += pad;
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      const x = i => margin.left + (ticks.length <= 1 ? 0 : i * plotW / (ticks.length - 1));
      const y = v => margin.top + (max - v) * plotH / (max - min || 1);

      for (let i = 0; i <= 5; i++) {{
        const gy = margin.top + i * plotH / 5;
        const val = max - i * (max - min) / 5;
        svg.insertAdjacentHTML('beforeend', `<line class="grid" x1="${{margin.left}}" y1="${{gy}}" x2="${{width - margin.right}}" y2="${{gy}}"></line>`);
        svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{margin.left - 10}}" y="${{gy + 4}}" text-anchor="end">${{fmt(val)}}亿</text>`);
      }}
      ticks.forEach((t, i) => {{
        if (i % Math.max(1, Math.floor(ticks.length / 6)) === 0 || i === ticks.length - 1) {{
          svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{x(i)}}" y="${{height - 18}}" text-anchor="middle">${{t}}</text>`);
        }}
      }});

      const labelItems = [];
      packed.forEach((series, idx) => {{
        const last = series.points.slice().reverse().find(p => p.net !== null);
        const color = last?.net >= 0 ? palette[idx % 6] : palette[6 + (idx % 6)];
        const coords = series.points.map((p, i) => p.net === null ? null : [x(i), y(p.net)]).filter(Boolean);
        if (!coords.length) return;
        const path = coords.map((p, i) => `${{i ? 'L' : 'M'}} ${{p[0].toFixed(1)}} ${{p[1].toFixed(1)}}`).join(' ');
        svg.insertAdjacentHTML('beforeend', `<path class="line series-line" d="${{path}}" stroke="${{color}}" data-name="${{escAttr(series.name)}}"></path>`);
        svg.insertAdjacentHTML('beforeend', `<path class="series-hitline" d="${{path}}" data-name="${{escAttr(series.name)}}"></path>`);
        series.points.forEach((point, pointIndex) => {{
          if (point.net === null) return;
          const cx = x(pointIndex);
          const cy = y(point.net);
          const pointLabel = point.label || point.time || ticks[pointIndex] || '';
          const daily = point.dailyNet === null || point.dailyNet === undefined ? '' : point.dailyNet;
          svg.insertAdjacentHTML('beforeend', `<circle class="hover-point" cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="8" fill="${{color}}" data-name="${{escAttr(series.name)}}" data-label="${{escAttr(pointLabel)}}" data-net="${{escAttr(point.net)}}" data-daily="${{escAttr(daily)}}" data-single="${{isSingle ? '1' : '0'}}"></circle>`);
        }});
        if (last) labelItems.push({{ series, last, color, rawY: y(last.net) }});
      }});

      labelLayout(labelItems, margin.top + 14, height - margin.bottom - 14, 28).forEach(item => {{
        const {{ series, last, color }} = item;
        const lx = width - margin.right + 12;
        const ly = item.labelY;
        const text = `${{series.name}}  ${{fmt(last.net)}}`;
        const bg = last.net >= 0 ? '#fff1f2' : '#ecfdf5';
        svg.insertAdjacentHTML('beforeend', `<line class="series-guide" x1="${{width - margin.right}}" y1="${{item.rawY.toFixed(1)}}" x2="${{lx - 2}}" y2="${{ly.toFixed(1)}}" stroke="${{color}}" stroke-width="1" opacity=".45" data-name="${{escAttr(series.name)}}"></line>`);
        svg.insertAdjacentHTML('beforeend', `<g class="series-label" data-name="${{escAttr(series.name)}}"><rect x="${{lx}}" y="${{ly - 13}}" width="${{Math.min(215, 46 + text.length * 10)}}" height="24" fill="${{bg}}" stroke="${{color}}"></rect><text x="${{lx + 8}}" y="${{ly + 4}}" fill="${{color}}">${{text}}</text></g>`);
      }});

      function focusChartSeries(name) {{
        svg.querySelectorAll('.series-line, .series-guide, .series-label').forEach(el => {{
          const same = el.dataset.name === name;
          el.classList.toggle('is-dim', !same);
          el.classList.toggle('is-focused', same && el.classList.contains('series-line'));
        }});
      }}

      function clearChartSeriesFocus() {{
        svg.querySelectorAll('.series-line, .series-guide, .series-label').forEach(el => {{
          el.classList.remove('is-dim', 'is-focused');
        }});
      }}

      svg.querySelectorAll('.series-hitline, .series-label').forEach(el => {{
        el.addEventListener('mouseenter', event => focusChartSeries(event.currentTarget.dataset.name));
        el.addEventListener('mousemove', event => focusChartSeries(event.currentTarget.dataset.name));
        el.addEventListener('mouseleave', clearChartSeriesFocus);
      }});

      svg.querySelectorAll('.hover-point').forEach(point => {{
        point.addEventListener('mousemove', event => {{
          const ds = event.currentTarget.dataset;
          focusChartSeries(ds.name);
          const isPointSingle = ds.single === '1';
          const valueTitle = isPointSingle ? '净额' : '累计';
          const dailyLine = isPointSingle || ds.daily === '' ? '' : `<div class="tooltip-row"><span>当日</span><strong>${{fmt(ds.daily)}} 亿</strong></div>`;
          tooltip.innerHTML = `<div class="tooltip-title">${{escHtml(ds.name)}}</div><div class="tooltip-row"><span>${{isPointSingle ? '时间' : '日期'}}</span><strong>${{escHtml(ds.label)}}</strong></div>${{dailyLine}}<div class="tooltip-row"><span>${{valueTitle}}</span><strong>${{fmt(ds.net)}} 亿</strong></div>`;
          const wrap = svg.parentElement.getBoundingClientRect();
          tooltip.style.display = 'block';
          const nextLeft = Math.min(event.clientX - wrap.left + 14, wrap.width - tooltip.offsetWidth - 10);
          const nextTop = Math.min(event.clientY - wrap.top + 14, wrap.height - tooltip.offsetHeight - 10);
          tooltip.style.left = `${{Math.max(10, nextLeft)}}px`;
          tooltip.style.top = `${{Math.max(10, nextTop)}}px`;
        }});
        point.addEventListener('mouseleave', () => {{
          tooltip.style.display = 'none';
          clearChartSeriesFocus();
        }});
      }});
    }}

    function drawTable() {{
      const rows = filteredRows(lastSnapshotRows(mode)).sort((a, b) => b.net - a.net);
      const {{ start, end, isSingle }} = dateRange();
      document.getElementById('tableNote').textContent = `${{rows.length}} 个板块 · ${{isSingle ? '各板块最近可用记录' : `${{start}} 至 ${{end}} 净额求和`}}`;
      const countText = v => v === null || v === undefined ? '' : v;
      const header = '<tr><th>序号</th><th>板块</th><th>净额(亿)</th><th title="过去半小时净额">半小时净额</th><th>涨跌幅</th><th>上涨</th><th>下跌</th><th>公司数</th><th>领涨股</th><th>领涨股涨幅</th></tr>';
      const body = rows.map((r, i) => {{
        const half = isSingle ? halfHourNet(r) : null;
        const halfText = half === null ? '<td></td>' : `<td class="${{half >= 0 ? 'pos' : 'neg'}}">${{fmt(half)}}</td>`;
        return `<tr><td>${{i + 1}}</td><td title="${{r.name}}">${{r.name}}</td><td class="${{r.net >= 0 ? 'pos' : 'neg'}}">${{fmt(r.net)}}</td>${{halfText}}<td>${{fmt(r.change)}}%</td><td class="pos">${{countText(r.upCount)}}</td><td class="neg">${{countText(r.downCount)}}</td><td>${{r.companies || ''}}</td><td title="${{r.leader}}">${{r.leader || ''}}</td><td>${{r.leaderChange ? fmt(r.leaderChange) + '%' : ''}}</td></tr>`;
      }}).join('');
      document.getElementById('fullTable').innerHTML = header + body;
    }}

    function drawTotalFlowChart() {{
      const {{ start, end, isSingle }} = dateRange();
      const kindName = mode === 'industry' ? '行业' : '概念';
      const data = totalFlowPoints(mode);
      const points = data.points || [];
      const ticks = data.ticks || [];
      const note = document.getElementById('totalFlowNote');
      const latest = document.getElementById('totalFlowLatest');
      const change = document.getElementById('totalFlowChange');
      const source = document.getElementById('totalFlowSource');
      const svg = document.getElementById('totalFlowChart');
      const tooltip = document.getElementById('totalFlowTooltip');
      svg.innerHTML = '';
      tooltip.style.display = 'none';
      note.textContent = `${{kindName}} · ${{data.modeText}} · ${{isSingle ? start : `${{start}} 至 ${{end}}`}}`;
      source.textContent = '全板块合计，不受搜索框影响';
      if (!points.length) {{
        latest.textContent = '';
        change.textContent = '';
        svg.insertAdjacentHTML('beforeend', '<text class="axis-label" x="50%" y="50%" text-anchor="middle">暂无总资金数据</text>');
        return;
      }}
      const last = points[points.length - 1];
      const first = points[0];
      const delta = last.net - first.net;
      latest.textContent = `${{fmt(last.net)}}亿`;
      latest.className = `mini-price ${{last.net >= 0 ? 'pos' : 'neg'}}`;
      change.className = delta >= 0 ? 'pos' : 'neg';
      change.textContent = `${{delta >= 0 ? '+' : ''}}${{fmt(delta)}}亿`;

      const width = 1120, height = 280;
      const margin = {{ left: 58, right: 24, top: 18, bottom: 36 }};
      svg.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);
      const values = points.map(p => p.net);
      let min = Math.min(0, ...values), max = Math.max(0, ...values);
      const pad = Math.max(5, (max - min) * 0.16);
      min -= pad; max += pad;
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      const x = i => margin.left + (points.length <= 1 ? 0 : i * plotW / (points.length - 1));
      const y = value => margin.top + (max - value) * plotH / (max - min || 1);
      for (let i = 0; i <= 4; i++) {{
        const gy = margin.top + i * plotH / 4;
        const val = max - i * (max - min) / 4;
        svg.insertAdjacentHTML('beforeend', `<line class="grid" x1="${{margin.left}}" y1="${{gy}}" x2="${{width - margin.right}}" y2="${{gy}}"></line>`);
        svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{margin.left - 8}}" y="${{gy + 4}}" text-anchor="end">${{fmt(val)}}亿</text>`);
      }}
      const zeroY = y(0);
      svg.insertAdjacentHTML('beforeend', `<line x1="${{margin.left}}" y1="${{zeroY}}" x2="${{width - margin.right}}" y2="${{zeroY}}" stroke="#98a2b3" stroke-width="1.4"></line>`);
      ticks.forEach((t, i) => {{
        if (i % Math.max(1, Math.floor(ticks.length / 7)) === 0 || i === ticks.length - 1) {{
          svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{x(i)}}" y="${{height - 12}}" text-anchor="middle">${{t}}</text>`);
        }}
      }});
      const color = last.net >= 0 ? '#b72d39' : '#0f766e';
      const path = points.map((point, i) => `${{i ? 'L' : 'M'}} ${{x(i).toFixed(1)}} ${{y(point.net).toFixed(1)}}`).join(' ');
      svg.insertAdjacentHTML('beforeend', `<path class="line" d="${{path}}" stroke="${{color}}"></path>`);
      points.forEach((point, i) => {{
        const daily = point.dailyNet === undefined ? '' : point.dailyNet;
        svg.insertAdjacentHTML('beforeend', `<circle class="hover-point total-flow-point" cx="${{x(i).toFixed(1)}}" cy="${{y(point.net).toFixed(1)}}" r="8" fill="${{color}}" data-label="${{escAttr(point.label)}}" data-net="${{escAttr(point.net)}}" data-daily="${{escAttr(daily)}}" data-count="${{escAttr(point.count)}}" data-single="${{isSingle ? '1' : '0'}}"></circle>`);
      }});
      svg.querySelectorAll('.total-flow-point').forEach(point => {{
        point.addEventListener('mousemove', event => {{
          const ds = event.currentTarget.dataset;
          const dailyLine = ds.single === '1' || ds.daily === '' ? '' : `<div class="tooltip-row"><span>当日合计</span><strong>${{fmt(ds.daily)}} 亿</strong></div>`;
          tooltip.innerHTML = `<div class="tooltip-title">${{kindName}}总资金</div><div class="tooltip-row"><span>${{ds.single === '1' ? '时间' : '日期'}}</span><strong>${{escHtml(ds.label)}}</strong></div>${{dailyLine}}<div class="tooltip-row"><span>${{ds.single === '1' ? '净额' : '累计'}}</span><strong>${{fmt(ds.net)}} 亿</strong></div><div class="tooltip-row"><span>板块数</span><strong>${{escHtml(ds.count)}}</strong></div>`;
          const wrap = svg.parentElement.getBoundingClientRect();
          tooltip.style.display = 'block';
          const nextLeft = Math.min(event.clientX - wrap.left + 14, wrap.width - tooltip.offsetWidth - 10);
          const nextTop = Math.min(event.clientY - wrap.top + 14, wrap.height - tooltip.offsetHeight - 10);
          tooltip.style.left = `${{Math.max(10, nextLeft)}}px`;
          tooltip.style.top = `${{Math.max(10, nextTop)}}px`;
        }});
        point.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
    }}

    function drawNasdaqChart() {{
      const data = payload.nasdaqFutures || {{}};
      const rows = data.rows || [];
      const dailyRows = data.dailyRows || [];
      const note = document.getElementById('nasdaqNote');
      const latest = document.getElementById('nasdaqLatest');
      const change = document.getElementById('nasdaqChange');
      const source = document.getElementById('nasdaqSource');
      const bars = document.getElementById('nasdaqBars');
      const svg = document.getElementById('nasdaqChart');
      const tooltip = document.getElementById('tooltip');
      svg.innerHTML = '';
      bars.innerHTML = '';
      if (!data.ok || !rows.length) {{
        note.textContent = data.error ? `获取失败：${{data.error}}` : '暂无数据';
        latest.textContent = '';
        change.textContent = '';
        source.textContent = data.source || '';
        return;
      }}

      note.textContent = `${{rows[0].label}} 至 ${{rows[rows.length - 1].label}} · 小时级`;
      const last = rows[rows.length - 1];
      latest.textContent = fmt(last.close);
      const totalPct = last.changeFromFirstPct || 0;
      change.className = totalPct >= 0 ? 'pos' : 'neg';
      change.textContent = `${{totalPct >= 0 ? '+' : ''}}${{fmt(totalPct)}}%`;
      source.textContent = `${{data.symbol}} · ${{data.source}} · ${{data.interval || '1h'}} · ${{(data.updatedAt || '').replace('T', ' ')}}`;

      const width = 1120, height = 320;
      const margin = {{ left: 54, right: 22, top: 20, bottom: 42 }};
      svg.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);
      const values = rows.map(r => r.close);
      let min = Math.min(...values), max = Math.max(...values);
      const pad = Math.max(20, (max - min) * 0.18);
      min -= pad; max += pad;
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      const x = i => margin.left + (rows.length <= 1 ? 0 : i * plotW / (rows.length - 1));
      const y = value => margin.top + (max - value) * plotH / (max - min || 1);
      for (let i = 0; i <= 4; i++) {{
        const gy = margin.top + i * plotH / 4;
        const val = max - i * (max - min) / 4;
        svg.insertAdjacentHTML('beforeend', `<line class="grid" x1="${{margin.left}}" y1="${{gy}}" x2="${{width - margin.right}}" y2="${{gy}}"></line>`);
        svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{margin.left - 8}}" y="${{gy + 4}}" text-anchor="end">${{fmt(val)}}</text>`);
      }}
      const seenDates = new Set();
      rows.forEach((row, i) => {{
        if (!seenDates.has(row.date) || i === rows.length - 1) {{
          seenDates.add(row.date);
          svg.insertAdjacentHTML('beforeend', `<line class="grid" x1="${{x(i)}}" y1="${{margin.top}}" x2="${{x(i)}}" y2="${{height - margin.bottom}}" opacity=".45"></line>`);
          svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{x(i)}}" y="${{height - 14}}" text-anchor="middle">${{row.date.slice(5)}}</text>`);
        }}
      }});
      const color = totalPct >= 0 ? '#b72d39' : '#0f766e';
      const path = rows.map((row, i) => `${{i ? 'L' : 'M'}} ${{x(i).toFixed(1)}} ${{y(row.close).toFixed(1)}}`).join(' ');
      svg.insertAdjacentHTML('beforeend', `<path class="line" d="${{path}}" stroke="${{color}}"></path>`);
      rows.forEach((row, i) => {{
        const marker = i === rows.length - 1 || i % 6 === 0;
        svg.insertAdjacentHTML('beforeend', `<circle class="nasdaq-point" cx="${{x(i).toFixed(1)}}" cy="${{y(row.close).toFixed(1)}}" r="${{marker ? 3.2 : 2}}" fill="${{color}}" data-label="${{escAttr(row.label)}}" data-close="${{escAttr(row.close)}}" data-pct="${{escAttr(row.changeFromFirstPct)}}"></circle>`);
      }});
      svg.querySelectorAll('.nasdaq-point').forEach(point => {{
        point.addEventListener('mousemove', event => {{
          const ds = event.currentTarget.dataset;
          tooltip.innerHTML = `<div class="tooltip-title">纳斯达克连续期指</div><div class="tooltip-row"><span>时间</span><strong>${{escHtml(ds.label)}}</strong></div><div class="tooltip-row"><span>点位</span><strong>${{fmt(ds.close)}}</strong></div><div class="tooltip-row"><span>7日</span><strong>${{Number(ds.pct) >= 0 ? '+' : ''}}${{fmt(ds.pct)}}%</strong></div>`;
          const wrap = svg.parentElement.getBoundingClientRect();
          tooltip.style.display = 'block';
          const nextLeft = Math.min(event.clientX - wrap.left + 14, wrap.width - tooltip.offsetWidth - 10);
          const nextTop = Math.min(event.clientY - wrap.top + 14, wrap.height - tooltip.offsetHeight - 10);
          tooltip.style.left = `${{Math.max(10, nextLeft)}}px`;
          tooltip.style.top = `${{Math.max(10, nextTop)}}px`;
        }});
        point.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
      dailyRows.forEach(day => {{
        const pct = day.changePct;
        const pctText = `${{pct >= 0 ? '+' : ''}}${{fmt(pct)}}%`;
        const cls = pct >= 0 ? 'pos' : 'neg';
        bars.insertAdjacentHTML('beforeend', `<div class="mini-bar"><div class="mini-bar-date">${{day.date}} · ${{day.points}}点</div><div class="mini-bar-value">${{fmt(day.close)}}</div><div class="${{cls}}">${{pctText}}</div><div>高 ${{fmt(day.high)}} / 低 ${{fmt(day.low)}}</div></div>`);
      }});
    }}

    function recentStats(row, lookback = 3) {{
      const {{ end }} = dateRange();
      const dates = (payload.availableDates || []).filter(d => d <= end).slice(-lookback);
      const rows = dates.map(date => latestRowsForDate(row.kind, date).find(r => r.name === row.name)).filter(Boolean);
      const total = rows.reduce((sum, r) => sum + r.net, 0);
      const positiveDays = rows.filter(r => r.net > 0).length;
      const negativeDays = rows.filter(r => r.net < 0).length;
      return {{ total, positiveDays, negativeDays, days: rows.length }};
    }}

    function nasdaqSignal() {{
      const rows = payload.nasdaqFutures?.rows || [];
      if (!rows.length) return {{ text: '纳指数据暂缺', bias: 'neutral', cls: '' }};
      const last = rows[rows.length - 1];
      const totalPct = last.changeFromFirstPct || 0;
      const dailyRows = payload.nasdaqFutures?.dailyRows || [];
      const lastDay = dailyRows[dailyRows.length - 1] || null;
      const lastPct = lastDay?.changePct ?? 0;
      const upDays = dailyRows.filter(r => (r.changePct || 0) > 0).length;
      const bias = totalPct >= 1.2 && lastPct >= -0.8 ? 'positive' : (totalPct <= -1.2 || lastPct <= -1.5 ? 'negative' : 'neutral');
      const text = `纳指7日小时线${{totalPct >= 0 ? '+' : ''}}${{fmt(totalPct)}}%，最近交易段${{lastPct >= 0 ? '+' : ''}}${{fmt(lastPct)}}%，上涨日 ${{upDays}}/${{dailyRows.length || 0}}`;
      return {{ text, bias, cls: bias === 'positive' ? 'pos' : (bias === 'negative' ? 'neg' : '') }};
    }}

    function analysisScore(row, stats, isSingle) {{
      const half = isSingle ? halfHourNet(row) : 0;
      return row.net * 2.2 + half * 1.4 + row.change * 2.4 + (row.leaderChange || 0) * 0.35 + stats.total * 0.22;
    }}

    function renderAnalysisItem(row, kind, isRisk, isSingle) {{
      const stats = recentStats(row);
      const half = isSingle ? halfHourNet(row) : null;
      const leader = row.leader ? `${{escHtml(row.leader)}} ${{row.leaderChange ? fmt(row.leaderChange) + '%' : ''}}` : '暂无领涨股';
      const flow = isSingle ? `半小时 ${{fmt(half)}}亿` : `${{stats.days}}日 ${{fmt(stats.total)}}亿`;
      const trend = `${{stats.days}}日累计 ${{fmt(stats.total)}}亿`;
      const reason = isRisk
        ? `净额 ${{fmt(row.net)}}亿，${{flow}}，涨跌幅 ${{fmt(row.change)}}%，个股：${{leader}}`
        : `今日净额 ${{fmt(row.net)}}亿，涨跌幅 ${{fmt(row.change)}}%，${{flow}}，个股：${{leader}}`;
      const netCls = row.net >= 0 ? 'pos' : 'neg';
      return `<div class="analysis-item">
        <div class="analysis-sector" title="${{escAttr(row.name)}}">${{escHtml(row.name)}}</div>
        <div>
          <div class="analysis-meta"><span class="${{netCls}}">净额 ${{fmt(row.net)}}亿</span><span>${{trend}}</span><span>${{kind}}</span></div>
          <div class="analysis-reason" title="${{escAttr(reason)}}">${{reason}}</div>
        </div>
      </div>`;
    }}

    function stockCodeText(code) {{
      const text = String(code || '').trim();
      return /^\\d+$/.test(text) ? text.padStart(6, '0') : text;
    }}

    function renderStockCard(stock) {{
      const changeCls = stock.change >= 0 ? 'pos' : 'neg';
      const netCls = stock.net >= 0 ? 'pos' : 'neg';
      return `<div class="stock-card">
        <div class="stock-name" title="${{escAttr(stock.name)}}">${{escHtml(stock.name)}}<span class="stock-code">${{escHtml(stockCodeText(stock.code))}}</span></div>
        <div class="stock-metrics"><span class="${{netCls}}">大单净 ${{fmtMoney(stock.net)}}</span><span class="${{changeCls}}">涨跌 ${{stock.change >= 0 ? '+' : ''}}${{fmt(stock.change)}}%</span><span>买入 ${{fmtMoney(stock.inflow)}}</span></div>
      </div>`;
    }}

    function drawAnalysis() {{
      const {{ start, end, isSingle }} = dateRange();
      const kindName = mode === 'industry' ? '行业' : '概念';
      const rows = filteredRows(lastSnapshotRows(mode));
      const signal = nasdaqSignal();
      const enriched = rows.map(row => {{
        const stats = recentStats(row);
        const half = isSingle ? halfHourNet(row) : 0;
        const score = analysisScore(row, stats, isSingle);
        return {{ ...row, stats, half, score }};
      }});
      const strong = enriched
        .filter(r => r.net > 0 && r.change >= -0.4 && (!isSingle || r.half >= -0.05))
        .sort((a, b) => b.score - a.score)
        .slice(0, 6);
      const risk = enriched
        .filter(r => r.net < 0 || r.change <= -1 || (isSingle && r.half < 0) || (r.stats.total < 0 && r.change < 0))
        .sort((a, b) => a.score - b.score)
        .slice(0, 6);
      document.getElementById('analysisNote').textContent = `${{kindName}} · 优先当天 · ${{isSingle ? `${{start}} 最新快照` : `${{start}} 至 ${{end}}`}}`;
      document.getElementById('analysisStrong').innerHTML = strong.length
        ? strong.map(row => renderAnalysisItem(row, kindName, false, isSingle)).join('')
        : '<div class="analysis-item"><div class="analysis-sector">暂无明显强势</div><div class="analysis-reason">当前筛选下没有同时满足资金和走势条件的板块</div></div>';
      document.getElementById('analysisRisk').innerHTML = risk.length
        ? risk.map(row => renderAnalysisItem(row, kindName, true, isSingle)).join('')
        : '<div class="analysis-item"><div class="analysis-sector">暂无明显风险</div><div class="analysis-reason">当前筛选下没有显著净流出或转弱板块</div></div>';
      const stockDate = end;
      const stockRows = latestStocksForDate(stockDate);
      const stockStrong = stockRows
        .filter(s => s.net > 0 && s.change >= 0)
        .sort((a, b) => (b.net * 2 + b.change * 0.2 + b.inflow * 0.3) - (a.net * 2 + a.change * 0.2 + a.inflow * 0.3))
        .slice(0, 8);
      const stockRisk = stockRows
        .filter(s => s.change <= -3 || s.net < 0)
        .sort((a, b) => (a.change * 0.25 + a.net * 2) - (b.change * 0.25 + b.net * 2))
        .slice(0, 8);
      const stockHtml = [
        ...stockStrong.map(renderStockCard),
        ...stockRisk.map(renderStockCard),
      ].join('');
      document.getElementById('analysisStocks').innerHTML = stockHtml
        || '<div class="analysis-item"><div class="analysis-sector">暂无大单数据</div><div class="analysis-reason">当前日期的快照还没有大单追踪数据，下一次刷新后会显示</div></div>';
      const techHint = signal.bias === 'positive'
        ? '外盘科技风险偏好偏强，对算力、半导体、消费电子等科技映射更友好。'
        : (signal.bias === 'negative' ? '外盘科技走弱，A股科技高弹性方向更容易放大波动。' : '外盘科技信号中性，更多看本地资金持续性。');
      document.getElementById('analysisNasdaq').innerHTML = `<div><strong class="${{signal.cls}}">${{signal.text}}</strong><div>${{techHint}}</div></div><div>口径：板块资金流优先 + 个股大单追踪 + 近3个有数据交易日 + 领涨股 + 纳指期指</div>`;
    }}

    function redraw() {{ drawChart(); drawTable(); drawTotalFlowChart(); drawNasdaqChart(); drawAnalysis(); }}
    function setMode(next) {{
      mode = next;
      industryBtn.classList.toggle('active', mode === 'industry');
      conceptBtn.classList.toggle('active', mode === 'concept');
      redraw();
    }}
    function updateClock() {{
      const d = new Date();
      document.getElementById('clock').textContent = d.toTimeString().slice(0, 5);
    }}

    function isTradingRefreshTime(date) {{
      const day = date.getDay();
      if (day === 0 || day === 6) return false;
      const minutes = date.getHours() * 60 + date.getMinutes();
      return (minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60);
    }}

    function maybeReloadForTradingData() {{
      if (isTradingRefreshTime(new Date())) window.location.reload();
    }}

    const dates = payload.availableDates || [];
    const latest = payload.latestDate || dates[dates.length - 1] || '';
    document.getElementById('startDate').value = latest;
    document.getElementById('endDate').value = latest;
    document.getElementById('datePreset').innerHTML = dates.map(d => `<option value="${{d}}">${{d}}</option>`).join('');
    document.getElementById('datePreset').value = latest;
    document.getElementById('startDate').min = dates[0] || '';
    document.getElementById('startDate').max = dates[dates.length - 1] || '';
    document.getElementById('endDate').min = dates[0] || '';
    document.getElementById('endDate').max = dates[dates.length - 1] || '';
    document.getElementById('industryBtn').onclick = () => setMode('industry');
    document.getElementById('conceptBtn').onclick = () => setMode('concept');
    document.getElementById('search').oninput = redraw;
    document.getElementById('startDate').onchange = redraw;
    document.getElementById('endDate').onchange = redraw;
    document.getElementById('datePreset').onchange = event => {{
      document.getElementById('startDate').value = event.target.value;
      document.getElementById('endDate').value = event.target.value;
      redraw();
    }};
    updateClock();
    setInterval(updateClock, 1000);
    setInterval(maybeReloadForTradingData, 300000);
    redraw();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=str(DATA_ROOT))
    parser.add_argument("--output", default=str(OUTPUT_HTML))
    args = parser.parse_args()

    data_root = Path(args.data_root)
    payload = build_payload(find_snapshots(data_root))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(payload), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
