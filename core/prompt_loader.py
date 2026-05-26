"""统一 Prompt 加载器（Phase M0 引入）。

设计要点:
    - 文件格式：MD + YAML frontmatter（`---` 围起来）
    - 内容用 `## SYSTEM` / `## USER` 两个二级标题分段
    - 支持 ``{{include:NAME}}`` 引用 ``_shared/NAME.md`` 复用片段
    - 零新依赖：手写 YAML 子集解析器（key/quoted-str/list-of-str/int/float/bool）

调用链:
    AIConfig.get_prompt_template(template_id)
        └→ PromptLoader.get(category, prompt_id)
                ├→ _parse_file()         读取文件 + 解析 frontmatter / SYSTEM / USER
                └→ _expand_includes()    递归展开 {{include:xxx}}（带循环检测）

文件示例 ``prompts/analysis/news_focused.md``::

    ---
    id: news_focused
    name: 新闻分析
    version: 1.0
    category: analysis
    description: 专注新闻催化剂挖掘
    ---

    ## SYSTEM

    你是一名资深 A 股投资策略分析师...

    {{include:news_priority_rules}}

    ## USER

    请基于以下新闻数据，挖掘投资机会：

    {news_data}
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_FRONTMATTER_DELIM = "---"
_SYSTEM_MARKER_PAT = re.compile(r"(?m)^##\s+SYSTEM\s*$")
_USER_MARKER_PAT = re.compile(r"(?m)^##\s+USER\s*$")
_INCLUDE_PAT = re.compile(r"\{\{include:([A-Za-z0-9_\-]+)\}\}")
_MAX_INCLUDE_DEPTH = 8


class PromptError(Exception):
    """PromptLoader 基础异常。"""


class PromptNotFoundError(PromptError):
    """指定 category/prompt_id 不存在。"""


class PromptParseError(PromptError):
    """frontmatter / 内容解析失败。"""


class CircularIncludeError(PromptError):
    """include 出现循环引用。"""


@dataclass
class PromptTemplate:
    """单个 prompt 模板的运行时表示。

    Attributes:
        id: 文件名（不含 .md）
        name: 展示名（必填）
        category: 所属类别（analysis / market_fetch / theme_extraction 等）
        version: 版本号，缺省 "1.0"
        description: 简介
        system_prompt: 已展开 include 的 SYSTEM 段
        user_prompt_template: 已展开 include 的 USER 段
        provider_default / model_default / temperature_default:
            模板自带的推荐配置，调用方可覆盖
        file_path: 源文件路径
        metadata: 整个 frontmatter dict（兜底，含未列出的扩展字段）
    """

    id: str
    name: str
    category: str
    version: str = "1.0"
    description: str = ""
    system_prompt: str = ""
    user_prompt_template: str = ""
    provider_default: Optional[str] = None
    model_default: Optional[str] = None
    temperature_default: Optional[float] = None
    file_path: Optional[Path] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_text: Optional[str] = None
    raw_system: Optional[str] = None
    raw_user: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """兼容 ``AIConfig.get_prompt_template()`` 老返回格式。"""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "version": self.version,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "user_prompt_template": self.user_prompt_template,
        }


def _strip_inline_comment(value: str) -> str:
    """去掉行尾 `# comment`，但保留字符串内的 `#`。"""
    in_str = False
    quote: Optional[str] = None
    out_chars = []
    for ch in value:
        if in_str:
            out_chars.append(ch)
            if ch == quote:
                in_str = False
                quote = None
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            out_chars.append(ch)
            continue
        if ch == "#":
            break
        out_chars.append(ch)
    return "".join(out_chars).rstrip()


def _parse_scalar(value: str) -> Any:
    """把 YAML 标量字符串转换成 Python 值。

    支持: ``"quoted"`` / ``'quoted'`` / int / float / true/false/null
    其他情况按 strip 后的字符串返回。
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return ""

    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        inner = raw[1:-1]
        # 处理常见转义
        if raw.startswith('"'):
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner

    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None

    try:
        if raw.startswith(("0x", "-0x")):
            return int(raw, 16)
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass

    return raw


