"""📊 盘后数据页面（Phase M3）。

入口位于主窗口侧边栏「📤 数据导出」与「🤖 AI分析」之间。

布局::

    ┌── 操作区 ──────────────────────────────┐
    │ 交易日 / 模式 / 强制重拉 / 顶部按钮         │
    │ 状态行（completeness / 耗时 / 警告）         │
    │ 进度日志框                                 │
    ├── QTabWidget ────────────────────────────┤
    │ 总览 / 板块 / 涨停 / 龙虎榜 / 异动 / MD / JSON │
    └───────────────────────────────────────────┘

依赖::

    services.market.service.MarketSummaryService        # build / get / list_dates
    services.market.market_db.get_market_db              # 备份 / 列日期
    services.market.trader_aliases.TraderAliasMatcher    # 在 dialog 内独立持有
    gui.workers.market_fetch_worker.MarketFetchWorker
    gui.pages.trader_alias_dialog.TraderAliasDialog
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, TABLE_STYLE,
)
from gui.workers.market_fetch_worker import MarketFetchWorker
from gui.pages.trader_alias_dialog import TraderAliasDialog

_log = logging.getLogger(__name__)

# 来源色（与 catalysts_source 字段对应）
_COLOR_CLS = QColor("#E3F2FD")     # 浅蓝
_COLOR_AI = QColor("#FFF9C4")      # 浅黄
_COLOR_NONE = QColor("#FAFAFA")    # 灰白
_COLOR_TIM_FAMOUS_BG = QColor("#F3E5F5")  # 紫粉（已识别游资行）
_COLOR_TIM_FAMOUS_FG = QColor("#6A1B9A")  # 深紫文字
_COLOR_RED = QColor("#D32F2F")
_COLOR_GREEN = QColor("#388E3C")
_COLOR_MUTED = QColor("#8C8C8C")
_TODAY_QSS = (
    "font-size:22px;font-weight:bold;color:#262626;"
)


def _pct_color(v: Optional[float]) -> Optional[QColor]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 0.01:
        return _COLOR_RED
    if f < -0.01:
        return _COLOR_GREEN
    return None


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,d}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_rate(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_yi(v: Optional[float]) -> str:
    """金额（亿元）。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f} 亿"
    except (TypeError, ValueError):
        return "—"


