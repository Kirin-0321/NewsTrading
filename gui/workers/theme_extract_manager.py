"""题材抽取·任务队列管理器（2026-05-28 评估页提取与队列化改造）。

业务定位
--------
让评估页的「🎯 提取题材 / ⚡ 全部提取」与预测题材页的「🚀 开始抽取并入库」
共享同一条任务队列：连点 N 次排队、后台并发跑、5 态机透明。

设计原则
--------
* 与 :mod:`gui.workers.manual_backtest_manager` 完全同款的 5 态机 + 并发池 +
  跨线程 relay 信号机制，纯通用部分照抄；任务字段独立（``ThemeExtractTask``
  与 ``BacktestTask`` 结构差异大，强行抽公共基类反而模糊语义）。
* **GUI 专属交互层**：本模块不复刻 ``ThemeExtractor`` / ``ThemeStore`` 任何
  业务逻辑（核心抽取算法 / 落库语义都在 ``core.theme_extractor`` 和
  ``services.storage.ThemeStore``），只编排 worker 调度。
* **跨页单例**：评估页 + 预测题材页都通过 ``MainWindow.theme_extract_manager``
  访问同一实例（评估页提取后 toast 不切 Tab，主人自行切到预测题材页看进度）。
* **无持久化**：所有任务对象都在内存，关 GUI 自动清空。

并发模型
--------
::

    page._on_extract_clicked / 评估页._on_extract_one / 评估页._on_extract_all
       └── manager.enqueue(report_path, provider, model)   →  task_id
              └── _pump()
                     ├── 若 RUNNING < concurrency → 立即起 ThemeExtractWorker
                     └── 否则 PENDING 排队
                            └── 任意 worker finish → _pump 再次尝试

跨线程信号
----------
``ThemeExtractWorker`` 在自己的 ``QThread`` 跑，其 4 个信号
（``progress / streaming / finished_payload / error``）首位都是
``task_id``；为避免直接 lambda 在子线程改 manager 内部状态触发 race，
在 manager 里加一层 ``_relay_*`` 内部信号——子线程 emit ``_relay_*``，
``AutoConnection`` 检测到跨线程 → ``QueuedConnection`` → manager 槽函数
在主线程串行执行，所有状态修改严格主线程（与手动回测同款架构）。

调用链
------
::

    PromptEvalPage / ThemePredictionPage
       ├── manager = self.window().theme_extract_manager
       ├── manager.enqueue(report_path=..., provider=...) → task_added(tid)
       ├── manager.remove(tid)                            → task_removed(tid)
       └── 监听 5 个对外 signal 刷 UI

任务删除规则
------------
* status == RUNNING → 拒绝删除（与手动回测同款，避免半完成态污染）
* 其它状态 → 仅从内存 dict 删行；**不删 .md 文件 / 不动 ai_inference.db
  的 theme_predictions 等行**（产物清理是评估页·删除按钮的职责，本模块
  严格不越界）

并发参数
--------
* 默认并发 = 16（题材抽取每次 1 LLM 调用 ~30s~3min，DeepSeek V4 Pro
  60+ req/min 阈值下 16 并发安全）
* 硬上限 = 64（与手动回测同款）
* 运行时可调（spin 控件）：调大立即 _pump 拉 PENDING 起来，调小不强杀
  RUNNING 但停止派新单
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from gui.workers.theme_extract_worker import ThemeExtractWorker


# ---------------------------------------------------------------------------
# 状态机（与手动回测同款，UI 复用同一套 emoji + 颜色）
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """任务 5 态机。继承 str 让 dataclass json 序列化（如果将来要存）友好。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


STATUS_LABEL: Dict[TaskStatus, str] = {
    TaskStatus.PENDING: "⌛ 排队",
    TaskStatus.RUNNING: "⏳ 跑中",
    TaskStatus.SUCCESS: "✅ 完成",
    TaskStatus.SKIPPED: "⏹ 跳过",
    TaskStatus.FAILED: "❌ 失败",
}

STATUS_COLOR: Dict[TaskStatus, str] = {
    TaskStatus.PENDING: "#8c8c8c",
    TaskStatus.RUNNING: "#1890ff",
    TaskStatus.SUCCESS: "#52c41a",
    TaskStatus.SKIPPED: "#faad14",
    TaskStatus.FAILED: "#ff4d4f",
}


# ---------------------------------------------------------------------------
# 任务对象
# ---------------------------------------------------------------------------


