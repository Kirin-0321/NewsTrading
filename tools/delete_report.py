"""单报告删除 CLI（评估页「删除」按钮的 CLI 对位实现）。

业务定位
--------
按 ``ai_reports.id`` 级联删除一份报告及其所有遗物：

* ``ai_reports`` 1 行
* ``theme_predictions`` N 行（同 ``report_path``）
  → CASCADE 干掉 ``theme_stocks`` / ``theme_news`` /
  ``theme_prediction_scores`` / ``theme_stock_scores``
* ``data/AI_analysis/{file_path}.md`` 物理文件（可关 ``--no-md``）

防误删
------
默认只允许删 ``is_backtest=1``（虚拟回测产物）。要删真实日常报告
（``is_backtest=0``）必须显式传 ``--allow-real``，否则后端抛
:class:`PermissionError` 拒绝。

用法
----
::

    # 列出全部报告找 ID
    python tools/delete_report.py --list

    # 删 backtest 报告（id=96）
    python tools/delete_report.py --report-id 96

    # 预演（dry-run，只看不删）
    python tools/delete_report.py --report-id 96 --dry-run

    # 删真实日常报告（需显式 --allow-real）
    python tools/delete_report.py --report-id 11 --allow-real

    # 只删 db 行，保留 md 文件
    python tools/delete_report.py --report-id 96 --no-md

    # 批量删多个 id（逗号分隔）
    python tools/delete_report.py --report-id 96,97,98

退出码
------
* 0 全部成功
* 1 至少一项失败（含真实日常被拒）
* 2 参数错误 / 报告不存在
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
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.storage.ai_inference_db import (  # noqa: E402
    get_ai_inference_db,
)
from services.storage.ai_reports_store import (  # noqa: E402
    DeleteResult, delete_report,
)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="delete_report",
        description=(
            "按 ai_reports.id 级联删除一份报告（ai_reports + "
            "theme_predictions(+CASCADE) + .md 文件）"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--report-id",
        help=(
            "ai_reports.id；支持逗号分隔批量（如 96,97,98），二选一"
        ),
    )
    grp.add_argument(
        "--list", action="store_true",
        help="列出全部 ai_reports 找 ID（二选一）",
    )

    p.add_argument(
        "--allow-real", action="store_true",
        help=(
            "允许删 is_backtest=0 的真实日常报告（默认拒绝，"
            "需显式传该 flag）"
        ),
    )
    p.add_argument(
        "--no-md", action="store_true",
        help="只删 db 行，保留 .md 物理文件（默认同步删 md）",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="预演模式：只查会删什么，不动数据",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过交互确认（脚本/CI 用，否则交互式问 y/N）",
    )
    p.add_argument("--json", action="store_true",
                   help="JSON 输出（机器可读）")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# list 子命令
# ---------------------------------------------------------------------------


def _list_reports() -> int:
    """复用 score_one_report 的格式列出（id / date / BT / themes / prompt）。"""
    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ar.id, ar.report_date, ar.is_backtest, "
            "       ar.prompt_id, ar.file_path, "
            "       (SELECT COUNT(*) FROM theme_predictions tp "
            "        WHERE tp.report_path = ar.file_path) AS tp_n "
            "FROM ai_reports ar "
            "ORDER BY ar.report_date DESC, ar.id DESC"
        ).fetchall()

    print(f"{'id':>4}  {'date':<11} {'BT':<3} {'themes':<6} "
          f"prompt_id / file")
    print("-" * 100)
    for r in rows:
        d = dict(r)
        bt = "yes" if d["is_backtest"] else "no"
        fname = Path(str(d["file_path"] or "")).name
        print(
            f"{d['id']:>4}  {d['report_date']:<11} {bt:<3} "
            f"{int(d['tp_n']):<6} {d['prompt_id']} / {fname}"
        )
    return 0


# ---------------------------------------------------------------------------
# 删除主流程
# ---------------------------------------------------------------------------


def _parse_ids(raw: str) -> List[int]:
    out: List[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            v = int(token)
        except ValueError:
            raise SystemExit(f"--report-id 含非整数 token: {token!r}")
        if v <= 0:
            raise SystemExit(f"--report-id 必须为正整数: {v}")
        out.append(v)
    if not out:
        raise SystemExit("--report-id 解析后为空")
    return out


def _confirm_interactive(ids: List[int], allow_real: bool) -> bool:
    flag = " + --allow-real" if allow_real else ""
    prompt = (
        f"\n[确认] 将删除 {len(ids)} 份报告 (ids={ids}){flag}\n"
        f"  这不可逆：会同步删 theme_predictions + 子表 + .md 文件\n"
        f"  继续？(y/N): "
    )
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _print_result(res: DeleteResult, *, fail_reason: Optional[str] = None) -> None:
    tag = "DRY" if res.dry_run else ("OK" if res.ok else "FAIL")
    bt_txt = "BT" if res.is_backtest else "REAL"
    fname = Path(res.file_path or "").name or "-"
    err_msg = fail_reason or res.error
    err_tail = f"  | error: {err_msg}" if err_msg else ""
    print(
        f"  [{tag}] id={res.report_id} {bt_txt} "
        f"prompt={res.prompt_id} date={res.report_date} "
        f"-> ai_reports={res.ai_reports_deleted} "
        f"theme_predictions={res.theme_predictions_deleted} "
        f"md={'yes' if res.md_file_deleted else 'no'} "
        f"file={fname}{err_tail}"
    )


def _delete_one(
    rid: int, *, allow_real: bool, delete_md: bool, dry_run: bool,
) -> tuple[bool, DeleteResult, Optional[str]]:
    """返回 ``(ok, DeleteResult, error_msg)``，异常时构造伪 result。"""
    try:
        res = delete_report(
            rid, allow_real=allow_real,
            delete_md=delete_md, dry_run=dry_run,
        )
        return True, res, None
    except ValueError as exc:
        # report_id 不存在：构造伪 result
        fake = DeleteResult(ok=False, report_id=rid, error=str(exc))
        return False, fake, str(exc)
    except PermissionError as exc:
        fake = DeleteResult(ok=False, report_id=rid, error=str(exc))
        return False, fake, str(exc)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.list:
        return _list_reports()

    try:
        ids = _parse_ids(args.report_id)
    except SystemExit as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    # 交互确认（dry_run / --yes 跳过）
    if not args.dry_run and not args.yes:
        if not _confirm_interactive(ids, args.allow_real):
            print("[ABORT] 用户取消")
            return 0

    print(
        f"[plan] {len(ids)} 份报告 dry_run={args.dry_run} "
        f"allow_real={args.allow_real} delete_md={not args.no_md}"
    )

    results: List[DeleteResult] = []
    fail_n = 0
    for rid in ids:
        ok, res, err = _delete_one(
            rid,
            allow_real=args.allow_real,
            delete_md=not args.no_md,
            dry_run=args.dry_run,
        )
        results.append(res)
        if not ok:
            fail_n += 1
        _print_result(res, fail_reason=err)

    print(
        f"\n[summary] {len(results)} 任务: 成功 "
        f"{len(results) - fail_n} / 失败 {fail_n}"
    )

    if args.json:
        print(json.dumps(
            [
                {
                    "ok": r.ok,
                    "report_id": r.report_id,
                    "file_path": r.file_path,
                    "is_backtest": r.is_backtest,
                    "prompt_id": r.prompt_id,
                    "report_date": r.report_date,
                    "ai_reports_deleted": r.ai_reports_deleted,
                    "theme_predictions_deleted":
                        r.theme_predictions_deleted,
                    "md_file_deleted": r.md_file_deleted,
                    "dry_run": r.dry_run,
                    "error": r.error,
                }
                for r in results
            ],
            ensure_ascii=False, indent=2,
        ))

    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
