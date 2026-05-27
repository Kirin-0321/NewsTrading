"""模板评估页（plan M4 全新页面，一期占位 + 桩数据）。

页面分三层：
    上层 控制条 — 时间范围、忽略版本、重打分、导出 CSV
    中层 主表   — 每个 (prompt_id, prompt_version) 一行：
                   样本数 / D+1 / D+3 / D+5 平均分 / Alpha / 命中率 / AI 高质量占比
    下层 折线图 — 选 2~3 个模板对比 30 天 D+5 平均分走势（PyQtChart 一期降级 QLabel）

一期实现状态：
    - 控制条 UI 全部到位（按钮可点但仅弹"打分系统未上线"提示）
    - 主表渲染桩数据（从 theme_predictions 取 prompt 分布 + 占位指标）
    - 折线图区先用 QLabel 占位（PyQtChart 接入留给 plan M4.2）

打分系统真正上线（plan M3 + M4）后：
    - 接入 services/scoring/scoring_service.py 取真实分数
    - 移除桩数据 + 启用所有按钮真实逻辑
"""

from __future__ import annotations

import csv
from typing import Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.utils.styles import (
    BUTTON_PRIMARY,
    BUTTON_SUCCESS,
    COMBOBOX_STYLE,
    TABLE_STYLE,
)


_RANGE_OPTIONS = [
    ("最近 7 天", 7),
    ("最近 30 天", 30),
    ("最近 90 天", 90),
    ("全部", 0),
]


_EVAL_TABLE_COLS = [
    ("模板", 200),
    ("版本", 60),
    ("样本数", 70),
    ("D+1 平均分", 90),
    ("D+3 平均分", 90),
    ("D+5 平均分", 90),
    ("平均 Alpha", 90),
    ("命中率", 70),
    ("AI 高质量占比", 110),
    ("最近报告日", 100),
]


# 样本数低于此阈值时染色提示（review 补丁 P8）
_LOW_SAMPLE_THRESHOLD = 5


