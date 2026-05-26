"""一次性脚本：用两个新写的「短线投机」prompt 模板各跑一次 AI 分析，对比效果。

调用关系::

    last_trading_close_14()  →  确定新闻起点（对齐 GUI 上「上一收盘日 14:00 至今」）
    get_cached_summary_md()  →  取 20260526 盘后总结
    AnalysisService.analyze()  ×  2  →  分别跑 speculator_scalper / speculator_data_driven
        └→ DeepSeek V4 + deep_thinking=True

输出
----
* 两份 MD 报告（路径在 data/ai_reports/ 自动生成，由 AnalysisService 决定）
* 终端总结表：模板/耗时/输出字符数/报告路径
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from gui.utils.market_db_helper import get_cached_summary_md
from services.analysis_service import AnalysisService


def last_trading_close_14() -> datetime:
    """对齐 gui/pages/ai_analysis_page.py::_last_trading_close_14。"""
    now = datetime.now()
    today_14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
    if now.weekday() < 5 and now >= today_14:
        return today_14
    d = (now - timedelta(days=1)).date()
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return datetime(d.year, d.month, d.day, 14, 0, 0)


def run_one(
    template_id: str,
    market_summary: str,
    start: datetime,
    end: datetime,
) -> dict:
    """跑一个模板，返回 {template_id, ok, elapsed, chars, report_path, error}。"""
    print(f"\n{'=' * 70}")
    print(f"  [Run] template_id = {template_id}")
    print(f"{'=' * 70}")

    last_msg = ""

    def cb(msg: str, is_streaming: bool = False) -> None:
        nonlocal last_msg
        if is_streaming:
            return  # 跳过 token-level 输出
        if msg == last_msg:
            return
        last_msg = msg
        # 截断超长 progress
        m = msg[:120] + ("…" if len(msg) > 120 else "")
        print(f"    · {m}")

    t0 = time.time()
    service = AnalysisService()
    result = service.analyze(
        source="curated",
        start=start,
        end=end,
        template_id=template_id,
        provider="deepseek",
        max_sectors="auto",
        stocks_per_sector="auto",
        max_news=None,
        market_summary=market_summary,
        extract_themes=False,
        enable_deep_thinking=True,
        progress_callback=cb,
    )
    elapsed = time.time() - t0

    out = {
        "template_id": template_id,
        "ok": result.ok,
        "elapsed_s": round(elapsed, 1),
        "chars": len(result.result_text or ""),
        "report_path": result.report_path or "",
        "news_count": result.news_count,
        "error": result.error or "",
    }
    print(
        f"\n    [Done] ok={result.ok}  elapsed={elapsed:.1f}s  "
        f"chars={out['chars']}  news={out['news_count']}"
    )
    if out["report_path"]:
        print(f"    [Report] {out['report_path']}")
    if out["error"]:
        print(f"    [Error] {out['error']}")
    return out


def main() -> int:
    trade_date = "20260526"
    print("=" * 70)
    print("  短线投机 prompt 双模板对比")
    print("=" * 70)

    market_md = get_cached_summary_md(trade_date)
    if not market_md:
        print(f"[FAIL] 未找到 {trade_date} 盘后总结缓存")
        return 1
    print(f"[OK] 盘后总结 {trade_date}: {len(market_md)} 字符")

    start = last_trading_close_14()
    end = datetime.now()
    print(f"[OK] 新闻区间: {start} ~ {end}（{(end - start).total_seconds() / 3600:.1f} h）")

    # 默认两个模板都跑；命令行加 ID 则只跑指定的
    if len(sys.argv) > 1:
        template_ids = tuple(sys.argv[1:])
    else:
        template_ids = ("speculator_scalper", "speculator_data_driven")

    results = []
    for tid in template_ids:
        try:
            results.append(run_one(tid, market_md, start, end))
        except Exception as exc:  # noqa: BLE001
            print(f"\n[EXCEPTION] {tid}: {exc}")
            import traceback
            traceback.print_exc()
            results.append({
                "template_id": tid,
                "ok": False,
                "elapsed_s": 0,
                "chars": 0,
                "report_path": "",
                "news_count": 0,
                "error": str(exc),
            })

    print("\n" + "=" * 70)
    print("  对比汇总")
    print("=" * 70)
    print(f"  {'模板 ID':<28} {'OK':>4} {'耗时(s)':>8} {'字符':>6}  报告路径")
    print("  " + "-" * 88)
    for r in results:
        ok = "✓" if r["ok"] else "×"
        path = r["report_path"][-50:] if r["report_path"] else "—"
        print(
            f"  {r['template_id']:<28} {ok:>4} {r['elapsed_s']:>8} "
            f"{r['chars']:>6}  …{path}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
