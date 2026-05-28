"""query_all_stocks / query_all_sectors 回归测试（7 用例，走真实 market.db）。

覆盖范围
--------
1. query_all_stocks 基本功能：返回行数 >= 5000，字段齐全
2. query_all_stocks 默认按涨跌幅倒序
3. query_all_stocks 涨停标 limit_type 与 fact_limit_stock 一致
4. query_all_sectors 基本功能：返回行数 >= 400，rank 从 1 计且单调递增
5. 空 trade_date / 不存在的 trade_date 返回 [] 而非抛错
6. **v2** query_all_sectors(idx_type=...) 单源筛选行数与 counts 一致
7. **v2** query_sector_idx_type_counts 5 类合计 = 不筛选时总行数

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
    query_sector_idx_type_counts,
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


def case_06_sectors_idx_type_filter() -> None:
    """case_06: query_all_sectors(idx_type=...) 单源筛选与 counts 一致。"""
    td = _latest_trade_date()
    counts = query_sector_idx_type_counts(td)
    assert counts, f"counts 为空，td={td}"
    # 至少 dc 概念 + 行业板块 + ths 概念三类应同时存在（多源改造后）
    must_have = ("概念板块", "行业板块", "同花顺概念")
    for it in must_have:
        assert it in counts, (
            f"counts 缺 {it}，5 源数据可能未补齐: {counts}"
        )

    # 抽样验证 3 个 idx_type
    for idx_type in must_have:
        rows = query_all_sectors(td, idx_type=idx_type)
        assert len(rows) == counts[idx_type], (
            f"{idx_type} query 行数 {len(rows)} ≠ counts {counts[idx_type]}"
        )

    # 不传 idx_type → 全量 = 各 counts 之和
    rows_all = query_all_sectors(td)
    assert len(rows_all) == sum(counts.values()), (
        f"全量 {len(rows_all)} ≠ counts 合计 {sum(counts.values())}"
    )
    print(f"  ✅ 5 源筛选自洽 td={td}: {counts}")


def case_07_idx_type_counts_self_consistent() -> None:
    """case_07: counts 自洽性——空日期返回 {}，错误日期返回 {}。"""
    assert query_sector_idx_type_counts("") == {}
    assert query_sector_idx_type_counts("19000101") == {}

    td = _latest_trade_date()
    counts = query_sector_idx_type_counts(td)
    assert all(isinstance(v, int) and v > 0 for v in counts.values()), (
        f"counts 应全为正整数: {counts}"
    )
    # idx_type 取值应在已知白名单里
    allowed = {
        "概念板块", "行业板块", "地域板块",
        "同花顺行业", "同花顺概念",
    }
    extra = set(counts.keys()) - allowed
    assert not extra, f"出现未知 idx_type: {extra}"
    print(f"  ✅ counts 字段合法 {counts}")


def case_08_grouped_count_consistent() -> None:
    """case_08: query_grouped_sectors_count 与 query_grouped_sectors_tree 行数一致。"""
    from gui.utils.market_db_helper import (
        query_grouped_sectors_count,
        query_grouped_sectors_tree,
    )

    td = _latest_trade_date()
    n = query_grouped_sectors_count(td)
    rows = query_grouped_sectors_tree(td)
    assert n == len(rows), (
        f"count={n} 与 tree 行数 {len(rows)} 不一致"
    )
    # 空 / 无效输入
    assert query_grouped_sectors_count("") == 0
    assert query_grouped_sectors_tree("") == []
    print(f"  ✅ grouped count = {n}, tree rows = {len(rows)} 一致")


def case_09_grouped_lower_median() -> None:
    """case_09: 树形输出的 median 取值符合下中位定义（ASC 排序后 (n+1)/2 项）。"""
    from gui.utils.market_db_helper import query_grouped_sectors_tree

    td = _latest_trade_date()
    rows = query_grouped_sectors_tree(td)
    assert rows, "聚类视图无数据，前置 cluster apply 是否跑过？"

    multi = [g for g in rows if g["cnt_in_data"] >= 2]
    assert multi, "找不到任何 ≥2 成员组，无法验证中位算法"

    # 抽查 5 个多成员组
    for g in multi[:5]:
        members = g["members"]
        # 按 ASC 重排（query_grouped_sectors_tree 中 members 是 DESC 排序的）
        sorted_asc = sorted(
            members,
            key=lambda m: (
                m["pct_chg"] if m["pct_chg"] is not None else float("inf"),
                m["ts_code"],
            ),
        )
        n = len(sorted_asc)
        expected = sorted_asc[(n + 1) // 2 - 1]
        assert expected["ts_code"] == g["median_ts_code"], (
            f"组 {g['display_name']}: 期望 median={expected['ts_code']}"
            f"，实际={g['median_ts_code']}"
        )
        # is_median 标记唯一
        med_count = sum(1 for m in members if m.get("is_median"))
        assert med_count == 1, (
            f"组 {g['display_name']}: is_median 标记数 = {med_count}（应为 1）"
        )
    print(f"  ✅ 抽查 {min(5, len(multi))} 个多成员组，中位算法正确")


def case_10b_sectors_top_bottom_clustered() -> None:
    """case_10b: build() 走聚类版后 sectors_top=Top 30 / Bottom 15
    且字段含 group_name / members_count / sector_name。"""
    from services.market.service import MarketSummaryService

    s = MarketSummaryService()
    td = _latest_trade_date()
    top = s._read_sectors_top(td, 30, [])
    bot = s._read_sectors_bottom(td, 15, [])

    assert len(top) == 30, f"sectors_top 应为 30 行，实际 {len(top)}"
    assert len(bot) == 15, f"sectors_bottom 应为 15 行，实际 {len(bot)}"

    # 必含 v3 新字段
    sample = top[0]
    for k in ("group_name", "members_count", "sector_name", "name"):
        assert k in sample, f"sectors_top 缺字段 {k}"

    # 至少有一个多成员组进 Top 30（聚类应该有效）
    multi = [r for r in top if (r.get("members_count") or 1) > 1]
    assert multi, "Top 30 居然 0 个多成员组，聚类没生效？"

    # name 字段应等于 group_name 优先（聚类组）
    for r in multi:
        assert r["name"] == r["group_name"], (
            f"多成员组 name={r['name']} 应等于 group_name={r['group_name']}"
        )

    sample_str = ", ".join(
        f"{r['name']}×{r['members_count']}" for r in multi[:5]
    )
    print(
        f"  ✅ Top 30 / Bottom 15，含 {len(multi)} 个多成员组：{sample_str}"
    )


def case_10_grouped_unclassified_self_group() -> None:
    """case_10: 未聚类板块（group_id IS NULL）应每个自成一组。"""
    from gui.utils.market_db_helper import query_grouped_sectors_tree
    from services.market.market_db import get_market_db

    td = _latest_trade_date()
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        unclassified_in_data = conn.execute(
            "SELECT COUNT(DISTINCT s.ts_code) "
            "FROM fact_sector_daily s "
            "LEFT JOIN dim_sector d ON d.ts_code = s.ts_code "
            "WHERE s.trade_date = ? AND d.group_id IS NULL",
            (td,),
        ).fetchone()[0]

    rows = query_grouped_sectors_tree(td)
    unclassified_groups = [g for g in rows if g["group_name"] is None]
    assert len(unclassified_groups) == int(unclassified_in_data), (
        f"未聚类组数 {len(unclassified_groups)} ≠ "
        f"未聚类板块数 {unclassified_in_data}"
    )
    # 每个未聚类组只有一个成员
    for g in unclassified_groups:
        assert g["cnt_in_data"] == 1, (
            f"未聚类组 {g['display_name']} 应只有 1 成员，"
            f"实际 {g['cnt_in_data']}"
        )
    print(
        f"  ✅ 未聚类自成一组：{len(unclassified_groups)} 个一一对应"
    )


CASES = [
    ("case_01_stocks_basic", case_01_stocks_basic),
    ("case_02_stocks_sorted_by_pct", case_02_stocks_sorted_by_pct),
    ("case_03_stocks_limit_type", case_03_stocks_limit_type),
    ("case_04_sectors_basic", case_04_sectors_basic),
    ("case_05_empty_or_invalid", case_05_empty_or_invalid),
    ("case_06_sectors_idx_type_filter", case_06_sectors_idx_type_filter),
    ("case_07_idx_type_counts_self_consistent",
     case_07_idx_type_counts_self_consistent),
    ("case_08_grouped_count_consistent", case_08_grouped_count_consistent),
    ("case_09_grouped_lower_median", case_09_grouped_lower_median),
    ("case_10_grouped_unclassified_self_group",
     case_10_grouped_unclassified_self_group),
    ("case_10b_sectors_top_bottom_clustered",
     case_10b_sectors_top_bottom_clustered),
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
