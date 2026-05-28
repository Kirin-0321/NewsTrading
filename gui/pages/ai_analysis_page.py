"""AI 分析页面（2026-05-28 批量模板 + 任务队列改造）。

业务定位
--------
让主人在 GUI 上勾选 N 个模板 → 共用同一份数据源/时间窗/盘后总结 →
N 个任务后台并发跑，点哪个任务下面看哪个的进度/结果。

布局总览（参考手动回测页 manual_backtest_page.py）
--------------------------------------------------
::

    QVBoxLayout
    ├── title + subtitle
    ├── 配置区·上半（QHBoxLayout 左右两栏）
    │   ├── 左 1/3：📝 模板多选（QListWidget + 全选/全不选 + 已选 N）
    │   └── 右 2/3：⚙️ AI 配置 / 📂 数据源 / ⏱ 时间范围
    ├── 配置区·下半（盘后总结 + 选项开关）
    ├── 操作行：[🚀 批量分析 N 任务] [⏹ 终止选中任务]
    ├── 中段（QSplitter Horizontal，1）
    │   ├── 📊 进度日志（选中任务）
    │   └── 📋 任务队列表
    └── 下方（2）
        ├── 状态条 + 📂 打开报告
        └── 📝 分析结果（选中任务流式）

时间默认值
----------
* 数据源时间范围 = 「上一收盘日 14:00 至今」（``_last_trading_close_14``
  helper + ``range_preset_combo`` 默认选项）
* 盘后总结 = 最近已收盘交易日 ``market_summaries`` 缓存（保留旧逻辑）

调用链
------
::

    AIAnalysisPage._on_run_clicked
       └── 对每个勾选模板：manager.enqueue(task_kwargs)
              └── _pump → 起 AIAnalysisWorker

    manager.signals → AIAnalysisPage 槽 → 刷 UI

删除任务规则（与手动回测页完全一致）
------------------------------------
* RUNNING：拒绝删除（弹提示）
* 其它态：仅删 GUI 行（不动 md / db）

关 GUI 时
---------
``closeEvent`` 调 ``manager.shutdown()``（取消 PENDING，让 RUNNING 自然结束）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from PyQt5.QtCore import QDateTime, Qt, QTimer
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
    QTextEdit, QVBoxLayout, QWidget,
)

from gui.utils.paths import find_typora_executable, resolve_project_path
from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, INPUT_STYLE, TABLE_STYLE, TEXTBROWSER_STYLE,
)
from gui.workers.ai_analysis_manager import (
    AnalysisTask, AnalysisTaskManager, STATUS_COLOR, STATUS_LABEL,
    TaskStatus,
)


_PROVIDER_NAME_MAP = {
    'OpenAI': 'openai',
    'DeepSeek': 'deepseek',
    '智谱AI': 'zhipu',
    '通义千问': 'qwen',
    '火山引擎': 'volcengine',
}


class AIAnalysisPage(QWidget):
    """AI 分析页：批量勾选模板 + 任务队列 + 选中行联动结果。"""

    def __init__(self):
        super().__init__()
        # showEvent 自动填盘后总结的"是否已填过"标志
        self._summary_auto_filled_with_data = False
        # 模板列表 itemChanged 信号抑制（批量 set/load 时用）
        self._suppress_template_signal = False
        # 当前选中的 task_id（联动下方进度/结果）
        self._current_task_id: Optional[int] = None
        # 上一次的 last_report_file（仅用于"打开报告"按钮快速取路径）
        self._last_report_path: Optional[str] = None

        # 任务队列管理器（页面私有；main_window.closeEvent 兜底调 shutdown）
        self.manager = AnalysisTaskManager(self)

        self.init_ui()
        self.load_config()
        self.load_template_list()
        self._wire_manager_signals()
        self._start_elapsed_timer()

    # =====================================================================
    # 顶层 init_ui
    # =====================================================================

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(8)

        title = QLabel("🤖 AI 分析")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "勾选 N 个模板 → 共用同一数据源/时间窗/盘后总结 → 批量入队。"
            "点任务行联动下方进度/结果。默认时间窗 = 上一收盘日 14:00 至今。"
        )
        subtitle.setStyleSheet("color: #666; font-size: 12px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # 上半区：配置直接展开（不套 QScrollArea，避免按钮被滚动条吃掉）。
        # 三个子组件按自身 sizeHint 占空间，stretch=0 → 剩余空间全留给中段
        # （任务队列 / 进度日志）+ 下方（结果区）。
        layout.addWidget(self._build_top_config_group())
        layout.addWidget(self._build_params_group())
        layout.addWidget(self._build_actions_row())

        # 中段：横向 splitter（左=进度 / 右=任务队列）
        middle_split = QSplitter(Qt.Horizontal)
        middle_split.addWidget(self._build_progress_group())
        middle_split.addWidget(self._build_task_list_group())
        middle_split.setStretchFactor(0, 1)
        middle_split.setStretchFactor(1, 1)
        layout.addWidget(middle_split, 1)

        # 下方：结果区
        layout.addWidget(self._build_result_group(), 2)

    def _build_top_config_group(self) -> QGroupBox:
        """模板 + AI 配置 + 数据源 + 时间范围（左右两栏）。"""
        group = QGroupBox("⚙️ 模板 / AI 配置 / 数据源")
        outer = QHBoxLayout(group)
        outer.setSpacing(12)

        # ============== 左栏：模板多选 ==============
        left_box = QWidget()
        left_layout = QHBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(QLabel("📝 模板（✓ 多选）:"))

        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.template_list.setStyleSheet(
            "QListWidget { border: 1px solid #d9d9d9; border-radius: 4px; "
            "padding: 2px; background: white; } "
            "QListWidget::item { padding: 2px 6px; }"
        )
        self.template_list.setMinimumHeight(180)
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

        self.refresh_templates_btn = QPushButton("🔄 刷新")
        self.refresh_templates_btn.setToolTip("重新读取 prompts/ 目录")
        self.refresh_templates_btn.clicked.connect(self.load_template_list)
        col_btns.addWidget(self.refresh_templates_btn)

        self.manage_templates_btn = QPushButton("📝 管理…")
        self.manage_templates_btn.setToolTip(
            "跳转到「提示词管理」页面新建/编辑/删除"
        )
        self.manage_templates_btn.clicked.connect(self._goto_prompt_manager)
        col_btns.addWidget(self.manage_templates_btn)
        col_btns.addStretch()
        left_layout.addLayout(col_btns)

        outer.addWidget(left_box, 1)

        # ============== 右栏：AI 配置 + 数据源 + 时间范围 ==============
        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # AI 配置（一行紧凑）
        row_ai = QHBoxLayout()
        row_ai.addWidget(QLabel("AI:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(list(_PROVIDER_NAME_MAP.keys()))
        self.provider_combo.setStyleSheet(COMBOBOX_STYLE)
        self.provider_combo.currentTextChanged.connect(
            self.on_provider_changed
        )
        row_ai.addWidget(self.provider_combo)

        row_ai.addWidget(QLabel("模型:"))
        self.model_combo = QComboBox()
        self.model_combo.setStyleSheet(COMBOBOX_STYLE)
        self.update_model_list()
        self.model_combo.currentTextChanged.connect(
            self._update_deep_thinking_availability
        )
        row_ai.addWidget(self.model_combo, 1)

        row_ai.addWidget(QLabel("Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setStyleSheet(INPUT_STYLE)
        self.api_key_input.setPlaceholderText("API Key")
        row_ai.addWidget(self.api_key_input, 2)

        self.save_key_btn = QPushButton("💾 保存")
        self.save_key_btn.clicked.connect(self.save_api_key)
        row_ai.addWidget(self.save_key_btn)
        self.test_btn = QPushButton("🔌 测试")
        self.test_btn.clicked.connect(self.test_connection)
        row_ai.addWidget(self.test_btn)
        right_layout.addLayout(row_ai)

        # 数据源
        row_src = QHBoxLayout()
        row_src.addWidget(QLabel("📂 数据表:"))
        self.db_source_combo = QComboBox()
        self.db_source_combo.addItem("精选库 (curated)", "curated")
        self.db_source_combo.addItem("原始库 (raw)", "raw")
        self.db_source_combo.setStyleSheet(COMBOBOX_STYLE)
        self.db_source_combo.currentIndexChanged.connect(
            self._on_db_source_changed
        )
        row_src.addWidget(self.db_source_combo)

        row_src.addWidget(QLabel("快捷:"))
        self.range_preset_combo = QComboBox()
        # 把「上一收盘日 14:00 至今」放第一位 + 默认选中
        for label, key in [
            ("上一收盘日 14:00 至今", "last_close_14"),
            ("最近 24 小时", "24h"),
            ("最近 48 小时", "48h"),
            ("最近 7 天", "7d"),
            ("早上 7:00 至今", "since_7am"),
            ("全部数据", "all"),
            ("自定义", "custom"),
        ]:
            self.range_preset_combo.addItem(label, key)
        self.range_preset_combo.setStyleSheet(COMBOBOX_STYLE)
        self.range_preset_combo.currentIndexChanged.connect(
            self._on_range_preset_changed
        )
        row_src.addWidget(self.range_preset_combo, 1)
        right_layout.addLayout(row_src)

        # 时间范围（起止两行紧凑）
        row_dt = QHBoxLayout()
        row_dt.addWidget(QLabel("起始:"))
        self.start_datetime = QDateTimeEdit(
            QDateTime(self._last_trading_close_14())
        )
        self.start_datetime.setCalendarPopup(True)
        self.start_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        row_dt.addWidget(self.start_datetime)
        row_dt.addWidget(QLabel("结束:"))
        self.end_datetime = QDateTimeEdit(QDateTime.currentDateTime())
        self.end_datetime.setCalendarPopup(True)
        self.end_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        row_dt.addWidget(self.end_datetime)
        right_layout.addLayout(row_dt)

        self.db_stats_label = QLabel("")
        self.db_stats_label.setWordWrap(True)
        self.db_stats_label.setStyleSheet(
            "color: #8c8c8c; font-size: 12px;"
        )
        right_layout.addWidget(self.db_stats_label)

        right_layout.addStretch()
        outer.addWidget(right_box, 2)
        return group

    def _build_params_group(self) -> QGroupBox:
        """盘后总结 + 深度思考 + 题材抽取开关。"""
        group = QGroupBox("🎯 分析参数")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        layout.addWidget(QLabel("📝 盘后总结（可选，所有任务共用）:"))
        self.summary_input = QTextEdit()
        self.summary_input.setPlaceholderText(
            "showEvent 时若为空会自动填入最近一个交易日的盘后总结。\n"
            "也可手动输入：今日大盘 ±X%、北向资金流向、板块轮动观察等。"
        )
        self.summary_input.setStyleSheet(
            "QTextEdit { border: 1px solid #d9d9d9; border-radius: 4px; "
            "padding: 8px; background-color: white; font-size: 13px; } "
            "QTextEdit:focus { border-color: #1890ff; }"
        )
        self.summary_input.setMaximumHeight(110)
        layout.addWidget(self.summary_input)

        toggle_row = QHBoxLayout()
        self.deep_thinking_check = QCheckBox(
            "🧠 深度思考（DeepSeek V4 推理模式）"
        )
        self.deep_thinking_check.setChecked(True)
        self.deep_thinking_check.setStyleSheet(
            "QCheckBox { font-size: 13px; color: #262626; }"
        )
        self.deep_thinking_check.toggled.connect(self._save_deep_thinking_pref)
        toggle_row.addWidget(self.deep_thinking_check)

        self.extract_theme_check = QCheckBox(
            "📌 完成后自动抽取题材入库（题材预测库）"
        )
        self.extract_theme_check.setChecked(True)
        self.extract_theme_check.setStyleSheet(
            "QCheckBox { font-size: 13px; color: #262626; }"
        )
        toggle_row.addWidget(self.extract_theme_check)
        toggle_row.addStretch()
        layout.addLayout(toggle_row)
        return group

    def _build_actions_row(self) -> QWidget:
        """操作按钮行：分析 / 终止 / 任务数提示。"""
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("并发:"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.blockSignals(True)
        self.concurrency_spin.setRange(
            1, AnalysisTaskManager.MAX_CONCURRENT_HARD_CAP
        )
        self.concurrency_spin.setValue(self.manager.get_max_concurrent())
        self.concurrency_spin.setSuffix(" 路")
        self.concurrency_spin.setToolTip(
            f"同时跑几个任务（1~{AnalysisTaskManager.MAX_CONCURRENT_HARD_CAP}）。\n"
            "DeepSeek 付费档实测 60+ req/min 阈值，16 并发 ≈ 32 req/min；\n"
            "并发越高 LLM 偶发 5xx 概率轻微上升 + 本机 SQLite WAL 写压上升，\n"
            "但失败只影响单个任务，不污染其他。"
        )
        self.concurrency_spin.blockSignals(False)
        self.concurrency_spin.valueChanged.connect(
            self._on_concurrency_changed
        )
        row.addWidget(self.concurrency_spin)
        row.addSpacing(10)

        self.queue_hint_label = QLabel("📋 已选 0 模板 / 后台 16 并发")
        self.queue_hint_label.setStyleSheet(
            "color: #1890ff; font-size: 12px; padding: 0 8px;"
        )
        row.addWidget(self.queue_hint_label, 1)

        self.analyze_btn = QPushButton("🚀 批量分析")
        self.analyze_btn.setStyleSheet(BUTTON_PRIMARY)
        self.analyze_btn.setMinimumHeight(36)
        self.analyze_btn.setToolTip(
            "对每个勾选模板入队 1 个任务，共用页面顶部时间窗 + 盘后总结。"
        )
        self.analyze_btn.clicked.connect(self._on_run_clicked)
        row.addWidget(self.analyze_btn)

        self.stop_btn = QPushButton("⏹ 终止选中任务")
        self.stop_btn.setStyleSheet(BUTTON_DANGER)
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setToolTip(
            "向当前选中的 RUNNING 任务发送取消请求；"
            "LLM 已发出的部分会在下次回调时被中断。"
        )
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        row.addWidget(self.stop_btn)
        return wrap

    def _build_progress_group(self) -> QGroupBox:
        group = QGroupBox("📊 进度日志（选中任务）")
        layout = QVBoxLayout(group)
        self.progress_browser = QTextBrowser()
        self.progress_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.progress_browser.setMinimumHeight(160)
        self.progress_browser.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        layout.addWidget(self.progress_browser)
        return group

    def _build_task_list_group(self) -> QGroupBox:
        """任务队列表（仿手动回测页 _build_task_list_group）。

        5 列：# / 模板 / 状态 / 用时 / 🗑（删除）
        - 点击行 → 下方进度/结果联动
        - RUNNING 任务的删除按钮禁用
        """
        group = QGroupBox(
            "📋 任务队列（连点排队，点行联动下方）"
        )
        layout = QVBoxLayout(group)

        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["#", "模板", "状态", "用时", "🗑"]
        )
        self.task_table.setStyleSheet(TABLE_STYLE)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setMinimumHeight(160)

        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.task_table.setColumnWidth(0, 40)
        self.task_table.setColumnWidth(2, 90)
        self.task_table.setColumnWidth(3, 70)
        self.task_table.setColumnWidth(4, 50)

        self.task_table.itemSelectionChanged.connect(
            self._on_task_selection_changed
        )
        layout.addWidget(self.task_table)
        return group

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("📝 分析结果（选中任务流式）")
        layout = QVBoxLayout(group)

        toolbar = QHBoxLayout()
        self.status_label = QLabel("（勾选模板 → 点「批量分析」开跑）")
        self.status_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.status_label, 1)

        self.open_report_btn = QPushButton("📂 打开报告")
        self.open_report_btn.setStyleSheet(BUTTON_SUCCESS)
        self.open_report_btn.setEnabled(False)
        self.open_report_btn.clicked.connect(self.open_report)
        toolbar.addWidget(self.open_report_btn)
        layout.addLayout(toolbar)

        self.result_browser = QTextBrowser()
        self.result_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.result_browser.setMinimumHeight(180)
        self.result_browser.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        layout.addWidget(self.result_browser)
        return group

    # =====================================================================
    # showEvent / 页面切换刷新
    # =====================================================================

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_refresh_db_stats"):
            self._refresh_db_stats()
        if hasattr(self, "template_list"):
            self.load_template_list()
        if hasattr(self, "summary_input"):
            self._auto_fill_market_summary()
        # 首次进入或主人切回页时若 range_preset 还是默认值，主动触发一次
        # 让起止时间应用主人要求的「上一收盘日 14:00 ~ 现在」
        if (
            hasattr(self, "range_preset_combo")
            and self.range_preset_combo.currentData() == "last_close_14"
        ):
            self._on_range_preset_changed()

    def refresh(self):
        self.load_config()
        self.load_template_list()
        if hasattr(self, "_refresh_db_stats"):
            self._refresh_db_stats()

    def _auto_fill_market_summary(self) -> None:
        """summary_input 为空时，自动填最近一个已收盘交易日的盘后总结。

        规则保持与旧版一致（_summary_auto_filled_with_data 守护 + 主人手动
        输入时不覆盖）。详见 ``last_settled_trade_date``。
        """
        if self._summary_auto_filled_with_data:
            return
        if self.summary_input.toPlainText().strip():
            return

        try:
            from gui.utils.market_db_helper import (
                last_settled_trade_date,
                get_cached_summary_md,
            )
        except Exception:  # noqa: BLE001
            return

        td = last_settled_trade_date()
        if td is None:
            self.summary_input.setPlaceholderText(
                "未找到交易日历缓存，请先打开【盘后数据】页面拉一次数据，"
                "或执行：python tools/market_fetch_backfill.py --days 7"
            )
            return

        md = get_cached_summary_md(td)
        if md:
            self.summary_input.setPlainText(md)
            self._summary_auto_filled_with_data = True
        else:
            self.summary_input.setPlaceholderText(
                f"未找到 {td} 的盘后总结缓存。请先执行："
                f"python tools/market_fetch.py {td}\n\n"
                "也可手动输入盘后总结，AI 将结合新闻分析…"
            )

    # =====================================================================
    # 模板多选 helpers（仿手动回测页）
    # =====================================================================

    def load_template_list(self):
        """从 ``core.ai_config.AIConfig`` 拉所有 analysis 模板，填到多选列表。

        保留旧勾选状态（按 template_id 比对），首次加载时默认勾选
        ``current_template``（AIConfig.get_current_prompt_template）。
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
        try:
            default_id = config.get_current_prompt_template()
        except Exception:  # noqa: BLE001
            default_id = None

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
                        Qt.Checked if key == default_id else Qt.Unchecked
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

    def _refresh_queue_hint(self) -> None:
        n = len(self._collect_checked_template_ids())
        conc = (
            self.concurrency_spin.value()
            if hasattr(self, "concurrency_spin")
            else AnalysisTaskManager._DEFAULT_MAX_CONCURRENT
        )
        self.queue_hint_label.setText(
            f"📋 已选 {n} 模板 / 后台 {conc} 并发 → "
            f"点「批量分析」入队 {n} 个任务"
        )

    def _goto_prompt_manager(self) -> None:
        try:
            window = self.window()
            if hasattr(window, "show_page"):
                window.show_page("prompt_manager")
        except Exception as e:  # noqa: BLE001
            print(f"[AIAnalysis] 跳转提示词管理失败: {e}")

    # =====================================================================
    # AI 配置 / 数据源 helpers（沿用旧版）
    # =====================================================================

    def _get_source_store(self, source=None):
        from services.storage import CLEAN_CURATED, get_raw_store
        source = source or self.db_source_combo.currentData()
        status = CLEAN_CURATED if source == "curated" else None
        return get_raw_store(), status

    def _get_source_time_bounds(self, source=None):
        store, status = self._get_source_store(source)
        return store.get_time_bounds(status=status)

    def _on_db_source_changed(self, _index=None):
        self._refresh_db_stats()

    def _on_range_preset_changed(self, _index=None):
        key = self.range_preset_combo.currentData()
        if key == "all":
            start, end = self._get_source_time_bounds()
            if start and end:
                self.start_datetime.setDateTime(QDateTime(start))
                self.end_datetime.setDateTime(QDateTime(end))
            return
        if key == "custom":
            return

        now = QDateTime.currentDateTime()
        self.end_datetime.setDateTime(now)

        if key == "24h":
            self.start_datetime.setDateTime(now.addSecs(-86400))
        elif key == "48h":
            self.start_datetime.setDateTime(now.addSecs(-86400 * 2))
        elif key == "7d":
            self.start_datetime.setDateTime(now.addDays(-7))
        elif key == "last_close_14":
            self.start_datetime.setDateTime(
                QDateTime(self._last_trading_close_14())
            )
        elif key == "since_7am":
            self.start_datetime.setDateTime(QDateTime(self._since_morning_7()))

    @staticmethod
    def _since_morning_7() -> datetime:
        now = datetime.now()
        today_7 = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now < today_7:
            today_7 = today_7 - timedelta(days=1)
        return today_7

    @staticmethod
    def _last_trading_close_14() -> datetime:
        """返回"上一次收盘日下午 14:00"的时间。

        规则：
        * A 股交易日 = 周一~周五
        * 今天是交易日且当前已过 14:00 → 今天 14:00
        * 否则回溯最近一个早于今天的交易日 14:00
        """
        now = datetime.now()
        today_14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if now.weekday() < 5 and now >= today_14:
            return today_14
        d = (now - timedelta(days=1)).date()
        while d.weekday() >= 5:
            d = d - timedelta(days=1)
        return datetime(d.year, d.month, d.day, 14, 0, 0)

    def _refresh_db_stats(self):
        try:
            store, status = self._get_source_store()
            label = "精选库" if status else "原始库"
            start, end = store.get_time_bounds(status=status)
            count = store.count(status=status)
            if start and end:
                span = (
                    f"{label} {count} 条 | "
                    f"数据时间 {start.strftime('%m-%d %H:%M')} ~ "
                    f"{end.strftime('%m-%d %H:%M')}"
                )
            else:
                span = f"{label} 暂无数据"
            from services.storage import get_raw_store
            raw = get_raw_store()
            self.db_stats_label.setText(
                f"原始库 {raw.count()} 条 | 精选库 {raw.count_curated()} 条 | "
                f"待清洗 {raw.count_uncleaned()} 条 | {span}"
            )
        except Exception as e:  # noqa: BLE001
            self.db_stats_label.setText(f"数据库: {e}")

    def _format_time_bounds_hint(self, source):
        start, end = self._get_source_time_bounds(source)
        if not start or not end:
            return "当前数据源为空"
        name = "精选库" if source == "curated" else "原始库"
        return (
            f"{name} 实际数据时间：\n"
            f"{start.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{end.strftime('%Y-%m-%d %H:%M')}"
        )

    # =====================================================================
    # AI 配置加载 / 保存（沿用旧版）
    # =====================================================================

    def load_config(self):
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            current = config.get_current_provider()
            for display, key in _PROVIDER_NAME_MAP.items():
                if key == current:
                    self.provider_combo.setCurrentText(display)
                    break
            self.load_provider_config()
            params = config.get_analysis_params()
            if hasattr(self, "deep_thinking_check"):
                self.deep_thinking_check.setChecked(
                    params.get("deep_thinking_enabled", True)
                )
                self._update_deep_thinking_availability()
        except Exception as e:  # noqa: BLE001
            print(f"加载配置失败: {e}")

    def _supports_deep_thinking(self) -> bool:
        if self.provider_combo.currentText() != "DeepSeek":
            return False
        model = self.model_combo.currentText().strip()
        return model.startswith("deepseek-v4")

    def _update_deep_thinking_availability(self, *_args):
        if not hasattr(self, "deep_thinking_check"):
            return
        supported = self._supports_deep_thinking()
        self.deep_thinking_check.setEnabled(supported)
        if supported:
            self.deep_thinking_check.setToolTip(
                "开启后 DeepSeek V4 系列模型启用 thinking + "
                "reasoning_effort=max，分析质量更高但耗时更长。"
            )
        else:
            self.deep_thinking_check.setToolTip(
                "仅 DeepSeek V4 系列模型支持深度思考；当前不可用。"
            )

    def _save_deep_thinking_pref(self, checked: bool):
        try:
            from core.ai_config import AIConfig
            AIConfig().set_analysis_param("deep_thinking_enabled", checked)
        except Exception as e:  # noqa: BLE001
            print(f"保存深度思考配置失败: {e}")

    def load_provider_config(self):
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            provider = _PROVIDER_NAME_MAP.get(
                self.provider_combo.currentText()
            )
            if provider:
                api_key = config.get_api_key(provider)
                if api_key:
                    self.api_key_input.setText(api_key)
                else:
                    self.api_key_input.clear()
                model = config.get_model(provider)
                index = self.model_combo.findText(model)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)
        except Exception as e:  # noqa: BLE001
            print(f"加载服务商配置失败: {e}")

    def on_provider_changed(self, _text):
        self.update_model_list()
        self.load_provider_config()
        self._update_deep_thinking_availability()

    def update_model_list(self):
        from core.ai_config import AIConfig
        provider_key = _PROVIDER_NAME_MAP.get(
            self.provider_combo.currentText(), 'openai'
        )
        config = AIConfig()
        models = config.get_available_models(provider_key)
        self.model_combo.clear()
        self.model_combo.addItems(models)

    def save_api_key(self):
        try:
            from core.ai_config import AIConfig
            provider = _PROVIDER_NAME_MAP.get(
                self.provider_combo.currentText()
            )
            api_key = self.api_key_input.text().strip()
            model = self.model_combo.currentText()
            if not api_key:
                QMessageBox.warning(self, "警告", "请输入API Key")
                return
            config = AIConfig()
            config.set_api_key(provider, api_key)
            config.set_model(provider, model)
            config.set_current_provider(provider)
            QMessageBox.information(self, "成功", "配置已保存")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"保存失败: {str(e)}")

    def test_connection(self):
        try:
            from core.ai_news_analyzer import AINewsAnalyzer
            api_key = self.api_key_input.text().strip()
            if not api_key:
                QMessageBox.warning(self, "警告", "请先输入API Key")
                return
            self.save_api_key()
            provider = _PROVIDER_NAME_MAP.get(
                self.provider_combo.currentText()
            )
            analyzer = AINewsAnalyzer()
            result = analyzer.test_connection(provider)
            if result.get('success'):
                QMessageBox.information(
                    self, "成功",
                    "连接测试成功！\n" + result.get('message', '')
                )
            else:
                QMessageBox.warning(
                    self, "失败",
                    "连接测试失败！\n" + result.get('message', '')
                )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"测试失败: {str(e)}")

    def _on_concurrency_changed(self, n: int) -> None:
        actual = self.manager.set_max_concurrent(n)
        if actual != n:
            self.concurrency_spin.blockSignals(True)
            try:
                self.concurrency_spin.setValue(actual)
            finally:
                self.concurrency_spin.blockSignals(False)
        self._refresh_queue_hint()

    # =====================================================================
    # 批量入队
    # =====================================================================

    def _on_run_clicked(self):
        """对所有勾选模板入队 1 个任务，共用同一组参数。"""
        templates = self._collect_checked_templates()
        if not templates:
            QMessageBox.warning(self, "缺参数", "请先勾选至少一个模板")
            return

        api_key = self.api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "警告", "请先配置 API Key")
            return

        sqlite_source = self.db_source_combo.currentData()
        sqlite_start = self.start_datetime.dateTime().toPyDateTime()
        sqlite_end = self.end_datetime.dateTime().toPyDateTime()
        if sqlite_start >= sqlite_end:
            QMessageBox.warning(self, "警告", "起始时间必须早于结束时间")
            return

        # 预扫一次新闻数量，避免空入队
        try:
            from services.analysis_service import AnalysisService
            news_count = len(
                AnalysisService().load_news(
                    sqlite_source, sqlite_start, sqlite_end
                )
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"加载新闻失败: {exc}")
            return
        if news_count == 0:
            hint = self._format_time_bounds_hint(sqlite_source)
            QMessageBox.warning(
                self, "警告",
                f"选定时间范围内没有新闻数据。\n\n{hint}\n\n"
                "请将快捷范围改为「全部数据」，或手动调整起止时间。"
            )
            return

        # ≥3 任务弹确认（避免主人手抖批量耗 LLM 配额）
        n_tasks = len(templates)
        if n_tasks >= 3:
            tpl_names = "、".join(label for _tid, label in templates[:3])
            if len(templates) > 3:
                tpl_names += f" 等 {len(templates)} 个"
            thinking_on = (
                self.deep_thinking_check.isChecked()
                and self._supports_deep_thinking()
            )
            reply = QMessageBox.question(
                self, "确认批量分析",
                f"将创建 <b>{n_tasks}</b> 个任务<br>"
                f"&nbsp;&nbsp;模板：{tpl_names}<br>"
                f"&nbsp;&nbsp;新闻数：{news_count} 条<br>"
                f"&nbsp;&nbsp;时间窗：{sqlite_start.strftime('%m-%d %H:%M')} ~ "
                f"{sqlite_end.strftime('%m-%d %H:%M')}<br>"
                f"&nbsp;&nbsp;深度思考：{'开启' if thinking_on else '关闭'}<br>"
                f"&nbsp;&nbsp;并发：{self.concurrency_spin.value()} 路<br><br>"
                f"将消耗 ~{n_tasks} 次 LLM 调用，确认继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # 入队
        provider = _PROVIDER_NAME_MAP.get(self.provider_combo.currentText())
        market_summary = self.summary_input.toPlainText().strip()
        thinking_on = (
            self.deep_thinking_check.isChecked()
            and self._supports_deep_thinking()
        )
        extract_themes = self.extract_theme_check.isChecked()

        enqueued = 0
        for tid, label in templates:
            self.manager.enqueue({
                "template_id": tid,
                "template_label": label,
                "provider": provider,
                "sqlite_source": sqlite_source,
                "sqlite_start": sqlite_start,
                "sqlite_end": sqlite_end,
                "market_summary": market_summary,
                "extract_themes": extract_themes,
                "enable_deep_thinking": thinking_on,
            })
            enqueued += 1

        self.status_label.setText(
            f"<span style='color:#1890ff'>📋 已入队 {enqueued} 个任务"
            f"（{news_count} 条新闻）</span>"
        )

    def _on_stop_clicked(self):
        """终止当前选中的 RUNNING 任务。"""
        if self._current_task_id is None:
            QMessageBox.information(
                self, "未选中", "请先在任务队列里点一行"
            )
            return
        task = self.manager.get(self._current_task_id)
        if task is None:
            return
        if task.status != TaskStatus.RUNNING:
            QMessageBox.information(
                self, "非运行中",
                f"任务 #{task.task_id} 状态={task.status.value}，"
                "只能取消 RUNNING 任务"
            )
            return
        ok = self.manager.cancel_running(task.task_id)
        if ok:
            self.add_progress(
                f"已发送取消请求（任务 #{task.task_id}），等下次回调触发"
            )
        else:
            QMessageBox.warning(self, "取消失败", "manager 拒绝了取消请求")

    # =====================================================================
    # manager 信号槽（仿手动回测页）
    # =====================================================================

    def _wire_manager_signals(self) -> None:
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_started.connect(self._on_task_started)
        self.manager.task_progress.connect(self._on_task_progress)
        self.manager.task_streaming.connect(self._on_task_streaming)
        self.manager.task_finished.connect(self._on_task_finished)
        self.manager.task_removed.connect(self._on_task_removed)

    def _start_elapsed_timer(self) -> None:
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_running_elapsed)
        self._tick_timer.start()

    # --- 表行操作 helpers ---------------------------------------------------

    def _row_of_task(self, task_id: int) -> int:
        for r in range(self.task_table.rowCount()):
            item = self.task_table.item(r, 0)
            if item is not None and int(item.data(Qt.UserRole)) == task_id:
                return r
        return -1

    def _set_status_cell(self, row: int, task: AnalysisTask) -> None:
        item = QTableWidgetItem(STATUS_LABEL[task.status])
        item.setForeground(QColor(STATUS_COLOR[task.status]))
        item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 2, item)

    def _set_elapsed_cell(self, row: int, task: AnalysisTask) -> None:
        item = QTableWidgetItem(task.elapsed_text())
        item.setTextAlignment(Qt.AlignCenter)
        self.task_table.setItem(row, 3, item)

    def _set_delete_button(self, row: int, task: AnalysisTask) -> None:
        btn = QPushButton("🗑")
        btn.setToolTip("删除该任务行（不动 md 文件 / 数据库）")
        btn.setEnabled(task.status != TaskStatus.RUNNING)
        btn.clicked.connect(
            lambda _checked=False, tid=task.task_id:
            self._on_delete_task_clicked(tid)
        )
        self.task_table.setCellWidget(row, 4, btn)

    # --- manager 信号槽 -----------------------------------------------------

    def _on_task_added(self, task_id: int) -> None:
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
        self._set_status_cell(row, task)
        self._set_elapsed_cell(row, task)
        self._set_delete_button(row, task)

        # 自动选中新行
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
        if task_id == self._current_task_id:
            self.progress_browser.insertPlainText(line)
            self.progress_browser.moveCursor(QTextCursor.End)

    def _on_task_streaming(self, task_id: int, chunk: str) -> None:
        if task_id == self._current_task_id:
            self.result_browser.insertPlainText(chunk)
            self.result_browser.moveCursor(QTextCursor.End)

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

    # --- 行选中联动 ---------------------------------------------------------

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
        """回灌选中任务的 log / stream / 状态到下方区域。"""
        tid = self._current_task_id
        if tid is None:
            self._clear_lower_panels()
            return
        task = self.manager.get(tid)
        if task is None:
            self._clear_lower_panels()
            return

        # 进度日志回灌
        self.progress_browser.clear()
        for line in task.log_buffer:
            self.progress_browser.insertPlainText(line)
        self.progress_browser.moveCursor(QTextCursor.End)

        # 流式结果回灌
        self.result_browser.clear()
        for chunk in task.stream_buffer:
            self.result_browser.insertPlainText(chunk)
        self.result_browser.moveCursor(QTextCursor.End)

        # 状态条 + 打开报告按钮
        self._update_status_label_for(task)
        result = task.result or {}
        report_file = result.get("report_file")
        if task.status == TaskStatus.SUCCESS and report_file:
            abs_path = resolve_project_path(report_file)
            if abs_path and os.path.exists(abs_path):
                self.open_report_btn.setEnabled(True)
                self._last_report_path = abs_path
                return
        self.open_report_btn.setEnabled(False)
        self._last_report_path = None

    def _update_status_label_for(self, task: AnalysisTask) -> None:
        color = STATUS_COLOR[task.status]
        label = STATUS_LABEL[task.status]
        meta = f"#{task.task_id} {task.template_label}"
        suffix = ""
        if task.is_terminal:
            elapsed = (
                f"{task.elapsed_ms / 1000:.1f}s"
                if task.elapsed_ms is not None else "-"
            )
            res = task.result or {}
            if task.status == TaskStatus.SUCCESS:
                news = res.get("news_count", 0)
                themes = res.get("theme_count", 0)
                suffix = (
                    f"&nbsp;&nbsp;news={news} themes={themes} &nbsp;{elapsed}"
                )
            elif task.status == TaskStatus.SKIPPED:
                suffix = (
                    f"&nbsp;&nbsp;<span style='color:#c80'>"
                    f"{task.error or ''}</span> &nbsp;{elapsed}"
                )
            else:
                err = (task.error or "")[:80]
                suffix = (
                    f"&nbsp;&nbsp;<span style='color:#c00'>{err}</span>"
                    f" &nbsp;{elapsed}"
                )
        self.status_label.setText(
            f"<span style='color:{color}'>{label}</span>"
            f"&nbsp;&nbsp;<span style='color:#888'>{meta}</span>"
            f"{suffix}"
        )

    def _clear_lower_panels(self) -> None:
        self.status_label.setText(
            "（勾选模板 → 点「批量分析」开跑）"
        )
        self.progress_browser.clear()
        self.result_browser.clear()
        self.open_report_btn.setEnabled(False)
        self._last_report_path = None

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
        self.manager.remove(task_id)

    # --- QTimer 1s 刷 RUNNING 用时 -----------------------------------------

    def _refresh_running_elapsed(self) -> None:
        for task in self.manager.list_tasks():
            if task.status != TaskStatus.RUNNING:
                continue
            row = self._row_of_task(task.task_id)
            if row < 0:
                continue
            self._set_elapsed_cell(row, task)

    # =====================================================================
    # 兼容老接口 / 关页面
    # =====================================================================

    def add_progress(self, message: str) -> None:
        """兼容老调用：把消息追加到当前进度区。

        新代码请走 manager.task_progress 信号；此方法仅供页面内部少数
        直接刷状态条的逻辑（如「取消请求已发送」提示）使用。
        """
        ts = datetime.now().strftime("%H:%M:%S")
        self.progress_browser.append(f"[{ts}] {message}")
        self.progress_browser.moveCursor(QTextCursor.End)

    def open_report(self):
        """打开当前选中任务的报告（优先 Typora，否则系统默认）。"""
        report_path = self._last_report_path
        if not report_path or not os.path.exists(report_path):
            QMessageBox.warning(self, "警告", "报告文件不存在！")
            return

        import subprocess
        typora_path = find_typora_executable()
        if typora_path:
            try:
                subprocess.Popen([typora_path, report_path])
                self.add_progress(
                    f"已使用 Typora 打开报告: {os.path.basename(report_path)}"
                )
                return
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(
                    self, "警告", f"使用 Typora 打开失败: {str(e)}"
                )

        try:
            os.startfile(report_path)  # Windows
            self.add_progress(
                f"已打开报告: {os.path.basename(report_path)}"
            )
        except AttributeError:
            try:
                subprocess.Popen(["xdg-open", report_path])
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "错误", f"打开报告失败: {str(e)}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"打开报告失败: {str(e)}")

    def closeEvent(self, event):  # noqa: N802
        """关页面（或 main 退出）时取消 PENDING；RUNNING 自然结束。"""
        try:
            self.manager.shutdown()
        finally:
            super().closeEvent(event)
