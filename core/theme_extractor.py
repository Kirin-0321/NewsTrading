"""题材抽取器：读取分析报告 → AI 结构化输出 → 题材列表。

调用链:
    AnalysisService.analyze() 生成报告.md
        └→ ThemeExtractor.extract_from_file(report_path, progress_callback)
                ├→ _read_report()           # 读 md，去掉末尾引用区
                ├→ parse_news_id_map()      # 解析底部'**数据库ID**:'映射
                ├→ _call_llm(stream=True)   # 流式 + JSON mode + chunk 回调
                ├→ _parse_response()        # 容错解析（4 级兜底）
                └→ apply_priority_ranks_from_report()  # 从正文 7.2 表补全排序
        └→ ThemeStore.save_themes(meta, themes, news_id_map=...)

设计要点:
    - 不复用 ai_news_analyzer._stream_chat：分类抽取不需要思考模式
    - 默认 deepseek-v4-flash + temperature=0.2，分类任务降低随机性
    - stream=True + response_format=json_object：chunk 实时推 UI，避免 60-90s 假死
    - 4 级 JSON 容错：直接 loads → 正则提 {} → 截断修复 → 字符级救援
"""

import json
import os
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from core.ai_config import AIConfig, DEFAULT_MAX_OUTPUT_TOKENS


_REPORT_REF_HEADER = "## 📰 引用新闻详情"

_VALID_LEVELS = {"极强", "强", "中", "弱"}
_VALID_SENTIMENT = {"利好", "利空", "中性"}
_VALID_DURATION = {"短期", "中期", "长期"}
_VALID_GAP = {"高", "中高", "中", "中低", "低"}
_VALID_CATEGORY = {"科技AI", "新能源", "基建", "消费", "医药", "金融", "军工", "周期", "其他"}
_VALID_ROLE = {"核心", "上游", "中游", "下游", "潜力", "边缘"}
_VALID_RELATION = {"主因", "共振", "风险", "背景"}

_SYSTEM_PROMPT = """你是一名股票题材分析助手。你的任务是阅读一份 A 股盘后分析报告（Markdown），把报告中提到的所有题材（板块/方向）抽取为结构化 JSON。

【字段规范】严格按以下 schema 输出，未知字段直接省略键名（不要填 null）：

{
  "themes": [
    {
      "theme_name": "题材名（必填，如 '具身智能/人形机器人'）",
      "theme_category": "大类（科技AI/新能源/基建/消费/医药/金融/军工/周期/其他）",
      "strength_score": 0-100 整数（必填，综合优先级与影响强度打分）,
      "strength_level": "极强/强/中/弱（必填，对应🔴🟠🟡🟢）",
      "priority_rank": 报告'板块优先级排序'中的序号（整数）,
      "duration": "短期/中期/长期",
      "expectation_gap": "高/中高/中/中低/低",
      "sentiment": "利好/利空/中性（必填）",
      "is_cold": 0 或 1（必填，钝化/冷处理题材填 1，如中东、黄金、霍尔木兹等）,
      "reason": "1-3 句话核心逻辑（必填），融合：①利好/利空逻辑 ②催化事件，用'。'分隔",
      "risk_note": "仅在题材确有明显风险时填写（短句）",
      "stocks": [
        {
          "name": "股票中文名（必填）",
          "code": "如 '601689.SH'（不确定则省略）",
          "role": "核心/上游/中游/下游/潜力/边缘",
          "reason": "入选原因"
        }
      ],
      "news": ["新闻107", "新闻125"]
    }
  ]
}

【news 字段格式】
- 标准形式：字符串数组 ["新闻107", "新闻125"]，每项就是报告里的锚点 ref。
- 不要输出 title 或新闻原文——脚本会通过报告底部反查数据库取原文。
- 若必须标注关系类型，用扩展形式 [{"ref":"新闻107","rel":"主因"}]，否则一律用字符串数组。

【抽取规则】
1. 报告中"板块机会汇总""多逻辑共振""预期差挖掘""板块优先级排序""重点观察标的"等章节出现的所有题材都要抽
2. 同一题材在不同章节重复出现时，合并为一条，信息取最完整版本
3. 强度评分参考：🔴极强 80-100 / 🟠强 60-79 / 🟡中 40-59 / 🟢弱 0-39；并与多逻辑共振数、⭐数综合
4. strength_score 与 strength_level 必须自洽（按上面区间）
5. 风险提示、钝化题材也要抽，sentiment 填 '利空' 或 '中性'，is_cold 填 1（如适用）
6. stocks 数组要尽可能完整：核心标的、产业链上下游、潜力挖掘标的都要收
7. news 数组只填能直接看到锚点（如 [新闻107](#新闻107)）的，不要编造

【输出要求】
- 必须返回纯 JSON 对象（不要 markdown 代码块包裹，不要任何说明文字）
- **未知字段直接省略键名，不要填 null**（重要：可省 token）
- 不要编造报告中不存在的题材或标的"""

