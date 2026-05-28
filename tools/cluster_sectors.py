"""板块语义聚类 CLI（DeepSeek V4 Pro 一键聚类 / 子代理接力 / 手工微调三栈）。

业务定位
--------
2026-05-28 决策点 ai_source=D + cluster_method=llm_only：把 1500+ 板块按
"投资主题"语义聚类，让 AI 取 Top N 时同主题只看一行（取中位涨幅）。

主流程（一键，2026-05-28 12:40 切到直调 API）
--------------------------------------------
::

    python tools/cluster_sectors.py cluster              # 一键: 调 DeepSeek V4 Pro + 落库
    python tools/cluster_sectors.py cluster --dry-run    # 调 LLM 但不写库
    python tools/cluster_sectors.py cluster --no-llm \\
        --input .huiye/_sector_clustering_output.json    # 跳 LLM 直接落已存在的 JSON

辅助子命令
----------
::

    python tools/cluster_sectors.py merge-pass --dry-run  # 二阶段: LLM 推荐近义合并(预览)
    python tools/cluster_sectors.py merge-pass --apply    # 二阶段: 实际合并
    python tools/cluster_sectors.py dump-llm-input        # 导出"喂给任意 LLM 深度合并"提示词 md
    python tools/cluster_sectors.py export-md             # 导出全组成员清单 markdown 给主人审阅
    python tools/cluster_sectors.py stats              # 当前聚类状态快照
    python tools/cluster_sectors.py export             # 仅导出 input JSON（subagent 路径用）
    python tools/cluster_sectors.py apply              # 仅落库（已有 output JSON 时用）
    python tools/cluster_sectors.py reset --confirm    # 清空全部聚类（LLM 偷懒后救场）
    python tools/cluster_sectors.py merge --target 白酒 --sources 酿酒 葡萄酒
    python tools/cluster_sectors.py split --ts-code BK0917.DC

退出码
------
* 0 成功
* 1 业务失败（聚类不完整 / 校验未过 / API 失败）
* 2 参数错
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
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


_DEFAULT_INPUT = _ROOT / ".huiye" / "_sector_clustering_input.json"
_DEFAULT_OUTPUT = _ROOT / ".huiye" / "_sector_clustering_output.json"
_DEFAULT_MERGE_OUTPUT = _ROOT / ".huiye" / "_sector_merge_output.json"


def _cmd_export(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import export_for_subagent
    out = Path(args.out)
    n = export_for_subagent(out)
    print(f"[OK] 导出 {n} 个未聚类板块 → {out}")
    print(f"\n下一步（subagent 路径）：")
    print(f"  在 Cursor 里启 generalPurpose subagent，喂它：")
    print(f"  '读 {out.relative_to(_ROOT)}，按 instructions 做聚类，')")
    print(f"  '结果 JSON 写到 .huiye/_sector_clustering_output.json'")
    print(f"\n或者直接用：python tools/cluster_sectors.py cluster")
    return 0


def _cmd_cluster(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import (
        apply_clustering,
        cluster_via_deepseek,
        get_clustering_stats,
    )
    out_path = Path(args.output)

    if args.no_llm:
        if not out_path.exists():
            print(
                f"[ERROR] --no-llm 模式要求 {out_path} 已存在",
                file=sys.stderr,
            )
            return 2
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[ERROR] JSON 解析失败: {exc}", file=sys.stderr)
            return 2
        print(f"[skip-LLM] 直接读 {out_path}")
    else:
        # 一键调 DeepSeek
        try:
            payload = cluster_via_deepseek(
                output_path=out_path,
                progress=lambda msg: print(msg),
            )
        except RuntimeError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        print(f"[OK] LLM 输出已保存 → {out_path}")

    if args.dry_run:
        res = apply_clustering(payload, dry_run=True)
        print(f"\n=== 校验结果 [DRY-RUN] ===")
        print(f"  groups_created    = {res.groups_created}")
        print(f"  sectors_assigned  = {res.sectors_assigned}")
        if res.duplicate_assignments:
            print(f"  info:")
            for m in res.duplicate_assignments:
                print(f"     - {m}")
        if res.errors:
            print(f"  errors ({len(res.errors)}, 前 10):")
            for e in res.errors[:10]:
                print(f"     - {e}")
            return 1
        print("  通过校验，加 --apply 真实落库")
        return 0

    if args.apply:
        res = apply_clustering(payload, created_by="llm-v2")
        print(f"\n=== 落库结果 [APPLIED] ===")
        print(f"  groups_created    = {res.groups_created}")
        print(f"  groups_skipped    = {res.groups_skipped} (同名复用)")
        print(f"  sectors_assigned  = {res.sectors_assigned}")
        if res.duplicate_assignments:
            print(f"  info:")
            for m in res.duplicate_assignments:
                print(f"     - {m}")
        if res.errors:
            print(f"  errors ({len(res.errors)}, 前 10):")
            for e in res.errors[:10]:
                print(f"     - {e}")
            return 1
        # 末尾打印 stats
        s = get_clustering_stats()
        print(f"\n=== 聚类后状态 ===")
        print(
            f"  {s['clustered_sectors']}/{s['total_sectors']} 已聚类"
            f"，{s['groups_total']} 组"
        )
        return 0

    print(
        "\n仅 LLM 调用完成，未写库。加 --apply 真实落库（或 --dry-run 仅校验）",
    )
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import apply_clustering
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] 输入文件不存在: {in_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(in_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] JSON 解析失败: {exc}", file=sys.stderr)
        return 2

    res = apply_clustering(
        payload,
        created_by=args.created_by,
        dry_run=args.dry_run,
    )
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"=== 聚类落库 [{mode}] ===")
    print(f"  groups_created    = {res.groups_created}")
    print(f"  groups_skipped    = {res.groups_skipped} (同名复用)")
    print(f"  sectors_assigned  = {res.sectors_assigned}")
    if res.invalid_ts_codes:
        print(f"  invalid_ts_codes  = {len(res.invalid_ts_codes)} (前 5)")
        for c in res.invalid_ts_codes[:5]:
            print(f"     - {c}")
    if res.errors:
        print(f"  errors ({len(res.errors)}, 前 10):")
        for e in res.errors[:10]:
            print(f"     - {e}")
        return 1
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import get_clustering_stats
    s = get_clustering_stats()
    if args.as_json:
        json.dump(s, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        total = s["total_sectors"]
        clustered = s["clustered_sectors"]
        ratio = (clustered / total * 100) if total else 0
        print(f"=== 聚类状态 ===")
        print(f"  字典板块总数   : {total}")
        print(f"  已聚类       : {clustered} ({ratio:.1f}%)")
        print(f"  未聚类       : {s['unclustered_sectors']}")
        print(f"  独立组数     : {s['groups_total']}")
        if total > 0 and s["groups_total"] > 0:
            avg = clustered / s["groups_total"]
            print(f"  平均组大小   : {avg:.1f}")
        print(f"\n  最大 5 组：")
        for g in s["biggest_groups"]:
            print(f"    - {g['name']:20s} ×{g['members_count']}")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import merge_groups
    try:
        moved = merge_groups(args.target, args.sources)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] 合并 {args.sources} → {args.target!r}，转移 {moved} 个成员")
    return 0


def _cmd_merge_pass(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import (
        apply_merges,
        propose_merges_via_deepseek,
    )
    out_path = Path(args.output)

    if args.no_llm:
        if not out_path.exists():
            print(f"[ERROR] --no-llm 模式要求 {out_path} 已存在", file=sys.stderr)
            return 2
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[ERROR] JSON 解析失败: {exc}", file=sys.stderr)
            return 2
        print(f"[skip-LLM] 直接读 {out_path}")
    else:
        try:
            payload = propose_merges_via_deepseek(
                output_path=out_path,
                progress=lambda msg: print(msg),
            )
        except RuntimeError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        print(f"[OK] LLM 输出已保存 → {out_path}")

    if args.dry_run:
        res = apply_merges(payload, dry_run=True)
        print(f"\n=== 合并预览 [DRY-RUN] ===")
        print(f"  merges_applied   = {res['merges_applied']}")
        print(f"  groups_removed   = {res['groups_removed']}（被合并掉的源组数）")
        if res["skipped"]:
            print(f"  skipped ({len(res['skipped'])}):")
            for s in res["skipped"][:10]:
                print(f"     - {s}")
        print(f"\n  抽样合并预览（前 10 条）:")
        for m in (payload.get("merges") or [])[:10]:
            tgt = m.get("target", "")
            srcs = m.get("sources", []) or []
            rat = m.get("rationale") or m.get("notes") or ""
            print(f"    - {tgt!s:12s} ← {srcs}   {rat}")
        print("\n  加 --apply 真实合并")
        return 0

    if args.apply:
        res = apply_merges(payload)
        print(f"\n=== 合并落库 [APPLIED] ===")
        print(f"  merges_applied   = {res['merges_applied']}")
        print(f"  groups_removed   = {res['groups_removed']}")
        print(f"  members_moved    = {res['members_moved']} 板块换组")
        if res["skipped"]:
            print(f"  skipped ({len(res['skipped'])}, 前 10):")
            for s in res["skipped"][:10]:
                print(f"     - {s}")
            return 1 if any("失败" in s for s in res["skipped"]) else 0
        return 0

    print("\n仅 LLM 调用完成，未写库。加 --apply 真实合并（或 --dry-run 仅预览）")
    return 0


def _cmd_dump_llm_input(args: argparse.Namespace) -> int:
    from datetime import datetime
    from services.market.sector_grouping import dump_merge_pass_prompt

    if args.out:
        out = Path(args.out)
    else:
        ts = datetime.now().strftime("%m-%d-%H%M")
        out = (
            _ROOT / ".huiye"
            / f"_LLM深度合并输入_{ts}.md"
        )

    res = dump_merge_pass_prompt(out)
    print(f"[OK] 写入 {out}")
    print(f"  覆盖组数        = {res['groups']}")
    print(f"  覆盖板块        = {res['members']}")
    print(f"  字符数          = {res['char_count']}")
    print(f"  token 估算      = {res['tokens_estimate']} (按 3 char/token)")
    print(f"")
    print(f"下一步：")
    print(f"  1. 把 {out.name} 完整内容拷给任意 LLM")
    print(f"  2. LLM 输出 JSON 存到 .huiye/_sector_merge_output.json")
    print(f"  3. python tools/cluster_sectors.py merge-pass --no-llm --apply")
    return 0


def _cmd_export_md(args: argparse.Namespace) -> int:
    from datetime import datetime
    from services.market.sector_grouping import export_groups_to_markdown

    if args.out:
        out = Path(args.out)
    else:
        ts = datetime.now().strftime("%m-%d-%H%M")
        suffix = "板块归属扁平表" if args.flat else "板块聚类成员清单"
        out = _ROOT / "doc" / "reports" / f"{ts}-{suffix}.md"

    res = export_groups_to_markdown(out, flat=args.flat)
    print(f"[OK] 导出到 {out}")
    print(f"  组数         = {res['groups']}")
    print(f"  已聚类板块    = {res['members']}")
    print(f"  未聚类板块    = {res['unclustered']}")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import reset_all_clustering
    if not args.confirm:
        print(
            "[ERROR] 危险操作：清空 dim_sector_group + dim_sector.group_id 全部置 NULL。\n"
            "        如确认要做请加 --confirm",
            file=sys.stderr,
        )
        return 2
    res = reset_all_clustering()
    print(f"=== 聚类重置 [DONE] ===")
    print(f"  sectors_reset    = {res['sectors_reset']}")
    print(f"  groups_deleted   = {res['groups_deleted']}")
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    from services.market.sector_grouping import split_member
    try:
        new_gid = split_member(args.ts_code, args.new_group)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] {args.ts_code} → 新组 id={new_gid}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="板块语义聚类（subagent 接力 + 落库 + 微调）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser(
        "cluster",
        help="一键调 DeepSeek V4 Pro 聚类 + 落库（主流程）",
    )
    pc.add_argument(
        "--output", type=str, default=str(_DEFAULT_OUTPUT),
        help="LLM 输出 JSON 落地路径（默认 .huiye/_sector_clustering_output.json）",
    )
    pc.add_argument(
        "--no-llm", action="store_true",
        help="跳过 LLM，直接读已存在的 --output 文件落库（断点续跑）",
    )
    pc.add_argument(
        "--dry-run", action="store_true",
        help="完成 LLM 调用 + 校验但不写 db",
    )
    pc.add_argument(
        "--apply", action="store_true",
        help="实际写 dim_sector_group + UPDATE dim_sector.group_id",
    )
    pc.set_defaults(func=_cmd_cluster)

    pmp = sub.add_parser(
        "merge-pass",
        help="二阶段：拿现有 group 名再调 LLM 推荐近义合并（推荐先 --dry-run）",
    )
    pmp.add_argument(
        "--output", type=str, default=str(_DEFAULT_MERGE_OUTPUT),
        help="合并建议 JSON 落地路径（默认 .huiye/_sector_merge_output.json）",
    )
    pmp.add_argument(
        "--no-llm", action="store_true",
        help="跳过 LLM，直接读已存在的 --output 文件应用",
    )
    pmp.add_argument(
        "--dry-run", action="store_true",
        help="完成 LLM 调用 + 校验但不写 db",
    )
    pmp.add_argument(
        "--apply", action="store_true",
        help="实际执行合并（循环调 merge_groups）",
    )
    pmp.set_defaults(func=_cmd_merge_pass)

    pem = sub.add_parser(
        "export-md",
        help="导出全组成员清单 markdown（默认按组分章；--flat = 扁平表格 1510 行）",
    )
    pem.add_argument(
        "--out", type=str, default=None,
        help="输出 md 路径（默认 doc/reports/MM-DD-HHmm-{板块聚类成员清单|板块归属扁平表}.md）",
    )
    pem.add_argument(
        "--flat", action="store_true",
        help="扁平表格视图：每行一个原始板块，列出归属分组（适合一眼扫完 1510 板块）",
    )
    pem.set_defaults(func=_cmd_export_md)

    pdl = sub.add_parser(
        "dump-llm-input",
        help="导出『喂给任意 LLM 做深度合并』完整提示词 + 数据 md（主人手动换 AI 用）",
    )
    pdl.add_argument(
        "--out", type=str, default=None,
        help="输出 md 路径（默认 .huiye/_LLM深度合并输入_MM-DD-HHmm.md）",
    )
    pdl.set_defaults(func=_cmd_dump_llm_input)

    pe = sub.add_parser("export", help="（subagent 路径用）导出未聚类板块清单 JSON")
    pe.add_argument(
        "--out", type=str, default=str(_DEFAULT_INPUT),
        help="输出 JSON 路径（默认 .huiye/_sector_clustering_input.json）",
    )
    pe.set_defaults(func=_cmd_export)

    pa = sub.add_parser("apply", help="Stage 3: 把 subagent 输出的 JSON 落库")
    pa.add_argument(
        "--input", type=str, default=str(_DEFAULT_OUTPUT),
        help="输入 JSON 路径（默认 .huiye/_sector_clustering_output.json）",
    )
    pa.add_argument(
        "--created-by", type=str, default="llm-v1",
        help="dim_sector_group.created_by 标记（默认 llm-v1）",
    )
    pa.add_argument(
        "--dry-run", action="store_true",
        help="只校验不写库",
    )
    pa.set_defaults(func=_cmd_apply)

    ps = sub.add_parser("stats", help="查看当前聚类状态")
    ps.add_argument(
        "--json", dest="as_json", action="store_true",
        help="输出 JSON",
    )
    ps.set_defaults(func=_cmd_stats)

    pr = sub.add_parser(
        "reset",
        help="清空 dim_sector_group + 把 group_id 全置 NULL（救场用）",
    )
    pr.add_argument(
        "--confirm", action="store_true",
        help="必须显式确认才执行（防误操作）",
    )
    pr.set_defaults(func=_cmd_reset)

    pm = sub.add_parser("merge", help="合并组（手工修正）")
    pm.add_argument("--target", required=True, help="保留组名")
    pm.add_argument("--sources", nargs="+", required=True, help="被合并组名（多个）")
    pm.set_defaults(func=_cmd_merge)

    psp = sub.add_parser("split", help="把某个 ts_code 拆出原组成独立组")
    psp.add_argument("--ts-code", required=True, help="要拆出的板块代码")
    psp.add_argument(
        "--new-group", type=str, default=None,
        help="新组名，缺省取板块自身 name",
    )
    psp.set_defaults(func=_cmd_split)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
