"""按 theme_ids 列表批量取聚合打分 CLI（service 薄壳）。

按 ``.cursor/rules/cli-first-development.mdc`` 要求，给题材预测页筛选条
用到的 ``scoring_service.get_score_aggregates_by_theme_ids`` 配套 CLI 入口。

业务关系
--------
题材预测页 (v3 筛选条) 在主表渲染前调本接口一次性拉回 ``hit_rate_avg
/ direction_correct / alpha_avg / scored_pairs``，注入到 theme dict，
让"命中率下限 / 方向准确性 / α"三个筛选维度可用。

用法
----

按 id 列表查（人类可读）::

    python tools/query_theme_aggregates.py 1003918,1003920,1003922

JSON 输出（脚本可消费 / 未来 API 响应体雏形）::

    python tools/query_theme_aggregates.py 1003918,1003920 --json

退出码::

    0 成功（含空结果——传了 id 但无打分行）
    1 整个映射为空（参数解析失败 / 都未打分）
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

from services.scoring.scoring_service import (  # noqa: E402
    get_score_aggregates_by_theme_ids,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="query_theme_aggregates",
        description=(
            "按 theme_predictions.id 列表批量取聚合打分"
            "（命中率/累计方向/α/已打分天数）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "theme_ids",
        help="theme_id 列表，逗号分隔，如 1003918,1003920,1003922",
    )
    p.add_argument(
        "--json", action="store_true",
        help="以 JSON 输出（key 为 theme_id 字符串，value 为聚合 dict）",
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        ids = [int(t.strip()) for t in args.theme_ids.split(",") if t.strip()]
    except ValueError:
        print("[错误] theme_ids 解析失败，需为逗号分隔的整数", file=sys.stderr)
        return 2
    if not ids:
        print("[错误] theme_ids 不能为空", file=sys.stderr)
        return 2

    agg = get_score_aggregates_by_theme_ids(ids)

    if args.json:
        # 把 int key 转 str 以便 JSON 兼容
        payload = {str(k): v for k, v in agg.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if agg else 1

    print("=" * 72)
    print(f"  传入 {len(ids)} 个 theme_id，命中打分 {len(agg)} 个")
    print("=" * 72)
    if not agg:
        print("  （全部未打分）")
        return 1

    header = (
        f"  {'theme_id':>10}  {'命中率':>7}  {'方向':<6}  "
        f"{'α均值':>8}  {'已打分':>6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for tid in ids:
        rec = agg.get(tid)
        if rec is None:
            print(
                f"  {tid:>10}  {'—':>7}  {'未打分':<6}  "
                f"{'—':>8}  {'0':>6}"
            )
            continue
        hr = rec.get("hit_rate_avg")
        dc = rec.get("direction_correct")
        al = rec.get("alpha_avg")
        sp = rec.get("scored_pairs") or 0
        hr_t = f"{hr * 100:.0f}%" if hr is not None else "—"
        dc_t = "✓" if dc == 1 else ("✗" if dc == 0 else "—")
        al_t = f"{al:+.2f}%" if al is not None else "—"
        print(
            f"  {tid:>10}  {hr_t:>7}  {dc_t:<6}  "
            f"{al_t:>8}  {sp:>6}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
