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
    - 默认 deepseek-v4-pro + temperature=0.2，分类任务降低随机性
    - stream=True + response_format=json_object：chunk 实时推 UI，避免 60-90s 假死
    - 4 级 JSON 容错：直接 loads → 正则提 {} → 截断修复 → 字符级救援
    - Phase M0 起 prompt 从 prompts/theme_extraction/extract_themes.md 加载，
      不再硬编码常量

自动重试（2026-05-28 hotfix）:
    - httpx ``read`` timeout 改为 ``stream_idle_timeout``（默认 60s）作为
      "相邻 chunk 间最大等待秒数"——主人的"卡住"语义就是流式静默超阈值
    - ``extract_from_text`` 包重试循环，最多 ``max_attempts`` 次（默认 3）
    - 重试场景：流式静默超时 / SSE 断流 / 5xx / 连接异常 / JSON 完全解析失败
    - 不重试场景：鉴权/400 客户端错误、已救出部分题材的"部分成功"
    - 指数退避：2s → 4s → 8s（封顶 8s），失败信息通过 progress_callback 通知 UI
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from core.ai_config import AIConfig, DEFAULT_MAX_OUTPUT_TOKENS
from core.prompt_loader import PromptError, get_loader

logger = logging.getLogger(__name__)


_REPORT_REF_HEADER = "## 📰 引用新闻详情"

_VALID_LEVELS = {
    "重大利空", "较强利空", "弱利空", "中性偏空",
    "中性",
    "中性偏多", "弱利多", "较强利多", "重大利多",
}
_VALID_DURATION = {"短期", "中期", "长期"}
_VALID_GAP = {"高", "中高", "中", "中低", "低"}
_VALID_CATEGORY = {"科技AI", "新能源", "基建", "消费", "医药", "金融", "军工", "周期", "其他"}
_VALID_ROLE = {"核心", "上游", "中游", "下游", "潜力", "边缘"}


def _score_to_level(score: int) -> str:
    """strength_score -> strength_level（9 档映射，与 prompt v2 schema 对齐）。

    score >= +80                  -> 重大利多
    +60 <= score <= +79           -> 较强利多
    +40 <= score <= +59           -> 弱利多
    +1  <= score <= +39           -> 中性偏多
    score == 0                    -> 中性
    -39 <= score <= -1            -> 中性偏空
    -59 <= score <= -40           -> 弱利空
    -79 <= score <= -60           -> 较强利空
    score <= -80                  -> 重大利空
    """
    if score >= 80:
        return "重大利多"
    if score >= 60:
        return "较强利多"
    if score >= 40:
        return "弱利多"
    if score >= 1:
        return "中性偏多"
    if score == 0:
        return "中性"
    if score >= -39:
        return "中性偏空"
    if score >= -59:
        return "弱利空"
    if score >= -79:
        return "较强利空"
    return "重大利空"
_VALID_RELATION = {"主因", "共振", "风险", "背景"}

_PROMPT_CATEGORY = "theme_extraction"
_PROMPT_ID = "extract_themes"


