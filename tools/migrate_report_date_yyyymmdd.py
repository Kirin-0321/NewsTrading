"""一次性迁移 CLI：把历史 ``report_date`` 字段从 ``YYYY-MM-DD`` 规范化为 ``YYYYMMDD``，
顺手把 backtest 产物的文件名对齐到 v3 终态：

* v1 旧命名：``5月27日_18时12分_..._backtest_20260522.md``（前缀是真实生成时间）
* v2 中间命名：``5月22日_0时00分_..._backtest.md``（缺 template_id，同日多模板会互相覆盖）
* v3 终态命名：``5月22日_0时00分_..._backtest_{template_id}.md``

业务背景
--------
2026-05-27 发现 schema 协议（4 张表的 ``report_date`` 注释都写 ``YYYYMMDD``）
和实际入库（``analysis_service`` / ``theme_extractor`` 用 ``%Y-%m-%d``）长期不一致，
直接症状：

* GUI「单报告打分」按钮 → ``next_trade_date('2026-05-22')`` → ValueError
* GUI「区间重打分」按钮 → SQL ``BETWEEN`` 字典序比较失效 → 永远查 0 条静默跑空

源头已在本次提交修正（``%Y%m%d`` + 入口 ``_ensure_yyyymmdd`` 强校验 +
回测产物直接用模拟交易日命名）。本脚本负责清理存量数据，让历史与新协议对齐。

用法
----
::

    # 默认 dry-run：只打印影响行数 / 待重命名文件清单
    python tools/migrate_report_date_yyyymmdd.py

    # 实跑（不可逆，主人已选择直接更新不备份）
    python tools/migrate_report_date_yyyymmdd.py --apply

    # 仅迁移 db 字段，不重命名 md 文件
    python tools/migrate_report_date_yyyymmdd.py --apply --skip-rename

    # 仅扫描 md 文件命名问题
    python tools/migrate_report_date_yyyymmdd.py --rename-only

退出码
------
* ``0`` 全部成功
* ``1`` 校验失败 / 部分回归
* ``2`` 参数错误
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


_log = logging.getLogger("migrate_report_date")


# ---------------------------------------------------------------------------
# 表字段清单（4 张表的 report_date 列）
# ---------------------------------------------------------------------------

_TABLES_TO_MIGRATE = (
    "ai_reports",
    "theme_predictions",
    "theme_prediction_scores",
    "theme_stock_scores",
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class TableScanResult:
    table: str
    total_rows: int = 0
    already_yyyymmdd: int = 0
    will_convert: int = 0
    other_format: int = 0
    other_samples: List[str] = field(default_factory=list)


@dataclass
class RenamePlan:
    report_id: int
    old_file_rel: str
    new_file_rel: str
    old_basename: str
    new_basename: str
    report_date: str


# ---------------------------------------------------------------------------
# Step 1：扫描 db 字段
# ---------------------------------------------------------------------------


def _scan_table(conn, table: str) -> TableScanResult:
    """统计单表 ``report_date`` 列的格式分布。"""
    res = TableScanResult(table=table)
    cur = conn.execute(f"SELECT report_date FROM {table}")
    for row in cur:
        s = row["report_date"]
        res.total_rows += 1
        if not isinstance(s, str):
            res.other_format += 1
            if len(res.other_samples) < 3:
                res.other_samples.append(repr(s))
            continue
        if len(s) == 8 and s.isdigit():
            res.already_yyyymmdd += 1
        elif (
            len(s) == 10
            and s[4] == "-" and s[7] == "-"
            and s.replace("-", "").isdigit()
        ):
            res.will_convert += 1
        else:
            res.other_format += 1
            if len(res.other_samples) < 3:
                res.other_samples.append(s)
    return res


def _migrate_table(conn, table: str) -> int:
    """实际把 ``YYYY-MM-DD`` 行 UPDATE 成 ``YYYYMMDD``；返回更新行数。"""
    cur = conn.execute(
        f"UPDATE {table} SET report_date = REPLACE(report_date, '-', '') "
        f"WHERE length(report_date) = 10 AND substr(report_date, 5, 1) = '-' "
        f"AND substr(report_date, 8, 1) = '-'"
    )
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Step 2：扫描 backtest md 文件命名
# ---------------------------------------------------------------------------


# 旧命名 v1：5月27日_18时12分_盘后总结分析报告_backtest_20260522.md
#   前缀的月日时分 ≠ 后缀 _backtest_YYYYMMDD 里的日期
_RE_OLD_BACKTEST_V1 = re.compile(
    r"^(\d{1,2})月(\d{1,2})日_(\d{1,2})时(\d{2})分_(.+?)_backtest_(\d{8})\.md$"
)

# 旧命名 v2（2026-05-27 18:30 第一版重构后，缺 template_id 后缀）：
#   5月22日_0时00分_盘后总结分析报告_backtest.md
_RE_OLD_BACKTEST_V2 = re.compile(
    r"^(\d{1,2})月(\d{1,2})日_(\d{1,2})时(\d{2})分_(.+?)_backtest\.md$"
)

# v3（2026-05-27 20:00 hotfix）：
#   5月22日_0时00分_盘后总结分析报告_backtest_{template_id}.md
# v4（2026-05-27 20:30 hotfix2，终态）：
#   5月22日_0时00分_盘后总结分析报告_backtest_{template_id}_{HHMMSS}.md
#   兼容无 template 的极端兜底：_backtest_{HHMMSS}.md
# 注意：v3/v4 后缀都属于「合法终态」，迁移脚本不再动它们；以下正则只用于识别
_RE_TERMINAL_BACKTEST = re.compile(
    r"^(\d{1,2})月(\d{1,2})日_(\d{1,2})时(\d{2})分_(.+?)_backtest"
    r"(?:_[A-Za-z0-9_-]+)?\.md$"
)


def _sanitize_template_id(template_id: Optional[str]) -> str:
    """与 :meth:`AnalysisService._sanitize_template_id` 保持一致。"""
    if not template_id:
        return ""
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "", template_id)[:64]


def _expected_backtest_basename(
    report_date: str,
    base_title: str,
    template_id: Optional[str] = None,
) -> str:
    """根据 ``report_date(YYYYMMDD)`` + 报告题目部分，构造新规则下的标准文件名。

    Args:
        report_date: YYYYMMDD（必须已经迁移好）
        base_title: 报告题目部分，如 ``"盘后总结分析报告"``；不含 ``_backtest`` 后缀
        template_id: 同一天多模板回测时用作隔离后缀；为 None/空 时不附加（兼容旧链路）

    Returns:
        - 无 template_id: ``"5月22日_0时00分_盘后总结分析报告_backtest.md"``
        - 有 template_id: ``"5月22日_0时00分_盘后总结分析报告_backtest_custom_6.md"``
    """
    month = int(report_date[4:6])
    day = int(report_date[6:8])
    tail = "_backtest"
    safe_tpl = _sanitize_template_id(template_id)
    if safe_tpl:
        tail += f"_{safe_tpl}"
    return f"{month}月{day}日_0时00分_{base_title}{tail}.md"


def _expected_backtest_dir(report_date: str) -> str:
    """新规则下回测产物所在的子目录名：``{月}月{日}日``。"""
    month = int(report_date[4:6])
    day = int(report_date[6:8])
    return f"{month}月{day}日"


def _normalize_to_yyyymmdd(s: str) -> Optional[str]:
    """``"2026-05-22"`` / ``"20260522"`` → ``"20260522"``；都不是返回 None。"""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return s
    if (
        len(s) == 10 and s[4] == "-" and s[7] == "-"
        and s.replace("-", "").isdigit()
    ):
        return s.replace("-", "")
    return None


def _scan_rename_plan(conn) -> List[RenamePlan]:
    """扫描所有 ``is_backtest=1`` 的 ai_reports，列出需要重命名的文件清单。

    覆盖三种历史/中间命名，统一对齐到 v3 终态：

    * **v1 旧命名**：``5月27日_18时12分_..._backtest_20260522.md``
      （事后 rename 链路的产物；前缀是真实生成时间）
    * **v2 中间命名**：``5月22日_0时00分_..._backtest.md``
      （第一版 hotfix 后；缺 template_id 后缀，同日多模板会互相覆盖）
    * **v3 终态命名**：``5月22日_0时00分_..._backtest_{template_id}.md``

    兼容 db 字段尚未迁移的场景（dry-run 时也能正确扫到旧命名）：
    ``report_date`` 是 YYYY-MM-DD 也会被规范化为 YYYYMMDD 后参与命名推导。
    """
    plans: List[RenamePlan] = []
    rows = conn.execute(
        "SELECT id, file_path, report_date, prompt_id "
        "FROM ai_reports WHERE is_backtest = 1"
    ).fetchall()
    for r in rows:
        rid = int(r["id"])
        fp = (r["file_path"] or "").strip()
        raw_rd = (r["report_date"] or "").strip()
        prompt_id = (r["prompt_id"] or "").strip() or None
        rd = _normalize_to_yyyymmdd(raw_rd)
        if not fp or not rd:
            continue
        basename = os.path.basename(fp.replace("\\", "/"))
        # 已经是 v3/v4 终态命名 → 跳过（既支持 _backtest_{tpl}.md 也支持
        # _backtest_{tpl}_{HHMMSS}.md / _backtest_{HHMMSS}.md）
        if _RE_TERMINAL_BACKTEST.match(basename):
            continue
        # 尝试从 v1 拆出题目
        m1 = _RE_OLD_BACKTEST_V1.match(basename)
        m2 = _RE_OLD_BACKTEST_V2.match(basename)
        if m1:
            base_title = m1.group(5)
        elif m2:
            base_title = m2.group(5)
        else:
            base_title = "盘后总结分析报告"
        new_basename = _expected_backtest_basename(
            rd, base_title, template_id=prompt_id,
        )
        new_dir = _expected_backtest_dir(rd)
        new_rel = f"data/AI_analysis/{new_dir}/{new_basename}"
        if new_rel == fp.replace("\\", "/"):
            continue
        plans.append(
            RenamePlan(
                report_id=rid,
                old_file_rel=fp,
                new_file_rel=new_rel,
                old_basename=basename,
                new_basename=new_basename,
                report_date=rd,
            )
        )
    return plans


def _apply_rename_plan(conn, plan: RenamePlan) -> Tuple[bool, str]:
    """物理重命名 + 同步 db；返回 (ok, 详情)。"""
    project_root = Path(_ROOT)
    old_abs = project_root / plan.old_file_rel
    new_abs = project_root / plan.new_file_rel

    # 物理文件
    physical_moved = False
    if old_abs.exists():
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        if new_abs.exists():
            # 目标已存在：可能是另一份同 (template, date) 的回测；保守跳过
            return False, f"目标已存在: {plan.new_file_rel}"
        os.rename(old_abs, new_abs)
        physical_moved = True
    else:
        # md 文件早就丢了，db 也得跟上更新
        _log.warning(
            "源文件不存在(id=%d): %s → 仅更新 db",
            plan.report_id, plan.old_file_rel,
        )

    # db 同步
    conn.execute(
        "UPDATE ai_reports SET file_path = ? WHERE id = ?",
        (plan.new_file_rel, plan.report_id),
    )
    conn.execute(
        "UPDATE theme_predictions SET report_path = ?, report_id = ? "
        "WHERE report_path = ?",
        (plan.new_file_rel, plan.new_basename[:-3], plan.old_file_rel),
    )
    return True, (
        "已重命名" if physical_moved else "仅更新 db（文件不在）"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _print_scan_report(scans: List[TableScanResult]) -> None:
    print("=" * 70)
    print("[Step 1] db 字段扫描结果")
    print("=" * 70)
    print(
        f"{'table':<30}{'total':>8}{'已 YYYYMMDD':>14}"
        f"{'待转':>8}{'异常':>8}"
    )
    for s in scans:
        print(
            f"{s.table:<30}{s.total_rows:>8}{s.already_yyyymmdd:>14}"
            f"{s.will_convert:>8}{s.other_format:>8}"
        )
        if s.other_samples:
            print(f"  异常样例: {s.other_samples}")


def _print_rename_report(plans: List[RenamePlan]) -> None:
    print()
    print("=" * 70)
    print(f"[Step 2] backtest 产物文件命名修正（{len(plans)} 个）")
    print("=" * 70)
    if not plans:
        print("  ✓ 没有需要重命名的 backtest 产物")
        return
    for p in plans[:30]:
        print(f"  id={p.report_id}  rd={p.report_date}")
        print(f"    旧: {p.old_file_rel}")
        print(f"    新: {p.new_file_rel}")
    if len(plans) > 30:
        print(f"  ... 还有 {len(plans) - 30} 个，--apply 时会全部处理")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_report_date_yyyymmdd",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实跑（默认 dry-run，仅打印影响范围）",
    )
    parser.add_argument(
        "--skip-rename", action="store_true",
        help="只迁移 db 字段，不重命名 md 文件",
    )
    parser.add_argument(
        "--rename-only", action="store_true",
        help="只扫描/重命名 md 文件，不动 db report_date 字段",
    )
    args = parser.parse_args(argv)

    if args.skip_rename and args.rename_only:
        print("--skip-rename 与 --rename-only 互斥", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
    )

    from services.storage.ai_inference_db import get_ai_inference_db

    adb = get_ai_inference_db()
    adb.ensure_schema()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"模式: [{mode}]  仓库根: {_ROOT}")

    # 已存在的表
    existing: List[str] = []
    with adb.connect(readonly=True) as conn:
        for t in _TABLES_TO_MIGRATE:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name = ?",
                (t,),
            ).fetchone()
            if row:
                existing.append(t)

    # Step 1：扫描 + 迁移 db 字段
    scans: List[TableScanResult] = []
    if not args.rename_only:
        with adb.connect(readonly=True) as conn:
            for t in existing:
                scans.append(_scan_table(conn, t))
        _print_scan_report(scans)

        total_convert = sum(s.will_convert for s in scans)
        total_other = sum(s.other_format for s in scans)
        if total_other > 0:
            print(
                f"\n⚠ 有 {total_other} 行 report_date 不是 YYYYMMDD 也不是 "
                f"YYYY-MM-DD，本脚本不会动它们；请人工排查异常样例。"
            )

        if args.apply and total_convert > 0:
            with adb.connect() as conn:
                for t in existing:
                    n = _migrate_table(conn, t)
                    print(f"  [APPLY] {t}: 实际更新 {n} 行")

    # Step 2：扫描 + 重命名 md
    plans: List[RenamePlan] = []
    if not args.skip_rename:
        # 重命名要基于 YYYYMMDD 的 report_date，所以必须先跑完 Step 1
        # （dry-run 模式下 db 还是旧值，扫描会自动跳过 dash 行；
        #  apply 模式 + 同时跑两步时，已经在上面提交了）
        with adb.connect(readonly=True) as conn:
            plans = _scan_rename_plan(conn)
        _print_rename_report(plans)

        if args.apply and plans:
            ok_cnt = 0
            fail_cnt = 0
            with adb.connect() as conn:
                for p in plans:
                    ok, detail = _apply_rename_plan(conn, p)
                    status = "✓" if ok else "✗"
                    print(
                        f"  {status} id={p.report_id}: "
                        f"{p.old_basename} → {p.new_basename}  ({detail})"
                    )
                    if ok:
                        ok_cnt += 1
                    else:
                        fail_cnt += 1
            print(
                f"\n[APPLY] 重命名完成: 成功 {ok_cnt} / 失败 {fail_cnt}"
            )

    if not args.apply:
        print()
        print("=" * 70)
        print("当前是 DRY-RUN 模式；确认无误后追加 --apply 实跑。")
        print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
