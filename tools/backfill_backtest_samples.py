"""Phase 6 Step 6.5 · 批量虚拟回测堆样本 CLI（2026-05-27 时间窗重构后）。

业务定位
--------
一次性给 v3.1 主人指定的 3 个核心模板跑过去 N 个交易日的虚拟回测，堆出
``N × 3`` 份样本作为「模板评估」的初始数据；跑完自动触发 D+1~D+5 打分。

时间窗约定
----------
**每个 trade_date 都用主人默认窗**：

* 左边界 = ``trade_date 14:00``
* 右边界 = ``next_trade_date(trade_date) 09:00``

批量场景不暴露逐日时间窗微调（避免参数爆炸）；若需细调请用
:mod:`tools.backtest_prompt` 单跑或 GUI「手动回测」页面。

复用关系
--------
::

    BUILTIN_TEMPLATES
       ↓
    trade_dates_between  ← services.market.trade_date
       ↓
    backtest_one × N×T   ← tools.backtest_prompt  (Phase 6.2)
       ↓
    rescore_range(...)   ← services.scoring.scoring_service  (Phase 2)
       ↓
    print_summary()

设计取舍
--------
* **不再造轮子**：所有具体回测逻辑都在 ``backtest_one``，本工具只做
  「日期范围解析 + 笛卡尔积 + 失败重试报告 + 跑完触发打分」。
* **today 永远排除**：今天的 ``next_open 09:00`` 必然在未来，回测窗不完整 →
  始终排除今天。
* **失败容错**：单个 (template, date) 失败不阻断整批；失败列表汇总到末尾。
* **打分触发**：默认跑完调一次 ``rescore_range``；``--skip-score`` 关闭。

用法
----
::

    # 默认：3 内置模板 × 14 交易日 × DeepSeek × curated 新闻
    python tools/backfill_backtest_samples.py

    # 自定义模板 + 天数 + 全部新闻
    python tools/backfill_backtest_samples.py \\
        --templates custom_6,custom_7 --days 7 --workers 2 --all-news

    # 不跑打分（仅堆 md 样本）
    python tools/backfill_backtest_samples.py --skip-score

    # 重跑已有样本（覆盖）
    python tools/backfill_backtest_samples.py --overwrite

    # dry-run：不调 LLM 不写 db，只看任务清单 + snapshot 通不通
    python tools/backfill_backtest_samples.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
from tools.backtest_prompt import (  # noqa: E402
    BacktestResult,
    backtest_one,
)

_log = logging.getLogger("backfill_backtest_samples")

# v3.1 主人指定的 3 个核心模板
BUILTIN_TEMPLATES: List[str] = [
    "custom_1770292858",  # 三位一体综合分析_2月5日
    "custom_6",           # 新闻分析
    "custom_7",           # 超级综合模板
]


# ---------------------------------------------------------------------------
# 日期范围解析
# ---------------------------------------------------------------------------


def _resolve_target_dates(
    days: int,
    *,
    client: TushareClient,
) -> List[str]:
    """取过去 ``days`` 个交易日（**始终排除今天**）。

    今天的 ``next_open 09:00`` 必然在未来 → backtest_one 会拒掉 → 直接跳过。
    """
    today_dt = datetime.now()
    today_str = today_dt.strftime("%Y%m%d")
    earliest = (
        today_dt - timedelta(days=days + 21)
    ).strftime("%Y%m%d")

    try:
        all_open = trade_dates_between(earliest, today_str, client=client)
    except TradeDateError as exc:
        raise SystemExit(f"解析交易日历失败: {exc}")

    all_open = [d for d in all_open if d != today_str]

    if len(all_open) < days:
        _log.warning(
            "区间内只有 %d 个交易日 < 期望 %d 个；按实际数量回测",
            len(all_open), days,
        )
        return all_open
    return all_open[-days:]


def _resolve_templates(arg: Optional[str]) -> List[str]:
    if not arg or arg.strip().lower() == "builtin":
        return list(BUILTIN_TEMPLATES)
    return [t.strip() for t in arg.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------


def _run_batch(
    tasks: List[tuple],
    *,
    news_status: str,
    provider: Optional[str],
    overwrite: bool,
    dry_run: bool,
    workers: int,
) -> List[BacktestResult]:
    """跑批；workers<=1 走串行避免 SQLite 锁。"""
    results: List[BacktestResult] = []

    def _exec(t, d) -> BacktestResult:
        return backtest_one(
            t, d,
            news_status=news_status,
            provider=provider,
            overwrite=overwrite,
            dry_run=dry_run,
        )

    if workers <= 1:
        for t, d in tasks:
            res = _exec(t, d)
            results.append(res)
            _print_one(res)
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_exec, t, d): (t, d) for t, d in tasks
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
                    report_path=None, themes_count=0, elapsed_ms=0,
                    error=(
                        f"worker 异常: {type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=2)
                    ),
                )
            results.append(res)
            _print_one(res)
    return results


def _print_one(res: BacktestResult) -> None:
    flag = ""
    if res.skipped:
        flag = " (skipped)"
    elif not res.ok:
        flag = f" | error: {res.error}"
    print(
        f"  [{'OK' if res.ok else 'FAIL'}] {res.template_id} @ "
        f"{res.trade_date} news={res.snapshot_news_count} "
        f"themes={res.themes_count} {res.elapsed_ms}ms{flag}"
    )


# ---------------------------------------------------------------------------
# 跑完触发打分
# ---------------------------------------------------------------------------


def _trigger_scoring(
    dates: List[str], *, hit_threshold_pct: float,
) -> Optional[dict]:
    """跑完批量后立刻 rescore_range，让 D+1~D+5 都算上。

    Returns:
        rescore_range 返回的 batch 统计；失败返回 None。
    """
    if not dates:
        return None
    from services.scoring.scoring_service import rescore_range
    print(
        f"\n[score] 触发 rescore_range "
        f"[{dates[0]}, {dates[-1]}] D+1~D+5 ..."
    )
    try:
        t0 = time.perf_counter()
        result = rescore_range(
            dates[0], dates[-1],
            hit_threshold_pct=hit_threshold_pct,
        )
        elapsed = time.perf_counter() - t0
        print(
            f"  [OK] themes_total={result.themes_total} "
            f"themes_scored={result.themes_scored} "
            f"pairs={result.pairs_succeeded}/{result.pairs_attempted} "
            f"days={result.days_covered} elapsed={elapsed:.1f}s"
        )
        return {
            "themes_total": result.themes_total,
            "themes_scored": result.themes_scored,
            "pairs_succeeded": result.pairs_succeeded,
            "pairs_attempted": result.pairs_attempted,
            "days_covered": result.days_covered,
            "elapsed_s": round(elapsed, 1),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] rescore_range 异常: {exc}")
        return None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _print_summary(
    results: List[BacktestResult],
    *,
    elapsed_s: float,
    dates: List[str],
    templates: List[str],
    score_stat: Optional[dict],
) -> int:
    ok_n = sum(1 for r in results if r.ok and not r.skipped)
    skip_n = sum(1 for r in results if r.skipped)
    fail_n = sum(1 for r in results if not r.ok)

    print("\n" + "=" * 64)
    print(
        f"[summary] {len(results)} 任务 "
        f"({len(templates)} 模板 × {len(dates)} 天) | "
        f"成功 {ok_n} / 跳过 {skip_n} / 失败 {fail_n}"
    )
    print(f"  总耗时 {elapsed_s:.1f}s")
    if score_stat:
        print(
            f"  打分：题材 {score_stat['themes_scored']}/"
            f"{score_stat['themes_total']} "
            f"(对 {score_stat['pairs_succeeded']}/"
            f"{score_stat['pairs_attempted']}, "
            f"{score_stat['days_covered']} 天, "
            f"{score_stat['elapsed_s']}s)"
        )
    if fail_n:
        print(f"\n[failures] {fail_n} 个任务失败：")
        for r in results:
            if not r.ok:
                print(
                    f"  - {r.template_id}@{r.trade_date}: {r.error}"
                )

    print("\n[per-template]")
    for tmpl in templates:
        ok_per = sum(
            1 for r in results
            if r.template_id == tmpl and r.ok and not r.skipped
        )
        skip_per = sum(
            1 for r in results
            if r.template_id == tmpl and r.skipped
        )
        fail_per = sum(
            1 for r in results
            if r.template_id == tmpl and not r.ok
        )
        print(
            f"  {tmpl}: 新增 {ok_per} / 跳过 {skip_per} / "
            f"失败 {fail_per}"
        )
    print("=" * 64)

    return 0 if fail_n == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backfill_backtest_samples",
        description="批量虚拟回测堆样本（默认 3 模板 × 14 天 = 42 次 LLM）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--templates", default=None,
        help=(
            "prompt_id 逗号分隔，或 'builtin' 用 BUILTIN_TEMPLATES (默认)；"
            "如 --templates custom_6,custom_7"
        ),
    )
    p.add_argument(
        "--days", type=int, default=14,
        help="回测过去 N 个交易日（默认 14；自动跳过今天）",
    )
    p.add_argument(
        "--all-news", action="store_true",
        help="不过滤 clean_status（含 rejected/pending）；默认仅 curated",
    )
    p.add_argument(
        "--provider", default="deepseek",
        help="LLM provider，默认 deepseek",
    )
    p.add_argument(
        "--workers", type=int, default=4,
        help="并发 worker 数（默认 4；建议 ≤ 4 避免 API 限频）",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已存在的 backtest md（默认跳过）",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="不调 LLM 不打分，只看任务清单 + snapshot 通不通",
    )
    p.add_argument(
        "--skip-score", action="store_true",
        help="跑完不触发打分（默认会调 rescore_range）",
    )
    p.add_argument(
        "--hit-threshold-pct", type=float, default=3.0,
        help="打分用涨幅阈值（%%），默认 3.0",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    templates = _resolve_templates(args.templates)
    if not templates:
        raise SystemExit("[abort] 解析后无可用模板")

    client = TushareClient()
    dates = _resolve_target_dates(args.days, client=client)
    if not dates:
        raise SystemExit("[abort] 解析后无可用交易日")

    news_status = "" if args.all_news else "curated"
    tasks = [(t, d) for t in templates for d in dates]

    print("=" * 64)
    print(
        f"[plan] templates={templates} ({len(templates)} 个) "
        f"dates={dates[0]}..{dates[-1]} ({len(dates)} 天) "
        f"= {len(tasks)} 任务"
    )
    print(
        f"  news_window=主人默认 (14:00 → next_open 09:00) "
        f"news_status={news_status or 'all'} "
        f"workers={args.workers} provider={args.provider} "
        f"dry_run={args.dry_run} skip_score={args.skip_score}"
    )
    print("=" * 64)

    t_all = time.perf_counter()
    results = _run_batch(
        tasks,
        news_status=news_status,
        provider=args.provider,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        workers=args.workers,
    )

    score_stat = None
    if not args.dry_run and not args.skip_score:
        ok_dates = sorted({
            r.trade_date for r in results
            if r.ok and not r.skipped
        })
        if ok_dates:
            score_stat = _trigger_scoring(
                ok_dates,
                hit_threshold_pct=args.hit_threshold_pct,
            )

    elapsed_s = time.perf_counter() - t_all
    return _print_summary(
        results,
        elapsed_s=elapsed_s,
        dates=dates,
        templates=templates,
        score_stat=score_stat,
    )


if __name__ == "__main__":
    sys.exit(main())