def _load_extractor_prompts() -> Tuple[str, str]:
    """从 prompts/theme_extraction/extract_themes.md 加载题材抽取 prompt。

    返回 (system_prompt, user_prompt_template)。
    PromptLoader 异常会向上抛出，启动 validate_all 会提前发现这个问题。
    """
    loader = get_loader()
    tmpl = loader.get(_PROMPT_CATEGORY, _PROMPT_ID)
    return tmpl.system_prompt, tmpl.user_prompt_template


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
        self.model = model or ext_cfg.get("model") or "deepseek-v4-pro"
        self.temperature = ext_cfg.get("temperature", temperature)
        self.max_tokens = int(ext_cfg.get("max_tokens", max_tokens))
        # stream_idle_timeout：流式 chunk 间最大静默秒数（httpx read timeout）。
        # 默认 60s——正常 LLM chunk 间隔 < 5s，超过 60s 视为断流/假死，触发重试。
        # 旧字段 ``timeout`` 已废弃（向后兼容仍读，但仅作兜底）。
        try:
            self.stream_idle_timeout = float(
                ext_cfg.get("stream_idle_timeout", 60)
            )
        except (TypeError, ValueError):
            self.stream_idle_timeout = 60.0
        # 兼容旧配置：用户只配了 timeout 没配 stream_idle_timeout → 取 min(老值, 60)
        if "stream_idle_timeout" not in ext_cfg and "timeout" in ext_cfg:
            try:
                self.stream_idle_timeout = min(
                    float(ext_cfg["timeout"]), self.stream_idle_timeout
                )
            except (TypeError, ValueError):
                pass
        # max_attempts：含首次的最大尝试次数（默认 3 = 首次 + 2 次重试）
        try:
            self.max_attempts = max(1, int(ext_cfg.get("max_attempts", 3)))
        except (TypeError, ValueError):
            self.max_attempts = 3
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
        """对已读入的文本抽题材（带自动重试）。

        自动重试机制（2026-05-28 hotfix）：
            * 触发：httpx 流式 chunk 静默超 ``stream_idle_timeout`` 秒，httpx
              抛 ``ReadTimeout`` → 本方法捕获并重试
            * 涵盖范围：网络超时 / SSE 断流 / provider 5xx / 限流
              / 4 级 JSON 容错全部失败（视为完全无效响应）
            * 不重试：鉴权失败 / 400 客户端错误 / 已救出部分题材的"部分成功"
              （部分成功直接返回，避免重复入库浪费 token）
            * 退避：第 1/2/3 次重试间隔 2s/4s/8s（封顶 8s）
            * UI 反馈：每次重试前通过 ``progress_callback`` 推一行提示
        """
        last_err_msg: Optional[str] = None

        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1 and progress_callback:
                try:
                    progress_callback(
                        f"⏳ 题材抽取第 {attempt}/{self.max_attempts} 次尝试"
                        f"（上次失败：{last_err_msg}）"
                    )
                except Exception:
                    pass  # UI 回调异常不影响重试主流程

            # 每轮清状态，避免上次结果污染
            self._last_finish_reason = None
            self._last_raw_response = None

            try:
                raw_response = self._call_llm(report_text, progress_callback)
            except Exception as e:  # noqa: BLE001
                err_text = str(e)
                last_err_msg = f"{type(e).__name__}: {err_text}"

                if self._is_retryable_error(e) and attempt < self.max_attempts:
                    wait = min(2 ** attempt, 8)
                    if progress_callback:
                        try:
                            progress_callback(
                                f"⚠️ 题材抽取 LLM 调用失败（{last_err_msg}），"
                                f"{wait}s 后重试..."
                            )
                        except Exception:
                            pass
                    time.sleep(wait)
                    continue

                # 不可重试 / 重试用尽
                err_lower = err_text.lower()
                if "timed out" in err_lower or "timeout" in err_lower:
                    return [], (
                        f"调用 LLM 失败: {err_text}。"
                        f"（chunk 静默超 {int(self.stream_idle_timeout)}s；"
                        f"已尝试 {attempt} 次；可在 config/ai_config.json 调高 "
                        f"theme_extraction.stream_idle_timeout 或 max_attempts，"
                        f"或降低 theme_extraction.max_tokens）"
                    )
                return [], f"调用 LLM 失败（已尝试 {attempt} 次）: {err_text}"

            # 拿到响应，开始解析
            self._last_raw_response = raw_response
            themes, parse_err = self._parse_response(raw_response)

            # 截断时即使解析失败也尽量救出前面的题材
            if self._last_finish_reason == "length":
                hint = (
                    f"⚠️ AI 输出被截断（finish_reason=length，max_tokens={self.max_tokens}）。"
                    "已尝试修复 JSON 取出前面的题材。建议主人调大 config/ai_config.json 的 "
                    "theme_extraction.max_tokens。"
                )
                parse_err = f"{hint} | 原解析消息: {parse_err}" if parse_err else hint

            # 4 级容错都救不出题材 → 视为故障，可重试
            if not themes:
                last_err_msg = parse_err or "LLM 返回为空"
                if attempt < self.max_attempts:
                    wait = min(2 ** attempt, 8)
                    if progress_callback:
                        try:
                            progress_callback(
                                f"⚠️ 题材解析失败（{last_err_msg}），{wait}s 后重试..."
                            )
                        except Exception:
                            pass
                    time.sleep(wait)
                    continue

                # 重试用尽 → 落盘失败上下文供事后排查
                self._dump_failure(report_text, raw_response, parse_err or "")
                return [], parse_err or "重试用尽：未抽出任何题材"

            # 成功（部分或全部）→ matcher 富化后返回
            # 失败不抛异常，保留原数据
            try:
                from services.scoring.matcher import enrich_themes_with_matcher
                enrich_themes_with_matcher(themes)
            except Exception as e:
                logger.warning("matcher 富化失败，沿用原数据: %s", e)

            return themes, parse_err  # 部分成功时也带 warning 出去

        # 兜底（理论走不到 —— for 循环里所有分支都 return 或 continue）
        return [], f"重试 {self.max_attempts} 次均未成功: {last_err_msg}"

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """判定异常是否可重试。

        可重试（典型场景）：
            * httpx 超时/连接错误：ReadTimeout / ConnectTimeout / ConnectError
            * SSE 断流：RemoteProtocolError / ReadError / ChunkedEncodingError
            * provider 5xx：InternalServerError / BadGateway / ServiceUnavailable
            * 限流：RateLimitError（OpenAI/DeepSeek 偶发 429）
            * 流式静默后底层抛的 TimeoutError（含原生 socket / asyncio 形态）

        不可重试：
            * 配置缺失：ValueError（未配置 API Key）
            * 鉴权失败：AuthenticationError / PermissionDeniedError（401/403）
            * 客户端错误：BadRequestError / NotFoundError（400/404，重试也是错）
            * 模型不存在 / 参数非法 等

        判定不严格依赖 import openai/httpx 具体异常类——用类名 + 错误消息
        关键词覆盖三家 SDK（openai/zhipuai/httpx 内核），更稳健。
        """
        if isinstance(exc, ValueError):
            return False

        name_l = type(exc).__name__.lower()
        msg_l = str(exc).lower()

        # 明确不重试：鉴权 / 客户端错误
        non_retryable_names = (
            "authentication", "permissiondenied", "badrequest",
            "notfound", "unprocessable", "conflicterror",
        )
        if any(k in name_l for k in non_retryable_names):
            return False
        if any(k in msg_l for k in (
            "unauthorized", "forbidden", " 401", " 403", " 404", " 400",
        )):
            return False

        # 明确可重试：超时 / 连接 / SSE 断流 / 5xx / 限流
        retryable_names = (
            "timeout", "timeouterror",
            "connecterror", "connectionerror", "apiconnectionerror",
            "remoteprotocolerror", "readerror", "writeerror",
            "chunkedencodingerror",
            "internalserver", "serviceunavailable",
            "badgateway", "gatewaytimeout",
            "ratelimit", "apistatuserror",
        )
        if any(k in name_l for k in retryable_names):
            return True

        retryable_keywords = (
            "timed out", "timeout", "connection reset", "connection aborted",
            "remote end closed", "incomplete chunked", "broken pipe",
            "rate limit", "internal server error", "bad gateway",
            "service unavailable", "gateway timeout",
            " 500", " 502", " 503", " 504", " 429",
        )
        if any(k in msg_l for k in retryable_keywords):
            return True

        return False

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
    def _http_timeout(idle_seconds: float):
        """构建 httpx 超时。

        Args:
            idle_seconds: 流式 chunk 间最大静默秒数（即 ``stream_idle_timeout``）。
                httpx 的 ``read`` 在 SSE 场景下就是"相邻 chunk 间最大等待"，
                这个值砍小（默认 60s）= chunk 超过 60s 不到 → 抛 ReadTimeout
                → ``extract_from_text`` 捕获后自动重试。

        旧实现用 ``theme_extraction.timeout``（默认 1200s）的语义错位为
        "整体读超时"，导致断流时要傻等 20 分钟才能感知，主人因此遇到"卡住"。
        本次 hotfix（2026-05-28）把这个值砍到 60s 并配合外层重试循环。
        """
        from httpx import Timeout

        # 至少 15s——给 LLM 启动响应留余量，不要太激进
        sec = max(float(idle_seconds), 15.0)
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

        http_timeout = self._http_timeout(self.stream_idle_timeout)
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

        try:
            system_prompt, user_template = _load_extractor_prompts()
        except PromptError as e:
            raise RuntimeError(
                f"题材抽取 prompt 加载失败 "
                f"(prompts/{_PROMPT_CATEGORY}/{_PROMPT_ID}.md): {e}"
            ) from e
        user_prompt = user_template.format(report_text=report_text)

        # 注意：故意 *不* 加 response_format=json_object。
        # DeepSeek / OpenAI 在 JSON mode 下，服务端会先把完整 JSON 生成完
        # 才开始 SSE 推送，导致流式表现成"等几十秒 → 一次性爆发"。
        # 改为纯文本流式 + prompt 强约束 JSON + 4 级容错解析(已实现)，
        # 在主流场景下 chunks 真正逐字到达，UI 反馈实时。
        create_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
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

        score = pick_int(item.get("strength_score"), -100, 100)
        if score is None:
            score = 0
        level_raw = pick_enum(item.get("strength_level"), _VALID_LEVELS)
        level = _score_to_level(score) if not level_raw else level_raw

        return {
            "theme_name": name,
            "theme_category": pick_enum(item.get("theme_category"), _VALID_CATEGORY),
            "strength_score": score,
            "strength_level": level,
            "priority_rank": pick_int(item.get("priority_rank")),
            "duration": pick_enum(item.get("duration"), _VALID_DURATION),
            "expectation_gap": pick_enum(item.get("expectation_gap"), _VALID_GAP),
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

    示例输入: data/AI_analysis/5月22日/5月22日_0时00分_盘后总结分析报告_backtest.md
    输出: {
        'report_id': '5月22日_0时00分_盘后总结分析报告_backtest',
        'report_date': '20260522',   # YYYYMMDD（schema 协议）
        'report_time': '00:00',
        'report_path': 'data/AI_analysis/5月22日/...'  # 相对 posix
    }

    解析顺序（2026-05-27 重构）：
        1. 优先从文件名前缀 ``{M}月{D}日_{H}时{MM}分_`` 拿月/日/时/分；
           年份从文件 mtime（失败兜底 datetime.now()）。
        2. 文件名前缀不匹配时，完全回退到 mtime（再失败用当前时间）。

    历史背景：
        - 真实生成产物：``5月27日_18时12分_盘后总结分析报告.md``
        - 回测产物（重构后）：``5月22日_0时00分_盘后总结分析报告_backtest.md``
        两者都能被同一个正则匹配，回测产物从文件名前缀解析出来的日期天然是
        模拟交易日，再无需事后 UPDATE 覆盖。

    P1 修复（2026-05-27 review）：
        统一改用 _to_relative_posix() 规范化为相对 posix 路径。
    """
    abspath = os.path.abspath(report_path)
    basename = os.path.basename(report_path)
    report_id = os.path.splitext(basename)[0]

    # 用 mtime 拿年份（兜底当前年）；失败再退 datetime.now()
    if os.path.exists(abspath):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(abspath))
        except OSError:
            mtime = datetime.now()
    else:
        mtime = datetime.now()

    report_date: str
    report_time: Optional[str]

    m = re.match(r"^(\d{1,2})月(\d{1,2})日_(\d{1,2})时(\d{2})分_", basename)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        hour = int(m.group(3))
        minute = m.group(4)
        report_date = f"{mtime.year:04d}{month:02d}{day:02d}"
        report_time = f"{hour:02d}:{minute}"
    else:
        report_date = mtime.strftime("%Y%m%d")
        report_time = mtime.strftime("%H:%M")

    try:
        from services.storage.ai_reports_store import _to_relative_posix
        rel_path = _to_relative_posix(report_path)
    except Exception:
        rel_path = report_path.replace("\\", "/")

    return {
        "report_id": report_id,
        "report_date": report_date,
        "report_time": report_time,
        "report_path": rel_path,
    }
