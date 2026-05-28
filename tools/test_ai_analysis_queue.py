"""回归测试：AI 分析任务队列管理器（2026-05-28 批量模板改造配套）。

测试范围
--------
* AnalysisTaskManager 队列调度（FIFO + 可配置并发上限）
* 默认并发数 = 16（2026-05-28 23:00 主人指令：3 → 16；硬上限 16 → 64）
* 任务 5 态机（PENDING → RUNNING → SUCCESS/SKIPPED/FAILED）
* 删除策略（终态可删；运行中拒绝）
* shutdown 取消 PENDING
* set_max_concurrent 调大/调小/clamp（上限 64）
* task_finished 信号去重（worker 多终态信号同步触发只 emit 一次）

测试策略
--------
**不调真 LLM**：monkey-patch ``services.analysis_service.AnalysisService.analyze``
为 fake 实现（sleep + 返回预设 AnalysisResult）。worker 内部
``from services.analysis_service import AnalysisService`` 是模块属性查询，
patch 模块属性即可生效。

用法
----
::

    python tools/test_ai_analysis_queue.py

退出码：
* 0 = 全部用例通过
* 1 = 任一用例失败
* 2 = 测试本身崩溃
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import services.analysis_service as svc_module  # noqa: E402
from services.analysis_service import AnalysisResult  # noqa: E402

from gui.workers.ai_analysis_manager import (  # noqa: E402
    AnalysisTaskManager, TaskStatus,
)


# ---------------------------------------------------------------------------
# Fake AnalysisService.analyze
# ---------------------------------------------------------------------------


_fake_config = {
    "sleep": 0.3,            # 每次跑模拟耗时
    "fail_for": set(),       # 命中 template_id → result.ok=False
    "skip_for": set(),       # 命中 → news_count=0（manager 应归 SKIPPED）
    "exception_for": set(),  # 命中 → raise（worker.error 信号触发）
    "cancel_for": set(),     # 命中 → result.cancelled=True
}


def _fake_analyze(self, **kwargs):
    """快速 fake：sleep + 按 template_id 选定行为返回 AnalysisResult。

    注意：以实例方法形式 monkey-patch（self 为 AnalysisService），调用
    签名与原方法一致。
    """
    time.sleep(_fake_config["sleep"])
    tid = kwargs.get("template_id") or "default"
    if tid in _fake_config["exception_for"]:
        raise RuntimeError(f"mock exception: {tid}")
    if tid in _fake_config["cancel_for"]:
        return AnalysisResult(
            ok=False, cancelled=True, error="mock cancel",
        )
    if tid in _fake_config["fail_for"]:
        return AnalysisResult(
            ok=False, cancelled=False, error="mock fail",
        )
    is_skip = tid in _fake_config["skip_for"]
    return AnalysisResult(
        ok=True,
        cancelled=False,
        report_path="data/AI_analysis/fake.md",
        news_count=0 if is_skip else 100,
        time_range={"start": "2026-05-22 14:00", "end": "2026-05-23 09:00"},
        result_text="# fake report",
        theme_count=0 if is_skip else 5,
        theme_error=None,
    )


def _reset_fake():
    _fake_config["sleep"] = 0.3
    _fake_config["fail_for"] = set()
    _fake_config["skip_for"] = set()
    _fake_config["exception_for"] = set()
    _fake_config["cancel_for"] = set()


svc_module.AnalysisService.analyze = _fake_analyze  # type: ignore[assignment,method-assign]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_kwargs(template_id: str, *, extract_themes=False) -> dict:
    return {
        "template_id": template_id,
        "template_label": template_id,
        "provider": "deepseek",
        "sqlite_source": "curated",
        "sqlite_start": datetime(2026, 5, 22, 14, 0),
        "sqlite_end": datetime(2026, 5, 23, 9, 0),
        "market_summary": "fake summary",
        "extract_themes": extract_themes,
        "enable_deep_thinking": False,
    }


def _wait_until(
    cond_fn, *, timeout_s: float = 10.0, tick_s: float = 0.02,
) -> bool:
    """轮询 cond_fn 直到 True 或超时；每次 processEvents 推进 Qt 循环。"""
    app = QCoreApplication.instance()
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if app is not None:
            app.processEvents()
        if cond_fn():
            return True
        time.sleep(tick_s)
    return False


_pass = 0
_fail: List[str] = []


def _ok(case: str):
    global _pass
    _pass += 1
    print(f"  [OK] {case}")


def _err(case: str, msg: str):
    _fail.append(f"{case}: {msg}")
    print(f"  [FAIL] {case}: {msg}")


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def case_01_concurrency_limit():
    """5 个任务 enqueue，max=2，瞬时 RUNNING=2，PENDING=3。"""
    _reset_fake()
    _fake_config["sleep"] = 0.5
    mgr = AnalysisTaskManager(max_concurrent=2)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs(f"tpl_{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 2:
        _err("case_01", f"立即 RUNNING={len(running)}，期望 2")
        return
    if len(pending) != 3:
        _err("case_01", f"立即 PENDING={len(pending)}，期望 3")
        return

    ok = _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    if not ok:
        _err("case_01", "超时未全部完成")
        return
    if any(t.status != TaskStatus.SUCCESS for t in mgr.list_tasks()):
        bad = [
            (t.task_id, t.status) for t in mgr.list_tasks()
            if t.status != TaskStatus.SUCCESS
        ]
        _err("case_01", f"非全部 SUCCESS: {bad}")
        return
    _ok("case_01_concurrency_limit (5 任务 2 并发 FIFO)")


def case_02_default_concurrency_is_16():
    """构造时不传 max_concurrent → 默认应为 16（2026-05-28 23:00 hotfix）。

    20 任务、默认上限 16：立刻 16 RUNNING / 4 PENDING。
    """
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = AnalysisTaskManager()
    for i in range(20):
        mgr.enqueue(_make_kwargs(f"def_{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 16:
        _err("case_02", f"默认 RUNNING={len(running)}，期望 16")
        return
    if len(pending) != 4:
        _err("case_02", f"默认 PENDING={len(pending)}，期望 4")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    _ok("case_02_default_concurrency_is_16 (20 任务默认上限 16)")


def case_03_success_path():
    _reset_fake()
    _fake_config["sleep"] = 0.2
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("succ"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.SUCCESS:
        _err(
            "case_03",
            f"期望 SUCCESS，得到 {task.status if task else None}"
        )
        return
    if not task.result or not task.result.get("report_file"):
        _err("case_03", "result.report_file 缺失")
        return
    if not task.elapsed_ms:
        _err("case_03", "elapsed_ms 未填")
        return
    _ok("case_03_success_path")


def case_04_failed_task():
    _reset_fake()
    _fake_config["sleep"] = 0.2
    _fake_config["fail_for"] = {"tpl_fail"}
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_fail"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_04",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.error:
        _err("case_04", "task.error 为空")
        return
    _ok("case_04_failed_task (result.ok=False)")


def case_05_exception_task():
    """worker.run 内部抛异常 → worker.error 信号 → task.FAILED。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["exception_for"] = {"tpl_exc"}
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_exc"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_05",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.error or "分析失败" not in task.error:
        _err("case_05", f"error 内容不符: {(task.error or '')[:80]}")
        return
    _ok("case_05_exception_task (worker.run 抛异常)")


