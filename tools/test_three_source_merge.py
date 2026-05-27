"""涨停数据三源融合回归测试（Phase A6，10 用例，全部走真库 + 伪日期）。

施工方案：``doc/design/05-27-1722-涨停数据三源融合施工方案.md``。

覆盖范围
--------
1. parse_cons_nums("3连板") → 3   （kpl 主路径）
2. parse_cons_nums status="首板" → 1   （kpl 字面）
3. parse_cons_nums tag="3天3板" → 3   （ths 兜底）
4. parse_cons_nums tag="7天5板" → 5   （ths 间断梯队，本轮新增关键）
5. parse_cons_nums tag="12天10板" → 10   （极端情况）
6. parse_cons_nums tag="首板" → 1   （ths 字面首板）
7. parse_cons_nums 空字典 → 1   （兜底当首板）
8. build_limit_ladder 把 lu_desc / limit_up_suc_rate / tag 带入 stock dict
9. fetcher._merge_ths_into_limit_stock：INSERT OR IGNORE 给 d 漏掉的补底
   + UPDATE 合并 ths 独家字段（COALESCE 不覆盖已有值）
10. fetcher._ingest_limit_sprint：INSERT OR REPLACE 幂等，重复跑行数不变

测试隔离
--------
* 所有用例的 trade_date 用 ``99990520`` 等伪日期前缀 ``9999``，结尾自动清理。
* MockTushareClient 不耗 Tushare 配额。

运行::

    python tools/test_three_source_merge.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mock TushareClient（仅 case 10 用到，其他用例直接 import 函数）
# ---------------------------------------------------------------------------


class MockTushareClient:
    """模拟 ``TushareClient.call``，按预制 dict 应答。"""

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.responses = responses or {}
        self.call_count = 0

    def call(
        self,
        api_name: str,
        params: Optional[dict] = None,
        *,
        fields: str = "",
        **_kwargs,
    ) -> List[Dict[str, Any]]:
        self.call_count += 1
        resp = self.responses.get(api_name)
        if resp is None:
            return []
        if callable(resp):
            return resp(params or {})
        return list(resp)


# ---------------------------------------------------------------------------
# 测试隔离辅助
# ---------------------------------------------------------------------------


def _cleanup_fake_data() -> None:
    """清除所有以 ``9999`` 开头的伪测试数据。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM fact_limit_stock WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_limit_sprint WHERE trade_date LIKE '9999%'"
        )


# ---------------------------------------------------------------------------
# 用例 1~7: parse_cons_nums 多形态解析
# ---------------------------------------------------------------------------


def case_01_parse_kpl_status_3lianban() -> None:
    """parse_cons_nums status='3连板' → 3 (kpl 主路径)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"status": "3连板"})
    assert got == 3, f"期望 3, 得到 {got}"


def case_02_parse_kpl_status_shouban() -> None:
    """parse_cons_nums status='首板' → 1 (kpl 字面)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"status": "首板"})
    assert got == 1, f"期望 1, 得到 {got}"


def case_03_parse_ths_tag_3tian3ban() -> None:
    """parse_cons_nums tag='3天3板' → 3 (ths 兜底)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"tag": "3天3板"})
    assert got == 3, f"期望 3, 得到 {got}"


def case_04_parse_ths_tag_7tian5ban() -> None:
    """parse_cons_nums tag='7天5板' → 5 (ths 间断梯队，关键用例)。

    7 个交易日内 5 个涨停，cons_nums 应取板数 5 而非天数 7。
    """
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"tag": "7天5板"})
    assert got == 5, f"期望 5（板数）, 得到 {got}"


def case_05_parse_ths_tag_extreme() -> None:
    """parse_cons_nums tag='12天10板' → 10 (极端情况)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"tag": "12天10板"})
    assert got == 10, f"期望 10, 得到 {got}"