class PromptEvalPage(QWidget):
    """模板评估页（一期占位）。"""

    def __init__(self):
        super().__init__()
        self._current_rows: List[Dict] = []
        self.init_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("📊 模板评估面板")
        title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        # 顶部提示（一期未接打分系统）
        self.hint_label = QLabel(
            "⚠️ 打分系统（plan M3）未上线，当前仅展示桩数据。"
            "上线后此面板将自动接入真实分数。"
        )
        self.hint_label.setStyleSheet(
            "padding: 8px 12px; background: #fff7e6; color: #ad6800; "
            "border: 1px solid #ffd591; border-radius: 4px;"
        )
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        layout.addWidget(self._build_control_bar())
        layout.addWidget(self._build_table_area(), 2)
        layout.addWidget(self._build_chart_area(), 1)

    def _build_control_bar(self) -> QGroupBox:
        group = QGroupBox("🎛️ 筛选与操作")
        ctrl = QHBoxLayout()

        ctrl.addWidget(QLabel("时间范围:"))
        self.range_combo = QComboBox()
        self.range_combo.setStyleSheet(COMBOBOX_STYLE)
        for label, days in _RANGE_OPTIONS:
            self.range_combo.addItem(label, days)
        self.range_combo.setCurrentIndex(1)  # 默认 30 天
        self.range_combo.currentIndexChanged.connect(self.refresh)
        ctrl.addWidget(self.range_combo)

        ctrl.addSpacing(12)
        self.ignore_version_check = QCheckBox("忽略版本号（合并 v1/v2/v3）")
        self.ignore_version_check.setChecked(True)
        self.ignore_version_check.stateChanged.connect(self.refresh)
        ctrl.addWidget(self.ignore_version_check)

        ctrl.addStretch()

        self.rescore_btn = QPushButton("🔁 重打分（仅脚本部分）")
        self.rescore_btn.setStyleSheet(BUTTON_PRIMARY)
        self.rescore_btn.clicked.connect(self._on_rescore)
        ctrl.addWidget(self.rescore_btn)

        self.export_btn = QPushButton("⬇️ 导出 CSV")
        self.export_btn.setStyleSheet(BUTTON_SUCCESS)
        self.export_btn.clicked.connect(self._on_export_csv)
        ctrl.addWidget(self.export_btn)

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        ctrl.addWidget(self.refresh_btn)

        group.setLayout(ctrl)
        return group

    def _build_table_area(self) -> QGroupBox:
        group = QGroupBox("📈 模板评分汇总")
        layout = QVBoxLayout()

        self.table = QTableWidget(0, len(_EVAL_TABLE_COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in _EVAL_TABLE_COLS])
        self.table.setStyleSheet(TABLE_STYLE)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for i, (_, w) in enumerate(_EVAL_TABLE_COLS):
            self.table.setColumnWidth(i, w)
        self.table.horizontalHeader().setSectionResizeMode(
            len(_EVAL_TABLE_COLS) - 1, QHeaderView.Stretch
        )
        layout.addWidget(self.table)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #8c8c8c; padding-top: 6px;")
        layout.addWidget(self.stats_label)

        group.setLayout(layout)
        return group

    def _build_chart_area(self) -> QGroupBox:
        group = QGroupBox("📉 D+5 平均分对比（折线图）")
        layout = QVBoxLayout()
        self.chart_placeholder = QLabel(
            "📊 折线图区（plan M4.2 接入 PyQtChart 后启用）\n\n"
            "占位说明：上线后将支持选 2~3 个模板对比 30 天 D+5 平均分走势。"
        )
        self.chart_placeholder.setAlignment(Qt.AlignCenter)
        self.chart_placeholder.setStyleSheet(
            "color: #8c8c8c; padding: 40px; "
            "background: #fafafa; border: 1px dashed #d9d9d9;"
        )
        layout.addWidget(self.chart_placeholder)
        group.setLayout(layout)
        return group

    # ------------------------------------------------------------------
    # 数据加载（桩）
    # ------------------------------------------------------------------

    def refresh(self):
        """重载表格数据。

        一期数据源：直接从 theme_predictions 统计 (prompt_id, prompt_version)
        分布作为桩，所有评分指标都填占位符 "—"。
        """
        try:
            days = self.range_combo.currentData()
            ignore_version = self.ignore_version_check.isChecked()
            rows = self._load_stub_rows(
                days=days, ignore_version=ignore_version
            )
        except Exception as e:
            self.stats_label.setText(f"加载失败: {e}")
            return

        self._current_rows = rows
        self._render_table(rows)
        self.stats_label.setText(
            f"共 {len(rows)} 个模板组（"
            f"{'忽略版本' if ignore_version else '按版本拆分'}）"
        )

    def _load_stub_rows(
        self,
        *,
        days: int,
        ignore_version: bool,
    ) -> List[Dict]:
        """桩数据：从 theme_predictions 统计模板分布。"""
        from datetime import datetime, timedelta

        from gui.utils.prompt_name_helper import friendly_prompt_name
        from services.storage.ai_inference_db import get_ai_inference_db

        date_filter = ""
        params: List = []
        if days and days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).strftime(
                "%Y-%m-%d"
            )
            date_filter = "WHERE report_date >= ?"
            params.append(cutoff)

        if ignore_version:
            group_clause = "GROUP BY prompt_id"
            select_extra = "NULL AS prompt_version"
        else:
            group_clause = "GROUP BY prompt_id, prompt_version"
            select_extra = "COALESCE(prompt_version, '—') AS prompt_version"

        sql = f"""
            SELECT prompt_id,
                   {select_extra},
                   COUNT(*) AS sample_count,
                   MAX(report_date) AS last_date
            FROM theme_predictions
            {date_filter}
            {group_clause}
            ORDER BY sample_count DESC
        """

        with get_ai_inference_db().connect() as conn:
            raw_rows = conn.execute(sql, params).fetchall()

        rows: List[Dict] = []
        for r in raw_rows:
            pid = r["prompt_id"] or "—"
            rows.append({
                "prompt_id": pid,
                "prompt_name": friendly_prompt_name(pid, fallback="（未标注模板）"),
                "prompt_version": r["prompt_version"] or "—",
                "sample_count": r["sample_count"],
                "d1": "—",
                "d3": "—",
                "d5": "—",
                "alpha": "—",
                "hit_rate": "—",
                "ai_quality": "—",
                "last_date": r["last_date"] or "—",
            })
        return rows

    def _render_table(self, rows: List[Dict]) -> None:
        self.table.setRowCount(len(rows))
        for row, data in enumerate(rows):
            cells = [
                data["prompt_name"],
                str(data["prompt_version"]),
                str(data["sample_count"]),
                str(data["d1"]),
                str(data["d3"]),
                str(data["d5"]),
                str(data["alpha"]),
                str(data["hit_rate"]),
                str(data["ai_quality"]),
                str(data["last_date"]),
            ]
            sample = data["sample_count"] if isinstance(
                data["sample_count"], int
            ) else 0
            low_sample = sample < _LOW_SAMPLE_THRESHOLD

            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                # 样本数列染色（review 补丁 P8）
                if col == 2 and low_sample:
                    item.setBackground(QColor("#fff7e6"))
                    item.setToolTip(
                        f"样本数仅 {sample}，建议 ≥ {_LOW_SAMPLE_THRESHOLD} "
                        "才有统计意义"
                    )
                self.table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # 按钮 handlers
    # ------------------------------------------------------------------

    def _on_rescore(self):
        reply = QMessageBox.question(
            self,
            "确认重打分",
            "将对当前筛选范围的题材重跑脚本打分，约耗时数十秒；"
            "不会重新调 AI 评分员。\n确定继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        QMessageBox.information(
            self,
            "提示",
            "打分系统（plan M3）未上线，按钮已就绪。\n"
            "M3 落地后此处将调用 scoring_service.rescore_range()。",
        )

    def _on_export_csv(self):
        if not self._current_rows:
            QMessageBox.warning(self, "提示", "当前无数据可导出")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出模板评估 CSV",
            "prompt_eval.csv",
            "CSV 文件 (*.csv)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([c[0] for c in _EVAL_TABLE_COLS])
                for r in self._current_rows:
                    writer.writerow([
                        r["prompt_name"], r["prompt_version"],
                        r["sample_count"],
                        r["d1"], r["d3"], r["d5"],
                        r["alpha"], r["hit_rate"], r["ai_quality"],
                        r["last_date"],
                    ])
            QMessageBox.information(self, "成功", f"已导出: {path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
