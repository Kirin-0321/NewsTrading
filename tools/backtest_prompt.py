"""模板虚拟回测 CLI（Phase 6 Step 6.2 + 2026-05-27 时间窗语义重构）。

业务定位
--------
"时光机"工具：模拟在 ``trade_date`` 的 ``[news_start_dt, news_end_dt)``
新闻窗口下，用指定 ``template_id`` 跑一次 AI 分析，产物：

* ``data/AI_analysis/...`` 多一份 ``_backtest_{trade_date}.md`` 文件   
* ``ai_inference.db`` 的 ``ai_reports`` / ``theme_predictions`` 多
  ``is_backtest=1`` 的行
* 接到 Phase 2 打分链路里，可直接被 :mod:`services.scoring.scoring_service`
  消费

时间窗主人语义（**2026-05-27 重构**）
-----------------------------------
* **左边界（news_start_dt）默认 = trade_date 14:00**
* **右边界（news_end_dt）默认 = next_trade_date(trade_date) 09:00**
* **右边界硬上限 = next_trade_date(trade_date) 09:00**
* 详见 :func:`services.scoring.snapshot.build_snapshot`

实现链路
--------
::

    build_snapshot(trade_date, news_start_dt, news_end_dt, news_status)
        ↓
    AnalysisService.analyze(start=news_start_dt, end=news_end_dt,
                            market_summary=snap.market_summary_md)
        ↓ 自动调
        AINewsAnalyzer → 写 md → _record_ai_report → _maybe_extract_themes
        ↓
    重命名 md 文件加 _backtest_{trade_date} 后缀，并：
        UPDATE ai_reports        SET is_backtest=1, file_path=new_path,
                                      report_date=trade_date
        UPDATE theme_predictions SET is_backtest=1, report_path=new_path,
                                      report_date=trade_date

防穿越铁律：
    * news_end_dt 不能晚于 ``now``（不允许回测"未来"）
    * news_end_dt 不能超过 next_trade_date(trade_date) 09:00（snapshot
      内部强制）

用法
----
::

    # 单日单模板（用主人默认窗）
    python tools/backtest_prompt.py \\
        --template custom_1770292858 --date 20260520

    # 区间多模板 + 显式时间窗
    python tools/backtest_prompt.py \\
        --template custom_1770292858,custom_6 \\
        --date-range 20260513..20260526 \\
        --news-start 202605131400 \\
        --news-end 202605140900 \\
        --provider deepseek --workers 4

    # dry-run + 全部新闻（含 rejected）
    python tools/backtest_prompt.py --template custom_6 \\
        --date 20260520 --all-news --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.market.trade_date import (  # noqa: E402
    TradeDateError,
    trade_dates_between,
)
from services.market.tushare_client import TushareClient  # noqa: E402
from services.scoring.snapshot import (  # noqa: E402
    SnapshotError,
    build_snapshot,
    compute_default_news_window,
)

_log = logging.getLogger("backtest_prompt")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """单次 (template, date) 虚拟回测结果。"""

    ok: bool
    template_id: str
    trade_date: str
    news_start_iso: str            # 实际生效的左边界 ISO
    news_end_iso: str              # 实际生效的右边界 ISO
    news_status: str               # curated / all
    snapshot_news_count: int
    report_path: Optional[str]
    themes_count: int
    elapsed_ms: int
    error: Optional[str] = None
    skipped: bool = False          # True = 已存在 / 无新闻自动跳过
    overwritten: bool = False      # True = 命中已存在且 overwrite 删旧重跑
    deleted_reports: int = 0       # overwrite 时实删 ai_reports 行数
    deleted_themes: int = 0        # overwrite 时实删 theme_predictions 行数
    deleted_md_files: int = 0      # overwrite 时实删 md 物理文件数


# ---------------------------------------------------------------------------
# 单次回测
# ---------------------------------------------------------------------------


def backtest_one(
    template_id: str,
    trade_date: str,
    *,
    news_start_dt: Optional[datetime] = None,
    news_end_dt: Optional[datetime] = None,
    news_status: str = "curated",
    provider: Optional[str] = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> BacktestResult:
    """对 ``template_id`` 在 ``trade_date`` 跑一次回测。

    Args:
        template_id: prompt_id
        trade_date: YYYYMMDD（锚定日，可以是非交易日）
        news_start_dt: 新闻窗左边界，None = trade_date 14:00（主人默认）
        news_end_dt: 新闻窗右边界，None = next_trade_date 09:00（硬上限）
        news_status: "curated"（精选，默认）/ ""（全部，含 rejected）
        provider: LLM provider，None = 走默认
        overwrite: 命中同 (template, date, is_backtest=1) 时：
            * True  → 调 :func:`_delete_existing_backtest` 删旧 5 表 + md
              文件后重跑（**会真的删数据**）
            * False → 跳过本次回测，error 提示传 --overwrite
        dry_run: 仅重建 snapshot 不调 LLM

    防穿越：``news_end_dt`` 不能晚于 ``now``（避免回测"未来"）。
    """
    t0 = time.perf_counter()
    client = TushareClient()

    # 1. 解析默认窗（用于早期返回时的元数据 + 主人默认值）
    default_start, default_end = compute_default_news_window(
        trade_date, client=client,
    )
    eff_start = news_start_dt or default_start
    eff_end = news_end_dt or default_end
    start_iso = eff_start.isoformat(timespec="seconds")
    end_iso = eff_end.isoformat(timespec="seconds")

    overwrite_stats = {
        "overwritten": False,
        "deleted_reports": 0,
        "deleted_themes": 0,
        "deleted_md_files": 0,
    }

    def _make_result(
        *,
        ok: bool,
        snap_news_count: int = 0,
        report_path: Optional[str] = None,
        themes_count: int = 0,
        error: Optional[str] = None,
        skipped: bool = False,
    ) -> BacktestResult:
        return BacktestResult(
            ok=ok,
            template_id=template_id,
            trade_date=trade_date,
            news_start_iso=start_iso,
            news_end_iso=end_iso,
            news_status=news_status or "all",
            snapshot_news_count=snap_news_count,
            report_path=report_path,
            themes_count=themes_count,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error=error,
            skipped=skipped,
            overwritten=overwrite_stats["overwritten"],
            deleted_reports=overwrite_stats["deleted_reports"],
            deleted_themes=overwrite_stats["deleted_themes"],
            deleted_md_files=overwrite_stats["deleted_md_files"],
        )

    # 2. 防穿越（不能回测未来）
    if eff_end > datetime.now():
        return _make_result(
            ok=False,
            error=(
                f"穿越保护：news_end_dt={eff_end.isoformat()} > now，"
                f"不能回测未来时间窗"
            ),
        )

    # 3. 重建快照
    try:
        snap = build_snapshot(
            trade_date,
            news_start_dt=news_start_dt,  # 传 None 让 snapshot 自己用默认
            news_end_dt=news_end_dt,
            news_status=news_status,
            client=client,
        )
    except SnapshotError as exc:
        return _make_result(ok=False, error=f"snapshot 失败: {exc}")

    # 4. dry-run 提前返回
    if dry_run:
        return _make_result(
            ok=True, snap_news_count=snap.news_count,
        )

    # 5. 防御：snapshot 无新闻 → 直接跳过
    if snap.news_count == 0:
        return _make_result(
            ok=True, snap_news_count=0, skipped=True,
            error="snapshot 无新闻（历史新闻库未覆盖该窗），自动跳过",
        )

    # 6. 已存在 backtest 记录 → overwrite 则删旧重跑，否则跳过
    if _has_existing_backtest(template_id, trade_date):
        if overwrite:
            stats = _delete_existing_backtest(template_id, trade_date)
            overwrite_stats["overwritten"] = True
            overwrite_stats["deleted_reports"] = stats["ai_reports"]
            overwrite_stats["deleted_themes"] = stats["theme_predictions"]
            overwrite_stats["deleted_md_files"] = stats["md_files"]
            _log.info(
                "overwrite: 清理旧 backtest 记录 "
                "ai_reports=%d theme_predictions=%d md_files=%d",
                stats["ai_reports"], stats["theme_predictions"],
                stats["md_files"],
            )
        else:
            return _make_result(
                ok=True, snap_news_count=snap.news_count, skipped=True,
                error=(
                    "已存在同 (template, date) 的 backtest 记录，"
                    "传 --overwrite 删旧重跑"
                ),
            )

    # 7. 调 AnalysisService（这里 LLM 真实消耗发生处）
    try:
        new_path, themes_count = _run_analyze_and_mark_backtest(
            template_id=template_id,
            trade_date=trade_date,
            news_start_dt=snap.news_start_dt,
            news_end_dt=snap.news_end_dt,
            snap_market_md=snap.market_summary_md,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        return _make_result(
            ok=False, snap_news_count=snap.news_count,
            error=f"analyze 失败: {type(exc).__name__}: {exc}",
        )

    return _make_result(
        ok=True, snap_news_count=snap.news_count,
        report_path=new_path, themes_count=themes_count,
    )


# ---------------------------------------------------------------------------
# 内部：analyze + mark backtest
# ---------------------------------------------------------------------------


def _run_analyze_and_mark_backtest(
    *,
    template_id: str,
    trade_date: str,
    news_start_dt: datetime,
    news_end_dt: datetime,
    snap_market_md: Optional[str],
    provider: Optional[str],
) -> tuple[Optional[str], int]:
    """调 AnalysisService 跑一次分析 + 重命名 + 标记 is_backtest=1。

    用 ``news_start_dt / news_end_dt`` 直接喂 ``AnalysisService.analyze``，
    避免重复算 start/end，保持与 snapshot 完全一致。

    Bug 修复（2026-05-27 15:20）：
        见 doc/design/05-27-1515-回测时间边界说明书.md §6.1
        - bug 1：路径不匹配 → 改用 ``res.report_id`` 做主键 UPDATE
        - bug 2：``report_date`` 未透传 → 显式覆盖为
          ``trade_date`` 转 ``YYYY-MM-DD``
        - bug 3：``file_path`` 改为相对 posix（对齐 db 存储格式）
        - bug 4：theme_predictions 也同步改 report_date / report_path

    Returns:
        ``(新 md 文件相对 posix 路径, theme_count)``；
        report_path 为 ``None`` 表示 analyze 失败
    """
    from services.analysis_service import AnalysisService
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import _to_relative_posix

    svc = AnalysisService()
    res = svc.analyze(
        source="curated",
        start=news_start_dt,
        end=news_end_dt,
        template_id=template_id,
        provider=provider,
        market_summary=snap_market_md,
        auto_market=False,
        extract_themes=True,
    )
    if not res.ok or not res.report_path:
        raise RuntimeError(res.error or "analyze 返回 ok=False")

    old_path_abs = str(res.report_path)
    old_path_rel = _to_relative_posix(old_path_abs)
    new_path_abs = _rename_to_backtest(old_path_abs, trade_date)
    new_path_rel = _to_relative_posix(new_path_abs)
    new_basename = Path(new_path_rel).name

    target_report_date = (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    )

    # 标记 is_backtest=1 + report_date 改 + file_path 改
    adb = get_ai_inference_db()
    with adb.connect() as conn:
        # ai_reports: 用 report_id 主键避开路径匹配脆弱性
        ar_rows = 0
        if res.report_id:
            cur = conn.execute(
                "UPDATE ai_reports SET "
                "  is_backtest = 1, "
                "  report_date = ?, "
                "  file_path = ? "
                "WHERE id = ?",
                (target_report_date, new_path_rel, res.report_id),
            )
            ar_rows = cur.rowcount
        else:
            cur = conn.execute(
                "UPDATE ai_reports SET "
                "  is_backtest = 1, "
                "  report_date = ?, "
                "  file_path = ? "
                "WHERE file_path = ?",
                (target_report_date, new_path_rel, old_path_rel),
            )
            ar_rows = cur.rowcount

        tp_cur = conn.execute(
            "UPDATE theme_predictions SET "
            "  is_backtest = 1, "
            "  report_date = ?, "
            "  report_path = ?, "
            "  report_id = ? "
            "WHERE report_path = ?",
            (
                target_report_date, new_path_rel,
                new_basename, old_path_rel,
            ),
        )
        tp_rows = tp_cur.rowcount

    _log.info(
        "backtest mark: ai_reports updated=%d "
        "theme_predictions updated=%d "
        "(report_id=%s old_rel=%s new_rel=%s)",
        ar_rows, tp_rows, res.report_id, old_path_rel, new_path_rel,
    )

    return new_path_rel, int(res.theme_count or 0)


def _has_existing_backtest(
    template_id: str, trade_date: str,
) -> bool:
    """检查是否已有同 (template, date) 的 backtest 记录。

    注意：``ai_reports.report_date`` 以 ``YYYY-MM-DD`` 格式存储，
    本函数接受 ``YYYYMMDD`` 入参，内部转换。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    target_report_date = (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    )
    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM ai_reports "
            "WHERE is_backtest = 1 "
            "  AND prompt_id = ? "
            "  AND report_date = ? "
            "LIMIT 1",
            (template_id, target_report_date),
        ).fetchone()
    return row is not None


