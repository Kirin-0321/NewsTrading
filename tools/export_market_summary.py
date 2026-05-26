"""盘后总结导出 CLI（Phase M6）。

**纯导出工具**：不调 Tushare、不调 AI、不写 DB。从 ``market_summaries`` 表
读已缓存的 ``summary_json``，用**最新 renderer**重新渲染成 Markdown 输出。

适合：
- 改 ``services/market/renderer.py`` 后立刻验证渲染效果（无需重跑 build）
- 把某日 / 多日的 MD 批量喂给 AI 分析
- 主人脚本化生成 / 发邮件 / 存档

用法
----

最近一个有缓存的交易日，stdout 输出 Markdown::

    python tools/export_market_summary.py

指定日 + 同时存文件::

    python tools/export_market_summary.py 20260526 --save data/exports/20260526.md

只输出 MD 主体（去掉 CLI 头部 / KPI 速览），适合 ``>`` 重定向::

    python tools/export_market_summary.py --quiet > out.md

输出 JSON 而非 Markdown::

    python tools/export_market_summary.py 20260526 --json --save out.json

同时输出 MD + JSON（save 路径加 .json/.md 后缀）::

    python tools/export_market_summary.py 20260526 --both --save data/exports/20260526

用 DB 里缓存的 summary_md（不重渲染，便于对比新旧版渲染）::

    python tools/export_market_summary.py 20260526 --use-cached-md

批量导出最近 N 天到目录::

    python tools/export_market_summary.py --recent 7 --save-dir data/exports/

退出码
------

0  成功
1  指定日无缓存（提示主人先跑 ``market_fetch_backfill.py``）
2  参数错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.market.market_db import get_market_db  # noqa: E402
from services.market.renderer import MarketSummaryRenderer  # noqa: E402
from services.market.service import MarketSummaryService  # noqa: E402


_log = logging.getLogger("export_market_summary")


def _list_cached_dates(limit: int = 30) -> List[str]:
    """列出 market_summaries 表里最近 N 个有缓存的交易日（DESC）。"""
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date FROM market_summaries "
            "ORDER BY trade_date DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _render_md(
    summary_json: dict,
    *,
    use_cached_md: bool,
    cached_md: Optional[str],
) -> str:
    """从 summary_json 渲染 MD；优先用 cached_md（如 use_cached_md=True）。"""
    if use_cached_md and cached_md:
        return cached_md
    return MarketSummaryRenderer().render_compact(summary_json)


def _print_header(result, args: argparse.Namespace) -> None:
    """打印 CLI 头部（quiet 模式跳过）。"""
    if args.quiet:
        return
    print("=" * 72, file=sys.stderr)
    print(f"  导出 {result.trade_date} 盘后总结", file=sys.stderr)
    print("  数据来源    : market_summaries 缓存", file=sys.stderr)
    mode_text = (
        "cached_md（DB 缓存）" if args.use_cached_md
        else "live renderer（重渲染）"
    )
    print(f"  渲染模式    : {mode_text}", file=sys.stderr)
    print(f"  完整度      : {result.completeness:.2%}",
          file=sys.stderr)
    print(f"  build 模式  : {result.mode}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)


def _export_single(
    args: argparse.Namespace,
    td: str,
    svc: MarketSummaryService,
    *,
    save_to: Optional[Path] = None,
) -> int:
    """导出单日。返回退出码（0/1）。"""
    result = svc.get(td)
    if not result.ok or not result.summary_json:
        msg = (
            f"[错误] {td} 在 market_summaries 表中无缓存。\n"
            f"  先跑: python tools/market_fetch_backfill.py "
            f"--start {td} --end {td} --force-refresh --top-n 20"
        )
        print(msg, file=sys.stderr)
        return 1

    md = _render_md(
        result.summary_json,
        use_cached_md=args.use_cached_md,
        cached_md=result.summary_md,
    )
    js = json.dumps(
        result.summary_json, ensure_ascii=False, indent=2, default=str
    )

    _print_header(result, args)

    # stdout 输出
    if not save_to:
        if args.json:
            print(js)
        elif args.both:
            print("\n```markdown")
            print(md)
            print("```\n")
            print("```json")
            print(js)
            print("```")
        else:
            print(md)
        return 0

    # 文件输出
    save_to.parent.mkdir(parents=True, exist_ok=True)
    if args.both:
        md_path = save_to.with_suffix(".md")
        json_path = save_to.with_suffix(".json")
        md_path.write_text(md, encoding="utf-8")
        json_path.write_text(js, encoding="utf-8")
        if not args.quiet:
            print(f"[保存] MD   → {md_path}", file=sys.stderr)
            print(f"[保存] JSON → {json_path}", file=sys.stderr)
    elif args.json:
        save_to.write_text(js, encoding="utf-8")
        if not args.quiet:
            print(f"[保存] JSON → {save_to}", file=sys.stderr)
    else:
        save_to.write_text(md, encoding="utf-8")
        if not args.quiet:
            print(f"[保存] MD   → {save_to}", file=sys.stderr)
    return 0


def _export_recent(
    args: argparse.Namespace,
    svc: MarketSummaryService,
    save_dir: Path,
) -> int:
    """批量导出最近 N 天到目录。"""
    dates = _list_cached_dates(limit=args.recent)
    if not dates:
        print("[错误] market_summaries 表无任何缓存", file=sys.stderr)
        return 1
    print(f"[批量导出] {len(dates)} 个交易日 → {save_dir}",
          file=sys.stderr)
    save_dir.mkdir(parents=True, exist_ok=True)
    fail = 0
    for td in dates:
        ext = "json" if args.json else "md"
        save_to = save_dir / f"{td}.{ext}"
        rc = _export_single(args, td, svc, save_to=save_to)
        if rc != 0:
            fail += 1
    print(f"[完成] 成功 {len(dates) - fail} / 失败 {fail}",
          file=sys.stderr)
    return 0 if fail == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export_market_summary",
        description="从 market_summaries 缓存读盘后数据 + 用最新 renderer 导出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "trade_date", nargs="?", default=None,
        help="交易日 YYYYMMDD；省略时取最近一个有缓存的日（与 --recent 互斥）",
    )
    p.add_argument(
        "--recent", type=int, default=None,
        help="批量导出最近 N 个有缓存的交易日（需配 --save-dir）",
    )
    p.add_argument(
        "--save", type=str, default=None,
        help="单日：保存到该文件路径（与 --both 配合时作前缀，自动加 .md/.json）",
    )
    p.add_argument(
        "--save-dir", type=str, default=None,
        help="批量：保存到该目录（文件名 = {trade_date}.{md|json}）",
    )
    p.add_argument(
        "--json", action="store_true",
        help="输出 JSON 而非 Markdown",
    )
    p.add_argument(
        "--both", action="store_true",
        help="同时输出 Markdown + JSON（stdout 模式用代码块包裹）",
    )
    p.add_argument(
        "--use-cached-md", action="store_true",
        help="用 DB 里的 summary_md（不重渲染）；适合对比新旧 renderer",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="去掉 CLI 头部 / 保存提示（stdout 仅 MD/JSON 内容）",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG 级别日志",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.json and args.both:
        print("[错误] --json 与 --both 互斥", file=sys.stderr)
        return 2
    if args.recent is not None and args.trade_date:
        print("[错误] --recent 与 位置参数 trade_date 互斥", file=sys.stderr)
        return 2
    if args.recent is not None and not args.save_dir:
        print("[错误] --recent 需要配 --save-dir", file=sys.stderr)
        return 2

    svc = MarketSummaryService()

    # 批量模式
    if args.recent is not None:
        return _export_recent(args, svc, Path(args.save_dir))

    # 单日模式
    td = args.trade_date
    if not td:
        dates = _list_cached_dates(limit=1)
        if not dates:
            print("[错误] market_summaries 表无任何缓存。先跑 "
                  "market_fetch_backfill.py", file=sys.stderr)
            return 1
        td = dates[0]
        if not args.quiet:
            print(f"[自动选日] 最近缓存日: {td}", file=sys.stderr)
    elif not (len(td) == 8 and td.isdigit()):
        print(f"[错误] trade_date 格式应为 YYYYMMDD，实际: {td!r}",
              file=sys.stderr)
        return 2

    save_to = Path(args.save) if args.save else None
    return _export_single(args, td, svc, save_to=save_to)


if __name__ == "__main__":
    sys.exit(main())
