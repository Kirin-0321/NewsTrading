"""AI 分析·任务队列管理器（2026-05-28 批量模板改造）。

业务定位
--------
让 AI 分析页支持「勾选 N 个模板 → 共用同一份数据源/时间窗/盘后总结 →
N 个任务排队 + 后台并发跑 + 点行联动下方结果」。

设计同源
--------
完全仿照 ``gui/workers/manual_backtest_manager.py``：
* 5 态机：PENDING / RUNNING / SUCCESS / SKIPPED / FAILED
* relay 信号跨线程串行化（避免多 worker 同时完成 race）
* 默认并发 ``16``（2026-05-28 23:00 主人指令；与手动回测的 8 拉开是因为
  AI 分析多模板批量场景普遍需要一次跑十几个）
* 硬上限 ``64``（与手动回测对齐）

差异点（与手动回测）
--------------------
* 不需要 trade_date 字段（AI 分析是按时间窗读新闻，不锚定交易日）
* 字段语义偏「数据源 + 时间窗 + 盘后总结 + 深度思考开关 + 题材抽取开关」
* SKIPPED 语义不同：AI 分析无 dry-run，但「时间窗内 0 新闻」算 SKIPPED
* 没有 overwrite 概念（每次都生成新报告）

    并发模型
    --------
    ::

        page._on_run_clicked
           └── manager.enqueue(task_kwargs) × N   →  task_id × N
                  └── _pump()
                         ├── 若 RUNNING < max_concurrent → 起 AIAnalysisWorker
                         └── 否则 PENDING 排队
                                └── 任意 worker finish → _pump 再次尝试

调用链
------
::

    AIAnalysisPage
       ├── manager.enqueue(...)  →  task_added(tid)
       ├── manager.remove(tid)   →  task_removed(tid)
       └── 监听 6 个 signal 刷 UI

任务删除规则
------------
* status==RUNNING → 拒绝删除，返回 False
* 其它状态 → 仅从内存 dict 删行；**不删 md 文件 / 不删 ai_reports.db
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

from gui.workers.ai_analysis_worker import AIAnalysisWorker


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """任务 5 态机（继承 str 让 JSON 序列化友好）。"""

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
class AnalysisTask:
    """单个 AI 分析任务的内存对象。

    所有字段 public 直接读：GUI 内部 dataclass，被 manager / page 直接访问，
    封装无意义。
    """

    task_id: int
    template_id: str
    template_label: str        # 展示用（去掉 [key] 后缀）
    provider: str
    sqlite_source: str         # "curated" / "raw"
    sqlite_start: datetime
    sqlite_end: datetime
    market_summary: str        # 盘后总结（可空字符串）
    extract_themes: bool
    enable_deep_thinking: bool
    status: TaskStatus = TaskStatus.PENDING
    submit_ts: float = field(default_factory=time.time)
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    elapsed_ms: Optional[int] = None
    stream_buffer: List[str] = field(default_factory=list)
    log_buffer: List[str] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancelled: bool = False
    worker: Optional[AIAnalysisWorker] = None
    # task_finished 信号去重：worker 终态时可能多个信号槽同步触发
    # （error + finished_result + cancelled 任意 2 个组合），只允许 emit 一次
    _finished_emitted: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABEL[self.status]

    @property
    def status_color(self) -> str:
        return STATUS_COLOR[self.status]

    @property
    def is_terminal(self) -> bool:
        """是否终态（不再变化）。"""
        return self.status in (
            TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.FAILED,
        )

    def elapsed_text(self) -> str:
        """用时列显示文本。

        - PENDING: "-"
        - RUNNING: 实时秒数（由 page QTimer 1s 刷新）
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