def _safe_get(d: Optional[Dict[str, Any]], *keys, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------------------
# MarketSummaryPage
# ---------------------------------------------------------------------------


class MarketSummaryPage(QWidget):
    """盘后数据主页面。"""

    def __init__(self) -> None:
        super().__init__()
        self.worker: Optional[MarketFetchWorker] = None
        self._summary: Optional[Dict[str, Any]] = None
        self._summary_md: Optional[str] = None
        self._current_trade_date: Optional[str] = None
        self._init_ui()
        self._refresh_date_combo()
        self._load_latest_if_any()

    # ------------------------------------------------------------------
    # 整体 UI
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        title = QLabel("📊 盘后数据")
        title.setStyleSheet(_TODAY_QSS)
        layout.addWidget(title)

        layout.addWidget(self._build_action_group())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tab_overview(), "📊 总览")
        self.tabs.addTab(self._build_tab_sectors(), "🏢 板块 Top 10")
        self.tabs.addTab(self._build_tab_ladder(), "🚀 涨停 & 连板")
        self.tabs.addTab(self._build_tab_dragon(), "🐉 龙虎榜")
        self.tabs.addTab(self._build_tab_shock(), "⚡ 异动时间线")
        self.tabs.addTab(self._build_tab_md(), "📝 Markdown")
        self.tabs.addTab(self._build_tab_json(), "🔍 原始 JSON")
        layout.addWidget(self.tabs, 1)

    # ------------------------------------------------------------------
    # 操作区
    # ------------------------------------------------------------------

    def _build_action_group(self) -> QGroupBox:
        group = QGroupBox("📋 操作")
        outer = QVBoxLayout(group)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)

        row1.addWidget(QLabel("交易日:"))
        self.date_combo = QComboBox()
        self.date_combo.setEditable(True)
        self.date_combo.setMinimumWidth(140)
        self.date_combo.setStyleSheet(COMBOBOX_STYLE)
        self.date_combo.lineEdit().setPlaceholderText(
            "YYYYMMDD 或留空 = 最近交易日"
        )
        self.date_combo.activated.connect(self._on_date_selected)
        row1.addWidget(self.date_combo)

        row1.addWidget(QLabel("模式:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("hybrid（推荐）", "hybrid")
        self.mode_combo.addItem("tushare-only", "tushare-only")
        self.mode_combo.addItem("ai-full（M3 等同 hybrid）", "ai-full")
        self.mode_combo.setStyleSheet(COMBOBOX_STYLE)
        row1.addWidget(self.mode_combo)

        self.force_check = QCheckBox("强制重拉")
        self.force_check.setToolTip(
            "勾选后无视缓存，重新调 Tushare + AI 兜底。"
            "未勾选：命中 market_summaries 表 → 直接读出。"
        )
        row1.addWidget(self.force_check)

        row1.addStretch()

        self.fetch_btn = QPushButton("🔄 获取/重新获取")
        self.fetch_btn.setStyleSheet(BUTTON_PRIMARY)
        self.fetch_btn.setMinimumHeight(36)
        self.fetch_btn.clicked.connect(self.start_fetch)
        row1.addWidget(self.fetch_btn)

        self.stop_btn = QPushButton("⏹ 终止")
        self.stop_btn.setStyleSheet(BUTTON_DANGER)
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_fetch)
        row1.addWidget(self.stop_btn)

        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.copy_btn = QPushButton("📋 复制 Markdown")
        self.copy_btn.setStyleSheet(BUTTON_SUCCESS)
        self.copy_btn.clicked.connect(self.copy_markdown)
        row2.addWidget(self.copy_btn)

        self.send_btn = QPushButton("➡️ 发送到 AI 分析")
        self.send_btn.setStyleSheet(BUTTON_PRIMARY)
        self.send_btn.clicked.connect(self.send_to_analysis)
        row2.addWidget(self.send_btn)

        self.alias_btn = QPushButton("👥 编辑游资别名")
        self.alias_btn.clicked.connect(self.edit_trader_aliases)
        row2.addWidget(self.alias_btn)

        self.backup_btn = QPushButton("💾 备份数据库")
        self.backup_btn.clicked.connect(self.backup_market_db)
        row2.addWidget(self.backup_btn)

        row2.addStretch()

        self.status_label = QLabel("状态: 待命")
        self.status_label.setStyleSheet("color:#595959")
        row2.addWidget(self.status_label)

        outer.addLayout(row2)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet(
            "color:#FA8C16;background:#FFF7E6;"
            "border:1px solid #FFD591;border-radius:4px;"
            "padding:4px 8px;"
        )
        self.warning_label.setVisible(False)
        self.warning_label.setWordWrap(True)
        outer.addWidget(self.warning_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(80)
        self.log_box.setStyleSheet(
            "QPlainTextEdit{background:#FAFAFA;color:#595959;"
            "font-size:12px;border:1px solid #F0F0F0;}"
        )
        self.log_box.setPlaceholderText(
            "进度日志会显示在这里…"
        )
        outer.addWidget(self.log_box)

        return group

    # ------------------------------------------------------------------
    # Tab 1: 总览
    # ------------------------------------------------------------------

    def _build_tab_overview(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        # 指数卡片组
        idx_group = QGroupBox("大盘指数")
        idx_layout = QGridLayout(idx_group)
        idx_layout.setHorizontalSpacing(10)
        idx_layout.setVerticalSpacing(8)
        self._index_cards: Dict[str, Dict[str, QLabel]] = {}
        labels = [
            ("sh", "上证综指"),
            ("sz", "深证成指"),
            ("cyb", "创业板指"),
            ("sci", "科创 50"),
            ("zz500", "中证 500"),
            ("cybs", "创业板综"),
        ]
        for i, (key, label) in enumerate(labels):
            card = self._make_index_card(label)
            self._index_cards[key] = card
            idx_layout.addWidget(card["widget"], i // 3, i % 3)
        outer.addWidget(idx_group)

        # 量能 + 北向 + 情绪
        mid_row = QHBoxLayout()
        mid_row.setSpacing(12)

        flow_group = QGroupBox("成交 / 北向")
        flow_layout = QGridLayout(flow_group)
        self.turnover_label = self._make_kpi_label("—")
        self.prev_turnover_label = self._make_kpi_label("—")
        self.north_label = self._make_kpi_label("—")
        self.south_label = self._make_kpi_label("—")
        flow_layout.addWidget(QLabel("两市成交:"), 0, 0)
        flow_layout.addWidget(self.turnover_label, 0, 1)
        flow_layout.addWidget(QLabel("昨日成交:"), 1, 0)
        flow_layout.addWidget(self.prev_turnover_label, 1, 1)
        flow_layout.addWidget(QLabel("北向净买:"), 2, 0)
        flow_layout.addWidget(self.north_label, 2, 1)
        flow_layout.addWidget(QLabel("南向净买:"), 3, 0)
        flow_layout.addWidget(self.south_label, 3, 1)
        mid_row.addWidget(flow_group, 1)

        breadth_group = QGroupBox("涨跌停 / 情绪")
        breadth_layout = QGridLayout(breadth_group)
        self.up_label = self._make_kpi_label("—", color=_COLOR_RED)
        self.down_label = self._make_kpi_label("—", color=_COLOR_GREEN)
        self.fail_label = self._make_kpi_label("—")
        self.seal_label = self._make_kpi_label("—")
        self.promo_label = self._make_kpi_label("—")
        self.height_label = self._make_kpi_label("—")
        breadth_layout.addWidget(QLabel("涨停:"), 0, 0)
        breadth_layout.addWidget(self.up_label, 0, 1)
        breadth_layout.addWidget(QLabel("跌停:"), 0, 2)
        breadth_layout.addWidget(self.down_label, 0, 3)
        breadth_layout.addWidget(QLabel("炸板:"), 1, 0)
        breadth_layout.addWidget(self.fail_label, 1, 1)
        breadth_layout.addWidget(QLabel("封板率:"), 1, 2)
        breadth_layout.addWidget(self.seal_label, 1, 3)
        breadth_layout.addWidget(QLabel("晋级率:"), 2, 0)
        breadth_layout.addWidget(self.promo_label, 2, 1)
        breadth_layout.addWidget(QLabel("最高板:"), 2, 2)
        breadth_layout.addWidget(self.height_label, 2, 3)
        mid_row.addWidget(breadth_group, 1)

        outer.addLayout(mid_row)

        # 元信息
        meta_group = QGroupBox("元信息")
        meta_layout = QGridLayout(meta_group)
        self.meta_mode = QLabel("—")
        self.meta_completeness = QLabel("—")
        self.meta_generated_at = QLabel("—")
        self.meta_api_calls = QLabel("—")
        self.meta_elapsed = QLabel("—")
        self.meta_merge_stats = QLabel("—")
        meta_layout.addWidget(QLabel("模式:"), 0, 0)
        meta_layout.addWidget(self.meta_mode, 0, 1)
        meta_layout.addWidget(QLabel("完整度:"), 0, 2)
        meta_layout.addWidget(self.meta_completeness, 0, 3)
        meta_layout.addWidget(QLabel("生成时间:"), 1, 0)
        meta_layout.addWidget(self.meta_generated_at, 1, 1)
        meta_layout.addWidget(QLabel("API 调用:"), 1, 2)
        meta_layout.addWidget(self.meta_api_calls, 1, 3)
        meta_layout.addWidget(QLabel("耗时:"), 2, 0)
        meta_layout.addWidget(self.meta_elapsed, 2, 1)
        meta_layout.addWidget(QLabel("合并统计:"), 2, 2)
        meta_layout.addWidget(self.meta_merge_stats, 2, 3, 1, 2)
        outer.addWidget(meta_group)

        outer.addStretch()
        return wrap

    def _make_index_card(self, title: str) -> Dict[str, Any]:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setStyleSheet(
            "QFrame{background:white;border:1px solid #F0F0F0;"
            "border-radius:6px;}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet("color:#8C8C8C;font-size:12px;")
        close_label = QLabel("—")
        close_label.setStyleSheet(
            "font-size:18px;font-weight:bold;color:#262626;"
        )
        pct_label = QLabel("—")
        pct_label.setStyleSheet("font-size:13px;")
        amount_label = QLabel("—")
        amount_label.setStyleSheet("color:#8C8C8C;font-size:11px;")
        lay.addWidget(title_label)
        lay.addWidget(close_label)
        lay.addWidget(pct_label)
        lay.addWidget(amount_label)
        return {
            "widget": card,
            "title": title_label,
            "close": close_label,
            "pct": pct_label,
            "amount": amount_label,
        }

    def _make_kpi_label(
        self, text: str, *, color: Optional[QColor] = None
    ) -> QLabel:
        lab = QLabel(text)
        font = lab.font()
        font.setPointSize(11)
        font.setBold(True)
        lab.setFont(font)
        if color is not None:
            lab.setStyleSheet(f"color: {color.name()};")
        return lab

    # ------------------------------------------------------------------
    # Tab 2: 板块
    # ------------------------------------------------------------------

    _SECTOR_COLS = [
        ("#", 32),
        ("板块", 140),
        ("涨跌幅", 76),
        ("5日涨幅", 80),
        ("涨停数", 60),
        ("主力净流入(亿)", 110),
        ("超大单(亿)", 90),
        ("大单(亿)", 90),
        ("催化（cls/ai）", 420),
    ]

    def _build_tab_sectors(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)

        legend = QHBoxLayout()
        legend.setSpacing(12)
        legend.addWidget(QLabel("催化来源:"))
        legend.addWidget(self._color_legend(_COLOR_CLS, "CLS"))
        legend.addWidget(self._color_legend(_COLOR_AI, "AI 兜底"))
        legend.addWidget(self._color_legend(_COLOR_NONE, "未命中"))
        legend.addStretch()
        layout.addLayout(legend)

        self.sector_table = QTableWidget()
        self.sector_table.setColumnCount(len(self._SECTOR_COLS))
        self.sector_table.setHorizontalHeaderLabels(
            [c[0] for c in self._SECTOR_COLS]
        )
        for i, (_, w) in enumerate(self._SECTOR_COLS):
            self.sector_table.setColumnWidth(i, w)
        header = self.sector_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(
                len(self._SECTOR_COLS) - 1, QHeaderView.Stretch
            )
        self.sector_table.verticalHeader().setVisible(False)
        self.sector_table.setAlternatingRowColors(True)
        self.sector_table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )
        self.sector_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.sector_table.setStyleSheet(TABLE_STYLE)
        layout.addWidget(self.sector_table, 1)
        return wrap

    def _color_legend(self, color: QColor, text: str) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        sample = QLabel()
        sample.setFixedSize(16, 16)
        sample.setStyleSheet(
            f"background:{color.name()};border:1px solid #CCC;"
            "border-radius:2px;"
        )
        h.addWidget(sample)
        h.addWidget(QLabel(text))
        return wrap

    # ------------------------------------------------------------------
    # Tab 3: 涨停 & 连板
    # ------------------------------------------------------------------

    def _build_tab_ladder(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        info = QLabel(
            "<span style='color:#8C8C8C'>"
            "首板按 cons_nums=1（含未识别）；点行可展开/折叠"
            "</span>"
        )
        layout.addWidget(info)

        self.ladder_tree = QTreeWidget()
        self.ladder_tree.setHeaderLabels([
            "股票 / 桶", "题材", "涨停时间", "炸板次数",
        ])
        self.ladder_tree.setColumnWidth(0, 260)
        self.ladder_tree.setColumnWidth(1, 320)
        self.ladder_tree.setColumnWidth(2, 100)
        self.ladder_tree.setAlternatingRowColors(True)
        layout.addWidget(self.ladder_tree, 1)
        return wrap

    # ------------------------------------------------------------------
    # Tab 4: 龙虎榜
    # ------------------------------------------------------------------

    _DT_STOCK_COLS = [
        ("代码", 110),
        ("名称", 100),
        ("净买入(亿)", 100),
        ("上榜原因", 460),
    ]
    _TRADER_COLS = [
        ("游资别名", 130),
        ("操作", 60),
        ("关联股票", 110),
        ("净买入(亿)", 100),
        ("营业部全名", 460),
    ]

    def _build_tab_dragon(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        split = QSplitter(Qt.Vertical)

        # 上：龙虎榜个股
        stocks_group = QGroupBox("📋 龙虎榜个股")
        sl = QVBoxLayout(stocks_group)
        self.dt_stock_table = QTableWidget()
        self.dt_stock_table.setColumnCount(len(self._DT_STOCK_COLS))
        self.dt_stock_table.setHorizontalHeaderLabels(
            [c[0] for c in self._DT_STOCK_COLS]
        )
        for i, (_, w) in enumerate(self._DT_STOCK_COLS):
            self.dt_stock_table.setColumnWidth(i, w)
        header = self.dt_stock_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(
                len(self._DT_STOCK_COLS) - 1, QHeaderView.Stretch
            )
        self.dt_stock_table.verticalHeader().setVisible(False)
        self.dt_stock_table.setAlternatingRowColors(True)
        self.dt_stock_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.dt_stock_table.setStyleSheet(TABLE_STYLE)
        sl.addWidget(self.dt_stock_table)
        split.addWidget(stocks_group)

        # 下：已识别游资席位
        traders_group = QGroupBox("⭐ 已识别游资席位")
        tl = QVBoxLayout(traders_group)
        self.trader_table = QTableWidget()
        self.trader_table.setColumnCount(len(self._TRADER_COLS))
        self.trader_table.setHorizontalHeaderLabels(
            [c[0] for c in self._TRADER_COLS]
        )
        for i, (_, w) in enumerate(self._TRADER_COLS):
            self.trader_table.setColumnWidth(i, w)
        header = self.trader_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(
                len(self._TRADER_COLS) - 1, QHeaderView.Stretch
            )
        self.trader_table.verticalHeader().setVisible(False)
        self.trader_table.setAlternatingRowColors(True)
        self.trader_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.trader_table.setStyleSheet(TABLE_STYLE)
        tl.addWidget(self.trader_table)
        split.addWidget(traders_group)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        return wrap

    # ------------------------------------------------------------------
    # Tab 5: 异动时间线
    # ------------------------------------------------------------------

    def _build_tab_shock(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        info = QLabel(
            "<span style='color:#8C8C8C'>"
            "数据源: fact_cls_market_shock — "
            "按 first_shock_time 升序，同板块同方向多次合并"
            "</span>"
        )
        layout.addWidget(info)
        self.shock_list = QListWidget()
        self.shock_list.setStyleSheet(
            "QListWidget{background:white;}"
            "QListWidget::item{padding:6px 8px;"
            "border-bottom:1px solid #F0F0F0;}"
        )
        layout.addWidget(self.shock_list, 1)
        return wrap

    # ------------------------------------------------------------------
    # Tab 6/7: Markdown / JSON
    # ------------------------------------------------------------------

    def _build_tab_md(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)
        self.md_browser = QTextBrowser()
        self.md_browser.setOpenExternalLinks(True)
        font = QFont("Microsoft YaHei UI")
        font.setPointSize(10)
        self.md_browser.setFont(font)
        layout.addWidget(self.md_browser, 1)
        return wrap

    def _build_tab_json(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)
        self.json_browser = QPlainTextEdit()
        self.json_browser.setReadOnly(True)
        font = QFont("Consolas")
        font.setPointSize(10)
        self.json_browser.setFont(font)
        self.json_browser.setStyleSheet(
            "QPlainTextEdit{background:#FAFAFA;}"
        )
        layout.addWidget(self.json_browser, 1)
        return wrap

    # ==================================================================
    # 数据填充
    # ==================================================================

    def _populate_all(self) -> None:
        if not self._summary:
            return
        self._populate_overview(self._summary)
        self._populate_sectors(self._summary)
        self._populate_ladder(self._summary)
        self._populate_dragon_tiger(self._summary)
        self._populate_shock(self._summary)
        self._populate_md(self._summary_md)
        self._populate_json(self._summary)

    def _populate_overview(self, summary: Dict[str, Any]) -> None:
        indices = summary.get("indices") or {}
        for key, card in self._index_cards.items():
            info = indices.get(key) or {}
            close = info.get("close")
            pct = info.get("pct_chg")
            amount = info.get("amount_yi")
            card["close"].setText(
                _fmt_num(close, 2) if close is not None else "—"
            )
            card["pct"].setText(_fmt_pct(pct))
            color = _pct_color(pct)
            if color is not None:
                card["pct"].setStyleSheet(
                    f"font-size:13px;font-weight:bold;color:{color.name()}"
                )
            else:
                card["pct"].setStyleSheet("font-size:13px;color:#595959")
            card["amount"].setText(
                f"成交 {_fmt_num(amount, 0)} 亿"
                if amount is not None else "成交 —"
            )

        turnover = indices.get("total_turnover_yi")
        prev_turnover = indices.get("prev_turnover_yi")
        self.turnover_label.setText(_fmt_yi(turnover))
        self.prev_turnover_label.setText(_fmt_yi(prev_turnover))

        cap = summary.get("capital_flow") or {}
        north = cap.get("north_net_yi")
        north_delay = cap.get("north_is_delayed")
        north_text = _fmt_yi(north)
        if north_delay:
            north_text += f"  (T-1 {cap.get('north_data_date') or ''})"
        self.north_label.setText(north_text)
        if north is not None:
            self.north_label.setStyleSheet(
                "color:" + (
                    _COLOR_RED.name() if north > 0 else
                    _COLOR_GREEN.name() if north < 0 else "#262626"
                )
            )
        self.south_label.setText(_fmt_yi(cap.get("south_net_yi")))

        breadth = summary.get("breadth") or {}
        sentiment = summary.get("sentiment") or {}
        self.up_label.setText(_fmt_int(breadth.get("limit_up")))
        self.down_label.setText(_fmt_int(breadth.get("limit_down")))
        self.fail_label.setText(_fmt_int(breadth.get("failed_limit")))
        self.seal_label.setText(_fmt_rate(sentiment.get("seal_rate")))
        self.promo_label.setText(
            _fmt_rate(sentiment.get("promotion_rate"))
        )
        height_val = sentiment.get("max_height")
        max_stock = sentiment.get("max_stock") or ""
        if height_val:
            self.height_label.setText(
                f"{height_val} 板 · {max_stock}"
                if max_stock else f"{height_val} 板"
            )
        else:
            self.height_label.setText("—")

        meta = summary.get("meta") or {}
        quality = summary.get("quality") or {}
        self.meta_mode.setText(str(meta.get("mode") or "—"))
        comp = quality.get("completeness_score") or 0
        self.meta_completeness.setText(f"{float(comp) * 100:.1f}%")
        self.meta_generated_at.setText(
            str(meta.get("generated_at") or "—")
        )
        self.meta_api_calls.setText(str(meta.get("api_call_count") or "—"))
        elapsed_ms = meta.get("elapsed_ms")
        self.meta_elapsed.setText(
            f"{int(elapsed_ms) / 1000:.1f} 秒"
            if elapsed_ms else "—"
        )
        ms = meta.get("merge_stats") or {}
        if ms:
            self.meta_merge_stats.setText(
                f"CLS {ms.get('catalysts_from_cls', 0)} / "
                f"AI {ms.get('catalysts_from_ai', 0)} / "
                f"未匹配 {len(ms.get('catalysts_unfilled') or [])} / "
                f"异动 {ms.get('market_shock_events', 0)}"
            )
        else:
            self.meta_merge_stats.setText("—")

    def _populate_sectors(self, summary: Dict[str, Any]) -> None:
        sectors = summary.get("sectors_top") or []
        self.sector_table.setRowCount(len(sectors))
        for row, s in enumerate(sectors):
            pct = s.get("pct_chg")
            pct_5d = s.get("pct_chg_5d")
            cats = s.get("catalysts") or []
            src = (s.get("catalysts_source") or "none").lower()
            match = s.get("catalysts_match") or ""

            cells = [
                str(s.get("rank") or row + 1),
                str(s.get("name") or s.get("ts_code") or ""),
                _fmt_pct(pct),
                _fmt_pct(pct_5d),
                _fmt_int(s.get("limit_up_count")),
                _fmt_num(s.get("main_net_yi")),
                _fmt_num(s.get("main_elg_yi")),
                _fmt_num(s.get("main_lg_yi")),
                "  ·  ".join(cats) if cats else "—",
            ]

            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                # 涨跌幅着色
                if col == 2:
                    c = _pct_color(pct)
                    if c is not None:
                        item.setForeground(c)
                        font = QFont()
                        font.setBold(True)
                        item.setFont(font)
                elif col == 3:
                    c = _pct_color(pct_5d)
                    if c is not None:
                        item.setForeground(c)
                elif col == 5:
                    val = s.get("main_net_yi")
                    if val is not None:
                        try:
                            f = float(val)
                            item.setForeground(
                                _COLOR_RED if f > 0 else _COLOR_GREEN
                                if f < 0 else _COLOR_MUTED
                            )
                        except (TypeError, ValueError):
                            pass
                # catalysts 列上色
                if col == len(cells) - 1:
                    if src == "cls":
                        item.setBackground(QBrush(_COLOR_CLS))
                        item.setToolTip(
                            f"来源: CLS（匹配方式: {match or 'exact'}）"
                        )
                    elif src == "ai":
                        item.setBackground(QBrush(_COLOR_AI))
                        item.setToolTip("来源: AI 兜底")
                    else:
                        item.setBackground(QBrush(_COLOR_NONE))
                        item.setForeground(_COLOR_MUTED)
                        item.setToolTip("未命中：raw_news 中也找不到证据")
                self.sector_table.setItem(row, col, item)

    def _populate_ladder(self, summary: Dict[str, Any]) -> None:
        ladder = summary.get("limit_ladder") or {}
        self.ladder_tree.clear()
        if not isinstance(ladder, dict):
            return
        order = sorted(
            ladder.keys(),
            key=lambda k: (
                0 if "及以上" in k else
                1 if k.startswith("3") else
                2 if k.startswith("2") else 3
            ),
        )
        for bucket_name in order:
            items = ladder.get(bucket_name) or []
            if not isinstance(items, list):
                continue
            parent = QTreeWidgetItem([
                f"📈 {bucket_name} ({len(items)})",
                "",
                "",
                "",
            ])
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            for it in items:
                name = it.get("name") or ""
                code = it.get("code") or ""
                theme = it.get("theme") or ""
                lu_time = it.get("lu_time") or ""
                open_times = it.get("open_times")
                child = QTreeWidgetItem([
                    f"{name} ({code})" if code else name,
                    theme,
                    str(lu_time)[-8:] if lu_time else "",
                    str(open_times) if open_times is not None else "",
                ])
                parent.addChild(child)
            self.ladder_tree.addTopLevelItem(parent)
            # 首板默认折叠
            parent.setExpanded(not bucket_name.startswith("首板"))

    def _populate_dragon_tiger(self, summary: Dict[str, Any]) -> None:
        dt = summary.get("dragon_tiger") or {}
        stocks = dt.get("stocks") or []
        famous = dt.get("famous_traders") or []

        self.dt_stock_table.setRowCount(len(stocks))
        for row, s in enumerate(stocks):
            net = s.get("net_amount_yi")
            reasons = s.get("reasons") or []
            cells = [
                str(s.get("ts_code") or ""),
                str(s.get("name") or "—"),
                _fmt_num(net),
                "  ·  ".join(reasons) if reasons else "—",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 2 and net is not None:
                    try:
                        f = float(net)
                        item.setForeground(
                            _COLOR_RED if f > 0 else _COLOR_GREEN
                            if f < 0 else _COLOR_MUTED
                        )
                    except (TypeError, ValueError):
                        pass
                self.dt_stock_table.setItem(row, col, item)

        self.trader_table.setRowCount(len(famous))
        for row, t in enumerate(famous):
            side = str(t.get("side") or "")
            side_label = (
                "买入"
                if side.lower() in ("buy", "b") else
                "卖出"
                if side.lower() in ("sell", "s") else side
            )
            net = t.get("net_buy_yi")
            cells = [
                str(t.get("alias") or ""),
                side_label,
                str(t.get("stock") or ""),
                _fmt_num(net),
                str(t.get("exalter_raw") or ""),
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                # 整行紫粉背景
                item.setBackground(QBrush(_COLOR_TIM_FAMOUS_BG))
                item.setForeground(_COLOR_TIM_FAMOUS_FG)
                if col == 0:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.trader_table.setItem(row, col, item)

    def _populate_shock(self, summary: Dict[str, Any]) -> None:
        events = summary.get("market_shock") or []
        self.shock_list.clear()
        if not events:
            self.shock_list.addItem("（无异动事件）")
            return
        for e in events:
            t = str(e.get("first_shock_time") or "")
            status = str(e.get("status") or "")
            count = int(e.get("shock_count") or 0)
            sector = str(e.get("sector") or "")
            icon = "⬆️" if status == "up" else "⬇️"
            text = f"{icon}  {t}   {sector}   ×{count}"
            item = QListWidgetItem(text)
            if status == "up":
                item.setForeground(_COLOR_RED)
            else:
                item.setForeground(_COLOR_GREEN)
            self.shock_list.addItem(item)

    def _populate_md(self, md: Optional[str]) -> None:
        if not md:
            self.md_browser.setMarkdown("（暂无 Markdown 输出）")
            return
        self.md_browser.setMarkdown(md)

    def _populate_json(self, summary: Dict[str, Any]) -> None:
        text = json.dumps(
            summary, ensure_ascii=False, indent=2, sort_keys=True
        )
        self.json_browser.setPlainText(text)

    # ==================================================================
    # 用户操作
    # ==================================================================

    def start_fetch(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(
                self, "正在运行", "已有抓取任务进行中，请先等待或终止。"
            )
            return

        trade_date = (
            self.date_combo.currentText().strip() or None
        )
        # 把『最近交易日』占位提示转为 None
        if trade_date and not trade_date.isdigit():
            QMessageBox.warning(
                self, "格式错误",
                "交易日请填 YYYYMMDD（如 20260525），"
                "或留空 = 最近交易日"
            )
            return

        mode = self.mode_combo.currentData() or "hybrid"
        force = self.force_check.isChecked()

        self.log_box.clear()
        self.warning_label.setVisible(False)
        self._set_running(True)
        self._set_status(f"启动 {mode} 模式…")

        self.worker = MarketFetchWorker(
            trade_date=trade_date,
            mode=str(mode),
            force_refresh=force,
        )
        self.worker.progress.connect(self._on_worker_progress)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.error.connect(self._on_worker_error)
        self.worker.cancelled.connect(self._on_worker_cancelled)
        self.worker.start()

    def stop_fetch(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_cancel()
            self._set_status("正在终止…")

    def copy_markdown(self) -> None:
        if not self._summary_md:
            QMessageBox.information(
                self, "提示", "尚未获取盘后数据。"
            )
            return
        clip = QApplication.clipboard()
        if clip is not None:
            clip.setText(self._summary_md)
        self._set_status("📋 已复制 Markdown 到剪贴板")

    def send_to_analysis(self) -> None:
        if not self._summary_md:
            QMessageBox.information(
                self, "提示", "尚未获取盘后数据。"
            )
            return
        main_window = self.window()
        ai_page = None
        if main_window is not None and hasattr(main_window, "pages"):
            ai_page = main_window.pages.get("ai_analysis")
        if ai_page is None or not hasattr(ai_page, "summary_input"):
            QMessageBox.warning(
                self, "失败", "找不到 AI 分析页面，主窗口未注册？"
            )
            return
        ai_page.summary_input.setPlainText(self._summary_md)
        if hasattr(main_window, "show_page"):
            main_window.show_page("ai_analysis")
        QMessageBox.information(
            self, "已发送",
            f"已将 {self._current_trade_date or '该日'} "
            "盘后数据填入 AI 分析输入框。"
        )

    def edit_trader_aliases(self) -> None:
        dlg = TraderAliasDialog(self)
        dlg.exec_()
        # 关闭后无论是否保存都尝试重读 dragon_tiger（如果有当前 summary）
        if dlg.dirty and self._current_trade_date:
            self._set_status(
                "游资别名已更新，下次「强制重拉」时生效。"
            )

    def backup_market_db(self) -> None:
        try:
            from services.market.market_db import get_market_db

            db = get_market_db()
        except Exception as e:
            QMessageBox.critical(
                self, "失败", f"加载 MarketDB 失败: {e}"
            )
            return
        default_dir = Path("data/backups")
        default_dir.mkdir(parents=True, exist_ok=True)
        suggested = default_dir / (
            "market.db.bak."
            + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择备份保存位置",
            str(suggested),
            "SQLite 备份 (*.bak *.db);;所有文件 (*)",
        )
        if not path:
            return
        try:
            target = db.backup_to(Path(path))
        except Exception as e:
            QMessageBox.critical(
                self, "失败", f"备份失败: {e}"
            )
            return
        QMessageBox.information(
            self, "已备份",
            f"market.db 已备份到:\n{target}",
        )

    # ==================================================================
    # Worker 回调
    # ==================================================================

    def _on_worker_progress(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)
        self._set_status(msg)

    def _on_worker_finished(self, payload: Dict[str, Any]) -> None:
        self._set_running(False)
        td = payload.get("trade_date")
        completeness = payload.get("completeness") or 0
        elapsed = (payload.get("elapsed_ms") or 0) / 1000.0
        api_calls = payload.get("api_call_count") or 0
        from_cache = payload.get("from_cache")
        warnings = payload.get("warnings") or []

        self._summary = payload.get("summary_json")
        self._summary_md = payload.get("summary_md")
        self._current_trade_date = td

        status = (
            f"✅ {td} 完整度 {completeness * 100:.1f}% · "
            f"耗时 {elapsed:.1f}s · API {api_calls} 次"
        )
        if from_cache:
            status += " · 来自缓存"
        self._set_status(status)

        if warnings:
            self.warning_label.setText(
                "⚠️ " + " ; ".join(str(w) for w in warnings)
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        self._populate_all()
        self._refresh_date_combo(prefer=td)

    def _on_worker_error(self, msg: str) -> None:
        self._set_running(False)
        self._set_status(f"❌ 失败: {msg.splitlines()[0]}")
        self.log_box.appendPlainText(msg)
        QMessageBox.critical(self, "盘后数据获取失败", msg)

    def _on_worker_cancelled(self) -> None:
        self._set_running(False)
        self._set_status("⏹ 已终止")

    def _set_running(self, running: bool) -> None:
        self.fetch_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.date_combo.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.force_check.setEnabled(not running)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(f"状态: {text}")

    # ==================================================================
    # 日期下拉 / 缓存读取
    # ==================================================================

    def _refresh_date_combo(self, prefer: Optional[str] = None) -> None:
        try:
            from services.market.service import MarketSummaryService

            dates = MarketSummaryService().list_dates()
        except Exception as e:
            _log.warning("list_dates 失败: %s", e)
            dates = []
        current_edit = self.date_combo.currentText().strip()
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        self.date_combo.addItem("（最近交易日）", "")
        for d in dates:
            self.date_combo.addItem(d, d)
        # 选回 prefer / 之前输入
        target = prefer or current_edit
        if target:
            idx = self.date_combo.findText(target)
            if idx >= 0:
                self.date_combo.setCurrentIndex(idx)
            else:
                self.date_combo.setEditText(target)
        self.date_combo.blockSignals(False)

    def _on_date_selected(self, index: int) -> None:
        td = self.date_combo.itemData(index) or ""
        if not td:
            return  # 「最近交易日」占位项
        self._load_from_cache(str(td))

    def _load_latest_if_any(self) -> None:
        """页面首次打开时，尝试加载最近一条缓存。"""
        try:
            from services.market.service import MarketSummaryService

            svc = MarketSummaryService()
            dates = svc.list_dates()
            if dates:
                self._load_from_cache(dates[0])
        except Exception as e:
            _log.info("启动时未自动加载缓存: %s", e)

    def _load_from_cache(self, trade_date: str) -> None:
        try:
            from services.market.service import MarketSummaryService

            cached = MarketSummaryService().get(trade_date)
        except Exception as e:
            QMessageBox.warning(
                self, "读取失败", f"{trade_date}: {e}"
            )
            return
        if not cached.ok:
            self._set_status(
                f"⚠ 缓存中无 {trade_date}（{cached.error}）"
            )
            return
        self._summary = cached.summary_json
        self._summary_md = cached.summary_md
        self._current_trade_date = cached.trade_date

        self._set_status(
            f"📥 从缓存加载 {cached.trade_date} · "
            f"完整度 {cached.completeness * 100:.1f}% · "
            f"模式 {cached.mode}"
        )
        if cached.warnings:
            self.warning_label.setText(
                "⚠️ " + " ; ".join(str(w) for w in cached.warnings)
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        self._populate_all()

    # 外部调用
    def refresh(self) -> None:
        """主窗口切换到本页时调用（main_window.show_page 已挂钩）。"""
        self._refresh_date_combo(prefer=self._current_trade_date)
