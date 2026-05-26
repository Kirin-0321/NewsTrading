"""Phase M1a 验收测试 —— market.db 与 ai_reports 索引表。

覆盖项：

1. ``MarketDB.ensure_schema()`` 在空目录里能跑通 001 + 002 两个迁移
2. 14 张业务表全部建出
3. ``dim_trader_alias`` 种子条数 ≥ 30 且包含拉萨/章盟主等知名席位
4. ``schema_migrations`` 记录了 2 个版本号
5. 二次调用 ``ensure_schema()`` 不重复跑（幂等）
6. ``backup_to()`` 能正常输出文件
7. ``verify_integrity()`` 返回 ok
8. ``attach_to()`` 跨库 JOIN 能跑通
9. ``services/storage`` 的 ``ai_reports`` 表建出
10. ``AIReportsStore`` CRUD 能跑通（写 / 查 / 标记 / 删）

用法::

    python tools/test_market_db.py

成功退出码 0，任何断言失败退出码 1。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services.market.market_db import MarketDB  # noqa: E402


EXPECTED_TABLES = {
    # 维度
    "dim_trade_calendar",
    "dim_sector",
    "dim_stock",
    "dim_trader_alias",
    # 事实
    "fact_index_daily",
    "fact_sector_daily",
    "fact_limit_stock",
    "fact_hsgt_daily",
    "fact_top_list",
    "fact_top_inst",
    "fact_cls_stock_shock",
    "fact_cls_market_shock",
    # 汇总
    "market_summaries",
    "ai_enrich_patches",
    # 元数据
    "schema_migrations",
}

KNOWN_ALIAS_SAMPLES = ["章盟主", "赵老哥", "拉萨金珠西", "作手新一", "机构专用"]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


_failures: list[str] = []


def _ok(msg: str) -> None:
    print(f"[OK]   {msg}")


def _fail(msg: str) -> None:
    _failures.append(msg)
    print(f"[FAIL] {msg}")


def _check(condition: bool, msg: str) -> None:
    if condition:
        _ok(msg)
    else:
        _fail(msg)


# ---------------------------------------------------------------------------
# market.db 测试
# ---------------------------------------------------------------------------


def test_market_db_in_tmp(tmp_dir: Path) -> None:
    print("\n===== test_market_db_in_tmp =====")
    db_path = tmp_dir / "market.db"
    backup_dir = tmp_dir / "backups"
    db = MarketDB(db_path=db_path, backup_dir=backup_dir)

    # 1) 首次 ensure_schema：应应用 migrations/ 下所有迁移
    expected_versions = sorted(
        int(p.name.split("_", 1)[0])
        for p in (_ROOT / "services" / "market" / "migrations").glob("*.sql")
    )
    applied = db.ensure_schema(do_backup=False)
    _check(
        applied == expected_versions,
        f"首次 ensure_schema 应用 {len(expected_versions)} 个迁移，实际: {applied}",
    )

    # 2) 14 张表全部存在
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {r[0] for r in rows}
    missing = EXPECTED_TABLES - tables
    extra = tables - EXPECTED_TABLES
    _check(not missing, f"缺失表: {missing}")
    if extra:
        _ok(f"额外的表（允许存在）: {extra}")
    _check(
        len(tables) >= 15,
        f"业务表 + 元数据 ≥ 15，实际 {len(tables)}",
    )

    # 3) 种子游资 ≥ 30
    with db.connect(readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM dim_trader_alias").fetchone()[0]
        famous_n = conn.execute(
            "SELECT COUNT(*) FROM dim_trader_alias WHERE is_famous = 1"
        ).fetchone()[0]
        alias_set = {
            r[0]
            for r in conn.execute(
                "SELECT alias FROM dim_trader_alias"
            ).fetchall()
        }
        exalter_set = {
            r[0]
            for r in conn.execute(
                "SELECT exalter FROM dim_trader_alias"
            ).fetchall()
        }
    _check(n >= 30, f"游资种子条数应 ≥ 30，实际 {n}")
    _check(famous_n >= 25, f"知名游资 (is_famous=1) ≥ 25，实际 {famous_n}")

    sample_ok = 0
    for sample in KNOWN_ALIAS_SAMPLES:
        hit_alias = any(sample in a for a in alias_set)
        hit_exalter = any(sample in e for e in exalter_set)
        if hit_alias or hit_exalter:
            sample_ok += 1
        else:
            _fail(f"种子未命中关键字: {sample}")
    if sample_ok == len(KNOWN_ALIAS_SAMPLES):
        _ok(f"种子样本全部命中（{sample_ok}/{len(KNOWN_ALIAS_SAMPLES)}）")

    # 4) schema_migrations 与 migrations/*.sql 数量一致
    with db.connect(readonly=True) as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    _check(
        versions == expected_versions,
        f"schema_migrations 应为 {expected_versions}，实际 {versions}",
    )

    # 5) 幂等：再跑一次 ensure_schema 应返回空列表
    applied2 = db.ensure_schema(do_backup=False)
    _check(
        applied2 == [],
        f"重复 ensure_schema 应返回 [] (幂等)，实际 {applied2}",
    )

    # 6) backup_to
    backup_target = tmp_dir / "manual.bak"
    db.backup_to(backup_target)
    _check(
        backup_target.exists() and backup_target.stat().st_size > 0,
        f"backup_to 输出文件 {backup_target.name} 应存在且非空",
    )

    # 备份文件本身也应是合法的 SQLite 库（可读取）
    conn = sqlite3.connect(backup_target)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM dim_trader_alias"
        ).fetchone()
        _check(
            rows[0] == n,
            f"备份文件中 dim_trader_alias 应等于原库 ({n} 条)",
        )
    finally:
        conn.close()

    # 7) verify_integrity
    ok, msg = db.verify_integrity()
    _check(ok, f"verify_integrity 应返回 ok，实际 {ok} / {msg}")

    # 8) attach_to 跨库 JOIN：在临时 news 库上挂 market 库 JOIN
    news_path = tmp_dir / "news.db"
    news_conn = sqlite3.connect(news_path)
    try:
        news_conn.execute(
            "CREATE TABLE raw_news (id TEXT PRIMARY KEY, title TEXT, "
            "published_at TEXT)"
        )
        news_conn.execute(
            "INSERT INTO raw_news VALUES ('n1', '芯片板块涨停', '20260525')"
        )
        news_conn.commit()
        db.attach_to(news_conn, alias="m")
        rows = news_conn.execute(
            "SELECT n.id, n.title, "
            "(SELECT COUNT(*) FROM m.dim_trader_alias) AS alias_cnt "
            "FROM raw_news n"
        ).fetchall()
        _check(
            rows and rows[0][2] == n,
            f"ATTACH 跨库 JOIN：alias_cnt 应 = {n}，实际 {rows}",
        )
    finally:
        news_conn.close()

    # 9) get_stats 跑通
    stats = db.get_stats()
    _check(
        "tables" in stats and stats["tables"].get("dim_trader_alias") == n,
        "get_stats 返回 tables 计数正确",
    )


# ---------------------------------------------------------------------------
# ai_reports 表测试
# ---------------------------------------------------------------------------


def test_ai_reports_store(tmp_dir: Path) -> None:
    """在隔离的 news.db 路径下测试 AIReportsStore CRUD。"""
    print("\n===== test_ai_reports_store =====")

    iso_news_db = tmp_dir / "news_iso.db"

    # 用 monkeypatch 风格切换 get_db_path 指向临时库
    from services.storage import database as db_mod

    original_path = db_mod.get_db_path()
    db_mod.get_db_path = lambda: str(iso_news_db)  # type: ignore[assignment]
    try:
        from services.storage.ai_reports_store import (
            AIReportRecord,
            AIReportsStore,
        )

        db_mod.init_database(str(iso_news_db))
        store = AIReportsStore()

        # 验证 ai_reports 表存在
        conn = sqlite3.connect(iso_news_db)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='ai_reports'"
            ).fetchone()
        finally:
            conn.close()
        _check(
            cur is not None and cur[0] == "ai_reports",
            "news.db 新增 ai_reports 表存在",
        )

        # 验证表索引
        conn = sqlite3.connect(iso_news_db)
        try:
            idx_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='ai_reports'"
            ).fetchall()
        finally:
            conn.close()
        idx_names = {r[0] for r in idx_rows}
        wanted = {
            "idx_ai_reports_date",
            "idx_ai_reports_prompt",
            "idx_ai_reports_theme_flag",
        }
        _check(
            wanted.issubset(idx_names),
            f"ai_reports 三个索引应都建出，实际索引: {idx_names}",
        )

        # 写一条记录（虚构 file_path，不强求文件存在 → size/md5 为 None）
        rec = AIReportRecord(
            report_date="20260525",
            file_path="data/AI_analysis/test_20260525_AI分析.md",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_category="analysis",
            prompt_id="news_focused",
            prompt_version="1.2",
            news_range_start="2026-05-25 09:00",
            news_range_end="2026-05-25 16:30",
            news_count=120,
            used_market_date="20260525",
            theme_extracted=0,
        )
        new_id = store.record(rec)
        _check(new_id > 0, f"record 返回 id > 0，实际 {new_id}")

        got = store.get_by_path(rec.file_path)
        _check(
            got is not None
            and got.prompt_id == "news_focused"
            and got.news_count == 120
            and got.theme_extracted == 0,
            "get_by_path 返回正确记录",
        )

        # ON CONFLICT 覆盖：同 file_path 再写一遍，news_count 改成 999
        rec2 = AIReportRecord(
            report_date="20260525",
            file_path=rec.file_path,
            provider="qwen",
            model="qwen-max",
            prompt_category="analysis",
            prompt_id="trinity_resonance",
            prompt_version="1.0",
            news_count=999,
        )
        store.record(rec2)
        got2 = store.get_by_path(rec.file_path)
        _check(
            got2 is not None
            and got2.provider == "qwen"
            and got2.news_count == 999,
            "ON CONFLICT 同 file_path 应覆盖原记录",
        )
        _check(
            store.count() == 1,
            f"覆盖后总数应仍为 1，实际 {store.count()}",
        )

        # mark_theme_extracted
        ok = store.mark_theme_extracted(rec.file_path, True)
        got3 = store.get_by_path(rec.file_path)
        _check(
            ok and got3 is not None and got3.theme_extracted == 1,
            "mark_theme_extracted=True 后字段应为 1",
        )

        # list_by_date
        rec3 = AIReportRecord(
            report_date="20260524",
            file_path="data/AI_analysis/old_20260524.md",
            provider="deepseek",
            prompt_id="comprehensive",
        )
        store.record(rec3)
        all_in_range = store.list_by_date("20260520", "20260525")
        _check(
            len(all_in_range) == 2,
            f"日期范围内应有 2 条，实际 {len(all_in_range)}",
        )

        # list_pending_theme_extraction：只剩 rec3 还没抽
        pending = store.list_pending_theme_extraction()
        pending_ok = (
            len(pending) == 1
            and pending[0].file_path.endswith("old_20260524.md")
        )
        _check(
            pending_ok,
            f"待抽题材应剩 1 条 (old_20260524)，实际 {len(pending)}",
        )

        # delete_by_path
        ok = store.delete_by_path(rec.file_path)
        _check(
            ok and store.count() == 1,
            "delete_by_path 删除一条后总数应为 1",
        )
    finally:
        # 还原全局 get_db_path
        db_mod.get_db_path = (  # type: ignore[assignment]
            lambda original=original_path: original
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 70)
    print("Phase M1a 验收测试 —— market.db / ai_reports")
    print("=" * 70)

    # ignore_cleanup_errors=True: Windows 下 WAL/-shm 文件被 sqlite 进程持有
    # 时清理会抛 WinError 32，对测试结果无影响
    with tempfile.TemporaryDirectory(
        prefix="phase_m1a_", ignore_cleanup_errors=True
    ) as td:
        tmp_dir = Path(td)
        print(f"临时工作目录: {tmp_dir}")

        try:
            test_market_db_in_tmp(tmp_dir)
        except Exception as exc:  # noqa: BLE001
            _fail(f"test_market_db_in_tmp 抛异常: {exc}")
            traceback.print_exc()

        try:
            test_ai_reports_store(tmp_dir)
        except Exception as exc:  # noqa: BLE001
            _fail(f"test_ai_reports_store 抛异常: {exc}")
            traceback.print_exc()

    print()
    print("=" * 70)
    if _failures:
        print(f"验收失败：{len(_failures)} 项")
        for m in _failures:
            print(f"  - {m}")
        print("=" * 70)
        return 1
    print("验收通过：所有检查项 OK")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
