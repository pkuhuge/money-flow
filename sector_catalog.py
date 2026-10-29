"""Keep the THS sector directory independent of paginated fund-flow data."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path


def load_sector_catalog(data_root: Path) -> dict:
    path = data_root / "sector_catalog.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_sector_catalog(ak, data_root: Path) -> dict:
    catalog = load_sector_catalog(data_root)
    today = datetime.now().strftime("%Y-%m-%d")
    for kind in ("industry", "concept"):
        entry = catalog.setdefault(kind, {})
        if entry.get("updatedDate") == today:
            continue
        flow_names = set(entry.get("flowNames", []))
        for path in data_root.glob(f"*_sector_fund_flow/{kind}_fund_flow_now.csv"):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("行业") or "").strip()
                    if name and row.get("净额") not in (None, "", "--", "-"):
                        flow_names.add(name)
        names = set(entry.get("names", [])) | flow_names
        try:
            df = getattr(ak, f"stock_board_{kind}_name_ths")()
            if df.empty or "name" not in df:
                raise ValueError("empty sector directory")
            names.update(df["name"].dropna().astype(str).str.strip())
            entry["updatedDate"] = today
        except Exception as exc:
            print(f"{kind} sector directory failed: {type(exc).__name__}: {exc}")
        entry["names"] = sorted(names - {""})
        entry["flowNames"] = sorted(flow_names)
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "sector_catalog.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return catalog
