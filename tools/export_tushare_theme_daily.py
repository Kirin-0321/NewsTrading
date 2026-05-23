"""导出各 Tushare 题材源指定交易日的完整数据（对比选型 / 回测用）。

用法:
    python tools/export_tushare_theme_daily.py
    python tools/export_tushare_theme_daily.py 20260522

输出目录: data/tushare_theme_compare/
Token: .env 中 tushare=
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "tushare_theme_compare"
URL = "http://api.tushare.pro"

# 用户关心的热门题材（模糊匹配用）
THEME_KEYWORDS = [
    ("机器人", ["机器人", "人形机器人", "具身智能"]),
    ("半导体", ["半导体", "芯片"]),
    ("电网", ["电网", "智能电网", "特高压", "电力"]),
    ("算力", ["算力", "数据中心", "东数西算", "核心城市算力"]),
    ("AI应用", ["AI应用", "人工智能", "多模态AI", "ChatGPT", "AI智能体", "AIGC", "Sora"]),
]


def load_token() -> str:
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("tushare="):
                return line.split("=", 1)[1].strip()
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise SystemExit("未找到 TUSHARE token（.env 中 tushare=）")
    return token


def call(token: str, api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
    body = json.dumps(
        {
            "api_name": api_name,
            "token": token,
            "params": params or {},
            "fields": fields,
        }
    ).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read().decode())
    if r.get("code") != 0:
        raise RuntimeError(f"{api_name}: {r.get('msg')}")
    flds = r["data"]["fields"]
    return [dict(zip(flds, row)) for row in r["data"]["items"]]


def write_csv(path: Path, rows: list[dict], col_order: list[str] | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("empty\n", encoding="utf-8-sig")
        return 0
    keys = col_order or list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in keys})
    return len(rows)


def resolve_trade_date(token: str, arg: str | None) -> str:
    if arg and len(arg) == 8 and arg.isdigit():
        return arg
    # 默认：SSE 最近一个开市日
    from datetime import datetime, timedelta

    d = datetime.now()
    for _ in range(14):
        ds = d.strftime("%Y%m%d")
        rows = call(token, "trade_cal", {"exchange": "SSE", "start_date": ds, "end_date": ds})
        if rows and str(rows[0].get("is_open")) == "1":
            return ds
        d -= timedelta(days=1)
    raise SystemExit("无法解析最近交易日")


def export_all(trade_date: str) -> dict[str, int]:
    token = load_token()
    stats: dict[str, int] = {}

    # 大盘快照
    indices = []
    for code, label in [
        ("000001.SH", "上证"),
        ("399001.SZ", "深成指"),
        ("399006.SZ", "创业板"),
        ("000300.SH", "沪深300"),
    ]:
        rows = call(token, "index_daily", {"ts_code": code, "start_date": trade_date, "end_date": trade_date})
        if rows:
            rows[0]["label"] = label
            indices.append(rows[0])
    stats["market_indices"] = write_csv(
        OUT_DIR / f"market_indices_{trade_date}.csv",
        indices,
        ["label", "ts_code", "trade_date", "pct_chg", "close", "vol", "amount"],
    )

    dc = call(token, "dc_index", {"trade_date": trade_date})
    dc.sort(key=lambda x: float(x.get("pct_change") or 0), reverse=True)
    stats["dc_index"] = write_csv(
        OUT_DIR / f"dc_index_{trade_date}.csv",
        dc,
        [
            "ts_code", "name", "pct_change", "leading", "leading_pct",
            "up_num", "down_num", "turnover_rate", "total_mv", "idx_type",
        ],
    )

    dc_daily = call(token, "dc_daily", {"trade_date": trade_date})
    name_map = {r["ts_code"]: r.get("name", "") for r in dc}
    for r in dc_daily:
        r["name"] = name_map.get(r.get("ts_code", ""), "")
    dc_daily.sort(key=lambda x: float(x.get("pct_change") or 0), reverse=True)
    stats["dc_daily"] = write_csv(
        OUT_DIR / f"dc_daily_{trade_date}.csv",
        dc_daily,
        ["ts_code", "name", "trade_date", "close", "pct_change", "category", "turnover_rate", "amount"],
    )

    kpl = call(token, "kpl_concept", {"trade_date": trade_date})
    try:
        alt = call(token, "kpl_list", {"trade_date": trade_date})
        if len(alt) > len(kpl):
            kpl = alt
            kpl_name = f"kpl_list_{trade_date}.csv"
        else:
            kpl_name = f"kpl_concept_{trade_date}.csv"
    except RuntimeError:
        kpl_name = f"kpl_concept_{trade_date}.csv"
    pct_key = next(
        (k for k in (kpl[0].keys() if kpl else []) if "pct" in k.lower() or "change" in k.lower()),
        None,
    )
    if pct_key:
        kpl.sort(key=lambda x: float(x.get(pct_key) or 0), reverse=True)
    else:
        kpl.sort(key=lambda x: int(x.get("z_t_num") or 0), reverse=True)
    stats["kpl"] = write_csv(OUT_DIR / kpl_name, kpl)

    ths_idx = call(token, "ths_index", {"exchange": "A"})
    type_map = {x["ts_code"]: x.get("type") for x in ths_idx}
    name_map_ths = {x["ts_code"]: x.get("name") for x in ths_idx}

    ths = call(token, "ths_daily", {"trade_date": trade_date})
    for r in ths:
        r.setdefault("name", name_map_ths.get(r.get("ts_code"), ""))
        r.setdefault("type", type_map.get(r.get("ts_code"), ""))
    pct_col = "pct_change" if ths and "pct_change" in ths[0] else "pct_chg"
    ths.sort(key=lambda x: float(x.get(pct_col) or 0), reverse=True)
    stats["ths_all"] = write_csv(
        OUT_DIR / f"ths_ths_daily_by_date_{trade_date}.csv",
        ths,
        ["ts_code", "name", "type", "trade_date", "pct_change", "pct_chg", "close", "vol"],
    )

    ths_n = sorted([r for r in ths if r.get("type") == "N"], key=lambda x: float(x.get(pct_col) or 0), reverse=True)
    ths_i = sorted([r for r in ths if r.get("type") == "I"], key=lambda x: float(x.get(pct_col) or 0), reverse=True)
    stats["ths_concept_n"] = write_csv(
        OUT_DIR / f"ths_concept_typeN_{trade_date}.csv",
        ths_n,
        ["ts_code", "name", "type", "trade_date", "pct_change", "pct_chg", "close", "vol"],
    )
    stats["ths_industry_i"] = write_csv(
        OUT_DIR / f"ths_industry_typeI_{trade_date}.csv",
        ths_i,
        ["ts_code", "name", "type", "trade_date", "pct_change", "pct_chg", "close", "vol"],
    )

    concept = call(token, "concept", {})
    stats["concept_static"] = write_csv(
        OUT_DIR / f"concept_static_{trade_date}.csv",
        concept,
        ["code", "name", "src"],
    )

    try:
        limit_cpt = call(token, "limit_cpt_list", {"trade_date": trade_date})
        limit_cpt.sort(key=lambda x: float(x.get("pct_chg") or 0), reverse=True)
        stats["limit_cpt"] = write_csv(
            OUT_DIR / f"limit_cpt_list_{trade_date}.csv",
            limit_cpt,
            ["ts_code", "name", "trade_date", "pct_chg", "days", "up_stat", "cons_nums", "up_nums", "rank"],
        )
    except RuntimeError as e:
        print(f"  [skip] limit_cpt_list: {e}")
        stats["limit_cpt"] = 0

    # 关键词对比
    compare_rows = []
    sources = [
        ("东财dc_index", dc, "name"),
        ("开盘啦", kpl, "name"),
        ("同花顺概念N", ths_n, "name"),
    ]

    def match_name(name: str, kws: list[str]) -> bool:
        return bool(name) and any(kw in name for kw in kws)

    for theme_label, kws in THEME_KEYWORDS:
        for src_label, rows, col in sources:
            for r in rows:
                name = r.get(col) or ""
                if not match_name(name, kws):
                    continue
                compare_rows.append({
                    "用户题材": theme_label,
                    "数据源": src_label,
                    "匹配名称": name,
                    "代码": r.get("ts_code") or r.get("code") or "",
                    "涨跌幅": r.get("pct_change") or r.get("pct_chg") or "",
                    "涨停数": r.get("z_t_num", ""),
                    "上涨家数": r.get("up_num") or r.get("up_nums", ""),
                    "其他": r.get("leading", "") or r.get("idx_type", "") or r.get("src", ""),
                })

    stats["keyword_compare"] = write_csv(
        OUT_DIR / f"theme_keyword_compare_{trade_date}.csv",
        compare_rows,
        ["用户题材", "数据源", "匹配名称", "代码", "涨跌幅", "涨停数", "上涨家数", "其他"],
    )

    # 板块涨跌统计摘要
    if dc:
        vals = [float(x["pct_change"]) for x in dc if x.get("pct_change") not in (None, "")]
        up = sum(1 for v in vals if v > 0)
        down = sum(1 for v in vals if v < 0)
        summary = [{
            "trade_date": trade_date,
            "dc_concept_count": len(vals),
            "up_count": up,
            "down_count": down,
            "avg_pct_change": round(sum(vals) / len(vals), 4) if vals else "",
        }]
        write_csv(OUT_DIR / f"dc_index_summary_{trade_date}.csv", summary)

    return stats


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    token = load_token()
    trade_date = resolve_trade_date(token, arg)
    print(f"交易日: {trade_date}")
    print(f"输出目录: {OUT_DIR.resolve()}\n")
    stats = export_all(trade_date)
    for k, n in stats.items():
        print(f"  {k}: {n} rows")
    print("\n完成。")


if __name__ == "__main__":
    main()