_USER_PROMPT_TEMPLATE = """请抽取以下分析报告中的题材，按系统提示词中的 schema 输出 JSON。

【分析报告全文】
{report_text}

请直接输出 JSON 对象，不要任何前后文字。"""


class ThemeExtractor:
    """从分析报告中抽取结构化题材。"""

    def __init__(
        self,
        config: Optional[AIConfig] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ):
        self.config = config or AIConfig()
        ext_cfg = self.config.get_theme_extraction_config()
        self.provider = provider or ext_cfg.get("provider") or "deepseek"
        self.model = model or ext_cfg.get("model") or "deepseek-v4-flash"
        self.temperature = ext_cfg.get("temperature", temperature)
        self.max_tokens = int(ext_cfg.get("max_tokens", max_tokens))
        try:
            self.timeout = float(ext_cfg.get("timeout", 1200))
        except (TypeError, ValueError):
            self.timeout = 1200.0
        self._last_finish_reason: Optional[str] = None
        self._last_raw_response: Optional[str] = None

    def extract_from_file(
        self,
        report_path: str,
        progress_callback: Optional[Callable] = None,
    ) -> Tuple[List[Dict], Dict[str, str], Optional[str]]:
        """读 .md → 抽题材，同时建立"新闻N → raw_news.id"映射。

        Args:
            progress_callback: 形如 ``cb(msg, is_streaming=False)``，
                流式 chunk 时 ``is_streaming=True`` 直接拼接到 UI；
                阶段提示时 ``is_streaming=False`` 带时间戳。

        Returns:
            (themes, news_id_map, error)
                - themes      : 抽取出的题材列表
                - news_id_map : {'新闻107': 'curated_id', ...}，从报告底部解析
                - error       : 错误/警告字符串，None 表示无错
        """
        news_id_map: Dict[str, str] = {}
        try:
            report_text = self._read_report(report_path)
        except Exception as e:
            return [], news_id_map, f"读取报告失败: {e}"

        if not report_text.strip():
            return [], news_id_map, "报告内容为空"

        news_id_map = parse_news_id_map(report_path)
        themes, err = self.extract_from_text(report_text, progress_callback)
        themes = apply_priority_ranks_from_report(themes, report_text)
        return themes, news_id_map, err

    def extract_from_text(
        self,
        report_text: str,
        progress_callback: Optional[Callable] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        """对已读入的文本抽题材。"""
        try:
            raw_response = self._call_llm(report_text, progress_callback)
        except Exception as e:
            err = str(e)
            if "timed out" in err.lower():
                return [], (
                    f"调用 LLM 失败: {err}。"
                    f"（读超时约 {int(self.timeout)}s；可在 config/ai_config.json 调高 "
                    f"theme_extraction.timeout，或降低 theme_extraction.max_tokens）"
                )
            return [], f"调用 LLM 失败: {e}"

        self._last_raw_response = raw_response
        themes, err = self._parse_response(raw_response)

        # 截断时即使解析失败也尽量救出前面的题材
        if self._last_finish_reason == "length":
            hint = (
                f"⚠️ AI 输出被截断（finish_reason=length，max_tokens={self.max_tokens}）。"
                "已尝试修复 JSON 取出前面的题材。建议主人调大 config/ai_config.json 的 "
                "theme_extraction.max_tokens。"
            )
            err = f"{hint} | 原解析消息: {err}" if err else hint

        if err and not themes:
            self._dump_failure(report_text, raw_response, err)
            return [], err
        return themes, err  # 部分成功时也带 warning 出去

    @staticmethod
    def _read_report(path: str) -> str:
        """读 .md，去掉末尾的"引用新闻详情"段，节省 token。"""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        idx = text.find(_REPORT_REF_HEADER)
        if idx > 0:
            text = text[:idx].rstrip()
        return text

    @staticmethod
    def _http_timeout(seconds: float):
        """构建 httpx 超时：流式场景 read 为相邻 chunk 间最大等待秒数。"""
        from httpx import Timeout

        sec = max(float(seconds), 60.0)
        return Timeout(connect=15.0, read=sec, write=sec, pool=sec)

    def _call_llm(
        self,
        report_text: str,
        progress_callback: Optional[Callable] = None,
    ) -> str:
        """流式调用 LLM，请求 JSON 输出。

        chunk 一边到达一边通过 progress_callback(msg, is_streaming=True) 推给 UI，
        避免 60-90s 长任务无任何反馈的"假死"观感。
        """
        provider_cfg = self.config.get_provider_config(self.provider)
        api_key = provider_cfg.get("api_key")
        if not api_key:
            raise ValueError(f"未配置 {self.provider} API Key")

        http_timeout = self._http_timeout(self.timeout)
        if self.provider == "zhipu":
            try:
                from zhipuai import ZhipuAI
            except ImportError:
                raise ImportError("请安装 zhipuai: pip install zhipuai")
            client = ZhipuAI(api_key=api_key, timeout=http_timeout)
        else:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("请安装 openai: pip install openai")
            base_url = provider_cfg.get("base_url")
            if self.provider == "qwen" and not base_url:
                base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=http_timeout)

        user_prompt = _USER_PROMPT_TEMPLATE.format(report_text=report_text)

        # 注意：故意 *不* 加 response_format=json_object。
        # DeepSeek / OpenAI 在 JSON mode 下，服务端会先把完整 JSON 生成完
        # 才开始 SSE 推送，导致流式表现成"等几十秒 → 一次性爆发"。
        # 改为纯文本流式 + prompt 强约束 JSON + 4 级容错解析(已实现)，
        # 在主流场景下 chunks 真正逐字到达，UI 反馈实时。
        create_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        # 与主分析器一致：V4 默认 thinking，分类抽取必须关闭，否则长时间无 content 易触发读超时
        if self.provider == "deepseek" and str(self.model).startswith("deepseek-v4"):
            create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        response = client.chat.completions.create(**create_kwargs)

        content_parts: List[str] = []
        last_finish: Optional[str] = None
        for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            # 消费 reasoning_content，保持 SSE 活跃，避免思考阶段长时间无数据导致读超时
            _ = getattr(delta, "reasoning_content", None)
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
                if progress_callback:
                    try:
                        progress_callback(content, is_streaming=True)
                    except Exception:
                        pass  # UI 回调异常不影响主流程
            # finish_reason 只在最后一个 chunk 给出
            fr = getattr(choice, "finish_reason", None)
            if fr:
                last_finish = fr

        self._last_finish_reason = last_finish
        return "".join(content_parts)

    @staticmethod
    def _parse_response(text: str) -> Tuple[List[Dict], Optional[str]]:
        """容错解析：先 json.loads → 正则提 {} → 截断修复 → 提取已完成的题材项。"""
        if not text:
            return [], "LLM 返回为空"

        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)

        # 第一层：直接解析
        obj, parse_err = ThemeExtractor._try_load_json(stripped)
        if obj is None:
            # 第二层：正则提最外层 {}（应对前后带说明文字的情况）
            match = re.search(r"\{[\s\S]*\}", stripped)
            if match:
                obj, parse_err = ThemeExtractor._try_load_json(match.group(0))

        # 第三层：截断修复（移除尾随逗号 + 补全闭合括号）
        if obj is None:
            repaired = ThemeExtractor._repair_truncated_json(stripped)
            if repaired:
                obj, parse_err = ThemeExtractor._try_load_json(repaired)

        # 第四层：从原文里逐条扫"themes 数组的对象"
        if obj is None:
            partial = ThemeExtractor._salvage_theme_objects(stripped)
            if partial:
                cleaned = []
                for item in partial:
                    theme = ThemeExtractor._sanitize_theme(item)
                    if theme:
                        cleaned.append(theme)
                if cleaned:
                    return cleaned, f"JSON 解析失败但已抢救 {len(cleaned)} 条题材: {parse_err}"
            return [], f"JSON 解析失败: {parse_err}"

        themes_raw = obj.get("themes") if isinstance(obj, dict) else None
        if not isinstance(themes_raw, list):
            return [], "JSON 缺少 themes 数组"

        cleaned: List[Dict] = []
        for item in themes_raw:
            if not isinstance(item, dict):
                continue
            theme = ThemeExtractor._sanitize_theme(item)
            if theme:
                cleaned.append(theme)
        return cleaned, None

    @staticmethod
    def _try_load_json(text: str) -> Tuple[Optional[Dict], Optional[str]]:
        try:
            return json.loads(text), None
        except json.JSONDecodeError as e:
            return None, f"{e.msg} at line {e.lineno} column {e.colno}"

    @staticmethod
    def _repair_truncated_json(text: str) -> Optional[str]:
        """截断 JSON 修复：
            1. 在字符串外移除尾随逗号 ,]  ,}
            2. 统计未闭合的 { [ ，按栈序补 } ]
            3. 若末尾停在字符串内部（奇数个未转义"），先截到上一个 " 处再处理

        实现思路要点：必须区分"字符串内/外"——这是 JSON 截断修复的核心难点。
        """
        if not text:
            return None
        text = text.strip()
        if not (text.startswith("{") or text.startswith("[")):
            return None

        in_string = False
        escape = False
        stack: List[str] = []  # 装 { 或 [
        last_safe_idx = -1     # 字符串外的最近一个安全字符位置

        for i, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                    last_safe_idx = i
                continue
            # 字符串外
            if ch == '"':
                in_string = True
                continue
            if ch in "{[":
                stack.append(ch)
                last_safe_idx = i
            elif ch in "}]":
                if stack and ((ch == "}" and stack[-1] == "{") or
                              (ch == "]" and stack[-1] == "[")):
                    stack.pop()
                    last_safe_idx = i
            elif not ch.isspace() and ch != ",":
                last_safe_idx = i

        if in_string:
            # 末尾停在未闭合字符串里 → 砍到最近一个安全位置
            if last_safe_idx < 0:
                return None
            text = text[:last_safe_idx + 1]

        # 去尾部所有空白与悬挂逗号
        text = re.sub(r"[\s,]+$", "", text)

        # 按栈补尾
        for opener in reversed(stack):
            text += "}" if opener == "{" else "]"

        return text

    @staticmethod
    def _salvage_theme_objects(text: str) -> List[Dict]:
        """最后一层兜底：从原始文本里扫"themes": [ {...}, {...} ] 中已完整的对象。

        策略：找到 "themes" 数组开始位置后，逐个字符跟踪 { } 平衡，
        每完整一个对象就 try json.loads，能 parse 的就加入结果。
        """
        m = re.search(r'"themes"\s*:\s*\[', text)
        if not m:
            return []
        start = m.end()

        results: List[Dict] = []
        i = start
        n = len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i >= n or text[i] != "{":
                break
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[i:j + 1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                results.append(obj)
                        except json.JSONDecodeError:
                            pass
                        j += 1
                        break
                j += 1
            if depth != 0:
                # 走到末尾仍未平衡 → 这条对象被截断了，放弃
                break
            i = j
        return results

    @staticmethod
    def _dump_failure(report_text: str, raw_response: str, error: str) -> None:
        """把失败上下文写到 .huiye/_last_theme_extract_failure.txt 便于事后排查。"""
        try:
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..")
            )
            target_dir = os.path.join(project_root, ".huiye")
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, "_last_theme_extract_failure.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write(f"=== 错误 ===\n{error}\n\n")
                f.write(f"=== 报告长度 ===\n{len(report_text)} 字符\n\n")
                f.write(f"=== 原始 LLM 输出 ({len(raw_response)} 字符) ===\n")
                f.write(raw_response)
        except Exception:
            pass

    @staticmethod
    def _sanitize_theme(item: Dict) -> Optional[Dict]:
        """枚举值范围限制 + 空字段剔除，避免脏数据进库。"""
        name = (item.get("theme_name") or "").strip()
        reason = (item.get("reason") or "").strip()
        if not name or not reason:
            return None

        def pick_enum(v, allowed):
            if isinstance(v, str) and v.strip() in allowed:
                return v.strip()
            return None

        def pick_int(v, lo: Optional[int] = None, hi: Optional[int] = None):
            try:
                n = int(v)
            except (TypeError, ValueError):
                return None
            if lo is not None:
                n = max(lo, n)
            if hi is not None:
                n = min(hi, n)
            return n

        stocks_raw = item.get("stocks") or []
        stocks: List[Dict] = []
        if isinstance(stocks_raw, list):
            for s in stocks_raw:
                if not isinstance(s, dict):
                    continue
                sname = (s.get("name") or s.get("stock_name") or "").strip()
                if not sname:
                    continue
                stocks.append({
                    "name": sname,
                    "code": (s.get("code") or s.get("stock_code")) or None,
                    "role": pick_enum(s.get("role"), _VALID_ROLE),
                    "reason": s.get("reason") or None,
                })

        news = ThemeExtractor._normalize_news_field(item.get("news"))

        return {
            "theme_name": name,
            "theme_category": pick_enum(item.get("theme_category"), _VALID_CATEGORY),
            "strength_score": pick_int(item.get("strength_score"), 0, 100),
            "strength_level": pick_enum(item.get("strength_level"), _VALID_LEVELS),
            "priority_rank": pick_int(item.get("priority_rank")),
            "duration": pick_enum(item.get("duration"), _VALID_DURATION),
            "expectation_gap": pick_enum(item.get("expectation_gap"), _VALID_GAP),
            "sentiment": pick_enum(item.get("sentiment"), _VALID_SENTIMENT) or "利好",
            "is_cold": 1 if item.get("is_cold") else 0,
            "reason": reason,
            "risk_note": item.get("risk_note") or None,
            "stocks": stocks,
            "news": news,
        }

    @staticmethod
    def _normalize_news_field(raw) -> List[Dict]:
        """兼容 news 字段的两种格式：

        - 字符串数组（新版精简形式）   ["新闻107", "新闻125"]
        - 对象数组（带 rel 关系类型）  [{"ref": "新闻107", "rel": "主因"}]
        - 老版对象（含 title/relation_type） [{"ref": ..., "title": ..., "relation_type": ...}]

        统一返回 [{"ref": str, "relation_type": Optional[str]}]，
        title 不再保留——下游通过 news_id 反查 raw_news 拿。
        """
        if not isinstance(raw, list):
            return []
        out: List[Dict] = []
        for n in raw:
            ref = None
            rel = None
            if isinstance(n, str):
                ref = n.strip()
            elif isinstance(n, dict):
                ref = (n.get("ref") or n.get("news_ref") or "").strip()
                rel_raw = n.get("rel") or n.get("relation_type")
                if isinstance(rel_raw, str) and rel_raw.strip() in _VALID_RELATION:
                    rel = rel_raw.strip()
            if not ref:
                continue
            out.append({"ref": ref, "relation_type": rel})
        return out


