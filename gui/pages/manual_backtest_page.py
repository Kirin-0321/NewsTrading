"""手动回测页面（Phase 6 配套 GUI + 2026-05-27 多任务队列并行改造）。

业务定位
--------
让主人在 GUI 上手选「模板 / 交易日 / 新闻时间窗 / 新闻状态」一键跑单次
虚拟回测，**支持连点 N 次排队、后台 8 并发、点哪个任务看哪个**：

1. 时间边界预览（snapshot_inspect 同款，包含 D+1~D+5）
2. dry-run 试算（不调 LLM，看 snapshot 通不通）
3. 正式跑（异步调 ``tools.backtest_prompt.backtest_one``）
4. 任务列表实时看每个任务状态/用时；点行 → 下方三块联动切换
5. 结果：md 内容预览 + 题材列表 + 完整流式日志

对应规约：
* ``doc/design/05-27-1515-回测时间边界说明书.md``（时间窗语义）
* ``doc/design/05-27-2101-手动回测任务队列并行设计.md``（队列并行设计）
* ``doc/design/05-27-2110-手动回测任务队列并行施工方案.md``（本次施工方案）

时间窗主人语义
--------------
* 左边界（**自由可调**）默认 = trade_date 14:00
* 右边界（**只可向左调，硬上限**）默认 = next_trade_date 09:00
* 「精选新闻」默认勾选（``curated``）；取消勾选 = ``raw_news`` 全部

调用链（2026-05-27 改造后）
------------------------
::

    ManualBacktestPage
       ├── _refresh_preview()     →  inspect_snapshot(...)（同步）
       └── _on_run_clicked()
              └── self.manager.enqueue(task_kwargs) → task_id
                     ├── 队列空位 → 立即起 ManualBacktestWorker
                     └── 队列满（8 并发）→ PENDING 排队等

    BacktestTaskManager
       ├── task_added/started/progress/streaming/finished/removed signal
       └── 槽函数把 chunk/log 写到 task.stream_buffer/log_buffer
              → 当前选中行的任务实时刷下方三块；其它任务安静累积

注意事项
--------
* trade_date 限制 ≤ 今天（QDateEdit.setMaximumDate(today)）
* 右边界 QDateTimeEdit.setMaximumDateTime(next_open 09:00) 兜底
* 删除已完成任务**只删 GUI 行**，不动 md/db（那是「评估页·删除」的职责）
* 关 GUI 时 manager.shutdown() 取消 PENDING，RUNNING 自然结束
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt5.QtCore import QDate, QDateTime, Qt, QTimer
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDateTimeEdit,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, INPUT_STYLE, TABLE_STYLE, TEXTBROWSER_STYLE,
)
from gui.workers.manual_backtest_manager import (
    BacktestTask, BacktestTaskManager, STATUS_COLOR, STATUS_LABEL,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# 主页面
# ---------------------------------------------------------------------------


class ManualBacktestPage(QWidget):
    """手动回测页面：选模板/时间窗/新闻状态，一键跑单次虚拟回测。"""

    def __init__(self):
        super().__init__()
        self._suppress_window_signal = False
        self._suppress_template_signal = False
        self._current_task_id: Optional[int] = None
        self._theme_section_marked_for_task: dict[int, bool] = {}
        # 当前交易日范围解析结果（YYYYMMDD 列表，升序，已排除今天）
        self._current_dates_cache: List[str] = []
        self.manager = BacktestTaskManager(self)
        self.init_ui()
        # spin 与 manager 默认值都是 8；如以后 manager 默认变了，这里同步
        if self.concurrency_spin.value() != self.manager.get_max_concurrent():
            self.concurrency_spin.setValue(
                self.manager.get_max_concurrent()
            )
        self.load_template_list()
        self._refresh_dates_count()
        self._apply_default_window()
        self._refresh_preview()
        self._wire_manager_signals()
        self._start_elapsed_timer()

    # =====================================================================
    # UI 构建
    # =====================================================================

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        title = QLabel("🎛️ 手动回测")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "勾选 (模板 ✓多选 / 交易日范围 / 新闻状态) → 一键批量跑回测。"
            "任务数 = 模板数 × 交易日数。默认窗 = 14:00 ~ next_open 09:00。"
            "单日时可微调时间窗；多日批量强制走默认窗。"
        )
        subtitle.setStyleSheet("color: #666; font-size: 12px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_config_group())

        # 中段：横向 splitter「时间边界预览（左）｜任务列表（右）」
        # 复用「时间预览」右边的空白区，主人点哪个任务下面三块联动切换
        middle_split = QSplitter(Qt.Horizontal)
        middle_split.addWidget(self._build_preview_group())
        middle_split.addWidget(self._build_task_list_group())
        middle_split.setStretchFactor(0, 1)
        middle_split.setStretchFactor(1, 1)
        layout.addWidget(middle_split, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_md_group())
        splitter.addWidget(self._build_themes_group())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 4)

    def _build_config_group(self) -> QGroupBox:
        """回测参数面板：左右两栏布局（2026-05-28 17:55 重构）。

        左栏 ≈ 1/3 宽：模板多选列表 + 全选/全不选/已选 N 按钮组
        右栏 ≈ 2/3 宽：交易日范围 / 时间窗 / 并发 + 操作按钮（垂直排）
        """
        group = QGroupBox("回测参数")
        outer = QHBoxLayout(group)
        outer.setSpacing(12)

        # ============================================================
        # 左栏：模板多选区
        # ============================================================
        left_box = QWidget()
        left_layout = QHBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(QLabel("模板（✓ 多选）:"))

        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.template_list.setStyleSheet(
            "QListWidget { border: 1px solid #d9d9d9; border-radius: 4px; "
            "padding: 2px; background: white; } "
            "QListWidget::item { padding: 2px 6px; }"
        )
        # 左右布局后纵向空间宽裕，从 110 撑到 160
        self.template_list.setMinimumHeight(160)
        self.template_list.setMinimumWidth(280)
        self.template_list.itemChanged.connect(
            self._on_template_check_changed
        )
        left_layout.addWidget(self.template_list, 1)

        col_btns = QVBoxLayout()
        col_btns.setSpacing(4)
        self.tpl_select_all_btn = QPushButton("全选")
        self.tpl_select_all_btn.clicked.connect(
            lambda: self._set_all_templates_checked(True)
        )
        col_btns.addWidget(self.tpl_select_all_btn)
        self.tpl_clear_btn = QPushButton("全不选")
        self.tpl_clear_btn.clicked.connect(
            lambda: self._set_all_templates_checked(False)
        )
        col_btns.addWidget(self.tpl_clear_btn)
        self.tpl_count_label = QLabel("已选 0")
        self.tpl_count_label.setStyleSheet("color: #666; font-size: 12px;")
        self.tpl_count_label.setAlignment(Qt.AlignCenter)
        col_btns.addWidget(self.tpl_count_label)
        col_btns.addStretch()
        left_layout.addLayout(col_btns)

        outer.addWidget(left_box, 1)

        # ============================================================
        # 右栏：交易日范围 / 时间窗 / 并发 + 按钮（垂直排）
        # ============================================================
        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # 第 1 行：交易日范围 + 精选新闻 + Provider
        row_dates = QHBoxLayout()
        row_dates.addWidget(QLabel("交易日范围:"))
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_edit.setStyleSheet(INPUT_STYLE)
        self.start_date_edit.setMaximumDate(QDate.currentDate())
        self.start_date_edit.setDate(QDate.currentDate().addDays(-1))
        self.start_date_edit.dateChanged.connect(self._on_date_range_changed)
        row_dates.addWidget(self.start_date_edit)
        row_dates.addWidget(QLabel("~"))
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_date_edit.setStyleSheet(INPUT_STYLE)
        self.end_date_edit.setMaximumDate(QDate.currentDate())
        self.end_date_edit.setDate(QDate.currentDate().addDays(-1))
        self.end_date_edit.dateChanged.connect(self._on_date_range_changed)
        row_dates.addWidget(self.end_date_edit)
        self.dates_count_label = QLabel("（— 个交易日）")
        self.dates_count_label.setStyleSheet(
            "color: #1890ff; font-size: 12px; padding: 0 8px;"
        )
        row_dates.addWidget(self.dates_count_label)
        row_dates.addSpacing(15)

        self.curated_checkbox = QCheckBox("仅精选新闻 (curated)")
        self.curated_checkbox.setChecked(True)
        self.curated_checkbox.setToolTip(
            "勾选：仅用 clean_status='curated' 的精选新闻\n"
            "取消：用 raw_news 全部（含 rejected / pending）"
        )
        self.curated_checkbox.stateChanged.connect(self._refresh_preview)
        row_dates.addWidget(self.curated_checkbox)
        row_dates.addSpacing(15)

        row_dates.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.setStyleSheet(COMBOBOX_STYLE)
        self.provider_combo.addItems(["deepseek", "openai", "qwen"])
        self.provider_combo.setCurrentText("deepseek")
        self.provider_combo.setMinimumWidth(110)
        row_dates.addWidget(self.provider_combo)
        row_dates.addStretch()
        right_layout.addLayout(row_dates)

        # 第 2 行：新闻窗（仅单日时可调）+ 重置默认
        row_window = QHBoxLayout()
        self.window_hint_label = QLabel("时间窗（仅单日有效）:")
        row_window.addWidget(self.window_hint_label)
        self.news_start_edit = QDateTimeEdit()
        self.news_start_edit.setCalendarPopup(True)
        self.news_start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.news_start_edit.setStyleSheet(INPUT_STYLE)
        self.news_start_edit.dateTimeChanged.connect(
            self._on_window_changed
        )
        row_window.addWidget(QLabel("左:"))
        row_window.addWidget(self.news_start_edit)
        row_window.addSpacing(10)

        self.news_end_edit = QDateTimeEdit()
        self.news_end_edit.setCalendarPopup(True)
        self.news_end_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.news_end_edit.setStyleSheet(INPUT_STYLE)
        self.news_end_edit.setToolTip(
            "右边界最大值 = next_trade_date(交易日) 09:00（硬上限）"
        )
        self.news_end_edit.dateTimeChanged.connect(
            self._on_window_changed
        )
        row_window.addWidget(QLabel("右:"))
        row_window.addWidget(self.news_end_edit)
        row_window.addSpacing(10)

        self.reset_window_btn = QPushButton("↺ 默认窗")
        self.reset_window_btn.setStyleSheet(BUTTON_SUCCESS)
        self.reset_window_btn.setToolTip(
            "重置为主人默认：左 = trade_date 14:00 / 右 = next_open 09:00"
        )
        self.reset_window_btn.clicked.connect(self._apply_default_window)
        row_window.addWidget(self.reset_window_btn)
        row_window.addStretch()
        right_layout.addLayout(row_window)

        # 第 3 行：并发数 + 动作按钮
        row_actions = QHBoxLayout()
        row_actions.addWidget(QLabel("并发:"))
        self.concurrency_spin = QSpinBox()
        # blockSignals 包住初始 set，避免 setRange/setValue 在 queue_hint_label
        # 创建前触发 _on_concurrency_changed → AttributeError
        self.concurrency_spin.blockSignals(True)
        self.concurrency_spin.setRange(
            1, BacktestTaskManager.MAX_CONCURRENT_HARD_CAP
        )
        self.concurrency_spin.setValue(8)
        self.concurrency_spin.setSuffix(" 路")
        self.concurrency_spin.setToolTip(
            "同时跑几个任务（1~64）。\n"
            "DeepSeek 付费档实测 8 路稳定，60+ req/min 阈值内可上 32~64；\n"
            "并发越高 LLM 偶发 5xx 概率轻微上升 + SQLite WAL 写入压力上升，\n"
            "但失败只影响单个任务，不污染其他。"
        )
        self.concurrency_spin.blockSignals(False)
        self.concurrency_spin.valueChanged.connect(
            self._on_concurrency_changed
        )
        row_actions.addWidget(self.concurrency_spin)
        row_actions.addSpacing(15)

        self.refresh_btn = QPushButton("🔄 刷新预览")
        self.refresh_btn.setStyleSheet(BUTTON_PRIMARY)
        self.refresh_btn.clicked.connect(self._refresh_preview)
        row_actions.addWidget(self.refresh_btn)

        self.dry_run_btn = QPushButton("🧪 dry-run 试算")
        self.dry_run_btn.setStyleSheet(BUTTON_SUCCESS)
        self.dry_run_btn.setToolTip("不调 LLM，仅看 snapshot 是否通")
        self.dry_run_btn.clicked.connect(
            lambda: self._on_run_clicked(dry_run=True)
        )
        row_actions.addWidget(self.dry_run_btn)

        self.run_btn = QPushButton("🚀 正式跑回测")
        self.run_btn.setStyleSheet(BUTTON_PRIMARY)
        self.run_btn.setToolTip(
            "笛卡尔积：模板数 × 交易日数 = N 任务。\n"
            "全部入队，按设定的并发数后台跑（消耗 LLM）。"
        )
        self.run_btn.clicked.connect(
            lambda: self._on_run_clicked(dry_run=False)
        )
        row_actions.addWidget(self.run_btn, 1)
        right_layout.addLayout(row_actions)

        # 第 4 行：任务数提示（横向占满，独立成行避免被按钮挤）
        row_hint = QHBoxLayout()
        self.queue_hint_label = QLabel("")
        self.queue_hint_label.setStyleSheet(
            "color: #1890ff; font-size: 12px; padding: 0 8px;"
        )
        row_hint.addWidget(self.queue_hint_label)
        row_hint.addStretch()
        right_layout.addLayout(row_hint)

        right_layout.addStretch()
        outer.addWidget(right_box, 2)

        return group

    def _build_preview_group(self) -> QGroupBox:
        group = QGroupBox("⏱ 时间边界预览（自动同步当前参数）")
        layout = QVBoxLayout(group)
        self.preview_browser = QTextBrowser()
        self.preview_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.preview_browser.setMaximumHeight(220)
        self.preview_browser.setOpenLinks(False)
        layout.addWidget(self.preview_browser)
        return group

    def _build_task_list_group(self) -> QGroupBox:
        """任务列表（2026-05-27 多任务队列改造新增）。

        6 列：# / 模板 / 日期 / 状态 / 用时 / 删除
        - 点击行 → 下方三块（流式 / md / 题材）联动切换
        - 删除列嵌按钮：仅终态任务可删；运行中按钮 disable
        """
        group = QGroupBox(
            "📋 任务列表（连点排队，后台 8 并发，点行联动下方）"
        )
        layout = QVBoxLayout(group)

        self.task_table = QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(
            ["#", "模板", "日期", "状态", "用时", "删除"]
        )
        self.task_table.setStyleSheet(TABLE_STYLE)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setMinimumHeight(180)

        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.task_table.setColumnWidth(0, 40)
        self.task_table.setColumnWidth(2, 100)
        self.task_table.setColumnWidth(3, 90)
        self.task_table.setColumnWidth(4, 70)
        self.task_table.setColumnWidth(5, 60)

        self.task_table.itemSelectionChanged.connect(
            self._on_task_selection_changed
        )
        layout.addWidget(self.task_table)
        return group

    def _build_md_group(self) -> QGroupBox:
        """报告 md 预览区，内部垂直切分：

        ┌── toolbar：status_label + 📂 打开 md ───┐
        ├── QSplitter(Qt.Vertical) ───────────────┤
        │   上：🤖 AI 流式输出（chunk 实时粘连）   │
        │   下：📄 完成后报告 md 预览              │
        └─────────────────────────────────────────┘

        2026-05-27 新增上半流式区：LLM 分析 chunk 与题材抽取 JSON chunk
        共用同一个 ``stream_browser``，第二阶段开始时插一行
        ``--- 题材抽取 JSON ---`` 分隔（见 :meth:`_append_progress` 内的
        启发式判断）。
        """
        group = QGroupBox("📄 报告预览 / 🤖 AI 流式输出")
        layout = QVBoxLayout(group)

        toolbar = QHBoxLayout()
        self.status_label = QLabel("（点「正式跑回测」后这里出结果）")
        self.status_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.status_label, 1)

        self.open_md_btn = QPushButton("📂 打开 md 文件")
        self.open_md_btn.setStyleSheet(BUTTON_SUCCESS)
        self.open_md_btn.setEnabled(False)
        self.open_md_btn.clicked.connect(self._open_md_externally)
        toolbar.addWidget(self.open_md_btn)
        layout.addLayout(toolbar)

        inner_split = QSplitter(Qt.Vertical)

        # 上半：AI 流式输出（chunk 粘连写入）
        stream_box = QWidget()
        stream_layout = QVBoxLayout(stream_box)
        stream_layout.setContentsMargins(0, 0, 0, 0)
        stream_layout.setSpacing(4)
        stream_layout.addWidget(
            QLabel("🤖 AI 实时输出（流式，分析 → 题材抽取 共用）：")
        )
        self.stream_browser = QTextBrowser()
        self.stream_browser.setStyleSheet(
            TEXTBROWSER_STYLE
            + "QTextBrowser { font-family: 'Consolas','Courier New',"
            "monospace; font-size: 12px; color: #595959; }"
        )
        stream_layout.addWidget(self.stream_browser, 1)
        inner_split.addWidget(stream_box)

        # 下半：完成后的 md 预览
        md_box = QWidget()
        md_layout = QVBoxLayout(md_box)
        md_layout.setContentsMargins(0, 0, 0, 0)
        md_layout.setSpacing(4)
        md_layout.addWidget(QLabel("📄 完成后报告 md 预览（完整内容）："))
        self.md_browser = QTextBrowser()
        self.md_browser.setStyleSheet(TEXTBROWSER_STYLE)
        md_layout.addWidget(self.md_browser, 1)
        inner_split.addWidget(md_box)

        inner_split.setStretchFactor(0, 3)
        inner_split.setStretchFactor(1, 2)
        layout.addWidget(inner_split, 1)
        return group

    def _build_themes_group(self) -> QGroupBox:
        group = QGroupBox("🎯 抽到的题材（is_backtest=1）")
        layout = QVBoxLayout(group)

        self.themes_table = QTableWidget(0, 5)
        self.themes_table.setHorizontalHeaderLabels(
            ["theme_id", "题材", "强度", "分级", "板块代码"]
        )
        self.themes_table.setStyleSheet(TABLE_STYLE)
        self.themes_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.themes_table.verticalHeader().setVisible(False)
        layout.addWidget(self.themes_table)
        return group

    # =====================================================================
    # 数据加载
    # =====================================================================

    def load_template_list(self):
        """从 `core.ai_config.AIConfig` 拉所有 analysis 模板，填到多选列表。

        保留旧勾选状态（按 template_id 比对），首次加载时默认勾选 ``custom_6``。
        """
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            templates = config.get_prompt_templates()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "加载模板失败",
                f"无法加载 prompts/analysis/*.md: {exc}",
            )
            return

        prev_checked = self._collect_checked_template_ids()
        first_load = self.template_list.count() == 0

        self._suppress_template_signal = True
        try:
            self.template_list.clear()
            for key, tmpl in templates.items():
                display_name = tmpl.get("name", key)
                item = QListWidgetItem(f"{display_name}　[{key}]")
                item.setData(Qt.UserRole, key)
                item.setData(Qt.UserRole + 1, display_name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                if first_load and not prev_checked:
                    item.setCheckState(
                        Qt.Checked if key == "custom_6" else Qt.Unchecked
                    )
                else:
                    item.setCheckState(
                        Qt.Checked if key in prev_checked else Qt.Unchecked
                    )
                self.template_list.addItem(item)
        finally:
            self._suppress_template_signal = False

        self._refresh_template_count_label()

    def _collect_checked_template_ids(self) -> set[str]:
        out: set[str] = set()
        for i in range(self.template_list.count()):
            it = self.template_list.item(i)
            if it.checkState() == Qt.Checked:
                tid = it.data(Qt.UserRole)
                if tid:
                    out.add(str(tid))
        return out

    def _collect_checked_templates(self) -> List[Tuple[str, str]]:
        """返回已勾选模板列表 [(template_id, display_label), ...]。"""
        out: List[Tuple[str, str]] = []
        for i in range(self.template_list.count()):
            it = self.template_list.item(i)
            if it.checkState() == Qt.Checked:
                tid = str(it.data(Qt.UserRole) or "")
                label = str(it.data(Qt.UserRole + 1) or tid)
                if tid:
                    out.append((tid, label))
        return out

    def _set_all_templates_checked(self, checked: bool) -> None:
        self._suppress_template_signal = True
        try:
            state = Qt.Checked if checked else Qt.Unchecked
            for i in range(self.template_list.count()):
                self.template_list.item(i).setCheckState(state)
        finally:
            self._suppress_template_signal = False
        self._refresh_template_count_label()

    def _on_template_check_changed(self, _item: QListWidgetItem) -> None:
        if self._suppress_template_signal:
            return
        self._refresh_template_count_label()

    def _refresh_template_count_label(self) -> None:
        n = len(self._collect_checked_template_ids())
        total = self.template_list.count()
        self.tpl_count_label.setText(f"已选 {n} / {total}")
        self._refresh_queue_hint()

    def refresh(self):
        """主窗口切换到本页时调用：刷新模板列表 + 默认窗 + 预览。"""
        self.load_template_list()
        self._refresh_dates_count()
        self._apply_default_window()
        self._refresh_preview()

    # =====================================================================
    # 时间窗 / 默认值管理
    # =====================================================================

    def _on_date_range_changed(self, _date: QDate):
        """日期范围变了 → 重算交易日数 + 默认时间窗（以 start 为锚） + 刷预览。"""
        # 防止 start > end：若主人把 start 调到 end 之后，自动把 end 拉齐
        if self.start_date_edit.date() > self.end_date_edit.date():
            self.end_date_edit.blockSignals(True)
            self.end_date_edit.setDate(self.start_date_edit.date())
            self.end_date_edit.blockSignals(False)
        self._refresh_dates_count()
        self._apply_default_window()
        self._refresh_preview()

    def _on_window_changed(self, _dt: QDateTime):
        if self._suppress_window_signal:
            return
        self._refresh_preview()

    def _on_concurrency_changed(self, n: int) -> None:
        """主人调并发 spinbox → 通知 manager（manager 内部会 clamp）。"""
        actual = self.manager.set_max_concurrent(n)
        if actual != n:
            self.concurrency_spin.blockSignals(True)
            try:
                self.concurrency_spin.setValue(actual)
            finally:
                self.concurrency_spin.blockSignals(False)
        self._refresh_queue_hint()

    def _refresh_queue_hint(self) -> None:
        """刷新「将创建 N 任务」的提示标签。"""
        templates = self._collect_checked_templates()
        n_dates = len(self._current_dates_cache)
        n_tasks = len(templates) * n_dates
        conc = self.concurrency_spin.value() if hasattr(
            self, "concurrency_spin"
        ) else 8
        self.queue_hint_label.setText(
            f"📋 {len(templates)} 模板 × {n_dates} 交易日 = "
            f"{n_tasks} 任务 / 后台 {conc} 并发"
        )

    def _refresh_dates_count(self) -> None:
        """根据当前 (start, end) 解析交易日列表（排除今天），刷新计数标签。"""
        start_date = self.start_date_edit.date().toString("yyyyMMdd")
        end_date = self.end_date_edit.date().toString("yyyyMMdd")
        try:
            from services.market.trade_date import trade_dates_between
            from services.market.tushare_client import TushareClient
            dates = trade_dates_between(
                start_date, end_date, client=TushareClient(),
            )
        except Exception as exc:  # noqa: BLE001
            self._current_dates_cache = []
            self.dates_count_label.setText(
                f"<span style='color:#c00'>解析失败: {exc}</span>"
            )
            self._update_window_editability()
            self._refresh_queue_hint()
            return

        today_str = datetime.now().strftime("%Y%m%d")
        dates = [d for d in dates if d != today_str]
        self._current_dates_cache = dates

        if not dates:
            self.dates_count_label.setText(
                "<span style='color:#c80'>（区间内无可用交易日）</span>"
            )
        elif len(dates) == 1:
            self.dates_count_label.setText(f"（单日: {dates[0]}）")
        else:
            self.dates_count_label.setText(
                f"（{len(dates)} 个交易日: {dates[0]}~{dates[-1]}）"
            )
        self._update_window_editability()
        self._refresh_queue_hint()

    def _update_window_editability(self) -> None:
        """单日 → 时间窗可调；多日 → 灰掉时间窗 + 默认窗按钮。"""
        single_day = len(self._current_dates_cache) <= 1
        for w in (
            self.news_start_edit, self.news_end_edit,
            self.reset_window_btn,
        ):
            w.setEnabled(single_day)
        if single_day:
            self.window_hint_label.setText("时间窗（单日可微调）:")
            self.window_hint_label.setStyleSheet("")
        else:
            self.window_hint_label.setText(
                "时间窗（多日批量 → 强制默认窗）:"
            )
            self.window_hint_label.setStyleSheet("color: #999;")

    def _apply_default_window(self):
        """按主人语义重算默认窗 + 右边界硬上限，刷到两个 datetime 控件。

        - 左 = start_date 14:00
        - 右 = next_trade_date(start_date) 09:00
        - 右边界 QDateTimeEdit.setMaximumDateTime = 同一个上限

        多日批量场景下时间窗已被禁用，本函数只对锚点日 (start) 算一次默认值
        让 GUI 显示有意义的初值，实际入队时会按各日重算（见
        :func:`_resolve_per_date_window`）。
        """
        trade_date = self.start_date_edit.date().toString("yyyyMMdd")
        try:
            from services.market.tushare_client import TushareClient
            from services.scoring.snapshot import (
                compute_default_news_window,
            )
            start_dt, end_dt = compute_default_news_window(
                trade_date, client=TushareClient(),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "默认窗失败",
                f"无法解析 next_trade_date({trade_date}): {exc}\n"
                "请手动设置时间窗",
            )
            return

        self._suppress_window_signal = True
        try:
            qstart = QDateTime(
                start_dt.year, start_dt.month, start_dt.day,
                start_dt.hour, start_dt.minute,
            )
            qend = QDateTime(
                end_dt.year, end_dt.month, end_dt.day,
                end_dt.hour, end_dt.minute,
            )
            # 设上限再设值，避免被夹
            self.news_end_edit.setMaximumDateTime(qend)
            self.news_start_edit.setMaximumDateTime(qend)
            self.news_start_edit.setDateTime(qstart)
            self.news_end_edit.setDateTime(qend)
        finally:
            self._suppress_window_signal = False

    # =====================================================================
    # 预览 & 试算
    # =====================================================================

    def _current_inputs(self) -> dict:
        """汇总当前 GUI 输入。

        多模板/多日批量入口返回的 ``trade_date`` 取「锚点日 = start_date」，
        仅供时间窗预览用；真正入队时按 ``self._current_dates_cache`` 笛卡尔积
        + 每日重算时间窗（见 :func:`_resolve_per_date_window`）。
        """
        templates = self._collect_checked_templates()
        return {
            "templates": templates,                  # [(tid, label), ...]
            "dates": list(self._current_dates_cache),  # YYYYMMDD 列表
            "trade_date": self.start_date_edit.date().toString("yyyyMMdd"),
            "news_start_dt": self.news_start_edit.dateTime().toPyDateTime(),
            "news_end_dt": self.news_end_edit.dateTime().toPyDateTime(),
            "news_status": (
                "curated" if self.curated_checkbox.isChecked() else ""
            ),
            "provider": self.provider_combo.currentText() or None,
        }

    def _refresh_preview(self):
        """调 ``tools.snapshot_inspect.inspect_snapshot`` 渲染时间边界。

        多日批量场景仅按 **锚点日 = start_date** 渲染时间边界示例，
        让主人对默认窗有直观印象；真正入队时按各日重算。

        注意：这是同步调用，但 snapshot 重建本身只读 SQLite ~100ms 完成，
        不会卡住 GUI。
        """
        inputs = self._current_inputs()
        if not inputs["templates"]:
            self.preview_browser.setHtml(
                "<i style='color:#999'>（请先勾选至少一个模板）</i>"
            )
            return

        try:
            from tools.snapshot_inspect import inspect_snapshot
            rep = inspect_snapshot(
                inputs["trade_date"],
                news_start_dt=inputs["news_start_dt"],
                news_end_dt=inputs["news_end_dt"],
                news_status=inputs["news_status"],
            )
        except Exception as exc:  # noqa: BLE001
            self.preview_browser.setHtml(
                f"<span style='color:#c00'>预览失败: {exc}</span>"
            )
            return

        if rep.get("error"):
            self.preview_browser.setHtml(
                f"<span style='color:#c00'>{rep['error']}</span>"
            )
            return

        d = rep["derived"]
        w = rep["window"]
        complete_color = "#0a0" if d["snapshot_complete"] else "#c80"
        hit_max_tag = (
            "&nbsp;&nbsp;<span style='color:#c80'>← 已触上限</span>"
            if w["hit_max"] else ""
        )
        d_plus_lines = " | ".join(
            f"D+{x['day']}={x['score_date'] or 'ERR'}"
            for x in rep["d_plus"]
        )
        status_label = (
            "curated（精选）" if inputs["news_status"] == "curated"
            else "all（含 raw / rejected）"
        )
        html = (
            f"<div style='font-family:Consolas,monospace;font-size:12px'>"
            f"<b>视角</b>: trade_date={inputs['trade_date']} "
            f"&nbsp;|&nbsp; news_status={status_label}<br>"
            f"<b>新闻窗</b>: {w['news_window_human']}{hit_max_tag}<br>"
            f"<b>上限</b>: {w['max_news_end_dt']} "
            f"(= next_trade_date 09:00)<br>"
            f"<b>实际新闻数</b>: <span style='color:{complete_color}'>"
            f"{d['news_count']}</span>"
            f"&nbsp;&nbsp;(范围: "
            f"{d['news_ts_actual']['earliest'] or '-'} ~ "
            f"{d['news_ts_actual']['latest'] or '-'})<br>"
            f"<b>大盘日期</b>: {d['market_summary_date']} "
            f"(md 长度 {d['market_summary_md_len']} 字符)<br>"
            f"<b>snapshot 完整</b>: "
            f"<span style='color:{complete_color}'>"
            f"{d['snapshot_complete']}</span>"
            + (
                f"&nbsp;&nbsp;<span style='color:#c80'>"
                f"missing: {d['missing_reason']}</span>"
                if d["missing_reason"] else ""
            )
            + f"<br><b>打分日期</b>: {d_plus_lines}</div>"
        )
        self.preview_browser.setHtml(html)

    # =====================================================================
    # 跑回测
    # =====================================================================

    def _resolve_per_date_window(
        self, trade_date: str,
    ) -> Optional[Tuple[datetime, datetime]]:
        """对单个 trade_date 算「主人默认窗」：(14:00, next_open 09:00)。

        失败返回 None，调用方负责跳过该日 + 收集 errors。
        """
        try:
            from services.market.tushare_client import TushareClient
            from services.scoring.snapshot import (
                compute_default_news_window,
            )
            return compute_default_news_window(
                trade_date, client=TushareClient(),
            )
        except Exception:  # noqa: BLE001
            return None

    def _on_run_clicked(self, *, dry_run: bool):
        """笛卡尔积入队：勾选模板 × 解析交易日 = N 任务全部进队列。

        - 单日（len(dates) == 1）：可用主人手填的时间窗（含微调）
        - 多日批量：每日按 (14:00, next_open 09:00) 默认窗算

        2026-05-28 批量改造：取消单模板单日的限制，改为多模板多日笛卡尔积。
        并发上限继续由 BacktestTaskManager 管理（已支持运行时调）。
        """
        inputs = self._current_inputs()
        templates = inputs["templates"]
        dates = inputs["dates"]
        if not templates:
            QMessageBox.warning(self, "缺参数", "请先勾选至少一个模板")
            return
        if not dates:
            QMessageBox.warning(
                self, "缺参数",
                "解析后无可用交易日，请检查日期范围（今天会被自动排除）"
            )
            return

        single_day = len(dates) == 1

        # 单日时复用主人手填的时间窗，需做穿越/逆序校验
        if single_day:
            if inputs["news_end_dt"] > datetime.now():
                QMessageBox.warning(
                    self, "穿越保护",
                    f"news_end_dt={inputs['news_end_dt']} 还在未来，"
                    "不能回测未来时间窗",
                )
                return
            if inputs["news_start_dt"] >= inputs["news_end_dt"]:
                QMessageBox.warning(
                    self, "参数错误",
                    f"news_start_dt({inputs['news_start_dt']}) >= "
                    f"news_end_dt({inputs['news_end_dt']})",
                )
                return

        n_tasks = len(templates) * len(dates)

        # 任务量大时弹一次确认（≥ 5 任务，避免主人手抖跑爆 API 配额）
        if n_tasks >= 5 and not dry_run:
            tmpl_names = "、".join(label for _tid, label in templates[:3])
            if len(templates) > 3:
                tmpl_names += f" 等 {len(templates)} 个"
            reply = QMessageBox.question(
                self, "确认批量回测",
                f"将创建 <b>{n_tasks}</b> 个任务<br>"
                f"&nbsp;&nbsp;模板（{len(templates)}）：{tmpl_names}<br>"
                f"&nbsp;&nbsp;交易日（{len(dates)}）："
                f"{dates[0]} ~ {dates[-1]}<br>"
                f"&nbsp;&nbsp;时间窗：{'手填（单日）' if single_day else '主人默认（每日 14:00 ~ next_open 09:00）'}<br>"
                f"&nbsp;&nbsp;并发：{self.concurrency_spin.value()} 路<br><br>"
                f"将消耗 ~{n_tasks} 次 LLM 调用，确认继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # 批量解析每日窗口（多日时）。失败的 (date) 收集起来一并提示。
        per_date_window: dict[str, Tuple[datetime, datetime]] = {}
        failed_dates: List[str] = []
        if single_day:
            per_date_window[dates[0]] = (
                inputs["news_start_dt"], inputs["news_end_dt"],
            )
        else:
            for d in dates:
                w = self._resolve_per_date_window(d)
                if w is None:
                    failed_dates.append(d)
                else:
                    # 多日的「未来穿越」防御：next_open 09:00 不能在未来
                    # （比如选了今天，next_open 还没到）
                    if w[1] > datetime.now():
                        failed_dates.append(d + "(未来)")
                        continue
                    per_date_window[d] = w
            if failed_dates:
                QMessageBox.warning(
                    self, "部分日期解析失败",
                    f"以下 {len(failed_dates)} 个日期会被跳过：\n"
                    f"  {', '.join(failed_dates[:10])}"
                    + ("..." if len(failed_dates) > 10 else "")
                    + "\n\n剩余日期继续入队"
                )
            if not per_date_window:
                QMessageBox.warning(
                    self, "无可用日期",
                    "所有日期都解析失败，已取消入队"
                )
                return

        # 笛卡尔积入队
        prefix = "[dry-run] " if dry_run else ""
        enqueued = 0
        for tid, label in templates:
            for d in dates:
                if d not in per_date_window:
                    continue
                start_dt, end_dt = per_date_window[d]
                self.manager.enqueue({
                    "template_id": tid,
                    "template_label": f"{prefix}{label}",
                    "trade_date": d,
                    "news_start_dt": start_dt,
                    "news_end_dt": end_dt,
                    "news_status": inputs["news_status"],
                    "provider": inputs["provider"],
                    "overwrite": False,
                    "dry_run": dry_run,
                })
                enqueued += 1

        # 入队完成提示（任务列表会自动选中最后一个；此处仅给个状态行）
        self.status_label.setText(
            f"<span style='color:#1890ff'>📋 已入队 {enqueued} 个任务"
            f"（{len(templates)} 模板 × {len(dates)} 交易日，"
            f"跳过 {len(failed_dates)}）</span>"
        )

    # =====================================================================
    # 任务列表 ↔ 下方三块联动（2026-05-27 多任务队列改造）
    # =====================================================================

    def _wire_manager_signals(self) -> None:
        """连接 BacktestTaskManager 的 6 个信号到本页槽函数。"""
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_started.connect(self._on_task_started)
        self.manager.task_progress.connect(self._on_task_progress)
        self.manager.task_streaming.connect(self._on_task_streaming)
        self.manager.task_finished.connect(self._on_task_finished)
        self.manager.task_removed.connect(self._on_task_removed)

    def _start_elapsed_timer(self) -> None:
        """QTimer 1s 刷新所有 RUNNING 任务的「用时」列。"""
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_running_elapsed)
        self._tick_timer.start()

    # --- 任务行操作 helpers -------------------------------------------------

    def _row_of_task(self, task_id: int) -> int:
        """通过 task_id 找表行号（首列存 task_id）。-1 表示找不到。"""
        for r in range(self.task_table.rowCount()):
            item = self.task_table.item(r, 0)
            if item is not None and int(item.data(Qt.UserRole)) == task_id:
                return r
        return -1

    def _set_status_cell(self, row: int, task: BacktestTask) -> None:
        """渲染状态列：emoji + 中文 + 颜色。"""
        item = QTableWidgetItem(STATUS_LABEL[task.status])
        item.setForeground(QColor(STATUS_COLOR[task.status]))
        item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 3, item)

    def _set_elapsed_cell(self, row: int, task: BacktestTask) -> None:
        item = QTableWidgetItem(task.elapsed_text())
        item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 4, item)

    def _set_delete_button(self, row: int, task: BacktestTask) -> None:
        """嵌入第 6 列的删除按钮；运行中灰化。"""
        btn = QPushButton("🗑")
        btn.setToolTip(
            "删除该任务（仅 GUI 行；不动 md 文件 / 数据库）"
        )
        btn.setEnabled(task.status != TaskStatus.RUNNING)
        btn.clicked.connect(
            lambda _checked=False, tid=task.task_id:
            self._on_delete_task_clicked(tid)
        )
        self.task_table.setCellWidget(row, 5, btn)

    # --- manager 信号槽 -----------------------------------------------------

    def _on_task_added(self, task_id: int) -> None:
        """新任务入队 → 追加一行 + 自动选中（追加决策 q3：默认选最新）。"""
        task = self.manager.get(task_id)
        if task is None:
            return
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)

        id_item = QTableWidgetItem(str(task.task_id))
        id_item.setData(Qt.UserRole, task.task_id)
        id_item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 0, id_item)
        self.task_table.setItem(
            row, 1, QTableWidgetItem(task.template_label)
        )
        date_str = (
            f"{task.trade_date[:4]}-{task.trade_date[4:6]}-"
            f"{task.trade_date[6:8]}"
        )
        date_item = QTableWidgetItem(date_str)
        date_item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 2, date_item)
        self._set_status_cell(row, task)
        self._set_elapsed_cell(row, task)
        self._set_delete_button(row, task)

        # 自动选中新行（行选中触发 _on_task_selection_changed 刷下方）
        self.task_table.selectRow(row)
        self.task_table.scrollToBottom()

    def _on_task_started(self, task_id: int) -> None:
        task = self.manager.get(task_id)
        if task is None:
            return
        row = self._row_of_task(task_id)
        if row < 0:
            return
        self._set_status_cell(row, task)
        self._set_elapsed_cell(row, task)
        self._set_delete_button(row, task)
        if task_id == self._current_task_id:
            self._refresh_for_selected_task()

    def _on_task_progress(self, task_id: int, line: str) -> None:
        """阶段日志：仅当前选中任务才 append 到 stream_browser。"""
        if task_id == self._current_task_id:
            self.stream_browser.insertPlainText(line)
            self.stream_browser.moveCursor(QTextCursor.End)

    def _on_task_streaming(self, task_id: int, chunk: str) -> None:
        """LLM chunk：仅当前选中任务才 append。"""
        if task_id == self._current_task_id:
            self.stream_browser.insertPlainText(chunk)
            self.stream_browser.moveCursor(QTextCursor.End)

    def _on_task_finished(self, task_id: int) -> None:
        task = self.manager.get(task_id)
        if task is None:
            return
        row = self._row_of_task(task_id)
        if row < 0:
            return
        self._set_status_cell(row, task)
        self._set_elapsed_cell(row, task)
        self._set_delete_button(row, task)
        if task_id == self._current_task_id:
            self._refresh_for_selected_task()

    def _on_task_removed(self, task_id: int) -> None:
        row = self._row_of_task(task_id)
        if row >= 0:
            self.task_table.removeRow(row)
        if task_id == self._current_task_id:
            self._current_task_id = None
            self._clear_lower_panels()

    # --- 行选中 & 下方三块联动渲染 ------------------------------------------

    def _on_task_selection_changed(self) -> None:
        rows = self.task_table.selectionModel().selectedRows()
        if not rows:
            self._current_task_id = None
            self._clear_lower_panels()
            return
        row = rows[0].row()
        id_item = self.task_table.item(row, 0)
        if id_item is None:
            return
        self._current_task_id = int(id_item.data(Qt.UserRole))
        self._refresh_for_selected_task()

    def _refresh_for_selected_task(self) -> None:
        """整段回灌当前选中任务的 stream / md / themes 到下方三块。"""
        tid = self._current_task_id
        if tid is None:
            self._clear_lower_panels()
            return
        task = self.manager.get(tid)
        if task is None:
            self._clear_lower_panels()
            return

        # 流式区：清空 → 回灌 log_buffer + stream_buffer
        self.stream_browser.clear()
        for line in task.log_buffer:
            self.stream_browser.insertPlainText(line)
        for chunk in task.stream_buffer:
            self.stream_browser.insertPlainText(chunk)
        self.stream_browser.moveCursor(QTextCursor.End)

        # 状态栏
        self._update_status_label_for(task)

        # md / themes 区
        result = task.result or {}
        report_path = result.get("report_path")

        if task.status == TaskStatus.PENDING:
            self.md_browser.setHtml(
                "<i style='color:#888'>⌛ 排队中，等待空位...</i>"
            )
            self.themes_table.setRowCount(0)
            self.open_md_btn.setEnabled(False)
            return
        if task.status == TaskStatus.RUNNING:
            self.md_browser.setHtml(
                "<i style='color:#888'>⏳ 跑中，完成后显示报告 md</i>"
            )
            self.themes_table.setRowCount(0)
            self.open_md_btn.setEnabled(False)
            return
        if task.status == TaskStatus.FAILED:
            err_html = (
                f"<pre style='color:#c00'>❌ 失败\n\n"
                f"{(task.error or '未知错误')}</pre>"
            )
            self.md_browser.setHtml(err_html)
            self.themes_table.setRowCount(0)
            self.open_md_btn.setEnabled(False)
            return
        if task.status == TaskStatus.SKIPPED:
            self.md_browser.setHtml(
                f"<i style='color:#c80'>⏹ 跳过："
                f"{result.get('error') or '无新闻或 dry-run'}</i>"
            )
            self.themes_table.setRowCount(0)
            self.open_md_btn.setEnabled(False)
            return
        # SUCCESS
        if report_path:
            self._render_md(report_path)
            self._render_themes(report_path)
            self.open_md_btn.setEnabled(True)
            self._last_report_path = report_path
        else:
            self.md_browser.setHtml(
                "<i style='color:#888'>dry-run 模式：无 md 产出</i>"
            )
            self.themes_table.setRowCount(0)
            self.open_md_btn.setEnabled(False)

    def _update_status_label_for(self, task: BacktestTask) -> None:
        """根据 task 状态刷 status_label（位于 md_group 顶部 toolbar）。"""
        color = STATUS_COLOR[task.status]
        label = STATUS_LABEL[task.status]
        meta = (
            f"#{task.task_id} {task.template_label} @ "
            f"{task.trade_date}"
        )
        suffix = ""
        if task.status in (
            TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.FAILED,
        ):
            elapsed = (
                f"{task.elapsed_ms} ms"
                if task.elapsed_ms is not None else "-"
            )
            res = task.result or {}
            news_count = res.get("snapshot_news_count")
            themes_count = res.get("themes_count")
            if task.status == TaskStatus.SUCCESS:
                suffix = (
                    f"&nbsp;&nbsp;news={news_count} "
                    f"themes={themes_count} &nbsp;{elapsed}"
                )
            else:
                suffix = f"&nbsp;&nbsp;{elapsed}"
        self.status_label.setText(
            f"<span style='color:{color}'>{label}</span>"
            f"&nbsp;&nbsp;<span style='color:#888'>{meta}</span>"
            f"{suffix}"
        )

    def _clear_lower_panels(self) -> None:
        """没选中任务时下方三块的默认态。"""
        self.status_label.setText(
            "（点「正式跑回测」后这里出结果）"
        )
        self.stream_browser.clear()
        self.md_browser.clear()
        self.themes_table.setRowCount(0)
        self.open_md_btn.setEnabled(False)

    # --- 删除任务 -----------------------------------------------------------

    def _on_delete_task_clicked(self, task_id: int) -> None:
        task = self.manager.get(task_id)
        if task is None:
            return
        if task.status == TaskStatus.RUNNING:
            QMessageBox.information(
                self, "跑中不能删",
                f"任务 #{task_id} 正在跑，等它完成或失败后再删"
            )
            return
        # 不弹二次确认：删除本身仅删 GUI 行，无破坏性
        self.manager.remove(task_id)

    # --- QTimer 1s 刷新 RUNNING 用时 ---------------------------------------

    def _refresh_running_elapsed(self) -> None:
        for task in self.manager.list_tasks():
            if task.status != TaskStatus.RUNNING:
                continue
            row = self._row_of_task(task.task_id)
            if row < 0:
                continue
            self._set_elapsed_cell(row, task)

    # --- 关 GUI 时清理 ------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 (Qt 约定大写驼峰)
        """关页面（或 main 退出）时取消 PENDING；RUNNING 自然结束。"""
        try:
            self.manager.shutdown()
        finally:
            super().closeEvent(event)

    # =====================================================================
    # 渲染辅助
    # =====================================================================

    def _render_md(self, report_path: str):
        """读 md 文件完整内容显示（2026-05-28 移除 8000 字截断）。"""
        from services.storage.database import get_project_root
        abs_path = Path(get_project_root()) / report_path
        if not abs_path.exists():
            self.md_browser.setHtml(
                f"<span style='color:#c00'>文件不存在: {abs_path}</span>"
            )
            return
        try:
            text = abs_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.md_browser.setHtml(
                f"<span style='color:#c00'>读取失败: {exc}</span>"
            )
            return
        self.md_browser.setMarkdown(text)

    def _render_themes(self, report_path: str):
        """从 ai_inference.db 反查这份 md 抽到的题材。"""
        from services.storage.ai_inference_db import get_ai_inference_db
        adb = get_ai_inference_db()
        with adb.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT id, theme_name, strength_score, strength_level, "
                "       sector_ts_code "
                "FROM theme_predictions "
                "WHERE report_path = ? "
                "ORDER BY strength_score DESC",
                (report_path,),
            ).fetchall()
        self.themes_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self.themes_table.setItem(
                r, 0, QTableWidgetItem(str(row["id"]))
            )
            self.themes_table.setItem(
                r, 1, QTableWidgetItem(row["theme_name"] or "")
            )
            score = row["strength_score"]
            score_str = f"{score:+d}" if score is not None else "-"
            score_item = QTableWidgetItem(score_str)
            if score is not None:
                if score > 0:
                    score_item.setForeground(Qt.darkRed)
                elif score < 0:
                    score_item.setForeground(Qt.darkGreen)
            self.themes_table.setItem(r, 2, score_item)
            self.themes_table.setItem(
                r, 3, QTableWidgetItem(row["strength_level"] or "")
            )
            self.themes_table.setItem(
                r, 4, QTableWidgetItem(row["sector_ts_code"] or "-")
            )

    def _open_md_externally(self):
        """用系统默认程序打开 md。"""
        if not getattr(self, "_last_report_path", None):
            return
        from services.storage.database import get_project_root
        abs_path = Path(get_project_root()) / self._last_report_path
        if not abs_path.exists():
            QMessageBox.warning(
                self, "文件不存在", f"{abs_path} 已被移走/删除"
            )
            return
        try:
            os.startfile(str(abs_path))  # Windows 专属
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", str(abs_path)])
