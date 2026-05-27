"""matcher 回归测试 CLI（services.scoring.matcher 配套）。

用法：
    python tools/test_matcher.py                       # 跑全部用例（推荐）
    python tools/test_matcher.py --case stock          # 只测股票代码标准化
    python tools/test_matcher.py --case sector         # 只测板块匹配
    python tools/test_matcher.py --case enrich         # 只测批量富化
    python tools/test_matcher.py --probe 先进封装        # 试探单个题材匹配结果

退出码：
    0  全部用例通过
    1  至少一个用例失败
    2  参数错误

什么时候重跑？
    - 修改 services/scoring/matcher.py 后
    - 修改 dim_sector / dim_stock 表结构后
    - matcher 阈值 / 规则调整后
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from services.scoring.matcher import (  # noqa: E402
    normalize_stock_code,
    match_sector_ts_code,
    enrich_themes_with_matcher,
)


# ---------------------------------------------------------------------------
# 用例集
# ---------------------------------------------------------------------------

# (原始代码, 原始名称, 期望结果, 备注)
_STOCK_CASES = [
    # 已带后缀
    ("600172.SH", None, "600172.SH", "带后缀直传"),
    ("000001.SZ", None, "000001.SZ", "深市带后缀"),
    ("832023.BJ", None, "832023.BJ", "北交所带后缀"),
    # SH/SZ 前缀形式
    ("SH600172", None, "600172.SH", "SH 前缀"),
    ("SZ000001", None, "000001.SZ", "SZ 前缀"),
    # 6 位纯数字
    ("600172", None, "600172.SH", "沪市主板纯数字"),
    ("000001", None, "000001.SZ", "深市主板纯数字"),
    ("300750", None, "300750.SZ", "创业板纯数字"),
    ("688981", None, "688981.SH", "科创板纯数字"),
    ("832023", None, "832023.BJ", "北交所纯数字"),
    # 名称兜底（依赖 dim_stock 入库）
    (None, "平安银行", "000001.SZ", "纯名称反查 dim_stock"),
    ("INVALID", "平安银行", "000001.SZ", "代码无效，名称兜底"),
    # 边界
    (None, None, None, "全空"),
    ("999999", None, None, "9 开头 B 股暂不支持"),
    ("12345", None, None, "5 位非法长度"),
    # 大小写 / 空格
    ("  sh600172  ", None, "600172.SH", "前后空格 + 小写"),
]


_SECTOR_PROBE_CASES = [
    # 题材名 -> 期望大致命中（不强校验，只要 conf >= 0.5 即可）
    "先进封装",
    "PCB",
    "人形机器人",
    "玻璃基板",
    "黄金概念",
    "稀缺资源",
    "存储芯片",
    "PEEK材料",
    "宝鸡黑色",  # 故意写一个肯定不存在的，验证落 None
]


_ENRICH_CASES = [
    {
        "theme_name": "先进封装",
        "stocks": [
            {"name": "晶方科技", "code": "603005"},
            {"name": "通富微电", "code": "002156"},
        ],
    },
    {
        "theme_name": "人形机器人",
        "stocks": [
            {"name": "兆威机电", "code": "003021"},
            {"name": "未知小公司", "code": "999999"},  # 故意失败
        ],
    },
]


# ---------------------------------------------------------------------------
# 用例执行
# ---------------------------------------------------------------------------

def _run_stock_cases() -> int:
    print("\n=== 用例 1：normalize_stock_code ===")
    fails = 0
    for raw_code, raw_name, expected, note in _STOCK_CASES:
        actual = normalize_stock_code(raw_code, raw_name)
        ok = (actual == expected)
        marker = "[OK]" if ok else "[FAIL]"
        print(f"  {marker} {note:18s}  in=({raw_code!r}, {raw_name!r}) "
              f"expected={expected}  actual={actual}")
        if not ok:
            fails += 1
    print(f"  小计：{len(_STOCK_CASES) - fails}/{len(_STOCK_CASES)} 通过")
    return fails


def _run_sector_cases() -> int:
    print("\n=== 用例 2：match_sector_ts_code（探查模式）===")
    fails = 0
    for name in _SECTOR_PROBE_CASES:
        ts_code, conf = match_sector_ts_code(name)
        marker = "[HIT]" if ts_code else "[MISS]"
        print(f"  {marker} {name:15s}  -> {ts_code}  conf={conf}")
    print("  小计：探查模式不判定对错，仅供肉眼看结果是否合理")
    return fails


def _run_enrich_cases() -> int:
    print("\n=== 用例 3：enrich_themes_with_matcher（批量）===")
    enriched = enrich_themes_with_matcher(_ENRICH_CASES)
    fails = 0
    for theme in enriched:
        name = theme["theme_name"]
        sec = theme.get("sector_ts_code")
        conf = theme.get("sector_match_conf")
        print(f"  [theme] {name:12s}  -> sector={sec}  conf={conf}")
        for stock in theme.get("stocks") or []:
            n = stock.get("name") or "?"
            raw = stock.get("code") or "?"
            norm = stock.get("normalized_code")
            tag = "[OK]" if norm else "[MISS]"
            print(f"    {tag} {n:12s}  raw={raw:8s} -> normalized={norm}")
    return fails


def _probe_sector(name: str) -> int:
    print(f"\n=== 探查：{name} ===")
    ts_code, conf = match_sector_ts_code(name)
    if ts_code:
        print(f"  最佳匹配：{ts_code}  置信度={conf}")
    else:
        print(f"  无匹配（最高置信度={conf}，低于阈值 0.5）")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "--case", choices=["stock", "sector", "enrich", "all"],
        default="all",
        help="跑哪一类用例（默认 all）",
    )
    parser.add_argument(
        "--probe", type=str, default=None,
        help="试探单个题材的板块匹配结果（如 --probe 先进封装）",
    )
    args = parser.parse_args()

    if args.probe:
        return _probe_sector(args.probe)

    total_fails = 0
    if args.case in ("stock", "all"):
        total_fails += _run_stock_cases()
    if args.case in ("sector", "all"):
        total_fails += _run_sector_cases()
    if args.case in ("enrich", "all"):
        total_fails += _run_enrich_cases()

    print(f"\n========== 总结：{total_fails} 个失败 ==========")
    return 1 if total_fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[用户中断]")
        sys.exit(2)