def _delete_existing_backtest(
    template_id: str, trade_date: str,
) -> dict:
    """删除同 (prompt_id, report_date, is_backtest=1) 的所有遗物。

    本函数为「批量删 (prompt, date) 命中的所有 backtest 报告」的薄包装，
    内部循环调用 :func:`services.storage.ai_reports_store.delete_report`
    （单文件 DRY）。这样删除策略只有一处实现，GUI / CLI / 回测都走它。

    Args:
        template_id: prompt_id
        trade_date: YYYYMMDD（与 _has_existing_backtest 对齐，内部转 -）

    Returns:
        ``{"ai_reports": n1, "theme_predictions": n2, "md_files": n3,
          "paths": [rel_posix, ...]}``
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import delete_report

    target_report_date = (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    )
    adb = get_ai_inference_db()

    with adb.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM ai_reports "
            "WHERE is_backtest = 1 "
            "  AND prompt_id = ? AND report_date = ?",
            (template_id, target_report_date),
        ).fetchall()
        candidates: List[tuple[int, str]] = [
            (int(r["id"]), str(r["file_path"] or "")) for r in rows
        ]

    if not candidates:
        return {
            "ai_reports": 0, "theme_predictions": 0,
            "md_files": 0, "paths": [],
        }

    tp_n = ar_n = md_n = 0
    paths: List[str] = []
    for rid, fp in candidates:
        try:
            # 此处 allow_real=False 安全：上面 WHERE is_backtest=1
            # 已过滤，命中的不可能是真实日常
            res = delete_report(rid, allow_real=False, delete_md=True)
        except (ValueError, PermissionError) as exc:
            _log.warning(
                "_delete_existing_backtest 漏单 id=%d (%s): %s",
                rid, fp, exc,
            )
            continue
        ar_n += res.ai_reports_deleted
        tp_n += res.theme_predictions_deleted
        if res.md_file_deleted:
            md_n += 1
        if res.file_path:
            paths.append(res.file_path)

    _log.info(
        "delete_existing_backtest: prompt=%s date=%s "
        "ai_reports=%d theme_predictions=%d md_files=%d",
        template_id, trade_date, ar_n, tp_n, md_n,
    )
    return {
        "ai_reports": ar_n,
        "theme_predictions": tp_n,
        "md_files": md_n,
        "paths": paths,
    }


def _rename_to_backtest(old_path: str, trade_date: str) -> str:
    """物理重命名 md 文件，名字加 ``_backtest_{trade_date}`` 后缀。

    若 old_path 已经包含后缀则原样返回（幂等）。
    """
    p = Path(old_path)
    if not p.exists():
        return old_path
    suffix = f"_backtest_{trade_date}"
    if suffix in p.stem:
        return old_path
    new_name = f"{p.stem}{suffix}{p.suffix}"
    new_path = p.with_name(new_name)
    if new_path.exists():
        new_path.unlink()
    os.rename(p, new_path)
    return str(new_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_yyyymmddhhmm(s: str) -> datetime:
    """``202605221400`` → datetime(2026, 5, 22, 14, 0)。"""
    s = s.strip()
    if len(s) != 12 or not s.isdigit():
        raise SystemExit(
            f"时间格式应为 12 位 YYYYMMDDHHMM，得到 {s!r}"
        )
    return datetime.strptime(s, "%Y%m%d%H%M")


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backtest_prompt",
        description="模板虚拟回测：在过去某天的指定时间窗下跑 AI 分析",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--template", required=True,
        help="prompt_id 或多个用逗号分隔，如 custom_6 或 custom_6,custom_7",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date", help="单日 YYYYMMDD")
    grp.add_argument(
        "--date-range",
        help="区间 YYYYMMDD..YYYYMMDD（自动跳过非交易日）",
    )
    p.add_argument(
        "--news-start", default=None,
        help="新闻窗左边界 YYYYMMDDHHMM；不传 = trade_date 14:00",
    )
    p.add_argument(
        "--news-end", default=None,
        help=(
            "新闻窗右边界 YYYYMMDDHHMM；不传 = next_trade_date 09:00"
            "（也是硬上限）"
        ),
    )
    p.add_argument(
        "--all-news", action="store_true",
        help="不过滤 clean_status（含 rejected/pending）；默认仅 curated",
    )
    p.add_argument(
        "--provider", default=None,
        help="LLM provider（如 deepseek/qwen），不传走默认",
    )
    p.add_argument("--workers", type=int, default=1,
                   help="并发 worker 数（默认 1 = 串行）")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖已存在的 backtest md")
    p.add_argument("--dry-run", action="store_true",
                   help="仅重建 snapshot 不调 LLM")
    return p.parse_args(argv)


def _resolve_dates(args: argparse.Namespace) -> List[str]:
    if args.date:
        return [args.date]
    start, _sep, end = args.date_range.partition("..")
    if not (start and end):
        raise SystemExit(
            f"--date-range 格式应为 YYYYMMDD..YYYYMMDD: {args.date_range}"
        )
    try:
        return trade_dates_between(
            start, end, client=TushareClient(),
        )
    except TradeDateError as exc:
        raise SystemExit(f"解析交易日失败: {exc}")


def _resolve_templates(args: argparse.Namespace) -> List[str]:
    return [t.strip() for t in args.template.split(",") if t.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    templates = _resolve_templates(args)
    dates = _resolve_dates(args)
    tasks = [(t, d) for t in templates for d in dates]

    news_start_dt = (
        _parse_yyyymmddhhmm(args.news_start) if args.news_start else None
    )
    news_end_dt = (
        _parse_yyyymmddhhmm(args.news_end) if args.news_end else None
    )
    news_status = "" if args.all_news else "curated"

    print(
        f"[plan] templates={templates} dates={len(dates)} 天 "
        f"= {len(tasks)} 个回测任务 (workers={args.workers}) "
        f"news_status={news_status or 'all'} "
        f"dry_run={args.dry_run}"
    )
    if news_start_dt or news_end_dt:
        print(
            f"  manual_window start={args.news_start or '默认'} "
            f"end={args.news_end or '默认'}"
        )

    results: List[BacktestResult] = []

    def _run(t, d) -> BacktestResult:
        return backtest_one(
            t, d,
            news_start_dt=news_start_dt,
            news_end_dt=news_end_dt,
            news_status=news_status,
            provider=args.provider,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )

    t_all = time.perf_counter()
    if args.workers <= 1:
        for t, d in tasks:
            res = _run(t, d)
            results.append(res)
            _print_result(res)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map = {
                pool.submit(_run, t, d): (t, d)
                for t, d in tasks
            }
            for fut in as_completed(future_map):
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    t, d = future_map[fut]
                    res = BacktestResult(
                        ok=False, template_id=t, trade_date=d,
                        news_start_iso="", news_end_iso="",
                        news_status=news_status or "all",
                        snapshot_news_count=0,
                        report_path=None, themes_count=0,
                        elapsed_ms=0,
                        error=f"worker 异常: {exc}\n"
                              + traceback.format_exc(limit=2),
                    )
                results.append(res)
                _print_result(res)

    ok_n = sum(1 for r in results if r.ok and not r.skipped)
    skip_n = sum(1 for r in results if r.skipped)
    fail_n = sum(1 for r in results if not r.ok)
    print(
        f"\n[summary] {len(results)} 任务: "
        f"成功 {ok_n} / 跳过 {skip_n} / 失败 {fail_n} "
        f"总耗时 {time.perf_counter() - t_all:.1f}s"
    )
    if fail_n:
        return 1
    return 0


def _print_result(res: BacktestResult) -> None:
    flag = ""
    if res.skipped:
        flag = " (skipped)"
        if res.error:
            flag += f": {res.error}"
    elif not res.ok:
        flag = f" | error: {res.error}"
    if res.overwritten:
        flag += (
            f" [overwritten: -ai_reports={res.deleted_reports} "
            f"-theme_predictions={res.deleted_themes} "
            f"-md_files={res.deleted_md_files}]"
        )
    win = f"[{res.news_start_iso[5:16]}, {res.news_end_iso[5:16]})"
    print(
        f"  [{'OK' if res.ok else 'FAIL'}] {res.template_id} @ "
        f"{res.trade_date} win={win} status={res.news_status} "
        f"news={res.snapshot_news_count} themes={res.themes_count} "
        f"elapsed={res.elapsed_ms}ms{flag}"
    )


if __name__ == "__main__":
    sys.exit(main())