class AnalysisTaskManager(QObject):
    """AI 分析任务队列管理器。

    并发数默认 ``16``（2026-05-28 23:00 主人指令：从初版 3 直接拉到 16）。

    背景：主人确认 DeepSeek V4 Pro 付费档可稳定承受 60+ req/min。每个 AI 分析
    任务约 1~2 req（主分析 + 题材抽取），16 并发 ≈ 32 req/min，远低于阈值；
    硬上限 ``64`` 与手动回测对齐，给主人留出"批量 30 模板一次跑完"的空间。

    构造时可通过 ``max_concurrent`` 参数覆盖；GUI 默认走类默认值 16。

    Signals:
        task_added(int task_id):      新任务进队列
        task_started(int task_id):    PENDING → RUNNING
        task_progress(int task_id, str msg):    阶段日志
        task_streaming(int task_id, str chunk): LLM chunk
        task_finished(int task_id):   任务终态（SUCCESS/SKIPPED/FAILED 任一）
        task_removed(int task_id):    主人删除（仅 GUI 行消失）
    """

    task_added = pyqtSignal(int)
    task_started = pyqtSignal(int)
    task_progress = pyqtSignal(int, str)
    task_streaming = pyqtSignal(int, str)
    task_finished = pyqtSignal(int)
    task_removed = pyqtSignal(int)

    # 内部 relay 信号——worker 子线程 emit → manager 主线程槽（QueuedConnection），
    # 所有状态修改严格主线程串行（避免 _pump 重复起 worker 的 race）。
    _relay_progress = pyqtSignal(int, str)
    _relay_streaming = pyqtSignal(int, str)
    _relay_finished = pyqtSignal(int, dict)
    _relay_error = pyqtSignal(int, str)
    _relay_cancelled = pyqtSignal(int)
    _relay_qthread_done = pyqtSignal(int)

    # 类默认值（2026-05-28 23:00：3 → 16）
    _DEFAULT_MAX_CONCURRENT: int = 16
    # 硬上限（2026-05-28 23:00：16 → 64，与手动回测对齐）。
    # 实测 DeepSeek 付费档 64 并发对应 ~60 req/min 仍在阈值内；进一步上调
    # 主要受 LLM 5xx 概率与本机 SQLite WAL 写入压力影响（失败只影响单任务）。
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
        self._tasks: "OrderedDict[int, AnalysisTask]" = OrderedDict()
        self._pending: "deque[int]" = deque()
        self._running: set[int] = set()
        self._next_id: int = 1

        self._relay_progress.connect(self._on_worker_progress)
        self._relay_streaming.connect(self._on_worker_streaming)
        self._relay_finished.connect(self._on_worker_finished)
        self._relay_error.connect(self._on_worker_error)
        self._relay_cancelled.connect(self._on_worker_cancelled)
        self._relay_qthread_done.connect(self._on_worker_qthread_done)

    # =====================================================================
    # 对外 API
    # =====================================================================

    def enqueue(self, task_kwargs: Dict[str, Any]) -> int:
        """加一个任务到队列。

        Args:
            task_kwargs: 必含字段——
                template_id, template_label, provider,
                sqlite_source, sqlite_start, sqlite_end,
                market_summary, extract_themes, enable_deep_thinking

        Returns:
            分配的 task_id（进程内自增，从 1 开始）

        副作用:
            * emit task_added(tid)
            * 调 _pump() 立即尝试调度（队列有空位则直接 RUNNING）
        """
        tid = self._next_id
        self._next_id += 1

        task = AnalysisTask(
            task_id=tid,
            template_id=task_kwargs["template_id"],
            template_label=task_kwargs.get(
                "template_label", task_kwargs["template_id"],
            ),
            provider=task_kwargs.get("provider") or "deepseek",
            sqlite_source=task_kwargs.get("sqlite_source") or "curated",
            sqlite_start=task_kwargs["sqlite_start"],
            sqlite_end=task_kwargs["sqlite_end"],
            market_summary=task_kwargs.get("market_summary") or "",
            extract_themes=bool(
                task_kwargs.get("extract_themes", True)
            ),
            enable_deep_thinking=bool(
                task_kwargs.get("enable_deep_thinking", True)
            ),
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

        重要约定：**仅删 GUI 内存对象**，绝不动 md 文件 / ai_reports.db 行。
        产物清理是「评估页·单报告删除」按钮的职责。
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

    def cancel_running(self, task_id: int) -> bool:
        """主动取消一个正在跑的任务（调 worker.request_cancel）。

        Returns:
            * True: 已发出取消请求（实际终止由 LLM 流式回调下次触发）
            * False: 任务不存在 / 不在 RUNNING 态
        """
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            return False
        if task.worker is not None:
            try:
                task.worker.request_cancel()
                task.cancelled = True
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def get(self, task_id: int) -> Optional[AnalysisTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[AnalysisTask]:
        """按 submit_ts 升序返回所有任务（OrderedDict 天然保序）。"""
        return list(self._tasks.values())

    def has_active(self) -> bool:
        """是否还有未到终态的任务（PENDING + RUNNING）。"""
        return any(not t.is_terminal for t in self._tasks.values())

    def get_max_concurrent(self) -> int:
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

        RUNNING 的 worker 不强杀（LLM 已发出，靠 cancel_check 在下次回调触发；
        本期不保证 RUNNING 立即终止）。PENDING 全部转 FAILED("已取消")。
        """
        for tid in list(self._pending):
            task = self._tasks.get(tid)
            if task is not None:
                task.status = TaskStatus.FAILED
                task.error = "GUI 关闭：任务被取消"
                task.elapsed_ms = 0
                self._emit_finished_once(task)
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

    def _start_worker(self, task: AnalysisTask) -> None:
        """实际起一个 AIAnalysisWorker 跑 task。"""
        task.status = TaskStatus.RUNNING
        task.start_ts = time.time()
        self._running.add(task.task_id)

        worker = AIAnalysisWorker(
            provider=task.provider,
            max_sectors='auto',
            stocks_per_sector='auto',
            max_news=5000,
            template_id=task.template_id,
            market_summary=task.market_summary,
            sqlite_source=task.sqlite_source,
            sqlite_start=task.sqlite_start,
            sqlite_end=task.sqlite_end,
            extract_themes=task.extract_themes,
            enable_deep_thinking=task.enable_deep_thinking,
            task_id=task.task_id,
        )
        task.worker = worker

        # lambda 在 worker 子线程被调用 → emit relay 信号；relay 连到 manager
        # 自身槽，AutoConnection 跨线程 → QueuedConnection → 主线程串行。
        # 注意：worker 业务结果走 `finished_result`（dict）而不是 `finished` —
        # 后者是 QThread 自带的无参信号，专门留给本类的 _relay_qthread_done 调度。
        tid = task.task_id
        worker.progress.connect(
            lambda msg, _t=tid: self._relay_progress.emit(_t, msg)
        )
        worker.streaming.connect(
            lambda chunk, _t=tid: self._relay_streaming.emit(_t, chunk)
        )
        worker.finished_result.connect(
            lambda res, _t=tid: self._relay_finished.emit(_t, res)
        )
        worker.error.connect(
            lambda err, _t=tid: self._relay_error.emit(_t, err)
        )
        worker.cancelled.connect(
            lambda _t=tid: self._relay_cancelled.emit(_t)
        )
        # QThread.finished()（无参）：业务终态发完之后 QThread 退出时自然 emit，
        # 唯一负责「摘 _running + 调下一个 PENDING」
        worker.finished.connect(
            lambda _t=tid: self._relay_qthread_done.emit(_t)
        )

        self.task_started.emit(task.task_id)
        worker.start()

    # =====================================================================
    # 信号转发槽（worker → manager → page）
    # =====================================================================

    def _emit_finished_once(self, task: AnalysisTask) -> None:
        """task_finished 信号去重 emit（同一 task 仅 emit 一次）。

        触发场景：worker 在终态时可能多个信号同步 emit（如失败时同时 emit
        error + finished_result），多个槽都想 emit task_finished。GUI 收两次
        不致命但浪费；本 helper 保证恰好一次。
        """
        if task._finished_emitted:
            return
        task._finished_emitted = True
        if not task.end_ts:
            task.end_ts = time.time()
            if task.start_ts:
                task.elapsed_ms = int(
                    (task.end_ts - task.start_ts) * 1000
                )
        self.task_finished.emit(task.task_id)

    def _on_worker_progress(self, tid: int, msg: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        task.log_buffer.append(line)
        self.task_progress.emit(tid, line)

    def _on_worker_streaming(self, tid: int, chunk: str) -> None:
        task = self._tasks.get(tid)
        if task is None:
            return
        task.stream_buffer.append(chunk)
        self.task_streaming.emit(tid, chunk)

    def _on_worker_finished(self, tid: int, res: dict) -> None:
        """worker 业务终态（worker.finished_result emit dict）。

        终态语义：
        * ``ok==False`` + ``cancelled==True``  → FAILED（error="已取消"）
        * ``ok==False`` 其它                   → FAILED
        * ``ok==True`` + ``news_count==0``    → SKIPPED（理论不会走到，因
          service 在 0 新闻时 ok=False，但留个保险分支）
        * ``ok==True``                         → SUCCESS
        """
        task = self._tasks.get(tid)
        if task is None:
            return
        task.result = res
        # 已被 error / cancelled 槽推到终态时，保留原 status 仅刷 result
        if not task.is_terminal:
            if not res.get("ok"):
                task.status = TaskStatus.FAILED
                task.error = res.get("error") or "未知错误"
                if res.get("cancelled"):
                    task.cancelled = True
            elif res.get("news_count", 0) == 0:
                task.status = TaskStatus.SKIPPED
                task.error = "时间窗内无新闻"
            else:
                task.status = TaskStatus.SUCCESS
        self._emit_finished_once(task)

    def _on_worker_error(self, tid: int, err: str) -> None:
        """worker 报错（business error 或 线程级异常）。"""
        task = self._tasks.get(tid)
        if task is None:
            return
        if not task.is_terminal:
            task.status = TaskStatus.FAILED
            task.error = err
        self._emit_finished_once(task)

    def _on_worker_cancelled(self, tid: int) -> None:
        """worker 收到取消请求并退出（cancel_check 触发）。"""
        task = self._tasks.get(tid)
        if task is None:
            return
        if not task.is_terminal:
            task.status = TaskStatus.FAILED
            task.error = "已取消"
        task.cancelled = True
        self._emit_finished_once(task)

    def _on_worker_qthread_done(self, tid: int) -> None:
        """QThread.finished：清理 worker 强引用 + 摘出 _running + 调度下一个。"""
        self._running.discard(tid)
        task = self._tasks.get(tid)
        if task is not None:
            task.worker = None
        self._pump()
