"""
将 AnalysisService 实际发送给 AI 的完整 prompt（system + user）dump 到 markdown 文件。

用途：
    - 调试 prompt（模板内容、新闻编号是否对齐）
    - 检查盘后总结/auto 模式渲染结果
    - 估算 token 用量，避免触发模型 context 上限

链路：
    SQLite -> AnalysisService.load_news
        -> AnalysisService._write_temp_json
        -> DataLoader.load -> DataLoader.format_for_ai (1-based 编号文本)
    AIConfig.prompt_templates[current]
        -> AINewsAnalyzer.get_system_prompt -> system_prompt
        -> AINewsAnalyzer.build_user_prompt(news_data, ...) -> user_prompt
    最终: [{"role":"system",system_prompt}, {"role":"user",user_prompt}]

用法示例:
    py tools/show_ai_input.py                                # 默认 curated + 最近 24h
    py tools/show_ai_input.py --source raw --hours 6
    py tools/show_ai_input.py --template short_term --market-summary "今日大盘下跌1.5%"
    py tools/show_ai_input.py --start "2026-05-22 09:00:00" --end "2026-05-22 15:00:00"
    py tools/show_ai_input.py --limit 20 --output ./debug_prompt.md
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Union

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.analysis_service import AnalysisService  # noqa: E402
from core.data_loader import DataLoader  # noqa: E402
from core.ai_news_analyzer import AINewsAnalyzer  # noqa: E402
from core.ai_config import AIConfig  # noqa: E402


def _int_or_auto(value: str) -> Union[int, str]:
    """argparse 类型：接受整数或字符串 'auto'。"""
    if value == "auto":
        return "auto"
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"必须是整数或 'auto'，收到: {value!r}"
        ) from exc


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump 完整 AI prompt 输入（system + user）到 markdown 文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例:", 1)[1] if "用法示例:" in __doc__ else "",
    )
    parser.add_argument(
        "--source", choices=["curated", "raw"], default="curated",
        help="数据源：curated 精选库 / raw 原始库（默认 curated）",
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="最近 N 小时（与 --start/--end 互斥，默认 24）",
    )
    parser.add_argument(
        "--start", type=_parse_dt, default=None,
        help='起始时间 "YYYY-MM-DD HH:MM:SS"（覆盖 --hours）',
    )
    parser.add_argument(
        "--end", type=_parse_dt, default=None,
        help='结束时间 "YYYY-MM-DD HH:MM:SS"（默认当前时间）',
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="最多取 N 条（默认不截断）",
    )
    parser.add_argument(
        "--template", default=None,
        help="模板 ID（standard / short_term / aggressive / conservative / value 等），"
             "默认取 AIConfig 当前模板",
    )
    parser.add_argument(
        "--max-sectors", default="auto", type=_int_or_auto,
        help="板块数（整数或 'auto'，默认 auto）",
    )
    parser.add_argument(
        "--stocks-per-sector", default="auto", type=_int_or_auto,
        help="每板块股票数（整数或 'auto'，默认 auto）",
    )
    parser.add_argument(
        "--market-summary", default=None,
        help="盘后总结文本（可选，short_term 模板会嵌入）",
    )
    parser.add_argument(
        "--output", default=os.path.join(".huiye", "ai_input_sample.md"),
        help="输出 markdown 文件路径（默认 .huiye/ai_input_sample.md）",
    )
    args = parser.parse_args()

    # ---- 时间范围 ----
    end = args.end or datetime.now()
    start = args.start or (end - timedelta(hours=args.hours))
    if start >= end:
        print(
            f"[ERROR] 起始时间必须早于结束时间: {start} ~ {end}",
            file=sys.stderr,
        )
        return 2

    # ---- 取数 + 走 service 同样链路 ----
    service = AnalysisService()
    news_list = service.load_news(source=args.source, start=start, end=end)
    if args.limit:
        news_list = news_list[: args.limit]

    if not news_list:
        print(
            f"[WARN] 选定范围内无新闻 (source={args.source}, "
            f"{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')})"
        )
        return 1

    temp_path = service._write_temp_json(news_list, args.source)
    try:
        loader = DataLoader()
        data = loader.load(temp_path)
        news_data = loader.format_for_ai(data["news_list"])
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    # ---- 模板 + 服务商 ----
    cfg = AIConfig()
    template_id = args.template or cfg.get_current_prompt_template()
    template = cfg.get_prompt_template(template_id) or {}
    provider = cfg.get_current_provider()
    model = cfg.get_model(provider)

    analyzer = AINewsAnalyzer(config=cfg)
    system_prompt = analyzer.get_system_prompt(template_id)
    user_prompt = analyzer.build_user_prompt(
        news_data,
        max_sectors=args.max_sectors,
        stocks_per_sector=args.stocks_per_sector,
        template_name=template_id,
        market_summary=args.market_summary,
    )

    sys_tokens = loader.estimate_tokens(system_prompt)
    user_tokens = loader.estimate_tokens(user_prompt)
    total_tokens = sys_tokens + user_tokens

    # ---- 写报告 ----
    out_path = os.path.abspath(args.output)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    lines = [
        "# AI 完整输入样本（system + user prompt）",
        "",
        f"- 生成时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- 数据源: `{args.source}`",
        f"- 时间范围: `{start.strftime('%Y-%m-%d %H:%M:%S')}` ~ "
        f"`{end.strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- 新闻条数: **{len(news_list)}** 条"
        + (f"（已截断至 {args.limit}）" if args.limit else ""),
        f"- 服务商: `{provider}` | 模型: `{model}`",
        f"- 模板: `{template_id}`（{template.get('name', '未知模板')}）",
        f"- max_sectors: `{args.max_sectors}` | "
        f"stocks_per_sector: `{args.stocks_per_sector}`",
        f"- 盘后总结: {'已注入（' + str(len(args.market_summary)) + ' 字）' if args.market_summary else '无'}",
        f"- 估算 tokens: system **{sys_tokens}** + user **{user_tokens}** "
        f"= **{total_tokens}**",
        "",
        "> 注：实际发送给 AI 的消息为 "
        "`[{role:system, content:<下方 1>}, {role:user, content:<下方 2>}]`",
        "",
        "---",
        "",
        "## 1. System Prompt",
        "",
        "```text",
        system_prompt,
        "```",
        "",
        "---",
        "",
        "## 2. User Prompt（含 1-based 编号的新闻数据）",
        "",
        "```text",
        user_prompt,
        "```",
        "",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[OK] 已生成: {out_path}")
    print(
        f"     新闻 {len(news_list)} 条 | "
        f"system={sys_tokens} + user={user_tokens} = {total_tokens} tokens"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
