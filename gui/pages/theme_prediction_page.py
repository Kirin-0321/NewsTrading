"""题材预测库页面。

上半区: 手动选择分析报告 .md → 调 AI 抽取 → 入库
下半区: 按报告日期查看已入库题材，行点击展开标的与新闻

调用链:
    init_ui()
        ├── _build_extract_group()       手动抽取
        └── _build_viewer_group()        查看已入库
    start_extract()
        └→ ThemeExtractWorker (线程)
    showEvent / refresh / 日期切换
        └→ _reload_dates() / _reload_themes_for_date()
    选中题材表格行
        └→ _show_theme_detail()         填充标的与新闻表
"""

import os
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QComboBox, QLineEdit, QFileDialog, QMessageBox, QTextBrowser,
    QTableWidget, QTableWidgetItem, QSplitter, QTabWidget,
)

from gui.utils.styles import (
    BUTTON_PRIMARY, BUTTON_SUCCESS, COMBOBOX_STYLE, INPUT_STYLE,
    TEXTBROWSER_STYLE, TABLE_STYLE,
)
from gui.workers.theme_extract_worker import ThemeExtractWorker


_THEME_TABLE_COLS = [
    ("时间", 56),
    ("题材", 180),
    ("等级", 90),
    ("分数", 60),
    ("板块代码", 90),
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


class ThemePredictionPage(QWidget):
    """题材预测库页面。"""

    def __init__(self):
        super().__init__()
        self.worker: Optional[ThemeExtractWorker] = None
        self._current_themes: list = []
        self._sort_col: Optional[int] = None
        self._sort_asc: bool = True
        self.init_ui()
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

    # ---------- 上半区：手动抽取 ----------

    def _build_extract_group(self) -> QGroupBox:
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
        self.extract_btn.clicked.connect(self.start_extract)
        action_row.addWidget(self.extract_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        # 阶段日志（带时间戳）
        self.extract_log = QTextBrowser()
        self.extract_log.setStyleSheet(TEXTBROWSER_STYLE)
        self.extract_log.setMaximumHeight(100)
        layout.addWidget(self.extract_log)

        # AI 流式输出预览（chunk 实时粘连写入，避免长任务假死观感）
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

    def start_extract(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "提示", "抽取正在进行中")
            return

        path = self.file_input.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请先选择报告文件")
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "提示", f"文件不存在：{path}")
            return

        self.extract_btn.setEnabled(False)
        self.extract_log.clear()
        self.stream_browser.clear()
        self._append_log(f"开始抽取: {os.path.basename(path)}")

        self.worker = ThemeExtractWorker(report_path=path)
        self.worker.progress.connect(self._append_log)
        self.worker.streaming.connect(self._append_stream)
        self.worker.finished.connect(self._on_extract_finished)
        self.worker.error.connect(self._on_extract_error)
        self.worker.start()

    def _append_stream(self, chunk: str):
        """流式 chunk 粘连写入预览区，自动滚到末尾。"""
        self.stream_browser.insertPlainText(chunk)
        self.stream_browser.moveCursor(QTextCursor.End)

    def _on_extract_finished(self, payload: dict):
        msg = (
            f"✅ 入库完成: {payload['themes']} 题材 / "
            f"{payload['stocks']} 标的 / {payload['news']} 新闻引用"
        )
        self._append_log(msg)
        if payload.get("warning"):
            self._append_log(f"⚠️ 警告: {payload['warning']}")

        self.extract_btn.setEnabled(True)

        # 自动刷新查看区到对应日期
        self._reload_dates(prefer_date=payload.get("report_date"))

    def _on_extract_error(self, msg: str):
        self._append_log(f"❌ {msg}")
        self.extract_btn.setEnabled(True)
        QMessageBox.critical(self, "抽取失败", msg)

    def _append_log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.extract_log.append(f"[{ts}] {msg}")

    # ---------- 下半区：查看 ----------

    def _build_viewer_group(self) -> QGroupBox:
        group = QGroupBox("📊 已入库题材")
        layout = QVBoxLayout()

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

        # 打分明细 Tab（一期占位，等 plan M3 打分系统上线后填充）
        self.score_detail_browser = QTextBrowser()
        self.score_detail_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.score_detail_browser.setHtml(
            '<div style="padding:20px;color:#8c8c8c;">'
            '📊 <b>打分明细</b>（plan M3 打分系统未上线）<br><br>'
            '上线后此处将展示该题材 5 天追踪期的逐日表现：<br>'
            '• 板块涨幅 vs 标的平均涨幅 vs 大盘涨幅 三线对比<br>'
            '• D+1~D+5 脚本分 + 命中率 + Alpha<br>'
            '• D+5 AI 评分员定性评语<br>'
            '</div>'
        )

        self.detail_tab.addTab(self.reason_browser, "📝 逻辑/原因")
        self.detail_tab.addTab(self.stock_table, "💼 关联标的")
        self.detail_tab.addTab(self.news_splitter, "📰 关联新闻")
        self.detail_tab.addTab(self.score_detail_browser, "📊 打分明细")
        layout.addWidget(self.detail_tab, 2)

        group.setLayout(layout)
        return group

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
        date = self.date_combo.currentData()
        if date:
            self._reload_themes_for_date(date)

    def _on_prompt_changed(self, _idx: int):
        date = self.date_combo.currentData()
        if date:
            self._reload_themes_for_date(date)

    def _reload_themes_for_date(self, report_date: str):
        from services.storage import get_theme_store
        prompt_id = self.prompt_combo.currentData()
        themes = get_theme_store().get_by_date(
            report_date, prompt_id=prompt_id
        )
        self._reset_theme_sort()
        self._current_themes = themes
        self._render_theme_table(themes)
        self._clear_detail()

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
        self.theme_table.setRowCount(len(themes))
        for row, t in enumerate(themes):
            score = t.get("strength_score")
            sector_code = t.get("sector_ts_code") or ""
            sector_conf = t.get("sector_match_conf")
            sector_text = (
                f"{sector_code} ({sector_conf:.2f})"
                if sector_code and sector_conf is not None
                else sector_code
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
                        and sector_conf < 0.7:
                    # 板块匹配置信度低 → 标橙提示需人工确认
                    item.setForeground(QColor("#fa8c16"))
                self.theme_table.setItem(row, col, item)

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