def _parse_yaml_frontmatter(text: str) -> Dict[str, Any]:
    """YAML 子集解析器（够用即止）。

    支持的语法（按行）:
        key: value                    -> {key: value}
        key: "quoted value"           -> {key: "quoted value"}
        key:                          -> 开始列表或子映射
          - item                      -> list of strings
          - item

    不支持: 嵌套 dict 第二层及以上、流式 (`[a, b]`)、anchors、tags

    Args:
        text: frontmatter 内部内容（不含两端 ``---`` 行）

    Returns:
        dict 形式的 metadata。

    Raises:
        PromptParseError: 缩进 / 格式错乱时抛出。
    """
    lines = text.splitlines()
    result: Dict[str, Any] = {}
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # 顶层 K-V
        if ":" not in line:
            raise PromptParseError(
                f"frontmatter 第 {i + 1} 行格式错误：缺少冒号。原始行: {line!r}"
            )

        key_part, sep, value_part = line.partition(":")
        if line.startswith(" ") or line.startswith("\t"):
            raise PromptParseError(
                f"frontmatter 第 {i + 1} 行缩进异常（顶层 key 不应缩进）: {line!r}"
            )
        key = key_part.strip()
        value = _strip_inline_comment(value_part)

        if value.strip() == "":
            # 空 value -> 看下一行是否是 list
            items: List[Any] = []
            j = i + 1
            while j < n:
                child = lines[j]
                if not child.strip():
                    j += 1
                    continue
                if child.startswith(("  - ", "\t- ", "- ")):
                    # list item
                    item_text = child.lstrip()[2:]
                    items.append(_parse_scalar(_strip_inline_comment(item_text)))
                    j += 1
                    continue
                if child.startswith((" ", "\t")):
                    raise PromptParseError(
                        f"frontmatter 第 {j + 1} 行: 暂不支持嵌套映射，仅支持列表"
                    )
                break
            if items:
                result[key] = items
            else:
                result[key] = None
            i = j
            continue

        result[key] = _parse_scalar(value)
        i += 1

    return result


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """切分 frontmatter 与正文。返回 (metadata, body)。"""
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise PromptParseError("文件首行必须是 ---（YAML frontmatter 起始）")

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            end_idx = i
            break

    if end_idx == -1:
        raise PromptParseError("找不到 frontmatter 结束的 ---")

    fm_text = "\n".join(lines[1:end_idx])
    body_text = "\n".join(lines[end_idx + 1 :])
    metadata = _parse_yaml_frontmatter(fm_text)
    return metadata, body_text


def _split_system_user(body: str) -> Tuple[str, str]:
    """按 ## SYSTEM / ## USER 拆分正文，返回 (system_text, user_text)。

    允许只有 USER 段（部分场景如 theme_extraction 可以省略 SYSTEM），
    但若两者都缺失则抛 PromptParseError。
    """
    sys_match = _SYSTEM_MARKER_PAT.search(body)
    user_match = _USER_MARKER_PAT.search(body)

    if sys_match and user_match:
        sys_start = sys_match.start()
        sys_end = sys_match.end()
        usr_start = user_match.start()
        usr_end = user_match.end()
        if sys_start < usr_start:
            system_text = body[sys_end:usr_start].strip("\n")
            user_text = body[usr_end:].strip("\n")
        else:
            user_text = body[usr_end:sys_start].strip("\n")
            system_text = body[sys_end:].strip("\n")
        return system_text, user_text

    if user_match:
        system_text = body[:user_match.start()].strip("\n")
        user_text = body[user_match.end():].strip("\n")
        return system_text, user_text

    if sys_match:
        system_text = body[sys_match.end():].strip("\n")
        return system_text, ""

    raise PromptParseError("正文缺少 `## SYSTEM` 或 `## USER` 段")


