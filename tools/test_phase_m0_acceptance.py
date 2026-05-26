"""Phase M0 综合验收脚本。

依次跑:
    1. PromptLoader 单元测试 (tools/test_prompt_loader.py)
    2. 迁移等价性 (tools/verify_migration_equivalence.py)
    3. theme_extractor prompt 加载
    4. AIConfig 经 PromptLoader 端到端 (get/save/delete 全套)
    5. show_ai_input 全链路 dry run (system + user prompt 构造)

任何一步失败立即返回非零。

跑法::

    py tools/test_phase_m0_acceptance.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _run_script(name: str, args: list = None) -> int:
    args = args or []
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, str(ROOT / "tools" / name)] + args
    result = subprocess.run(cmd, env=env, cwd=str(ROOT))
    return result.returncode


def step_1_unit_tests() -> bool:
    _section("Step 1/5: PromptLoader 单元测试")
    rc = _run_script("test_prompt_loader.py")
    return rc == 0


def step_2_migration_equivalence() -> bool:
    _section("Step 2/5: 迁移等价性 (legacy.json vs prompts/analysis/*.md)")
    if not (ROOT / "config" / "ai_config.legacy.json").exists():
        print("[SKIP] 找不到 ai_config.legacy.json（可能尚未迁移）")
        return True
    rc = _run_script("verify_migration_equivalence.py")
    return rc == 0


def step_3_theme_extractor_prompt() -> bool:
    _section("Step 3/5: theme_extractor prompt 加载")
    from core.theme_extractor import _load_extractor_prompts  # noqa: WPS433

    system, user = _load_extractor_prompts()
    if not system.strip():
        print("[FAIL] system prompt 为空")
        return False
    if "{report_text}" not in user:
        print("[FAIL] user template 缺少 {report_text} 占位符")
        return False
    if "schema" not in system:
        print("[FAIL] system prompt 似乎不正确（找不到 'schema' 关键词）")
        return False
    print(f"[PASS] system_len={len(system)} user_len={len(user)}")
    return True


def step_4_ai_config_roundtrip() -> bool:
    _section("Step 4/5: AIConfig 经 PromptLoader 端到端")
    from core.ai_config import AIConfig  # noqa: WPS433

    cfg = AIConfig()

    templates = cfg.get_prompt_templates()
    if not isinstance(templates, dict) or len(templates) < 5:
        print(
            f"[FAIL] get_prompt_templates 返回异常: type={type(templates)} "
            f"count={len(templates) if hasattr(templates, '__len__') else '?'}"
        )
        return False
    print(f"[PASS] get_prompt_templates: {len(templates)} 个模板")

    tmpl = cfg.get_template("standard")
    if not tmpl or not tmpl.get("system_prompt"):
        print("[FAIL] standard 模板加载失败")
        return False
    print(
        f"[PASS] get_template('standard'): name={tmpl.get('name')!r} "
        f"sys_len={len(tmpl.get('system_prompt') or '')} "
        f"usr_len={len(tmpl.get('user_prompt_template') or '')}"
    )

    fallback = cfg.get_prompt_template("__definitely_not_existing__")
    if fallback and fallback.get("system_prompt"):
        print(
            f"[PASS] get_prompt_template fallback 到 standard, "
            f"name={fallback.get('name')!r}"
        )
    else:
        print("[FAIL] fallback 行为异常")
        return False

    test_id = "m0_acceptance_test"
    test_name = "M0 验收测试"
    test_sys = "你是 M0 验收测试用 system prompt。"
    test_usr = "请输出以下新闻: {news_data}"
    try:
        cfg.save_template(test_id, test_name, test_sys, test_usr)
        loaded = cfg.get_template(test_id)
        if not loaded:
            print("[FAIL] save 后立即 get 拿不到")
            return False
        if loaded.get("system_prompt") != test_sys:
            print("[FAIL] save 后 system_prompt 不一致")
            print(f"   want: {test_sys!r}")
            print(f"   got : {loaded.get('system_prompt')!r}")
            return False
        if loaded.get("name") != test_name:
            print("[FAIL] save 后 name 不一致")
            return False
        print(f"[PASS] save_template + get_template 往返一致: {test_id}")

        deleted = cfg.delete_template(test_id)
        if not deleted:
            print("[FAIL] delete_template 返回 False")
            return False
        after = cfg.get_template(test_id)
        if after is not None:
            print("[FAIL] 删除后仍能 get 到模板")
            return False
        print(f"[PASS] delete_template: {test_id} 已被清理")
    finally:
        try:
            cfg.delete_template(test_id)
        except Exception:
            pass

    builtin_protect = cfg.delete_template("standard")
    if builtin_protect:
        print("[FAIL] 内置模板 'standard' 被错误地删除了")
        return False
    print("[PASS] 内置模板 'standard' 不可删除")
    return True


def step_5_show_ai_input() -> bool:
    _section("Step 5/5: show_ai_input 全链路")
    with tempfile.NamedTemporaryFile(
        suffix=".md", delete=False, dir=str(ROOT / ".huiye")
    ) as tf:
        out_path = tf.name
    try:
        rc = _run_script(
            "show_ai_input.py",
            ["--template", "standard", "--hours", "1", "--limit", "1",
             "--output", out_path],
        )
        if rc != 0:
            print(f"[FAIL] show_ai_input 退出码 {rc}")
            return False
        with open(out_path, "r", encoding="utf-8") as f:
            content = f.read()
        if "## 1. System Prompt" not in content:
            print("[FAIL] 输出中缺少 System Prompt 段")
            return False
        if "## 2. User Prompt" not in content:
            print("[FAIL] 输出中缺少 User Prompt 段")
            return False
        print(f"[PASS] 生成 {len(content)} 字节; 链路完整")
        return True
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 72)
    print("  Phase M0 综合验收 - Prompt 文件化")
    print("=" * 72)

    steps = [
        ("PromptLoader 单测", step_1_unit_tests),
        ("迁移等价性", step_2_migration_equivalence),
        ("theme_extractor prompt", step_3_theme_extractor_prompt),
        ("AIConfig 端到端", step_4_ai_config_roundtrip),
        ("show_ai_input 链路", step_5_show_ai_input),
    ]
    fails = []
    for name, fn in steps:
        try:
            ok = fn()
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[EXCEPTION] {name}: {type(e).__name__}: {e}")
        if not ok:
            fails.append(name)

    _section("汇总")
    print(f"  通过: {len(steps) - len(fails)} / {len(steps)}")
    if fails:
        for n in fails:
            print(f"    [FAIL] {n}")
        return 1
    print("  [OK] Phase M0 全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