_PRIORITY_SECTION_PAT = re.compile(
    r"(?:#{2,4}\s*)?7\.2\s*板块优先级排序|板块优先级排序",
)
_PRIORITY_TABLE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*(?:\*\*)?([^|*]+?)(?:\*\*)?\s*\|",
    re.MULTILINE,
)
_PRIORITY_SECTION_END = re.compile(
    r"\n#{2,4}\s*(?:7\.3|八、|📰)",
)


def _norm_theme_key(name: str) -> str:
    """题材名规范化，用于与报告表格做模糊匹配。"""
    s = (name or "").strip()
    s = re.sub(r"[\s/*\-、，。·•()（）\[\]【】]", "", s)
    return s.lower()


def parse_priority_rank_table(report_text: str) -> List[Tuple[int, str]]:
    """从报告正文解析「7.2 板块优先级排序」表。

    Returns:
        [(rank, 板块名), ...]，按 rank 升序；无该章节则 []。
    """
    m = _PRIORITY_SECTION_PAT.search(report_text)
    if not m:
        return []

    section = report_text[m.start() : m.start() + 6000]
    end_m = _PRIORITY_SECTION_END.search(section[30:])
    if end_m:
        section = section[: 30 + end_m.start()]

    ranks: List[Tuple[int, str]] = []
    seen: set = set()
    for row in _PRIORITY_TABLE_ROW.finditer(section):
        rank = int(row.group(1))
        name = row.group(2).strip()
        if not name or name in ("板块", "#", "---") or rank in seen:
            continue
        ranks.append((rank, name))
        seen.add(rank)
    ranks.sort(key=lambda x: x[0])
    return ranks