@dataclass
class ThemeExtractTask:
    """单个题材抽取任务的内存对象。

    所有字段 public 直接读：本对象是 GUI 内部 dataclass，被 manager / page
    直接访问。
    """

    task_id: int
    report_path: str               # 报告 .md 绝对路径
    report_basename: str           # 仅文件名（任务表展示用）
    provider: Optional[str] = None
    model: Optional[str] = None

    status: TaskStatus = TaskStatus.PENDING
    submit_ts: float = field(default_factory=time.time)
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    elapsed_ms: Optional[int] = None

    # 跑完后回填（成功时）
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    # 内存缓冲：所有 chunk / log 都先存这里，UI 切换任务时整段回灌
    stream_buffer: List[str] = field(default_factory=list)
    log_buffer: List[str] = field(default_factory=list)

    # worker 强引用，避免 QThread 被 Python GC 提前回收；跑完置 None
    worker: Optional[ThemeExtractWorker] = None

    @property
    def status_label(self) -> str:
        return STATUS_LABEL[self.status]

    @property
    def status_color(self) -> str:
        return STATUS_COLOR[self.status]

    @property
    def is_terminal(self) -> bool:
        """是否处于终态（不再变化）。"""
        return self.status in (
            TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.FAILED,
        )

    def elapsed_text(self) -> str:
        """用时列显示文本。

        - PENDING: "-"
        - RUNNING: 实时秒数（由 page 的 QTimer 1s 刷新）
        - 终态: 定格的 elapsed_ms 转秒
        """
        if self.status == TaskStatus.PENDING:
            return "-"
        if self.status == TaskStatus.RUNNING and self.start_ts:
            return f"{time.time() - self.start_ts:.1f}s"
        if self.elapsed_ms is not None:
            return f"{self.elapsed_ms / 1000:.1f}s"
        return "-"


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class ThemeExtractTaskManager(QObject):
    """题材抽取任务队列管理器。

    跨页单例：由 ``MainWindow.__init__`` 持有；评估页与预测题材页通过
    ``self.window().theme_extract_manager`` 共享同一实例。

    Signals:
        task_added(int):    新任务进队列（PENDING 或直接 RUNNING）
        task_started(int):  任务从 PENDING 切到 RUNNING
        task_progress(int, str): 阶段日志（worker.progress 转发）
        task_streaming(int, str): LLM chunk（worker.streaming 转发）
        task_finished(int): 任务终态（SUCCESS/SKIPPED/FAILED 任一）
        task_removed(int):  任务被主人删除（仅 GUI 行消失）
    """

    # 对外信号（page 监听）
    task_added = pyqtSignal(int)
    task_started = pyqtSignal(int)
    task_progress = pyqtSignal(int, str)
    task_streaming = pyqtSignal(int, str)
    task_finished = pyqtSignal(int)
    task_removed = pyqtSignal(int)

    # 内部 relay 信号（worker 子线程 → manager 主线程槽）
    _relay_progress = pyqtSignal(int, str)
    _relay_streaming = pyqtSignal(int, str)
    _relay_finished_payload = pyqtSignal(int, dict)
    _relay_error = pyqtSignal(int, str)
    _relay_qthread_done = pyqtSignal(int)

    # 默认并发 16（手动回测默认 8；题材抽取 1 LLM 调用单任务 ~30s~3min，
    # 16 并发对应 ~32 req/min，DeepSeek V4 Pro 阈值内）
    _DEFAULT_MAX_CONCURRENT: int = 16
    # 硬上限 64（与手动回测同款）
    MAX_CONCURRENT_HARD_CAP: int = 64

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        max_concurrent: Optional[int] = None,
    ):
        super().__init__(parent)
        self._max_concurrent: int = (
            max_concurrent
            if max_concurrent is not None
            else self._DEFAULT_MAX_CONCURRENT
        )
        self._tasks: "OrderedDict[int, ThemeExtractTask]" = OrderedDict()
        self._pending: "deque[int]" = deque()
        self._running: set[int] = set()
        self._next_id: int = 1

        self._relay_progress.connect(self._on_worker_progress)
        self._relay_streaming.connect(self._on_worker_streaming)
        self._relay_finished_payload.connect(self._on_worker_finished)
        self._relay_error.connect(self._on_worker_error)
        self._relay_qthread_done.connect(self._on_worker_qthread_done)

    # =====================================================================
    # 对外 API
    # =====================================================================

    def enqueue(
        self,
        *,
        report_path: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> int:
        """加一个题材抽取任务到队列。

        Args:
            report_path: 报告 .md 绝对路径（必须存在）
            provider: LLM provider，None=用配置默认
            model: 模型名，None=用配置默认

        Returns:
            分配的 task_id（进程内自增，从 1 开始）

        副作用:
            * emit task_added(tid)
            * 调 _pump() 立即尝试调度（队列有空位则直接 RUNNING）
        """
        tid = self._next_id
        self._next_id += 1

        task = ThemeExtractTask(
            task_id=tid,
            report_path=report_path,
            report_basename=os.path.basename(report_path),
            provider=provider,
            model=model,
        )
        self._tasks[tid] = task
        self._pending.append(tid)
        self.task_added.emit(tid)
        self._pump()
        return tid

    def remove(self, task_id: int) -> bool:
        """从队列删除一个任务。

        Args:
            task_id: 任务 id

        Returns:
            * True: 删除成功
            * False: 任务不存在 / 任务正在跑（拒绝删）

        重要约定：
            本方法**仅删 GUI 内存对象**，绝不动 .md 文件 / ai_inference.db
            行。产物清理是「评估页·删除报告」按钮的职责，本模块严格不越界。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.RUNNING:
            return False
        if task.status == TaskStatus.PENDING:
            try:
                self._pending.remove(task_id)
            except ValueError:
                pass
        self._tasks.pop(task_id, None)
        self.task_removed.emit(task_id)
        return True

    def get(self, task_id: int) -> Optional[ThemeExtractTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[ThemeExtractTask]:
        """按 submit_ts 升序返回所有任务（OrderedDict 天然保序）。"""
        return list(self._tasks.values())

    def has_active(self) -> bool:
        """是否还有未到终态的任务（PENDING + RUNNING）。"""
        return any(not t.is_terminal for t in self._tasks.values())

    def has_active_for_path(self, report_path: str) -> bool:
        """该 .md 路径是否有 PENDING/RUNNING 任务。

        评估页「批量删除」前置检查用：避免删一份正在抽题材的报告造成
        worker 跑完时 INSERT 失败。
        """
        target = os.path.abspath(report_path)
        for t in self._tasks.values():
            if t.is_terminal:
                continue
            if os.path.abspath(t.report_path) == target:
                return True
        return False

    def get_max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, n: int) -> int:
        """运行时调整并发上限。

        Args:
            n: 期望并发数（会被 clamp 到 [1, MAX_CONCURRENT_HARD_CAP]）

        Returns:
            实际生效的并发数
        """
        n = max(1, min(int(n), self.MAX_CONCURRENT_HARD_CAP))
        self._max_concurrent = n
        self._pump()
        return n

    def shutdown(self) -> None:
        """关 GUI 时调用：取消所有 PENDING，让 RUNNING 自然结束。"""
        for tid in list(self._pending):
            task = self._tasks.get(tid)
            if task is not None:
                task.status = TaskStatus.FAILED
                task.error = "GUI 关闭：任务被取消"
                task.end_ts = time.time()
                task.elapsed_ms = 0
                self.task_finished.emit(tid)
        self._pending.clear()

    # =====================================================================
    # 内部调度
    # =====================================================================

    def _pump(self) -> None:
        """调度循环：把 PENDING 推到 RUNNING 直到并发上限。"""
        while (
            len(self._running) < self._max_concurrent
            and self._pending
        ):
            tid = self._pending.popleft()
            task = self._tasks.get(tid)
            if task is None:
                continue
            self._start_worker(task)

    def _start_worker(self, task: ThemeExtractTask) -> None:
        """实际起一个 ThemeExtractWorker 跑 task。"""
        task.status = TaskStatus.RUNNING
        task.start_ts = time.time()
        self._running.add(task.task_id)

        worker = ThemeExtractWorker(
            task_id=task.task_id,
            report_path=task.report_path,
            provider=task.provider,
            model=task.model,
        )
        task.worker = worker

        tid = task.task_id
        worker.progress.connect(
            lambda _t, msg: self._relay_progress.emit(_t, msg)
        )
        worker.streaming.connect(
            lambda _t, chunk: self._relay_streaming.emit(_t, chunk)
        )
        worker.finished_payload.connect(
            lambda _t, res: self._relay_finished_payload.emit(_t, res)
        )
        worker.error.connect(
            lambda _t, err: self._relay_error.emit(_t, err)
        )
        worker.finished.connect(
            lambda _t=tid: self._relay_qthread_done.emit(_t)
        )

        self.task_started.emit(task.task_id)
        worker.start()

    # =====================================================================
    # 信号转发槽（worker → manager → page）
    # =====================================================================

    def _on_worker_progress(self, tid: int, msg: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        task.log_buffer.append(line)
        self.task_progress.emit(tid, line)

    def _on_worker_streaming(self, tid: int, chunk: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        task.stream_buffer.append(chunk)
        self.task_streaming.emit(tid, chunk)

    def _on_worker_finished(self, tid: int, res: dict) -> None:
        """worker 业务结束（成功）。

        题材抽取没有 SKIPPED 语义（不像回测有 overwrite=False 跳过），
        若 ``res['ok']==True`` 即视为 SUCCESS；若 ok=False 视为 FAILED。
        """
        task = self._tasks.get(tid)
        if task is None:
            return
        task.result = res
        if res.get("ok"):
            task.status = TaskStatus.SUCCESS
        else:
            task.status = TaskStatus.FAILED
            task.error = res.get("error") or res.get("warning")
        task.end_ts = time.time()
        if task.start_ts:
            task.elapsed_ms = int((task.end_ts - task.start_ts) * 1000)
        self.task_finished.emit(tid)

    def _on_worker_error(self, tid: int, err: str) -> None:
        """worker 线程级异常。"""
        task = self._tasks.get(tid)
        if task is None:
            return
        task.status = TaskStatus.FAILED
        task.error = err
        task.end_ts = time.time()
        if task.start_ts:
            task.elapsed_ms = int((task.end_ts - task.start_ts) * 1000)
        self.task_finished.emit(tid)

    def _on_worker_qthread_done(self, tid: int) -> None:
        """QThread 自身结束（业务 finished 之后晚一拍触发）。

        清理 worker 强引用 + 从 _running 移除 + _pump 拉下一个。
        """
        self._running.discard(tid)
        task = self._tasks.get(tid)
        if task is not None:
            task.worker = None
        self._pump()
