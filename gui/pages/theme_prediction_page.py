"""题材预测库页面。

上半区（2026-05-28 队列化改造后改为左右两栏）：
    左栏：手动选择分析报告 .md → 调 AI 抽取 → 入库（原 UI 不动）
    右栏：📋 任务队列（评估页 + 本页所有抽取任务汇入同一条队列）
下半区: 按报告日期查看已入库题材，行点击展开标的与新闻

调用链:
    init_ui()
        ├── _build_extract_group()       上半区（左：手动抽取 / 右：队列）
        └── _build_viewer_group()        下半区（查看已入库）
    start_extract()
        └→ self._tem.enqueue(report_path=...) → ThemeExtractWorker (线程)
    set_theme_extract_manager(mgr)        MainWindow 注入 manager 单例
        └→ _wire_manager_signals()       接 5 个信号刷队列表
    showEvent / refresh / 日期切换
        └→ _reload_dates() / _reload_themes_for_date()
    选中题材表格行
        └→ _show_theme_detail()         填充标的与新闻表
"""

import os
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QAbstractItemView, QHeaderView, QSpinBox,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QComboBox, QLineEdit, QFileDialog, QMessageBox, QTextBrowser,
    QTableWidget, QTableWidgetItem, QSplitter, QTabWidget,
)

from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, INPUT_STYLE, TEXTBROWSER_STYLE, TABLE_STYLE,
)
from gui.widgets.sparkline import SparklineWidget
from gui.workers.theme_extract_manager import (
    STATUS_COLOR, STATUS_LABEL, TaskStatus, ThemeExtractTaskManager,
)


# 任务队列表格列定义（4 列：# / 报告名 / 状态+用时 / 删除）
_QUEUE_TABLE_COLS = [
    ("#", 40),
    ("报告名", 320),
    ("状态 / 用时", 130),
    ("删除", 60),
]


_THEME_TABLE_COLS = [
    ("时间", 56),
    ("题材", 180),
    ("等级", 90),
    ("分数", 60),
    ("板块", 180),
    ("模板", 150),
    ("冷处理", 60),
    ("持续性", 70),
    ("预期差", 70),
    ("排名", 50),
    ("核心逻辑（含催化）", 240),
]

# 可点击排序的列索引 -> 数据字段
_SORTABLE_COLS = {
    2: "strength_level",
    3: "strength_score",
    8: "expectation_gap",
    9: "priority_rank",
}

# 9 档等级排序（高利多 → 中性 → 高利空，绝对值越大数值越大）
_LEVEL_ORDER = {
    "重大利多": 9,
    "较强利多": 8,
    "弱利多": 7,
    "中性偏多": 6,
    "中性": 5,
    "中性偏空": 4,
    "弱利空": 3,
    "较强利空": 2,
    "重大利空": 1,
}
# 预期差序（高 → 低）
_GAP_ORDER = {"高": 5, "中高": 4, "中": 3, "中低": 2, "低": 1}

_STOCK_TABLE_COLS = [
    ("标的", 140),
    ("代码（原始）", 110),
    ("代码（标准化）", 130),
    ("角色", 70),
    ("理由", 380),
]

_NEWS_TABLE_COLS = [
    ("新闻锚点", 80),
    ("关联类型", 70),
    ("时间", 130),
    ("来源", 90),
    ("标题（反查 raw_news）", 360),
]

_NEWS_TITLE_COL = 4

# 板块走势 Tab 的表格列
_SECTOR_TREND_COLS = [
    ("交易日", 100),
    ("偏移", 60),
    ("涨跌幅 %", 90),
    ("主力净流入 (亿)", 130),
    ("当日排名", 80),
]

# 评估窗口（D+1 ~ D+EVAL_DAYS）长度，与打分模块对齐
_TREND_EVAL_DAYS = 5

# 板块匹配置信度弱匹配阈值：低于该值视为「弱匹配 / 仅供参考」。
# 与评估页 prompt_eval_page._render_theme_branch 的 ⚠ 角标判定保持一致
# （历史上题材页用 0.7、评估页用 0.5 不对齐，05-29 统一为 0.5）。
_SECTOR_WEAK_MATCH_CONF = 0.5


