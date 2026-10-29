"""Regression checks for missing and duplicated sector data."""

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from build_sector_fund_flow_dashboard import Snapshot, build_payload, find_snapshots, read_snapshot_table, render_html
from pull_akshare_monthly_daily_data import pull_sector_flow_now
from run_sector_fund_flow_schedule_tick import csv_row_count, log_missing_sectors, run_pull_once


class SectorCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_retry_merges_real_data_and_deduplicates_names(self):
        fetch = Mock(side_effect=[
            pd.DataFrame({"行业": ["银行", "银行"], "净额": [1, 2]}),
            pd.DataFrame({"行业": ["半导体", "银行"], "净额": [-3, 4]}),
        ])
        ak = SimpleNamespace(stock_fund_flow_industry=fetch, stock_board_industry_summary_ths=lambda: pd.DataFrame())
        catalog = {"industry": {"names": ["银行", "半导体", "新板块"], "flowNames": ["银行", "半导体"]}}
        result = pull_sector_flow_now(ak, self.conn, self.root, 2, "industry", catalog)
        rows = pd.read_csv(self.root / "industry_fund_flow_now.csv").set_index("行业")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.error, "")
        self.assertEqual(rows.loc["银行", "净额"], 4)
        self.assertEqual(rows.loc["半导体", "净额"], -3)
        self.assertTrue(pd.isna(rows.loc["新板块", "净额"]))
        self.assertEqual(csv_row_count(self.root / "industry_fund_flow_now.csv"), 2)

    def test_residual_gap_is_null_and_reported(self):
        ak = SimpleNamespace(stock_fund_flow_concept=lambda symbol: pd.DataFrame({"行业": ["人工智能"], "净额": [0]}))
        catalog = {"concept": {"names": ["人工智能", "芯片概念"], "flowNames": ["人工智能", "芯片概念"]}}
        result = pull_sector_flow_now(ak, self.conn, self.root, 2, "concept", catalog)
        self.assertIn("芯片概念", result.error)
        snapshot = Snapshot(self.root, datetime(2026, 10, 9, 10), "2026-10-09", "10:00")
        rows = {r["name"]: r for r in read_snapshot_table(snapshot, "concept_fund_flow_now.csv", "concept")}
        self.assertEqual(rows["人工智能"]["net"], 0)
        self.assertIsNone(rows["人工智能"]["change"])
        self.assertIsNone(rows["芯片概念"]["net"])
        self.assertIsNone(rows["芯片概念"]["companies"])
        json.dumps(list(rows.values()), allow_nan=False)

    def test_directory_preserves_sector_absent_from_all_snapshots(self):
        (self.root / "sector_catalog.json").write_text(json.dumps({"industry": {"names": ["半导体"]}}))
        with patch("build_sector_fund_flow_dashboard.fetch_nasdaq_futures_7d", return_value={}):
            payload = build_payload([], self.root)
        self.assertEqual(payload["sectorNames"]["industry"], ["半导体"])
        render_html(payload)

    def test_failed_concept_pull_does_not_hide_industry_snapshot(self):
        snapshot_dir = self.root / "20261009_093000_sector_fund_flow"
        snapshot_dir.mkdir()
        pd.DataFrame({"行业": ["半导体"], "净额": [1]}).to_csv(snapshot_dir / "industry_fund_flow_now.csv", index=False)
        snapshots = find_snapshots(self.root)
        self.assertEqual(len(snapshots), 1)
        with patch("build_sector_fund_flow_dashboard.fetch_nasdaq_futures_7d", return_value={}):
            payload = build_payload(snapshots, self.root)
        self.assertEqual(payload["snapshotTimes"]["2026-10-09"], ["09:30"])
        self.assertEqual(payload["rows"][0]["name"], "半导体")

    def test_completeness_count_ignores_duplicates_and_nulls(self):
        path = self.root / "industry_fund_flow_now.csv"
        pd.DataFrame({"行业": ["银行", "银行", "半导体", "新板块"], "净额": [1, 2, 0, None]}).to_csv(path, index=False)
        self.assertEqual(csv_row_count(path), 2)

    def test_schedule_logs_residual_gap_but_not_success(self):
        completed = subprocess.CompletedProcess([], 0, "success", "")
        with patch("run_sector_fund_flow_schedule_tick.subprocess.run", return_value=completed), patch("run_sector_fund_flow_schedule_tick.log") as log:
            run_pull_once(2, 240)
            log.assert_not_called()
        pd.DataFrame([{"label": "industry_fund_flow_now", "error": ""},
                      {"label": "concept_fund_flow_now", "error": "missing 1 sector"}]).to_csv(self.root / "pull_summary.csv", index=False)
        with patch("run_sector_fund_flow_schedule_tick.log") as log, patch("run_sector_fund_flow_schedule_tick.append_status") as status:
            log_missing_sectors(self.root)
            log.assert_called_once()
            status.assert_called_once()
            self.assertEqual(status.call_args.kwargs["status"], "incomplete")

    def test_dashboard_keeps_nulls_stale_times_and_cumulative_gaps(self):
        def row(date, time, name, net):
            return {"kind": "industry", "date": date, "time": time,
                    "stamp": f"{date}T{time}:00", "name": name, "net": net,
                    "change": 0 if net is not None else None, "leader": "",
                    "leaderChange": None, "companies": None, "upCount": None,
                    "downCount": None}
        payload = {"availableDates": ["2026-10-08", "2026-10-09", "2026-10-10"],
                   "latestDate": "2026-10-09", "sectorNames": {"industry": ["半导体", "银行", "无数据"], "concept": []},
                   "rows": [row("2026-10-08", "15:00", "半导体", -150),
                            row("2026-10-09", "09:35", "半导体", 10),
                            row("2026-10-09", "10:05", "半导体", 80),
                            row("2026-10-09", "10:10", "半导体", None),
                            row("2026-10-09", "10:10", "银行", 0),
                            row("2026-10-10", "15:00", "半导体", None)],
                   "nasdaqFutures": {}, "stocks": []}
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(0, 'utf8');
const data = html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1];
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'', textContent:'', innerHTML:'', style:{},
    classList:{toggle(){}}, setAttribute(){}, querySelectorAll(){return []},
    insertAdjacentHTML(position, value){this.innerHTML += value;}});
  return elements.get(id);
}
element('payload').textContent = data;
const context = vm.createContext({document:{getElementById:element}, setInterval(){}});
vm.runInContext(script, context);
vm.runInContext(`
  const rows = latestRowsForDate('industry', '2026-10-09');
  const semi = rows.find(r => r.name === '半导体');
  if (rows.length !== 3 || semi.net !== 80 || !semi.stale) throw Error('lost sector or stale data');
  if (rows.find(r => r.name === '无数据').net !== null) throw Error('missing sector became zero');
  if (halfHourNet(semi) !== 70) throw Error('invalid half-hour baseline');
  if (halfHourNet(payload.rows[1]) !== 10) throw Error('opening half-hour');
  if (halfHourNet(rows.find(r => r.name === '无数据')) !== null) throw Error('null half-hour');
  if (selectedNames('industry').includes('无数据')) throw Error('null entered ranking');
  document.getElementById('startDate').value = '2026-10-08';
  document.getElementById('endDate').value = '2026-10-10';
  const range = lastSnapshotRows('industry');
  if (range.find(r => r.name === '半导体').net !== -70) throw Error('incorrect cumulative sum');
  if (range.find(r => r.name === '无数据').net !== null) throw Error('null range became zero');
  const points = chartPoints('industry', '半导体').points;
  if (points[0].net !== -150 || points[1].net !== -70 || points[2].net !== null) throw Error('incorrect chart gap');
  redraw();
`, context);
assert(element('fullTable').innerHTML.includes('无数据'));
assert(element('fullTable').innerHTML.includes('null'));
assert(!element('fullTable').innerHTML.includes('NaN'));
assert(!element('chart').innerHTML.includes('NaN'));
"""
        subprocess.run(["node", "-e", script], input=render_html(payload), text=True, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
