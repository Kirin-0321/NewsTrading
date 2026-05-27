"""ai_inference.db 连接与迁移管理（Phase -1）。

提供:

* :class:`AIInferenceDB` — 单一入口，封装连接 / 迁移 / 备份 / 完整性校验。
* :func:`get_ai_inference_db` — 进程内单例（懒初始化）。

设计要点
--------
1. **责任边界**: 本库专放 AI 衍生数据（题材抽取/打分/复审），与 news.db
   （爬虫产物）和 market.db（行情接口产物）平级。详见三库表结构详细设计。
2. **迁移目录**: ``services/storage/ai_migrations/`` 下 ``NNN_{name}.sql``
   文件按字典序顺序执行，已应用版本号记录在 ``schema_migrations`` 表。
3. **幂等**: 所有迁移 SQL 必须使用 ``CREATE TABLE IF NOT EXISTS`` /
   ``INSERT OR IGNORE``，允许重复跑。
4. **启动备份**: ``ensure_schema()`` 在跑任何迁移之前都会先复制一份
   ``data/backups/ai_inference.db.{yyyymmdd_HHMMSS}.bak``，并清理仅保留最近 7 份。
5. **WAL + 外键 + 长 busy_timeout**: 由 :meth:`connect` 上下文统一启用。
6. **跨库 JOIN**: :meth:`attach_to` 把本库 ATTACH 到外部连接，
   常用于打分 SQL 跨库联查 ``market.fact_stock_daily`` / ``news.raw_news``。

使用示例::

    from services.storage.ai_inference_db import get_ai_inference_db

    db = get_ai_inference_db()
    db.ensure_schema()        # 首次启动建 6 张表 + 17 个索引

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM theme_predictions WHERE is_backtest=0"
        ).fetchall()

    db.backup_to(Path("data/backups/manual.bak"))
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "ai_inference.db"
DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "ai_migrations"
DEFAULT_BACKUP_DIR = _PROJECT_ROOT / "data" / "backups"

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")

BACKUP_RETENTION = 7

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AIInferenceDBError(RuntimeError):
    """ai_inference.db 相关错误的根类。"""


class MigrationError(AIInferenceDBError):
    """迁移执行失败。"""


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class AIInferenceDB:
    """``data/ai_inference.db`` 的连接与迁移管理器。

    所有公开方法都是线程安全的（迁移用进程级 RLock 保护）。
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        migrations_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
    ) -> None:
        self.path: Path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.migrations_dir: Path = (
            Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR
        )
        self.backup_dir: Path = (
            Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
        )
        self._lock = threading.RLock()
        self._schema_ready = False

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    @contextmanager
    def connect(
        self, *, readonly: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """获取连接上下文。

        - 启用 WAL / foreign_keys / busy_timeout=30s
        - ``row_factory = sqlite3.Row`` 方便按字段名访问
        - 正常退出自动 commit，异常自动 rollback
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self.path}?mode=ro" if readonly else None
        if uri:
            conn = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            if not readonly:
                raise
        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception:
            if not readonly:
                conn.rollback()
            raise
        finally:
            conn.close()

    def attach_to(
        self, foreign_conn: sqlite3.Connection, alias: str = "a"
    ) -> None:
        """把本 ai_inference.db ATTACH 到外部连接，用于跨库 JOIN。

        默认 alias = 'a'（与 market 的 'm'、news 的 'n' 对称）。
        外部代码完成后建议调 ``foreign_conn.execute(f"DETACH DATABASE {alias}")``
        或走 ``services.storage.cross_db.attached_dbs`` 上下文工具自动管理。
        """
        if not self.path.exists():
            raise AIInferenceDBError(
                f"ai_inference.db 不存在: {self.path}"
            )
        foreign_conn.execute(
            f"ATTACH DATABASE '{self.path.as_posix()}' AS {alias}"
        )

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def ensure_schema(self, *, do_backup: bool = True) -> List[int]:
        """检查并应用所有待应用的迁移。

        Args:
            do_backup: 若 ``ai_inference.db`` 已存在且有待应用迁移，则先备份。

        Returns:
            本次应用的版本号列表（按顺序）。空表示已是最新。
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self._list_pending_migrations()
            if not pending:
                self._schema_ready = True
                return []

            existed = self.path.exists() and self.path.stat().st_size > 0
            if existed and do_backup:
                try:
                    self.backup_on_startup(tag="pre-migration")
                except Exception as exc:
                    _log.warning("迁移前自动备份失败: %s", exc)

            applied: List[int] = []
            for ver, name, sql_path in pending:
                self._apply_migration(ver, name, sql_path)
                applied.append(ver)

            self._schema_ready = True
            _log.info("ai_inference.db 迁移完成：%s", applied)
            return applied

    def _list_pending_migrations(self) -> List[Tuple[int, str, Path]]:
        """返回未应用的迁移列表 ``[(ver, name, sql_path), ...]``。"""
        if not self.migrations_dir.exists():
            raise AIInferenceDBError(
                f"迁移目录不存在: {self.migrations_dir}"
            )

        all_files: List[Tuple[int, str, Path]] = []
        for f in sorted(self.migrations_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() != ".sql":
                continue
            m = _MIGRATION_FILENAME_RE.match(f.name)
            if not m:
                _log.warning("跳过不符合命名规范的迁移文件: %s", f.name)
                continue
            ver = int(m.group(1))
            stem = f.stem
            all_files.append((ver, stem, f))

        if not all_files:
            return []

        applied_versions = self._get_applied_versions()
        return [t for t in all_files if t[0] not in applied_versions]

    def _get_applied_versions(self) -> set[int]:
        """读取 schema_migrations 已应用版本号。表不存在时返回空集合。"""
        if not self.path.exists():
            return set()
        try:
            with self.connect(readonly=True) as conn:
                rows = conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
                return {int(r[0]) for r in rows}
        except sqlite3.OperationalError:
            return set()

    def _apply_migration(self, ver: int, name: str, sql_path: Path) -> None:
        """在单次连接中执行 .sql 并写入 schema_migrations。

        迁移期间会**临时关闭外键约束**——SQLite 重建表（DROP/RENAME）
        要求 FK 必须关，否则带 REFERENCES 的子表会被孤立。这是 SQLite
        官方推荐的"12-step ALTER TABLE"流程的必要步骤。
        """
        _log.info("apply migration %s (%s)", name, sql_path.name)
        sql_text = sql_path.read_text(encoding="utf-8")
        try:
            with self.connect() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.executescript(sql_text)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " version INTEGER PRIMARY KEY,"
                    " name TEXT NOT NULL,"
                    " applied_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations "
                    "(version, name, applied_at) VALUES (?, ?, ?)",
                    (ver, name, _now_iso()),
                )
                problems = conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if problems:
                    raise MigrationError(
                        f"迁移 {name} 后外键校验失败: {problems[:3]}"
                    )
                conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            raise MigrationError(
                f"迁移 {name} 执行失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------

    def backup_on_startup(self, *, tag: str = "startup") -> Optional[Path]:
        """启动时自动备份。

        若 ``ai_inference.db`` 不存在或大小为 0，跳过。
        备份文件命名: ``ai_inference.db.{tag}.{YYYYMMDD_HHMMSS}.bak``，
        保留最近 ``BACKUP_RETENTION`` 份。
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.backup_dir / f"ai_inference.db.{tag}.{ts}.bak"
        self.backup_to(target)
        self._cleanup_old_backups()
        return target

    def backup_to(self, target: Path) -> Path:
        """使用 SQLite 原生 ``Connection.backup()`` 复制数据库。"""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            raise AIInferenceDBError(
                f"源 ai_inference.db 不存在: {self.path}"
            )
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(target)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
        _log.info(
            "backup 完成: %s -> %s", self.path.name, target
        )
        return target

    def _cleanup_old_backups(self) -> None:
        """仅保留最近 BACKUP_RETENTION 份 ai_inference.db.*.bak。"""
        if not self.backup_dir.exists():
            return
        files = sorted(
            self.backup_dir.glob("ai_inference.db.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[BACKUP_RETENTION:]:
            try:
                stale.unlink()
                _log.debug("清理旧备份: %s", stale.name)
            except OSError as exc:
                _log.warning("清理旧备份失败 %s: %s", stale.name, exc)

    def list_backups(self) -> List[Path]:
        """按时间倒序列出当前 backups 目录下的备份文件。"""
        if not self.backup_dir.exists():
            return []
        return sorted(
            self.backup_dir.glob("ai_inference.db.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    # ------------------------------------------------------------------
    # 检查 / 统计
    # ------------------------------------------------------------------

    def verify_integrity(self) -> Tuple[bool, str]:
        """运行 ``PRAGMA integrity_check`` 返回 ``(ok, message)``。"""
        if not self.path.exists():
            return False, "ai_inference.db 文件不存在"
        with self.connect(readonly=True) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            msg = row[0] if row else "no result"
            return (msg == "ok"), msg

    def get_stats(self) -> dict:
        """返回各表行数 + 数据库文件大小 + 备份信息（GUI 状态对话框用）。"""
        size = self.path.stat().st_size if self.path.exists() else 0
        backups = [
            {
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(
                    p.stat().st_mtime
                ).isoformat(),
            }
            for p in self.list_backups()
        ]
        stats: dict = {
            "db_path": str(self.path),
            "size_bytes": size,
            "tables": {},
            "applied_migrations": [],
            "backups": backups,
        }
        if not self.path.exists():
            return stats
        with self.connect(readonly=True) as conn:
            try:
                rows = conn.execute(
                    "SELECT version, name, applied_at FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
                stats["applied_migrations"] = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass

            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for t in tables:
                tname = t[0]
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {tname}"
                ).fetchone()[0]
                stats["tables"][tname] = count
        return stats


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


_singleton: Optional[AIInferenceDB] = None
_singleton_lock = threading.Lock()


def get_ai_inference_db() -> AIInferenceDB:
    """返回进程内 ``AIInferenceDB`` 单例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AIInferenceDB()
    return _singleton


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    db = AIInferenceDB()
    print(f"[ai_inference_db] db_path = {db.path}")
    applied = db.ensure_schema()
    print(f"[ai_inference_db] applied migrations = {applied}")

    ok, msg = db.verify_integrity()
    print(f"[ai_inference_db] integrity_check = {ok} ({msg})")

    stats = db.get_stats()
    print("[ai_inference_db] stats:")
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))

    os._exit(0 if ok else 1)
