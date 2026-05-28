"""回归测试：手动回测任务队列管理器（2026-05-27 多任务队列改造配套）。

测试范围
--------
* BacktestTaskManager 的队列调度（可配置并发上限 + FIFO）
* 默认并发数 = 8（hotfix 2026-05-27 21:30 从 3 提到 8）
* 任务状态机（PENDING → RUNNING → SUCCESS/SKIPPED/FAILED）
* 删除策略（终态可删；运行中拒绝）
* shutdown 取消 PENDING

测试策略
--------
**不调真的 backtest_one**：直接 monkey-patch
``tools.backtest_prompt.backtest_one`` 为 fake 实现（sleep 一会 + 返回
预设 BacktestResult）。worker 内部 ``from tools.backtest_prompt import
backtest_one`` 是模块属性查询，patch 模块属性即可生效。

用法
----
::

    python tools/test_manual_backtest_queue.py

退出码：
* 0 = 全部用例通过
* 1 = 任一用例失败
* 2 = 测试本身崩溃（import / QApplication 创建失败）
"""

from __future__ import annotations

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

# Qt headless 模式（CI / 服务器无显示）
import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import tools.backtest_prompt as bp_module  # noqa: E402
from tools.backtest_prompt import BacktestResult  # noqa: E402

from gui.workers.manual_backtest_manager import (  # noqa: E402
    BacktestTaskManager, TaskStatus,
)


# ---------------------------------------------------------------------------
# Fake backtest_one
# ---------------------------------------------------------------------------


# 全局可调控的 fake 行为
_fake_config = {
    "sleep": 0.3,          # 每次跑模拟耗时
    "fail_for": set(),     # 命中模板名/日期就抛异常
    "skip_for": set(),     # 命中就 skipped=True
    "exception_for": set(),  # 命中就 raise（让 worker.error 信号触发）
}


def fake_backtest_one(template_id, trade_date, **kwargs):
    """快速 fake：sleep + 按配置返回 BacktestResult / 抛异常。"""
    time.sleep(_fake_config["sleep"])
    key = f"{template_id}@{trade_date}"
    if key in _fake_config["exception_for"]:
        raise RuntimeError(f"mock exception: {key}")
    is_skip = key in _fake_config["skip_for"]
    is_fail = key in _fake_config["fail_for"]
    return BacktestResult(
        ok=not is_fail,
        template_id=template_id,
        trade_date=trade_date,
        news_start_iso="2026-05-22T14:00:00",
        news_end_iso="2026-05-23T09:00:00",
        news_status=kwargs.get("news_status") or "curated",
        snapshot_news_count=100 if not is_skip else 0,
        report_path="data/AI_analysis/fake.md" if not (
            is_fail or is_skip
        ) else None,
        themes_count=5 if not (is_fail or is_skip) else 0,
        elapsed_ms=int(_fake_config["sleep"] * 1000),
        error="mock skip reason" if is_skip else (
            "mock fail reason" if is_fail else None
        ),
        skipped=is_skip,
    )


def _reset_fake():
    _fake_config["sleep"] = 0.3
    _fake_config["fail_for"] = set()
    _fake_config["skip_for"] = set()
    _fake_config["exception_for"] = set()


