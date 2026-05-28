"""手动回测·任务队列管理器（2026-05-27 多任务并行改造）。

业务定位
--------
让手动回测页支持「连点 N 次跑按钮排 N 个任务进队列、后台同时跑 3 个、
点哪个任务下面流式/md/题材三块联动切换显示」。

设计原则
--------
* **GUI 专属交互层**：本模块只编排 ManualBacktestWorker 调度，不复刻
  ``backtest_one`` 任何业务逻辑（CLI ``tools/backtest_prompt.py --workers``
  早已具备等价并发能力，本模块对它的依赖通过 worker 间接发生）
* **单一职责**：队列 + 调度 + 信号转发，三件事
* **状态机透明**：5 态 Enum + 每态 emoji+中文 label，UI 直接读
* **无持久化**：所有任务对象都在内存，关 GUI 自动清空

并发模型
--------
::

    page._on_run_clicked
       └── manager.enqueue(task_kwargs)   →  task_id
              └── _pump()
                     ├── 若 RUNNING < 3 → 立即起 ManualBacktestWorker
                     └── 否则 PENDING 排队
                            └── 任意 worker finish → _pump 再次尝试

跨线程信号
----------
ManualBacktestWorker 在自己的 QThread 跑，emit 信号默认走 Qt
``QueuedConnection``（跨线程安全）。manager 的槽函数在主线程
（GUI 线程）执行，再用 ``task_id`` 在 ``_tasks`` dict 里反查任务对象，
更新 ``stream_buffer / log_buffer`` 后转发出去。

调用链
------
::

    ManualBacktestPage
       ├── manager.enqueue(...)     →  task_added(tid)
       ├── manager.remove(tid)      →  task_removed(tid)
       └── 监听 5 个 signal 刷 UI

任务删除规则（来自内容文档 §三·B）
----------------------------------
* status==RUNNING → 拒绝删除，返回 False
* 其它状态 → 仅从内存 dict 删行；**不删 md 文件 / 不删 ai_inference.db
  行**（那是「评估页·单报告删除」按钮的职责，本模块严格不越界）
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from gui.workers.manual_backtest_worker import ManualBacktestWorker


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """任务 5 态机。继承 str 让 dataclass json 序列化（如果将来要存）友好。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


# UI 渲染映射（label + 颜色）。常量集中在此，UI 层不重复定义。
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
class BacktestTask:
    """单个手动回测任务的内存对象。

    所有字段 public 直接读：本对象是 GUI 内部 dataclass，被 manager / page
    直接访问，做无谓封装属于辉夜强迫症的边界外。
    """

    task_id: int
    template_id: str
    template_label: str           # 展示用（去掉 [key] 后缀）
    trade_date: str               # YYYYMMDD
    news_start_dt: datetime
    news_end_dt: datetime
    news_status: str
    provider: Optional[str]
    dry_run: bool
    overwrite: bool = False
    status: TaskStatus = TaskStatus.PENDING
    submit_ts: float = field(default_factory=time.time)
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    elapsed_ms: Optional[int] = None
    # 内存缓冲：所有 chunk / log 都先存这里，UI 切换任务时整段回灌
    stream_buffer: List[str] = field(default_factory=list)
    log_buffer: List[str] = field(default_factory=list)
    # 启发式：已插过"题材抽取阶段"分隔栏（沿用 page 旧逻辑）
    theme_section_marked: bool = False
    # 任务跑完后 finished_result 信号原样存放
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # worker 强引用，避免 QThread 被 Python GC 提前回收；跑完置 None
    worker: Optional[ManualBacktestWorker] = None

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


