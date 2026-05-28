"""回归测试：题材抽取任务队列管理器（2026-05-28 评估页提取与队列化改造配套）。

测试范围
--------
* ``ThemeExtractTaskManager`` 的队列调度（可配置并发上限 + FIFO）
* 默认并发数 = 16
* 任务状态机（PENDING → RUNNING → SUCCESS / FAILED）
* 删除策略（终态可删；运行中拒绝）
* shutdown 取消 PENDING
* ``has_active_for_path``（评估页批量删除前置检查用）

测试策略
--------
**不调真的 LLM**：直接 monkey-patch
``core.theme_extractor.ThemeExtractor`` / ``parse_report_meta`` /
``services.storage.get_theme_store`` 为 fake 实现（sleep + 返回预设 dict）。
worker 内部走 ``from core.theme_extractor import ThemeExtractor`` 是模块
属性查询，patch 模块属性即生效。

用法
----
::

    python tools/test_theme_extract_queue.py

退出码：
* 0 = 全部用例通过
* 1 = 任一用例失败
* 2 = 测试本身崩溃（import / QApplication 创建失败）
"""

from __future__ import annotations

import sys
import time
import traceback
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

import core.theme_extractor as te_module  # noqa: E402
import services.storage as ss_module  # noqa: E402

from gui.workers.theme_extract_manager import (  # noqa: E402
    ThemeExtractTaskManager, TaskStatus,
)


# ---------------------------------------------------------------------------
# Fake ThemeExtractor + ThemeStore
# ---------------------------------------------------------------------------


# 全局可调控的 fake 行为
_fake_config = {
    "sleep": 0.3,
    "fail_for": set(),       # 命中路径就 ok=False（business fail）
    "exception_for": set(),  # 命中路径就 raise（worker.error 路径）
}


class _FakeThemeExtractor:
    """模拟 ThemeExtractor：构造时接 provider/model，extract_from_file
    返回 (themes, news_id_map, err)。"""

    def __init__(self, provider=None, model=None):
        self.provider = provider or "fake-provider"
        self.model = model or "fake-model"

    def extract_from_file(self, report_path, progress_callback=None):
        time.sleep(_fake_config["sleep"])
        if report_path in _fake_config["exception_for"]:
            raise RuntimeError(f"mock exception: {report_path}")
        if report_path in _fake_config["fail_for"]:
            return [], {}, "mock fail reason"
        # 推一条流式 chunk + 一条阶段进度，验证 progress_callback 通路
        if progress_callback is not None:
            try:
                progress_callback("mock chunk", is_streaming=True)
                progress_callback("阶段提示", is_streaming=False)
            except Exception:
                pass
        themes = [{"name": "题材A"}]
        news_id_map = {1: 100}
        return themes, news_id_map, None


def _fake_parse_report_meta(report_path):
    """模拟 parse_report_meta：返回 dict（report_id 是 .md basename）。"""
    bn = os.path.basename(report_path)
    return {
        "report_id": os.path.splitext(bn)[0],
        "report_date": "20260528",
        "report_time": "1500",
    }


class _FakeThemeStore:
    def save_themes(self, meta, themes, news_id_map=None):
        return {
            "themes": len(themes),
            "stocks": 3,
            "news": len(news_id_map or {}),
        }


def _fake_get_theme_store():
    return _FakeThemeStore()


def _reset_fake():
    _fake_config["sleep"] = 0.3
    _fake_config["fail_for"] = set()
    _fake_config["exception_for"] = set()


# 安装 patch（worker 内部 from 是模块属性查询，改属性即生效）
te_module.ThemeExtractor = _FakeThemeExtractor
te_module.parse_report_meta = _fake_parse_report_meta
ss_module.get_theme_store = _fake_get_theme_store


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _wait_until(
    cond_fn, *, timeout_s: float = 10.0, tick_s: float = 0.02,
) -> bool:
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
    """5 个任务 enqueue，瞬时 RUNNING<=3，PENDING>=2（显式注入 max=3）。"""
    _reset_fake()
    _fake_config["sleep"] = 0.5
    mgr = ThemeExtractTaskManager(max_concurrent=3)
    for i in range(1, 6):
        mgr.enqueue(report_path=f"/tmp/fake_{i}.md")

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 3:
        _err("case_01", f"立即 RUNNING={len(running)}，期望 3")
        return
    if len(pending) != 2:
        _err("case_01", f"立即 PENDING={len(pending)}，期望 2")
        return

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
    _ok("case_01_concurrency_limit (5 任务 3 并发，显式注入)")


