"""Tushare Pro HTTP 直连封装（Phase M1b）。

提供:

* :func:`load_token` —— 从 ``.env`` 的 ``tushare=`` 或环境变量
  ``TUSHARE_TOKEN`` 读取 token。
* :class:`TushareClient` —— 单一入口；线程安全（每次调用都走独立连接）。

设计要点
--------
* **零依赖**：使用 ``urllib.request`` + ``json``，不引入 ``requests``。
* **指数退避**：默认重试 3 次（2s / 4s / 8s），仅对网络异常或限频专属
  错误码（``-2002`` / ``40001``）重试；参数错（``40203`` 等）立刻 raise。
* **超时**：默认 120s，可覆盖。
* **错误信息**：把 ``api_name`` + ``msg`` 一起塞进 ``TushareAPIError``，便于上层
  落 ``warnings``/日志时定位是哪一个接口在抖。
* **复用**：沿用 ``tools/export_tushare_theme_daily.py`` 跑了几个月的
  ``call()`` 路径与字段展开方式（``[dict(zip(fields, row)) for row in items]``）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TUSHARE_URL = "http://api.tushare.pro"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 限频专属错误码 / 关键字（命中走重试，否则直接 raise）
_RATE_LIMIT_CODES = {-2002, 40001}
_RATE_LIMIT_KEYWORDS = ("每分钟", "每小时", "频率", "too many", "rate limit")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class TushareError(RuntimeError):
    """Tushare 客户端异常的基类。"""


class TushareAPIError(TushareError):
    """Tushare 返回 ``code != 0`` 时抛出。

    Attributes:
        api_name: 调用的接口名。
        code: Tushare 返回的 code。
        msg: Tushare 返回的 msg（中文）。
        params: 这次调用的参数（脱敏后；token 不入此处）。
    """

    def __init__(
        self,
        api_name: str,
        code: Any,
        msg: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.api_name = api_name
        self.code = code
        self.msg = msg or ""
        self.params = params or {}
        super().__init__(f"[Tushare {api_name}] code={code} msg={msg}")


class TushareTokenError(TushareError):
    """token 加载失败 / 配置缺失。"""


# ---------------------------------------------------------------------------
# token 加载
# ---------------------------------------------------------------------------


def load_token(*, env_path: Optional[Path] = None) -> str:
    """读取 Tushare token，优先级：

    1. 显式传入 ``env_path``（通常测试用）
    2. ``<repo>/.env`` 中 ``tushare=...`` 行
    3. 环境变量 ``TUSHARE_TOKEN``

    Raises:
        TushareTokenError: 全部来源都拿不到。
    """
    env_file = Path(env_path) if env_path else PROJECT_ROOT / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip().lower() == "tushare":
                    token = value.strip().strip("\"'")
                    if token:
                        return token
        except OSError as exc:
            _log.warning("读取 %s 失败: %s", env_file, exc)

    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    raise TushareTokenError(
        "未找到 Tushare token，请在 .env 中配置 tushare=xxx "
        "或设置环境变量 TUSHARE_TOKEN"
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class TushareClient:
    """轻量 HTTP 客户端，封装 token + 重试 + 字段展开。

    使用示例::

        client = TushareClient()
        rows = client.call(
            "index_daily",
            params={"ts_code": "000001.SH", "trade_date": "20260525"},
        )
        # rows 是 list[dict]，每行已展开为字段名 → 值
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        timeout: float = 120.0,
        url: str = TUSHARE_URL,
    ) -> None:
        self.token = token or load_token()
        self.timeout = timeout
        self.url = url
        self._call_count = 0
        self._counter_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        """本客户端累计成功调用次数（含重试只计一次）。"""
        return self._call_count

    def reset_counter(self) -> None:
        with self._counter_lock:
            self._call_count = 0

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def call(
        self,
        api_name: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        fields: str = "",
        max_retries: int = 3,
        initial_backoff: float = 2.0,
    ) -> List[Dict[str, Any]]:
        """调用 Tushare API，返回 ``list[dict]``。

        Args:
            api_name: 接口名，如 ``"index_daily"`` / ``"trade_cal"``。
            params: 接口参数 dict。
            fields: 想要的字段列表，逗号分隔；空字符串 = Tushare 默认字段集。
            max_retries: 网络异常 / 限频时的最大重试次数。
            initial_backoff: 首次重试等待秒数，后续指数退避。

        Returns:
            list[dict]，每条记录字段名 → 值；空时返回 ``[]``。

        Raises:
            TushareAPIError: 参数错 / 鉴权错 等 Tushare 业务错误。
            TushareError: 重试用尽后的网络异常 / 解析异常。
        """
        body = json.dumps(
            {
                "api_name": api_name,
                "token": self.token,
                "params": params or {},
                "fields": fields,
            }
        ).encode("utf-8")

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                rows = self._do_call(api_name, body, params)
                with self._counter_lock:
                    self._call_count += 1
                return rows
            except TushareAPIError as exc:
                if self._is_rate_limited(exc) and attempt < max_retries:
                    delay = initial_backoff * (2**attempt)
                    _log.warning(
                        "Tushare %s 限频，第 %d 次重试，等待 %.1fs (msg=%s)",
                        api_name,
                        attempt + 1,
                        delay,
                        exc.msg,
                    )
                    time.sleep(delay)
                    last_exc = exc
                    continue
                # 参数错 / 鉴权错 / 其他业务错：立刻抛
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise TushareError(
                        f"Tushare {api_name} 网络异常重试 {max_retries} 次仍失败: {exc}"
                    ) from exc
                delay = initial_backoff * (2**attempt)
                _log.warning(
                    "Tushare %s 网络异常 (%s)，第 %d 次重试，等待 %.1fs",
                    api_name,
                    exc,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
            except (json.JSONDecodeError, KeyError) as exc:
                raise TushareError(
                    f"Tushare {api_name} 响应解析失败: {exc}"
                ) from exc

        # 理论不会到这里，安全兜底
        raise TushareError(
            f"Tushare {api_name} 调用失败: {last_exc}"
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _do_call(
        self,
        api_name: str,
        body: bytes,
        params: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """单次 HTTP POST 调用 + 字段展开（不含重试）。"""
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw)

        code = payload.get("code", -1)
        if code != 0:
            raise TushareAPIError(
                api_name=api_name,
                code=code,
                msg=str(payload.get("msg") or ""),
                params=params,
            )

        data = payload.get("data") or {}
        flds = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(flds, row)) for row in items]

    @staticmethod
    def _is_rate_limited(exc: TushareAPIError) -> bool:
        if exc.code in _RATE_LIMIT_CODES:
            return True
        msg_lower = (exc.msg or "").lower()
        return any(kw in msg_lower for kw in _RATE_LIMIT_KEYWORDS) or any(
            kw in exc.msg for kw in ("每分钟", "每小时", "频率", "限制")
        )


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            # type: ignore[attr-defined]
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
    )

    client = TushareClient()
    print(f"[tushare_client] token loaded (len={len(client.token)})")
    rows = client.call(
        "trade_cal",
        params={
            "exchange": "SSE",
            "start_date": "20260520",
            "end_date": "20260530",
        },
    )
    print(f"[tushare_client] trade_cal 返回 {len(rows)} 行")
    for r in rows[:5]:
        print("  ", r)
    print(f"[tushare_client] 累计调用次数 = {client.call_count}")
