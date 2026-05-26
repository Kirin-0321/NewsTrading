"""AI 兜底：仅补 CLS / 别名 表都没命中的字段。

设计原则::

    输入   : tushare-only summary + cls_result + raw_news_today
    输出   : 局部 patch（JSON）——仅包含未命中板块的 catalysts 和未命中营业部的别名
    边界   : ① 不允许覆盖任何 tushare 数值字段
            ② Prompt 中明示「找不到证据 → 直接省略键名」
            ③ 失败 / 超时 → 返回空 patch + warning，不抛
    依赖   : core.ai_config / core.prompt_loader / services.storage.raw_store

落库::

    enrich_*() 返回 ``AIEnrichPart``；
    service.py 把多个 part 合并写入 ``ai_enrich_patches`` 表。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.ai_config import AIConfig
from core.prompt_loader import PromptError, PromptLoader, get_loader
from services.storage import get_raw_store

_log = logging.getLogger(__name__)

# 默认 prompt id（与 ``prompts/market_fetch/*.md`` 文件名一致）
DEFAULT_SECTORS_PROMPT_ID = "ai_enrich_sectors"
DEFAULT_TRADERS_PROMPT_ID = "ai_enrich_traders"

# 默认模型：DeepSeek V4 Pro（不开思考链，纯 JSON 输出）
# pro 的 JSON 严格性 + 工具调用稳定性显著好于 flash，且 token 成本可控
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT_SEC = 90
DEFAULT_MAX_OUTPUT_TOKENS = 2000
DEFAULT_RAW_NEWS_TOP_N = 40


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AIEnrichPart:
    """单次 AI 调用的产物（sectors / traders 各一份）。"""

    ok: bool = False
    patch: Dict[str, Any] = field(default_factory=dict)
    prompt_id: str = ""
    prompt_version: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    raw_response_snippet: Optional[str] = None


@dataclass
class AIEnrichPatch:
    """组合产物——sectors + traders。"""

    trade_date: str = ""
    sectors: AIEnrichPart = field(default_factory=AIEnrichPart)
    traders: AIEnrichPart = field(default_factory=AIEnrichPart)

    def is_empty(self) -> bool:
        return not (self.sectors.patch or self.traders.patch)

    def merged_patch_json(self) -> Dict[str, Any]:
        """把两个 part 的 patch 合并为单个 JSON dict（落库用）。"""
        out: Dict[str, Any] = {}
        if self.sectors.patch:
            out["sectors_catalysts"] = self.sectors.patch.get(
                "sectors_catalysts", {}
            )
        if self.traders.patch:
            out["traders_aliases"] = self.traders.patch.get(
                "traders_aliases", {}
            )
        return out


# ---------------------------------------------------------------------------
# AIEnricher 主类
# ---------------------------------------------------------------------------


class AIEnricher:
    """兜底补全 catalysts / trader 别名。"""

    def __init__(
        self,
        ai_config: Optional[AIConfig] = None,
        prompt_loader: Optional[PromptLoader] = None,
        raw_store: Any = None,
    ) -> None:
        self.ai_config = ai_config or AIConfig()
        self.prompt_loader = prompt_loader or get_loader()
        self.raw_store = raw_store or get_raw_store()

    # ------------------------------------------------------------------
    # public — sectors
    # ------------------------------------------------------------------

    def enrich_sectors(
        self,
        trade_date: str,
        unmatched_sectors: Sequence[str],
        known_cls_plates: Sequence[str],
        market_kpi_text: str,
        raw_news_text: Optional[str] = None,
        prompt_id: str = DEFAULT_SECTORS_PROMPT_ID,
    ) -> AIEnrichPart:
        """对 unmatched_sectors 调用 AI 补 catalysts。"""
        unmatched_list = [
            str(s).strip() for s in unmatched_sectors if str(s).strip()
        ]
        if not unmatched_list:
            return AIEnrichPart(
                ok=True,
                prompt_id=prompt_id,
                prompt_version="-",
                warnings=["no unmatched sectors, skip"],
            )

        if raw_news_text is None:
            raw_news_text = self._load_recent_news_text(
                trade_date, top_n=DEFAULT_RAW_NEWS_TOP_N
            )

        context = {
            "trade_date": trade_date,
            "market_kpi": market_kpi_text or "(略)",
            "unmatched_sectors": _format_list_compact(unmatched_list),
            "known_cls_plates": _format_list_compact(
                list(known_cls_plates)[:80]
            ),
            "raw_news_today": (
                raw_news_text.strip() if raw_news_text else "(无)"
            ),
        }

        return self._invoke_prompt(
            category="market_fetch",
            prompt_id=prompt_id,
            context=context,
            expected_keys=("sectors_catalysts",),
            post_filter=lambda patch: _filter_sectors_patch(
                patch, allowed=set(unmatched_list)
            ),
        )

    # ------------------------------------------------------------------
    # public — traders
    # ------------------------------------------------------------------

    def enrich_traders(
        self,
        unmatched_exalters: Sequence[str],
        known_aliases: Sequence[Dict[str, Any]],
        trade_date: str = "",
        prompt_id: str = DEFAULT_TRADERS_PROMPT_ID,
    ) -> AIEnrichPart:
        unmatched_list = [
            str(s).strip() for s in unmatched_exalters if str(s).strip()
        ]
        if not unmatched_list:
            return AIEnrichPart(
                ok=True,
                prompt_id=prompt_id,
                prompt_version="-",
                warnings=["no unmatched exalters, skip"],
            )

        known_text = _format_known_aliases(known_aliases)
        context = {
            "context_date": trade_date or "-",
            "unmatched_exalters": _format_list_compact(unmatched_list),
            "known_aliases": known_text or "(无)",
        }

        return self._invoke_prompt(
            category="market_fetch",
            prompt_id=prompt_id,
            context=context,
            expected_keys=("traders_aliases",),
            post_filter=lambda patch: _filter_traders_patch(
                patch, allowed=set(unmatched_list)
            ),
        )

    # ------------------------------------------------------------------
    # combined
    # ------------------------------------------------------------------

    def enrich(
        self,
        trade_date: str,
        unmatched_sectors: Sequence[str],
        known_cls_plates: Sequence[str],
        market_kpi_text: str,
        unmatched_exalters: Sequence[str],
        known_aliases: Sequence[Dict[str, Any]],
        raw_news_text: Optional[str] = None,
    ) -> AIEnrichPatch:
        patch = AIEnrichPatch(trade_date=trade_date)
        patch.sectors = self.enrich_sectors(
            trade_date=trade_date,
            unmatched_sectors=unmatched_sectors,
            known_cls_plates=known_cls_plates,
            market_kpi_text=market_kpi_text,
            raw_news_text=raw_news_text,
        )
        patch.traders = self.enrich_traders(
            unmatched_exalters=unmatched_exalters,
            known_aliases=known_aliases,
            trade_date=trade_date,
        )
        return patch

    # ------------------------------------------------------------------
    # internal — prompt 调用
    # ------------------------------------------------------------------

    def _invoke_prompt(
        self,
        category: str,
        prompt_id: str,
        context: Dict[str, Any],
        expected_keys: Sequence[str],
        post_filter,
    ) -> AIEnrichPart:
        part = AIEnrichPart(prompt_id=prompt_id)

        try:
            tmpl = self.prompt_loader.get(category, prompt_id)
        except PromptError as e:
            part.error = f"prompt load failed: {e}"
            _log.warning(part.error)
            return part
        part.prompt_version = tmpl.version

        try:
            system_text = self._render_template(tmpl.system_prompt, context)
            user_text = self._render_template(
                tmpl.user_prompt_template, context
            )
        except KeyError as e:
            part.error = f"prompt missing placeholder: {e}"
            _log.warning(part.error)
            return part

        # provider 选择优先级：market_fetch 配置 > prompt 默认 > 全局默认
        provider = (
            self._cfg_market_fetch_value("provider")
            or tmpl.provider_default
            or DEFAULT_PROVIDER
        )
        # model 选择优先级：market_fetch 配置 > 当前 provider 配置的 model
        # > prompt 默认 > 全局默认
        provider_model: Optional[str] = None
        try:
            provider_model = self.ai_config.get_provider_config(
                provider
            ).get("model") or None
        except Exception:
            provider_model = None
        model = (
            self._cfg_market_fetch_value("model")
            or provider_model
            or tmpl.model_default
            or DEFAULT_MODEL
        )
        temperature = (
            tmpl.temperature_default
            if tmpl.temperature_default is not None
            else DEFAULT_TEMPERATURE
        )

        part.provider = provider
        part.model = model

        t0 = time.time()
        try:
            raw_text, in_tokens, out_tokens = _call_chat_json(
                ai_config=self.ai_config,
                provider=provider,
                model=model,
                system_text=system_text,
                user_text=user_text,
                temperature=temperature,
                timeout_sec=DEFAULT_TIMEOUT_SEC,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        except Exception as e:
            part.elapsed_ms = int((time.time() - t0) * 1000)
            part.error = f"AI call failed: {type(e).__name__}: {e}"
            _log.warning(part.error)
            return part
        part.elapsed_ms = int((time.time() - t0) * 1000)
        part.input_tokens = in_tokens
        part.output_tokens = out_tokens
        part.raw_response_snippet = (raw_text or "")[:500]

        parsed = _extract_json(raw_text)
        if parsed is None or not isinstance(parsed, dict):
            part.error = "AI response is not valid JSON"
            _log.warning(
                "%s | snippet=%r",
                part.error,
                part.raw_response_snippet,
            )
            return part

        if not any(k in parsed for k in expected_keys):
            part.warnings.append(
                f"missing expected keys {expected_keys}, "
                f"keys got={list(parsed.keys())}"
            )

        try:
            filtered = post_filter(parsed)
        except Exception as e:
            part.error = f"post_filter failed: {e}"
            _log.warning(part.error)
            return part

        part.patch = filtered
        part.ok = bool(filtered)
        return part

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_template(text: str, context: Dict[str, Any]) -> str:
        """安全替换 ``{key}`` 占位符。

        不用 ``str.format_map`` —— prompt 内的 JSON 示例会带大量 ``{}``
        被误判为占位符。改用「逐 key 字符串替换」的策略：
        仅替换 context 里出现的 ``{key}``，其它 ``{...}`` 文本原样保留。
        """
        if not text:
            return ""
        out = text
        for k, v in context.items():
            out = out.replace("{" + str(k) + "}", str(v))
        return out

    def _cfg_market_fetch_value(self, key: str) -> Optional[str]:
        getter = getattr(self.ai_config, "get_market_fetch_config", None)
        if not callable(getter):
            return None
        try:
            cfg = getter() or {}
        except Exception:
            return None
        v = cfg.get(key)
        return str(v) if v else None

    # ------------------------------------------------------------------
    # 当日新闻拼接
    # ------------------------------------------------------------------

    def _load_recent_news_text(self, trade_date: str, top_n: int) -> str:
        """读取 trade_date 当日的 curated 新闻，拼成压缩文本块。

        不依赖 datetime 实现细节；只取 raw_store 已有的接口。
        失败时返回空串，由 prompt 自行用 "(无)" 兜底。
        """
        try:
            from datetime import datetime

            dt = datetime.strptime(trade_date, "%Y%m%d")
            start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end = dt.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
            rows = self.raw_store.get_news_in_range(start, end)
        except Exception as e:
            _log.warning("load news for %s failed: %s", trade_date, e)
            return ""

        if not rows:
            return ""

        # 仅取标题 + 时间，前 top_n 条
        lines: List[str] = []
        for i, n in enumerate(rows[:top_n], start=1):
            ts = (
                n.get("datetime") or n.get("time") or n.get("publish_time")
                or ""
            )
            title = (n.get("title") or "").strip()
            if not title:
                continue
            ts_short = str(ts)[5:16].replace("T", " ") if ts else "--"
            lines.append(f"[{i:02d}] {ts_short} {title}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI 兼容 chat — 同步、非流式、JSON-mode
# ---------------------------------------------------------------------------


def _call_chat_json(
    ai_config: AIConfig,
    provider: str,
    model: str,
    system_text: str,
    user_text: str,
    temperature: float,
    timeout_sec: int,
    max_output_tokens: int,
):
    """返回 (text, input_tokens, output_tokens)。失败抛异常。"""
    provider_cfg = ai_config.get_provider_config(provider) or {}
    api_key = provider_cfg.get("api_key")
    if not api_key:
        raise RuntimeError(f"provider {provider!r} 未配置 api_key")
    base_url = provider_cfg.get("base_url")
    if provider == "qwen" and not base_url:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    if provider == "zhipu":
        try:
            from zhipuai import ZhipuAI
        except ImportError as e:
            raise ImportError("请安装 zhipuai") from e
        client = ZhipuAI(api_key=api_key, timeout=timeout_sec)
    else:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("请安装 openai") from e
        client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_sec
        )

    create_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "stream": False,
    }

    # DeepSeek V4 系列默认开启 thinking，AIEnricher 不需要
    if provider == "deepseek" and model.startswith("deepseek-v4"):
        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    # response_format = json_object，DeepSeek / OpenAI 都支持；不支持时降级即可
    try:
        resp = client.chat.completions.create(
            response_format={"type": "json_object"},
            **create_kwargs,
        )
    except TypeError:
        # 部分 SDK 旧版本不识别 response_format
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as e:
        msg = str(e).lower()
        if "response_format" in msg or "json_object" in msg:
            resp = client.chat.completions.create(**create_kwargs)
        else:
            raise

    text = ""
    if resp and resp.choices:
        msg = resp.choices[0].message
        text = (getattr(msg, "content", None) or "").strip()
    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    return text, in_tok, out_tok


# ---------------------------------------------------------------------------
# helpers — formatting & parsing
# ---------------------------------------------------------------------------


def _format_list_compact(items: Sequence[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def _format_known_aliases(items: Sequence[Dict[str, Any]]) -> str:
    """已知映射示例（限制 ≤ 20 条，作为 AI 风格参考）。"""
    if not items:
        return ""
    out: List[str] = []
    for it in list(items)[:20]:
        exalter = str(it.get("exalter", "")).strip()
        alias = str(it.get("alias", "")).strip()
        if not exalter or not alias:
            continue
        fame = (
            "famous" if bool(it.get("is_famous")) else "regular"
        )
        out.append(f"- {exalter} → {alias} [{fame}]")
    return "\n".join(out)


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> Optional[Any]:
    """从模型响应里提取 JSON 对象（容忍 ```json 包裹 / 前后白噪声）。"""
    if not text:
        return None
    s = text.strip()

    # 1) 直接 parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2) ```json ... ``` fenced block
    m = _FENCE_RE.search(s)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

    # 3) 截取第一个 { ... } 配对块
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = s[start: i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def _filter_sectors_patch(
    patch: Dict[str, Any],
    allowed: set,
) -> Dict[str, Any]:
    """只保留 allowed 板块名的 catalysts，每项最多 3 条 + 长度截断。"""
    if not isinstance(patch, dict):
        return {}
    raw = patch.get("sectors_catalysts") or {}
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, List[str]] = {}
    for name, cats in raw.items():
        name_clean = str(name).strip()
        if name_clean not in allowed:
            continue
        if not isinstance(cats, (list, tuple)):
            continue
        items: List[str] = []
        seen: set = set()
        for c in cats:
            if not isinstance(c, str):
                continue
            txt = c.strip().replace("\n", " ")
            if not txt or txt in seen:
                continue
            if len(txt) > 80:
                txt = txt[:79] + "…"
            seen.add(txt)
            items.append(txt)
            if len(items) >= 3:
                break
        if items:
            cleaned[name_clean] = items
    return {"sectors_catalysts": cleaned} if cleaned else {}


def _filter_traders_patch(
    patch: Dict[str, Any],
    allowed: set,
) -> Dict[str, Any]:
    if not isinstance(patch, dict):
        return {}
    raw = patch.get("traders_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Dict[str, Any]] = {}
    for name, val in raw.items():
        name_clean = str(name).strip()
        if name_clean not in allowed:
            continue
        if not isinstance(val, dict):
            continue
        alias = str(val.get("alias", "")).strip()
        if not alias:
            continue
        if len(alias) > 16:
            alias = alias[:16]
        is_famous = bool(val.get("is_famous", False))
        notes_raw = val.get("notes")
        notes = (
            str(notes_raw).strip() if notes_raw else None
        )
        if notes and len(notes) > 60:
            notes = notes[:59] + "…"
        cleaned[name_clean] = {
            "alias": alias,
            "is_famous": is_famous,
            "notes": notes,
        }
    return {"traders_aliases": cleaned} if cleaned else {}


__all__ = [
    "AIEnricher",
    "AIEnrichPart",
    "AIEnrichPatch",
    "DEFAULT_SECTORS_PROMPT_ID",
    "DEFAULT_TRADERS_PROMPT_ID",
]
