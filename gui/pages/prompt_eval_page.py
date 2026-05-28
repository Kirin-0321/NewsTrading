"""模板评估页（2026-05-27 master-detail 重构 · Phase 4 + Step 6.4）。

业务定位
--------
让主人在 GUI 上一眼看出 **每个模板的 D+1~D+5 表现** + **下钻到每份报告**。

UI 布局
-------
::

    ┌──────────────────────────────────────────────────────────────┐
    │ 控制条：时间维度 radio | 时间范围 | 真/回测/全部 | 忽略版本   │
    │         + 重打分 + 导出 + 刷新                                │
    ├──────────────────────────────────────────────────────────────┤
    │ 【上表 master】 模板汇总（按 prompt_id 聚合）                │
    │   模板 | 版本 | 样本 | D+1 D+2 D+3 D+4 D+5 | α | 命中率 |    │
    │   方向准 | AI 高质量 | 最近报告日                              │
    ├──────────────────────────────────────────────────────────────┤
    │ 【下表 detail】 选中模板的报告级明细（按 ai_reports.id 聚合）│
    │   报告日期 | 真/回 | 题材数 | D+1~D+5 | α | 命中率 | 文件名   │
    │   （双击 → 跳到「题材预测」页查看该报告的题材列表）           │
    └──────────────────────────────────────────────────────────────┘

时间维度
--------
* ``score_date``：按"最近 N 天**发生过打分**"过滤（调度/CLI 视角）
* ``report_date`` **默认**：按"最近 N 天**生成**的报告"过滤（评估视角）

调用链
------
::

    refresh()
       ├── get_template_eval(...)                  → 填上表
       └── get_report_eval(prompt_id=选中, ...)     → 填下表

    _on_rescore()
       └── RescoreWorker → scoring_service.rescore_range(...)

    _on_report_double_clicked()
       └── self.theme_drilldown_requested.emit(report_date, prompt_id)
           → main_window 切到题材预测页并预选

未实现
------
* ``AI 高质量占比`` 依赖 Phase 7 ``ai_scorer.py``，本期占 ``-``
* 折线图区不接入（plan M4.2 单独任务）
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from gui.utils.date_format import format_yyyymmdd_to_dash

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QRadioButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, TABLE_STYLE,
)
from gui.workers.delete_report_worker import DeleteReportWorker
from gui.workers.rescore_worker import RescoreWorker


_RANGE_OPTIONS = [
    ("最近 7 天", 7),
    ("最近 30 天", 30),
    ("最近 90 天", 90),
    ("最近 1 年", 365),
]


_BACKTEST_FILTERS = [
    ("全部", None),
    ("仅真实 (is_backtest=0)", 0),
    ("仅回测 (is_backtest=1)", 1),
]


_TPL_COLS = [
    ("模板", 220), ("版本", 60), ("真/回测", 70),
    ("题材", 50), ("已打", 50),
    ("D+1", 75), ("D+2", 75), ("D+3", 75), ("D+4", 75), ("D+5", 75),
    ("α", 75), ("命中率", 70), ("方向准", 70),
    ("AI 高质量", 80), ("最近报告日", 100),
]


# 报告树（QTreeWidget）共享 13 列定义（2026-05-28 树形展开重构）
#
# 同一列在 3 级（📄 报告 / 🎯 题材 / 📈 标的）下语义对齐：
#   * 列 0  名称：📄报告日期+真/回 / 🎯题材名 / 📈标的名（带图标前缀）
#   * 列 1  类型/等级/角色
#   * 列 2  辅助：📄版本 / 🎯板块代码 / 📈标准化代码
#   * 列 3  子项数/排名：📄题材数 / 🎯排名 / 📈—
#   * 列 4  进度/分数：📄打分 X/Y / 🎯强度分 / 📈—
#   * 列 5~9  D+1~D+5（单位 %，红涨绿跌染色统一）
#   * 列 10 α / —
#   * 列 11 命中率（📄/🎯）/ 命中标记 ✓✗（📈）
#   * 列 12 文件名 / AI 评语 / 理由（stretch 末列，📄 行尾内嵌「打分/删除」按钮）
_TREE_COLS = [
    ("名称",            260),  # 0
    ("类型/等级/角色",   100),  # 1
    ("辅助",            100),  # 2
    ("子项/排名",        70),  # 3
    ("进度/分数",        80),  # 4
    ("D+1",             75),  # 5
    ("D+2",             75),  # 6
    ("D+3",             75),  # 7
    ("D+4",             75),  # 8
    ("D+5",             75),  # 9
    ("α",               75),  # 10
    ("命中率",           80),  # 11
    ("文件名/理由/按钮",   0),  # 12 stretch
]
_COL_NAME = 0
_COL_TYPE = 1
_COL_AUX = 2
_COL_COUNT = 3
_COL_SCORE = 4
_COL_D1, _COL_D2, _COL_D3, _COL_D4, _COL_D5 = 5, 6, 7, 8, 9
_COL_ALPHA = 10
_COL_HIT = 11
_COL_TAIL = 12

# 节点类型枚举（写在 QTreeWidgetItem.data(0, Qt.UserRole) 的 dict.kind 中）
_KIND_REPORT = "report"
_KIND_THEME = "theme"
_KIND_STOCK = "stock"


# 按钮三态样式（评估页打分按钮专用）
_BTN_NONE = """
QPushButton {
    background-color: #52c41a; color: white; border-radius: 4px;
    padding: 4px 8px; font-weight: 600;
}
QPushButton:hover { background-color: #389e0d; }
QPushButton:disabled { background-color: #d9d9d9; color: #999; }
"""
_BTN_PARTIAL = """
QPushButton {
    background-color: #faad14; color: white; border-radius: 4px;
    padding: 4px 8px; font-weight: 600;
}
QPushButton:hover { background-color: #d48806; }
QPushButton:disabled { background-color: #d9d9d9; color: #999; }
"""
_BTN_FULL = """
QPushButton {
    background-color: #1890ff; color: white; border-radius: 4px;
    padding: 4px 8px; font-weight: 600;
}
QPushButton:hover { background-color: #096dd9; }
QPushButton:disabled { background-color: #d9d9d9; color: #999; }
"""
# 删除按钮：backtest 浅红 / real 深红警告
_BTN_DEL_BT = """
QPushButton {
    background-color: #ff7875; color: white; border-radius: 4px;
    padding: 4px 8px; font-weight: 600;
}
QPushButton:hover { background-color: #ff4d4f; }
QPushButton:disabled { background-color: #d9d9d9; color: #999; }
"""
_BTN_DEL_REAL = """
QPushButton {
    background-color: #a8071a; color: white; border-radius: 4px;
    padding: 4px 8px; font-weight: 700;
}
QPushButton:hover { background-color: #820014; }
QPushButton:disabled { background-color: #d9d9d9; color: #999; }
"""


_LOW_SAMPLE_THRESHOLD = 5


# ---------------------------------------------------------------------------
# 树节点子类：仅顶层（报告级）参与排序，子级保持插入顺序
# ---------------------------------------------------------------------------


class _ReportTreeItem(QTreeWidgetItem):
    """支持顶层排序、子级锁顺序的 QTreeWidgetItem。

    设计要点：
        * 顶层 📄 报告行：按当前 sortColumn 比较 ``Qt.UserRole + 1`` 槽里的
          原始可比较值（int / float / str / None），NULL 永远排末尾
        * 子级（🎯 题材 / 📈 标的）：``__lt__`` 永远返回 False，让 Qt 内部
          排序时不动子节点（QTreeWidget 默认会递归全树）
    """

    def __lt__(self, other):  # noqa: D401
        # 子级（parent 非空）保持插入顺序
        if self.parent() is not None:
            return False
        tree = self.treeWidget()
        if tree is None:
            return False
        col = tree.sortColumn()
        a = self.data(col, Qt.UserRole + 1)
        b = other.data(col, Qt.UserRole + 1) if other is not None else None
        # NULL 末尾（升序时排后；降序时 Qt 自动反转，依然在末尾）
        if a is None and b is None:
            return False
        if a is None:
            return False
        if b is None:
            return True
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


# ---------------------------------------------------------------------------
# 主页面
# ---------------------------------------------------------------------------


class PromptEvalPage(QWidget):
    """模板评估页（master-detail 双表）。"""

    #: 双击下表行时发出：(report_date YYYYMMDD, prompt_id)
    #: 主窗口接住后切换到题材预测页并预选筛选器
    theme_drilldown_requested = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._template_rows: List[Dict] = []
        self._report_rows: List[Dict] = []
        self._selected_prompt_id: Optional[str] = None
        self._rescore_worker: Optional[RescoreWorker] = None
        self._delete_worker: Optional[DeleteReportWorker] = None
        # 树形展开缓存：{ai_reports.id: List[theme_dict]}, {theme_id: List[stock_dict]}
        self._theme_cache: Dict[int, List[Dict]] = {}
        self._stock_cache: Dict[int, List[Dict]] = {}
        self.init_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        title = QLabel("📊 模板评估面板")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "上表：模板汇总（按 prompt_id 聚合 D+1~D+5）　|　"
            "下表：选中模板的每份报告明细　|　"
            "双击下表行跳到「题材预测」页查看题材"
        )
        subtitle.setStyleSheet("color: #666; font-size: 12px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_control_bar())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_template_group())
        splitter.addWidget(self._build_report_group())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)

    def _build_control_bar(self) -> QGroupBox:
        group = QGroupBox("🎛️ 筛选与操作")
        outer = QVBoxLayout(group)

        # 第 1 行：时间维度 + 时间范围 + 回测/真实 + 忽略版本
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("时间维度:"))
        self.dim_report_radio = QRadioButton("按 report_date")
        self.dim_score_radio = QRadioButton("按 score_date")
        self.dim_report_radio.setChecked(True)  # 评估页默认
        self.dim_report_radio.setToolTip(
            "按报告生成日期过滤（评估视角：最近 N 天生成的报告效果）"
        )
        self.dim_score_radio.setToolTip(
            "按打分日期过滤（调度视角：最近 N 天有打分发生）"
        )
        self._dim_group = QButtonGroup(self)
        self._dim_group.addButton(self.dim_report_radio, 0)
        self._dim_group.addButton(self.dim_score_radio, 1)
        self._dim_group.buttonClicked.connect(lambda _b: self.refresh())
        row1.addWidget(self.dim_report_radio)
        row1.addWidget(self.dim_score_radio)
        row1.addSpacing(15)

        row1.addWidget(QLabel("时间范围:"))
        self.range_combo = QComboBox()
        self.range_combo.setStyleSheet(COMBOBOX_STYLE)
        for label, days in _RANGE_OPTIONS:
            self.range_combo.addItem(label, days)
        self.range_combo.setCurrentIndex(1)  # 30 天
        self.range_combo.currentIndexChanged.connect(self.refresh)
        row1.addWidget(self.range_combo)
        row1.addSpacing(15)

        row1.addWidget(QLabel("样本类型:"))
        self.backtest_combo = QComboBox()
        self.backtest_combo.setStyleSheet(COMBOBOX_STYLE)
        for label, val in _BACKTEST_FILTERS:
            self.backtest_combo.addItem(label, val)
        self.backtest_combo.setCurrentIndex(0)  # 全部
        self.backtest_combo.currentIndexChanged.connect(self.refresh)
        row1.addWidget(self.backtest_combo)
        row1.addSpacing(15)

        self.ignore_version_check = QCheckBox("忽略版本号（合并 v1/v2/v3）")
        self.ignore_version_check.setChecked(True)
        self.ignore_version_check.stateChanged.connect(self.refresh)
        row1.addWidget(self.ignore_version_check)

        row1.addStretch()
        outer.addLayout(row1)

        # 第 2 行：操作按钮 + 状态提示
        row2 = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #666;")
        row2.addWidget(self.status_label, 1)

        self.rescore_btn = QPushButton("🔁 重打分（按筛选范围）")
        self.rescore_btn.setStyleSheet(BUTTON_DANGER)
        self.rescore_btn.setToolTip(
            "对当前【时间范围】内的所有题材重跑 D+1~D+5 脚本打分。"
            "不会重新调 AI 评分员。"
        )
        self.rescore_btn.clicked.connect(self._on_rescore)
        row2.addWidget(self.rescore_btn)

        self.export_btn = QPushButton("⬇️ 导出 CSV")
        self.export_btn.setStyleSheet(BUTTON_SUCCESS)
        self.export_btn.clicked.connect(self._on_export_csv)
        row2.addWidget(self.export_btn)

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setStyleSheet(BUTTON_PRIMARY)
        self.refresh_btn.clicked.connect(self.refresh)
        row2.addWidget(self.refresh_btn)

        outer.addLayout(row2)
        return group

    def _build_template_group(self) -> QGroupBox:
        group = QGroupBox("📈 模板汇总")
        layout = QVBoxLayout(group)

        self.tpl_table = QTableWidget(0, len(_TPL_COLS))
        self.tpl_table.setHorizontalHeaderLabels(
            [c[0] for c in _TPL_COLS]
        )
        self.tpl_table.setStyleSheet(TABLE_STYLE)
        self.tpl_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tpl_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tpl_table.setSelectionMode(QTableWidget.SingleSelection)
        self.tpl_table.verticalHeader().setVisible(False)
        for i, (_, w) in enumerate(_TPL_COLS):
            if w > 0:
                self.tpl_table.setColumnWidth(i, w)
        self.tpl_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.tpl_table.itemSelectionChanged.connect(
            self._on_template_selected
        )
        layout.addWidget(self.tpl_table)

        self.tpl_stats_label = QLabel("")
        self.tpl_stats_label.setStyleSheet("color: #8c8c8c;")
        layout.addWidget(self.tpl_stats_label)
        return group

    def _build_report_group(self) -> QGroupBox:
        group = QGroupBox(
            "📄 报告 / 🎯 题材 / 📈 标的　"
            "(点 ▶ 逐级展开｜双击 📄 跳「题材预测」页｜点击表头排序)"
        )
        layout = QVBoxLayout(group)

        self.report_tree = QTreeWidget()
        self.report_tree.setColumnCount(len(_TREE_COLS))
        self.report_tree.setHeaderLabels([c[0] for c in _TREE_COLS])
        self.report_tree.setStyleSheet(TABLE_STYLE)
        self.report_tree.setUniformRowHeights(True)
        self.report_tree.setRootIsDecorated(True)
        self.report_tree.setAlternatingRowColors(True)
        self.report_tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.report_tree.setSortingEnabled(True)
        # 默认按报告日期降序（_COL_NAME 列存日期作为可比较值）
        self.report_tree.sortByColumn(_COL_NAME, Qt.DescendingOrder)

        for i, (_, w) in enumerate(_TREE_COLS):
            if w > 0:
                self.report_tree.setColumnWidth(i, w)
        self.report_tree.header().setSectionResizeMode(
            _COL_TAIL, QHeaderView.Stretch
        )

        self.report_tree.itemExpanded.connect(self._on_item_expanded)
        self.report_tree.itemDoubleClicked.connect(
            self._on_tree_double_clicked
        )
        layout.addWidget(self.report_tree)

        self.report_stats_label = QLabel("")
        self.report_stats_label.setStyleSheet("color: #8c8c8c;")
        layout.addWidget(self.report_stats_label)
        return group

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _current_filters(self) -> dict:
        return {
            "days": int(self.range_combo.currentData() or 30),
            "time_dim": (
                "report_date"
                if self.dim_report_radio.isChecked()
                else "score_date"
            ),
            "is_backtest_filter": self.backtest_combo.currentData(),
            "ignore_version": self.ignore_version_check.isChecked(),
        }

    def refresh(self):
        """刷新上下两表（独立查询，互不阻塞）。"""
        self.status_label.setText("加载中...")
        try:
            f = self._current_filters()
            from services.scoring.scoring_service import (
                get_template_eval,
            )
            tpl_rows = get_template_eval(
                days=f["days"],
                ignore_version=f["ignore_version"],
                is_backtest_filter=f["is_backtest_filter"],
                time_dim=f["time_dim"],
            )
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(
                f"<span style='color:#c00'>上表加载失败: {exc}</span>"
            )
            return

        self._template_rows = tpl_rows
        self._render_template_table(tpl_rows)
        self.tpl_stats_label.setText(
            f"共 {len(tpl_rows)} 个模板组　|　"
            f"时间维度: {f['time_dim']}　|　"
            f"样本类型: {self.backtest_combo.currentText()}"
        )

        # 下表跟着重查（如果之前选了某模板就保留过滤）
        self._reload_report_table()

        self.status_label.setText(
            f"<span style='color:#0a0'>✅ 加载完成</span> "
            f"模板 {len(tpl_rows)} / 报告 {len(self._report_rows)}"
        )

    def _reload_report_table(self):
        """根据当前选中的模板重查报告树（顶层 📄 节点）。"""
        try:
            f = self._current_filters()
            from services.scoring.scoring_service import get_report_eval
            rep_rows = get_report_eval(
                days=f["days"],
                prompt_id=self._selected_prompt_id,
                is_backtest_filter=f["is_backtest_filter"],
                time_dim=f["time_dim"],
            )
        except Exception as exc:  # noqa: BLE001
            self.report_stats_label.setText(
                f"<span style='color:#c00'>下表加载失败: {exc}</span>"
            )
            return

        self._report_rows = rep_rows
        # 树重建会丢失展开状态 + 缓存可能过期，整体清缓存（点 ▶ 时按需重拉）
        self._theme_cache.clear()
        self._stock_cache.clear()
        self._render_tree_top_level(rep_rows)
        prompt_tag = (
            f"prompt_id={self._selected_prompt_id}"
            if self._selected_prompt_id
            else "全部模板"
        )
        self.report_stats_label.setText(
            f"共 {len(rep_rows)} 份报告　({prompt_tag})"
        )

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _render_template_table(self, rows: List[Dict]) -> None:
        from gui.utils.prompt_name_helper import friendly_prompt_name
        self.tpl_table.setRowCount(len(rows))
        for r_idx, data in enumerate(rows):
            pid = data.get("prompt_id") or "—"
            themes_total = int(
                data.get("themes_total")
                or data.get("sample_count") or 0
            )
            scored_themes = int(data.get("scored_themes") or 0)
            cells = [
                friendly_prompt_name(pid, fallback=pid),
                str(data.get("prompt_version") or "—"),
                self._fmt_backtest_marker(data),
                str(themes_total),
                str(scored_themes),
                _fmt_pct(data.get("d1_avg")),
                _fmt_pct(data.get("d2_avg")),
                _fmt_pct(data.get("d3_avg")),
                _fmt_pct(data.get("d4_avg")),
                _fmt_pct(data.get("d5_avg")),
                _fmt_pct(data.get("alpha_avg")),
                _fmt_rate(data.get("hit_rate_avg")),
                _fmt_rate(data.get("direction_correct_rate")),
                "—",  # AI 高质量占比（Phase 7 待实现）
                format_yyyymmdd_to_dash(data.get("last_report_date") or "—"),
            ]
            low_sample = themes_total < _LOW_SAMPLE_THRESHOLD
            unscored = themes_total > 0 and scored_themes == 0
            for c_idx, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                # prompt_id 实际值塞到第 0 列的 Qt.UserRole
                if c_idx == 0:
                    item.setData(Qt.UserRole, pid)
                # 题材列（小样本警告）
                if c_idx == 3 and low_sample:
                    item.setBackground(QColor("#fff7e6"))
                    item.setToolTip(
                        f"题材仅 {themes_total}，建议 ≥ "
                        f"{_LOW_SAMPLE_THRESHOLD} 才有统计意义"
                    )
                # 已打列（全 0 时高亮提示需要打分）
                if c_idx == 4 and unscored:
                    item.setBackground(QColor("#fff1f0"))
                    item.setForeground(QColor("#cf1322"))
                    item.setToolTip(
                        "该模板下题材都未打分，去下表点「打分」按钮"
                    )
                # D+N / α 染色（c_idx 5~10）
                if c_idx in (5, 6, 7, 8, 9, 10):
                    self._colorize_pct(item, data, c_idx, "d_or_alpha")
                # AI 高质量占比列：tooltip 说明
                if c_idx == 13:
                    item.setForeground(QColor("#bfbfbf"))
                    item.setToolTip("等 Phase 7 AI 评分员（ai_scorer.py）落地")
                self.tpl_table.setItem(r_idx, c_idx, item)

    def _render_tree_top_level(self, rows: List[Dict]) -> None:
        """渲染顶层 📄 报告节点；每个节点挂一个 placeholder 让 ▶ 出现。"""
        # 排序时短暂关闭（一次性 fill 完再开）→ 防止边填边触发排序
        self.report_tree.setSortingEnabled(False)
        self.report_tree.clear()

        from gui.utils.prompt_name_helper import friendly_prompt_name
        for data in rows:
            bt = int(data.get("is_backtest") or 0)
            fp = data.get("file_path") or ""
            fname = Path(fp).name if fp else "-"
            themes_n = int(data.get("themes_count") or 0)
            scored_n = int(data.get("scored_pairs") or 0)
            expected_n = int(data.get("expected_pairs") or 0)
            status = data.get("score_status") or "none"
            pid_display = friendly_prompt_name(
                data.get("prompt_id") or "",
                fallback=data.get("prompt_id") or "—",
            )

            name_text = (
                f"📄 {format_yyyymmdd_to_dash(data.get('report_date') or '—')}"
                + ("　[回]" if bt == 1 else "　[真]")
            )
            type_text = pid_display
            aux_text = str(data.get("prompt_version") or "—")
            count_text = str(themes_n)
            score_text = (
                f"{scored_n}/{expected_n}" if expected_n else "0/0"
            )

            item = _ReportTreeItem([
                name_text,
                type_text,
                aux_text,
                count_text,
                score_text,
                _fmt_pct(data.get("d1_avg")),
                _fmt_pct(data.get("d2_avg")),
                _fmt_pct(data.get("d3_avg")),
                _fmt_pct(data.get("d4_avg")),
                _fmt_pct(data.get("d5_avg")),
                _fmt_pct(data.get("alpha_avg")),
                _fmt_rate(data.get("hit_rate_avg")),
                "",  # 尾列由 setItemWidget 装按钮容器
            ])
            # 节点元数据（懒加载用）
            item.setData(_COL_NAME, Qt.UserRole, {
                "kind": _KIND_REPORT,
                "loaded": False,
                "data": data,
            })
            # 排序辅助槽（UserRole+1 存可比较的原始值）
            item.setData(_COL_NAME, Qt.UserRole + 1,
                         data.get("report_date") or "")
            item.setData(_COL_TYPE, Qt.UserRole + 1, pid_display)
            item.setData(_COL_COUNT, Qt.UserRole + 1, themes_n)
            item.setData(_COL_SCORE, Qt.UserRole + 1, scored_n)
            for c_idx, key in zip(
                (_COL_D1, _COL_D2, _COL_D3, _COL_D4, _COL_D5, _COL_ALPHA),
                ("d1_avg", "d2_avg", "d3_avg", "d4_avg",
                 "d5_avg", "alpha_avg"),
            ):
                v = data.get(key)
                item.setData(c_idx, Qt.UserRole + 1,
                             None if v is None else float(v))
            item.setData(
                _COL_HIT, Qt.UserRole + 1,
                None if data.get("hit_rate_avg") is None
                else float(data.get("hit_rate_avg")),
            )

            # 染色
            if status == "none":
                item.setForeground(_COL_SCORE, QColor("#cf1322"))
            elif status == "partial":
                item.setForeground(_COL_SCORE, QColor("#d48806"))
            elif status == "full":
                item.setForeground(_COL_SCORE, QColor("#389e0d"))
            if bt == 1:
                item.setForeground(_COL_NAME, QColor("#c80"))
            for c_idx, key in zip(
                (_COL_D1, _COL_D2, _COL_D3, _COL_D4, _COL_D5, _COL_ALPHA),
                ("d1_avg", "d2_avg", "d3_avg", "d4_avg",
                 "d5_avg", "alpha_avg"),
            ):
                self._paint_pct_cell(item, c_idx, data.get(key))

            # placeholder 子项（让 ▶ 可见）
            placeholder = QTreeWidgetItem(["  (展开加载中...)"])
            placeholder.setForeground(0, QColor("#bfbfbf"))
            item.addChild(placeholder)

            self.report_tree.addTopLevelItem(item)
            # setItemWidget 必须在 addTopLevelItem 后调用
            self.report_tree.setItemWidget(
                item, _COL_TAIL,
                self._build_report_tail_widget(data, status, themes_n, fname),
            )

        self.report_tree.setSortingEnabled(True)

    def _build_report_tail_widget(
        self, data: Dict, status: str, themes_n: int, fname: str,
    ) -> QWidget:
        """报告父行尾列容器：[⚡ 打分][🗑 删除]　文件名 label。"""
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(4)
        h.addWidget(self._build_score_button(data, status, themes_n))
        h.addWidget(self._build_delete_button(data))
        lbl = QLabel(fname)
        lbl.setToolTip(data.get("file_path") or "")
        lbl.setStyleSheet("color: #595959;")
        h.addWidget(lbl, 1)
        return wrap

    # --- 懒加载触发：itemExpanded 信号 ---

    def _on_item_expanded(self, item: QTreeWidgetItem):
        payload = item.data(_COL_NAME, Qt.UserRole) or {}
        if payload.get("loaded"):
            return
        kind = payload.get("kind")
        if kind == _KIND_REPORT:
            self._lazy_load_themes(item, payload.get("data") or {})
        elif kind == _KIND_THEME:
            self._lazy_load_stocks(item, payload.get("data") or {})
        payload["loaded"] = True
        item.setData(_COL_NAME, Qt.UserRole, payload)

    def _lazy_load_themes(self, parent: QTreeWidgetItem, data: Dict) -> None:
        """点 📄 ▶ 时拉该报告下所有题材填子节点。"""
        report_id = int(data.get("report_id") or 0)
        # 清空 placeholder
        parent.takeChildren()
        if report_id <= 0:
            self._add_empty_child(parent, "（缺 report_id）")
            return

        try:
            if report_id in self._theme_cache:
                themes = self._theme_cache[report_id]
            else:
                from services.scoring.scoring_service import (
                    get_theme_eval_for_report,
                )
                themes = get_theme_eval_for_report(report_id)
                self._theme_cache[report_id] = themes
        except Exception as exc:  # noqa: BLE001
            self._add_empty_child(
                parent, f"题材加载失败：{exc}", color="#c00"
            )
            return

        if not themes:
            self._add_empty_child(parent, "（该报告下无题材）")
            return

        for t in themes:
            self._append_theme_child(parent, t)

    def _append_theme_child(
        self, parent: QTreeWidgetItem, t: Dict,
    ) -> None:
        theme_id = int(t.get("theme_id") or 0)
        scored_pairs = int(t.get("scored_pairs") or 0)
        stocks_n = int(t.get("stocks_count") or 0)
        rank = t.get("priority_rank")
        score = t.get("strength_score")
        sector_code = t.get("sector_ts_code")
        match_conf = t.get("sector_match_conf")

        # 2026-05-28 v2 角标：让主人一眼看出这条题材是否参与报告级加权
        # 以及板块匹配是否可靠
        prefix_badges: List[str] = ["🎯"]
        tooltip_lines: List[str] = []

        # ① 报告级聚合参与状态：strength_score 决定
        if score is None:
            tooltip_lines.append("⚠ 无 strength_score（不参与报告级加权）")
            prefix_badges.append("❓")
        elif score < 0:
            tooltip_lines.append(
                f"🐻 看空题材 strength={score}（不参与报告级加权）"
            )
            prefix_badges.append("🐻")
        elif score == 0:
            tooltip_lines.append("⚪ 中性 strength=0（不参与报告级加权）")
            prefix_badges.append("⚪")

        # ② 板块绑定状态：sector_ts_code + sector_match_conf
        if not sector_code:
            tooltip_lines.append("🚫 字典无板块（D+N 永远空，请补 dim_sector）")
            prefix_badges.append("🚫")
            sector_cell = "🚫 未绑板块"
        elif match_conf is not None and float(match_conf) < 0.5:
            tooltip_lines.append(
                f"⚠ 弱匹配 conf={match_conf:.2f}（D+N 仅供参考）"
            )
            prefix_badges.append("⚠")
            sector_cell = f"⚠ {sector_code} ({match_conf:.2f})"
        else:
            sector_cell = sector_code

        cells = [
            f"{''.join(prefix_badges)} {t.get('theme_name') or '—'}",
            str(t.get("strength_level") or "—"),
            sector_cell,
            str(rank) if rank is not None else "—",
            str(score) if score is not None else "—",
            _fmt_pct(t.get("d1")),
            _fmt_pct(t.get("d2")),
            _fmt_pct(t.get("d3")),
            _fmt_pct(t.get("d4")),
            _fmt_pct(t.get("d5")),
            _fmt_pct(t.get("alpha_avg")),
            _fmt_rate(t.get("hit_rate_avg")),
            (
                f"标的 {stocks_n} / 已打 {scored_pairs}"
                if stocks_n or scored_pairs
                else "（未打分）"
            ),
        ]
        item = _ReportTreeItem(cells)
        item.setData(_COL_NAME, Qt.UserRole, {
            "kind": _KIND_THEME,
            "loaded": False,
            "data": t,
        })
        # 角标 tooltip：题材名列 + 板块列都挂
        if tooltip_lines:
            tip = "\n".join(tooltip_lines)
            item.setToolTip(_COL_NAME, tip)
            item.setToolTip(_COL_AUX, tip)
        # 题材级染色
        for c_idx, key in zip(
            (_COL_D1, _COL_D2, _COL_D3, _COL_D4, _COL_D5, _COL_ALPHA),
            ("d1", "d2", "d3", "d4", "d5", "alpha_avg"),
        ):
            self._paint_pct_cell(item, c_idx, t.get(key))
        # 标的层 placeholder
        if stocks_n > 0:
            ph = QTreeWidgetItem(["  (展开加载标的...)"])
            ph.setForeground(0, QColor("#bfbfbf"))
            item.addChild(ph)
        parent.addChild(item)

    def _lazy_load_stocks(self, parent: QTreeWidgetItem, data: Dict) -> None:
        """点 🎯 ▶ 时拉该题材下所有标的填孙节点。"""
        theme_id = int(data.get("theme_id") or 0)
        parent.takeChildren()
        if theme_id <= 0:
            self._add_empty_child(parent, "（缺 theme_id）")
            return

        try:
            if theme_id in self._stock_cache:
                stocks = self._stock_cache[theme_id]
            else:
                from services.scoring.scoring_service import (
                    get_stock_scores_for_theme,
                )
                stocks = get_stock_scores_for_theme(theme_id)
                self._stock_cache[theme_id] = stocks
        except Exception as exc:  # noqa: BLE001
            self._add_empty_child(
                parent, f"标的加载失败：{exc}", color="#c00"
            )
            return

        if not stocks:
            self._add_empty_child(parent, "（该题材下未抽到标的）")
            return

        for s in stocks:
            self._append_stock_child(parent, s)

    def _append_stock_child(
        self, parent: QTreeWidgetItem, s: Dict,
    ) -> None:
        # 列 11（命中率/命中）改用"汇总命中率"（命中天数 / 已打分天数）
        scored_days = int(s.get("scored_days") or 0)
        hits = sum(
            1 for k in ("d1_hit", "d2_hit", "d3_hit", "d4_hit", "d5_hit")
            if (s.get(k) or 0) == 1
        )
        hit_text = (
            f"{hits}/{scored_days}" if scored_days else "—"
        )
        cells = [
            f"📈 {s.get('stock_name') or '—'}",
            str(s.get("role") or "—"),
            str(s.get("normalized_code") or "—"),
            "—",  # 排名列对标的不适用
            "—",  # 强度分对标的不适用
            _fmt_pct(s.get("d1_pct")),
            _fmt_pct(s.get("d2_pct")),
            _fmt_pct(s.get("d3_pct")),
            _fmt_pct(s.get("d4_pct")),
            _fmt_pct(s.get("d5_pct")),
            "—",
            hit_text,
            (s.get("reason") or "")[:120],
        ]
        item = _ReportTreeItem(cells)
        item.setData(_COL_NAME, Qt.UserRole, {
            "kind": _KIND_STOCK,
            "loaded": True,  # 叶子节点不再展开
            "data": s,
        })
        # 标的级 D+N 染色
        for c_idx, key in zip(
            (_COL_D1, _COL_D2, _COL_D3, _COL_D4, _COL_D5),
            ("d1_pct", "d2_pct", "d3_pct", "d4_pct", "d5_pct"),
        ):
            self._paint_pct_cell(item, c_idx, s.get(key))
        # 命中标记列底色
        if scored_days > 0:
            if hits == scored_days:
                item.setBackground(_COL_HIT, QColor("#fff1f0"))  # 全命中浅红
            elif hits == 0:
                item.setBackground(_COL_HIT, QColor("#f6ffed"))  # 全未命中浅绿
        parent.addChild(item)

    @staticmethod
    def _add_empty_child(
        parent: QTreeWidgetItem, text: str, color: str = "#8c8c8c",
    ) -> None:
        ph = QTreeWidgetItem([text])
        ph.setForeground(0, QColor(color))
        parent.addChild(ph)

    @staticmethod
    def _paint_pct_cell(
        item: QTreeWidgetItem, col: int, val,
    ) -> None:
        if val is None:
            item.setForeground(col, QColor("#bfbfbf"))
            return
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        if v > 0:
            item.setForeground(col, QColor("#c62828"))
        elif v < 0:
            item.setForeground(col, QColor("#2e7d32"))

    def _build_score_button(
        self, data: Dict, status: str, themes_n: int,
    ) -> QPushButton:
        """构造该行的「打分」按钮，根据 score_status 切换文字/样式。"""
        report_id = int(data.get("report_id") or 0)
        if themes_n == 0:
            btn = QPushButton("✗ 无题材")
            btn.setStyleSheet(_BTN_NONE)
            btn.setEnabled(False)
            btn.setToolTip("该报告未抽取到题材，无法打分")
            return btn
        if status == "none":
            btn = QPushButton("⚡ 初次打分")
            btn.setStyleSheet(_BTN_NONE)
        elif status == "partial":
            scored = int(data.get("scored_pairs") or 0)
            expected = int(data.get("expected_pairs") or 0)
            btn = QPushButton(f"🔄 续打 ({scored}/{expected})")
            btn.setStyleSheet(_BTN_PARTIAL)
            btn.setToolTip(
                "部分 (theme, score_date) 已打分，"
                "继续把剩余的补齐（不会重复写已有行）"
            )
        else:  # full
            btn = QPushButton("♻ 重新打分")
            btn.setStyleSheet(_BTN_FULL)
            btn.setToolTip(
                "全部 (theme, score_date) 已打过分，"
                "重新打会覆盖（UNIQUE 约束 + INSERT OR REPLACE）"
            )
        btn.clicked.connect(
            lambda _checked=False, rid=report_id: self._on_score_clicked(rid)
        )
        return btn

    def _build_delete_button(self, data: Dict) -> QPushButton:
        """构造该行的「删除」按钮。

        * ``is_backtest=1`` → 浅红「🗑 删除」，单次确认
        * ``is_backtest=0`` → 深红「⚠ 删真实」，**双重**确认
          （第二次要求输入 report_id 4 位防误删）
        """
        report_id = int(data.get("report_id") or 0)
        is_bt = int(data.get("is_backtest") or 0)
        if report_id <= 0:
            btn = QPushButton("✗")
            btn.setEnabled(False)
            btn.setToolTip("缺 report_id，无法定位")
            return btn
        if is_bt == 1:
            btn = QPushButton("🗑 删除")
            btn.setStyleSheet(_BTN_DEL_BT)
            btn.setToolTip(
                "删除该 backtest 报告\n"
                "—— 同步删 theme_predictions(+CASCADE 子表) + .md 文件"
            )
        else:
            btn = QPushButton("⚠ 删真实")
            btn.setStyleSheet(_BTN_DEL_REAL)
            btn.setToolTip(
                "⚠ 真实日常报告（is_backtest=0）\n"
                "—— 需双重确认 + 输入 report_id 末 4 位"
            )
        btn.clicked.connect(
            lambda _checked=False, rid=report_id, bt=is_bt,
            d=data: self._on_delete_clicked(rid, bt, d)
        )
        return btn

    @staticmethod
    def _fmt_backtest_marker(data: Dict) -> str:
        """模板汇总行没有逐报告 is_backtest 字段；显示 "—"（已被筛选条决定）。"""
        return "—"

    @staticmethod
    def _colorize_pct(item: QTableWidgetItem, data: Dict,
                      _col_idx: int, _kind: str) -> None:
        text = item.text()
        if text in ("—", "    -", "-"):
            return
        try:
            val = float(text.rstrip("%").rstrip().lstrip("+"))
        except ValueError:
            return
        if val > 0:
            item.setForeground(QColor("#c62828"))  # 红涨
        elif val < 0:
            item.setForeground(QColor("#2e7d32"))  # 绿跌

    # ------------------------------------------------------------------
    # 交互 handlers
    # ------------------------------------------------------------------

    def _on_template_selected(self):
        rows = self.tpl_table.selectionModel().selectedRows()
        if not rows:
            self._selected_prompt_id = None
        else:
            item = self.tpl_table.item(rows[0].row(), 0)
            self._selected_prompt_id = (
                item.data(Qt.UserRole) if item else None
            )
        self._reload_report_table()

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _col: int):
        """双击 📄 报告父行 → emit 跳「题材预测」页；子行无效。"""
        if item is None:
            return
        payload = item.data(_COL_NAME, Qt.UserRole) or {}
        if payload.get("kind") != _KIND_REPORT:
            return
        data = payload.get("data") or {}
        rd = data.get("report_date") or ""
        pid = data.get("prompt_id") or ""
        if not rd:
            return
        self.theme_drilldown_requested.emit(rd, pid)

    def _on_delete_clicked(
        self, report_id: int, is_backtest: int, data: Dict,
    ):
        """下表「删除」按钮回调。

        * ``is_backtest=1`` → 单次 Yes/No 弹窗
        * ``is_backtest=0`` → 双重确认：Yes/No + 文本框输入 report_id
          末 4 位（含整体 < 4 位时输入完整 id）
        """
        if self._delete_worker and self._delete_worker.isRunning():
            QMessageBox.information(
                self, "正在删除", "已有删除任务在跑，请稍候"
            )
            return
        if report_id <= 0:
            QMessageBox.warning(
                self, "参数错误", "report_id 异常，请刷新后重试"
            )
            return

        prompt_id = data.get("prompt_id") or "—"
        report_date = format_yyyymmdd_to_dash(
            data.get("report_date") or "—"
        )
        fp = data.get("file_path") or ""
        fname = Path(fp).name if fp else "-"
        themes_n = int(data.get("themes_count") or 0)
        scored_n = int(data.get("scored_pairs") or 0)
        bt_txt = "回测 (is_backtest=1)" if is_backtest else "⚠ 真实日常 (is_backtest=0)"

        body = (
            f"<b>即将删除报告 id={report_id}</b><br>"
            f"&nbsp;&nbsp;类型：{bt_txt}<br>"
            f"&nbsp;&nbsp;模板：{prompt_id}<br>"
            f"&nbsp;&nbsp;日期：{report_date}<br>"
            f"&nbsp;&nbsp;文件：{fname}<br>"
            f"&nbsp;&nbsp;题材：{themes_n} 个（含 {scored_n} 条打分）<br>"
            f"<br>会同步删 <b>theme_predictions(+CASCADE 子表) + .md 文件</b>。<br>"
            f"<span style='color:#c00'>此操作不可逆。</span>"
        )
        reply = QMessageBox.question(
            self, "确认删除", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # 真实日常报告：第二道关 —— 输入 id 末 4 位
        if not is_backtest:
            from PyQt5.QtWidgets import QInputDialog
            expected = str(report_id)[-4:] if report_id >= 1000 else str(report_id)
            text, ok = QInputDialog.getText(
                self,
                "⚠ 二次确认：删除真实日常报告",
                f"这是真实日常生成的报告（非回测），不应轻易删除。\n\n"
                f"请输入 report_id 末 4 位 <b>{expected}</b> 以确认：",
            )
            if not ok:
                return
            if text.strip() != expected:
                QMessageBox.warning(
                    self, "确认失败",
                    f"输入「{text.strip()}」与「{expected}」不一致，已取消删除"
                )
                return

        self._set_buttons_busy(True)
        self.status_label.setText(
            f"<span style='color:#c80'>🗑 删除中 report_id={report_id} ...</span>"
        )
        self._delete_worker = DeleteReportWorker(
            report_id=report_id,
            allow_real=not bool(is_backtest),
            delete_md=True,
            dry_run=False,
        )
        self._delete_worker.stage.connect(
            lambda msg: self.status_label.setText(
                f"<span style='color:#c80'>🗑 {msg}</span>"
            )
        )
        self._delete_worker.finished_result.connect(self._on_delete_done)
        self._delete_worker.error.connect(self._on_delete_error)
        self._delete_worker.start()

    def _on_delete_done(self, res: dict):
        self._set_buttons_busy(False)
        ok = bool(res.get("ok"))
        rid = res.get("report_id")
        if not ok:
            err = res.get("error") or "未知错误"
            self.status_label.setText(
                f"<span style='color:#c00'>❌ 删除失败 id={rid}: "
                f"{err.splitlines()[0]}</span>"
            )
            QMessageBox.warning(self, "删除失败", err)
            return
        self.status_label.setText(
            f"<span style='color:#0a0'>✅ 已删除 id={rid} | "
            f"ai_reports={res.get('ai_reports_deleted')} "
            f"theme_predictions={res.get('theme_predictions_deleted')} "
            f"md={'yes' if res.get('md_file_deleted') else 'no'}</span>"
        )
        self.refresh()

    def _on_delete_error(self, msg: str):
        self._set_buttons_busy(False)
        self.status_label.setText(
            f"<span style='color:#c00'>❌ 删除线程异常: "
            f"{msg.splitlines()[0]}</span>"
        )
        QMessageBox.critical(self, "删除线程异常", msg)

    def _on_score_clicked(self, report_id: int):
        """下表「打分」按钮回调：单报告精准打分。"""
        if self._rescore_worker and self._rescore_worker.isRunning():
            QMessageBox.information(
                self, "正在打分", "已有打分任务在跑，请稍候"
            )
            return
        if report_id <= 0:
            QMessageBox.warning(
                self, "参数错误", "report_id 异常，请刷新后重试"
            )
            return

        self._set_buttons_busy(True)
        self.status_label.setText(
            f"<span style='color:#06c'>⏳ 单报告打分 "
            f"report_id={report_id} ...</span>"
        )
        self._rescore_worker = RescoreWorker(
            report_id=report_id,
            days_back=5,
            hit_threshold_pct=3.0,
        )
        self._rescore_worker.stage.connect(
            lambda msg: self.status_label.setText(
                f"<span style='color:#06c'>⏳ {msg}</span>"
            )
        )
        self._rescore_worker.finished_result.connect(self._on_rescore_done)
        self._rescore_worker.error.connect(self._on_rescore_error)
        self._rescore_worker.start()

    def _on_rescore(self):
        if self._rescore_worker and self._rescore_worker.isRunning():
            QMessageBox.information(
                self, "正在打分", "已有打分任务在跑，请稍候"
            )
            return
        f = self._current_filters()
        start_dt = datetime.now() - timedelta(days=f["days"])
        rd_start = start_dt.strftime("%Y%m%d")
        rd_end = datetime.now().strftime("%Y%m%d")

        reply = QMessageBox.question(
            self,
            "确认重打分",
            f"将对 report_date ∈ [{rd_start}, {rd_end}] "
            f"范围内的全部题材（约 {self._estimate_theme_count()} 个）"
            f"重跑 D+1~D+5 脚本打分。\n\n"
            f"耗时约 5~30 秒（取决于样本量），期间面板会刷新进度。\n"
            f"不会重新调 AI 评分员（Phase 7 才有）。\n\n"
            f"确定继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._set_buttons_busy(True)
        self.status_label.setText(
            f"<span style='color:#06c'>⏳ 重打分中... "
            f"[{rd_start}, {rd_end}]</span>"
        )
        self._rescore_worker = RescoreWorker(
            rd_start, rd_end,
            days_back=5,
            hit_threshold_pct=3.0,
        )
        self._rescore_worker.stage.connect(
            lambda msg: self.status_label.setText(
                f"<span style='color:#06c'>⏳ {msg}</span>"
            )
        )
        self._rescore_worker.finished_result.connect(self._on_rescore_done)
        self._rescore_worker.error.connect(self._on_rescore_error)
        self._rescore_worker.start()

    def _set_buttons_busy(self, busy: bool):
        self.rescore_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.range_combo.setEnabled(not busy)
        self.backtest_combo.setEnabled(not busy)
        self.dim_report_radio.setEnabled(not busy)
        self.dim_score_radio.setEnabled(not busy)
        self.ignore_version_check.setEnabled(not busy)

    def _on_rescore_done(self, res: dict):
        self._set_buttons_busy(False)
        ok = res.get("ok")
        themes = res.get("themes_scored") or 0
        pairs = res.get("pairs_succeeded") or 0
        attempted = res.get("pairs_attempted") or 0
        days_cov = res.get("days_covered") or 0
        ms = res.get("elapsed_ms") or 0
        mode = res.get("mode") or "range"
        prefix = (
            f"✅ 单报告打分完成 (id={res.get('report_id')})"
            if mode == "one_report"
            else "✅ 区间打分完成"
        )
        color = "#0a0" if ok else "#c80"
        self.status_label.setText(
            f"<span style='color:{color}'>"
            f"{prefix}: 题材 {themes} / 对 {pairs}/{attempted} / "
            f"覆盖 {days_cov} 天 / 耗时 {ms} ms"
            f"</span>"
        )
        if res.get("errors"):
            QMessageBox.warning(
                self, "打分有部分失败",
                "前 5 条错误：\n" + "\n".join(res["errors"]),
            )
        self.refresh()

    def _on_rescore_error(self, msg: str):
        self._set_buttons_busy(False)
        self.status_label.setText(
            f"<span style='color:#c00'>❌ 打分异常: "
            f"{msg.splitlines()[0]}</span>"
        )
        QMessageBox.critical(self, "打分异常", msg)

    def _estimate_theme_count(self) -> int:
        """粗估：当前模板汇总表里所有模板的样本数之和。"""
        return sum(
            int(r.get("sample_count") or 0) for r in self._template_rows
        )

    # ------------------------------------------------------------------
    # 导出 CSV
    # ------------------------------------------------------------------

    def _on_export_csv(self):
        if not self._template_rows and not self._report_rows:
            QMessageBox.warning(self, "提示", "当前无数据可导出")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出模板评估 CSV",
            "prompt_eval.csv", "CSV 文件 (*.csv)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                # Section 1: 模板汇总
                writer.writerow(["[模板汇总]"])
                writer.writerow([c[0] for c in _TPL_COLS])
                for r in self._template_rows:
                    themes_total = (
                        r.get("themes_total") or r.get("sample_count") or 0
                    )
                    writer.writerow([
                        r.get("prompt_id") or "—",
                        r.get("prompt_version") or "—",
                        "—",
                        themes_total,
                        r.get("scored_themes") or 0,
                        r.get("d1_avg"), r.get("d2_avg"),
                        r.get("d3_avg"), r.get("d4_avg"),
                        r.get("d5_avg"),
                        r.get("alpha_avg"),
                        r.get("hit_rate_avg"),
                        r.get("direction_correct_rate"),
                        "",  # AI 高质量
                        format_yyyymmdd_to_dash(
                            r.get("last_report_date") or ""
                        ),
                    ])
                # 空行
                writer.writerow([])
                # Section 2: 报告级
                writer.writerow(["[报告级]"])
                writer.writerow([
                    "report_id", "report_date", "is_backtest",
                    "prompt_id", "prompt_version",
                    "themes_count", "scored_pairs", "expected_pairs",
                    "score_status",
                    "d1_avg", "d2_avg", "d3_avg", "d4_avg", "d5_avg",
                    "alpha_avg", "hit_rate_avg", "direction_correct_rate",
                    "file_path",
                ])
                for r in self._report_rows:
                    writer.writerow([
                        r.get("report_id"),
                        format_yyyymmdd_to_dash(r.get("report_date") or ""),
                        int(r.get("is_backtest") or 0),
                        r.get("prompt_id") or "",
                        r.get("prompt_version") or "",
                        r.get("themes_count") or 0,
                        r.get("scored_pairs") or 0,
                        r.get("expected_pairs") or 0,
                        r.get("score_status") or "none",
                        r.get("d1_avg"), r.get("d2_avg"),
                        r.get("d3_avg"), r.get("d4_avg"),
                        r.get("d5_avg"),
                        r.get("alpha_avg"),
                        r.get("hit_rate_avg"),
                        r.get("direction_correct_rate"),
                        r.get("file_path") or "",
                    ])

                # Section 3 + 4: 题材级 + 标的级（全量加载，含未展开的）
                writer.writerow([])
                writer.writerow(["[题材级]"])
                # 2026-05-28 v2：d1~d5 现在来自板块行情 sector_pct，
                # sector_match_conf 标记板块匹配置信度（< 0.5 算弱匹配）
                writer.writerow([
                    "report_id", "theme_id", "theme_name",
                    "strength_level", "strength_score",
                    "sector_ts_code", "sector_match_conf",
                    "stocks_count", "scored_pairs",
                    "d1", "d2", "d3", "d4", "d5",
                    "alpha_avg", "hit_rate_avg", "direction_correct_rate",
                    "sector_pct_avg",
                ])
                writer.writerow([])
                stock_rows_section = [["[标的级]"], [
                    "theme_id", "theme_stock_id", "stock_name",
                    "normalized_code", "role", "scored_days",
                    "d1_pct", "d2_pct", "d3_pct", "d4_pct", "d5_pct",
                    "d1_hit", "d2_hit", "d3_hit", "d4_hit", "d5_hit",
                ]]

                from services.scoring.scoring_service import (
                    get_stock_scores_for_theme,
                    get_theme_eval_for_report,
                )
                for r in self._report_rows:
                    rid = int(r.get("report_id") or 0)
                    if rid <= 0:
                        continue
                    themes = self._theme_cache.get(rid)
                    if themes is None:
                        themes = get_theme_eval_for_report(rid)
                        self._theme_cache[rid] = themes
                    for t in themes:
                        writer.writerow([
                            rid,
                            t.get("theme_id"),
                            t.get("theme_name") or "",
                            t.get("strength_level") or "",
                            t.get("strength_score"),
                            t.get("sector_ts_code") or "",
                            t.get("sector_match_conf"),
                            t.get("stocks_count") or 0,
                            t.get("scored_pairs") or 0,
                            t.get("d1"), t.get("d2"),
                            t.get("d3"), t.get("d4"),
                            t.get("d5"),
                            t.get("alpha_avg"),
                            t.get("hit_rate_avg"),
                            t.get("direction_correct_rate"),
                            t.get("sector_pct_avg"),
                        ])
                        # 标的级（拼到 stock_rows_section 末尾批量写）
                        tid = int(t.get("theme_id") or 0)
                        if tid <= 0 or not (t.get("stocks_count") or 0):
                            continue
                        stocks = self._stock_cache.get(tid)
                        if stocks is None:
                            stocks = get_stock_scores_for_theme(tid)
                            self._stock_cache[tid] = stocks
                        for s in stocks:
                            stock_rows_section.append([
                                tid,
                                s.get("theme_stock_id"),
                                s.get("stock_name") or "",
                                s.get("normalized_code") or "",
                                s.get("role") or "",
                                s.get("scored_days") or 0,
                                s.get("d1_pct"), s.get("d2_pct"),
                                s.get("d3_pct"), s.get("d4_pct"),
                                s.get("d5_pct"),
                                s.get("d1_hit"), s.get("d2_hit"),
                                s.get("d3_hit"), s.get("d4_hit"),
                                s.get("d5_hit"),
                            ])

                for row in stock_rows_section:
                    writer.writerow(row)
            QMessageBox.information(self, "成功", f"已导出: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))

    def showEvent(self, event):
        super().showEvent(event)
        # 切回本页时自动刷新一次（避免被其他页改了数据后失同步）
        self.refresh()


# ---------------------------------------------------------------------------
# 格式化辅助
# ---------------------------------------------------------------------------


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_rate(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"
