"""AI 板块代码准确率回归测试（用于验证是否能让 AI 直接给 sector_ts_code）。

历史结论：
    2026-05-27 首次实测 DeepSeek V4 Pro：准确率 7.4% (2/27)
    → 维持决策 A2 = No（AI 不给板块代码，纯 matcher 模糊匹配）

何时重跑：
    - DeepSeek 升级到 v5 / 接入新模型时
    - 想验证其他厂商（OpenAI / 智谱 / 通义千问）准确率
    - dim_sector 数据扩充后重新评估

用法：
    python tools/test_ai_sector_code_accuracy.py [--provider deepseek|qwen|...]

测试集设计:
    - 主流热门: 先进封装、人形机器人、培育钻石、PCB、玻璃基板
    - 中等关注: 黄金概念、稀缺资源、小金属、电子纸、屏下摄像
    - 冷门小众: PEEK 材料、纳米银、地热能、东数西算、F5G

流程:
    1) 从 dim_sector 取真实样本 (ts_code, name)
    2) 让 AI 仅给 ts_code（强制 JSON 输出）
    3) 对比正确率
    4) 输出汇总 + 决策建议（≥80% 改 A2=Yes / 50~80% 双保险 / <50% 维持 No）
"""
from __future__ import annotations
import json
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.ai_config import AIConfig


SAMPLE_NAMES = [
    # 主流热门
    "先进封装", "人形机器人", "培育钻石", "PCB", "玻璃基板",
    "存储器", "AI算力", "光伏", "新能源车", "锂电池",
    # 中等关注
    "黄金概念", "稀缺资源", "小金属概念", "电子纸概念", "屏下摄像",
    "白酒", "医药", "军工", "光通信", "数据中心",
    # 冷门小众
    "PEEK材料概念", "纳米银", "地热能", "F5G概念", "东数西算",
    "氢能", "钠离子电池", "脑机接口", "可控核聚变", "工业母机",
]


def get_ground_truth() -> dict:
    """从 dim_sector 取真实代码（按 name 完全/模糊匹配，取最高置信度）。"""
    db = sqlite3.connect("data/market.db")
    try:
        rows = db.execute(
            "SELECT ts_code, name FROM dim_sector"
        ).fetchall()
    finally:
        db.close()

    truth = {}
    for name in SAMPLE_NAMES:
        # 完全相等
        exact = [(c, n) for c, n in rows if n == name]
        if exact:
            truth[name] = exact[0][0]
            continue
        # 包含关系（短者在长者里）
        contains = [(c, n) for c, n in rows if name in n or n in name]
        if contains:
            # 取名字最短的（最贴近原意）
            contains.sort(key=lambda x: abs(len(x[1]) - len(name)))
            truth[name] = contains[0][0]
            continue
        truth[name] = None
    return truth


def call_deepseek(names: list[str]) -> dict:
    """一次性问 DeepSeek 所有板块名 → 返回 {name: code} 字典。"""
    from openai import OpenAI

    cfg = AIConfig()
    pcfg = cfg.get_provider_config("deepseek")
    api_key = pcfg.get("api_key")
    base_url = pcfg.get("base_url")
    model = pcfg.get("model", "deepseek-v4-pro")

    if not api_key:
        raise SystemExit("[FAIL] 未配置 deepseek API Key")

    client = OpenAI(api_key=api_key, base_url=base_url)

    system_prompt = (
        "你是 A 股板块代码专家。东方财富对每个概念板块分配一个 BK 代码（格式 BKxxxx.DC）。"
        "用户给你一个板块名列表，你必须返回严格 JSON 对象 {板块名: BK代码}。"
        "不知道的板块代码填 null，不要编造。不要给同花顺(THS)或其他口径的代码。"
    )
    user_prompt = (
        "请给出以下 A 股概念板块的东方财富 BK 代码（格式 BKxxxx.DC）：\n"
        + json.dumps(names, ensure_ascii=False, indent=2)
        + "\n\n直接返回 JSON 对象 {板块名: BK代码}，不要任何其他文字。"
    )

    print(f"[Info] 调用 {model} 中...")
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    elapsed = time.time() - t0
    content = resp.choices[0].message.content
    print(f"[Info] 耗时 {elapsed:.1f}s, 返回 {len(content)} 字符")

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[Warn] JSON 解析失败: {e}")
        print(content[:500])
        return {}


def main() -> int:
    print("=" * 70)
    print("  DeepSeek V4 Pro 板块代码熟悉度测试")
    print("=" * 70)

    truth = get_ground_truth()
    truth_have = sum(1 for v in truth.values() if v)
    print(f"\n[Step 1] dim_sector 真实数据覆盖: {truth_have}/{len(SAMPLE_NAMES)}")

    ai_answer = call_deepseek(SAMPLE_NAMES)
    print(f"\n[Step 2] AI 返回 {len(ai_answer)} 项")

    # 对比
    correct = 0
    partial = 0       # AI 给了非空但与真实不符
    miss = 0          # AI 说不知道 (None)
    wrong = 0         # AI 编造（真实是 None，AI 给了具体）
    no_truth = 0      # 真实数据缺失，AI 给的没法验证

    print("\n" + "=" * 90)
    print(f"  {'板块名':<14} {'真实代码':<14} {'AI 代码':<14} {'结果':<8}")
    print("-" * 90)
    rows = []
    for name in SAMPLE_NAMES:
        gt = truth.get(name)
        ai = ai_answer.get(name)

        if gt is None:
            status = "[无真实] " + ("AI 给了" if ai else "AI 也不知")
            no_truth += 1
            color = "?"
        elif ai is None:
            status = "[未知]"
            miss += 1
            color = "M"
        elif ai == gt:
            status = "[正确]"
            correct += 1
            color = "Y"
        elif ai and gt and ai.split(".")[0] == gt.split(".")[0]:
            status = "[代码相同后缀不同]"
            correct += 1  # 算对
            color = "Y"
        else:
            status = "[错误]"
            partial += 1
            color = "N"

        print(f"  {name:<14} {str(gt or '—'):<14} {str(ai or '—'):<14} {color}  {status}")
        rows.append((name, gt, ai, color))

    print("=" * 90)

    valid_total = len([r for r in rows if r[1] is not None])
    if valid_total > 0:
        accuracy = correct / valid_total * 100
    else:
        accuracy = 0.0

    print(f"\n[Step 3] 统计")
    print(f"  样本总数:       {len(SAMPLE_NAMES)}")
    print(f"  真实数据覆盖:   {truth_have}")
    print(f"  正确 (Y):       {correct}")
    print(f"  错误 (N):       {partial}")
    print(f"  AI 说不知 (M):  {miss}")
    print(f"  真实缺失 (?):   {no_truth}")
    print(f"")
    print(f"  >>> 准确率（基于真实数据）: {correct}/{valid_total} = {accuracy:.1f}%")

    # 结论
    print("\n[Step 4] 结论建议")
    if accuracy >= 80:
        print(f"  AI 准确率 ≥80%，可以让 AI 直接给 sector_ts_code")
        print(f"  对 A2 决策的建议: 改为 Yes（matcher 仍作兜底）")
    elif accuracy >= 50:
        print(f"  AI 准确率中等，建议 AI 给 + matcher 校验双保险")
    else:
        print(f"  AI 准确率 < 50%，维持原决策（不让 AI 给代码，纯 matcher）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