# 安装 patch（worker 内部 from 是模块属性查询，改属性即生效）
bp_module.backtest_one = fake_backtest_one


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_kwargs(template_id: str, trade_date: str, dry_run=False) -> dict:
    return {
        "template_id": template_id,
        "template_label": template_id,
        "trade_date": trade_date,
        "news_start_dt": datetime(2026, 5, 22, 14, 0),
        "news_end_dt": datetime(2026, 5, 23, 9, 0),
        "news_status": "curated",
        "provider": None,
        "dry_run": dry_run,
        "overwrite": False,
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


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


_pass = 0
_fail: List[str] = []


def _ok(case: str):
    global _pass
    _pass += 1
    print(f"  [OK] {case}")


def _err(case: str, msg: str):
    _fail.append(f"{case}: {msg}")
    print(f"  [FAIL] {case}: {msg}")


def case_01_concurrency_limit():
    """5 个任务 enqueue，瞬时 RUNNING<=3，PENDING>=2（显式注入 max=3）。"""
    _reset_fake()
    _fake_config["sleep"] = 0.5
    mgr = BacktestTaskManager(max_concurrent=3)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs("tpl_a", f"2026052{i}"))

    # 立即检查：前 3 应 RUNNING，后 2 应 PENDING
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 3:
        _err("case_01", f"立即 RUNNING={len(running)}，期望 3")
        return
    if len(pending) != 2:
        _err("case_01", f"立即 PENDING={len(pending)}，期望 2")
        return

    # 等到全部完成
    ok = _wait_until(lambda: not mgr.has_active(), timeout_s=10)
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
    _ok("case_01_concurrency_limit (5 任务 3 并发 FIFO，显式注入)")