def _match_priority_rank(
    theme_name: str, rank_table: List[Tuple[int, str]]
) -> Optional[int]:
    """将 AI 题材名与表格板块名匹配，返回 priority_rank。"""
    tn = _norm_theme_key(theme_name)
    if not tn:
        return None

    for rank, sector in rank_table:
        if tn == _norm_theme_key(sector):
            return rank

    best_rank: Optional[int] = None
    best_overlap = 0
    for rank, sector in rank_table:
        sn = _norm_theme_key(sector)
        if not sn:
            continue
        if tn in sn or sn in tn:
            overlap = min(len(tn), len(sn))
            if overlap > best_overlap:
                best_overlap = overlap
                best_rank = rank
    return best_rank


def apply_priority_ranks_from_report(
    themes: List[Dict], report_text: str
) -> List[Dict]:
    """用报告正文优先级表补全 priority_rank（仅当前报告批次）。

    priority_rank 按 report_id 快照入库，全库允许重复（各报告各有 1、2、3…）。
    以正文表格为准，覆盖 AI 漏填或填错的序号。
    """
    rank_table = parse_priority_rank_table(report_text)
    if not rank_table:
        return themes

    out: List[Dict] = []
    for theme in themes:
        t = dict(theme)
        name = (t.get("theme_name") or "").strip()
        matched = _match_priority_rank(name, rank_table)
        if matched is not None:
            t["priority_rank"] = matched
        out.append(t)
    return out


