"""theme_extractor 内部规范化 + matcher 接入验证 CLI（不调真实 LLM）。

用法：
    python tools/test_theme_normalize.py                  # 全部用例
    python tools/test_theme_normalize.py --case score     # 只测 score→level 映射
    python tools/test_theme_normalize.py --case parse     # 只测 _normalize_theme（含负 score）
    python tools/test_theme_normalize.py --case meta      # 只测 parse_report_meta（含 P1 路径规范化）
    python tools/test_theme_normalize.py --case e2e       # 模拟一段 fake LLM 输出走完整流程（含 matcher）

退出码：
    0  全部用例通过
    1  至少一个用例失败
    2  参数错误

何时重跑：
    - 修改 core/theme_extractor.py 后
    - 升级 prompts/theme_extraction/extract_themes.md 后
    - matcher 接口签名变化后
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.theme_extractor import (  # noqa: E402
    ThemeExtractor,
    _score_to_level,
    parse_report_meta,
)


# ---------------------------------------------------------------------------
# 用例 1: _score_to_level 9 档映射
# ---------------------------------------------------------------------------
_SCORE_LEVEL_CASES = [
    (100, "重大利多"),
    (80, "重大利多"),
    (79, "较强利多"),
    (60, "较强利多"),
    (59, "弱利多"),
    (40, "弱利多"),
    (39, "中性偏多"),
    (1, "中性偏多"),
    (0, "中性"),
    (-1, "中性偏空"),
    (-39, "中性偏空"),
    (-40, "弱利空"),
    (-59, "弱利空"),
    (-60, "较强利空"),
    (-79, "较强利空"),
    (-80, "重大利空"),
    (-100, "重大利空"),
]


def _run_score_cases() -> int:
    print("\n=== 用例 1：_score_to_level 9 档映射 ===")
    fails = 0
    for score, expected in _SCORE_LEVEL_CASES:
        actual = _score_to_level(score)
        ok = actual == expected
        marker = "[OK]" if ok else "[FAIL]"
        print(f"  {marker} score={score:+4d}  expected={expected:6s}  actual={actual}")
        if not ok:
            fails += 1
    print(f"  小计：{len(_SCORE_LEVEL_CASES) - fails}/{len(_SCORE_LEVEL_CASES)} 通过")
    return fails


# ---------------------------------------------------------------------------
# 用例 2: _normalize_theme 字段规范化（含负 score / 不再有 sentiment）
# ---------------------------------------------------------------------------
_NORMALIZE_CASES = [
    {
        "input": {
            "theme_name": "先进封装",
            "strength_score": 75,
            "strength_level": "较强利多",
            "reason": "AI 算力需求拉动",
        },
        "expect": {"score": 75, "level": "较强利多"},
        "note": "标准利多",
    },
    {
        "input": {
            "theme_name": "稀土打压",
            "strength_score": -65,
            "reason": "海外政策风险",
        },
        "expect": {"score": -65, "level": "较强利空"},
        "note": "负 score 自动推断 level",
    },
    {
        "input": {
            "theme_name": "中性观望",
            "strength_score": 0,
            "reason": "等待催化",
        },
        "expect": {"score": 0, "level": "中性"},
        "note": "中性",
    },
    {
        "input": {
            "theme_name": "超界利多",
            "strength_score": 999,
            "reason": "测试截断",
        },
        "expect": {"score": 100, "level": "重大利多"},
        "note": "score 上限截断到 +100",
    },
    {
        "input": {
            "theme_name": "超界利空",
            "strength_score": -250,
            "reason": "测试截断",
        },
        "expect": {"score": -100, "level": "重大利空"},
        "note": "score 下限截断到 -100",
    },
    {
        "input": {
            "theme_name": "sentiment 残留",
            "strength_score": 50,
            "sentiment": "利好",
            "reason": "v1 残留字段应被丢弃",
        },
        "expect": {"score": 50, "level": "弱利多"},
        "note": "v1 sentiment 字段被忽略（不应出现在输出）",
    },
]


def _run_normalize_cases() -> int:
    print("\n=== 用例 2：_normalize_theme 字段规范化 ===")
    fails = 0
    for case in _NORMALIZE_CASES:
        actual = ThemeExtractor._sanitize_theme(case["input"])
        if actual is None:
            print(f"  [FAIL] {case['note']}: _sanitize_theme 返回 None（reason 可能被过滤）")
            fails += 1
            continue
        expect = case["expect"]
        note = case["note"]

        score_ok = actual.get("strength_score") == expect["score"]
        level_ok = actual.get("strength_level") == expect["level"]
        no_sentiment = "sentiment" not in actual

        all_ok = score_ok and level_ok and no_sentiment
        marker = "[OK]" if all_ok else "[FAIL]"
        print(f"  {marker} {note}")
        print(f"       score   expected={expect['score']:+4d}  actual={actual.get('strength_score')}")
        print(f"       level   expected={expect['level']:6s}  actual={actual.get('strength_level')}")
        print(f"       no_sentiment={no_sentiment}")
        if not all_ok:
            fails += 1
    print(f"  小计：{len(_NORMALIZE_CASES) - fails}/{len(_NORMALIZE_CASES)} 通过")
    return fails


# ---------------------------------------------------------------------------
# 用例 3: parse_report_meta 路径规范化（P1 修复验证）
# ---------------------------------------------------------------------------
def _run_meta_cases() -> int:
    print("\n=== 用例 3：parse_report_meta（P1 路径规范化）===")
    fails = 0

    test_cases = [
        # (输入路径, 期望 report_path 必须以这个开头/或这个相对路径)
        ("data/AI_analysis/5月22日/5月22日_18时25分_盘后总结分析报告.md",
         "data/AI_analysis"),
        ("F:\\NewsTrading\\data\\AI_analysis\\5月22日\\xxx.md",
         "data/AI_analysis"),
        ("./data/AI_analysis/5月22日/xxx.md",
         "data/AI_analysis"),
    ]

    for raw_path, expect_prefix in test_cases:
        meta = parse_report_meta(raw_path)
        rel_path = meta.get("report_path", "")

        # 关键校验：
        # 1) 必须是相对路径（不含盘符 F:、不以 / 开头）
        # 2) 必须用正斜杠（无 \）
        # 3) 必须以期望前缀开头
        is_relative = not (rel_path.startswith("/") or
                           (len(rel_path) >= 2 and rel_path[1] == ":"))
        no_backslash = "\\" not in rel_path
        has_prefix = rel_path.startswith(expect_prefix)

        all_ok = is_relative and no_backslash and has_prefix
        marker = "[OK]" if all_ok else "[FAIL]"
        print(f"  {marker} raw={raw_path}")
        print(f"       rel_path={rel_path}")
        print(f"       is_relative={is_relative}  no_backslash={no_backslash}  has_prefix={has_prefix}")
        if not all_ok:
            fails += 1

    print(f"  小计：{len(test_cases) - fails}/{len(test_cases)} 通过")
    return fails


# ---------------------------------------------------------------------------
# 用例 4: 端到端模拟（fake LLM 输出 -> _parse_response -> matcher 富化）
# ---------------------------------------------------------------------------
_FAKE_LLM_OUTPUT = """
{
  "themes": [
    {
      "theme_name": "先进封装",
      "theme_category": "科技AI",
      "strength_score": 75,
      "reason": "AI 芯片需求拉动 Chiplet 技术",
      "stocks": [
        {"name": "晶方科技", "code": "603005", "role": "核心"},
        {"name": "通富微电", "code": "002156", "role": "核心"}
      ],
      "news": ["新闻101"]
    },
    {
      "theme_name": "未匹配上的奇怪题材xxx",
      "strength_score": -30,
      "reason": "测试 matcher miss 不影响后续",
      "stocks": [{"name": "未知股票", "code": "999999"}]
    }
  ]
}
"""


def _run_e2e_cases() -> int:
    print("\n=== 用例 4：端到端 fake LLM -> 解析 -> matcher 富化 ===")
    fails = 0

    extractor = ThemeExtractor.__new__(ThemeExtractor)
    extractor._last_finish_reason = "stop"

    themes, err = extractor._parse_response(_FAKE_LLM_OUTPUT)
    if err:
        print(f"  [WARN] 解析返回 err: {err}")

    # 模拟 extract_from_text 末尾的 matcher 富化
    if themes:
        try:
            from services.scoring.matcher import enrich_themes_with_matcher
            enrich_themes_with_matcher(themes)
        except Exception as e:
            print(f"  [FAIL] matcher 富化失败: {e}")
            fails += 1

    for theme in themes:
        name = theme.get("theme_name")
        score = theme.get("strength_score")
        level = theme.get("strength_level")
        sector = theme.get("sector_ts_code")
        conf = theme.get("sector_match_conf")
        no_sentiment = "sentiment" not in theme
        print(f"  [theme] {name}")
        print(f"          score={score}  level={level}  no_sentiment={no_sentiment}")
        print(f"          sector_ts_code={sector}  conf={conf}")
        for stock in theme.get("stocks") or []:
            n = stock.get("name")
            raw = stock.get("code")
            norm = stock.get("normalized_code")
            print(f"          stock={n}  raw={raw}  normalized={norm}")
        if not no_sentiment:
            print("          [FAIL] sentiment 字段未删除")
            fails += 1

    # 期望
    # 1) 第一条命中先进封装 BK1101.DC，标的全部规范化
    # 2) 第二条未命中，sector_ts_code = None，但 conf 有值
    if themes:
        first = themes[0]
        if first.get("sector_ts_code") != "BK1101.DC":
            print(f"  [FAIL] 第一条期望 sector=BK1101.DC，实际 {first.get('sector_ts_code')}")
            fails += 1
        first_stocks_norm = [s.get("normalized_code") for s in first.get("stocks") or []]
        if not all(first_stocks_norm):
            print(f"  [FAIL] 第一条标的应全部规范化: {first_stocks_norm}")
            fails += 1
        if len(themes) >= 2:
            second = themes[1]
            if second.get("sector_ts_code") is not None:
                print(f"  [FAIL] 第二条应未命中 sector，实际 {second.get('sector_ts_code')}")
                fails += 1

    print(f"  小计：{0 if fails == 0 else fails} 个失败")
    return fails


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "--case", choices=["score", "parse", "meta", "e2e", "all"],
        default="all",
    )
    args = parser.parse_args()

    total_fails = 0
    if args.case in ("score", "all"):
        total_fails += _run_score_cases()
    if args.case in ("parse", "all"):
        total_fails += _run_normalize_cases()
    if args.case in ("meta", "all"):
        total_fails += _run_meta_cases()
    if args.case in ("e2e", "all"):
        total_fails += _run_e2e_cases()

    print(f"\n========== 总结：{total_fails} 个失败 ==========")
    return 1 if total_fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[用户中断]")
        sys.exit(2)
