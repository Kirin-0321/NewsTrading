"""Phase 5 快照重建回归测试（9 用例 · 2026-05-27 时间窗重构后）。

覆盖
----
1. 默认窗重建（不传 start/end，snapshot 自己用主人默认窗）
2. 穿越保护（monkey-patch _load_news_in_range 注入脏新闻）
3. news_end_dt 早于 trade_date 16:00 → market_date = 前一交易日
4. news_end_dt 晚于 trade_date 16:00 → market_date = trade_date 当天
5. 周末 trade_date（5/23）→ market_date 自动回退到 5/22 周五
6. 重建稳定性：同参数两次调用结果一致
7. **硬上限校验**：news_end_dt 超过 next_open 09:00 → 抛 SnapshotError
8. **默认窗 helper**：周五 5/22 应得 (5/22 14:00, 5/25 09:00)
9. **news_status 切换**：``curated`` vs ``all`` 数量对比

数据隔离
--------
* 用例 2 通过 monkey-patch 注入脏数据，不写真实表
* 其他用例只读真实数据；用例 9 写一条 rejected 测试数据，结尾清理

运行
----
::

    python tools/test_snapshot.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _seed_fake_news(
    news_id: str, published_ts: int, *, status: str = "curated",
) -> None:
    """插一条伪新闻（status 可控）。"""
    from services.storage.database import get_connection
    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO raw_news "
            "(id, title, content, source, published_at, published_ts, "
            " crawled_at, clean_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                news_id, f"伪新闻 {news_id}", "伪内容",
                "test_source", now_iso, published_ts, now_iso, status,
            ),
        )


def _cleanup_fake_news() -> None:
    """删所有 TEST_SNAP_* 前缀的伪新闻。"""
    from services.storage.database import get_connection
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM raw_news WHERE id LIKE 'TEST_SNAP_%'"
        )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_default_window() -> None:
    """默认窗（不传 start/end）：5/26 → [5/26 14:00, 5/27 09:00) 应有新闻。"""
    from services.scoring.snapshot import build_snapshot
    snap = build_snapshot("20260526")
    assert snap.news_count > 0, (
        f"默认窗应有新闻，得到 {snap.news_count}"
    )
    assert snap.market_summary_date == "20260526", (
        f"5/26 默认窗右=5/27 09:00 > 5/26 16:00，应取当天 5/26 盘后，"
        f"得到 {snap.market_summary_date}"
    )
    assert snap.market_summary_md is not None
    assert len(snap.market_summary_md) > 100
    # 默认窗的左边界必然是 5/26 14:00
    assert snap.news_start_dt == datetime(2026, 5, 26, 14, 0), (
        f"默认左应为 5/26 14:00，得到 {snap.news_start_dt}"
    )
    assert snap.news_end_dt == datetime(2026, 5, 27, 9, 0), (
        f"默认右应为 5/27 09:00，得到 {snap.news_end_dt}"
    )


def case_02_anti_lookahead_news() -> None:
    """注入 published_ts >= news_end_ts 的脏新闻 → 必抛 SnapshotError。"""
    from services.scoring.snapshot import SnapshotError, build_snapshot
    from services.scoring import snapshot as snap_mod

    orig_load = snap_mod._load_news_in_range

    def _fake_load(*, start_ts, end_ts, status):
        return [{
            "id": "TEST_SNAP_DIRTY",
            "title": "脏数据",
            "published_ts": end_ts + 100,
        }]
    snap_mod._load_news_in_range = _fake_load  # type: ignore
    try:
        build_snapshot("20260526")
    except SnapshotError as exc:
        assert "穿越" in str(exc), f"应提示穿越: {exc}"
        return
    finally:
        snap_mod._load_news_in_range = orig_load  # type: ignore
    raise AssertionError(
        "脏数据（published_ts >= news_end_ts）应抛 SnapshotError"
    )


def case_03_end_before_settle() -> None:
    """news_end_dt = 5/26 10:00（早盘前）→ market_date = 5/25。"""
    from services.scoring.snapshot import build_snapshot
    end_dt = datetime(2026, 5, 26, 10, 0)
    start_dt = datetime(2026, 5, 25, 14, 0)
    snap = build_snapshot(
        "20260526", news_start_dt=start_dt, news_end_dt=end_dt,
    )
    assert snap.market_summary_date == "20260525", (
        f"news_end_dt < 16:00 应取 5/25 盘后，"
        f"得到 {snap.market_summary_date}"
    )


def case_04_end_after_settle() -> None:
    """news_end_dt = 5/26 18:00（盘后）→ market_date = 5/26。"""
    from services.scoring.snapshot import build_snapshot
    end_dt = datetime(2026, 5, 26, 18, 0)
    start_dt = datetime(2026, 5, 26, 14, 0)
    snap = build_snapshot(
        "20260526", news_start_dt=start_dt, news_end_dt=end_dt,
    )
    assert snap.market_summary_date == "20260526", (
        f"news_end_dt >= 16:00 应取 5/26 当天，"
        f"得到 {snap.market_summary_date}"
    )


def case_05_weekend_no_crash() -> None:
    """5/23 周六默认窗 → market_date 自动回退到 5/22 周五。"""
    from services.scoring.snapshot import build_snapshot
    snap = build_snapshot("20260523")
    # 周末非开市，无论 news_end_dt 多晚都取 pretrade_date
    assert snap.market_summary_date == "20260522", (
        f"周六应取 5/22 周五盘后，得到 {snap.market_summary_date}"
    )


def case_06_stable_rebuild() -> None:
    """重建结果稳定：同参数两次调用 news_count + market_date 一致。"""
    from services.scoring.snapshot import build_snapshot
    s1 = build_snapshot("20260526")
    s2 = build_snapshot("20260526")
    assert s1.news_count == s2.news_count
    assert s1.market_summary_date == s2.market_summary_date
    assert (
        (s1.market_summary_md or "").strip()
        == (s2.market_summary_md or "").strip()
    )
    assert s1.news_end_ts == s2.news_end_ts
    ids1 = [n["id"] for n in s1.news[:5]]
    ids2 = [n["id"] for n in s2.news[:5]]
    assert ids1 == ids2, f"前 5 条新闻 id 不一致：{ids1} vs {ids2}"


def case_07_max_end_enforced() -> None:
    """news_end_dt 超过 next_open 09:00 硬上限 → SnapshotError。"""
    from services.scoring.snapshot import SnapshotError, build_snapshot
    # trade_date=5/22 周五；next_open=5/25 周一 09:00
    # 故意传 5/25 12:00 应抛错
    too_late = datetime(2026, 5, 25, 12, 0)
    try:
        build_snapshot(
            "20260522",
            news_end_dt=too_late,
        )
    except SnapshotError as exc:
        assert "硬上限" in str(exc) or "next_trade_date" in str(exc), (
            f"应提示硬上限: {exc}"
        )
        return
    raise AssertionError(
        "news_end_dt 超过 next_open 09:00 应抛 SnapshotError"
    )


def case_08_default_window_helper() -> None:
    """compute_default_news_window: 周五 5/22 应得 (5/22 14:00, 5/25 09:00)。"""
    from services.scoring.snapshot import compute_default_news_window
    start, end = compute_default_news_window("20260522")
    assert start == datetime(2026, 5, 22, 14, 0), (
        f"周五左应为 5/22 14:00，得到 {start}"
    )
    assert end == datetime(2026, 5, 25, 9, 0), (
        f"周五右应为 5/25 09:00（next_open），得到 {end}"
    )


def case_09_news_status_switch() -> None:
    """news_status 切换：curated vs all 数量不同。

    插一条 rejected 伪新闻 → curated 模式不应拉到，all 模式应拉到。
    """
    from services.scoring.snapshot import build_snapshot
    # 在 5/26 15:00 插一条 rejected
    rej_ts = int(datetime(2026, 5, 26, 15, 0).timestamp())
    _seed_fake_news("TEST_SNAP_REJ", rej_ts, status="rejected")

    end_dt = datetime(2026, 5, 26, 16, 0)
    start_dt = datetime(2026, 5, 26, 14, 0)

    snap_curated = build_snapshot(
        "20260526",
        news_start_dt=start_dt, news_end_dt=end_dt,
        news_status="curated",
    )
    snap_all = build_snapshot(
        "20260526",
        news_start_dt=start_dt, news_end_dt=end_dt,
        news_status="",
    )

    curated_ids = {n["id"] for n in snap_curated.news}
    all_ids = {n["id"] for n in snap_all.news}

    assert "TEST_SNAP_REJ" not in curated_ids, (
        "curated 模式不应拉到 rejected 伪新闻"
    )
    assert "TEST_SNAP_REJ" in all_ids, (
        "all 模式应拉到 rejected 伪新闻"
    )
    assert snap_all.news_count >= snap_curated.news_count + 1, (
        f"all({snap_all.news_count}) 应 >= curated({snap_curated.news_count}) + 1"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_default_window", case_01_default_window),
    ("02_anti_lookahead_news", case_02_anti_lookahead_news),
    ("03_end_before_settle", case_03_end_before_settle),
    ("04_end_after_settle", case_04_end_after_settle),
    ("05_weekend_no_crash", case_05_weekend_no_crash),
    ("06_stable_rebuild", case_06_stable_rebuild),
    ("07_max_end_enforced", case_07_max_end_enforced),
    ("08_default_window_helper", case_08_default_window_helper),
    ("09_news_status_switch", case_09_news_status_switch),
]


def main() -> int:
    print(
        f"=== Phase 5 快照重建回归 ({len(_CASES)} 用例) ==="
        f"  · 时间窗新语义\n"
    )
    try:
        for name, fn in _CASES:
            try:
                fn()
                print(f"[PASS] {name}")
            except AssertionError as exc:
                print(f"[FAIL] {name}: {exc}")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
                return 1
    finally:
        _cleanup_fake_news()
        print("\n[cleanup] 已删除所有 TEST_SNAP_* 伪新闻")
    print(f"\n[全部通过] {len(_CASES)}/{len(_CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