class ThemePredictionPage(QWidget):
    """题材预测库页面。"""

    def __init__(self):
        super().__init__()
        # manager 由 MainWindow 在 pages 创建完成后通过
        # set_theme_extract_manager 显式注入（self.window() 在 __init__
        # 阶段拿不到 mainwindow，必须走 setter）。
        self._tem: Optional[ThemeExtractTaskManager] = None
        # 队列表格里的 task_id ↔ row 双向映射（信号增量更新用）
        self._row_to_tid: list[int] = []
        self._tid_to_row: dict[int, int] = {}
        # 当前选中"看进度"的 task_id（左栏日志/流式回灌的目标）
        self._current_task_id: Optional[int] = None
        self._current_themes: list = []
        self._sort_col: Optional[int] = None
        self._sort_asc: bool = True
        # 锁定到 ai_reports.id 的单报告精确浏览模式（评估页双击进入）；
        # None = 普通"按日期+模板"浏览。详见 load_by_report_id 文档。
        self._locked_report_id: Optional[int] = None
        self.init_ui()
        # RUNNING 任务用时实时刷新（参考手动回测同款 1s tick）
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_running_elapsed)
        self._elapsed_timer.start()
        self.refresh()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = QLabel("🎯 题材预测库")
        title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_extract_group())
        splitter.addWidget(self._build_viewer_group())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 700])
        layout.addWidget(splitter, 1)

    # ---------- 上半区：手动抽取（左）+ 任务队列（右） ----------

    def _build_extract_group(self) -> QWidget:
        """上半区改造（2026-05-28 队列化）：横向左右两栏 QSplitter。

        左栏：原"手动抽取"GroupBox（文件选择 / 抽取按钮 / 阶段日志 /
              流式预览），日志和流式都跟随当前选中任务回灌。
        右栏：新增"📋 任务队列"GroupBox（4 列任务表 + 顶部并发 spin
              + 清完成按钮），评估页 / 本页发起的抽取任务全部汇入此队列。
        """
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_manual_extract_group())
        split.addWidget(self._build_queue_panel())
        # 左 3 / 右 2 的初始比例（左栏是日志主区，右栏只看任务行）
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([600, 400])
        return split

    def _build_manual_extract_group(self) -> QGroupBox:
        """左栏：原"手动抽取"UI 完全保留，日志 / 流式跟随当前选中任务。"""
        group = QGroupBox("📥 手动抽取题材（选择已有分析报告）")
        layout = QVBoxLayout()

        # 文件选择行
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("报告文件:"))
        self.file_input = QLineEdit()
        self.file_input.setStyleSheet(INPUT_STYLE)
        self.file_input.setPlaceholderText(
            "选择 data/AI_analysis/ 下的 *.md 报告"
        )
        file_row.addWidget(self.file_input, 1)
        self.browse_btn = QPushButton("📂 浏览")
        self.browse_btn.clicked.connect(self._on_browse)
        file_row.addWidget(self.browse_btn)
        layout.addLayout(file_row)

        # 提取按钮 + 状态
        action_row = QHBoxLayout()
        self.extract_btn = QPushButton("🚀 开始抽取并入库")
        self.extract_btn.setStyleSheet(BUTTON_PRIMARY)
        self.extract_btn.setMinimumHeight(36)
        self.extract_btn.setToolTip(
            "把当前选中的 .md 加入队列（连点 N 次排 N 个任务）"
        )
        self.extract_btn.clicked.connect(self.start_extract)
        action_row.addWidget(self.extract_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        # 阶段日志（带时间戳；跟随当前选中任务回灌）
        self.extract_log = QTextBrowser()
        self.extract_log.setStyleSheet(TEXTBROWSER_STYLE)
        self.extract_log.setMaximumHeight(100)
        layout.addWidget(self.extract_log)

        # AI 流式输出预览（chunk 实时粘连写入；跟随当前选中任务回灌）
        layout.addWidget(QLabel("🤖 AI 实时输出（流式 JSON 预览）:"))
        self.stream_browser = QTextBrowser()
        self.stream_browser.setStyleSheet(
            TEXTBROWSER_STYLE
            + "QTextBrowser { font-family: 'Consolas', 'Courier New', monospace;"
            "  font-size: 12px; color: #595959; }"
        )
        self.stream_browser.setMaximumHeight(160)
        layout.addWidget(self.stream_browser)

        group.setLayout(layout)
        return group

    def _build_queue_panel(self) -> QGroupBox:
        """右栏：任务队列面板（4 列任务表 + 顶部并发 spin + 清完成按钮）。

        信号绑定要等 manager 注入（``set_theme_extract_manager``）后才接，
        构造时控件先创出来即可。
        """
        group = QGroupBox(
            "📋 题材抽取队列（评估页 / 本页所有抽取任务汇入此处）"
        )
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        # 顶栏：并发数微调器 + 清完成按钮
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("并发:"))
        self.queue_concurrency_spin = QSpinBox()
        self.queue_concurrency_spin.setRange(
            1, ThemeExtractTaskManager.MAX_CONCURRENT_HARD_CAP
        )
        self.queue_concurrency_spin.setValue(
            ThemeExtractTaskManager._DEFAULT_MAX_CONCURRENT
        )
        self.queue_concurrency_spin.setSuffix(" 路")
        self.queue_concurrency_spin.setToolTip(
            "同时跑几个题材抽取任务（1~64）。\n"
            "默认 16 路与 DeepSeek V4 Pro 60+ req/min 阈值匹配；\n"
            "调大立即填满空位，调小不强杀 RUNNING 但停止派新单。"
        )
        self.queue_concurrency_spin.valueChanged.connect(
            self._on_queue_concurrency_changed
        )
        top_row.addWidget(self.queue_concurrency_spin)
        top_row.addSpacing(12)

        self.queue_hint_label = QLabel("（暂无任务）")
        self.queue_hint_label.setStyleSheet(
            "color: #1890ff; font-size: 12px; padding: 0 6px;"
        )
        top_row.addWidget(self.queue_hint_label, 1)

        self.clear_done_btn = QPushButton("🧹 清完成")
        self.clear_done_btn.setStyleSheet(BUTTON_SUCCESS)
        self.clear_done_btn.setToolTip(
            "把所有终态任务（成功 / 失败 / 跳过）从队列移除"
        )
        self.clear_done_btn.clicked.connect(self._on_clear_done)
        top_row.addWidget(self.clear_done_btn)
        layout.addLayout(top_row)

        # 任务表
        self.queue_table = QTableWidget(0, len(_QUEUE_TABLE_COLS))
        self.queue_table.setHorizontalHeaderLabels(
            [c[0] for c in _QUEUE_TABLE_COLS]
        )
        self.queue_table.setStyleSheet(TABLE_STYLE)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, (_, w) in enumerate(_QUEUE_TABLE_COLS):
            self.queue_table.setColumnWidth(i, w)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.queue_table.itemSelectionChanged.connect(
            self._on_queue_row_selected
        )
        layout.addWidget(self.queue_table, 1)

        return group

    def _on_browse(self):
        default_dir = os.path.abspath(os.path.join("data", "AI_analysis"))
        if not os.path.isdir(default_dir):
            default_dir = os.path.abspath("data")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择分析报告",
            default_dir,
            "Markdown 文件 (*.md);;所有文件 (*.*)",
        )
        if path:
            self.file_input.setText(path)

    # ---------- manager 注入 + 信号绑定 ----------

    def set_theme_extract_manager(
        self, manager: ThemeExtractTaskManager
    ) -> None:
        """MainWindow 在 pages 创建完成后调用，把跨页 manager 单例注入。

        一次性绑定 5 个信号；UI 控件创建已在 init_ui 完成，此处只做接线。
        """
        if self._tem is not None:
            return
        self._tem = manager
        # 同步 spin 当前值（manager 默认 16，与 spin 默认一致；保险起见同步）
        self.queue_concurrency_spin.blockSignals(True)
        self.queue_concurrency_spin.setValue(manager.get_max_concurrent())
        self.queue_concurrency_spin.blockSignals(False)

        manager.task_added.connect(self._on_task_added)
        manager.task_started.connect(self._on_task_started)
        manager.task_progress.connect(self._on_task_progress)
        manager.task_streaming.connect(self._on_task_streaming)
        manager.task_finished.connect(self._on_task_finished)
        manager.task_removed.connect(self._on_task_removed)

    # ---------- 入队 ----------

    def start_extract(self):
        """把当前选中的 .md 入队（连点 N 次 = 排 N 个任务）。"""
        if self._tem is None:
            QMessageBox.warning(
                self, "未就绪",
                "题材抽取 manager 未注入，请重启 GUI 后再试。"
            )
            return

        path = self.file_input.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请先选择报告文件")
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "提示", f"文件不存在：{path}")
            return

        tid = self._tem.enqueue(report_path=os.path.abspath(path))
        # 立即把这个新任务设为"看进度"焦点，主人能立刻在左栏看见日志/流式
        self._select_task_row(tid)

    # ---------- manager 信号槽 ----------

    def _on_task_added(self, tid: int) -> None:
        if self._tem is None:
            return
        task = self._tem.get(tid)
        if task is None:
            return
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        self._row_to_tid.append(tid)
        self._tid_to_row[tid] = row
        self._render_task_row(row, tid)
        self._refresh_queue_hint()

    def _on_task_started(self, tid: int) -> None:
        self._render_task_row(self._tid_to_row.get(tid, -1), tid)
        self._refresh_queue_hint()

    def _on_task_progress(self, tid: int, line: str) -> None:
        if tid != self._current_task_id:
            return
        self.extract_log.append(line)

    def _on_task_streaming(self, tid: int, chunk: str) -> None:
        if tid != self._current_task_id:
            return
        self.stream_browser.insertPlainText(chunk)
        self.stream_browser.moveCursor(QTextCursor.End)

    def _on_task_finished(self, tid: int) -> None:
        self._render_task_row(self._tid_to_row.get(tid, -1), tid)
        self._refresh_queue_hint()
        if self._tem is None:
            return
        task = self._tem.get(tid)
        if task is None:
            return
        # 成功 → 自动刷新查看区到对应日期
        if (
            task.status == TaskStatus.SUCCESS
            and task.result
            and task.result.get("report_date")
        ):
            self._reload_dates(prefer_date=task.result["report_date"])

    def _on_task_removed(self, tid: int) -> None:
        row = self._tid_to_row.pop(tid, None)
        if row is None:
            return
        self.queue_table.removeRow(row)
        self._row_to_tid.pop(row)
        # 重排 row → tid 映射（被移除行之后的行号 -1）
        self._tid_to_row = {t: i for i, t in enumerate(self._row_to_tid)}
        if self._current_task_id == tid:
            self._current_task_id = None
            self.extract_log.clear()
            self.stream_browser.clear()
        self._refresh_queue_hint()

    # ---------- 队列 UI 辅助 ----------

    def _render_task_row(self, row: int, tid: int) -> None:
        if row < 0 or self._tem is None:
            return
        task = self._tem.get(tid)
        if task is None:
            return
        # # 列
        self.queue_table.setItem(row, 0, QTableWidgetItem(str(tid)))
        # 报告名列
        name_item = QTableWidgetItem(task.report_basename)
        name_item.setToolTip(task.report_path)
        self.queue_table.setItem(row, 1, name_item)
        # 状态 / 用时
        status_text = f"{task.status_label} / {task.elapsed_text()}"
        st_item = QTableWidgetItem(status_text)
        st_item.setForeground(QColor(task.status_color))
        self.queue_table.setItem(row, 2, st_item)
        # 删除按钮（RUNNING disable）
        del_btn = QPushButton("🗑")
        del_btn.setStyleSheet(BUTTON_DANGER)
        del_btn.setEnabled(task.status != TaskStatus.RUNNING)
        del_btn.setToolTip(
            "RUNNING 任务不可删；其它状态删除仅清队列行，不动 .md / 数据库"
        )
        del_btn.clicked.connect(lambda _, _t=tid: self._on_remove_task(_t))
        self.queue_table.setCellWidget(row, 3, del_btn)

    def _on_remove_task(self, tid: int) -> None:
        if self._tem is None:
            return
        if not self._tem.remove(tid):
            QMessageBox.information(
                self, "无法删除",
                "任务正在运行中，无法删除。等它跑完再试。"
            )

    def _on_clear_done(self) -> None:
        if self._tem is None:
            return
        terminal_tids = [
            t.task_id for t in self._tem.list_tasks() if t.is_terminal
        ]
        for tid in terminal_tids:
            self._tem.remove(tid)

    def _on_queue_concurrency_changed(self, n: int) -> None:
        if self._tem is None:
            return
        applied = self._tem.set_max_concurrent(n)
        if applied != n:
            self.queue_concurrency_spin.blockSignals(True)
            self.queue_concurrency_spin.setValue(applied)
            self.queue_concurrency_spin.blockSignals(False)

    def _on_queue_row_selected(self) -> None:
        rows = self.queue_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self._row_to_tid):
            self._select_task_row(self._row_to_tid[row])

    def _select_task_row(self, tid: int) -> None:
        """把 tid 任务设为左栏日志/流式回灌的目标，并自动选中表格行。"""
        if self._tem is None:
            return
        task = self._tem.get(tid)
        if task is None:
            return
        self._current_task_id = tid
        # 整段回灌
        self.extract_log.clear()
        for line in task.log_buffer:
            self.extract_log.append(line)
        self.stream_browser.clear()
        if task.stream_buffer:
            self.stream_browser.setPlainText("".join(task.stream_buffer))
            self.stream_browser.moveCursor(QTextCursor.End)
        # 同步表格选中态
        row = self._tid_to_row.get(tid)
        if row is not None and self.queue_table.currentRow() != row:
            self.queue_table.blockSignals(True)
            self.queue_table.selectRow(row)
            self.queue_table.blockSignals(False)

    def _refresh_running_elapsed(self) -> None:
        """1s tick：刷新所有 RUNNING 任务行的"用时"显示。"""
        if self._tem is None:
            return
        for tid, row in self._tid_to_row.items():
            task = self._tem.get(tid)
            if task is None or task.status != TaskStatus.RUNNING:
                continue
            it = self.queue_table.item(row, 2)
            if it is not None:
                it.setText(f"{task.status_label} / {task.elapsed_text()}")

    def _refresh_queue_hint(self) -> None:
        if self._tem is None:
            self.queue_hint_label.setText("（暂无任务）")
            return
        tasks = self._tem.list_tasks()
        if not tasks:
            self.queue_hint_label.setText("（暂无任务）")
            return
        n_pending = sum(1 for t in tasks if t.status == TaskStatus.PENDING)
        n_running = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
        n_success = sum(1 for t in tasks if t.status == TaskStatus.SUCCESS)
        n_failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
        self.queue_hint_label.setText(
            f"⌛ {n_pending} / ⏳ {n_running} / "
            f"✅ {n_success} / ❌ {n_failed}"
        )

    # ---------- 下半区：查看 ----------

    def _build_viewer_group(self) -> QGroupBox:
        group = QGroupBox("📊 已入库题材")
        layout = QVBoxLayout()

        # 单报告 lock 指示条（评估页双击进入时显示，点击解除回到日期模式）
        self.lock_indicator = QLabel("")
        self.lock_indicator.setStyleSheet(
            "background:#fffbe6;border:1px solid #ffe58f;"
            "padding:6px 10px;color:#874d00;"
        )
        self.lock_indicator.setOpenExternalLinks(False)
        self.lock_indicator.linkActivated.connect(
            lambda _href: self._release_report_lock()
        )
        self.lock_indicator.setVisible(False)
        layout.addWidget(self.lock_indicator)

        # 控制行：日期选择 + 模板筛选 + 刷新
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("报告日期:"))
        self.date_combo = QComboBox()
        self.date_combo.setStyleSheet(COMBOBOX_STYLE)
        self.date_combo.setMinimumWidth(160)
        self.date_combo.currentIndexChanged.connect(self._on_date_changed)
        ctrl.addWidget(self.date_combo)

        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("模板:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setStyleSheet(COMBOBOX_STYLE)
        self.prompt_combo.setMinimumWidth(200)
        self.prompt_combo.currentIndexChanged.connect(self._on_prompt_changed)
        ctrl.addWidget(self.prompt_combo)

        ctrl.addSpacing(20)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #8c8c8c;")
        ctrl.addWidget(self.stats_label)
        ctrl.addStretch()

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self._reload_dates)
        ctrl.addWidget(self.refresh_btn)

        self.delete_btn = QPushButton("🗑️ 删除当前报告题材")
        self.delete_btn.setStyleSheet(BUTTON_SUCCESS)
        self.delete_btn.clicked.connect(self._on_delete_current)
        ctrl.addWidget(self.delete_btn)
        layout.addLayout(ctrl)

        # 题材表
        self.theme_table = QTableWidget(0, len(_THEME_TABLE_COLS))
        self.theme_table.setHorizontalHeaderLabels([c[0] for c in _THEME_TABLE_COLS])
        self.theme_table.setStyleSheet(TABLE_STYLE)
        self.theme_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.theme_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.theme_table.setSelectionMode(QTableWidget.SingleSelection)
        self.theme_table.verticalHeader().setVisible(False)
        self.theme_table.horizontalHeader().setStretchLastSection(True)
        for i, (_, w) in enumerate(_THEME_TABLE_COLS):
            self.theme_table.setColumnWidth(i, w)
        header = self.theme_table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_theme_header_clicked)
        self.theme_table.itemSelectionChanged.connect(self._on_theme_selected)
        layout.addWidget(self.theme_table, 2)

        # 详情区（Tab）
        self.detail_tab = QTabWidget()

        self.reason_browser = QTextBrowser()
        self.reason_browser.setStyleSheet(TEXTBROWSER_STYLE)

        self.stock_table = QTableWidget(0, len(_STOCK_TABLE_COLS))
        self.stock_table.setHorizontalHeaderLabels([c[0] for c in _STOCK_TABLE_COLS])
        self.stock_table.setStyleSheet(TABLE_STYLE)
        self.stock_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stock_table.verticalHeader().setVisible(False)
        self.stock_table.horizontalHeader().setStretchLastSection(True)
        for i, (_, w) in enumerate(_STOCK_TABLE_COLS):
            self.stock_table.setColumnWidth(i, w)

        self.news_table = QTableWidget(0, len(_NEWS_TABLE_COLS))
        self.news_table.setHorizontalHeaderLabels([c[0] for c in _NEWS_TABLE_COLS])
        self.news_table.setStyleSheet(TABLE_STYLE)
        self.news_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.news_table.verticalHeader().setVisible(False)
        self.news_table.horizontalHeader().setStretchLastSection(True)
        for i, (_, w) in enumerate(_NEWS_TABLE_COLS):
            self.news_table.setColumnWidth(i, w)
        self.news_table.cellClicked.connect(self._on_news_cell_clicked)

        self.news_content_browser = QTextBrowser()
        self.news_content_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.news_content_browser.setPlaceholderText("点击左侧标题查看时间与原文")

        self.news_splitter = QSplitter(Qt.Horizontal)
        self.news_splitter.addWidget(self.news_table)
        self.news_splitter.addWidget(self.news_content_browser)
        self.news_splitter.setStretchFactor(0, 1)
        self.news_splitter.setStretchFactor(1, 1)
        self.news_splitter.setChildrenCollapsible(False)
        # 等大比例，布局完成后左右约各占一半
        self.news_splitter.setSizes([10000, 10000])

        # 打分明细 Tab：接通 scoring_service.get_theme_score_detail（2026-05-29）
        # 选中题材后渲染顶部摘要 + 逐日表（HTML）。未打分时显示空态。
        self.score_detail_browser = QTextBrowser()
        self.score_detail_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.score_detail_browser.setHtml(
            '<div style="padding:20px;color:#8c8c8c;">'
            '📊 <b>打分明细</b><br><br>'
            '请在上表选中一个题材以查看其 D+1~D+5 逐日打分。'
            '</div>'
        )

        # 板块走势 Tab：迷你折线图 + 每日明细表
        self.sector_trend_tab = self._build_sector_trend_tab()

        self.detail_tab.addTab(self.reason_browser, "📝 逻辑/原因")
        self.detail_tab.addTab(self.stock_table, "💼 关联标的")
        self.detail_tab.addTab(self.news_splitter, "📰 关联新闻")
        self.detail_tab.addTab(self.score_detail_browser, "📊 打分明细")
        self.detail_tab.addTab(self.sector_trend_tab, "📈 板块走势")
        layout.addWidget(self.detail_tab, 2)

        group.setLayout(layout)
        return group

    def _build_sector_trend_tab(self) -> QWidget:
        """构造「📈 板块走势」Tab：顶部迷你折线图 + 底部每日明细表。"""
        from PyQt5.QtWidgets import QSizePolicy
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # 顶栏：板块标识 + 全量统计
        self.trend_header = QLabel("（请在上表选中一个题材以查看其板块走势）")
        self.trend_header.setStyleSheet("color: #595959; font-weight: 600;")
        self.trend_header.setWordWrap(True)
        v.addWidget(self.trend_header)

        # 迷你折线图（高度固定，宽度跟随）
        self.trend_sparkline = SparklineWidget()
        self.trend_sparkline.setFixedHeight(120)
        self.trend_sparkline.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        v.addWidget(self.trend_sparkline)

        # 每日明细表
        self.trend_table = QTableWidget(0, len(_SECTOR_TREND_COLS))
        self.trend_table.setHorizontalHeaderLabels(
            [c[0] for c in _SECTOR_TREND_COLS]
        )
        self.trend_table.setStyleSheet(TABLE_STYLE)
        self.trend_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.trend_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.trend_table.verticalHeader().setVisible(False)
        self.trend_table.horizontalHeader().setStretchLastSection(True)
        for i, (_, w) in enumerate(_SECTOR_TREND_COLS):
            self.trend_table.setColumnWidth(i, w)
        v.addWidget(self.trend_table, 1)

        return container

    # ---------- 数据加载 ----------

    def refresh(self):
        """切到此页时刷新日期列表与表格。"""
        self._reload_dates()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _reload_dates(self, prefer_date: Optional[str] = None):
        try:
            from services.storage import get_theme_store
            from services.storage.ai_inference_db import get_ai_inference_db
        except Exception as e:
            self.stats_label.setText(f"数据库不可用: {e}")
            return

        try:
            with get_ai_inference_db().connect() as conn:
                rows = conn.execute(
                    """
                    SELECT report_date, COUNT(*) AS cnt
                    FROM theme_predictions
                    GROUP BY report_date
                    ORDER BY report_date DESC
                    """
                ).fetchall()
        except Exception as e:
            self.stats_label.setText(f"查询失败: {e}")
            return

        from gui.utils.date_format import format_yyyymmdd_to_dash
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        for r in rows:
            rd = r["report_date"]
            self.date_combo.addItem(
                f"{format_yyyymmdd_to_dash(rd)} ({r['cnt']} 条)", rd
            )

        # 模板筛选下拉重载（友好名显示，data 仍是 prompt_id）
        self.prompt_combo.blockSignals(True)
        self.prompt_combo.clear()
        self.prompt_combo.addItem("全部模板", None)
        try:
            from gui.utils.prompt_name_helper import friendly_prompt_name
            prompts = get_theme_store().list_distinct_prompts()
            for pid in prompts:
                self.prompt_combo.addItem(friendly_prompt_name(pid), pid)
        except Exception as e:
            self._log_debug(f"加载模板列表失败: {e}")
        self.prompt_combo.blockSignals(False)

        total = get_theme_store().count()
        self.stats_label.setText(
            f"全库共 {total} 条题材，{len(rows)} 个报告日期"
        )

        if prefer_date:
            for i in range(self.date_combo.count()):
                if self.date_combo.itemData(i) == prefer_date:
                    self.date_combo.setCurrentIndex(i)
                    break

        self.date_combo.blockSignals(False)

        if self.date_combo.count() > 0:
            self._reload_themes_for_date(self.date_combo.currentData())
        else:
            self._current_themes = []
            self.theme_table.setRowCount(0)
            self._clear_detail()

    def _on_date_changed(self, _idx: int):
        # 用户切日期 → 自动解锁，回到普通浏览
        if self._locked_report_id is not None:
            self._release_report_lock(reload=False)
        date = self.date_combo.currentData()
        if date:
            self._reload_themes_for_date(date)

    def _on_prompt_changed(self, _idx: int):
        # 用户切模板 → 自动解锁
        if self._locked_report_id is not None:
            self._release_report_lock(reload=False)
        date = self.date_combo.currentData()
        if date:
            self._reload_themes_for_date(date)

    def _reload_themes_for_date(self, report_date: str):
        # lock 模式下 date/prompt 切换不应触发覆盖（load_by_report_id 接管）
        if self._locked_report_id is not None:
            return
        from services.storage import get_theme_store
        prompt_id = self.prompt_combo.currentData()
        themes = get_theme_store().get_by_date(
            report_date, prompt_id=prompt_id
        )
        self._reset_theme_sort()
        self._current_themes = themes
        self._render_theme_table(themes)
        self._clear_detail()

    def load_by_report_id(
        self,
        ai_report_id: int,
        *,
        hint_date: str = "",
        hint_prompt: str = "",
    ) -> None:
        """按 ``ai_reports.id`` 精确加载单份报告题材并进入 lock 模式。

        业务定位：
            评估页双击某行报告时，主窗口透传 ai_report_id 调本方法；
            解决同日同模板多份报告题材混在一张表里的痛点。

        Args:
            ai_report_id: ``ai_reports.id``（int，>0）
            hint_date: 该报告的 report_date（用于把 date_combo 同步过去，
                解锁后用户能继续看全天题材）；空串则不动 combo
            hint_prompt: 该报告的 prompt_id（同上，为 prompt_combo 提供提示）

        实现要点：
            * 设 _locked_report_id 标记 → _reload_themes_for_date 在 lock
              下短路，避免被切 combo 触发重渲染
            * 同步 combo 用 blockSignals 防循环触发
            * 锁定期间禁用「删除当前报告题材」按钮（语义是按日期+模板删，
              与 lock 模式语义不一致，避免误删）
        """
        from services.storage import get_theme_store
        try:
            themes = get_theme_store().get_by_ai_report_id(int(ai_report_id))
        except Exception as exc:  # noqa: BLE001
            self.lock_indicator.setText(
                f'<span style="color:#c00;">'
                f'加载 ai_report_id={ai_report_id} 失败：{exc}'
                f'</span>'
            )
            self.lock_indicator.setVisible(True)
            return

        self._locked_report_id = int(ai_report_id)

        # 同步 combo（不触发槽，避免立即解锁）
        if hint_date:
            for combo in (self.date_combo,):
                combo.blockSignals(True)
                for i in range(combo.count()):
                    if combo.itemData(i) == hint_date:
                        combo.setCurrentIndex(i)
                        break
                combo.blockSignals(False)
        if hint_prompt:
            self.prompt_combo.blockSignals(True)
            for i in range(self.prompt_combo.count()):
                if self.prompt_combo.itemData(i) == hint_prompt:
                    self.prompt_combo.setCurrentIndex(i)
                    break
            self.prompt_combo.blockSignals(False)

        # 渲染锁指示条 + 题材表 + 清空详情
        from gui.utils.date_format import format_yyyymmdd_to_dash
        from gui.utils.prompt_name_helper import friendly_prompt_name
        date_show = (
            format_yyyymmdd_to_dash(hint_date) if hint_date else "—"
        )
        prompt_show = (
            friendly_prompt_name(hint_prompt, fallback=hint_prompt or "—")
            if hint_prompt else "—"
        )
        self.lock_indicator.setText(
            f'🔒 已锁定单报告浏览：'
            f'<b>ai_reports.id={ai_report_id}</b>　'
            f'<span style="color:#8c8c8c;">'
            f'{date_show}　{prompt_show}　'
            f'共 {len(themes)} 个题材</span>　'
            f'<a href="#release" style="color:#1890ff;">[✕ 解除]</a>'
        )
        self.lock_indicator.setVisible(True)

        self._reset_theme_sort()
        self._current_themes = themes
        self._render_theme_table(themes)
        self._clear_detail()
        # 锁定模式下禁用"按日期+模板"语义的删除按钮
        if hasattr(self, "delete_btn"):
            self.delete_btn.setEnabled(False)

    def _release_report_lock(self, reload: bool = True) -> None:
        """解除单报告锁定，回到「日期+模板」浏览模式。

        Args:
            reload: True = 按当前 date/prompt 立即重渲染；False = 由调用方
                后续触发（避免双重 reload）
        """
        if self._locked_report_id is None:
            return
        self._locked_report_id = None
        self.lock_indicator.setVisible(False)
        self.lock_indicator.setText("")
        if hasattr(self, "delete_btn"):
            self.delete_btn.setEnabled(True)
        if reload:
            date = self.date_combo.currentData()
            if date:
                self._reload_themes_for_date(date)

    @staticmethod
    def _log_debug(msg: str) -> None:
        """调试日志（避免污染主日志区）。"""
        import logging
        logging.getLogger(__name__).debug(msg)

    def _reset_theme_sort(self):
        """切换日期或刷新时恢复数据库默认顺序。"""
        self._sort_col = None
        self._sort_asc = True
        header = self.theme_table.horizontalHeader()
        header.setSortIndicator(-1, Qt.AscendingOrder)

    def _on_theme_header_clicked(self, col: int):
        """点击可排序列：同列切换升降序，新列按业务默认方向。"""
        if col not in _SORTABLE_COLS:
            return
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            # 排名默认升序（1 在前）；等级/分数/预期差默认降序（高在前）
            self._sort_asc = col == 8
        self._apply_theme_sort()
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self.theme_table.horizontalHeader().setSortIndicator(col, order)

    def _apply_theme_sort(self):
        if self._sort_col is None or not self._current_themes:
            return
        field = _SORTABLE_COLS[self._sort_col]
        asc = self._sort_asc

        def sort_key(theme: dict):
            if field == "strength_level":
                val = _LEVEL_ORDER.get(theme.get("strength_level") or "", -1)
                return val if asc else -val
            if field == "strength_score":
                score = theme.get("strength_score")
                val = score if score is not None else -1
                return val if asc else -val
            if field == "expectation_gap":
                val = _GAP_ORDER.get(theme.get("expectation_gap") or "", -1)
                return val if asc else -val
            rank = theme.get("priority_rank")
            if rank is None:
                return (1, 0)
            return (0, rank if asc else -rank)

        self._current_themes = sorted(self._current_themes, key=sort_key)
        self._render_theme_table(self._current_themes)

    def _render_theme_table(self, themes: list):
        from gui.utils.prompt_name_helper import friendly_prompt_name
        from services.market.sector_daily_query import get_sector_names

        # 批量反查 dim_sector 中文名（market.db 跨库一次，避免逐行查）
        name_map = get_sector_names(
            [t.get("sector_ts_code") or "" for t in themes]
        )

        self.theme_table.setRowCount(len(themes))
        for row, t in enumerate(themes):
            score = t.get("strength_score")
            sector_code = t.get("sector_ts_code") or ""
            sector_conf = t.get("sector_match_conf")
            sector_name = name_map.get(sector_code, "")
            sector_text = self._format_sector_cell(
                sector_name, sector_code, sector_conf
            )
            cells = [
                t.get("report_time") or "",
                t.get("theme_name") or "",
                t.get("strength_level") or "",
                self._format_score(score),
                sector_text,
                friendly_prompt_name(t.get("prompt_id"), fallback="—"),
                "❄️" if t.get("is_cold") else "",
                t.get("duration") or "",
                t.get("expectation_gap") or "",
                str(t.get("priority_rank") or ""),
                t.get("reason") or "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 2:  # 等级
                    item.setForeground(self._level_color(text))
                elif col == 3:  # 分数（按正负染色）
                    item.setForeground(self._score_color(score))
                elif col == 4 and sector_code and sector_conf is not None \
                        and sector_conf < _SECTOR_WEAK_MATCH_CONF:
                    # 板块匹配置信度低 → 标橙提示需人工确认（与评估页 ⚠ 同阈值）
                    item.setForeground(QColor("#fa8c16"))
                self.theme_table.setItem(row, col, item)

    @staticmethod
    def _format_sector_cell(name: str, code: str, conf) -> str:
        """板块单元格文案：``{中文名} ({代码} 0.95)``。

        - 三者都有 → ``酿酒概念 (BK0477.DC 0.95)``
        - 无中文名（dim_sector 未命中） → ``BK0477.DC (0.95)``（旧格式兜底）
        - 无 conf → ``酿酒概念 (BK0477.DC)`` / ``BK0477.DC``
        - 无 code → 空串
        """
        if not code:
            return ""
        conf_part = f"{conf:.2f}" if conf is not None else ""
        if name:
            return (
                f"{name} ({code} {conf_part})" if conf_part
                else f"{name} ({code})"
            )
        return f"{code} ({conf_part})" if conf_part else code

    @staticmethod
    def _format_score(score) -> str:
        """带符号显示：+75 / -50 / 0；None 显示空串。"""
        if score is None:
            return ""
        if isinstance(score, int):
            return f"{score:+d}" if score != 0 else "0"
        try:
            n = int(score)
            return f"{n:+d}" if n != 0 else "0"
        except (TypeError, ValueError):
            return str(score)

    @staticmethod
    def _level_color(text: str):
        """9 档等级色（利多红橙暖，利空蓝绿冷，中性灰）。"""
        return {
            "重大利多": QColor("#cf1322"),
            "较强利多": QColor("#fa541c"),
            "弱利多": QColor("#fa8c16"),
            "中性偏多": QColor("#faad14"),
            "中性": QColor("#8c8c8c"),
            "中性偏空": QColor("#13c2c2"),
            "弱利空": QColor("#1890ff"),
            "较强利空": QColor("#2f54eb"),
            "重大利空": QColor("#722ed1"),
        }.get(text, QColor("#262626"))

    @staticmethod
    def _score_color(score):
        """分数染色：正数红、负数蓝、零灰；强度越大颜色越深。"""
        if score is None:
            return QColor("#262626")
        try:
            s = int(score)
        except (TypeError, ValueError):
            return QColor("#262626")
        if s == 0:
            return QColor("#8c8c8c")
        if s >= 80:
            return QColor("#cf1322")
        if s >= 40:
            return QColor("#fa8c16")
        if s >= 1:
            return QColor("#faad14")
        if s >= -39:
            return QColor("#13c2c2")
        if s >= -79:
            return QColor("#1890ff")
        return QColor("#722ed1")

    def _on_theme_selected(self):
        rows = self.theme_table.selectionModel().selectedRows()
        if not rows:
            self._clear_detail()
            return
        idx = rows[0].row()
        if 0 <= idx < len(self._current_themes):
            self._show_theme_detail(self._current_themes[idx])

    def _show_theme_detail(self, theme: dict):
        parts = []
        if theme.get("reason"):
            parts.append(f"### 核心逻辑（含催化事件）\n\n{theme['reason']}")
        if theme.get("risk_note"):
            parts.append(f"### ⚠️ 风险提示\n\n{theme['risk_note']}")
        if theme.get("theme_category"):
            parts.append(f"**大类**: {theme['theme_category']}")
        self.reason_browser.setMarkdown("\n\n".join(parts) or "（无）")

        stocks = theme.get("stocks") or []
        self.stock_table.setRowCount(len(stocks))
        for r, s in enumerate(stocks):
            raw_code = s.get("stock_code") or ""
            norm = s.get("normalized_code") or ""
            cells = [
                s.get("stock_name") or "",
                raw_code,
                norm or ("⚠️未匹配" if raw_code else ""),
                s.get("role") or "",
                s.get("reason") or "",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if c == 2 and not norm and raw_code:
                    item.setForeground(QColor("#fa8c16"))
                self.stock_table.setItem(r, c, item)

        news = theme.get("news") or []
        # 反查 raw_news 拿 title / source / published_at
        news_meta = self._fetch_news_meta(
            [n.get("news_id") for n in news if n.get("news_id")]
        )
        self.news_table.setRowCount(len(news))
        for r, n in enumerate(news):
            news_id = n.get("news_id") or ""
            meta = news_meta.get(news_id, {})
            title_text = meta.get("title") or "（未反查到，可能 news_id 缺失）"
            cells = [
                n.get("news_ref") or "",
                n.get("relation_type") or "",
                meta.get("published_at") or "",
                meta.get("source") or "",
                title_text,
            ]
            detail_payload = {
                "news_id": news_id,
                "published_at": meta.get("published_at") or "",
                "content": meta.get("content") or "",
            }
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(
                    "点击查看时间与原文" if c == _NEWS_TITLE_COL else text
                )
                if c == _NEWS_TITLE_COL:
                    item.setForeground(QColor("#1890ff"))
                    item.setData(Qt.UserRole, detail_payload)
                self.news_table.setItem(r, c, item)

        # 打分明细 Tab：用 theme.id 反查 theme_prediction_scores
        self._render_score_detail(theme)

        # 板块走势 Tab：用题材的 sector_ts_code + report_date 刷新
        self._reload_sector_trend(
            sector_ts_code=theme.get("sector_ts_code") or "",
            sector_name=theme.get("sector_name") or "",
            anchor_report_date=theme.get("report_date") or "",
            theme_name=theme.get("theme_name") or "",
        )

    def _on_news_cell_clicked(self, row: int, col: int):
        """点击标题列 → 右侧展示时间与原文。"""
        if col != _NEWS_TITLE_COL:
            return
        item = self.news_table.item(row, col)
        if not item:
            return
        payload = item.data(Qt.UserRole)
        if not isinstance(payload, dict):
            return
        self.news_content_browser.setHtml(self._format_news_detail_html(payload))

    @staticmethod
    def _escape_html(text) -> str:
        return (
            str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

    @classmethod
    def _format_news_detail_html(cls, payload: dict) -> str:
        time_text = (payload.get("published_at") or "").strip() or "（未知）"
        content = (payload.get("content") or "").strip()
        if content:
            body_text = content
        elif payload.get("news_id"):
            body_text = "（数据库中暂无正文）"
        else:
            body_text = "（未关联 news_id，无法反查正文）"

        rows = [("时间", time_text), ("原文", body_text)]
        parts = ['<div style="font-family: sans-serif; line-height: 1.6;">']
        for label, value in rows:
            parts.append(
                f'<p><b>{cls._escape_html(label)}</b><br>'
                f'{cls._escape_html(value)}</p>'
            )
        parts.append("</div>")
        return "".join(parts)

    @staticmethod
    def _fetch_news_meta(news_ids: list) -> dict:
        """通过 news_id 从 raw_news 反查 title/source/published_at/content。

        注意：raw_news 表在 ``news.db``，不在 ``ai_inference.db``。
        Phase -1 三库重构曾把这里误改成 ai_inference 连接，导致 GUI
        新闻反查全部走空（2026-05-27 bug 修复）。
        """
        ids = [i for i in news_ids if i]
        if not ids:
            return {}
        try:
            from services.storage.database import get_connection
            placeholders = ",".join("?" for _ in ids)
            with get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, title, content, source, published_at
                    FROM raw_news
                    WHERE id IN ({placeholders})
                    """,
                    ids,
                ).fetchall()
        except Exception:
            return {}
        return {
            r["id"]: {
                "title": r["title"],
                "content": r["content"],
                "source": r["source"],
                "published_at": r["published_at"],
            }
            for r in rows
        }

    def _clear_detail(self):
        self.reason_browser.clear()
        self.stock_table.setRowCount(0)
        self.news_table.setRowCount(0)
        self.news_content_browser.clear()
        # 打分明细 Tab 回到空态
        self.score_detail_browser.setHtml(
            '<div style="padding:20px;color:#8c8c8c;">'
            '📊 <b>打分明细</b><br><br>'
            '请在上表选中一个题材以查看其 D+1~D+5 逐日打分。'
            '</div>'
        )
        # 板块走势 Tab 一并清空
        self.trend_header.setText("（请在上表选中一个题材以查看其板块走势）")
        self.trend_sparkline.clear()
        self.trend_table.setRowCount(0)

    # ---------- 打分明细 Tab ----------

    def _render_score_detail(self, theme: dict) -> None:
        """渲染选中题材的 D+1~D+5 逐日打分到「📊 打分明细」Tab。

        数据源：``scoring_service.get_theme_score_detail(theme_id)`` →
        ``theme_prediction_scores`` 表行（已按 score_date / days_offset 升序）。

        渲染结构（setHtml 单 widget，KISS 避免再造一个表格 widget）：

        * 顶部摘要：题材名 / 报告日 / 强度 / 已打分天数 / 平均命中率 / 累计方向
        * 中部表格：偏移 / 交易日 / 板块% / 题材% / 中证1000% / α+N / 命中率 / 累计方向

        空态分两种：
        * ``theme.id`` 缺失（极端老数据，几乎不会发生）→ 提示数据异常
        * 表为空 → 提示「该题材尚未打分，去评估页一键打分」
        """
        theme_id = theme.get("id")
        if theme_id is None:
            self.score_detail_browser.setHtml(
                '<div style="padding:20px;color:#c00;">'
                '题材 id 缺失，无法反查打分明细（数据异常）'
                '</div>'
            )
            return

        try:
            from services.scoring.scoring_service import (
                get_theme_score_detail,
            )
            rows = get_theme_score_detail(int(theme_id))
        except Exception as exc:  # noqa: BLE001
            self.score_detail_browser.setHtml(
                f'<div style="padding:20px;color:#c00;">'
                f'查询打分明细失败：{exc}'
                f'</div>'
            )
            return

        theme_name = theme.get("theme_name") or "—"
        report_date = theme.get("report_date") or "—"
        strength_score = theme.get("strength_score")
        strength_level = theme.get("strength_level") or "—"

        if not rows:
            self.score_detail_browser.setHtml(
                '<div style="padding:20px;color:#8c8c8c;">'
                f'📊 <b>{theme_name}</b>（{report_date}）<br><br>'
                '该题材尚未打分。<br><br>'
                '<span style="color:#1890ff;">'
                '👉 切到「📊 模板评估」页 → 选中对应报告 → '
                '点行内「⚡初次打分」按钮，即可生成 D+1~D+5 追踪数据。'
                '</span>'
                '</div>'
            )
            return

        # 顶部摘要计算
        hit_rates = [
            r["hit_rate"] for r in rows
            if r.get("hit_rate") is not None
        ]
        avg_hr = sum(hit_rates) / len(hit_rates) if hit_rates else None
        # direction_correct 是累计判定（05-29 算法重设），取最后一天的值即可
        last_dir = rows[-1].get("direction_correct") if rows else None

        head_lines = [
            f'<span style="font-size:14px;font-weight:600;">'
            f'📊 {theme_name}</span>',
            f'<span style="color:#595959;">　报告日 {report_date}</span>',
        ]
        if strength_score is not None:
            head_lines.append(
                f'<span style="color:#595959;">　强度 '
                f'{int(strength_score):+d}（{strength_level}）</span>'
            )
        head_lines.append(
            f'<span style="color:#595959;">　已打分 {len(rows)} 天</span>'
        )
        if avg_hr is not None:
            head_lines.append(
                f'<span style="color:#595959;">　平均命中率 '
                f'<b>{avg_hr * 100:.1f}%</b></span>'
            )
        if last_dir is not None:
            dir_text = (
                '<span style="color:#cf1322;">✓ 正确</span>'
                if int(last_dir) == 1
                else '<span style="color:#389e0d;">✗ 不准</span>'
            )
            head_lines.append(
                f'<span style="color:#595959;">　累计方向 {dir_text}</span>'
            )
        header_html = '<div style="margin-bottom:10px;">' + ''.join(
            head_lines
        ) + '</div>'

        # 逐日表
        thead = (
            '<tr style="background:#fafafa;">'
            '<th style="padding:4px 8px;text-align:right;">偏移</th>'
            '<th style="padding:4px 8px;">交易日</th>'
            '<th style="padding:4px 8px;text-align:right;">板块%</th>'
            '<th style="padding:4px 8px;text-align:right;">题材%</th>'
            '<th style="padding:4px 8px;text-align:right;">中证1000%</th>'
            '<th style="padding:4px 8px;text-align:right;">α+N</th>'
            '<th style="padding:4px 8px;text-align:right;">命中率</th>'
            '<th style="padding:4px 8px;text-align:center;">累计方向</th>'
            '</tr>'
        )

        body_rows = []
        for r in rows:
            offset = r.get("days_offset")
            sd = r.get("score_date") or ""
            sec = r.get("sector_pct")
            tpct = r.get("theme_pct")
            zz = r.get("benchmark_zz1000_pct")
            alpha = (
                tpct - zz if tpct is not None and zz is not None else None
            )
            hr = r.get("hit_rate")
            hc = r.get("hit_count")
            tc = r.get("total_count")
            dc = r.get("direction_correct")

            body_rows.append(
                '<tr>'
                f'<td style="padding:3px 8px;text-align:right;">D+{offset}</td>'
                f'<td style="padding:3px 8px;color:#8c8c8c;">{sd}</td>'
                f'<td style="padding:3px 8px;text-align:right;">'
                f'{self._fmt_pct_colored(sec)}</td>'
                f'<td style="padding:3px 8px;text-align:right;">'
                f'{self._fmt_pct_colored(tpct)}</td>'
                f'<td style="padding:3px 8px;text-align:right;color:#8c8c8c;">'
                f'{self._fmt_pct_plain(zz)}</td>'
                f'<td style="padding:3px 8px;text-align:right;">'
                f'{self._fmt_pct_colored(alpha)}</td>'
                f'<td style="padding:3px 8px;text-align:right;">'
                f'{self._fmt_hit(hr, hc, tc)}</td>'
                f'<td style="padding:3px 8px;text-align:center;">'
                f'{self._fmt_direction(dc)}</td>'
                '</tr>'
            )

        table_html = (
            '<table border="0" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;width:100%;font-size:12px;">'
            + thead + ''.join(body_rows) + '</table>'
        )

        # AI 评分员评语（如有，挂在尾部）
        ai_comments = [
            (r.get("days_offset"), r.get("ai_review_label"),
             r.get("ai_review_comment"))
            for r in rows
            if r.get("ai_review_label") or r.get("ai_review_comment")
        ]
        ai_html = ""
        if ai_comments:
            ai_lines = []
            for off, lbl, cmt in ai_comments:
                ai_lines.append(
                    f'<div style="margin-top:6px;">'
                    f'<b>D+{off} {lbl or ""}</b>：'
                    f'<span style="color:#595959;">{cmt or ""}</span>'
                    f'</div>'
                )
            ai_html = (
                '<div style="margin-top:14px;padding-top:8px;'
                'border-top:1px solid #f0f0f0;">'
                '<div style="color:#1890ff;font-weight:600;">'
                '🤖 AI 评分员评语</div>'
                + ''.join(ai_lines) + '</div>'
            )

        self.score_detail_browser.setHtml(
            '<div style="padding:8px 12px;">'
            + header_html + table_html + ai_html
            + '</div>'
        )

    @staticmethod
    def _fmt_pct_colored(v) -> str:
        if v is None:
            return '<span style="color:#bfbfbf;">—</span>'
        try:
            f = float(v)
        except (TypeError, ValueError):
            return '<span style="color:#bfbfbf;">—</span>'
        color = "#cf1322" if f > 0 else ("#389e0d" if f < 0 else "#595959")
        return f'<span style="color:{color};">{f:+.2f}%</span>'

    @staticmethod
    def _fmt_pct_plain(v) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):+.2f}%"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_hit(hr, hc, tc) -> str:
        if hr is None and (hc is None or tc is None):
            return '<span style="color:#bfbfbf;">—</span>'
        if tc is None or int(tc) == 0:
            return '<span style="color:#bfbfbf;">无标的</span>'
        try:
            rate = float(hr) * 100 if hr is not None else 0.0
        except (TypeError, ValueError):
            rate = 0.0
        color = "#cf1322" if rate >= 50 else "#595959"
        return (
            f'<span style="color:{color};">'
            f'{int(hc or 0)}/{int(tc)} ({rate:.0f}%)</span>'
        )

    @staticmethod
    def _fmt_direction(v) -> str:
        if v is None:
            return '<span style="color:#bfbfbf;">—</span>'
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return '<span style="color:#bfbfbf;">—</span>'
        if iv == 1:
            return '<span style="color:#cf1322;">✓</span>'
        return '<span style="color:#389e0d;">✗</span>'

    # ---------- 板块走势 Tab ----------

    def _reload_sector_trend(
        self,
        *,
        sector_ts_code: str,
        sector_name: str,
        anchor_report_date: str,
        theme_name: str,
    ) -> None:
        """根据选中题材的板块代码刷新折线图 + 明细表。

        无 ``sector_ts_code`` 时清空并提示「题材未匹配到板块」。

        Args:
            sector_ts_code: 题材关联板块 ts_code（如 ``BK0477.DC``）
            sector_name:    板块名（``theme_predictions`` 没冗余存板块名，传 ``""``
                            即可，查询时会从 ``dim_sector`` 反查）
            anchor_report_date: 报告日 YYYYMMDD（折线图竖线锚点）
            theme_name:     题材名（仅用于顶栏展示）
        """
        if not sector_ts_code:
            self.trend_header.setText(
                f"题材「{theme_name or '—'}」未匹配到板块代码，"
                f"无走势可展示"
            )
            self.trend_sparkline.clear()
            self.trend_table.setRowCount(0)
            return

        try:
            from services.market.sector_daily_query import (
                get_sector_daily_history,
            )
            history = get_sector_daily_history(
                sector_ts_code, order="asc"
            )
        except Exception as exc:  # noqa: BLE001
            self.trend_header.setText(
                f"<span style='color:#c00'>板块行情查询失败：{exc}</span>"
            )
            self.trend_sparkline.clear()
            self.trend_table.setRowCount(0)
            return

        display_name = history.name or sector_name or "—"
        if history.count == 0:
            # 两种情况分别提示
            if not history.in_dim:
                # 字典都没——多半 sector_ts_code 写错了 / dim_sector 未同步
                self.trend_header.setText(
                    f"<span style='color:#c00'>板块代码 "
                    f"<b>{sector_ts_code}</b> 不存在于板块字典 "
                    f"dim_sector</span><br>"
                    f"<span style='color:#8c8c8c;'>"
                    f"多半是题材 matcher 写入了错误的 ts_code，"
                    f"或 dim_sector 未同步最新板块（跑 tushare_fetcher "
                    f"_fetch_sector_dict）"
                    f"</span>"
                )
            else:
                # 字典有名字，但 fact 表无行——典型的数据源未覆盖
                self.trend_header.setText(
                    f"<b>{display_name}</b>　({sector_ts_code})　"
                    f"<span style='color:#c80'>"
                    f"类型: {history.idx_type or '—'}"
                    f"</span><br>"
                    f"<span style='color:#8c8c8c;'>"
                    f"⚠️ 该板块在 dim_sector 字典里有名字，但 "
                    f"fact_sector_daily 没数据。<br>"
                    f"根因：Tushare <code>moneyflow_ind_dc</code> "
                    f"接口只返回<b>概念板块</b>的资金流，"
                    f"<b>行业板块</b>（如稀土 / 钴 / 镍 / 被动元件 等）"
                    f"不返。<br>"
                    f"修复路径：主线后续补抓 "
                    f"<code>moneyflow_ind_ths</code>（同花顺行业）或 "
                    f"<code>moneyflow_ind_sw</code>（申万行业）"
                    f"接入 fact_sector_daily，本 Tab 自动有数据。"
                    f"</span>"
                )
            self.trend_sparkline.clear()
            self.trend_table.setRowCount(0)
            return

        # ---- 顶栏统计 ----
        latest = history.rows[-1]
        earliest = history.rows[0]
        idx_type_tag = (
            f"<span style='color:#1890ff;'>[{history.idx_type}]</span>　"
            if history.idx_type else ""
        )
        self.trend_header.setText(
            f"<b>{display_name}</b>　({sector_ts_code})　"
            f"{idx_type_tag}"
            f"<span style='color:#8c8c8c;'>"
            f"题材「{theme_name or '—'}」　|　"
            f"全量 {history.count} 个交易日（"
            f"{self._fmt_date_dash(earliest.trade_date)} ~ "
            f"{self._fmt_date_dash(latest.trade_date)}）　|　"
            f"最新涨跌 <b>{self._fmt_pct(latest.pct_chg)}</b>"
            f"</span>"
        )

        # ---- 折线图（升序数据） ----
        points = [(r.trade_date, r.pct_chg) for r in history.rows]
        self.trend_sparkline.set_data(
            points,
            anchor_date=anchor_report_date or None,
            eval_days=_TREND_EVAL_DAYS,
        )

        # ---- 表格（降序展示：最新在前） ----
        rows_desc = list(reversed(history.rows))
        self._render_sector_trend_table(rows_desc, anchor_report_date)

    def _render_sector_trend_table(
        self,
        rows: list,
        anchor_report_date: str,
    ) -> None:
        """填充板块走势明细表。

        渲染规则:
            * 报告日 D 那行底色 = 浅橙（#fff7e6）
            * 报告日 +1 ~ +5 评估窗口底色 = 浅黄（#feffe6）
            * 涨跌幅 / 主力净流入按正负染色（红涨绿跌）
        """
        from gui.utils.date_format import format_yyyymmdd_to_dash

        self.trend_table.setRowCount(len(rows))

        # 计算偏移：报告日 D 在 rows 中的位置；rows 是降序的（新→旧）
        # 但偏移仍按交易日相对 anchor 的位置
        anchor_idx_in_desc = None
        if anchor_report_date:
            for i, r in enumerate(rows):
                if r.trade_date == anchor_report_date:
                    anchor_idx_in_desc = i
                    break

        for r_idx, row in enumerate(rows):
            # 偏移文本：D / D+N / D-N
            if anchor_idx_in_desc is None:
                offset_text = ""
            else:
                # rows 是降序：anchor 之前（更新）的是 D+N，之后（更旧）是 D-N
                delta = anchor_idx_in_desc - r_idx
                if delta == 0:
                    offset_text = "D"
                elif delta > 0:
                    offset_text = f"D+{delta}"
                else:
                    offset_text = f"D{delta}"

            cells = [
                format_yyyymmdd_to_dash(row.trade_date),
                offset_text,
                self._fmt_pct(row.pct_chg),
                self._fmt_net_yi(row.main_net_yi),
                self._fmt_int(row.rank_today),
            ]

            # 行底色规则
            row_bg: Optional[QColor] = None
            if anchor_idx_in_desc is not None:
                delta = anchor_idx_in_desc - r_idx
                if delta == 0:
                    row_bg = QColor("#fff7e6")  # D 报告日：浅橙
                elif 1 <= delta <= _TREND_EVAL_DAYS:
                    row_bg = QColor("#feffe6")  # D+1~D+5：浅黄

            for c_idx, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if c_idx == 2:  # 涨跌幅染色
                    item.setForeground(self._pct_color(row.pct_chg))
                elif c_idx == 3:  # 主力净流入：>0 红 / <0 绿
                    item.setForeground(self._pct_color(row.main_net_yi))
                if row_bg is not None:
                    item.setBackground(row_bg)
                self.trend_table.setItem(r_idx, c_idx, item)

    @staticmethod
    def _fmt_pct(v) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):+.2f}%"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_net_yi(v) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):+.2f}"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_int(v) -> str:
        if v is None:
            return "—"
        try:
            return str(int(v))
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_date_dash(yyyymmdd: str) -> str:
        if isinstance(yyyymmdd, str) and len(yyyymmdd) == 8 and yyyymmdd.isdigit():
            return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
        return yyyymmdd or "—"

    @staticmethod
    def _pct_color(v) -> QColor:
        """正红负绿零灰（与 _score_color 同色系，配 A 股习惯）。"""
        if v is None:
            return QColor("#8c8c8c")
        try:
            f = float(v)
        except (TypeError, ValueError):
            return QColor("#8c8c8c")
        if f > 0:
            return QColor("#cf1322")
        if f < 0:
            return QColor("#389e0d")
        return QColor("#8c8c8c")

    # ---------- 删除 ----------

    def _on_delete_current(self):
        rows = self.theme_table.selectionModel().selectedRows()
        if not rows or not self._current_themes:
            QMessageBox.warning(self, "提示", "请先选中一行题材")
            return
        idx = rows[0].row()
        if not (0 <= idx < len(self._current_themes)):
            return
        report_id = self._current_themes[idx].get("report_id")
        if not report_id:
            QMessageBox.warning(self, "提示", "无法定位报告 ID")
            return

        reply = QMessageBox.question(
            self,
            "确认",
            f"将删除报告「{report_id}」的全部题材（含关联标的与新闻），不可撤销。\n确定继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        from services.storage import get_theme_store
        removed = get_theme_store().delete_by_report(report_id)
        QMessageBox.information(self, "成功", f"已删除 {removed} 条主表记录")
        self._reload_dates()
