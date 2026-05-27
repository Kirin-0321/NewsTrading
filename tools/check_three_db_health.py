"""三库 schema 健康检查（Phase 0 入口卫兵）。

用途
----
- 单独跑：作为 schema 巡检 / 上线前体检
- 被 main.py 启动时调：缺数据库友好提示主人先跑 ensure_schema()

输出形如::

    [OK] news.db: 2 tables (raw_news, sync_meta), 19 news records
    [OK] market.db: 17 tables, 5 migrations applied
         - fact_stock_daily: 0 rows
         - fact_sector_daily: 14660 rows
    [OK] ai_inference.db: 7 tables (含 schema_migrations), 1 migration applied
         - ai_reports: 0 rows
         - theme_predictions: 0 rows
    [OK] 跨库 ATTACH 烟测通过
    [OK] FK 启用：foreign_keys=ON
    [OK] WAL 模式：journal_mode=WAL

CLI
---
::

    python tools/check_three_db_health.py            # 默认输出
    python tools/check_three_db_health.py --quiet    # 只输出 [FAIL] 行
    python tools/check_three_db_health.py --json     # 机器可读 JSON

退出码
------
- 0  全部 [OK]
- 1  至少一项 [FAIL]
- 2  数据库文件本身缺失（建议跑 ensure_schema）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CheckItem:
    name: str
    status: str  # "OK" / "FAIL" / "WARN"
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthReport:
    items: List[CheckItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(i.status != "FAIL" for i in self.items)

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.items if i.status == "FAIL")


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------


# 各库期望的"骨架表"，缺一个就视为 schema 不完整
_NEWS_REQUIRED_TABLES = {"raw_news", "sync_meta"}
_MARKET_REQUIRED_TABLES = {
    "dim_trade_calendar",
    "dim_sector",
    "dim_stock",
    "fact_stock_daily",
    "fact_sector_daily",
    "schema_migrations",
}
_AI_REQUIRED_TABLES = {
    "ai_reports",
    "theme_predictions",
    "theme_stocks",
    "theme_news",
    "theme_prediction_scores",
    "theme_stock_scores",
}


def _list_tables(path: Path) -> List[str]:
    conn = sqlite3.connect(path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()


def _row_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return -1
    finally:
        conn.close()


def check_news_db(report: HealthReport) -> None:
    from services.storage.database import get_db_path
    path = Path(get_db_path())
    if not path.exists():
        report.items.append(CheckItem(
            "news.db", "FAIL",
            f"文件不存在: {path}（建议跑 services.storage.database.init_database()）",
        ))
        return
    tables = set(_list_tables(path))
    missing = _NEWS_REQUIRED_TABLES - tables
    legacy = ({"ai_reports", "theme_predictions",
               "theme_stocks", "theme_news"} & tables)

    raw_count = _row_count(path, "raw_news") if "raw_news" in tables else -1
    extra = {
        "path": str(path),
        "tables": sorted(tables),
        "raw_news_rows": raw_count,
    }

    if missing:
        report.items.append(CheckItem(
            "news.db", "FAIL",
            f"缺失必备表 {sorted(missing)}（建议跑 init_database()）",
            extra,
        ))
        return
    if legacy:
        report.items.append(CheckItem(
            "news.db", "WARN",
            f"仍存在 Phase -1 应已 DROP 的旧 AI 表 {sorted(legacy)}（"
            f"重新启动 main.py 会自动清掉）",
            extra,
        ))
        return
    report.items.append(CheckItem(
        "news.db", "OK",
        f"{len(tables)} tables (raw_news, sync_meta), "
        f"{raw_count} news records",
        extra,
    ))


def check_market_db(report: HealthReport) -> None:
    from services.market.market_db import get_market_db
    db = get_market_db()
    path = db.path
    if not path.exists():
        report.items.append(CheckItem(
            "market.db", "FAIL",
            f"文件不存在: {path}（建议跑 MarketDB().ensure_schema()）",
        ))
        return
    tables = set(_list_tables(path))
    missing = _MARKET_REQUIRED_TABLES - tables

    fsd = (
        _row_count(path, "fact_stock_daily")
        if "fact_stock_daily" in tables else -1
    )
    fsec = (
        _row_count(path, "fact_sector_daily")
        if "fact_sector_daily" in tables else -1
    )

    applied_versions: List[int] = []
    if "schema_migrations" in tables:
        conn = sqlite3.connect(path)
        try:
            applied_versions = [
                int(r[0])
                for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        finally:
            conn.close()

    extra = {
        "path": str(path),
        "tables_count": len(tables),
        "applied_migrations": applied_versions,
        "fact_stock_daily_rows": fsd,
        "fact_sector_daily_rows": fsec,
    }

    if missing:
        report.items.append(CheckItem(
            "market.db", "FAIL",
            f"缺失必备表 {sorted(missing)}（建议跑 MarketDB().ensure_schema()）",
            extra,
        ))
        return

    report.items.append(CheckItem(
        "market.db", "OK",
        f"{len(tables)} tables, {len(applied_versions)} migrations applied",
        extra,
    ))
    report.items.append(CheckItem(
        "market.db.fact_stock_daily", "OK" if fsd >= 0 else "FAIL",
        f"{fsd} rows" + ("（待 Phase 1 填充）" if fsd == 0 else ""),
        {"rows": fsd},
    ))
    report.items.append(CheckItem(
        "market.db.fact_sector_daily", "OK" if fsec >= 0 else "FAIL",
        f"{fsec} rows" + ("（待 Phase 1 填充）" if fsec == 0 else ""),
        {"rows": fsec},
    ))


def check_ai_inference_db(report: HealthReport) -> None:
    from services.storage.ai_inference_db import get_ai_inference_db
    db = get_ai_inference_db()
    path = db.path
    if not path.exists():
        report.items.append(CheckItem(
            "ai_inference.db", "FAIL",
            f"文件不存在: {path}（建议跑 get_ai_inference_db().ensure_schema()）",
        ))
        return
    tables = set(_list_tables(path))
    missing = _AI_REQUIRED_TABLES - tables

    applied_versions: List[int] = []
    if "schema_migrations" in tables:
        conn = sqlite3.connect(path)
        try:
            applied_versions = [
                int(r[0])
                for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        finally:
            conn.close()

    row_counts = {
        t: _row_count(path, t)
        for t in sorted(_AI_REQUIRED_TABLES & tables)
    }

    extra = {
        "path": str(path),
        "tables_count": len(tables),
        "applied_migrations": applied_versions,
        "rows": row_counts,
    }

    if missing:
        report.items.append(CheckItem(
            "ai_inference.db", "FAIL",
            f"缺失必备表 {sorted(missing)}（建议跑 "
            f"get_ai_inference_db().ensure_schema()）",
            extra,
        ))
        return

    report.items.append(CheckItem(
        "ai_inference.db", "OK",
        f"{len(tables)} tables (含 schema_migrations), "
        f"{len(applied_versions)} migration applied",
        extra,
    ))
    for t in ("ai_reports", "theme_predictions", "theme_prediction_scores"):
        n = row_counts.get(t, -1)
        report.items.append(CheckItem(
            f"ai_inference.db.{t}", "OK" if n >= 0 else "FAIL",
            f"{n} rows",
            {"rows": n},
        ))


def check_cross_db_attach(report: HealthReport) -> None:
    """跨库 ATTACH 烟测：用 attached_dbs 同时挂三库执行一条 join。"""
    try:
        from services.storage.cross_db import attached_dbs
        with attached_dbs(
            primary="ai",
            attach=("market", "news"),
            readonly=True,
        ) as conn:
            row = conn.execute(
                "SELECT (SELECT COUNT(*) FROM ai_reports) AS ar, "
                "       (SELECT COUNT(*) FROM market.fact_sector_daily)"
                "         AS fsec, "
                "       (SELECT COUNT(*) FROM news.raw_news) AS rn"
            ).fetchone()
        report.items.append(CheckItem(
            "cross_db ATTACH",
            "OK",
            f"三库挂载成功（ai={row['ar']} "
            f"market={row['fsec']} news={row['rn']}）",
        ))
    except Exception as exc:
        report.items.append(CheckItem(
            "cross_db ATTACH", "FAIL", f"挂载失败: {exc}",
        ))


def check_pragmas(report: HealthReport) -> None:
    """主库各开一次连接，验证 FK / WAL 启用。"""
    checks = [
        (
            "ai_inference.db",
            "services.storage.ai_inference_db",
            "get_ai_inference_db",
        ),
        ("market.db", "services.market.market_db", "get_market_db"),
    ]
    for name, mod, fn_name in checks:
        try:
            mod_obj = __import__(mod, fromlist=[fn_name])
            db = getattr(mod_obj, fn_name)()
            with db.connect() as conn:
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except Exception as exc:
            report.items.append(CheckItem(
                f"PRAGMA {name}", "FAIL", f"取值失败: {exc}",
            ))
            continue
        ok_fk = int(fk) == 1
        ok_jm = str(jm).lower() == "wal"
        status = "OK" if (ok_fk and ok_jm) else "FAIL"
        report.items.append(CheckItem(
            f"PRAGMA {name}", status,
            f"foreign_keys={fk}, journal_mode={jm}",
            {"foreign_keys": fk, "journal_mode": jm},
        ))


def run_all_checks() -> HealthReport:
    report = HealthReport()
    check_news_db(report)
    check_market_db(report)
    check_ai_inference_db(report)
    if report.ok:
        # 仅在 schema 完整时跑跨库测试，否则容易二次报错
        check_cross_db_attach(report)
        check_pragmas(report)
    return report


# ---------------------------------------------------------------------------
# CLI 输出
# ---------------------------------------------------------------------------


def _print_text(report: HealthReport, quiet: bool) -> None:
    icon = {"OK": "[OK]", "FAIL": "[FAIL]", "WARN": "[WARN]"}
    for item in report.items:
        if quiet and item.status == "OK":
            continue
        line = f"{icon.get(item.status, '[?]'):6s} {item.name}"
        if item.detail:
            line += f": {item.detail}"
        print(line)


def _print_json(report: HealthReport) -> None:
    payload = {
        "ok": report.ok,
        "fail_count": report.fail_count,
        "items": [asdict(i) for i in report.items],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="三库 schema 健康检查（Phase 0 卫兵）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--quiet", action="store_true",
                        help="仅输出 FAIL/WARN 行")
    parser.add_argument("--json", action="store_true",
                        help="JSON 输出（机器可读）")
    args = parser.parse_args(argv)

    report = run_all_checks()

    if args.json:
        _print_json(report)
    else:
        _print_text(report, quiet=args.quiet)

    if report.fail_count == 0:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
