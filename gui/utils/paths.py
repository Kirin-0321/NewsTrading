"""GUI 路径解析工具。"""

import os
import sys
from typing import Optional


def get_app_root() -> str:
    """返回应用根目录（开发环境=项目根，打包后=exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    from services.storage.database import get_project_root
    return get_project_root()


def resolve_project_path(path: str) -> str:
    """将相对路径解析为基于项目根目录的绝对路径。"""
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    from services.storage.database import get_project_root
    return os.path.normpath(os.path.join(get_project_root(), path))


def find_typora_executable() -> Optional[str]:
    """查找 Typora 可执行文件路径。"""
    candidates = [
        os.path.join(get_app_root(), "Typora", "Typora.exe"),
    ]
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates.append(os.path.join(local_app, "Programs", "Typora", "Typora.exe"))
    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_key)
        if base:
            candidates.append(os.path.join(base, "Typora", "Typora.exe"))

    seen = set()
    for path in candidates:
        norm = os.path.normcase(os.path.normpath(path))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.exists(path):
            return path
    return None
