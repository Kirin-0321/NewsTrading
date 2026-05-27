"""query_all_stocks / query_all_sectors 回归测试（5 用例，走真实 market.db）。

覆盖范围
--------
1. query_all_stocks 基本功能：返回行数 >= 5000，字段齐全
2. query_all_stocks 默认按涨跌幅倒序
3. query_all_stocks 涨停标 limit_type 与 fact_limit_stock 一致
4. query_all_sectors 基本功能：返回行数 >= 400，rank 从 1 计且单调递增
5. 空 trade_date / 不存在的 trade_date 返回 [] 而非抛错

运行::

    python tools/test_query_all.py

成功输出 [全部通过]，失败抛 AssertionError 并 exit(1)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from gui.utils.market_db_helper import (  # noqa: E402
    query_all_stocks,
    query_all_sectors,
)
from services.market.market_db import get_market_db  # noqa: E402


def _latest_trade_date() -> str:
    """取 fact_stock_daily 最新交易日（测试用例锚点）。"""
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM fact_stock_daily"
        ).fetchone()
    return str(row[0]) if row and row[0] else ""


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_stocks_basic() -> None:
    """case_01: query_all_stocks 基本功能（行数 + 字段齐全）。"""
    td = _latest_trade_date()
    assert td, "fact_stock_daily 为空，用例无法执行"
    rows = query_all_stocks(td)
    assert len(rows) >= 5000, f"全 A 股应 >= 5000 行，得到 {len(rows)}"

    required_keys = {
        "ts_code", "name", "industry", "market",
        "pct_chg", "close", "amount_yi", "limit_type",
    }
    sample = rows[0]
    missing = required_keys - set(sample.keys())
    assert not missing, f"字段缺失 {missing}"
    print(f"  ✅ td={td}  rows={len(rows)}  字段齐")


def case_02_stocks_sorted_by_pct() -> None:
    """case_02: query_all_stocks 默认按 pct_chg DESC 排序。"""
    td = _latest_trade_date()
    rows = query_all_stocks(td)
    pcts = [r["pct_chg"] for r in rows if r["pct_chg"] is not None]
    for i in range(len(pcts) - 1):
        assert pcts[i] >= pcts[i + 1], (
            f"涨跌幅排序错误: idx={i} {pcts[i]} < {pcts[i+1]}"
        )
    print(f"  ✅ {len(pcts)} 行按涨跌幅倒序")


def case_03_stocks_limit_type() -> None:
    """case_03: limit_type 与 fact_limit_stock 一致。"""
    td = _latest_trade_date()
    rows = query_all_stocks(td)
    by_type = {"U": 0, "Z": 0, "D": 0}
    for r in rows:
        lt = r.get("limit_type")
        if lt in by_type:
            by_type[lt] += 1

    db = get_market_db()
    with db.connect(readonly=True) as conn:
        truth = {}
        for lt in ("U", "Z", "D"):
            row = conn.execute(
                "SELECT COUNT(*) FROM fact_limit_stock "
                "WHERE trade_date=? AND limit_type=?",
                (td, lt),
            ).fetchone()
            truth[lt] = int(row[0]) if row else 0

    for lt in ("U", "Z", "D"):
        assert by_type[lt] == truth[lt], (
            f"{lt}: helper={by_type[lt]} vs db={truth[lt]}"
        )
    print(f"  ✅ U={by_type['U']} Z={by_type['Z']} D={by_type['D']}")


def case_04_sectors_basic() -> None:
    """case_04: query_all_sectors 行数 + rank 单调。"""
    td = _latest_trade_date()
    rows = query_all_sectors(td)
    assert len(rows) >= 400, f"全部板块应 >= 400 行，得到 {len(rows)}"

    for i, r in enumerate(rows, 1):
        assert r["rank"] == i, f"rank 不连续: idx={i} got={r['rank']}"

    pcts = [r["pct_chg"] for r in rows if r["pct_chg"] is not None]
    for i in range(len(pcts) - 1):
        assert pcts[i] >= pcts[i + 1], (
            f"板块涨跌幅排序错误: idx={i}"
        )
    print(f"  ✅ {len(rows)} 个板块，rank 1~{len(rows)} 连续，pct 倒序")


def case_05_empty_or_invalid() -> None:
    """case_05: 空 / 不存在 / 无效 trade_date 返回 [] 不抛错。"""
    assert query_all_stocks("") == [], "空 trade_date 应返回 []"
    assert query_all_stocks(None) == [], (  # type: ignore[arg-type]
        "None trade_date 应返回 []"
    )
    assert query_all_stocks("19000101") == [], "不存在交易日应返回 []"
    assert query_all_sectors("") == []
    assert query_all_sectors("19000101") == []
    print("  ✅ 边界输入全部安全返回 []")


CASES = [
    ("case_01_stocks_basic", case_01_stocks_basic),
    ("case_02_stocks_sorted_by_pct", case_02_stocks_sorted_by_pct),
    ("case_03_stocks_limit_type", case_03_stocks_limit_type),
    ("case_04_sectors_basic", case_04_sectors_basic),
    ("case_05_empty_or_invalid", case_05_empty_or_invalid),
]


def main() -> int:
    print(f"=== test_query_all ({len(CASES)} 用例) ===\n")
    failed: list[tuple[str, str]] = []
    for name, fn in CASES:
        print(f"[{name}]")
        try:
            fn()
        except AssertionError as exc:
            print(f"  ❌ {exc}")
            failed.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"  💥 {type(exc).__name__}: {exc}")
            failed.append((name, f"{type(exc).__name__}: {exc}"))

    print()
    if failed:
        print(f"=== 失败 {len(failed)}/{len(CASES)} ===")
        for n, e in failed:
            print(f"  - {n}: {e}")
        return 1
    print(f"=== 全部通过 {len(CASES)}/{len(CASES)} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