class PromptLoader:
    """统一 prompt 加载器（线程安全）。

    用法::

        loader = PromptLoader()                                # 默认 prompts/ 根目录
        tmpl = loader.get("analysis", "news_focused")
        print(tmpl.system_prompt)

    设计约束:
        - 缓存 by (category, prompt_id)；GUI 编辑后必须 ``reload()``
        - include 仅允许引用 ``_shared/{name}.md``，不允许跨类别相互引用，
          防止意外依赖
    """

    SHARED_DIR_NAME = "_shared"

    def __init__(self, root: Optional[Path] = None):
        self.root = (root or Path("prompts")).resolve() if root else Path("prompts")
        # 不强行 resolve()，方便测试时传相对路径
        if root is None:
            self.root = Path("prompts")
        self._cache: Dict[Tuple[str, str], PromptTemplate] = {}
        self._shared_cache: Dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- 公共接口 ----

    def get(self, category: str, prompt_id: str) -> PromptTemplate:
        """加载指定 prompt（命中缓存则直接返回）。

        Raises:
            PromptNotFoundError: 文件不存在
            PromptParseError: 文件格式错误
            CircularIncludeError: include 循环引用
        """
        key = (category, prompt_id)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            tmpl = self._load(category, prompt_id)
            self._cache[key] = tmpl
            return tmpl

    def list_category(self, category: str) -> List[PromptTemplate]:
        """列出某 category 下所有 prompt。"""
        cat_dir = self.root / category
        if not cat_dir.is_dir():
            return []
        results: List[PromptTemplate] = []
        for md_file in sorted(cat_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            try:
                results.append(self.get(category, md_file.stem))
            except PromptError as e:
                # 不让单个坏文件阻塞列表（GUI 用），但保留错误信息
                results.append(
                    PromptTemplate(
                        id=md_file.stem,
                        name=f"[加载失败] {md_file.stem}",
                        category=category,
                        description=str(e),
                        file_path=md_file,
                    )
                )
        return results

    def list_all(self) -> Dict[str, List[PromptTemplate]]:
        """全量列出，按 category 分组。"""
        out: Dict[str, List[PromptTemplate]] = {}
        if not self.root.is_dir():
            return out
        for sub in sorted(self.root.iterdir()):
            if sub.is_dir() and sub.name != self.SHARED_DIR_NAME:
                out[sub.name] = self.list_category(sub.name)
        return out

    def reload(self) -> None:
        """清空缓存。GUI 编辑器保存后调用。"""
        with self._lock:
            self._cache.clear()
            self._shared_cache.clear()

    def save(
        self,
        category: str,
        prompt_id: str,
        raw_text: str,
    ) -> PromptTemplate:
        """GUI 保存接口：写入文件 + 重新加载。

        Args:
            raw_text: 完整文件内容（含 frontmatter）
        """
        cat_dir = self.root / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        path = cat_dir / f"{prompt_id}.md"
        path.write_text(raw_text, encoding="utf-8")
        with self._lock:
            self._cache.pop((category, prompt_id), None)
            self._shared_cache.clear()
        return self.get(category, prompt_id)

    def delete(self, category: str, prompt_id: str) -> bool:
        """删除某 prompt 文件。"""
        path = self.root / category / f"{prompt_id}.md"
        if not path.exists():
            return False
        path.unlink()
        with self._lock:
            self._cache.pop((category, prompt_id), None)
        return True

    def validate_all(self) -> List[str]:
        """启动校验：返回错误清单（空表示无错）。

        检查项:
            - frontmatter 必填字段 (id / name)
            - id 与文件名一致
            - {{include:xxx}} 引用存在
            - 无循环 include
            - SYSTEM/USER 段存在
        """
        errors: List[str] = []
        if not self.root.is_dir():
            return [f"prompts/ 根目录不存在: {self.root}"]

        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir() or sub.name == self.SHARED_DIR_NAME:
                continue
            for md_file in sorted(sub.glob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                rel = md_file.relative_to(self.root)
                try:
                    tmpl = self._load(sub.name, md_file.stem)
                except PromptError as e:
                    errors.append(f"[{rel}] {type(e).__name__}: {e}")
                    continue

                if not tmpl.name:
                    errors.append(f"[{rel}] 缺少 name 字段")
                if tmpl.id != md_file.stem:
                    errors.append(
                        f"[{rel}] frontmatter.id ({tmpl.id!r}) 与文件名 "
                        f"({md_file.stem!r}) 不一致"
                    )
                if not tmpl.system_prompt and not tmpl.user_prompt_template:
                    errors.append(f"[{rel}] SYSTEM 与 USER 段均为空")

        return errors

    # ---- 内部实现 ----

    def _load(self, category: str, prompt_id: str) -> PromptTemplate:
        path = self.root / category / f"{prompt_id}.md"
        if not path.is_file():
            raise PromptNotFoundError(
                f"prompt 文件不存在: {path}（category={category!r}, "
                f"id={prompt_id!r}）"
            )
        raw = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(raw)
        system_raw, user_raw = _split_system_user(body)

        # 展开 include
        system_expanded = self._expand_includes(system_raw, [])
        user_expanded = self._expand_includes(user_raw, [])

        return PromptTemplate(
            id=str(metadata.get("id") or prompt_id),
            name=str(metadata.get("name") or prompt_id),
            category=str(metadata.get("category") or category),
            version=str(metadata.get("version") or "1.0"),
            description=str(metadata.get("description") or ""),
            system_prompt=system_expanded,
            user_prompt_template=user_expanded,
            provider_default=_optional_str(metadata.get("provider_default")),
            model_default=_optional_str(metadata.get("model_default")),
            temperature_default=_optional_float(metadata.get("temperature_default")),
            file_path=path,
            metadata=metadata,
            raw_text=raw,
            raw_system=system_raw,
            raw_user=user_raw,
        )

    def _expand_includes(self, text: str, stack: List[str]) -> str:
        """递归展开 ``{{include:name}}``。

        Args:
            text: 待展开文本
            stack: 当前展开栈（用于循环检测）

        Raises:
            CircularIncludeError: 循环引用
            PromptNotFoundError: 引用了不存在的 shared
        """
        if len(stack) > _MAX_INCLUDE_DEPTH:
            raise CircularIncludeError(
                f"include 深度超过 {_MAX_INCLUDE_DEPTH}（路径: "
                f"{' -> '.join(stack)}）"
            )

        def _repl(m: re.Match) -> str:
            name = m.group(1)
            if name in stack:
                raise CircularIncludeError(
                    f"include 循环引用：{' -> '.join(stack + [name])}"
                )
            content = self._load_shared(name)
            return self._expand_includes(content, stack + [name])

        return _INCLUDE_PAT.sub(_repl, text)

    def _load_shared(self, name: str) -> str:
        """加载 `_shared/{name}.md`（命中缓存）。

        共享片段无需 frontmatter / SYSTEM / USER，整个文件内容即结果。
        """
        cached = self._shared_cache.get(name)
        if cached is not None:
            return cached
        path = self.root / self.SHARED_DIR_NAME / f"{name}.md"
        if not path.is_file():
            raise PromptNotFoundError(
                f"shared 片段不存在: {path}（include 引用 {name!r}）"
            )
        text = path.read_text(encoding="utf-8")
        # 如果共享文件也带 frontmatter（用于版本标记），自动剥离
        if text.lstrip().startswith(_FRONTMATTER_DELIM + "\n") or text.lstrip().startswith(
            _FRONTMATTER_DELIM + "\r\n"
        ):
            try:
                _, body = _split_frontmatter(text)
                text = body.lstrip("\n")
            except PromptParseError:
                pass
        self._shared_cache[name] = text
        return text


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- 全局单例（线程安全惰性初始化）----

_GLOBAL_LOADER: Optional[PromptLoader] = None
_GLOBAL_LOADER_LOCK = threading.Lock()


def get_loader() -> PromptLoader:
    """获取进程全局 PromptLoader（默认根 ``prompts/``）。"""
    global _GLOBAL_LOADER
    if _GLOBAL_LOADER is None:
        with _GLOBAL_LOADER_LOCK:
            if _GLOBAL_LOADER is None:
                _GLOBAL_LOADER = PromptLoader()
    return _GLOBAL_LOADER


if __name__ == "__main__":  # pragma: no cover
    # CLI 自检：列出全部 prompt + 跑 validate_all
    import sys

    loader = PromptLoader()
    print(f"prompts 根目录: {loader.root.resolve()}")
    print()

    # 控制台可能是 GBK，主动切到 utf-8 避免 emoji 报错
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    all_prompts = loader.list_all()
    if not all_prompts:
        print("[INFO] prompts/ 目录暂无任何 .md 文件，请先跑迁移脚本")
    for category, templates in all_prompts.items():
        print(f"  [{category}] ({len(templates)} 个)")
        for t in templates:
            print(f"     - {t.id:<32} {t.name}  v{t.version}")

    print()
    errors = loader.validate_all()
    if errors:
        print(f"[FAIL] validate_all 发现 {len(errors)} 个问题:")
        for e in errors:
            print(f"   - {e}")
        sys.exit(1)
    print("[OK] validate_all 通过")
