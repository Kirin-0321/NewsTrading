"""
SQLite 原始库语义去重：分析重复项、导出核验文件、可选物理删除。

规则（与 crawl_sync 一致）：
- 30 分钟时间窗口
- 标题相似度 >= 30% 或 标题+正文相似度 >= 30% → 判为重复
- 按发布时间升序，保留最早一条

用法:
    python tools/dedupe_sqlite_raw.py
    python tools/dedupe_sqlite_raw.py --apply
    python tools/dedupe_sqlite_raw.py --threshold 0.35 --window 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.semantic_dedup import SemanticDeduplicator
from services.storage.database import get_connection, get_db_path
from services.storage.news_utils import row_to_news


def load_all_raw_news() -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM raw_news ORDER BY published_ts ASC, id ASC"
        ).fetchall()
    return [row_to_news(r) for r in rows]


def analyze_duplicates(
    news_list: List[Dict],
    deduper: SemanticDeduplicator,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    分析重复项。

    Returns:
        kept: 保留条目
        removed_records: 每条被剔除记录及匹配信息
        groups: 按 keeper 聚合的重复组
    """
    news_with_time: List[Tuple[Dict, datetime]] = []
    news_without_time: List[Dict] = []

    for news in news_list:
        dt = deduper._news_time(news)
        if dt:
            news_with_time.append((news, dt))
        else:
            news_without_time.append(news)

    kept: List[Dict] = []
    kept_with_time: List[Tuple[Dict, datetime]] = []
    removed_records: List[Dict] = []
    groups_map: Dict[str, Dict] = {}

    for current_news, current_time in news_with_time:
        matched_keeper = None
        matched_detail = None
        matched_keeper_time = None

        for keeper_news, keeper_time in reversed(kept_with_time):
            if not deduper._in_time_window(current_time, keeper_time):
                break
            detail = deduper.similarity_detail(current_news, keeper_news)
            if detail["is_duplicate"]:
                matched_keeper = keeper_news
                matched_detail = detail
                matched_keeper_time = keeper_time
                break

        if matched_keeper is None:
            kept.append(current_news)
            kept_with_time.append((current_news, current_time))
            continue

        assert matched_keeper_time is not None
        keeper_id = matched_keeper.get("id", "")
        time_diff = abs(
            (current_time - matched_keeper_time).total_seconds() / 60
        )
        removed_item = _record_item(
            current_news,
            role="removed",
            detail=matched_detail,
            keeper_id=keeper_id,
            keeper_title=matched_keeper.get("title", ""),
            time_diff_minutes=time_diff,
        )
        removed_records.append(removed_item)

        if keeper_id not in groups_map:
            groups_map[keeper_id] = {
                "keeper": _record_item(matched_keeper, role="kept"),
                "removed": [],
            }
        groups_map[keeper_id]["removed"].append(removed_item)

    kept.extend(news_without_time)
    groups = list(groups_map.values())
    return kept, removed_records, groups


def _record_item(
    news: Dict,
    role: str,
    detail: Dict | None = None,
    keeper_id: str | None = None,
    keeper_title: str | None = None,
    time_diff_minutes: float | None = None,
) -> Dict:
    item = {
        "role": role,
        "id": news.get("id", ""),
        "title": news.get("title", ""),
        "source": news.get("source", ""),
        "datetime": news.get("datetime") or news.get("time", ""),
        "content_preview": (news.get("content") or "")[:200],
    }
    if detail:
        item.update({
            "keeper_id": keeper_id,
            "keeper_title": keeper_title or "",
            "title_similarity": detail.get("title_similarity"),
            "merged_similarity": detail.get("merged_similarity"),
            "match_reason": detail.get("match_reason"),
            "time_diff_minutes": round(time_diff_minutes, 2)
            if time_diff_minutes is not None
            else None,
        })
    return item


def _member_letter(index: int) -> str:
    """成员序号转字母：0->a, 1->b, ..."""
    return chr(ord("a") + index)


def build_labeled_groups(groups: List[Dict]) -> List[Dict]:
    """
    将重复组转为带编号标签的结构。

    示例:
        1a 新闻标题1  (保留)
        1b 新闻标题2  (重复)
        2a 新闻标题3
        2b 新闻标题4
        2c 新闻标题5
    """
    sorted_groups = sorted(
        groups,
        key=lambda g: (g["keeper"].get("datetime", ""), g["keeper"].get("id", "")),
    )

    labeled_groups: List[Dict] = []
    for group_no, group in enumerate(sorted_groups, start=1):
        members: List[Dict] = []

        keeper = dict(group["keeper"])
        keeper.update({
            "group_no": group_no,
            "member_index": 0,
            "member_letter": "a",
            "group_label": f"{group_no}a",
            "display_line": f"{group_no}a {keeper.get('title', '')}",
        })
        members.append(keeper)

        for idx, removed in enumerate(group["removed"], start=1):
            letter = _member_letter(idx)
            item = dict(removed)
            item.update({
                "group_no": group_no,
                "member_index": idx,
                "member_letter": letter,
                "group_label": f"{group_no}{letter}",
                "display_line": f"{group_no}{letter} {item.get('title', '')}",
            })
            members.append(item)

        labeled_groups.append({
            "group_no": group_no,
            "member_count": len(members),
            "members": members,
        })

    return labeled_groups


