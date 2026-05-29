"""按 ai_reports.id 精确取该报告下所有题材 CLI（service 薄壳）。

按 ``.cursor/rules/cli-first-development.mdc`` 要求，给评估页双击联动到题材
页用到的 ``ThemeStore.get_by_ai_report_id`` 配套 CLI 入口，便于独立验证
+ 未来 web 化时直接映射为 ``GET /reports/{id}/themes``。

数据流::

    ai_reports.id
        → JOIN theme_predictions ON file_path = report_path
        → 展开 theme_stocks / theme_news

用法
----

按报告 id 查题材（人类可读）::

    python tools/query_themes_by_report.py 443

JSON 输出（脚本可消费 / 未来 API 响应体雏形）::

    python tools/query_themes_by_report.py 443 --json

只看前 N 条::

    python tools/query_themes_by_report.py 443 --limit 3

退出码::

    0 成功（含空结果）
    1 该 report_id 下无题材（rows 为空）
    2 参数错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.storage import get_theme_store  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query_themes_by_report",
        description=(
            "按 ai_reports.id 精确取该报告下所有题材"
            "（含关联标的/新闻）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "ai_report_id", type=int,
        help="ai_reports.id（int），评估页表内可见",
    )
    p.add_argument(
        "--json", action="store_true",
        help="以 JSON 输出（包含 stocks / news 子列表）",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="只展示前 N 条（0 = 全部）",
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.ai_report_id <= 0:
        print("[错误] ai_report_id 必须为正整数", file=sys.stderr)
        return 2

    themes = get_theme_store().get_by_ai_report_id(args.ai_report_id)
    if args.limit > 0:
        themes = themes[: args.limit]

    if args.json:
        # 兼容 dataclass / Row：theme_store._expand 返回纯 dict，可直接序列化
        print(json.dumps(themes, ensure_ascii=False, indent=2, default=str))
        return 0 if themes else 1

    print("=" * 72)
    print(
        f"  ai_reports.id={args.ai_report_id}  "
        f"题材数={len(themes)}"
    )
    print("=" * 72)
    if not themes:
        print(
            "  （无题材。可能 report_id 不存在，"
            "或该报告未抽取过题材）"
        )
        return 1

    header = (
        f"  {'强度':>5}  {'等级':<6}  {'板块代码':<12}  {'题材名'}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for t in themes:
        sc = t.get("strength_score")
        sc_text = f"{int(sc):+d}" if sc is not None else "—"
        print(
            f"  {sc_text:>5}  "
            f"{(t.get('strength_level') or '—'):<6}  "
            f"{(t.get('sector_ts_code') or '—'):<12}  "
            f"{t.get('theme_name') or ''}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