def case_06_skipped_task():
    """news_count=0 应归 SKIPPED。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["skip_for"] = {"tpl_skip"}
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_skip"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.SKIPPED:
        _err(
            "case_06",
            f"期望 SKIPPED，得到 {task.status if task else None}"
        )
        return
    _ok("case_06_skipped_task (news_count=0)")


def case_07_cancelled_task():
    """result.cancelled=True → FAILED + cancelled flag。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["cancel_for"] = {"tpl_cancel"}
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_cancel"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_07",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.cancelled:
        _err("case_07", "cancelled flag 未置 True")
        return
    _ok("case_07_cancelled_task")


def case_08_remove_completed():
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("rm_done"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    removed_signals: list[int] = []
    mgr.task_removed.connect(removed_signals.append)

    ok = mgr.remove(tid)
    if not ok:
        _err("case_08", "终态任务 remove 返回 False")
        return
    if mgr.get(tid) is not None:
        _err("case_08", "remove 后 get 仍能拿到")
        return
    if removed_signals != [tid]:
        _err("case_08", f"removed signal 未触发: {removed_signals}")
        return
    _ok("case_08_remove_completed")


def case_09_remove_running_rejected():
    _reset_fake()
    _fake_config["sleep"] = 1.0
    mgr = AnalysisTaskManager()
    tid = mgr.enqueue(_make_kwargs("rm_run"))

    _wait_until(
        lambda: mgr.get(tid) and mgr.get(tid).status == TaskStatus.RUNNING,
        timeout_s=2,
    )
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.RUNNING:
        _err("case_09", "任务未能进入 RUNNING")
        return

    ok = mgr.remove(tid)
    if ok:
        _err("case_09", "运行中任务 remove 居然返回 True")
        return
    if mgr.get(tid) is None:
        _err("case_09", "运行中任务被错误地从字典移除")
        return

    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_09_remove_running_rejected")


def case_10_shutdown_cancels_pending():
    _reset_fake()
    _fake_config["sleep"] = 1.0
    mgr = AnalysisTaskManager(max_concurrent=2)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs(f"sd_{i}"))

    mgr.shutdown()

    pending_after = [
        t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING
    ]
    failed_after = [
        t for t in mgr.list_tasks() if t.status == TaskStatus.FAILED
    ]
    if pending_after:
        _err(
            "case_10",
            f"shutdown 后仍有 {len(pending_after)} 个 PENDING"
        )
        return
    if len(failed_after) != 3:
        _err("case_10", f"FAILED={len(failed_after)}，期望 3")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=10)
    _ok("case_10_shutdown_cancels_pending")


