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
* :func:`delete_report` — 按 ``ai_reports.id`` 级联删除一份报告（含 md 文件）

删除语义（``delete_report``）
----------------------------
注意：``theme_predictions`` 并**未** FK 到 ``ai_reports``（仅靠
``report_path = ai_reports.file_path`` 软关联），所以删除要走两步：
::

    1. DELETE FROM theme_predictions WHERE report_path = ?      # 软关联
           ├── (CASCADE) theme_stocks
           ├── (CASCADE) theme_news
           ├── (CASCADE) theme_prediction_scores
           └── (CASCADE) theme_stock_scores
    2. DELETE FROM ai_reports        WHERE id = ?
    3. 物理删 data/AI_analysis/{file_path}.md（可关）

防误删：``allow_real=False`` 时若 ``is_backtest=0`` 直接抛
:class:`PermissionError`，避免脚本/手抖误删真实日常报告。
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

from services.storage.ai_inference_db import get_ai_inference_db
from services.storage.database import get_project_root

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class AIReportRecord:
    """``ai_reports`` 表的强类型表示（v5: 加 is_backtest）。"""

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
    is_backtest: int = 0  # v5: 0=真实日常生成, 1=虚拟回测 CLI 产物
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
    """``ai_reports`` 表的 CRUD 封装（v5: 已迁库到 ai_inference.db）。"""

    def __init__(self) -> None:
        get_ai_inference_db().ensure_schema()  # 确保表存在

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
            used_market_date, theme_extracted, is_backtest,
            file_size, md5, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            is_backtest      = excluded.is_backtest,
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
            int(bool(record.is_backtest)),
            size if record.file_size is None else record.file_size,
            md5_hex if record.md5 is None else record.md5,
            created_at,
        )

        with get_ai_inference_db().connect() as conn:
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
        with get_ai_inference_db().connect() as conn:
            cursor = conn.execute(
                "UPDATE ai_reports SET theme_extracted = ? "
                "WHERE file_path = ?",
                (1 if extracted else 0, rel),
            )
            return cursor.rowcount > 0

    def delete_by_path(self, file_path: str) -> bool:
        """删除一条索引（不删文件）。"""
        rel = _to_relative_posix(file_path)
        with get_ai_inference_db().connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ai_reports WHERE file_path = ?", (rel,)
            )
            return cursor.rowcount > 0

    # ------------- 查询 -------------

    def get_by_path(self, file_path: str) -> Optional[AIReportRecord]:
        rel = _to_relative_posix(file_path)
        with get_ai_inference_db().connect() as conn:
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
        with get_ai_inference_db().connect() as conn:
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
        with get_ai_inference_db().connect() as conn:
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
        with get_ai_inference_db().connect() as conn:
            rows = conn.execute(sql).fetchall()
            return [AIReportRecord.from_row(r) for r in rows]

    def count(self) -> int:
        with get_ai_inference_db().connect() as conn:
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
    is_backtest: bool = False,
) -> int:
    """便捷入口：生成 AI 分析报告后回写一条索引。

    Args:
        is_backtest: True = 虚拟回测 CLI 产物（Phase 6 用），
                     False = 真实日常生成（默认）

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
            is_backtest=1 if is_backtest else 0,
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
    "delete_report",
    "DeleteResult",
]


# ---------------------------------------------------------------------------
# 删除（评估页/CLI/回测 overwrite 共享）
# ---------------------------------------------------------------------------


@dataclass
class DeleteResult:
    """单份报告删除统计。

    Attributes:
        ok: 操作整体是否成功（包括 dry_run 也算 True）
        report_id: 入参回显
        file_path: 报告的相对 posix 路径（删除前查到的）
        is_backtest: 0/1，删之前的状态
        prompt_id: 删之前的 prompt_id
        report_date: 删之前的 report_date（YYYY-MM-DD 或 YYYYMMDD）
        ai_reports_deleted: 实删 ai_reports 行数（0 或 1）
        theme_predictions_deleted: 实删 theme_predictions 行数
        md_file_deleted: 是否实删了 md 文件
        dry_run: True = 仅预演不动数据
        error: 失败原因（ok=False 时填）
    """

    ok: bool
    report_id: int
    file_path: str = ""
    is_backtest: int = 0
    prompt_id: Optional[str] = None
    report_date: Optional[str] = None
    ai_reports_deleted: int = 0
    theme_predictions_deleted: int = 0
    md_file_deleted: bool = False
    dry_run: bool = False
    error: Optional[str] = None


def delete_report(
    report_id: int,
    *,
    allow_real: bool = False,
    delete_md: bool = True,
    dry_run: bool = False,
) -> DeleteResult:
    """按 ``ai_reports.id`` 级联删除一份报告。

    步骤：

    1. SELECT ai_reports 拿 file_path / is_backtest / prompt_id / report_date
    2. 防误删：``is_backtest=0`` 且 ``allow_real=False`` → 抛
       :class:`PermissionError`
    3. DELETE theme_predictions WHERE report_path = file_path
       （CASCADE 干掉 theme_stocks / theme_news /
       theme_prediction_scores / theme_stock_scores）
    4. DELETE ai_reports WHERE id = ?
    5. 物理删 ``data/AI_analysis/{file_path}.md``（``delete_md=True``）

    Args:
        report_id: ai_reports.id（必填，>0）
        allow_real: True 时允许删 ``is_backtest=0`` 的真实日常报告
        delete_md: True 时同步删物理 .md 文件（默认）
        dry_run: True 时只查不删，返回的统计数为"会删的数"

    Returns:
        :class:`DeleteResult`

    Raises:
        ValueError: ``report_id`` 不存在或非法
        PermissionError: 试图删真实日常报告但未传 ``allow_real=True``
    """
    if report_id is None or int(report_id) <= 0:
        raise ValueError(f"report_id 必须为正整数，得到 {report_id!r}")
    report_id = int(report_id)

    adb = get_ai_inference_db()
    with adb.connect() as conn:
        row = conn.execute(
            "SELECT id, file_path, is_backtest, prompt_id, report_date "
            "FROM ai_reports WHERE id = ?",
            (report_id,),
        ).fetchone()

        if row is None:
            raise ValueError(f"ai_reports.id={report_id} 不存在")

        file_path = str(row["file_path"] or "")
        is_bt = int(row["is_backtest"] or 0)
        prompt_id = row["prompt_id"]
        report_date = row["report_date"]

        if is_bt == 0 and not allow_real:
            raise PermissionError(
                f"report_id={report_id} (prompt={prompt_id}, "
                f"date={report_date}) 是真实日常报告（is_backtest=0），"
                f"需显式传 allow_real=True 才能删"
            )

        # 预统计（dry_run / 实跑都用）
        tp_count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM theme_predictions "
            "WHERE report_path = ?",
            (file_path,),
        ).fetchone()
        tp_count = int(tp_count_row["n"] or 0) if tp_count_row else 0

        md_abs = Path(get_project_root()) / file_path if file_path else None
        will_md_delete = bool(
            delete_md and md_abs and md_abs.exists() and md_abs.is_file()
        )

        if dry_run:
            return DeleteResult(
                ok=True,
                report_id=report_id,
                file_path=file_path,
                is_backtest=is_bt,
                prompt_id=prompt_id,
                report_date=report_date,
                ai_reports_deleted=1,
                theme_predictions_deleted=tp_count,
                md_file_deleted=will_md_delete,
                dry_run=True,
            )

        # 1) 先删 theme_predictions（CASCADE 子表）
        tp_n = 0
        if file_path:
            tp_cur = conn.execute(
                "DELETE FROM theme_predictions WHERE report_path = ?",
                (file_path,),
            )
            tp_n = tp_cur.rowcount

        # 2) 再删 ai_reports
        ar_cur = conn.execute(
            "DELETE FROM ai_reports WHERE id = ?", (report_id,),
        )
        ar_n = ar_cur.rowcount

    # 3) 物理删 md 文件（事务外，失败只警告不回滚 db）
    md_deleted = False
    if delete_md and file_path:
        abs_path = Path(get_project_root()) / file_path
        try:
            if abs_path.exists() and abs_path.is_file():
                abs_path.unlink()
                md_deleted = True
        except OSError as exc:
            _log.warning("删 md 文件失败 %s: %s", abs_path, exc)

    _log.info(
        "delete_report: id=%s prompt=%s date=%s bt=%s "
        "ai_reports=%d theme_predictions=%d md=%s",
        report_id, prompt_id, report_date, is_bt,
        ar_n, tp_n, md_deleted,
    )

    return DeleteResult(
        ok=True,
        report_id=report_id,
        file_path=file_path,
        is_backtest=is_bt,
        prompt_id=prompt_id,
        report_date=report_date,
        ai_reports_deleted=ar_n,
        theme_predictions_deleted=tp_n,
        md_file_deleted=md_deleted,
        dry_run=False,
    )
