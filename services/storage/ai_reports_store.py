"""AI 分析报告索引库（``news.db`` 内 ``ai_reports`` 表的访问层）。

报告**正文**仍然按文件存（``data/AI_analysis/*.md``），本表只索引：
路径、provider/model、prompt 元信息、新闻范围、是否抽过题材、文件 md5 等。

后续 Eval / 回测 / GUI 报告列表 等场景，都通过本表过滤候选文件，
避免每次都遍历目录读文件头。

主入口
------
* :class:`AIReportsStore` — 数据访问对象
* :func:`get_ai_reports_store` — 单例
* :func:`record_report` — 便捷函数（生成报告后回写时调用）
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from services.storage.database import (
    get_connection,
    get_project_root,
    init_database,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class AIReportRecord:
    """``ai_reports`` 表的强类型表示。"""

    id: Optional[int] = None
    report_date: str = ""
    file_path: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt_category: Optional[str] = None
    prompt_id: Optional[str] = None
    prompt_version: Optional[str] = None
    news_range_start: Optional[str] = None
    news_range_end: Optional[str] = None
    news_count: Optional[int] = None
    used_market_date: Optional[str] = None
    theme_extracted: int = 0
    file_size: Optional[int] = None
    md5: Optional[str] = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AIReportRecord":
        return cls(**{k: row[k] for k in row.keys()})


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AIReportsStore:
    """``ai_reports`` 表的 CRUD 封装。"""

    def __init__(self) -> None:
        init_database()  # 确保表存在

    # ------------- 写入 -------------

    def record(self, record: AIReportRecord) -> int:
        """新增或按 file_path 覆盖一条索引；返回 id。

        若 ``record.file_path`` 是绝对路径，会自动转换为相对仓库根的
        POSIX 风格路径再入库，便于跨平台、迁移时不破坏索引。
        """
        rel_path = _to_relative_posix(record.file_path)
        size, md5_hex = _stat_and_hash(rel_path)
        created_at = record.created_at or _now_iso()

        sql = """
        INSERT INTO ai_reports (
            report_date, file_path, provider, model,
            prompt_category, prompt_id, prompt_version,
            news_range_start, news_range_end, news_count,
            used_market_date, theme_extracted,
            file_size, md5, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(file_path) DO UPDATE SET
            report_date      = excluded.report_date,
            provider         = excluded.provider,
            model            = excluded.model,
            prompt_category  = excluded.prompt_category,
            prompt_id        = excluded.prompt_id,
            prompt_version   = excluded.prompt_version,
            news_range_start = excluded.news_range_start,
            news_range_end   = excluded.news_range_end,
            news_count       = excluded.news_count,
            used_market_date = excluded.used_market_date,
            theme_extracted  = excluded.theme_extracted,
            file_size        = excluded.file_size,
            md5              = excluded.md5
        """
        params = (
            record.report_date,
            rel_path,
            record.provider,
            record.model,
            record.prompt_category,
            record.prompt_id,
            record.prompt_version,
            record.news_range_start,
            record.news_range_end,
            record.news_count,
            record.used_market_date,
            int(bool(record.theme_extracted)),
            size if record.file_size is None else record.file_size,
            md5_hex if record.md5 is None else record.md5,
            created_at,
        )

        with get_connection() as conn:
            conn.execute(sql, params)
            row = conn.execute(
                "SELECT id FROM ai_reports WHERE file_path = ?",
                (rel_path,),
            ).fetchone()
            return int(row["id"]) if row else 0

    def mark_theme_extracted(
        self, file_path: str, extracted: bool = True
    ) -> bool:
        """标记一份报告已抽过题材入 ``theme_predictions``。"""
        rel = _to_relative_posix(file_path)
        with get_connection() as conn:
            cursor = conn.execute(
                "UPDATE ai_reports SET theme_extracted = ? "
                "WHERE file_path = ?",
                (1 if extracted else 0, rel),
            )
            return cursor.rowcount > 0

    def delete_by_path(self, file_path: str) -> bool:
        """删除一条索引（不删文件）。"""
        rel = _to_relative_posix(file_path)
        with get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM ai_reports WHERE file_path = ?", (rel,)
            )
            return cursor.rowcount > 0

    # ------------- 查询 -------------

    def get_by_path(self, file_path: str) -> Optional[AIReportRecord]:
        rel = _to_relative_posix(file_path)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM ai_reports WHERE file_path = ?", (rel,)
            ).fetchone()
            return AIReportRecord.from_row(row) if row else None

    def list_by_date(
        self,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> List[AIReportRecord]:
        """按 ``report_date`` 范围筛选，倒序返回。

        两个参数都为 None 时全量返回。
        """
        conditions: List[str] = []
        params: List[object] = []
        if date_start:
            conditions.append("report_date >= ?")
            params.append(date_start)
        if date_end:
            conditions.append("report_date <= ?")
            params.append(date_end)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        suffix = f" LIMIT {int(limit)}" if limit else ""
        sql = (
            "SELECT * FROM ai_reports"
            + where
            + " ORDER BY report_date DESC, id DESC"
            + suffix
        )
        with get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [AIReportRecord.from_row(r) for r in rows]

    def list_by_prompt(
        self,
        prompt_id: str,
        *,
        prompt_version: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[AIReportRecord]:
        sql = "SELECT * FROM ai_reports WHERE prompt_id = ?"
        params: List[object] = [prompt_id]
        if prompt_version:
            sql += " AND prompt_version = ?"
            params.append(prompt_version)
        sql += " ORDER BY report_date DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [AIReportRecord.from_row(r) for r in rows]

    def list_pending_theme_extraction(
        self, limit: Optional[int] = None
    ) -> List[AIReportRecord]:
        """返回 ``theme_extracted=0`` 的报告（题材抽取后台任务用）。"""
        sql = (
            "SELECT * FROM ai_reports WHERE theme_extracted = 0 "
            "ORDER BY report_date DESC, id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        with get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [AIReportRecord.from_row(r) for r in rows]

    def count(self) -> int:
        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM ai_reports").fetchone()
            return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 单例 + 便捷入口
# ---------------------------------------------------------------------------


_singleton: Optional[AIReportsStore] = None
_singleton_lock = threading.Lock()


def get_ai_reports_store() -> AIReportsStore:
    """进程内单例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AIReportsStore()
    return _singleton


def record_report(
    file_path: str,
    *,
    report_date: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    prompt_category: Optional[str] = None,
    prompt_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    news_range_start: Optional[str] = None,
    news_range_end: Optional[str] = None,
    news_count: Optional[int] = None,
    used_market_date: Optional[str] = None,
    theme_extracted: bool = False,
) -> int:
    """便捷入口：生成 AI 分析报告后回写一条索引。

    返回新写入或更新的 record id。绝对不抛异常（任何错误降级为日志），
    以避免索引环节的故障破坏正常的报告生成流程。
    """
    try:
        store = get_ai_reports_store()
        rec = AIReportRecord(
            report_date=report_date,
            file_path=file_path,
            provider=provider,
            model=model,
            prompt_category=prompt_category,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            news_range_start=news_range_start,
            news_range_end=news_range_end,
            news_count=news_count,
            used_market_date=used_market_date,
            theme_extracted=1 if theme_extracted else 0,
        )
        return store.record(rec)
    except Exception as exc:  # 索引环节不影响主流程
        _log.warning("ai_reports 索引写入失败 (%s): %s", file_path, exc)
        return 0


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_relative_posix(path_like: str) -> str:
    """把任意路径转为「相对仓库根 + POSIX 风格」，便于跨平台落库。"""
    if not path_like:
        return ""
    p = Path(path_like)
    if p.is_absolute():
        try:
            p = p.relative_to(Path(get_project_root()))
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def _stat_and_hash(rel_path: str) -> tuple[Optional[int], Optional[str]]:
    """对相对路径的文件取 size + md5。文件不存在时返回 (None, None)。"""
    if not rel_path:
        return None, None
    abs_path = Path(get_project_root()) / rel_path
    if not abs_path.exists() or not abs_path.is_file():
        return None, None
    try:
        size = abs_path.stat().st_size
        md5 = hashlib.md5()
        with abs_path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                md5.update(chunk)
        return size, md5.hexdigest()
    except OSError:
        return None, None


__all__ = [
    "AIReportRecord",
    "AIReportsStore",
    "get_ai_reports_store",
    "record_report",
]
