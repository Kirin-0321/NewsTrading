"""存量题材 sector_ts_code 回填 CLI。

业务定位
--------
2026-05-28 题材打分逻辑改造决策点 2.2·A：抽取层强制绑定。但历史 67 个
题材 sector_ts_code 是 NULL（matcher 老阈值 0.5 拦下了），需一次性补绑。

用法
----
::

    # DRY-RUN：列出每个待补题材的匹配候选 + 置信度，不写库
    python tools/backfill_theme_sector_code.py --dry-run

    # 实跑：用 matcher.force=True 给每个 NULL 题材强制绑板块
    python tools/backfill_theme_sector_code.py --apply

    # JSON 输出（机器可读）
    python tools/backfill_theme_sector_code.py --dry-run --json

退出码
------
* 0  全部回填成功
* 1  matcher 异常 / 部分题材匹配失败
* 2  参数错误
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _load_unbound_themes() -> List[Dict]:
    """读 ai_inference.db 里 sector_ts_code IS NULL 的题材。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, theme_name, theme_category, report_date, prompt_id "
            "FROM theme_predictions "
            "WHERE sector_ts_code IS NULL OR sector_ts_code = '' "
            "ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _load_sectors() -> List[Tuple[str, str, str]]:
    """读 market.db 里所有 dim_sector。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ts_code, name, src FROM dim_sector "
            "WHERE name IS NOT NULL"
        ).fetchall()
    return [(r[0], r[1], r[2] or "dc") for r in rows]


def _match_one(
    theme: Dict, sectors: List[Tuple[str, str, str]],
) -> Tuple[Optional[str], float, Optional[str]]:
    """对单题材跑一次 force=True 匹配，返回 (ts_code, conf, sector_name)。"""
    from services.scoring.matcher import match_sector_ts_code
    name = (theme.get("theme_name") or "").strip()
    if not name:
        return None, 0.0, None
    code, conf = match_sector_ts_code(
        name,
        theme_category=theme.get("theme_category"),
        sectors=sectors,
        force=True,
    )
    sec_name = None
    if code:
        for ts, n, _src in sectors:
            if ts == code:
                sec_name = n
                break
    return code, conf, sec_name


def _apply_one(theme_id: int, ts_code: str, conf: float) -> None:
    """UPDATE theme_predictions SET sector_ts_code/conf WHERE id=?"""
    from services.storage.ai_inference_db import get_ai_inference_db
    ai = get_ai_inference_db()
    with ai.connect() as conn:
        conn.execute(
            "UPDATE theme_predictions "
            "SET sector_ts_code = ?, sector_match_conf = ? "
            "WHERE id = ?",
            (ts_code, conf, theme_id),
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="存量题材 sector_ts_code 回填",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写库（默认仅 DRY-RUN 不写）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DRY-RUN 模式（与 --apply 互斥；缺省默认即 dry-run）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="结果输出为 JSON",
    )
    args = parser.parse_args()

    if args.apply and args.dry_run:
        print("ERROR: --apply 与 --dry-run 互斥", file=sys.stderr)
        return 2

    is_apply = args.apply

    themes = _load_unbound_themes()
    if not themes:
        msg = "没有未绑板块的题材，无需回填"
        if args.json:
            print(json.dumps({"ok": True, "msg": msg, "items": []},
                             ensure_ascii=False))
        else:
            print(msg)
        return 0

    sectors = _load_sectors()
    if not sectors:
        print("ERROR: dim_sector 为空，请先跑 sync_sector_daily", file=sys.stderr)
        return 1

    items: List[Dict] = []
    fail = 0
    for t in themes:
        code, conf, sec_name = _match_one(t, sectors)
        item = {
            "theme_id": t["id"],
            "theme_name": t["theme_name"],
            "theme_category": t.get("theme_category"),
            "report_date": t.get("report_date"),
            "matched_ts_code": code,
            "matched_sector_name": sec_name,
            "conf": conf,
        }
        items.append(item)
        if not code:
            fail += 1
            continue
        if is_apply:
            try:
                _apply_one(t["id"], code, conf)
                item["applied"] = True
            except Exception as exc:  # noqa: BLE001
                item["applied"] = False
                item["error"] = str(exc)
                fail += 1

    if args.json:
        print(json.dumps(
            {"ok": fail == 0, "total": len(themes), "fail": fail,
             "items": items, "applied": is_apply},
            ensure_ascii=False, indent=2,
        ))
    else:
        mode = "APPLY" if is_apply else "DRY-RUN"
        print(f"=== 存量题材 sector_ts_code 回填 [{mode}] ===")
        print(f"待补题材: {len(themes)} 个 / 字典板块: {len(sectors)} 个")
        print()
        # 分桶按 conf 高/中/低
        high = [i for i in items if i["conf"] >= 0.7]
        mid = [i for i in items if 0.3 <= i["conf"] < 0.7]
        low = [i for i in items if i["conf"] < 0.3]
        print(f"高 conf (>=0.7): {len(high)} | "
              f"中 conf (0.3~0.7): {len(mid)} | "
              f"低 conf (<0.3): {len(low)}")
        print()

        # 详细列出（按 conf 降序）
        items_sorted = sorted(items, key=lambda x: -x["conf"])
        for it in items_sorted:
            mark = "OK " if it["matched_ts_code"] else "MISS"
            applied_mark = ""
            if is_apply:
                applied_mark = (
                    " [applied]" if it.get("applied") else " [NOT applied]"
                )
            print(
                f"  [{mark}] id={it['theme_id']:>4d} "
                f"conf={it['conf']:.3f}  "
                f"{it['theme_name']!s:30s} -> "
                f"{it['matched_sector_name']!s:20s} "
                f"({it['matched_ts_code']}){applied_mark}"
            )

        print()
        print(f"=== 总计: {len(themes)} | 失败: {fail} ===")
        if not is_apply:
            print()
            print("DRY-RUN 模式，未写库。如要实际回填请加 --apply")

    return 1 if fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
