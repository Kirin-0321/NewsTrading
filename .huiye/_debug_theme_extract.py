"""调试用：跑一次真实题材抽取，把输入/输出/token 估算全部落盘。

输出：
    .huiye/_debug_io/01_system_prompt.txt      系统提示词
    .huiye/_debug_io/02_user_prompt.txt        用户提示词（含报告原文）
    .huiye/_debug_io/03_raw_response.txt       AI 原始返回
    .huiye/_debug_io/04_parsed_themes.json     解析后的题材结构
    .huiye/_debug_io/05_summary.md             token / 性能统计汇总

不入库，纯调试。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.ai_config import AIConfig
from core.theme_extractor import (
    ThemeExtractor,
    _SYSTEM_PROMPT,
    _USER_PROMPT_TEMPLATE,
)

REPORT_PATH = "data/AI_analysis/5月22日/5月22日_18时48分_盘后总结分析报告.md"
OUT_DIR = os.path.join(os.path.dirname(__file__), "_debug_io")
os.makedirs(OUT_DIR, exist_ok=True)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文按 1 char ≈ 0.6 token，英文按 4 char ≈ 1 token）。"""
    if not text:
        return 0
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cn
    return int(cn * 0.6 + other / 4)


def main():
    # 1. 读取并截断报告
    abspath = os.path.abspath(REPORT_PATH)
    print(f"[1] 读取报告: {abspath}")
    with open(abspath, "r", encoding="utf-8") as f:
        raw = f.read()
    idx = raw.find("## 📰 引用新闻详情")
    report_text = raw[:idx].rstrip() if idx > 0 else raw
    print(f"    原始 {len(raw)} 字符，截断后 {len(report_text)} 字符")

    # 2. 构造 prompts
    system_prompt = _SYSTEM_PROMPT
    user_prompt = _USER_PROMPT_TEMPLATE.format(report_text=report_text)

    sp_tok = estimate_tokens(system_prompt)
    up_tok = estimate_tokens(user_prompt)
    print(f"[2] system_prompt: {len(system_prompt)} 字符, ≈ {sp_tok} tokens")
    print(f"    user_prompt:   {len(user_prompt)} 字符, ≈ {up_tok} tokens")
    print(f"    输入合计:       ≈ {sp_tok + up_tok} tokens")

    # 落盘
    with open(os.path.join(OUT_DIR, "01_system_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(system_prompt)
    with open(os.path.join(OUT_DIR, "02_user_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(user_prompt)

    # 3. 调用 LLM
    cfg = AIConfig()
    ext_cfg = cfg.get_theme_extraction_config()
    print(f"[3] 调用 {ext_cfg['provider']} / {ext_cfg['model']} "
          f"(max_tokens={ext_cfg['max_tokens']}, temp={ext_cfg['temperature']})")

    extractor = ThemeExtractor()
    t0 = time.time()
    try:
        raw_response = extractor._call_llm(report_text)
    except Exception as e:
        print(f"    LLM 调用失败: {e}")
        return
    elapsed = time.time() - t0
    finish_reason = extractor._last_finish_reason
    out_tok = estimate_tokens(raw_response)
    print(f"    耗时: {elapsed:.1f}s")
    print(f"    finish_reason: {finish_reason}")
    print(f"    raw_response: {len(raw_response)} 字符, ≈ {out_tok} tokens")

    with open(os.path.join(OUT_DIR, "03_raw_response.txt"), "w", encoding="utf-8") as f:
        f.write(raw_response)

    # 4. 解析
    themes, err = extractor._parse_response(raw_response)
    print(f"[4] 解析: 题材数={len(themes)}, err={err}")
    with open(os.path.join(OUT_DIR, "04_parsed_themes.json"), "w", encoding="utf-8") as f:
        json.dump({"themes": themes, "error": err}, f, ensure_ascii=False, indent=2)

    # 5. 汇总
    total_in = sp_tok + up_tok
    total_all = total_in + out_tok

    # DeepSeek V4-Flash 计费参考（2026-04 公布价格）
    # 输入 0.5 元/百万 tok，输出 4.0 元/百万 tok
    cost_in = total_in * 0.5 / 1_000_000
    cost_out = out_tok * 4.0 / 1_000_000
    cost_total = cost_in + cost_out

    summary = f"""# 题材抽取 token 开销调试

## 输入

| 项目 | 字符数 | 估算 tokens |
|------|-------:|------------:|
| 报告原文 | {len(raw)} | - |
| 截断引用区后 | {len(report_text)} | - |
| system_prompt | {len(system_prompt)} | {sp_tok} |
| user_prompt (含报告) | {len(user_prompt)} | {up_tok} |
| **输入合计** | - | **{total_in}** |

## 输出

| 项目 | 值 |
|------|---|
| 模型 | {ext_cfg['provider']} / {ext_cfg['model']} |
| max_tokens 配置 | {ext_cfg['max_tokens']} |
| finish_reason | {finish_reason} |
| 响应字符数 | {len(raw_response)} |
| 响应估算 tokens | {out_tok} |
| 调用耗时 | {elapsed:.1f}s |
| 解析题材数 | {len(themes)} |
| 解析错误 | {err or '无'} |

## 成本估算（DeepSeek V4-Flash 价格表，仅供参考）

| 项目 | tokens | 单价（元/百万） | 成本（元） |
|------|-------:|----------------:|----------:|
| 输入 | {total_in} | 0.5 | {cost_in:.6f} |
| 输出 | {out_tok} | 4.0 | {cost_out:.6f} |
| **合计** | {total_all} | - | **{cost_total:.6f}** |

## 题材列表

"""
    for i, t in enumerate(themes, 1):
        summary += f"{i}. **{t['theme_name']}** | {t['strength_level']}{t['strength_score']} | {t['sentiment']} | stocks={len(t.get('stocks') or [])} | news={len(t.get('news') or [])}\n"

    with open(os.path.join(OUT_DIR, "05_summary.md"), "w", encoding="utf-8") as f:
        f.write(summary)

    print()
    print("=" * 60)
    print(f"汇总已写入: {OUT_DIR}")
    print("=" * 60)
    print(summary)


if __name__ == "__main__":
    main()
