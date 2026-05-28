"""一次性清空打分数据 CLI（2026-05-29 算法重设上线辅助）。

用途
----
打分算法 / 命中率口径变更上线时，旧口径打分行必须全部清空避免
新旧口径混算。本工具调用 ``services.scoring.scoring_service.purge_scoring_data``，
不动 ``theme_predictions`` / ``ai_reports`` / ``theme_stocks`` 三张表——
题材抽取数据完好保留，调度器跑下次打分时按新口径自动重写。

用法
----
::

    # 干跑（只统计待删行数，不动 DB）
    python tools/purge_scoring_data.py --dry-run

    # 真删（先备份再清空 + 文本摘要）
    python tools/purge_scoring_data.py

    # 真删 + JSON 输出
    python tools/purge_scoring_data.py --json

退出码
------
* ``0`` 成功
* ``1`` 业务失败（备份失败 / DB 异常）
* ``2`` 参数错误

注意
----
* 真删模式会先把当前 ``data/ai_inference.db`` 拷贝到
  ``data/backups/ai_inference.db.purge_scoring.{时间戳}.bak``，
  万一删错可手动复原
* 跑完后下一次调度（每日 17:00）会按新口径自动重新打分；
  也可手动跑 ``tools/rescore_unfinished.py`` 立即补打
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "整库清空打分数据（theme_prediction_scores + "
            "theme_stock_scores）。"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计待删行数，不实际删除（推荐先跑一次确认数量）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="结构化 JSON 输出（字段与 PurgeResult dataclass 一致）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过确认提示，直接真删（CI 场景用，本地慎用）",
    )

    try:
        args = parser.parse_args()
    except SystemExit:
        return 2

    from services.scoring.scoring_service import purge_scoring_data

    # 真删前确认（除非 --yes）
    if not args.dry_run and not args.yes:
        try:
            ans = input(
                "⚠️  即将清空 theme_prediction_scores + theme_stock_scores"
                "（已自动备份）。\n"
                "    输入 yes 继续，其他任意键取消：\n> "
            )
        except (EOFError, KeyboardInterrupt):
            print("已取消。")
            return 0
        if ans.strip().lower() != "yes":
            print("已取消。")
            return 0

    try:
        result = purge_scoring_data(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        err = f"清空失败：{type(exc).__name__}: {exc}"
        if args.json:
            print(json.dumps({"ok": False, "error": err},
                             ensure_ascii=False))
        else:
            print(err, file=sys.stderr)
        return 1

    if args.json:
        out = asdict(result)
        out["ok"] = True
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if args.dry_run:
            print(
                f"[DRY-RUN] 待删 theme_prediction_scores: "
                f"{result.rows_main} 行 / "
                f"theme_stock_scores: {result.rows_detail} 行"
            )
            print("（未实际删除，加 --yes 或省略 --dry-run 真删）")
        else:
            print(
                f"✅ 已清空：theme_prediction_scores={result.rows_main} 行，"
                f"theme_stock_scores={result.rows_detail} 行"
            )
            print(f"📦 备份文件：{result.backup_path}")
            print(
                "👉 下次调度（每日 17:00）会按新口径重新打分；"
                "可手动跑 tools/rescore_unfinished.py 立即补打。"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
