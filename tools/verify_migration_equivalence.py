"""验证迁移后 PromptLoader 读到的 system_prompt / user_prompt_template
与 ai_config.legacy.json 原文严格一致（按字符比较）。

跑法::

    py tools/verify_migration_equivalence.py

输出:
    - 每个 prompt_id 的对比结果（PASS / DIFF）
    - 若有 DIFF，打印前 200 字符的差异片段
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.prompt_loader import PromptLoader  # noqa: E402

LEGACY = ROOT / "config" / "ai_config.legacy.json"


def _show_first_diff(a: str, b: str, label: str) -> str:
    """返回首个不同位置上下文。"""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            ctx_start = max(0, i - 30)
            ctx_end = min(n, i + 30)
            return (
                f"{label} 首个差异位置={i}  长度 legacy={len(a)} new={len(b)}\n"
                f"  legacy[{ctx_start}:{ctx_end}] = {a[ctx_start:ctx_end]!r}\n"
                f"  new   [{ctx_start}:{ctx_end}] = {b[ctx_start:ctx_end]!r}"
            )
    if len(a) != len(b):
        tail = a[n:] if len(a) > len(b) else b[n:]
        return (
            f"{label} 前 {n} 字符一致但长度不同  "
            f"legacy={len(a)} new={len(b)}  尾部={tail[:60]!r}"
        )
    return f"{label} 完全一致"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not LEGACY.is_file():
        print(f"[ERROR] 找不到 {LEGACY}", file=sys.stderr)
        return 2

    with LEGACY.open("r", encoding="utf-8") as f:
        legacy = json.load(f)
    legacy_templates = legacy.get("prompt_templates") or {}
    if not legacy_templates:
        print("[ERROR] legacy 文件没有 prompt_templates", file=sys.stderr)
        return 2

    loader = PromptLoader(root=ROOT / "prompts")
    fails: list = []

    for prompt_id, template in legacy_templates.items():
        try:
            new_tmpl = loader.get("analysis", prompt_id)
        except Exception as e:  # noqa: BLE001
            fails.append((prompt_id, f"加载失败: {e}"))
            print(f"  [FAIL] {prompt_id}: 加载失败 {e}")
            continue

        legacy_sys = (template.get("system_prompt") or "").rstrip("\n")
        legacy_usr = (template.get("user_prompt_template") or "").rstrip("\n")
        new_sys = (new_tmpl.system_prompt or "").rstrip("\n")
        new_usr = (new_tmpl.user_prompt_template or "").rstrip("\n")
        legacy_name = template.get("name") or ""
        new_name = new_tmpl.name

        problems = []
        if legacy_name != new_name:
            problems.append(
                f"name 不一致: legacy={legacy_name!r} new={new_name!r}"
            )
        if legacy_sys != new_sys:
            problems.append(_show_first_diff(legacy_sys, new_sys, "system_prompt"))
        if legacy_usr != new_usr:
            problems.append(
                _show_first_diff(legacy_usr, new_usr, "user_prompt_template")
            )

        if problems:
            fails.append((prompt_id, "\n".join(problems)))
            print(f"  [DIFF] {prompt_id}")
            for p in problems:
                for line in p.splitlines():
                    print(f"          {line}")
        else:
            print(f"  [PASS] {prompt_id}")

    print()
    if fails:
        print(f"=== 失败 {len(fails)} 个 ===")
        return 1
    print(f"=== 全部通过 ({len(legacy_templates)} 个 prompt) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
