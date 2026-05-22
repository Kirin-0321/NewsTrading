"""
爬虫管理页面
包含参数设置、启动控制、实时进度显示
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                             QPushButton, QLabel, QSpinBox, QProgressBar,
                             QTextBrowser, QMessageBox, QCheckBox, QTableWidget,
                             QTableWidgetItem, QHeaderView)
from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QTextCursor

from gui.utils.styles import *
from gui.workers.crawler_worker import CrawlerWorker
import os
from datetime import datetime


class CrawlerPage(QWidget):
    """爬虫管理页面"""
    
    def __init__(self):
        super().__init__()
        self.worker = None
        self.init_ui()
        
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # 标题
        title = QLabel("🕷️ 爬虫管理")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)
        
        # 参数设置区域
        params_group = self.create_params_group()
        layout.addWidget(params_group)
        
        # 控制按钮区域
        control_layout = self.create_control_buttons()
        layout.addLayout(control_layout)
        
        # 进度显示区域
        progress_group = self.create_progress_group()
        layout.addWidget(progress_group)
        
        # 日志显示区域
        log_group = self.create_log_group()
        layout.addWidget(log_group, 1)  # 占据剩余空间
        
        # 历史记录（简化版）
        history_group = self.create_history_group()
        layout.addWidget(history_group)
        
    def create_params_group(self):
        """创建参数设置组"""
        group = QGroupBox("参数设置")
        group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        
        layout = QHBoxLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        
        # 滚动次数
        layout.addWidget(QLabel("滚动次数:"))
        self.scroll_times_spin = QSpinBox()
        self.scroll_times_spin.setRange(1, 100)
        self.scroll_times_spin.setValue(36)
        self.scroll_times_spin.setStyleSheet(INPUT_STYLE)
        layout.addWidget(self.scroll_times_spin)
        layout.addWidget(QLabel("次"))
        
        layout.addSpacing(20)
        
        # 等待时间
        layout.addWidget(QLabel("等待时间:"))
        self.wait_seconds_spin = QSpinBox()
        self.wait_seconds_spin.setRange(1, 60)
        self.wait_seconds_spin.setValue(6)
        self.wait_seconds_spin.setStyleSheet(INPUT_STYLE)
        layout.addWidget(self.wait_seconds_spin)
        layout.addWidget(QLabel("秒"))
        
        layout.addSpacing(20)
        
        # 无数据停止次数
        layout.addWidget(QLabel("无数据停止:"))
        self.max_no_change_spin = QSpinBox()
        self.max_no_change_spin.setRange(1, 20)
        self.max_no_change_spin.setValue(3)
        self.max_no_change_spin.setStyleSheet(INPUT_STYLE)
        self.max_no_change_spin.setToolTip("连续N次无新数据时停止滚动")
        layout.addWidget(self.max_no_change_spin)
        layout.addWidget(QLabel("次"))
        
        layout.addSpacing(20)
        
        # 无头模式
        self.headless_check = QCheckBox("无头模式")
        self.headless_check.setChecked(True)
        self.headless_check.setToolTip("后台运行，不显示浏览器窗口")
        layout.addWidget(self.headless_check)
        
        layout.addSpacing(20)
        
        # 自动停止（增量爬取）
        self.auto_stop_check = QCheckBox("自动停止")
        self.auto_stop_check.setChecked(True)
        self.auto_stop_check.setToolTip("遇到数据库中已有的新闻时自动停止爬取")
        layout.addWidget(self.auto_stop_check)
        
        layout.addStretch()
        
        group.setLayout(layout)
        return group
    
    def create_control_buttons(self):
        """创建控制按钮"""
        layout = QHBoxLayout()
        
        self.start_btn = QPushButton("▶ 开始爬取")
        self.start_btn.setStyleSheet(BUTTON_PRIMARY)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.clicked.connect(self.start_crawler)
        layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setStyleSheet(BUTTON_DANGER)
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_crawler)
        layout.addWidget(self.stop_btn)
        
        layout.addStretch()
        
        return layout
    
    def create_progress_group(self):
        """创建进度显示组"""
        group = QGroupBox("实时进度")
        group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(PROGRESS_STYLE)
        self.progress_bar.setMinimumHeight(30)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)
        
        # 进度信息
        info_layout = QHBoxLayout()
        self.progress_label = QLabel("等待开始...")
        self.progress_label.setStyleSheet("color: #595959; font-size: 13px;")
        info_layout.addWidget(self.progress_label)
        
        info_layout.addStretch()
        
        self.news_count_label = QLabel("已获取: 0 条新闻")
        self.news_count_label.setStyleSheet("color: #1890ff; font-weight: bold; font-size: 13px;")
        info_layout.addWidget(self.news_count_label)
        
        layout.addLayout(info_layout)
        
        group.setLayout(layout)
        return group
    
    def create_log_group(self):
        """创建日志显示组"""
        group = QGroupBox("运行日志")
        group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        
        self.log_browser = QTextBrowser()
        self.log_browser.setStyleSheet(TEXTBROWSER_STYLE + """
            QTextBrowser {
                font-family: Consolas, monospace;
                font-size: 12px;
                line-height: 1.5;
            }
        """)
        layout.addWidget(self.log_browser)
        
        # 清空日志按钮
        clear_btn = QPushButton("清空日志")
        clear_btn.setStyleSheet("border: 1px solid #d9d9d9; padding: 5px 10px; border-radius: 4px;")
        clear_btn.clicked.connect(self.log_browser.clear)
        layout.addWidget(clear_btn, alignment=Qt.AlignRight)
        
        group.setLayout(layout)
        return group
    
    def create_history_group(self):
        """创建历史记录组"""
        group = QGroupBox("最近记录")
        group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        
        # 显示最近生成的文件
        self.history_label = QLabel("暂无记录")
        self.history_label.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)
        
        group.setLayout(layout)
        return group
    
    @pyqtSlot(int, int, int)
    def start_crawler_from_schedule(self, scroll_times, wait_seconds, max_no_change):
        """从定时任务启动爬虫"""
        # 设置参数
        self.scroll_times_spin.setValue(scroll_times)
        self.wait_seconds_spin.setValue(wait_seconds)
        self.max_no_change_spin.setValue(max_no_change)
        
        # 启动爬虫
        self.start_crawler()
    
    def start_crawler(self):
        """启动爬虫"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "警告", "爬虫正在运行中！")
            return
        
        # 禁用开始按钮，启用停止按钮
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        
        # 重置进度
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在初始化...")
        self.news_count_label.setText("已获取: 0 条新闻")
        
        # 添加日志
        self.add_log("=" * 60)
        self.add_log(f"开始爬取 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.add_log("=" * 60)
        
        # 创建工作线程
        self.worker = CrawlerWorker(
            scroll_times=self.scroll_times_spin.value(),
            wait_seconds=self.wait_seconds_spin.value(),
            headless=self.headless_check.isChecked(),
            max_no_change=self.max_no_change_spin.value(),
            auto_stop=self.auto_stop_check.isChecked()
        )
        
        # 连接信号
        self.worker.progress_updated.connect(self.on_progress_updated)
        self.worker.finished.connect(self.on_crawler_finished)
        self.worker.error.connect(self.on_crawler_error)
        self.worker.log_message.connect(self.add_log)
        
        # 启动线程
        self.worker.start()
    
    def stop_crawler(self):
        """停止爬虫"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, '确认', '确定要停止爬虫吗？',
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.worker.stop()
                self.add_log("用户手动停止")
                self.reset_buttons()
    
    def on_progress_updated(self, current, total, news_count, message):
        """进度更新"""
        percentage = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(percentage)
        self.progress_label.setText(message)
        self.news_count_label.setText(f"已获取: {news_count} 条新闻")
    
    def on_crawler_finished(self, files):
        """爬虫完成"""
        self.add_log("=" * 60)
        self.add_log(f"爬取完成！生成了 {len(files)} 个文件")
        self.add_log("=" * 60)
        
        # 更新历史记录
        files_text = "\n".join([os.path.basename(f) for f in files])
        self.history_label.setText(f"最近生成的文件:\n{files_text}")
        
        self.reset_buttons()
        
        QMessageBox.information(
            self, "成功",
            f"爬取完成！\n生成了 {len(files)} 个文件"
        )
    
    def on_crawler_error(self, error_msg):
        """爬虫出错"""
        self.add_log(f"错误: {error_msg}")
        self.reset_buttons()
        QMessageBox.critical(self, "错误", error_msg)
    
    def reset_buttons(self):
        """重置按钮状态"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
    
    def add_log(self, message):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_browser.append(f"[{timestamp}] {message}")
        # 自动滚动到底部
        self.log_browser.moveCursor(QTextCursor.End)

