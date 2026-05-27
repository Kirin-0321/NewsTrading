"""prompt_id -> 友好显示名 工具（review 补丁 P5）。

主人看到 "speculator_scalper" 不如看到 "A股短线投机·实战派" 直观。
所有 prompts/*.md 的 frontmatter 都有 `name` 字段，通过 PromptLoader 翻译。

用法：
    from gui.utils.prompt_name_helper import friendly_prompt_name
    label = friendly_prompt_name("speculator_scalper")
    # -> "A股短线投机·实战派"

设计要点：
    - 缓存 PromptLoader 实例（loader 内部已经做了 mtime 缓存）
    - 找不到 prompt_id 时返回原值（向后兼容）
    - None / 空串返回 fallback（默认"未知模板"）
"""

from __future__ import annotations

from typing import Optional

from core.prompt_loader import PromptError, get_loader


def friendly_prompt_name(
    prompt_id: Optional[str],
    category: str = "analysis",
    fallback: str = "未知模板",
) -> str:
    """prompt_id -> 友好显示名。

    Args:
        prompt_id: 如 "speculator_scalper" / "short_term"
        category: 默认 "analysis"，需要时可指定 "theme_extraction" 等
        fallback: prompt_id 为空时的兜底文本

    Returns:
        友好名（取自 prompts/*.md frontmatter 的 name 字段）；
        若 prompt 文件不存在则回退到原 prompt_id（保留可追溯性）；
        若 prompt_id 本身为空则返回 fallback。
    """
    if not prompt_id:
        return fallback
    try:
        tpl = get_loader().get(category, prompt_id)
        return tpl.name or prompt_id
    except PromptError:
        return prompt_id
    except Exception:
        return prompt_id