def flatten_group_members(labeled_groups: List[Dict]) -> List[Dict]:
    """展平为逐行列表，供 CSV 使用。"""
    rows: List[Dict] = []
    for group in labeled_groups:
        rows.extend(group["members"])
    return rows


def export_report(
    output_path: str,
    deduper: SemanticDeduplicator,
    total: int,
    kept: List[Dict],
    removed_records: List[Dict],
    labeled_groups: List[Dict],
) -> None:
    flat_members = flatten_group_members(labeled_groups)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": get_db_path(),
        "rules": {
            "time_window_minutes": deduper.time_window_minutes,
            "title_threshold": deduper.title_threshold,
            "merged_threshold": deduper.merged_threshold,
            "compare": "title OR (both have content AND title+content)",
        },
        "summary": {
            "total": total,
            "kept": len(kept),
            "removed": len(removed_records),
            "duplicate_groups": len(labeled_groups),
            "duplicate_entries": len(flat_members),
        },
        "duplicate_groups": labeled_groups,
        "duplicate_entries": flat_members,
        "duplicate_pairs": removed_records,
    }
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def export_csv(output_path: str, labeled_groups: List[Dict]) -> None:
    """导出分组 CSV，每组内 a/b/c 成员全部列出。"""
    import csv

    fields = [
        "group_no",
        "group_label",
        "member_letter",
        "role",
        "id",
        "datetime",
        "title",
        "source",
        "keeper_id",
        "keeper_title",
        "title_similarity",
        "merged_similarity",
        "match_reason",
        "time_diff_minutes",
        "content_preview",
    ]
    rows = flatten_group_members(labeled_groups)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_txt(output_path: str, labeled_groups: List[Dict]) -> None:
    """导出纯文本，便于快速浏览：1a 标题 / 1b 标题。"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for group in labeled_groups:
            for member in group["members"]:
                f.write(f"{member['group_label']} {member.get('title', '')}\n")
            f.write("\n")


def apply_deletions(removed_ids: List[str]) -> Dict[str, int]:
    """从 raw_news 删除重复 id（清洗状态随记录一并删除）。"""
    if not removed_ids:
        return {"raw": 0}

    stats = {"raw": 0}
    chunk_size = 500
    with get_connection() as conn:
        for i in range(0, len(removed_ids), chunk_size):
            chunk = removed_ids[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"DELETE FROM raw_news WHERE id IN ({placeholders})",
                chunk,
            )
            stats["raw"] += cur.rowcount

    return stats


def main():
    parser = argparse.ArgumentParser(description="SQLite 原始库语义去重")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="确认后物理删除重复项（默认仅导出报告）",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="时间窗口（分钟），默认 30",
    )
    parser.add_argument(
        "--title-threshold",
        type=float,
        default=0.60,
        help="标题相似度阈值 0~1，默认 0.60",
    )
    parser.add_argument(
        "--merged-threshold",
        type=float,
        default=0.55,
        help="标题+正文合并相似度阈值 0~1，默认 0.55",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="兼容旧参数：两路共用同一阈值",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join("data", "exports"),
        help="报告输出目录，默认 data/exports",
    )
    args = parser.parse_args()

    deduper = SemanticDeduplicator(
        time_window_minutes=args.window,
        title_threshold=args.title_threshold,
        merged_threshold=args.merged_threshold,
        similarity_threshold=args.threshold,
    )

    print(f"数据库: {get_db_path()}")
    news_list = load_all_raw_news()
    print(f"原始库共 {len(news_list)} 条，开始分析...")

    kept, removed_records, groups = analyze_duplicates(news_list, deduper)
    labeled_groups = build_labeled_groups(groups)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(args.output_dir, f"dedup_raw_review_{stamp}.json")
    csv_path = os.path.join(args.output_dir, f"dedup_raw_review_{stamp}.csv")
    txt_path = os.path.join(args.output_dir, f"dedup_raw_review_{stamp}.txt")

    export_report(
        json_path, deduper, len(news_list), kept, removed_records, labeled_groups
    )
    export_csv(csv_path, labeled_groups)
    export_txt(txt_path, labeled_groups)

    flat_count = sum(g["member_count"] for g in labeled_groups)
    print("\n=== 去重分析结果 ===")
    print(f"总计:       {len(news_list)}")
    print(f"保留:       {len(kept)}")
    print(f"重复剔除:   {len(removed_records)}")
    print(f"重复组数:   {len(labeled_groups)}")
    print(f"重复词条数: {flat_count}  (含保留项，每组全导出)")
    print(f"\nJSON 报告: {os.path.abspath(json_path)}")
    print(f"CSV  报告: {os.path.abspath(csv_path)}")
    print(f"TXT  报告: {os.path.abspath(txt_path)}")

    if not args.apply:
        print("\n仅分析未删库。确认无误后执行: python tools/dedupe_sqlite_raw.py --apply")
        return

    removed_ids = [r["id"] for r in removed_records if r.get("id")]
    if not removed_ids:
        print("\n无重复项需要删除。")
        return

    print(f"\n即将删除 {len(removed_ids)} 条重复 raw 记录...")
    stats = apply_deletions(removed_ids)
    print(f"已删除 raw={stats['raw']}")


if __name__ == "__main__":
    main()
