"""
从项目根目录加载 .env 到 os.environ（无第三方依赖）
"""

import os
from typing import Optional


def load_dotenv(env_path: Optional[str] = None) -> bool:
    """
    加载 .env 文件。已存在的环境变量不会被覆盖。

    Returns:
        是否成功读取到文件
    """
    if env_path is None:
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env_path = os.path.join(base, ".env")

    if not os.path.isfile(env_path):
        return False

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        return True
    except OSError:
        return False
