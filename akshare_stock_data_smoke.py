#!/usr/bin/env python3
"""Smoke-test AKShare Stock Skill data access.

This checks the concrete APIs needed for the intraday sector-flow dashboard:
AKShare import, skill adapter fallback, and numeric sector net-flow fields.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path


SKILL_DIR = Path("/Users/holo/.codex/skills/akshare-stock")


def numeric(value: object) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(str(value).replace(",", "").strip())
        if math.isnan(parsed):
            return None
        return parsed
    except Exception:
        return None


def main() -> int:
    sys.path.insert(0, str(SKILL_DIR))

    try:
        import akshare as ak
    except Exception as exc:
        print(f"FAIL import akshare: {exc}")
        return 1

    print(f"akshare_version={ak.__version__}")

    required = [
        "stock_sector_fund_flow_rank",
        "stock_fund_flow_industry",
        "stock_sector_fund_flow_hist",
        "stock_board_industry_hist_min_em",
    ]
    missing = [name for name in required if not hasattr(ak, name)]
    if missing:
        print(f"FAIL missing APIs: {', '.join(missing)}")
        return 1
    print("required_apis=ok")

    from adapters import AkshareAdapter

    adapter = AkshareAdapter()
    result = adapter.sector_money_flow(top_n=20)
    if not result.get("ok"):
        print(f"FAIL sector_money_flow: {result.get('error')}")
        return 1

    api = result.get("api")
    items = (result.get("data") or {}).get("items") or []
    print(f"sector_money_flow_api={api}")
    print(f"sector_rows={len(items)}")
    if len(items) < 5:
        print("FAIL sector rows too few")
        return 1

    usable = []
    for row in items:
        if not isinstance(row, dict):
            continue
        sector_name = row.get("行业") or row.get("名称") or row.get("板块名称")
        net_flow = (
            row.get("净额")
            or row.get("主力净流入")
            or row.get("今日主力净流入-净额")
            or row.get("净流入")
        )
        parsed = numeric(net_flow)
        if sector_name and parsed is not None:
            usable.append((str(sector_name), parsed))

    print(f"usable_sector_net_flow_rows={len(usable)}")
    if len(usable) < 5:
        print("FAIL no reliable sector net-flow numeric field")
        return 1

    for name, value in usable[:5]:
        print(f"{name}: {value}")

    print("PASS akshare stock data access")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
