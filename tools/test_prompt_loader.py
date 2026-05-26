"""PromptLoader 自检脚本（Phase M0）。

跑法::

    py tools/test_prompt_loader.py

覆盖项:
    - YAML frontmatter 子集解析（标量 / 引号 / 列表 / 注释）
    - SYSTEM / USER 段切分（含只有 USER 段的场景）
    - {{include:xxx}} 单层展开 + 多层嵌套
    - include 循环引用检测
    - 缺失文件 / 缺失字段 / 错误格式的报错
    - PromptLoader.validate_all() 行为
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.prompt_loader import (  # noqa: E402
    CircularIncludeError,
    PromptLoader,
    PromptNotFoundError,
    PromptParseError,
    _parse_yaml_frontmatter,
    _split_frontmatter,
    _split_system_user,
)


# ---- 工具函数 ----

_FAILS: list = []
_PASS = 0


def _ok(name: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  [PASS] {name}")


def _fail(name: str, err: Exception) -> None:
    _FAILS.append((name, err))
    print(f"  [FAIL] {name}: {err!r}")
    traceback.print_exc()


def case(name: str):
    def deco(fn):
        try:
            fn()
            _ok(name)
        except AssertionError as e:
            _fail(name, e)
        except Exception as e:  # noqa: BLE001
            _fail(name, e)
        return fn

    return deco


# ---- YAML 子集 ----

@case("YAML: 简单 key/value")
def _():
    fm = _parse_yaml_frontmatter("id: news_focused\nversion: 1.0\n")
    assert fm["id"] == "news_focused"
    assert fm["version"] == 1.0, fm  # 数字识别


@case("YAML: 字符串带引号 / 引号内冒号")
def _():
    fm = _parse_yaml_frontmatter(
        'name: "A股: 短线分析"\n'
        "desc: 'foo bar'\n"
    )
    assert fm["name"] == "A股: 短线分析", fm
    assert fm["desc"] == "foo bar", fm


@case("YAML: 行尾注释剥离 / 字符串内 # 保留")
def _():
    fm = _parse_yaml_frontmatter(
        "id: foo  # this is comment\n"
        'note: "value with # inside"\n'
    )
    assert fm["id"] == "foo", fm
    assert fm["note"] == "value with # inside", fm


@case("YAML: 列表")
def _():
    fm = _parse_yaml_frontmatter(textwrap.dedent("""
        tags:
          - alpha
          - "beta gamma"
          - 42
        name: x
    """).lstrip())
    assert fm["tags"] == ["alpha", "beta gamma", 42], fm
    assert fm["name"] == "x"


@case("YAML: 布尔 / null")
def _():
    fm = _parse_yaml_frontmatter("a: true\nb: false\nc: null\n")
    assert fm == {"a": True, "b": False, "c": None}, fm


@case("YAML: 缺冒号抛错")
def _():
    try:
        _parse_yaml_frontmatter("invalid line\n")
    except PromptParseError:
        return
    raise AssertionError("应该抛 PromptParseError")


# ---- frontmatter / SYSTEM / USER 切分 ----

@case("frontmatter 切分")
def _():
    text = "---\nid: x\nname: Y\n---\n## SYSTEM\n\nhello\n\n## USER\n\nworld\n"
    meta, body = _split_frontmatter(text)
    assert meta == {"id": "x", "name": "Y"}
    s, u = _split_system_user(body)
    assert s == "hello", repr(s)
    assert u == "world", repr(u)


@case("仅 USER 段也允许")
def _():
    text = "## USER\n\nplain text\n"
    s, u = _split_system_user(text)
    assert s == "", repr(s)
    assert u == "plain text", repr(u)


@case("USER 在 SYSTEM 之前也兼容")
def _():
    text = "## USER\nu\n## SYSTEM\ns\n"
    s, u = _split_system_user(text)
    assert s == "s"
    assert u == "u"


@case("frontmatter 缺失抛错")
def _():
    try:
        _split_frontmatter("no frontmatter here\n")
    except PromptParseError:
        return
    raise AssertionError("应抛 PromptParseError")


# ---- PromptLoader 完整加载流程 ----

def _make_fake_prompts(root: Path) -> None:
    """搭一套最小可用的 prompts/ 目录。"""
    (root / "analysis").mkdir(parents=True)
    (root / "_shared").mkdir(parents=True)
    (root / "_shared" / "news_priority.md").write_text(
        "新闻分级规则：\n- 重点新闻：政策、技术突破\n- 一般新闻：常规动态\n",
        encoding="utf-8",
    )
    (root / "_shared" / "output_format.md").write_text(
        "输出 Markdown 格式。\n",
        encoding="utf-8",
    )
    (root / "analysis" / "news_focused.md").write_text(
        textwrap.dedent("""\
            ---
            id: news_focused
            name: 新闻分析
            version: 1.0
            description: 测试模板
            ---

            ## SYSTEM

            你是分析师。

            {{include:news_priority}}

            ## USER

            分析以下新闻 {news_data}。

            {{include:output_format}}
        """),
        encoding="utf-8",
    )


@case("PromptLoader.get: 正常加载 + include 展开")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        _make_fake_prompts(root)
        loader = PromptLoader(root=root)
        tmpl = loader.get("analysis", "news_focused")
        assert tmpl.id == "news_focused"
        assert tmpl.name == "新闻分析"
        assert "你是分析师" in tmpl.system_prompt
        assert "新闻分级规则" in tmpl.system_prompt, tmpl.system_prompt
        assert "{news_data}" in tmpl.user_prompt_template
        assert "输出 Markdown 格式" in tmpl.user_prompt_template


@case("PromptLoader.get: 缓存命中")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        _make_fake_prompts(root)
        loader = PromptLoader(root=root)
        t1 = loader.get("analysis", "news_focused")
        t2 = loader.get("analysis", "news_focused")
        assert t1 is t2, "未命中缓存"


@case("PromptLoader.reload: 清缓存")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        _make_fake_prompts(root)
        loader = PromptLoader(root=root)
        t1 = loader.get("analysis", "news_focused")
        loader.reload()
        t2 = loader.get("analysis", "news_focused")
        assert t1 is not t2, "reload 后应重新加载"


@case("PromptLoader: 缺失文件抛 PromptNotFoundError")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        root.mkdir()
        loader = PromptLoader(root=root)
        try:
            loader.get("analysis", "nope")
        except PromptNotFoundError:
            return
        raise AssertionError("应抛 PromptNotFoundError")


@case("PromptLoader: 引用不存在的 shared 抛错")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        (root / "analysis").mkdir(parents=True)
        (root / "analysis" / "t.md").write_text(
            "---\nid: t\nname: T\n---\n## USER\n{{include:no_such}}\n",
            encoding="utf-8",
        )
        loader = PromptLoader(root=root)
        try:
            loader.get("analysis", "t")
        except PromptNotFoundError:
            return
        raise AssertionError("应抛 PromptNotFoundError")


@case("PromptLoader: 循环 include 抛 CircularIncludeError")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        (root / "analysis").mkdir(parents=True)
        (root / "_shared").mkdir(parents=True)
        (root / "_shared" / "a.md").write_text(
            "{{include:b}}\n", encoding="utf-8"
        )
        (root / "_shared" / "b.md").write_text(
            "{{include:a}}\n", encoding="utf-8"
        )
        (root / "analysis" / "t.md").write_text(
            "---\nid: t\nname: T\n---\n## USER\n{{include:a}}\n",
            encoding="utf-8",
        )
        loader = PromptLoader(root=root)
        try:
            loader.get("analysis", "t")
        except CircularIncludeError:
            return
        raise AssertionError("应抛 CircularIncludeError")


@case("PromptLoader: 多层嵌套 include")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        (root / "analysis").mkdir(parents=True)
        (root / "_shared").mkdir(parents=True)
        (root / "_shared" / "level1.md").write_text(
            "L1 start\n{{include:level2}}\nL1 end\n", encoding="utf-8"
        )
        (root / "_shared" / "level2.md").write_text(
            "L2 content\n", encoding="utf-8"
        )
        (root / "analysis" / "t.md").write_text(
            "---\nid: t\nname: T\n---\n## USER\n{{include:level1}}\n",
            encoding="utf-8",
        )
        loader = PromptLoader(root=root)
        tmpl = loader.get("analysis", "t")
        assert "L1 start" in tmpl.user_prompt_template
        assert "L2 content" in tmpl.user_prompt_template
        assert "L1 end" in tmpl.user_prompt_template


@case("PromptLoader.list_category: 文件夹列举 + _ 前缀过滤")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        (root / "analysis").mkdir(parents=True)
        for name in ("a", "b", "_internal"):
            (root / "analysis" / f"{name}.md").write_text(
                f"---\nid: {name}\nname: {name.upper()}\n---\n## USER\nhello\n",
                encoding="utf-8",
            )
        loader = PromptLoader(root=root)
        items = loader.list_category("analysis")
        ids = {t.id for t in items}
        assert ids == {"a", "b"}, ids


@case("PromptLoader.validate_all: 检测 id 不一致")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        (root / "analysis").mkdir(parents=True)
        (root / "analysis" / "right.md").write_text(
            "---\nid: WRONG_ID\nname: x\n---\n## USER\nhi\n",
            encoding="utf-8",
        )
        loader = PromptLoader(root=root)
        errors = loader.validate_all()
        assert any("id" in e and "right" in e for e in errors), errors


@case("PromptLoader.save + delete")
def _():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "prompts"
        loader = PromptLoader(root=root)
        raw = "---\nid: new\nname: New\n---\n## USER\nbody\n"
        tmpl = loader.save("analysis", "new", raw)
        assert tmpl.name == "New"
        assert (root / "analysis" / "new.md").is_file()
        assert loader.delete("analysis", "new") is True
        assert not (root / "analysis" / "new.md").exists()


# ---- 汇总 ----

print("\n=== PromptLoader 自检结果 ===")
print(f"通过: {_PASS}    失败: {len(_FAILS)}")
if _FAILS:
    print("\n失败明细:")
    for name, err in _FAILS:
        print(f"  - {name}: {err}")
    sys.exit(1)
print("[OK] 全部通过")