def case_02_default_concurrency_is_16():
    """构造时不传 max_concurrent → 默认应为 16。"""
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = ThemeExtractTaskManager()
    for i in range(20):
        mgr.enqueue(report_path=f"/tmp/def_{i}.md")

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 16:
        _err("case_02", f"默认 RUNNING={len(running)}，期望 16")
        return
    if len(pending) != 4:
        _err("case_02", f"默认 PENDING={len(pending)}，期望 4")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=20)
    _ok("case_02_default_concurrency_is_16 (20 任务默认上限 16)")


def case_03_success_path():
    _reset_fake()
    _fake_config["sleep"] = 0.2
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(report_path="/tmp/success.md")
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None:
        _err("case_03", "task 丢失")
        return
    if task.status != TaskStatus.SUCCESS:
        _err("case_03", f"状态={task.status}")
        return
    if not task.result or task.result.get("themes") != 1:
        _err("case_03", f"result 字段不符: {task.result}")
        return
    _ok("case_03_success_path")


def case_04_failed_path():
    """extract_from_file 返回 (None, {}, err) → ok=False → FAILED。"""
    _reset_fake()
    _fake_config["sleep"] = 0.2
    target = "/tmp/fail.md"
    _fake_config["fail_for"] = {target}
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(report_path=target)
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
    _ok("case_04_failed_path (extract_from_file 业务错)")


