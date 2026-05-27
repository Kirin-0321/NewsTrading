"""验证 plan M3 打分任务调度桩响应 CLI。

用法：
    python tools/test_scoring_scheduled_stub.py

退出码：
    0  4 个新任务类型都能进入桩分支（返回 ok=False / stub=True / 有 hint）
    1  至少一个失败

何时重跑：
    - plan M3 真实实施时（这个脚本会失败 → 期望，因为真实逻辑会替换桩）
    - 修改 services/scheduled_runner.py 后
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from services.scheduled_runner import run_task_sync  # noqa: E402

_NEW_TYPES = [
    "stock_daily_sync",
    "sector_daily_sync",
    "theme_score_daily",
    "theme_ai_review",
]


def main() -> int:
    fails = 0
    print("\n=== plan M3 4 个新任务类型桩响应验证 ===")
    for t in _NEW_TYPES:
        task = {"id": f"test_{t}", "name": t, "type": t, "params": {}}
        result = run_task_sync(task)
        ok_stub = (
            result.get("ok") is False
            and result.get("type") == t
            and isinstance(result.get("result"), dict)
            and result["result"].get("stub") is True
            and "未实施" in (result.get("error") or "")
        )
        marker = "[OK]" if ok_stub else "[FAIL]"
        print(f"  {marker} {t}")
        print(f"       error: {result.get('error')}")
        if not ok_stub:
            fails += 1

    # 反向校验：旧任务类型不受影响
    print("\n=== 反向校验：未知任务类型仍返回 '未知任务类型' ===")
    unknown_task = {"id": "x", "name": "x", "type": "unknown_xxx",
                    "params": {}}
    result = run_task_sync(unknown_task)
    if (result.get("ok") is False
            and "未知任务类型" in (result.get("error") or "")):
        print("  [OK] unknown_xxx -> 仍走 '未知任务类型' 分支")
    else:
        print(f"  [FAIL] unknown_xxx 异常: {result}")
        fails += 1

    print(f"\n========== 总结：{fails} 个失败 ==========")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[用户中断]")
        sys.exit(2)