def parse_news_id_map(report_path: str) -> Dict[str, str]:
    """解析报告底部"📰 引用新闻详情"段，建立"新闻N → raw_news.id"映射。

    报告底部每条新闻形如:
        <a id="新闻107"></a>
        ### 新闻107
        **数据库ID**: `abc123def`
        **时间**: ...
        ...

    Returns:
        {'新闻107': 'abc123def', '新闻125': '...'}；找不到则返回空 dict。
    """
    if not os.path.exists(report_path):
        return {}

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    idx = text.find(_REPORT_REF_HEADER)
    if idx < 0:
        return {}
    ref_section = text[idx:]

    # 在每个 <a id="新闻N"></a> 之后找最近的 **数据库ID**: 行
    result: Dict[str, str] = {}
    pattern = re.compile(
        r'<a\s+id="(新闻\d+)"></a>'           # 锚点
        r'(?:.|\n){1,400}?'                    # 紧邻几百字符内
        r'\*\*数据库ID\*\*:\s*`?([^\s`]+)`?',  # 数据库 ID 行
        re.MULTILINE,
    )
    for m in pattern.finditer(ref_section):
        ref, db_id = m.group(1), m.group(2).strip()
        if ref and db_id:
            result[ref] = db_id
    return result


def parse_report_meta(report_path: str) -> Dict:
    """从报告路径推导 meta 信息。

    示例输入: data/AI_analysis/5月22日/5月22日_18时25分_盘后总结分析报告.md
    输出: {
        'report_id': '5月22日_18时25分_盘后总结分析报告',
        'report_date': '2026-05-22',  # 优先用文件 mtime
        'report_time': '18:25',
        'report_path': 绝对路径
    }
    """
    abspath = os.path.abspath(report_path)
    basename = os.path.basename(report_path)
    report_id = os.path.splitext(basename)[0]

    report_time: Optional[str] = None
    m = re.search(r"(\d{1,2})时(\d{2})分", basename)
    if m:
        report_time = f"{int(m.group(1)):02d}:{m.group(2)}"

    if os.path.exists(abspath):
        mtime = datetime.fromtimestamp(os.path.getmtime(abspath))
        report_date = mtime.strftime("%Y-%m-%d")
        if not report_time:
            report_time = mtime.strftime("%H:%M")
    else:
        report_date = datetime.now().strftime("%Y-%m-%d")
        if not report_time:
            report_time = datetime.now().strftime("%H:%M")

    return {
        "report_id": report_id,
        "report_date": report_date,
        "report_time": report_time,
        "report_path": abspath,
    }