def case_06_parse_ths_tag_shouban() -> None:
    """parse_cons_nums tag='首板' → 1 (ths 字面首板)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({"tag": "首板"})
    assert got == 1, f"期望 1, 得到 {got}"


def case_07_parse_empty_dict_default_1() -> None:
    """parse_cons_nums 空字典 → 1 (兜底当首板)。"""
    from services.market.metrics import parse_cons_nums
    got = parse_cons_nums({})
    assert got == 1, f"期望兜底 1, 得到 {got}"


# ---------------------------------------------------------------------------
# 用例 8: build_limit_ladder 字段透传
# ---------------------------------------------------------------------------


def case_08_ladder_carries_new_fields() -> None:
    """build_limit_ladder 应把 lu_desc / limit_up_suc_rate / tag 透传到 stock dict。"""
    from services.market.metrics import build_limit_ladder
    rows = [
        {
            "ts_code": "000001.SZ",
            "name": "TEST 三板股",
            "theme": "题材A",
            "status": "3连板",
            "lu_desc": "涨停催化剂A+B+C",
            "limit_up_suc_rate": 0.85,
            "tag": "3天3板",
            "lu_time": "10:00:00",
        },
        {
            "ts_code": "000002.SZ",
            "name": "TEST 首板股",
            "theme": "题材B",
            "status": "首板",
            "lu_desc": "首板催化剂X",
            "limit_up_suc_rate": 0.50,
            "tag": "首板",
            "lu_time": "11:00:00",
        },
    ]
    ladder = build_limit_ladder(rows)
    # 3 连板桶
    assert "3连板" in ladder, "桶字典必须含 '3连板'"
    bucket3 = ladder["3连板"]
    assert len(bucket3) == 1, f"3 连板桶应有 1 只, 得到 {len(bucket3)}"
    s = bucket3[0]
    assert s["lu_desc"] == "涨停催化剂A+B+C", (
        f"lu_desc 透传失败: {s['lu_desc']!r}"
    )
    assert s["limit_up_suc_rate"] == 0.85, (
        f"封板率透传失败: {s['limit_up_suc_rate']!r}"
    )
    assert s["tag"] == "3天3板", f"tag 透传失败: {s['tag']!r}"
    # 首板桶
    bucket1 = ladder["首板"]
    assert len(bucket1) == 1
    assert bucket1[0]["lu_desc"] == "首板催化剂X"


# ---------------------------------------------------------------------------
# 用例 9: fetcher._merge_ths_into_limit_stock 三源融合
# ---------------------------------------------------------------------------


def case_09_merge_ths_insert_and_update() -> None:
    """fetcher 的 ths 合并逻辑：
    * 已有 d 行（ths_code 相同）→ UPDATE 合并 ths 独家字段
    * d 漏掉的 ts_code → INSERT OR IGNORE 创建底
    * COALESCE 不覆盖已有的 industry / total_mv / theme（来自 d/kpl）
    """
    from services.market.market_db import get_market_db
    from services.market.tushare_client import TushareClient
    from services.market.tushare_fetcher import TushareMarketFetcher

    td = "99990520"
    db = get_market_db()
    # 先模拟 d 已经写了 1 行（招金黄金，industry='贵金属'）
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fact_limit_stock "
            "(trade_date, ts_code, limit_type, close, pct_chg, "
            " industry, total_mv, source) "
            "VALUES (?, 'TEST001.SZ', 'U', 15.4, 10.0, "
            " '贵金属', 14306873504.0, 'd')",
            (td,),
        )
    fetcher = TushareMarketFetcher(client=TushareClient(token="dummy"), db=db)
    ths_rows = [
        # 已有 d 行：UPDATE 合并字段
        {
            "ts_code": "TEST001.SZ",
            "name": "招金黄金",
            "price": 15.4,
            "pct_chg": 10.0,
            "lu_desc": "黄金+业绩暴增+国企",
            "limit_up_suc_rate": 0.6875,
            "market_type": "HS",
            "tag": "首板",
            "free_float": 14302087000.0,
        },
        # d 漏的：INSERT OR IGNORE 补底
        {
            "ts_code": "TEST002.SZ",
            "name": "*ST皇庭",
            "price": 3.5,
            "pct_chg": 5.0,
            "lu_desc": "退市博弈+债务化解+功率半导体",
            "limit_up_suc_rate": 0.6364,
            "market_type": "HS",
            "tag": "2天2板",
            "free_float": 1e9,
        },
    ]
    cnt, upd = fetcher._merge_ths_into_limit_stock(ths_rows, td, "U")
    assert cnt == 2, f"涨停池行数应为 2 (含补底), 得到 {cnt}"
    assert upd == 2, f"UPDATE 应处理 2 行, 得到 {upd}"

    # 验数据
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts_code, lu_desc, limit_up_suc_rate, market_type, tag, "
            "       industry, total_mv, source "
            "FROM fact_limit_stock "
            "WHERE trade_date = ? AND limit_type = 'U' "
            "ORDER BY ts_code",
            (td,),
        ).fetchall()
    assert len(rows) == 2
    # 招金黄金（d+ths 合并）
    r1 = dict(rows[0])
    assert r1["ts_code"] == "TEST001.SZ"
    assert r1["lu_desc"] == "黄金+业绩暴增+国企"
    assert r1["industry"] == "贵金属", (
        f"d 已有 industry 不应被 ths 覆盖, 得到 {r1['industry']!r}"
    )
    assert r1["source"] == "d+ths", (
        f"source 应为 'd+ths', 得到 {r1['source']!r}"
    )
    # *ST皇庭（仅 ths）
    r2 = dict(rows[1])
    assert r2["ts_code"] == "TEST002.SZ"
    assert r2["lu_desc"] == "退市博弈+债务化解+功率半导体"
    assert r2["industry"] is None
    assert r2["source"] == "ths"


# ---------------------------------------------------------------------------
# 用例 10: fact_limit_sprint INSERT OR REPLACE 幂等
# ---------------------------------------------------------------------------


def case_10_sprint_insert_replace_idempotent() -> None:
    """``_ingest_limit_sprint`` 重复跑同一天数据，行数应保持不变。"""
    from services.market.market_db import get_market_db
    from services.market.tushare_client import TushareClient
    from services.market.tushare_fetcher import TushareMarketFetcher

    td = "99990521"
    db = get_market_db()
    fetcher = TushareMarketFetcher(client=TushareClient(token="dummy"), db=db)
    sprint_rows = [
        {
            "ts_code": "TEST_SP01.SH",
            "name": "上纬新材",
            "price": 221.0,
            "pct_chg": 18.18,
            "rise_rate": 0.5,
            "turnover_rate": 3.22,
            "turnover": 27.29 * 1e8,
            "free_float": 891.43 * 1e8,
            "lu_desc": "STAR板新材料",
            "market_type": "STAR",
        },
        {
            "ts_code": "TEST_SP02.SZ",
            "name": "线上线下",
            "price": 218.99,
            "pct_chg": 17.37,
            "rise_rate": 0.3,
            "turnover_rate": 23.17,
            "turnover": 26.74 * 1e8,
            "free_float": 114.59 * 1e8,
            "lu_desc": None,
            "market_type": "GEM",
        },
    ]
    n1 = fetcher._ingest_limit_sprint(sprint_rows, td)
    assert n1 == 2, f"首次入库期望 2 行, 得到 {n1}"

    # 重复跑同一份数据
    n2 = fetcher._ingest_limit_sprint(sprint_rows, td)
    assert n2 == 2, f"重复跑期望 2 行（幂等）, 得到 {n2}"

    # 验库行数仍为 2
    with db.connect(readonly=True) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fact_limit_sprint WHERE trade_date = ?",
            (td,),
        ).fetchone()[0]
    assert cnt == 2, f"DB 行数应为 2, 得到 {cnt}"

    # 字段验证
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts_code, name, market_type, pct_chg "
            "FROM fact_limit_sprint WHERE trade_date = ? "
            "ORDER BY pct_chg DESC",
            (td,),
        ).fetchall()
    assert rows[0]["ts_code"] == "TEST_SP01.SH"
    assert rows[0]["market_type"] == "STAR"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_parse_kpl_status_3lianban", case_01_parse_kpl_status_3lianban),
    ("02_parse_kpl_status_shouban", case_02_parse_kpl_status_shouban),
    ("03_parse_ths_tag_3tian3ban", case_03_parse_ths_tag_3tian3ban),
    ("04_parse_ths_tag_7tian5ban", case_04_parse_ths_tag_7tian5ban),
    ("05_parse_ths_tag_extreme", case_05_parse_ths_tag_extreme),
    ("06_parse_ths_tag_shouban", case_06_parse_ths_tag_shouban),
    ("07_parse_empty_dict_default_1", case_07_parse_empty_dict_default_1),
    ("08_ladder_carries_new_fields", case_08_ladder_carries_new_fields),
    ("09_merge_ths_insert_and_update", case_09_merge_ths_insert_and_update),
    ("10_sprint_insert_replace_idempotent", case_10_sprint_insert_replace_idempotent),
]


def main() -> int:
    print(f"=== 三源融合回归 ({len(_CASES)} 用例) ===\n")
    passed: List[str] = []
    try:
        for name, fn in _CASES:
            try:
                fn()
                print(f"[PASS] {name}")
                passed.append(name)
            except AssertionError as exc:
                print(f"[FAIL] {name}: {exc}")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"[ERR ] {name}: {type(exc).__name__}: {exc}")
                return 1
        print(f"\n[全部通过] {len(passed)}/{len(_CASES)}")
        return 0
    finally:
        _cleanup_fake_data()


if __name__ == "__main__":
    sys.exit(main())
