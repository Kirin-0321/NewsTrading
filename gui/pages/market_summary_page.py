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

    # 指数卡片：key 与 tushare_fetcher.INDEX_CODES 第二列对齐
    _INDEX_CARDS: list = [
        ("sh", "上证综指"),
        ("sz", "深证成指"),
        ("cyb", "创业板指"),
        ("hs300", "沪深 300"),
        ("kc50", "科创 50"),
        ("zz500", "中证 500"),
        ("zz1000", "中证 1000"),
    ]

    def _build_tab_overview(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        # 1) 指数卡片组 —— 3×3 = 9 格；前 7 格放指数，后 2 格放上涨/下跌家数 KPI
        idx_group = QGroupBox("大盘指数 / 市场宽度")
        idx_layout = QGridLayout(idx_group)
        idx_layout.setHorizontalSpacing(10)
        idx_layout.setVerticalSpacing(8)
        self._index_cards: Dict[str, Dict[str, QLabel]] = {}
        for i, (key, label) in enumerate(self._INDEX_CARDS):
            card = self._make_index_card(label)
            self._index_cards[key] = card
            idx_layout.addWidget(card["widget"], i // 3, i % 3)
        # 第 3 行后两格：上涨/下跌家数（沿用 index_card 样式以保持视觉对齐）
        self.advance_card = self._make_index_card(
            "上涨家数",
            number_color=_COLOR_RED,
            subtitle="（advance）",
        )
        self.decline_card = self._make_index_card(
            "下跌家数",
            number_color=_COLOR_GREEN,
            subtitle="（decline）",
        )
        idx_layout.addWidget(self.advance_card["widget"], 2, 1)
        idx_layout.addWidget(self.decline_card["widget"], 2, 2)
        outer.addWidget(idx_group)

        # 2) 量能 + 资金流 + 涨跌停情绪
        mid_row = QHBoxLayout()
        mid_row.setSpacing(12)

        flow_group = QGroupBox("成交 / 资金流")
        flow_layout = QGridLayout(flow_group)
        self.turnover_label = self._make_kpi_label("—")
        self.prev_turnover_label = self._make_kpi_label("—")
        self.north_label = self._make_kpi_label("—")
        self.south_label = self._make_kpi_label("—")
        self.main_net_label = self._make_kpi_label("—")
        flow_layout.addWidget(QLabel("两市成交:"), 0, 0)
        flow_layout.addWidget(self.turnover_label, 0, 1)
        flow_layout.addWidget(QLabel("昨日成交:"), 1, 0)
        flow_layout.addWidget(self.prev_turnover_label, 1, 1)
        flow_layout.addWidget(QLabel("北向净买:"), 2, 0)
        flow_layout.addWidget(self.north_label, 2, 1)
        flow_layout.addWidget(QLabel("南向净买:"), 3, 0)
        flow_layout.addWidget(self.south_label, 3, 1)
        flow_layout.addWidget(QLabel("主力净流入:"), 4, 0)
        flow_layout.addWidget(self.main_net_label, 4, 1)
        mid_row.addWidget(flow_group, 1)

        breadth_group = QGroupBox("涨跌停 / 情绪")
        breadth_layout = QGridLayout(breadth_group)
        self.up_label = self._make_kpi_label("—", color=_COLOR_RED)
        self.down_label = self._make_kpi_label("—", color=_COLOR_GREEN)
        self.fail_label = self._make_kpi_label("—")
        self.fail_rate_label = self._make_kpi_label("—")
        self.seal_label = self._make_kpi_label("—")
        self.promo_label = self._make_kpi_label("—")
        self.height_label = self._make_kpi_label("—")
        breadth_layout.addWidget(QLabel("涨停:"), 0, 0)
        breadth_layout.addWidget(self.up_label, 0, 1)
        breadth_layout.addWidget(QLabel("跌停:"), 0, 2)
        breadth_layout.addWidget(self.down_label, 0, 3)
        breadth_layout.addWidget(QLabel("炸板数:"), 1, 0)
        breadth_layout.addWidget(self.fail_label, 1, 1)
        breadth_layout.addWidget(QLabel("炸板率:"), 1, 2)
        breadth_layout.addWidget(self.fail_rate_label, 1, 3)
        breadth_layout.addWidget(QLabel("封板率:"), 2, 0)
        breadth_layout.addWidget(self.seal_label, 2, 1)
        breadth_layout.addWidget(QLabel("晋级率:"), 2, 2)
        breadth_layout.addWidget(self.promo_label, 2, 3)
        breadth_layout.addWidget(QLabel("最高板:"), 3, 0)
        breadth_layout.addWidget(self.height_label, 3, 1, 1, 3)
        mid_row.addWidget(breadth_group, 1)

        outer.addLayout(mid_row)

        # 3) 元信息 + 数据质量（A5 拆为左右两栏）
        outer.addWidget(self._build_meta_quality_group())

        outer.addStretch()
        return wrap

    def _make_index_card(
        self,
        title: str,
        *,
        number_color: Optional[QColor] = None,
        subtitle: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成通用"指数卡片"控件。

        输入:
            title        卡片标题（如 "上证综指" / "上涨家数"）
            number_color 大字号数值的固定颜色（advance/decline 用红/绿；指数用 None=动态）
            subtitle     标题下方淡灰小字（用于标注字段来源，可省）
        输出:
            dict{widget, title, close, pct, amount} —— 字段名沿用旧约定以兼容 _populate_overview
        """
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setStyleSheet(
            "QFrame{background:white;border:1px solid #F0F0F0;"
            "border-radius:6px;}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        title_label = QLabel(
            f"{title}  <span style='color:#BFBFBF;font-size:10px'>"
            f"{subtitle}</span>" if subtitle else title
        )
        title_label.setStyleSheet("color:#8C8C8C;font-size:12px;")
        close_label = QLabel("—")
        color_css = (
            f"color:{number_color.name()};"
            if number_color is not None else "color:#262626;"
        )
        close_label.setStyleSheet(
            f"font-size:18px;font-weight:bold;{color_css}"
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
    # A5: 元信息 + 数据质量 左右两栏
    # ------------------------------------------------------------------

    def _build_meta_quality_group(self) -> QGroupBox:
        """元信息区。左栏：基础信息 / 右栏：数据质量分层 + gaps/conflicts 摘要。

        gaps / conflicts 的完整明细放进 tooltip（鼠标悬停可看），
        避免占用界面空间又能让主人随时查得到。
        """
        group = QGroupBox("元信息 / 数据质量")
        outer = QHBoxLayout(group)
        outer.setSpacing(20)

        # 左栏：基础信息
        left = QGroupBox("基础信息")
        left_lay = QGridLayout(left)
        self.meta_mode = QLabel("—")
        self.meta_generated_at = QLabel("—")
        self.meta_api_calls = QLabel("—")
        self.meta_elapsed = QLabel("—")
        self.meta_merge_stats = QLabel("—")
        left_lay.addWidget(QLabel("模式:"), 0, 0)
        left_lay.addWidget(self.meta_mode, 0, 1)
        left_lay.addWidget(QLabel("生成时间:"), 1, 0)
        left_lay.addWidget(self.meta_generated_at, 1, 1)
        left_lay.addWidget(QLabel("API 调用:"), 2, 0)
        left_lay.addWidget(self.meta_api_calls, 2, 1)
        left_lay.addWidget(QLabel("耗时:"), 3, 0)
        left_lay.addWidget(self.meta_elapsed, 3, 1)
        left_lay.addWidget(QLabel("合并统计:"), 4, 0)
        left_lay.addWidget(self.meta_merge_stats, 4, 1)
        outer.addWidget(left, 1)

        # 右栏：数据质量
        right = QGroupBox("数据质量")
        right_lay = QGridLayout(right)
        self.meta_completeness = QLabel("—")
        self.meta_completeness.setStyleSheet(
            "font-size:14px;font-weight:bold;color:#262626;"
        )
        self.meta_layer_detail = QLabel("—")
        self.meta_layer_detail.setStyleSheet("color:#595959;")
        self.meta_gaps_label = QLabel("缺失字段: —")
        self.meta_gaps_label.setStyleSheet("color:#FA8C16;")
        self.meta_conflicts_label = QLabel("合并冲突: —")
        self.meta_conflicts_label.setStyleSheet("color:#722ED1;")
        right_lay.addWidget(QLabel("总完整度:"), 0, 0)
        right_lay.addWidget(self.meta_completeness, 0, 1)
        right_lay.addWidget(QLabel("分层:"), 1, 0)
        right_lay.addWidget(self.meta_layer_detail, 1, 1)
        right_lay.addWidget(self.meta_gaps_label, 2, 0, 1, 2)
        right_lay.addWidget(self.meta_conflicts_label, 3, 0, 1, 2)
        outer.addWidget(right, 1)

        return group

    # ------------------------------------------------------------------
    # Tab 2: 板块
    # ------------------------------------------------------------------

    _SECTOR_COLS = [
        ("#", 32),
        ("板块（代码）", 200),
        ("涨幅", 70),
        ("5日", 70),
        ("涨停", 50),
        ("主力(亿)", 90),
        ("龙头股 Top3", 320),
        ("催化（cls/ai）", 320),
    ]
    # high_risk 行背景色（pct_chg_5d 阈值由 GUI 自算 — 后端 high_risk 当前永远 None）
    _RISK_HIGH_BG = QColor("#FFEBEE")    # 浅红：5 日累涨 > 15%
    _RISK_MID_BG = QColor("#FFF8E1")     # 浅黄：5 日累涨 > 8%

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
    # A4 新增「其他席位」表的列定义
    _OTHER_TRADER_COLS = [
        ("营业部全名", 380),
        ("操作", 60),
        ("关联股", 110),
        ("净买入(亿)", 100),
        ("买入(亿)", 90),
        ("卖出(亿)", 90),
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
        self.dt_stock_table = self._make_basic_table(self._DT_STOCK_COLS)
        sl.addWidget(self.dt_stock_table)
        split.addWidget(stocks_group)

        # 中：已识别游资席位
        traders_group = QGroupBox("⭐ 已识别游资席位（紫粉行）")
        tl = QVBoxLayout(traders_group)
        self.trader_table = self._make_basic_table(self._TRADER_COLS)
        tl.addWidget(self.trader_table)
        split.addWidget(traders_group)

        # 下：其他席位（A4 新增）—— 未命中 dim_trader_alias.is_famous=1 的普通席位
        other_group = QGroupBox(
            "📊 其他席位（未识别 / 普通机构）— 直查 fact_top_inst"
        )
        ol = QVBoxLayout(other_group)
        self.other_trader_table = self._make_basic_table(
            self._OTHER_TRADER_COLS
        )
        ol.addWidget(self.other_trader_table)
        split.addWidget(other_group)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 3)
        layout.addWidget(split, 1)
        return wrap

    def _make_basic_table(
        self, cols: list,
    ) -> QTableWidget:
        """统一构造 QTableWidget。

        输入 cols: list of (header_text, width)
        输出: 已设好列宽 / 表头 / 样式 / 只读 / 隐藏行号 的 QTableWidget。
        """
        tbl = QTableWidget()
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels([c[0] for c in cols])
        for i, (_, w) in enumerate(cols):
            tbl.setColumnWidth(i, w)
        header = tbl.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(
                len(cols) - 1, QHeaderView.Stretch
            )
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setStyleSheet(TABLE_STYLE)
        return tbl

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
        breadth = summary.get("breadth") or {}
        sentiment = summary.get("sentiment") or {}
        cap = summary.get("capital_flow") or {}

        # ------ 7 个指数卡片 ------
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

        # ------ 上涨家数 / 下跌家数（后端当前为 placeholder = None）------
        self._fill_breadth_card(
            self.advance_card,
            value=breadth.get("advance"),
            suffix="家",
            placeholder_hint="后端 service.py 暂未实装该字段，永远返回 None",
        )
        self._fill_breadth_card(
            self.decline_card,
            value=breadth.get("decline"),
            suffix="家",
            placeholder_hint="后端 service.py 暂未实装该字段，永远返回 None",
        )

        # ------ 量能 / 资金流 ------
        self.turnover_label.setText(
            _fmt_yi(indices.get("total_turnover_yi"))
        )
        self.prev_turnover_label.setText(
            _fmt_yi(indices.get("prev_turnover_yi"))
        )
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
        main_net = cap.get("main_net_yi")
        self.main_net_label.setText(
            _fmt_yi(main_net) if main_net is not None else "—"
        )
        if main_net is None:
            self.main_net_label.setToolTip(
                "后端 service.py 暂未实装该字段（capital_flow.main_net_yi 永远 None）"
            )

        # ------ 涨跌停 / 情绪 ------
        self.up_label.setText(_fmt_int(breadth.get("limit_up")))
        self.down_label.setText(_fmt_int(breadth.get("limit_down")))
        self.fail_label.setText(_fmt_int(breadth.get("failed_limit")))
        self.fail_rate_label.setText(_fmt_rate(sentiment.get("fail_rate")))
        seal = sentiment.get("seal_rate")
        seal_prev = sentiment.get("seal_rate_prev")
        seal_text = _fmt_rate(seal)
        if seal_prev is not None:
            seal_text += f"  (昨 {_fmt_rate(seal_prev)})"
        elif seal is not None:
            seal_text += "  (昨 —)"
        self.seal_label.setText(seal_text)
        self.promo_label.setText(_fmt_rate(sentiment.get("promotion_rate")))
        # 最高板：拼"X 板 · 股名 · 题材"
        height_val = sentiment.get("max_height")
        max_stock = sentiment.get("max_stock") or ""
        max_sector = sentiment.get("max_sector") or ""
        if height_val:
            parts = [f"{height_val} 板"]
            if max_stock:
                parts.append(str(max_stock))
            if max_sector:
                parts.append(str(max_sector))
            self.height_label.setText(" · ".join(parts))
        else:
            self.height_label.setText("—")

        # ------ A5 元信息 / 数据质量 ------
        self._populate_meta_quality(summary)

    def _fill_breadth_card(
        self,
        card: Dict[str, Any],
        *,
        value: Optional[int],
        suffix: str,
        placeholder_hint: str,
    ) -> None:
        """填充 advance/decline 卡片。

        value 为 None 时显示 "—" 并在 tooltip 中提示是后端 placeholder。
        """
        if value is None:
            card["close"].setText("—")
            card["pct"].setText("（暂无数据）")
            card["pct"].setStyleSheet("font-size:13px;color:#BFBFBF;")
            card["amount"].setText("")
            card["widget"].setToolTip(placeholder_hint)
        else:
            card["close"].setText(f"{int(value):,d}")
            card["pct"].setText(suffix)
            card["pct"].setStyleSheet("font-size:13px;color:#595959;")
            card["amount"].setText("")
            card["widget"].setToolTip("")

    def _populate_meta_quality(self, summary: Dict[str, Any]) -> None:
        """A5: 填充元信息左栏 + 数据质量右栏 + gaps/conflicts tooltip。"""
        meta = summary.get("meta") or {}
        quality = summary.get("quality") or {}
        gaps = summary.get("gaps") or []
        conflicts = summary.get("conflicts") or []

        # 左栏
        self.meta_mode.setText(str(meta.get("mode") or "—"))
        self.meta_generated_at.setText(str(meta.get("generated_at") or "—"))
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

        # 右栏：完整度 + 分层
        comp = quality.get("completeness_score") or 0
        comp_pct = float(comp) * 100
        comp_color = (
            "#52C41A" if comp_pct >= 90 else
            "#FA8C16" if comp_pct >= 60 else
            "#F5222D"
        )
        self.meta_completeness.setText(f"{comp_pct:.2f}%")
        self.meta_completeness.setStyleSheet(
            f"font-size:14px;font-weight:bold;color:{comp_color};"
        )
        l1 = quality.get("l1_complete")
        l2 = quality.get("l2_complete")
        l3 = quality.get("l3_complete")
        nfr = quality.get("numeric_field_rate")

        def _layer_part(label: str, val: Optional[float]) -> str:
            if val is None:
                return f"{label} —"
            return f"{label} {float(val) * 100:.0f}%"

        self.meta_layer_detail.setText(
            " / ".join([
                _layer_part("L1", l1),
                _layer_part("L2", l2),
                _layer_part("L3", l3),
                _layer_part("数值", nfr),
            ])
        )

        # gaps / conflicts 摘要 + tooltip 详情
        self.meta_gaps_label.setText(f"缺失字段: {len(gaps)} 项")
        if gaps:
            lines = [
                f"• {g.get('field', '?')}: {g.get('reason', '')}"
                for g in gaps[:20]
            ]
            if len(gaps) > 20:
                lines.append(f"…（另有 {len(gaps) - 20} 项未展示）")
            self.meta_gaps_label.setToolTip("\n".join(lines))
            self.meta_gaps_label.setStyleSheet(
                "color:#FA8C16;font-weight:bold;"
            )
        else:
            self.meta_gaps_label.setToolTip("无缺失字段，盘后数据完整。")
            self.meta_gaps_label.setStyleSheet("color:#52C41A;")

        self.meta_conflicts_label.setText(f"合并冲突: {len(conflicts)} 项")
        if conflicts:
            lines = [
                f"• [{c.get('type', '?')}] {c.get('field', '?')} "
                f"winner={c.get('winner', '?')} "
                f"loser={c.get('loser', '?')}"
                for c in conflicts[:20]
            ]
            if len(conflicts) > 20:
                lines.append(
                    f"…（另有 {len(conflicts) - 20} 项未展示）"
                )
            self.meta_conflicts_label.setToolTip("\n".join(lines))
            self.meta_conflicts_label.setStyleSheet(
                "color:#722ED1;font-weight:bold;"
            )
        else:
            self.meta_conflicts_label.setToolTip("merger 未发现 cls/ai 字段冲突。")
            self.meta_conflicts_label.setStyleSheet("color:#52C41A;")

    def _populate_sectors(self, summary: Dict[str, Any]) -> None:
        from gui.utils.market_db_helper import query_sector_leaders

        sectors = summary.get("sectors_top") or []
        td = (
            (summary.get("meta") or {}).get("trade_date")
            or self._current_trade_date
            or ""
        )

        self.sector_table.setRowCount(len(sectors))
        for row, s in enumerate(sectors):
            pct = s.get("pct_chg")
            pct_5d = s.get("pct_chg_5d")
            cats = s.get("catalysts") or []
            src = (s.get("catalysts_source") or "none").lower()
            match = s.get("catalysts_match") or ""
            name = str(s.get("name") or "")
            ts_code = str(s.get("ts_code") or "")
            main_net = s.get("main_net_yi")

            # 板块名 + 代码合并显示
            name_cell = (
                f"{name} [{ts_code}]" if name and ts_code else
                name or ts_code or "—"
            )

            # 龙头股 Top3 —— 后端 leaders 当前是空数组，用 helper 查 fact_limit_stock 自算
            leaders_text = self._format_sector_leaders(
                name, td, fallback=s.get("leaders") or []
            )

            # high_risk 自算（后端 high_risk 当前永远 None）
            risk_level, risk_bg, risk_tip = self._compute_high_risk(pct_5d)

            cells = [
                str(s.get("rank") or row + 1),
                name_cell,
                _fmt_pct(pct),
                _fmt_pct(pct_5d),
                _fmt_int(s.get("limit_up_count")),
                _fmt_num(main_net),
                leaders_text,
                "  ·  ".join(cats) if cats else "—",
            ]

            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)

                # 先按 high_risk 给所有列上行底色（最后一列后续会被 cls/ai 色覆盖）
                if risk_bg is not None:
                    item.setBackground(QBrush(risk_bg))

                # 板块名列：tooltip 显示 high_risk 详情
                if col == 1 and risk_tip:
                    item.setToolTip(risk_tip)

                # 涨幅 / 5 日涨幅着色
                if col == 2:
                    c = _pct_color(pct)
                    if c is not None:
                        item.setForeground(c)
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                elif col == 3:
                    c = _pct_color(pct_5d)
                    if c is not None:
                        item.setForeground(c)
                    if risk_level == "高":
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                elif col == 5 and main_net is not None:
                    try:
                        f_val = float(main_net)
                        item.setForeground(
                            _COLOR_RED if f_val > 0 else
                            _COLOR_GREEN if f_val < 0 else _COLOR_MUTED
                        )
                    except (TypeError, ValueError):
                        pass

                # catalysts 列（最后一列）—— cls/ai 色块覆盖 high_risk 底色
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

    def _format_sector_leaders(
        self,
        sector_name: str,
        trade_date: str,
        *,
        fallback: list,
    ) -> str:
        """从 fact_limit_stock 取该板块涨停股做龙头股代理，格式 "名(状态) · ..."。

        输入:
            sector_name 板块名（用于 LIKE 匹配 fact_limit_stock.theme）
            trade_date  YYYYMMDD
            fallback    后端 sectors_top[].leaders（M2 阶段填充，当前为空）
        输出:
            "{name}({状态}) · {name}({状态}) · ..."；查不到时返回 "—"
        """
        from gui.utils.market_db_helper import query_sector_leaders

        # 1) 后端 leaders 优先（未来后端实装后自动接管）
        if isinstance(fallback, list) and fallback:
            parts = []
            for ld in fallback[:3]:
                if not isinstance(ld, dict):
                    continue
                n = str(ld.get("name") or ld.get("ts_code") or "")
                st = str(ld.get("status") or "")
                parts.append(f"{n}({st})" if st else n)
            if parts:
                return " · ".join(parts)

        # 2) GUI 端自算
        if not (sector_name and trade_date):
            return "—"
        rows = query_sector_leaders(sector_name, trade_date, limit=3)
        if not rows:
            return "—"
        out = []
        for r in rows:
            n = str(r.get("name") or r.get("ts_code") or "")
            cons = r.get("cons_nums")
            try:
                cons_int = int(cons) if cons is not None else None
            except (TypeError, ValueError):
                cons_int = None
            if cons_int is not None and cons_int >= 2:
                out.append(f"{n}({cons_int}板)")
            elif cons_int == 1:
                out.append(f"{n}(首)")
            else:
                out.append(n)
        return " · ".join(out) if out else "—"

    def _compute_high_risk(
        self, pct_chg_5d: Optional[float],
    ) -> tuple:
        """根据 5 日累计涨幅判定板块高位风险等级。

        输出 (level, bg_color_or_None, tooltip_or_None)
            level   "高" / "中" / None
            bg      QColor 或 None
            tip     str 或 None
        """
        if pct_chg_5d is None:
            return (None, None, None)
        try:
            v = float(pct_chg_5d)
        except (TypeError, ValueError):
            return (None, None, None)
        if v > 15.0:
            return (
                "高", self._RISK_HIGH_BG,
                f"⚠️ 高位风险：5 日累计涨幅 {v:+.2f}% > 15%，注意追高风险",
            )
        if v > 8.0:
            return (
                "中", self._RISK_MID_BG,
                f"5 日累计涨幅 {v:+.2f}%，中等热度",
            )
        return (None, None, None)

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
        from gui.utils.market_db_helper import (
            query_other_traders, query_stock_names,
        )

        dt = summary.get("dragon_tiger") or {}
        stocks = dt.get("stocks") or []
        famous = dt.get("famous_traders") or []
        td = (
            (summary.get("meta") or {}).get("trade_date")
            or self._current_trade_date
            or ""
        )

        # 顺手补股票名（后端 service.py:712 写死 None）
        name_map: Dict[str, str] = {}
        if stocks:
            need = [
                str(s.get("ts_code") or "") for s in stocks
                if (not s.get("name")) and s.get("ts_code")
            ]
            name_map = query_stock_names(need) if need else {}

        # 上：龙虎榜个股
        self.dt_stock_table.setRowCount(len(stocks))
        for row, s in enumerate(stocks):
            net = s.get("net_amount_yi")
            reasons = s.get("reasons") or []
            code = str(s.get("ts_code") or "")
            name = str(s.get("name") or name_map.get(code) or "—")
            cells = [
                code,
                name,
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

        # 中：已识别游资席位
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

        # 下：其他席位（A4）—— GUI 直查 DB，不进 summary
        other_rows = query_other_traders(td) if td else []
        self.other_trader_table.setRowCount(len(other_rows))
        for row, r in enumerate(other_rows):
            side = str(r.get("side") or "")
            side_label = (
                "买入" if side.lower() in ("buy", "b") else
                "卖出" if side.lower() in ("sell", "s") else side
            )
            net = r.get("net_buy_yi")
            cells = [
                str(r.get("exalter") or ""),
                side_label,
                str(r.get("ts_code") or ""),
                _fmt_num(net),
                _fmt_num(r.get("buy_amount_yi")),
                _fmt_num(r.get("sell_amount_yi")),
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 3 and net is not None:
                    try:
                        f = float(net)
                        item.setForeground(
                            _COLOR_RED if f > 0 else
                            _COLOR_GREEN if f < 0 else _COLOR_MUTED
                        )
                    except (TypeError, ValueError):
                        pass
                self.other_trader_table.setItem(row, col, item)

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
