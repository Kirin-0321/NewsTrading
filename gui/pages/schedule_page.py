"""
定时任务页面
管理 crawl_sync / clean_sync / analyze 三类定时任务
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QDialog, QDialogButtonBox, QSpinBox, QTimeEdit, QFormLayout,
    QCheckBox, QLineEdit, QComboBox,
)
from PyQt5.QtCore import Qt, QTime

from gui.utils.styles import *

import json
import os
import uuid

TASK_TYPE_LABELS = {
    "crawl_sync": "数据获取（爬取入库）",
    "clean_sync": "数据清理（AI清洗）",
    "analyze": "新闻分析",
    "market_fetch": "盘后数据拉取",
    # 打分系统（plan M3 桩，等 M3 上线后真实可用）
    "stock_daily_sync": "📊 个股日行情同步（plan M3）",
    "sector_daily_sync": "📊 板块日行情同步（plan M3）",
    "theme_score_daily": "📊 题材每日打分（plan M3）",
    "theme_ai_review": "🤖 AI 评分员 D+5 复审（plan M3）",
}


class SchedulePage(QWidget):
    """定时任务页面"""

    def __init__(self, scheduler_service=None):
        super().__init__()
        self.tasks_file = "data/schedule_tasks.json"
        self.scheduler_service = scheduler_service
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = QLabel("⏰ 定时任务管理")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.addStretch()
        add_btn = QPushButton("➕ 新建任务")
        add_btn.setStyleSheet(BUTTON_PRIMARY)
        add_btn.clicked.connect(self.add_task)
        toolbar.addWidget(add_btn)
        layout.addLayout(toolbar)

        self.task_table = QTableWidget()
        self.task_table.setColumnCount(6)
        self.task_table.setHorizontalHeaderLabels(
            ["任务名称", "类型", "执行周期", "状态", "上次执行", "操作"]
        )
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 5):
            self.task_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeToContents
            )
        self.task_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        self.task_table.setColumnWidth(5, 180)
        self.task_table.setStyleSheet(TABLE_STYLE)
        self.task_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.task_table)

        info = QLabel(
            "<b>说明：</b><br>"
            "• <b>数据获取</b>：定时爬取并去重写入 SQLite 原始库，完成后自动 AI 清洗<br>"
            "• <b>数据清理</b>：单独清洗未处理原始数据（通常无需另建，爬取任务已串联）<br>"
            "• <b>新闻分析</b>：按配置对精选库数据生成分析报告<br>"
            "• 程序需保持运行，定时任务才会触发"
        )
        info.setStyleSheet(
            "background-color: #fff7e6; border: 1px solid #ffd591;"
            "border-radius: 4px; padding: 10px; color: #262626;"
        )
        layout.addWidget(info)

        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("📡 调度服务:"))
        running = self.scheduler_service and self.scheduler_service.running
        self.service_status = QLabel("运行中" if running else "未启动")
        color = "#52c41a" if running else "#ff4d4f"
        self.service_status.setStyleSheet(f"color: {color}; font-weight: bold;")
        status_layout.addWidget(self.service_status)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        self.load_tasks()

    def load_tasks(self):
        self.task_table.setRowCount(0)
        if not os.path.exists(self.tasks_file):
            return
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            for task in tasks:
                self.add_task_row(task)
        except Exception as e:
            print(f"加载任务失败: {e}")

    @staticmethod
    def _format_schedule(task: dict) -> str:
        hours = task.get("interval_hours")
        if hours:
            return f"每 {hours} 小时"
        return task.get("time", "")

    def add_task_row(self, task):
        from services.scheduled_runner import normalize_task

        task = normalize_task(task)
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)

        self.task_table.setItem(row, 0, QTableWidgetItem(task.get("name", "")))
        ttype = task.get("type", "crawl_sync")
        self.task_table.setItem(
            row, 1, QTableWidgetItem(TASK_TYPE_LABELS.get(ttype, ttype))
        )
        schedule_text = self._format_schedule(task)
        self.task_table.setItem(row, 2, QTableWidgetItem(schedule_text))
        status = "启用" if task.get("enabled", True) else "禁用"
        self.task_table.setItem(row, 3, QTableWidgetItem(status))
        self.task_table.setItem(
            row, 4, QTableWidgetItem(task.get("last_run", "从未执行"))
        )

        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(5, 2, 5, 2)

        toggle_btn = QPushButton("禁用" if task.get("enabled", True) else "启用")
        toggle_btn.clicked.connect(lambda: self.toggle_task(row))
        btn_layout.addWidget(toggle_btn)

        delete_btn = QPushButton("删除")
        delete_btn.setStyleSheet("color: #ff4d4f;")
        delete_btn.clicked.connect(lambda: self.delete_task(row))
        btn_layout.addWidget(delete_btn)

        self.task_table.setCellWidget(row, 5, btn_widget)

    def add_task(self):
        dialog = TaskDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.save_task(dialog.get_task())
            self.load_tasks()

    def save_task(self, new_task):
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        tasks = []
        if os.path.exists(self.tasks_file):
            try:
                with open(self.tasks_file, "r", encoding="utf-8") as f:
                    tasks = json.load(f)
            except json.JSONDecodeError:
                pass
        if "id" not in new_task:
            new_task["id"] = str(uuid.uuid4())
        tasks.append(new_task)
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        if self.scheduler_service:
            self.scheduler_service.reload_tasks()

    def toggle_task(self, row):
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            if row < len(tasks):
                tasks[row]["enabled"] = not tasks[row].get("enabled", True)
                with open(self.tasks_file, "w", encoding="utf-8") as f:
                    json.dump(tasks, f, ensure_ascii=False, indent=2)
                if self.scheduler_service:
                    self.scheduler_service.reload_tasks()
                self.load_tasks()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def delete_task(self, row):
        if QMessageBox.question(
            self, "确认", "确定删除此任务？", QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return
        try:
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            if row < len(tasks):
                del tasks[row]
                with open(self.tasks_file, "w", encoding="utf-8") as f:
                    json.dump(tasks, f, ensure_ascii=False, indent=2)
                if self.scheduler_service:
                    self.scheduler_service.reload_tasks()
                self.load_tasks()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))


class TaskDialog(QDialog):
    """新建定时任务对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建定时任务")
        self.resize(420, 360)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：早盘爬取")
        form.addRow("任务名称:", self.name_edit)

        self.type_combo = QComboBox()
        for key, label in TASK_TYPE_LABELS.items():
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("任务类型:", self.type_combo)

        self.schedule_mode = QComboBox()
        self.schedule_mode.addItem("按间隔", "interval")
        self.schedule_mode.addItem("每日固定时间", "daily")
        self.schedule_mode.currentIndexChanged.connect(self._on_schedule_mode_changed)
        self.schedule_mode_label = QLabel("调度方式:")
        form.addRow(self.schedule_mode_label, self.schedule_mode)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 24)
        self.interval_spin.setValue(1)
        self.interval_spin.setSuffix(" 小时")
        self.interval_row_label = QLabel("执行间隔:")
        form.addRow(self.interval_row_label, self.interval_spin)

        self.time_edit = QTimeEdit()
        self.time_edit.setTime(QTime(9, 0))
        self.time_row_label = QLabel("执行时间:")
        form.addRow(self.time_row_label, self.time_edit)

        self.scroll_spin = QSpinBox()
        self.scroll_spin.setRange(1, 100)
        self.scroll_spin.setValue(36)
        self.scroll_row_label = QLabel("滚动次数:")
        form.addRow(self.scroll_row_label, self.scroll_spin)

        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(1, 60)
        self.wait_spin.setValue(6)
        self.wait_row_label = QLabel("等待秒数:")
        form.addRow(self.wait_row_label, self.wait_spin)

        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 168)
        self.hours_spin.setValue(24)
        self.hours_row_label = QLabel("分析时间窗口(小时):")
        form.addRow(self.hours_row_label, self.hours_spin)

        # 盘后数据专用：模式 + 强制重拉
        self.market_mode_combo = QComboBox()
        self.market_mode_combo.addItem("hybrid（推荐）", "hybrid")
        self.market_mode_combo.addItem("tushare-only（无 AI）", "tushare-only")
        self.market_mode_row_label = QLabel("盘后数据模式:")
        form.addRow(self.market_mode_row_label, self.market_mode_combo)

        self.market_force_check = QCheckBox("无视缓存，强制重新获取")
        self.market_force_row_label = QLabel("强制重拉:")
        form.addRow(self.market_force_row_label, self.market_force_check)

        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(True)
        form.addRow("启用:", self.enabled_check)

        layout.addLayout(form)
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self._on_type_changed(0)
        self._on_schedule_mode_changed(0)

    def _on_schedule_mode_changed(self, _index):
        is_interval = self.schedule_mode.currentData() == "interval"
        self.interval_spin.setVisible(is_interval)
        self.interval_row_label.setVisible(is_interval)
        self.time_edit.setVisible(not is_interval)
        self.time_row_label.setVisible(not is_interval)

    def _on_type_changed(self, _index):
        ttype = self.type_combo.currentData()
        is_crawl = ttype == "crawl_sync"
        is_analyze = ttype == "analyze"
        is_market = ttype == "market_fetch"
        self.scroll_spin.setVisible(is_crawl)
        self.scroll_row_label.setVisible(is_crawl)
        self.wait_spin.setVisible(is_crawl)
        self.wait_row_label.setVisible(is_crawl)
        self.hours_spin.setVisible(is_analyze)
        self.hours_row_label.setVisible(is_analyze)
        self.market_mode_combo.setVisible(is_market)
        self.market_mode_row_label.setVisible(is_market)
        self.market_force_check.setVisible(is_market)
        self.market_force_row_label.setVisible(is_market)
        if is_crawl:
            idx = self.schedule_mode.findData("interval")
            if idx >= 0:
                self.schedule_mode.setCurrentIndex(idx)
        elif is_market:
            # 盘后数据强烈建议每日 16:00 跑
            idx = self.schedule_mode.findData("daily")
            if idx >= 0:
                self.schedule_mode.setCurrentIndex(idx)
            self.time_edit.setTime(QTime(16, 0))

    def get_task(self):
        ttype = self.type_combo.currentData()
        task = {
            "name": self.name_edit.text() or "未命名任务",
            "type": ttype,
            "enabled": self.enabled_check.isChecked(),
            "last_run": "从未执行",
            "params": {},
        }
        if self.schedule_mode.currentData() == "interval":
            task["interval_hours"] = self.interval_spin.value()
        else:
            task["time"] = self.time_edit.time().toString("HH:mm")
        if ttype == "crawl_sync":
            task["params"] = {
                "scroll_times": self.scroll_spin.value(),
                "wait_seconds": self.wait_spin.value(),
                "incremental": True,
                "headless": True,
                "auto_clean": True,
                "batch_size": 100,
                "provider": "deepseek",
                "limit": 500,
            }
        elif ttype == "clean_sync":
            task["params"] = {"batch_size": 100, "provider": "deepseek"}
        elif ttype == "analyze":
            task["params"] = {
                "source": "curated",
                "time_range_hours": self.hours_spin.value(),
                "template": "short_term",
                "provider": "deepseek",
            }
        elif ttype == "market_fetch":
            task["params"] = {
                "mode": self.market_mode_combo.currentData() or "hybrid",
                "force_refresh": self.market_force_check.isChecked(),
                "top_sector_n": 20,
            }
        return task