class BacktestTaskManager(QObject):
    """手动回测任务队列管理器。

    并发数默认 ``8``（2026-05-27 21:30 hotfix 从 3 提到 8）。

    背景：主人 21:30 实测 3 并发跑得顺，要求"更多路"。
    第一性分析：
    * SQLite WAL：CLI workers=4 跑 30 天历史已实测无错，5~8 完全扛得住
    * DeepSeek 限流：付费档实测可稳定 60+ req/min；每次回测 1~2 req，
      8 并发 ≈ 16 req/min，远低于阈值
    * 真正的瓶颈是 provider 偶发 5xx，并发越高单笔失败概率轻微升高
      但失败只影响单个 task，不污染其他

    构造时可通过 ``max_concurrent`` 参数覆盖（用于回归测试 mock 出
    "5 任务测 3 并发"这种语义）；GUI 默认走类默认值 8。

    Signals:
        task_added(int task_id):      新任务进队列（PENDING 或直接 RUNNING）
        task_started(int task_id):    任务从 PENDING 切到 RUNNING
        task_progress(int task_id, str msg):    阶段日志（worker.progress 转发）
        task_streaming(int task_id, str chunk): LLM chunk（worker.streaming 转发）
        task_finished(int task_id):   任务终态（SUCCESS/SKIPPED/FAILED 任一）
        task_removed(int task_id):    任务被主人删除（仅 GUI 行消失）
    """

    # 对外信号（page 监听）
    task_added = pyqtSignal(int)
    task_started = pyqtSignal(int)
    task_progress = pyqtSignal(int, str)
    task_streaming = pyqtSignal(int, str)
    task_finished = pyqtSignal(int)
    task_removed = pyqtSignal(int)

    # 内部 relay 信号（用于把 worker 子线程信号 → 主线程 manager 槽）。
    # 原因：worker.signal.connect(lambda) 模式下 lambda 在 worker 子线程
    # 执行，若直接在 lambda 里改 self._running / self._pending 会引发
    # 多 worker 同时完成的 race（_pump 重复起 worker）。
    # 改为 lambda → emit 本类信号 → AutoConnection 检测跨线程 →
    # QueuedConnection 排到主线程事件循环，所有状态修改严格主线程串行。
    _relay_stage = pyqtSignal(int, str)
    _relay_progress = pyqtSignal(int, str)
    _relay_streaming = pyqtSignal(int, str)
    _relay_finished_result = pyqtSignal(int, dict)
    _relay_error = pyqtSignal(int, str)
    _relay_qthread_done = pyqtSignal(int)

    # 类默认值（2026-05-27 21:30 hotfix：3 → 8）。
    # 通过 __init__(max_concurrent=...) 可覆盖（测试用）。
    _DEFAULT_MAX_CONCURRENT: int = 8
    # 硬上限（2026-05-28 17:55 主人指令：16 → 64）。
    # 实测 DeepSeek 付费档可承受 60+ req/min；64 路并发对应 ~60 req/min
    # 仍在阈值内，主要瓶颈转为 LLM 5xx 偶发概率与本机 SQLite WAL 写入。
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
        self._tasks: "OrderedDict[int, BacktestTask]" = OrderedDict()
        self._pending: "deque[int]" = deque()
        self._running: set[int] = set()
        self._next_id: int = 1

        # relay 接到主线程 slot（manager 在主线程，receiver 也在主线程，
        # 但 emit 来自 worker 子线程 → Qt 自动 QueuedConnection）
        self._relay_stage.connect(self._on_worker_stage)
        self._relay_progress.connect(self._on_worker_progress)
        self._relay_streaming.connect(self._on_worker_streaming)
        self._relay_finished_result.connect(self._on_worker_finished)
        self._relay_error.connect(self._on_worker_error)
        self._relay_qthread_done.connect(self._on_worker_qthread_done)

    # =====================================================================
    # 对外 API
    # =====================================================================

    def enqueue(self, task_kwargs: Dict[str, Any]) -> int:
        """加一个任务到队列。

        Args:
            task_kwargs: 必含字段——
                template_id, template_label, trade_date,
                news_start_dt, news_end_dt, news_status,
                provider, dry_run, overwrite

        Returns:
            分配的 task_id（进程内自增，从 1 开始）

        副作用:
            * emit task_added(tid)
            * 调 _pump() 立即尝试调度（队列有空位则直接 RUNNING）
        """
        tid = self._next_id
        self._next_id += 1

        task = BacktestTask(
            task_id=tid,
            template_id=task_kwargs["template_id"],
            template_label=task_kwargs.get(
                "template_label", task_kwargs["template_id"],
            ),
            trade_date=task_kwargs["trade_date"],
            news_start_dt=task_kwargs["news_start_dt"],
            news_end_dt=task_kwargs["news_end_dt"],
            news_status=task_kwargs.get("news_status", "curated"),
            provider=task_kwargs.get("provider"),
            dry_run=task_kwargs.get("dry_run", False),
            overwrite=task_kwargs.get("overwrite", False),
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

        重要约定（来自内容文档 §三·B 第 7 条）：
            本方法**仅删 GUI 内存对象**，绝不动 md 文件 / ai_inference.db 行。
            产物清理是「评估页·单报告删除」按钮的职责，本模块严格不越界。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.RUNNING:
            return False
        # PENDING 也要从 deque 摘掉
        if task.status == TaskStatus.PENDING:
            try:
                self._pending.remove(task_id)
            except ValueError:
                pass
        self._tasks.pop(task_id, None)
        self.task_removed.emit(task_id)
        return True

    def get(self, task_id: int) -> Optional[BacktestTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[BacktestTask]:
        """按 submit_ts 升序返回所有任务（OrderedDict 天然保序）。"""
        return list(self._tasks.values())

    def has_active(self) -> bool:
        """是否还有未到终态的任务（PENDING + RUNNING）。供页面退出时判断用。"""
        return any(not t.is_terminal for t in self._tasks.values())

    def get_max_concurrent(self) -> int:
        """当前并发上限（GUI 显示用）。"""
        return self._max_concurrent

    def set_max_concurrent(self, n: int) -> int:
        """运行时调整并发上限。

        Args:
            n: 期望并发数（会被 clamp 到 [1, MAX_CONCURRENT_HARD_CAP]）

        Returns:
            实际生效的并发数

        语义：
            * 调大 → 立即 _pump 把 PENDING 拉起来填满空位
            * 调小 → 不强杀已 RUNNING 的任务，等它们自然结束后停止派新单
        """
        n = max(1, min(int(n), self.MAX_CONCURRENT_HARD_CAP))
        self._max_concurrent = n
        self._pump()
        return n

    def shutdown(self) -> None:
        """关 GUI 时调用：取消所有 PENDING，让 RUNNING 自然结束。

        RUNNING 的 worker 不强杀（LLM 已发出难中断；本期不实现取消）。
        PENDING 全部转 FAILED("已取消")。
        """
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

    def _start_worker(self, task: BacktestTask) -> None:
        """实际起一个 ManualBacktestWorker 跑 task。"""
        task.status = TaskStatus.RUNNING
        task.start_ts = time.time()
        self._running.add(task.task_id)

        worker = ManualBacktestWorker(
            template_id=task.template_id,
            trade_date=task.trade_date,
            news_start_dt=task.news_start_dt,
            news_end_dt=task.news_end_dt,
            news_status=task.news_status,
            provider=task.provider,
            overwrite=task.overwrite,
            dry_run=task.dry_run,
            task_id=task.task_id,
        )
        task.worker = worker

        # lambda 在 worker 子线程被调用 → emit relay 信号；relay 连到
        # manager 自身槽，AutoConnection 跨线程 → QueuedConnection →
        # 所有状态修改在主线程串行（避免 _pump race）
        tid = task.task_id
        worker.stage.connect(
            lambda msg, _t=tid: self._relay_stage.emit(_t, msg)
        )
        worker.progress.connect(
            lambda msg, _t=tid: self._relay_progress.emit(_t, msg)
        )
        worker.streaming.connect(
            lambda chunk, _t=tid: self._relay_streaming.emit(_t, chunk)
        )
        worker.finished_result.connect(
            lambda res, _t=tid: self._relay_finished_result.emit(_t, res)
        )
        worker.error.connect(
            lambda err, _t=tid: self._relay_error.emit(_t, err)
        )
        worker.finished.connect(
            lambda _t=tid: self._relay_qthread_done.emit(_t)
        )

        self.task_started.emit(task.task_id)
        worker.start()

    # =====================================================================
    # 信号转发槽（worker → manager → page）
    # =====================================================================

    def _on_worker_stage(self, tid: int, msg: str) -> None:
        # stage 复用 progress 通道（page 旧逻辑就是这么混的）
        self._on_worker_progress(tid, msg)

    def _on_worker_progress(self, tid: int, msg: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"\n[{ts}] {msg}\n"
        # 启发式：第一次出现"题材"+"抽取/入库" → 插分隔栏
        if (
            not task.theme_section_marked
            and "题材" in msg
            and ("抽取" in msg or "入库" in msg)
        ):
            line = (
                "\n\n────────── 题材抽取阶段 ──────────\n" + line
            )
            task.theme_section_marked = True
        task.log_buffer.append(line)
        self.task_progress.emit(tid, line)

    def _on_worker_streaming(self, tid: int, chunk: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        task.stream_buffer.append(chunk)
        self.task_streaming.emit(tid, chunk)

    def _on_worker_finished(self, tid: int, res: dict) -> None:
        """worker 业务结束。注意：QThread 自身的 finished 还会再触发一次清理。"""
        task = self._tasks.get(tid)
        if task is None:
            return
        task.result = res
        task.error = res.get("error")
        if not res.get("ok"):
            task.status = TaskStatus.FAILED
        elif res.get("skipped"):
            task.status = TaskStatus.SKIPPED
        else:
            task.status = TaskStatus.SUCCESS
        task.end_ts = time.time()
        if task.start_ts:
            task.elapsed_ms = int((task.end_ts - task.start_ts) * 1000)
        self.task_finished.emit(tid)

    def _on_worker_error(self, tid: int, err: str) -> None:
        """worker 线程级异常（traceback 已被 worker 捕获后传上来）。"""
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
        """QThread.finished：清理 worker 强引用 + 摘出 _running + 调度下一个。"""
        self._running.discard(tid)
        task = self._tasks.get(tid)
        if task is not None:
            task.worker = None
        self._pump()
