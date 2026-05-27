"""手动回测页面（Phase 6 配套 GUI + 2026-05-27 时间窗语义重构）。

业务定位
--------
让主人在 GUI 上手选「模板 / 交易日 / 新闻时间窗 / 新闻状态」一键跑单次
虚拟回测，实时看到：

1. 时间边界预览（snapshot_inspect 同款，包含 D+1~D+5）
2. dry-run 试算（不调 LLM，看 snapshot 通不通）
3. 正式跑（异步调 ``tools.backtest_prompt.backtest_one``）
4. 结果：md 内容预览 + 题材列表

对应规约：``doc/design/05-27-1515-回测时间边界说明书.md``

时间窗主人语义
--------------
* 左边界（**自由可调**）默认 = trade_date 14:00
* 右边界（**只可向左调，硬上限**）默认 = next_trade_date 09:00
* 「精选新闻」默认勾选（``curated``）；取消勾选 = ``raw_news`` 全部

调用链
------
::

    ManualBacktestPage
       ├── _build_time_preview()  →  inspect_snapshot(...)（同步）
       ├── _on_dry_run_clicked()  →  ManualBacktestWorker(dry_run=True)
       └── _on_run_clicked()      →  ManualBacktestWorker(dry_run=False)
                                       └── backtest_one(...)（异步）

注意事项
--------
* 单次只允许一个 worker 在跑（按钮 disable / enable 二态）
* trade_date 限制 ≤ 今天（QDateEdit.setMaximumDate(today)）
* 右边界 QDateTimeEdit.setMaximumDateTime(next_open 09:00) 兜底
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QDate, QDateTime, Qt
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from gui.utils.styles import (
    BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS,
    COMBOBOX_STYLE, INPUT_STYLE, TABLE_STYLE, TEXTBROWSER_STYLE,
)
from gui.workers.manual_backtest_worker import ManualBacktestWorker


# ---------------------------------------------------------------------------
# 主页面
# ---------------------------------------------------------------------------


class ManualBacktestPage(QWidget):
    """手动回测页面：选模板/时间窗/新闻状态，一键跑单次虚拟回测。"""

    def __init__(self):
        super().__init__()
        self.worker: Optional[ManualBacktestWorker] = None
        self._suppress_window_signal = False
        self.init_ui()
        self.load_template_list()
        self._apply_default_window()
        self._refresh_preview()

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
            "选定 (模板 / 交易日 / 新闻窗 / 新闻状态) → 一键跑单次回测。"
            "默认窗 = trade_date 14:00 ~ next_open 09:00（主人语义）。"
            "右边界最大值 = 下一交易日 09:00（硬上限，防穿越）。"
        )
        subtitle.setStyleSheet("color: #666; font-size: 12px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_config_group())
        layout.addWidget(self._build_preview_group(), 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_md_group())
        splitter.addWidget(self._build_themes_group())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 4)

    def _build_config_group(self) -> QGroupBox:
        group = QGroupBox("回测参数")
        outer = QVBoxLayout(group)

        # 第 1 行：模板 + 交易日 + 精选新闻 + 重置默认窗
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("模板:"))
        self.template_combo = QComboBox()
        self.template_combo.setStyleSheet(COMBOBOX_STYLE)
        self.template_combo.setMinimumWidth(280)
        row1.addWidget(self.template_combo)
        row1.addSpacing(15)

        row1.addWidget(QLabel("交易日:"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setStyleSheet(INPUT_STYLE)
        self.date_edit.setMaximumDate(QDate.currentDate())
        self.date_edit.setDate(QDate.currentDate().addDays(-1))
        # 改 trade_date 自动重置时间窗为新默认值
        self.date_edit.dateChanged.connect(self._on_trade_date_changed)
        row1.addWidget(self.date_edit)
        row1.addSpacing(15)

        self.curated_checkbox = QCheckBox("仅精选新闻 (curated)")
        self.curated_checkbox.setChecked(True)
        self.curated_checkbox.setToolTip(
            "勾选：仅用 clean_status='curated' 的精选新闻\n"
            "取消：用 raw_news 全部（含 rejected / pending）"
        )
        self.curated_checkbox.stateChanged.connect(self._refresh_preview)
        row1.addWidget(self.curated_checkbox)
        row1.addSpacing(15)

        self.overwrite_checkbox = QCheckBox("已存在则覆盖")
        self.overwrite_checkbox.setChecked(True)
        self.overwrite_checkbox.setToolTip(
            "命中同 (模板, 交易日, is_backtest=1) 时：\n"
            "  勾选 = 删除旧记录(ai_reports + theme_predictions +\n"
            "         子表 + md 文件) 后重跑，得到全新结果\n"
            "  取消 = 直接跳过，保留旧记录（避免无意中重跑浪费 LLM）"
        )
        row1.addWidget(self.overwrite_checkbox)
        row1.addStretch()
        outer.addLayout(row1)

        # 第 2 行：新闻窗左边界 + 右边界 + 重置默认 + Provider
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("新闻窗左边界:"))
        self.news_start_edit = QDateTimeEdit()
        self.news_start_edit.setCalendarPopup(True)
        self.news_start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.news_start_edit.setStyleSheet(INPUT_STYLE)
        self.news_start_edit.dateTimeChanged.connect(
            self._on_window_changed
        )
        row2.addWidget(self.news_start_edit)
        row2.addSpacing(15)

        row2.addWidget(QLabel("右边界:"))
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
        row2.addWidget(self.news_end_edit)
        row2.addSpacing(10)

        self.reset_window_btn = QPushButton("↺ 默认窗")
        self.reset_window_btn.setStyleSheet(BUTTON_SUCCESS)
        self.reset_window_btn.setToolTip(
            "重置为主人默认：左 = trade_date 14:00 / 右 = next_open 09:00"
        )
        self.reset_window_btn.clicked.connect(self._apply_default_window)
        row2.addWidget(self.reset_window_btn)
        row2.addSpacing(15)

        row2.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.setStyleSheet(COMBOBOX_STYLE)
        self.provider_combo.addItems(["deepseek", "openai", "qwen"])
        self.provider_combo.setCurrentText("deepseek")
        self.provider_combo.setMinimumWidth(120)
        row2.addWidget(self.provider_combo)
        row2.addStretch()
        outer.addLayout(row2)

        # 第 3 行：动作按钮
        row3 = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 刷新预览")
        self.refresh_btn.setStyleSheet(BUTTON_PRIMARY)
        self.refresh_btn.clicked.connect(self._refresh_preview)
        row3.addWidget(self.refresh_btn)

        self.dry_run_btn = QPushButton("🧪 dry-run 试算（不调 LLM）")
        self.dry_run_btn.setStyleSheet(BUTTON_SUCCESS)
        self.dry_run_btn.clicked.connect(
            lambda: self._on_run_clicked(dry_run=True)
        )
        row3.addWidget(self.dry_run_btn)

        self.run_btn = QPushButton("🚀 正式跑回测（消耗 LLM）")
        self.run_btn.setStyleSheet(BUTTON_PRIMARY)
        self.run_btn.clicked.connect(
            lambda: self._on_run_clicked(dry_run=False)
        )
        row3.addWidget(self.run_btn, 1)

        self.cancel_btn = QPushButton("⏹ 取消")
        self.cancel_btn.setStyleSheet(BUTTON_DANGER)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip(
            "一期 LLM 调用难以中断；先用 dry-run 验证时间边界"
        )
        row3.addWidget(self.cancel_btn)
        outer.addLayout(row3)

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

    def _build_md_group(self) -> QGroupBox:
        group = QGroupBox("📄 报告 md 预览（最多 8000 字）")
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

        self.md_browser = QTextBrowser()
        self.md_browser.setStyleSheet(TEXTBROWSER_STYLE)
        layout.addWidget(self.md_browser, 1)
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
        """从 `core.ai_config.AIConfig` 拉所有 analysis 模板。"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            templates = config.get_prompt_templates()
            self.template_combo.blockSignals(True)
            self.template_combo.clear()
            for key, tmpl in templates.items():
                display_name = tmpl.get("name", key)
                self.template_combo.addItem(
                    f"{display_name}  [{key}]", key,
                )
            self.template_combo.blockSignals(False)
            for i in range(self.template_combo.count()):
                if self.template_combo.itemData(i) == "custom_6":
                    self.template_combo.setCurrentIndex(i)
                    break
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "加载模板失败",
                f"无法加载 prompts/analysis/*.md: {exc}",
            )

    def refresh(self):
        """主窗口切换到本页时调用：刷新模板列表 + 默认窗 + 预览。"""
        self.load_template_list()
        self._apply_default_window()
        self._refresh_preview()

    # =====================================================================
    # 时间窗 / 默认值管理
    # =====================================================================

    def _on_trade_date_changed(self, _date: QDate):
        """trade_date 变了 → 自动重置时间窗 + 刷预览。"""
        self._apply_default_window()
        self._refresh_preview()

    def _on_window_changed(self, _dt: QDateTime):
        if self._suppress_window_signal:
            return
        self._refresh_preview()

    def _apply_default_window(self):
        """按主人语义重算默认窗 + 右边界硬上限，刷到两个 datetime 控件。

        - 左 = trade_date 14:00
        - 右 = next_trade_date(trade_date) 09:00
        - 右边界 QDateTimeEdit.setMaximumDateTime = 同一个上限
        """
        trade_date = self.date_edit.date().toString("yyyyMMdd")
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
        return {
            "template_id": self.template_combo.currentData() or "",
            "trade_date": self.date_edit.date().toString("yyyyMMdd"),
            "news_start_dt": self.news_start_edit.dateTime().toPyDateTime(),
            "news_end_dt": self.news_end_edit.dateTime().toPyDateTime(),
            "news_status": (
                "curated" if self.curated_checkbox.isChecked() else ""
            ),
            "provider": self.provider_combo.currentText() or None,
            "overwrite": self.overwrite_checkbox.isChecked(),
        }

    def _refresh_preview(self):
        """调 ``tools.snapshot_inspect.inspect_snapshot`` 渲染时间边界。

        注意：这是同步调用，但 snapshot 重建本身只读 SQLite ~100ms 完成，
        不会卡住 GUI。
        """
        inputs = self._current_inputs()
        if not inputs["template_id"]:
            self.preview_browser.setHtml(
                "<i style='color:#999'>（请先选择模板）</i>"
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

    def _on_run_clicked(self, *, dry_run: bool):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self, "正在跑", "已有一个回测在跑，请等它完成"
            )
            return
        inputs = self._current_inputs()
        if not inputs["template_id"]:
            QMessageBox.warning(self, "缺参数", "请先选择模板")
            return

        # 防穿越提示（GUI 也提前拦一道，体验更好）
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

        action_text = "dry-run 试算" if dry_run else "正式跑回测"
        self.status_label.setText(
            f"<span style='color:#06c'>⏳ {action_text}中...</span>"
        )
        self.md_browser.clear()
        self.themes_table.setRowCount(0)
        self.open_md_btn.setEnabled(False)
        self._set_buttons_running(True)

        self.worker = ManualBacktestWorker(
            template_id=inputs["template_id"],
            trade_date=inputs["trade_date"],
            news_start_dt=inputs["news_start_dt"],
            news_end_dt=inputs["news_end_dt"],
            news_status=inputs["news_status"],
            provider=inputs["provider"],
            overwrite=inputs["overwrite"],
            dry_run=dry_run,
        )
        self.worker.stage.connect(self._on_stage)
        self.worker.finished_result.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _set_buttons_running(self, running: bool):
        self.refresh_btn.setEnabled(not running)
        self.dry_run_btn.setEnabled(not running)
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(False)  # 一期始终 disable

    def _on_stage(self, msg: str):
        self.status_label.setText(
            f"<span style='color:#06c'>⏳ {msg}</span>"
        )

    def _on_error(self, msg: str):
        self._set_buttons_running(False)
        self.status_label.setText(
            f"<span style='color:#c00'>❌ {msg.splitlines()[0]}</span>"
        )
        QMessageBox.critical(self, "回测异常", msg)

    def _on_finished(self, result: dict):
        self._set_buttons_running(False)
        ok = result.get("ok")
        skipped = result.get("skipped")
        if not ok:
            self.status_label.setText(
                f"<span style='color:#c00'>❌ 失败: "
                f"{result.get('error') or '未知错误'}</span>"
            )
            return
        if skipped:
            self.status_label.setText(
                f"<span style='color:#c80'>⚠ 跳过: "
                f"{result.get('error') or '已存在或无新闻'}</span>"
            )
            return

        report_path = result.get("report_path")
        themes_count = result.get("themes_count") or 0
        news_count = result.get("snapshot_news_count")
        elapsed_ms = result.get("elapsed_ms")

        overwrite_html = ""
        if result.get("overwritten"):
            overwrite_html = (
                f"&nbsp;&nbsp;<span style='color:#c80'>"
                f"🗑 已覆盖旧记录: "
                f"-ai_reports={result.get('deleted_reports', 0)} "
                f"-themes={result.get('deleted_themes', 0)} "
                f"-md={result.get('deleted_md_files', 0)}</span>"
            )

        self.status_label.setText(
            f"<span style='color:#0a0'>✅ 成功</span>"
            f"&nbsp;&nbsp;news={news_count} themes={themes_count} "
            f"&nbsp;{elapsed_ms} ms"
            + overwrite_html
            + (
                f"&nbsp;&nbsp;<span style='color:#888'>"
                f"file={Path(report_path).name}</span>"
                if report_path else ""
            )
        )

        if report_path:
            self._render_md(report_path)
            self._render_themes(report_path)
            self.open_md_btn.setEnabled(True)
            self._last_report_path = report_path
        else:
            self.md_browser.setHtml(
                "<i style='color:#888'>dry-run 模式：无 md 产出</i>"
            )

    # =====================================================================
    # 渲染辅助
    # =====================================================================

    def _render_md(self, report_path: str):
        """读 md 文件前 8000 字显示。"""
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
        truncated = text[:8000]
        suffix = (
            f"\n\n...（共 {len(text)} 字，仅显示前 8000）"
            if len(text) > 8000 else ""
        )
        self.md_browser.setMarkdown(truncated + suffix)

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
                    score_item.setForeground(Qt.darkGreen)
                elif score < 0:
                    score_item.setForeground(Qt.darkRed)
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
