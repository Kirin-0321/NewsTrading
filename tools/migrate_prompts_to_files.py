"""把 config/ai_config.json 中的 prompt_templates 一次性迁移到 prompts/*.md。

Phase M0 一次性任务。设计原则:
    - 默认 dry-run：只打印将要写入的文件清单，不动磁盘
    - --apply 才真正写盘
    - 备份原 ai_config.json 到 ai_config.legacy.json
    - 文件名直接沿用原 ID（custom_6.md / standard.md 等），
      保证 current_prompt_template 字段无须迁移
    - 写完后跑 PromptLoader.validate_all() 自检

用法::

    py tools/migrate_prompts_to_files.py            # dry-run，看清单
    py tools/migrate_prompts_to_files.py --apply    # 真正执行迁移

迁移产物::

    prompts/
        analysis/
            comprehensive.md
            standard.md
            aggressive.md
            conservative.md
            value.md
            short_term.md
            custom_6.md
            ...
    config/
        ai_config.json           # 已瘦身（不再含 prompt_templates）
        ai_config.legacy.json    # 完整备份
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.prompt_loader import PromptLoader  # noqa: E402


CONFIG_PATH = ROOT / "config" / "ai_config.json"
LEGACY_PATH = ROOT / "config" / "ai_config.legacy.json"
PROMPTS_ROOT = ROOT / "prompts"
ANALYSIS_DIR = PROMPTS_ROOT / "analysis"


def _quote_yaml(value: str) -> str:
    """把任意字符串安全地转成 YAML 标量。

    优先用单引号包裹（YAML 单引号字符串里只需把 `'` 翻倍即可），
    包含换行/控制字符时降级为带转义的双引号字符串。
    """
    if value is None:
        return "''"
    s = str(value)
    if "\n" in s or "\r" in s or "\t" in s:
        # 双引号字符串需要转义反斜杠和双引号
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return "'" + s.replace("'", "''") + "'"


def _render_prompt_md(
    prompt_id: str,
    template: Dict,
    *,
    category: str = "analysis",
    extra_notes: Optional[str] = None,
) -> str:
    """渲染 prompts/analysis/{id}.md 完整文件内容。"""
    name = template.get("name") or prompt_id
    system_prompt = template.get("system_prompt", "") or ""
    user_prompt_template = template.get("user_prompt_template", "") or ""

    fm_lines = [
        "---",
        f"id: {prompt_id}",
        f"name: {_quote_yaml(name)}",
        f"category: {category}",
        "version: '1.0'",
    ]
    if extra_notes:
        fm_lines.append(f"description: {_quote_yaml(extra_notes)}")
    fm_lines.append(
        f"migrated_from: {_quote_yaml('config/ai_config.json')}"
    )
    fm_lines.append(
        f"migrated_at: {_quote_yaml(datetime.now().strftime('%Y-%m-%d'))}"
    )
    fm_lines.append("---")

    parts = ["\n".join(fm_lines), ""]

    if system_prompt.strip():
        parts.append("## SYSTEM")
        parts.append("")
        parts.append(system_prompt.rstrip())
        parts.append("")

    parts.append("## USER")
    parts.append("")
    parts.append(user_prompt_template.rstrip())
    parts.append("")

    return "\n".join(parts)


def _load_ai_config() -> Dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _slim_config(original: Dict) -> Dict:
    """生成瘦身版 ai_config.json：移除 prompt_templates。

    保留 current_prompt_template / current_template 等字段以指向新文件。
    """
    slim = dict(original)
    slim.pop("prompt_templates", None)
    slim["_prompts_storage"] = (
        "file_based since 2026-05-26; see prompts/{category}/{id}.md"
    )
    return slim


def _plan_writes(config: Dict) -> Tuple[list, list]:
    """返回 (写入计划, 警告列表)。

    写入计划项: (路径 Path, 内容 str, prompt_id str)
    """
    templates = config.get("prompt_templates") or {}
    writes: list = []
    warnings: list = []

    if not templates:
        warnings.append("ai_config.json 中 prompt_templates 为空，无需迁移")
        return writes, warnings

    for prompt_id, template in templates.items():
        if not isinstance(template, dict):
            warnings.append(f"[{prompt_id}] 模板结构异常，跳过: {template!r}")
            continue
        target = ANALYSIS_DIR / f"{prompt_id}.md"
        content = _render_prompt_md(prompt_id, template)
        writes.append((target, content, prompt_id))

    return writes, warnings


def _print_plan(writes: list, warnings: list, apply: bool) -> None:
    print("=== 迁移计划 ===")
    print(f"目标目录: {ANALYSIS_DIR}")
    print(f"待写入文件数: {len(writes)}")
    print()
    for target, content, prompt_id in writes:
        size = len(content)
        exists = "已存在 (将覆盖)" if target.exists() else "新建"
        print(f"  - {prompt_id:<24} -> {target.relative_to(ROOT)}  "
              f"({size} 字节, {exists})")

    if warnings:
        print()
        print("=== 警告 ===")
        for w in warnings:
            print(f"  ! {w}")

    print()
    print("=== 配置文件操作 ===")
    print(f"  - 备份 {CONFIG_PATH.relative_to(ROOT)} -> "
          f"{LEGACY_PATH.relative_to(ROOT)}")
    print(f"  - 重写 {CONFIG_PATH.relative_to(ROOT)}: "
          "移除 prompt_templates 字段")
    print()

    if apply:
        print(">>> 执行模式: 真正写盘 (--apply)")
    else:
        print(">>> 演练模式: 不写盘 (加 --apply 真正执行)")


def _apply(writes: list, slim_config: Dict) -> None:
    PROMPTS_ROOT.mkdir(parents=True, exist_ok=True)
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    if not LEGACY_PATH.exists():
        shutil.copy2(CONFIG_PATH, LEGACY_PATH)
        print(f"[OK] 已备份原配置到 {LEGACY_PATH.relative_to(ROOT)}")
    else:
        print(f"[SKIP] {LEGACY_PATH.relative_to(ROOT)} 已存在，不覆盖备份")

    for target, content, prompt_id in writes:
        target.write_text(content, encoding="utf-8")
        print(f"[OK] 写入 {target.relative_to(ROOT)}")

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(slim_config, f, ensure_ascii=False, indent=2)
    print(f"[OK] 重写 {CONFIG_PATH.relative_to(ROOT)} (已瘦身)")


def _validate() -> bool:
    """跑 PromptLoader.validate_all() 校验产物。"""
    loader = PromptLoader(root=PROMPTS_ROOT)
    errors = loader.validate_all()
    if errors:
        print()
        print("=== 校验失败 ===")
        for e in errors:
            print(f"  ! {e}")
        return False

    print()
    print("=== 校验通过 ===")
    all_prompts = loader.list_all()
    for category, items in all_prompts.items():
        print(f"  [{category}] {len(items)} 个")
        for t in items:
            print(f"     - {t.id:<24} {t.name}  v{t.version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正写盘（默认 dry-run）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="即使目标文件已存在也覆盖",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    try:
        config = _load_ai_config()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    writes, warnings = _plan_writes(config)
    if not writes:
        print("没有可迁移的模板，退出")
        return 0

    if not args.force:
        existing = [t for t, _, _ in writes if t.exists()]
        if existing and not args.apply:
            print(
                f"[INFO] 检测到 {len(existing)} 个目标文件已存在；"
                "加 --force 才会覆盖（dry-run 模式仍展示完整清单）"
            )

    _print_plan(writes, warnings, apply=args.apply)

    if not args.apply:
        print()
        print("[Dry-run] 未对磁盘做任何修改。确认无误后追加 --apply 执行。")
        return 0

    if not args.force:
        existing_paths = [t for t, _, _ in writes if t.exists()]
        if existing_paths:
            print(
                f"[ERROR] {len(existing_paths)} 个目标文件已存在，"
                f"请加 --force 显式覆盖：",
                file=sys.stderr,
            )
            for p in existing_paths:
                print(f"  - {p.relative_to(ROOT)}", file=sys.stderr)
            return 3

    slim = _slim_config(config)
    _apply(writes, slim)

    ok = _validate()
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