def case_11_fifo_order():
    """连续 enqueue 4 个 max=3，第 4 PENDING，1 完成后 4 才上。"""
    _reset_fake()
    _fake_config["sleep"] = 0.3
    mgr = AnalysisTaskManager(max_concurrent=3)
    t1 = mgr.enqueue(_make_kwargs("fifo_1"))
    mgr.enqueue(_make_kwargs("fifo_2"))
    mgr.enqueue(_make_kwargs("fifo_3"))
    t4 = mgr.enqueue(_make_kwargs("fifo_4"))

    task4 = mgr.get(t4)
    if task4.status != TaskStatus.PENDING:
        _err("case_11", f"t4 初始状态={task4.status}，期望 PENDING")
        return

    _wait_until(
        lambda: mgr.get(t1).status == TaskStatus.SUCCESS,
        timeout_s=5,
    )
    _wait_until(
        lambda: mgr.get(t4).status != TaskStatus.PENDING,
        timeout_s=3,
    )
    if mgr.get(t4).status == TaskStatus.PENDING:
        _err("case_11", "t1 完成后 t4 仍未进 RUNNING")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_11_fifo_order")


def case_12_signals_emitted():
    """task_added / task_started / task_finished 都应 emit 一次。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = AnalysisTaskManager()

    sig_log: list[str] = []
    mgr.task_added.connect(lambda tid: sig_log.append(f"added({tid})"))
    mgr.task_started.connect(lambda tid: sig_log.append(f"started({tid})"))
    mgr.task_finished.connect(lambda tid: sig_log.append(f"finished({tid})"))

    tid = mgr.enqueue(_make_kwargs("sig"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    expected = [f"added({tid})", f"started({tid})", f"finished({tid})"]
    if sig_log != expected:
        _err("case_12", f"信号序列={sig_log}，期望={expected}")
        return
    _ok("case_12_signals_emitted")


def case_13_finished_emitted_once_on_fail():
    """失败路径：worker 同步 emit error+finished_result，task_finished 仅 1 次。

    这是 _emit_finished_once 幂等保护的核心回归。
    """
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["fail_for"] = {"dedup"}
    mgr = AnalysisTaskManager()

    finished_log: list[int] = []
    mgr.task_finished.connect(finished_log.append)

    tid = mgr.enqueue(_make_kwargs("dedup"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    if finished_log.count(tid) != 1:
        _err(
            "case_13",
            f"task_finished emit 次数={finished_log.count(tid)}，期望恰好 1"
        )
        return
    _ok("case_13_finished_emitted_once_on_fail")


def case_14_set_max_concurrent_grow():
    """运行时调大并发：5 任务 / 初始 max=2 → 调到 5 → 立即全部 RUNNING。"""
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = AnalysisTaskManager(max_concurrent=2)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs(f"grow_{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 2 or len(pending) != 3:
        _err(
            "case_14",
            f"初始 RUNNING={len(running)} PENDING={len(pending)}，期望 2/3"
        )
        return

    actual = mgr.set_max_concurrent(5)
    if actual != 5:
        _err("case_14", f"set_max_concurrent 返回 {actual}，期望 5")
        return
    QCoreApplication.instance().processEvents()
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 5 or pending:
        _err(
            "case_14",
            f"扩容后 RUNNING={len(running)} PENDING={len(pending)}，期望 5/0"
        )
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    _ok("case_14_set_max_concurrent_grow (2 → 5 立即拉满)")


def case_15_set_max_concurrent_shrink():
    """调小并发：不强杀 RUNNING；新 PENDING 不再被派给空位。"""
    _reset_fake()
    _fake_config["sleep"] = 0.5
    mgr = AnalysisTaskManager(max_concurrent=4)
    for i in range(1, 5):
        mgr.enqueue(_make_kwargs(f"shr_{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    if len(running) != 4:
        _err("case_15", f"初始 RUNNING={len(running)}，期望 4")
        return

    mgr.set_max_concurrent(1)
    mgr.enqueue(_make_kwargs("shr_extra1"))
    mgr.enqueue(_make_kwargs("shr_extra2"))
    QCoreApplication.instance().processEvents()
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 4:
        _err(
            "case_15",
            f"shrink 后 RUNNING={len(running)}，应保留所有原 RUNNING（=4）"
        )
        return
    if len(pending) != 2:
        _err("case_15", f"shrink 后 PENDING={len(pending)}，期望 2")
        return

    _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    _ok("case_15_set_max_concurrent_shrink (4 → 1 不杀 RUNNING)")


def case_16_set_max_concurrent_clamp():
    """超出 [1, HARD_CAP] 范围 → 自动 clamp，返回实际生效值。"""
    mgr = AnalysisTaskManager()
    cap = AnalysisTaskManager.MAX_CONCURRENT_HARD_CAP
    a1 = mgr.set_max_concurrent(0)
    a2 = mgr.set_max_concurrent(-100)
    a3 = mgr.set_max_concurrent(cap + 50)
    if a1 != 1:
        _err("case_16", f"clamp(0) → {a1}，期望 1")
        return
    if a2 != 1:
        _err("case_16", f"clamp(-100) → {a2}，期望 1")
        return
    if a3 != cap:
        _err("case_16", f"clamp(cap+50) → {a3}，期望 {cap}")
        return
    _ok(f"case_16_set_max_concurrent_clamp (clamp 到 [1, {cap}])")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        _ = QApplication.instance() or QApplication(sys.argv)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] QApplication 创建失败: {exc}")
        return 2

    cases = [
        case_01_concurrency_limit,
        case_02_default_concurrency_is_16,
        case_03_success_path,
        case_04_failed_task,
        case_05_exception_task,
        case_06_skipped_task,
        case_07_cancelled_task,
        case_08_remove_completed,
        case_09_remove_running_rejected,
        case_10_shutdown_cancels_pending,
        case_11_fifo_order,
        case_12_signals_emitted,
        case_13_finished_emitted_once_on_fail,
        case_14_set_max_concurrent_grow,
        case_15_set_max_concurrent_shrink,
        case_16_set_max_concurrent_clamp,
    ]
    print(f"[plan] {len(cases)} 用例")
    for fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            _err(fn.__name__, f"用例本身崩溃: {exc}")
            traceback.print_exc()

    total = _pass + len(_fail)
    print(
        f"\n[summary] {total} 用例: 通过 {_pass} / 失败 {len(_fail)}"
    )
    if _fail:
        for msg in _fail:
            print(f"  - {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