def case_05_exception_path():
    """worker.run 内部抛异常 → worker.error 信号 → FAILED。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    target = "/tmp/exc.md"
    _fake_config["exception_for"] = {target}
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(report_path=target)
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    task = mgr.get(tid)
    if task is None or task.status != TaskStatus.FAILED:
        _err(
            "case_05",
            f"期望 FAILED，得到 {task.status if task else None}"
        )
        return
    if not task.error or "题材抽取失败" not in task.error:
        _err("case_05", f"error 内容不符: {task.error[:80]}")
        return
    _ok("case_05_exception_path (worker.run 抛异常)")


def case_06_remove_completed():
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(report_path="/tmp/rm_completed.md")
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
    _fake_config["sleep"] = 1.0
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(report_path="/tmp/rm_running.md")

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

    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_07_remove_running_rejected")


def case_08_shutdown_cancels_pending():
    _reset_fake()
    _fake_config["sleep"] = 1.0
    mgr = ThemeExtractTaskManager(max_concurrent=3)
    for i in range(1, 6):
        mgr.enqueue(report_path=f"/tmp/shutdown_{i}.md")

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
    _wait_until(lambda: not mgr.has_active(), timeout_s=10)
    _ok("case_08_shutdown_cancels_pending")


def case_09_fifo_order():
    """连续 enqueue 4 个，前 3 RUNNING，第 4 个排队，1 完成后 4 才上。"""
    _reset_fake()
    _fake_config["sleep"] = 0.3
    mgr = ThemeExtractTaskManager(max_concurrent=3)
    t1 = mgr.enqueue(report_path="/tmp/fifo_1.md")
    mgr.enqueue(report_path="/tmp/fifo_2.md")
    mgr.enqueue(report_path="/tmp/fifo_3.md")
    t4 = mgr.enqueue(report_path="/tmp/fifo_4.md")

    task4 = mgr.get(t4)
    if task4.status != TaskStatus.PENDING:
        _err("case_09", f"t4 初始状态={task4.status}，期望 PENDING")
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
        _err("case_09", "t1 完成后 t4 仍未进 RUNNING")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    _ok("case_09_fifo_order")


def case_10_signals_emitted():
    """串一遍：task_added / task_started / task_finished 都 emit。"""
    _reset_fake()
    _fake_config["sleep"] = 0.1
    mgr = ThemeExtractTaskManager()

    sig_log: list[str] = []
    mgr.task_added.connect(lambda tid: sig_log.append(f"added({tid})"))
    mgr.task_started.connect(lambda tid: sig_log.append(f"started({tid})"))
    mgr.task_finished.connect(lambda tid: sig_log.append(f"finished({tid})"))

    tid = mgr.enqueue(report_path="/tmp/signals.md")
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)

    expected = [f"added({tid})", f"started({tid})", f"finished({tid})"]
    if sig_log != expected:
        _err("case_10", f"信号序列={sig_log}，期望={expected}")
        return
    _ok("case_10_signals_emitted")


def case_11_set_max_concurrent_grow():
    """运行时调大并发：5 任务 / 初始 max=2 → 调到 5 → 立即全部 RUNNING。"""
    _reset_fake()
    _fake_config["sleep"] = 0.6
    mgr = ThemeExtractTaskManager(max_concurrent=2)
    for i in range(1, 6):
        mgr.enqueue(report_path=f"/tmp/grow_{i}.md")

    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 2 or len(pending) != 3:
        _err(
            "case_11",
            f"初始 RUNNING={len(running)} PENDING={len(pending)}，期望 2/3"
        )
        return

    actual = mgr.set_max_concurrent(5)
    if actual != 5:
        _err("case_11", f"set_max_concurrent 返回 {actual}，期望 5")
        return
    QCoreApplication.instance().processEvents()
    running = [t for t in mgr.list_tasks() if t.status == TaskStatus.RUNNING]
    pending = [t for t in mgr.list_tasks() if t.status == TaskStatus.PENDING]
    if len(running) != 5 or pending:
        _err(
            "case_11",
            f"扩容后 RUNNING={len(running)} PENDING={len(pending)}，期望 5/0"
        )
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=10)
    _ok("case_11_set_max_concurrent_grow (2 → 5 立即拉满)")


def case_12_set_max_concurrent_clamp():
    """超出 [1, HARD_CAP] 范围 → 自动 clamp。"""
    mgr = ThemeExtractTaskManager()
    cap = ThemeExtractTaskManager.MAX_CONCURRENT_HARD_CAP
    a1 = mgr.set_max_concurrent(0)
    a2 = mgr.set_max_concurrent(-100)
    a3 = mgr.set_max_concurrent(cap + 50)
    if a1 != 1:
        _err("case_12", f"clamp(0) → {a1}，期望 1")
        return
    if a2 != 1:
        _err("case_12", f"clamp(-100) → {a2}，期望 1")
        return
    if a3 != cap:
        _err("case_12", f"clamp(cap+50) → {a3}，期望 {cap}")
        return
    _ok(f"case_12_set_max_concurrent_clamp (clamp 到 [1, {cap}])")


def case_13_has_active_for_path():
    """has_active_for_path 在 PENDING / RUNNING 时返回 True；终态返回 False。

    评估页「批量删除」前置检查依赖此方法——选中正在抽题材的报告就拦截。
    """
    _reset_fake()
    _fake_config["sleep"] = 0.6
    target = "/tmp/has_active.md"
    mgr = ThemeExtractTaskManager(max_concurrent=1)
    tid = mgr.enqueue(report_path=target)
    # 立即检查：RUNNING 应 has_active=True
    _wait_until(
        lambda: mgr.get(tid).status == TaskStatus.RUNNING,
        timeout_s=2,
    )
    if not mgr.has_active_for_path(target):
        _err("case_13", "RUNNING 期间 has_active_for_path=False")
        return
    if mgr.has_active_for_path("/tmp/other.md"):
        _err("case_13", "无关路径 has_active_for_path=True")
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=5)
    if mgr.has_active_for_path(target):
        _err("case_13", "终态后 has_active_for_path 仍 True")
        return
    _ok("case_13_has_active_for_path")


def case_14_basename_filled():
    """ThemeExtractTask.report_basename 应自动从 report_path 取 basename。"""
    _reset_fake()
    _fake_config["sleep"] = 0.05
    mgr = ThemeExtractTaskManager()
    tid = mgr.enqueue(
        report_path="/some/dir/with/spaces/5月22日_盘后总结_aggressive.md"
    )
    task = mgr.get(tid)
    if task.report_basename != "5月22日_盘后总结_aggressive.md":
        _err(
            "case_14",
            f"basename={task.report_basename}，期望 .md 文件名"
        )
        return
    _wait_until(lambda: not mgr.has_active(), timeout_s=3)
    _ok("case_14_basename_filled")


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
        case_04_failed_path,
        case_05_exception_path,
        case_06_remove_completed,
        case_07_remove_running_rejected,
        case_08_shutdown_cancels_pending,
        case_09_fifo_order,
        case_10_signals_emitted,
        case_11_set_max_concurrent_grow,
        case_12_set_max_concurrent_clamp,
        case_13_has_active_for_path,
        case_14_basename_filled,
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
