"""题材抽取·自动重试逻辑回归测试 CLI（mock _call_llm，不调真实 LLM）。

业务背景
--------
2026-05-28 hotfix：``ThemeExtractor.extract_from_text`` 包重试循环，
应对"流式输出长时间卡住"的故障。本 CLI 用 monkey-patch 替换
``_call_llm`` 模拟 4 种故障与成功场景，验证：

* 重试次数符合 ``max_attempts``（默认 3）
* 重试间退避真实发生（time.sleep 被调用，秒数符合 2/4/8 模式）
* ``_is_retryable_error`` 判定准确（鉴权/400 立即失败，超时/5xx 重试）
* 解析全失败也会重试，最后兜底落盘失败上下文
* 首次成功不触发任何重试

用法
----
    python tools/test_theme_extract_retry.py            # 全部 5 个用例
    python tools/test_theme_extract_retry.py --case A   # 单跑用例 A（首次成功）
    python tools/test_theme_extract_retry.py --case B   # 单跑用例 B（断流后重试成功）
    python tools/test_theme_extract_retry.py --case C   # 单跑用例 C（重试用尽）
    python tools/test_theme_extract_retry.py --case D   # 单跑用例 D（不可重试·立即失败）
    python tools/test_theme_extract_retry.py --case E   # 单跑用例 E（_is_retryable_error 单元）

退出码
------
    0  全部用例通过
    1  至少一个用例失败
    2  参数错误

何时重跑
--------
* 修改 ``core/theme_extractor.py::extract_from_text`` 重试逻辑后
* 修改 ``_is_retryable_error`` 判定规则后
* 调整 ``stream_idle_timeout`` / ``max_attempts`` 默认值后
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.theme_extractor import ThemeExtractor  # noqa: E402


# ---------------------------------------------------------------------------
# 测试工具：构造一个不走 __init__ 的 ThemeExtractor，避免依赖 AIConfig
# ---------------------------------------------------------------------------

def _make_extractor(max_attempts: int = 3) -> ThemeExtractor:
    """构造一个跳过 __init__ 的 ThemeExtractor，注入测试用属性。"""
    ext = ThemeExtractor.__new__(ThemeExtractor)
    ext.config = None
    ext.provider = "deepseek"
    ext.model = "deepseek-v4-pro"
    ext.temperature = 0.2
    ext.max_tokens = 65536
    ext.stream_idle_timeout = 60.0
    ext.max_attempts = max_attempts
    ext._last_finish_reason = None
    ext._last_raw_response = None
    return ext


def _patch_sleep(ext_module) -> Tuple[List[float], callable]:
    """把 time.sleep 替换成纯记录，避免测试真的等 2+4+8 秒。"""
    recorded: List[float] = []
    original = ext_module.time.sleep

    def fake_sleep(sec):
        recorded.append(sec)

    ext_module.time.sleep = fake_sleep
    return recorded, original


def _restore_sleep(ext_module, original) -> None:
    ext_module.time.sleep = original


# 一个最小的"合法 LLM JSON 输出"，能被 _parse_response 解析出题材
_VALID_LLM_OUTPUT = """
{
  "themes": [
    {
      "theme_name": "测试题材",
      "theme_category": "科技AI",
      "strength_score": 50,
      "reason": "回归测试用例",
      "stocks": [{"name": "测试股票A", "code": "000001"}],
      "news": ["新闻1"]
    }
  ]
}
"""


# 一个肯定解析失败的非 JSON 文本（4 级容错也救不出来）
_INVALID_LLM_OUTPUT = "我拒绝回答这个问题，请提供更多上下文信息。"


# ---------------------------------------------------------------------------
# 用例 A：首次成功 - 不应有任何重试，sleep 不被调用
# ---------------------------------------------------------------------------

def case_a_success_first_try() -> int:
    print("\n=== 用例 A：首次成功，无重试 ===")
    import core.theme_extractor as mod

    ext = _make_extractor(max_attempts=3)
    sleep_log, original = _patch_sleep(mod)

    call_count = {"n": 0}

    def fake_call(report_text, progress_callback=None):
        call_count["n"] += 1
        # 模拟流式输出（不抛异常，直接返回完整 JSON）
        return _VALID_LLM_OUTPUT

    ext._call_llm = fake_call

    progress_msgs: List[str] = []

    def cb(msg, is_streaming=False):
        progress_msgs.append(msg)

    fails = 0
    try:
        themes, err = ext.extract_from_text("fake report text", cb)
        if call_count["n"] != 1:
            print(f"  [FAIL] _call_llm 调用次数 {call_count['n']} ≠ 1")
            fails += 1
        if len(sleep_log) != 0:
            print(f"  [FAIL] sleep 不应被调用，实际 sleep={sleep_log}")
            fails += 1
        if not themes:
            print(f"  [FAIL] 题材列表为空，err={err}")
            fails += 1
        else:
            print(
                f"  [OK] 一次过：题材={len(themes)} 条, "
                f"首条={themes[0]['theme_name']}"
            )
        retry_hints = [
            m for m in progress_msgs if "重试" in m or "次尝试" in m
        ]
        if retry_hints:
            print(f"  [FAIL] 不应推送重试提示，实际收到: {retry_hints}")
            fails += 1
    finally:
        _restore_sleep(mod, original)

    return fails


# ---------------------------------------------------------------------------
# 用例 B：首次抛 ReadTimeout，第二次成功 - 应重试 1 次（sleep=[2]）
# ---------------------------------------------------------------------------

def case_b_retry_then_success() -> int:
    print("\n=== 用例 B：首次断流（ReadTimeout），第二次成功 ===")
    import core.theme_extractor as mod

    ext = _make_extractor(max_attempts=3)
    sleep_log, original = _patch_sleep(mod)

    call_count = {"n": 0}

    # 构造一个伪 ReadTimeout（用 TimeoutError 子类，因 _is_retryable_error
    # 是基于类名/消息匹配的，TimeoutError.__name__='TimeoutError' 命中 'timeout'）
    class FakeReadTimeout(TimeoutError):
        pass

    def fake_call(report_text, progress_callback=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise FakeReadTimeout("stream chunk read timed out after 60s")
        return _VALID_LLM_OUTPUT

    ext._call_llm = fake_call

    progress_msgs: List[str] = []

    def cb(msg, is_streaming=False):
        progress_msgs.append(msg)

    fails = 0
    try:
        themes, err = ext.extract_from_text("fake report", cb)
        if call_count["n"] != 2:
            print(f"  [FAIL] _call_llm 调用次数 {call_count['n']} ≠ 2")
            fails += 1
        if sleep_log != [2]:
            print(f"  [FAIL] sleep 序列应为 [2]，实际 {sleep_log}")
            fails += 1
        if not themes:
            print(f"  [FAIL] 题材应解析成功，实际 err={err}")
            fails += 1
        else:
            print(
                f"  [OK] 重试 1 次后成功: sleep_log={sleep_log} "
                f"题材={len(themes)} 条"
            )
        retry_hints = [m for m in progress_msgs if "重试" in m]
        if not retry_hints:
            print("  [FAIL] 应至少推送一条重试提示")
            fails += 1
        else:
            print(f"  [OK] 重试提示推送数: {len(retry_hints)}")
    finally:
        _restore_sleep(mod, original)

    return fails


# ---------------------------------------------------------------------------
# 用例 C：解析全失败（非 JSON 文本）3 次都失败 → 兜底返错
# ---------------------------------------------------------------------------

def case_c_parse_failure_exhausted() -> int:
    print("\n=== 用例 C：LLM 返回非 JSON，重试用尽返错 ===")
    import core.theme_extractor as mod

    ext = _make_extractor(max_attempts=3)
    sleep_log, original = _patch_sleep(mod)

    call_count = {"n": 0}

    def fake_call(report_text, progress_callback=None):
        call_count["n"] += 1
        return _INVALID_LLM_OUTPUT

    ext._call_llm = fake_call

    ext._dump_failure = staticmethod(lambda *args, **kwargs: None)

    progress_msgs: List[str] = []

    def cb(msg, is_streaming=False):
        progress_msgs.append(msg)

    fails = 0
    try:
        themes, err = ext.extract_from_text("fake report", cb)
        if call_count["n"] != 3:
            print(
                f"  [FAIL] _call_llm 调用次数 {call_count['n']} "
                f"≠ 3 (max_attempts)"
            )
            fails += 1
        if sleep_log != [2, 4]:
            print(f"  [FAIL] sleep 序列应为 [2, 4]，实际 {sleep_log}")
            fails += 1
        if themes:
            print(f"  [FAIL] 题材应为空，实际拿到 {len(themes)} 条")
            fails += 1
        if not err:
            print("  [FAIL] err 应非空")
            fails += 1
        else:
            print(
                f"  [OK] 重试用尽: 调用 {call_count['n']} 次, "
                f"sleep_log={sleep_log}"
            )
            print(f"       err 摘要: {err[:80]}...")
    finally:
        _restore_sleep(mod, original)

    return fails


# ---------------------------------------------------------------------------
# 用例 D：不可重试错误（鉴权失败 401）→ 立即失败，不重试
# ---------------------------------------------------------------------------

def case_d_non_retryable_fast_fail() -> int:
    print("\n=== 用例 D：不可重试错误（401 鉴权失败），立即失败 ===")
    import core.theme_extractor as mod

    ext = _make_extractor(max_attempts=3)
    sleep_log, original = _patch_sleep(mod)

    call_count = {"n": 0}

    class FakeAuthError(Exception):
        pass

    def fake_call(report_text, progress_callback=None):
        call_count["n"] += 1
        raise FakeAuthError("401 Unauthorized: invalid api key")

    ext._call_llm = fake_call

    progress_msgs: List[str] = []

    def cb(msg, is_streaming=False):
        progress_msgs.append(msg)

    fails = 0
    try:
        themes, err = ext.extract_from_text("fake report", cb)
        if call_count["n"] != 1:
            print(
                f"  [FAIL] 不可重试错误应只调用 1 次，"
                f"实际 {call_count['n']} 次"
            )
            fails += 1
        if sleep_log:
            print(f"  [FAIL] 不应 sleep，实际 sleep_log={sleep_log}")
            fails += 1
        if themes:
            print(f"  [FAIL] 题材应为空，实际拿到 {len(themes)} 条")
            fails += 1
        if not err:
            print("  [FAIL] err 应非空")
            fails += 1
        else:
            print(f"  [OK] 立即失败: 调用 1 次, sleep_log={sleep_log}")
            print(f"       err 摘要: {err[:80]}")
    finally:
        _restore_sleep(mod, original)

    return fails


# ---------------------------------------------------------------------------
# 用例 E：_is_retryable_error 判定单元测试（不走重试主循环）
# ---------------------------------------------------------------------------

def case_e_retryable_judge() -> int:
    print("\n=== 用例 E：_is_retryable_error 判定逻辑 ===")

    retryable_cases = [
        (TimeoutError("read timed out"), "TimeoutError"),
        (Exception("502 Bad Gateway"), "5xx 服务端错误"),
        (Exception("Connection reset by peer"), "连接重置"),
        (Exception("rate limit exceeded"), "限流"),
        (Exception("Remote end closed connection without response"), "SSE 断流"),
    ]
    non_retryable_cases = [
        (ValueError("未配置 deepseek API Key"), "ValueError·配置缺失"),
        (Exception("401 Unauthorized"), "401 鉴权"),
        (Exception("403 Forbidden"), "403 权限"),
        (Exception("400 Bad Request: invalid prompt"), "400 客户端错误"),
    ]

    fails = 0
    print("  -- 可重试场景 --")
    for exc, label in retryable_cases:
        got = ThemeExtractor._is_retryable_error(exc)
        marker = "[OK]" if got else "[FAIL]"
        if not got:
            fails += 1
        print(f"    {marker} {label}: _is_retryable_error -> {got}")

    print("  -- 不可重试场景 --")
    for exc, label in non_retryable_cases:
        got = ThemeExtractor._is_retryable_error(exc)
        marker = "[OK]" if not got else "[FAIL]"
        if got:
            fails += 1
        print(f"    {marker} {label}: _is_retryable_error -> {got}")

    return fails


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_CASES = {
    "A": ("首次成功", case_a_success_first_try),
    "B": ("断流后重试成功", case_b_retry_then_success),
    "C": ("重试用尽", case_c_parse_failure_exhausted),
    "D": ("不可重试·立即失败", case_d_non_retryable_fast_fail),
    "E": ("_is_retryable_error 判定", case_e_retryable_judge),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--case",
        type=str,
        choices=sorted(_CASES.keys()),
        help="单跑指定用例（A/B/C/D/E）；不传则跑全部",
    )
    args = parser.parse_args()

    if args.case:
        cases = [(args.case, _CASES[args.case])]
    else:
        cases = list(_CASES.items())

    total_fails = 0
    for cid, (name, func) in cases:
        try:
            fails = func()
        except Exception as exc:  # noqa: BLE001
            print(f"  [CRASH] 用例 {cid}({name}) 抛异常: {type(exc).__name__}: {exc}")
            fails = 1
        total_fails += fails

    print("\n" + "=" * 60)
    if total_fails == 0:
        print(f"全部通过：{len(cases)} 个用例 0 失败")
        return 0
    print(f"失败：{total_fails} 处")
    return 1


if __name__ == "__main__":
    sys.exit(main())