def case_11_default_concurrency_is_8():
    """构造时不传 max_concurrent → 默认应为 8（2026-05-27 21:30 hotfix）。

    实测：enqueue 10 个、sleep 长，应立刻 8 个 RUNNING / 2 个 PENDING。
    """
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = BacktestTaskManager()  # 不传，走默认
    for i in range(10):
        mgr.enqueue(_make_kwargs("tpl_def", f"2026060{i % 10}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 8:
        _err("case_11", f"默认 RUNNING={len(running)}，期望 8")
        return
    if len(pending) != 2:
        _err("case_11", f"默认 PENDING={len(pending)}，期望 2")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    _ok("case_11_default_concurrency_is_8 (10 任务默认上限 8)")


def case_02_success_path():
    _reset_fake()
    _fake_config["sleep"] = 0.2
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_b", "20260522"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None:
        _err("case_02", "task 丢失")
        return
    if task.status != TaskStatus.SUCCESS:
        _err("case_02", f"状态={task.status}")
        return
    if not task.result or not task.result.get("report_path"):
        _err("case_02", "result.report_path 缺失")
        return
    _ok("case_02_success_path")


def case_03_failed_task():
    _reset_fake()
    _fake_config["sleep"] = 0.2
    _fake_config["fail_for"] = {"tpl_c@20260522"}
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_c", "20260522"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_03",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.error:
        _err("case_03", "task.error 为空")
        return
    _ok("case_03_failed_task (ok=False)")


def case_04_exception_task():
    """worker.run 内部抛异常 → worker.error 信号 → task.FAILED。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["exception_for"] = {"tpl_d@20260522"}
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_d", "20260522"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_04",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.error or "manual backtest 线程异常" not in task.error:
        _err("case_04", f"error 内容不符: {task.error[:80]}")
        return
    _ok("case_04_exception_task (worker.run 抛异常)")


def case_05_skipped_task():
    _reset_fake()
    _fake_config["sleep"] = 0.1
    _fake_config["skip_for"] = {"tpl_e@20260522"}
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_e", "20260522"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.SKIPPED:
        _err(
            "case_05",
            f"期望 SKIPPED，得到 {task.status if task else None}"
        )
        return
    _ok("case_05_skipped_task")


def case_06_remove_completed():
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_f", "20260522"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    removed_signals: list[int] = []
    mgr.task_removed.connect(removed_signals.append)

    ok = mgr.remove(tid)
    if not ok:
        _err("case_06", "终态任务 remove 返回 False")
        return
    if mgr.get(tid) is not None:
        _err("case_06", "remove 后 get 仍能拿到")
        return
    if removed_signals != [tid]:
        _err("case_06", f"removed signal 未触发: {removed_signals}")
        return
    _ok("case_06_remove_completed")


def case_07_remove_running_rejected():
    _reset_fake()
    _fake_config["sleep"] = 1.0  # 故意拉长，确保 RUNNING 期间能测
    mgr = BacktestTaskManager()
    tid = mgr.enqueue(_make_kwargs("tpl_g", "20260522"))

    # 等到 RUNNING（应该立刻就是 RUNNING）
    _wait_until(
        lambda: mgr.get(tid) and mgr.get(tid).status == TaskStatus.RUNNING,
        timeout_s=2,
    )
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.RUNNING:
        _err("case_07", "任务未能进入 RUNNING")
        return

    ok = mgr.remove(tid)
    if ok:
        _err("case_07", "运行中任务 remove 居然返回 True")
        return
    if mgr.get(tid) is None:
        _err("case_07", "运行中任务被错误地从字典移除")
        return

    # 等它正常跑完，避免污染下一个 case
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_07_remove_running_rejected")


def case_08_shutdown_cancels_pending():
    _reset_fake()
    _fake_config["sleep"] = 1.0  # 让前 3 个不那么快完
    mgr = BacktestTaskManager(max_concurrent=3)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs("tpl_h", f"2026060{i}"))

    # 立即 shutdown：前 3 RUNNING 应保留，后 2 PENDING 应转 FAILED
    mgr.shutdown()

    pending_after = [
        t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING
    ]
    failed_after = [
        t for t in mgr.list_tasks() if t.status == TaskStatus.FAILED
    ]
    if pending_after:
        _err(
            "case_08",
            f"shutdown 后仍有 {len(pending_after)} 个 PENDING"
        )
        return
    if len(failed_after) != 2:
        _err(
            "case_08",
            f"FAILED={len(failed_after)}，期望 2"
        )
        return
    # 让 RUNNING 自然结束
    _wait_until(lambda: not mgr.has_active(), timeout_s=10)
    _ok("case_08_shutdown_cancels_pending")


def case_09_fifo_order():
    """连续 enqueue 4 个，前 3 应是 1/2/3，第 4 个排队，1 完成后 4 才上。"""
    _reset_fake()
    # 让任务 1 跑得最快，2/3 中等，4 不进 RUNNING 直到 1 完
    _fake_config["sleep"] = 0.3
    mgr = BacktestTaskManager(max_concurrent=3)
    t1 = mgr.enqueue(_make_kwargs("fifo", "20260601"))
    mgr.enqueue(_make_kwargs("fifo", "20260602"))
    mgr.enqueue(_make_kwargs("fifo", "20260603"))
    t4 = mgr.enqueue(_make_kwargs("fifo", "20260604"))

    # t4 此刻应是 PENDING
    task4 = mgr.get(t4)
    if task4.status != TaskStatus.PENDING:
        _err("case_09", f"t4 初始状态={task4.status}，期望 PENDING")
        return

    # 等 t1 完成 + 推 1 个事件循环，t4 应被 _pump 唤起到 RUNNING
    _wait_until(
        lambda: mgr.get(t1).status == TaskStatus.SUCCESS,
        timeout_s=5,
    )
    # 多 process 一次保证 _on_worker_qthread_done 已 dispatch
    _wait_until(
        lambda: mgr.get(t4).status != TaskStatus.PENDING,
        timeout_s=3,
    )
    if mgr.get(t4).status == TaskStatus.PENDING:
        _err("case_09", "t1 完成后 t4 仍未进 RUNNING")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_09_fifo_order")


def case_12_set_max_concurrent_grow():
    """运行时调大并发：5 任务 / 初始 max=2 → 调到 5 → 立即全部 RUNNING。"""
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = BacktestTaskManager(max_concurrent=2)
    for i in range(1, 6):
        mgr.enqueue(_make_kwargs("grow", f"2026070{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 2 or len(pending) != 3:
        _err(
            "case_12",
            f"初始 RUNNING={len(running)} PENDING={len(pending)}，期望 2/3"
        )
        return

    actual = mgr.set_max_concurrent(5)
    if actual != 5:
        _err("case_12", f"set_max_concurrent 返回 {actual}，期望 5")
        return
    QCoreApplication.instance().processEvents()
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 5 or pending:
        _err(
            "case_12",
            f"扩容后 RUNNING={len(running)} PENDING={len(pending)}，期望 5/0"
        )
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=10)
    _ok("case_12_set_max_concurrent_grow (2 → 5 立即拉满)")


def case_13_set_max_concurrent_shrink():
    """运行时调小并发：不强杀 RUNNING；新 PENDING 不再被派给空位。"""
    _reset_fake()
    _fake_config["sleep"] = 0.5
    mgr = BacktestTaskManager(max_concurrent=4)
    for i in range(1, 5):
        mgr.enqueue(_make_kwargs("shrink", f"2026080{i}"))

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    if len(running) != 4:
        _err("case_13", f"初始 RUNNING={len(running)}，期望 4")
        return

    mgr.set_max_concurrent(1)
    mgr.enqueue(_make_kwargs("shrink", "20260810"))
    mgr.enqueue(_make_kwargs("shrink", "20260811"))
    QCoreApplication.instance().processEvents()
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 4:
        _err(
            "case_13",
            f"shrink 后 RUNNING={len(running)}，应保留所有原 RUNNING（=4）"
        )
        return
    if len(pending) != 2:
        _err(
            "case_13",
            f"shrink 后 PENDING={len(pending)}，期望 2"
        )
        return

    _wait_until(lambda: not mgr.has_active(), timeout_s=15)
    _ok("case_13_set_max_concurrent_shrink (4 → 1 不杀 RUNNING)")


def case_14_set_max_concurrent_clamp():
    """超出 [1, HARD_CAP] 范围 → 自动 clamp，返回实际生效值。"""
    mgr = BacktestTaskManager()
    cap = BacktestTaskManager.MAX_CONCURRENT_HARD_CAP
    a1 = mgr.set_max_concurrent(0)
    a2 = mgr.set_max_concurrent(-100)
    a3 = mgr.set_max_concurrent(cap + 50)
    if a1 != 1:
        _err("case_14", f"clamp(0) → {a1}，期望 1")
        return
    if a2 != 1:
        _err("case_14", f"clamp(-100) → {a2}，期望 1")
        return
    if a3 != cap:
        _err("case_14", f"clamp(cap+50) → {a3}，期望 {cap}")
        return
    _ok(f"case_14_set_max_concurrent_clamp (clamp 到 [1, {cap}])")


def case_10_signals_emitted():
    """串一遍：task_added / task_started / task_finished 都得 emit。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = BacktestTaskManager()

    sig_log: list[str] = []
    mgr.task_added.connect(lambda tid: sig_log.append(f"added({tid})"))
    mgr.task_started.connect(lambda tid: sig_log.append(f"started({tid})"))
    mgr.task_finished.connect(lambda tid: sig_log.append(f"finished({tid})"))

    tid = mgr.enqueue(_make_kwargs("sig", "20260601"))
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    expected = [f"added({tid})", f"started({tid})", f"finished({tid})"]
    if sig_log != expected:
        _err("case_10", f"信号序列={sig_log}，期望={expected}")
        return
    _ok("case_10_signals_emitted")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        # 必须持有引用让 QApplication 不被 GC，但本函数不直接用 app 变量
        _ = QApplication.instance() or QApplication(sys.argv)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] QApplication 创建失败: {exc}")
        return 2

    cases = [
        case_01_concurrency_limit,
        case_02_success_path,
        case_03_failed_task,
        case_04_exception_task,
        case_05_skipped_task,
        case_06_remove_completed,
        case_07_remove_running_rejected,
        case_08_shutdown_cancels_pending,
        case_09_fifo_order,
        case_10_signals_emitted,
        case_11_default_concurrency_is_8,
        case_12_set_max_concurrent_grow,
        case_13_set_max_concurrent_shrink,
        case_14_set_max_concurrent_clamp,
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
        for line in _fail:
            print(f"  ✗ {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
