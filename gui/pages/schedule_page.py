"""
定时任务页面
管理定时爬取任务和任务流
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QTableWidget,
                             QTableWidgetItem, QHeaderView, QMessageBox,
                             QDialog, QDialogButtonBox, QSpinBox, QTimeEdit,
                             QFormLayout, QCheckBox, QLineEdit, QTabWidget,
                             QTextEdit)
from PyQt5.QtCore import Qt, QTime

from gui.utils.styles import *

import json
import os
import uuid


class SchedulePage(QWidget):
    """定时任务页面"""
    
    def __init__(self, scheduler_service=None):
        super().__init__()
        self.tasks_file = 'data/schedule_tasks.json'
        self.scheduler_service = scheduler_service
        self.init_ui()
        
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # 标题
        title = QLabel("⏰ 定时任务管理")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)
        
        # 标签页
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                background: white;
            }
            QTabBar::tab {
                background: #fafafa;
                border: 1px solid #d9d9d9;
                border-bottom: none;
                padding: 8px 20px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: white;
                border-bottom: 2px solid #1890ff;
            }
        """)
        
        # 标签页1: 普通定时任务
        self.simple_task_tab = self.create_simple_task_tab()
        self.tab_widget.addTab(self.simple_task_tab, "📅 定时任务")
        
        # 标签页2: 任务流
        self.workflow_tab = self.create_workflow_tab()
        self.tab_widget.addTab(self.workflow_tab, "🔄 任务流")
        
        layout.addWidget(self.tab_widget)
        
        # 服务状态
        status_layout = QHBoxLayout()
        status_label = QLabel("📡 调度服务:")
        status_label.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(status_label)
        
        self.service_status = QLabel("运行中" if self.scheduler_service and self.scheduler_service.running else "未启动")
        self.service_status.setStyleSheet("color: #52c41a; font-weight: bold;" if self.scheduler_service and self.scheduler_service.running else "color: #ff4d4f; font-weight: bold;")
        status_layout.addWidget(self.service_status)
        status_layout.addStretch()
        
        layout.addLayout(status_layout)
        
        # 加载数据
        self.load_tasks()
        self.load_workflows()
    
    def create_simple_task_tab(self):
        """创建普通定时任务标签页"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        
        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        
        add_btn = QPushButton("➕ 新建任务")
        add_btn.setStyleSheet(BUTTON_PRIMARY)
        add_btn.clicked.connect(self.add_task)
        toolbar.addWidget(add_btn)
        
        layout.addLayout(toolbar)
        
        # 任务表格
        self.task_table = QTableWidget()
        self.task_table.setColumnCount(5)
        self.task_table.setHorizontalHeaderLabels(["任务名称", "执行时间", "状态", "上次执行", "操作"])
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.task_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.task_table.setStyleSheet(TABLE_STYLE)
        self.task_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.task_table)
        
        # 说明
        info_label = QLabel("""
        <b>使用说明：</b><br>
        • 定时任务功能需要保持程序运行才能生效<br>
        • 任务将在每天指定时间自动执行爬虫<br>
        • 可以随时启用/禁用或删除任务
        """)
        info_label.setStyleSheet("""
            background-color: #fff7e6;
            border: 1px solid #ffd591;
            border-radius: 4px;
            padding: 10px;
            color: #262626;
        """)
        layout.addWidget(info_label)
        
        return tab
    
    def create_workflow_tab(self):
        """创建任务流标签页"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        
        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setStyleSheet(BUTTON_STYLE)
        refresh_btn.clicked.connect(self.refresh_workflows)
        toolbar.addWidget(refresh_btn)
        
        layout.addLayout(toolbar)
        
        # 任务流表格
        self.workflow_table = QTableWidget()
        self.workflow_table.setColumnCount(7)
        self.workflow_table.setHorizontalHeaderLabels([
            "任务流名称", "描述", "执行时间", "运行次数", "成功率", "最近状态", "操作"
        ])
        self.workflow_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.workflow_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.workflow_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.workflow_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.workflow_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.workflow_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.workflow_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Fixed)
        self.workflow_table.setColumnWidth(6, 280)
        self.workflow_table.setStyleSheet(TABLE_STYLE)
        self.workflow_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.workflow_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.workflow_table)
        
        # 实时日志显示
        log_group = QGroupBox("运行日志")
        log_group.setStyleSheet("""
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
                subcontrol-position: top left;
                padding: 0 5px;
                color: #262626;
            }
        """)
        log_layout = QVBoxLayout()
        
        from PyQt5.QtWidgets import QTextBrowser
        self.workflow_log_browser = QTextBrowser()
        self.workflow_log_browser.setStyleSheet(TEXTBROWSER_STYLE + """
            QTextBrowser {
                font-family: Consolas, monospace;
                font-size: 12px;
                line-height: 1.5;
            }
        """)
        log_layout.addWidget(self.workflow_log_browser)
        
        # 清空日志按钮
        clear_btn = QPushButton("清空日志")
        clear_btn.setStyleSheet("border: 1px solid #d9d9d9; padding: 5px 10px; border-radius: 4px;")
        clear_btn.clicked.connect(self.workflow_log_browser.clear)
        log_layout.addWidget(clear_btn, alignment=Qt.AlignRight)
        
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        
        return tab
    
    def load_workflows(self):
        """加载任务流列表"""
        try:
            from core.workflow_engine import get_workflow_engine
            
            engine = get_workflow_engine()
            workflows = engine.get_workflow_list()
            
            self.workflow_table.setRowCount(0)
            
            for workflow in workflows:
                self.add_workflow_row(workflow)
                
        except Exception as e:
            print(f"加载任务流失败: {e}")
            import traceback
            traceback.print_exc()
    
    def add_workflow_row(self, workflow):
        """添加任务流行"""
        row = self.workflow_table.rowCount()
        self.workflow_table.insertRow(row)
        
        # 任务流名称
        self.workflow_table.setItem(row, 0, QTableWidgetItem(workflow['name']))
        
        # 描述
        self.workflow_table.setItem(row, 1, QTableWidgetItem(workflow['description']))
        
        # 执行时间
        schedule = workflow.get('schedule', {})
        if schedule.get('enabled', False):
            time_str = f"每天 {schedule.get('time', '09:00')}"
        else:
            time_str = "未设置"
        self.workflow_table.setItem(row, 2, QTableWidgetItem(time_str))
        
        # 运行次数
        history = workflow.get('history', {})
        total_runs = history.get('total_runs', 0)
        self.workflow_table.setItem(row, 3, QTableWidgetItem(str(total_runs)))
        
        # 成功率
        if total_runs > 0:
            success_count = history.get('success_count', 0)
            success_rate = int(success_count / total_runs * 100)
            success_text = f"{success_rate}%"
        else:
            success_text = "-"
        self.workflow_table.setItem(row, 4, QTableWidgetItem(success_text))
        
        # 最近状态
        last_status = history.get('last_status')
        last_run = history.get('last_run', '从未执行')
        if last_status == 'success':
            status_text = f"✓ 成功\n{last_run}"
            status_item = QTableWidgetItem(status_text)
            from PyQt5.QtGui import QColor
            status_item.setForeground(QColor("#52c41a"))
        elif last_status == 'failure':
            status_text = f"✗ 失败\n{last_run}"
            status_item = QTableWidgetItem(status_text)
            from PyQt5.QtGui import QColor
            status_item.setForeground(QColor("#ff4d4f"))
        else:
            status_text = "从未执行"
            status_item = QTableWidgetItem(status_text)
        self.workflow_table.setItem(row, 5, status_item)
        
        # 操作按钮
        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(5, 2, 5, 2)
        btn_layout.setSpacing(5)
        
        toggle_btn = QPushButton("OFF" if workflow.get('enabled', True) else "ON")
        toggle_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #d9d9d9; border-radius: 3px;")
        toggle_btn.clicked.connect(lambda: self.toggle_workflow(workflow['workflow_id']))
        btn_layout.addWidget(toggle_btn)
        
        execute_btn = QPushButton("立即执行")
        execute_btn.setStyleSheet("padding: 3px 8px; background: #1890ff; color: white; border: none; border-radius: 3px;")
        execute_btn.clicked.connect(lambda: self.execute_workflow(workflow['workflow_id']))
        btn_layout.addWidget(execute_btn)
        
        details_btn = QPushButton("详情")
        details_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #d9d9d9; border-radius: 3px;")
        details_btn.clicked.connect(lambda: self.view_workflow_details(workflow['workflow_id']))
        btn_layout.addWidget(details_btn)
        
        logs_btn = QPushButton("日志")
        logs_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #d9d9d9; border-radius: 3px;")
        logs_btn.clicked.connect(lambda: self.view_workflow_logs(workflow['workflow_id']))
        btn_layout.addWidget(logs_btn)
        
        self.workflow_table.setCellWidget(row, 6, btn_widget)
        
        # 调整行高
        self.workflow_table.setRowHeight(row, 50)
    
    def refresh_workflows(self):
        """刷新任务流列表"""
        self.load_workflows()
        QMessageBox.information(self, "成功", "已刷新任务流列表")
    
    def toggle_workflow(self, workflow_id):
        """启用/禁用任务流"""
        try:
            from core.workflow_engine import get_workflow_engine
            
            engine = get_workflow_engine()
            info = engine.get_workflow_info(workflow_id)
            
            if info:
                new_enabled = not info.get('enabled', True)
                engine.toggle_workflow(workflow_id, new_enabled)
                
                self.load_workflows()
                status = "启用" if new_enabled else "禁用"
                QMessageBox.information(self, "成功", f"已{status}任务流")
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"操作失败: {str(e)}")
    
    def execute_workflow(self, workflow_id):
        """执行任务流"""
        try:
            from core.workflow_engine import get_workflow_engine
            from PyQt5.QtCore import QThread, pyqtSignal
            
            engine = get_workflow_engine()
            info = engine.get_workflow_info(workflow_id)
            
            if not info:
                QMessageBox.critical(self, "错误", "任务流不存在")
                return
            
            # 显示参数配置对话框
            dialog = WorkflowExecuteDialog(info, self)
            if dialog.exec_() != QDialog.Accepted:
                return
            
            params = dialog.get_params()
            
            # 清空日志
            self.workflow_log_browser.clear()
            self.workflow_log_browser.append(f"{'='*60}")
            self.workflow_log_browser.append(f"开始执行: {info['name']}")
            self.workflow_log_browser.append(f"{'='*60}\n")
            
            # 在后台执行
            class WorkflowThread(QThread):
                finished = pyqtSignal(dict)
                
                def __init__(self, workflow_id, params):
                    super().__init__()
                    self.workflow_id = workflow_id
                    self.params = params
                
                def run(self):
                    from core.workflow_engine import get_workflow_engine
                    engine = get_workflow_engine()
                    result = engine.execute_workflow(self.workflow_id, self.params)
                    self.finished.emit(result)
            
            # 创建执行线程
            self.workflow_thread = WorkflowThread(workflow_id, params)
            
            # 创建日志监控定时器
            from PyQt5.QtCore import QTimer
            self.log_timer = QTimer()
            self.last_log_pos = 0
            
            def update_log():
                try:
                    from pathlib import Path
                    log_file = Path(f"logs/workflows/{workflow_id}/latest.log")
                    if log_file.exists():
                        with open(log_file, 'r', encoding='utf-8') as f:
                            f.seek(self.last_log_pos)
                            new_content = f.read()
                            if new_content:
                                self.workflow_log_browser.append(new_content.rstrip())
                                self.last_log_pos = f.tell()
                                # 自动滚动到底部
                                scrollbar = self.workflow_log_browser.verticalScrollBar()
                                scrollbar.setValue(scrollbar.maximum())
                except:
                    pass
            
            self.log_timer.timeout.connect(update_log)
            self.log_timer.start(500)  # 每500ms更新一次
            
            def on_finished(result):
                self.log_timer.stop()
                update_log()  # 最后更新一次
                
                self.workflow_log_browser.append(f"\n{'='*60}")
                if result.get('status') == 'success':
                    duration = result.get('duration', 0)
                    self.workflow_log_browser.append(f"✓ 任务流执行成功！")
                    self.workflow_log_browser.append(f"总耗时: {duration:.1f}秒 ({duration/60:.1f}分钟)")
                    
                    results = result.get('results', {})
                    if 'analysis_file' in results:
                        self.workflow_log_browser.append(f"分析结果: {results['analysis_file']}")
                    
                    self.workflow_log_browser.append(f"{'='*60}")
                    
                    QMessageBox.information(self, "成功", f"任务流执行成功！\n总耗时: {duration:.1f}秒")
                else:
                    error = result.get('error', '未知错误')
                    self.workflow_log_browser.append(f"✗ 任务流执行失败")
                    self.workflow_log_browser.append(f"错误: {error}")
                    self.workflow_log_browser.append(f"{'='*60}")
                    
                    QMessageBox.critical(self, "失败", f"任务流执行失败：\n\n{error}\n\n请查看日志了解详情")
                
                # 刷新列表
                self.load_workflows()
                
                # 自动滚动到底部
                scrollbar = self.workflow_log_browser.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
            
            self.workflow_thread.finished.connect(on_finished)
            self.workflow_thread.start()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"执行失败: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def view_workflow_details(self, workflow_id):
        """查看任务流详情"""
        try:
            from core.workflow_engine import get_workflow_engine
            
            engine = get_workflow_engine()
            info = engine.get_workflow_info(workflow_id)
            
            if not info:
                QMessageBox.critical(self, "错误", "任务流不存在")
                return
            
            # 构建详情文本
            history = info.get('history', {})
            params = info.get('params', {})
            
            details = f"""
<h2>{info['name']}</h2>
<p><b>描述:</b> {info['description']}</p>

<h3>📊 执行统计</h3>
<ul>
<li><b>总运行次数:</b> {history.get('total_runs', 0)}</li>
<li><b>成功次数:</b> {history.get('success_count', 0)}</li>
<li><b>失败次数:</b> {history.get('failure_count', 0)}</li>
<li><b>最近执行:</b> {history.get('last_run', '从未执行')}</li>
<li><b>最近状态:</b> {history.get('last_status', '-')}</li>
<li><b>最近耗时:</b> {(history.get('last_duration') or 0):.1f}秒</li>
</ul>

<h3>⚙️ 执行步骤</h3>
<ol>
<li><b>Step 1: 爬取新闻</b><br>
    说明: 使用爬虫获取最新新闻，支持自动停止<br>
    参数: 滚动{params.get('crawler', {}).get('scroll_times', 36)}次，等待{params.get('crawler', {}).get('wait_seconds', 6)}秒，自动停止={params.get('crawler', {}).get('auto_stop', True)}</li>
<li><b>Step 2: 清洗数据</b><br>
    说明: 使用AI过滤无关新闻，保留重要信息<br>
    参数: AI服务商={params.get('cleaner', {}).get('ai_provider', 'deepseek')}，批量大小={params.get('cleaner', {}).get('batch_size', 100)}条</li>
<li><b>Step 3: 导出数据</b><br>
    说明: 将清洗后的数据导出为Markdown格式<br>
    参数: 数据源={params.get('export', {}).get('source', 'cleaned')}，格式={params.get('export', {}).get('format', 'markdown')}，时间范围={params.get('export', {}).get('time_range_hours', 24)}小时</li>
<li><b>Step 4: AI分析</b><br>
    说明: 使用短线模板分析新闻，结合盘后总结<br>
    参数: 分析模板={params.get('analyzer', {}).get('template', 'short_term')}</li>
</ol>
            """
            
            # 显示详情对话框
            dialog = QDialog(self)
            dialog.setWindowTitle("任务流详情")
            dialog.resize(600, 500)
            
            layout = QVBoxLayout(dialog)
            
            text_browser = QTextEdit()
            text_browser.setReadOnly(True)
            text_browser.setHtml(details)
            layout.addWidget(text_browser)
            
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dialog.close)
            layout.addWidget(close_btn)
            
            dialog.exec_()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"查看详情失败: {str(e)}")
    
    def view_workflow_logs(self, workflow_id):
        """查看任务流日志"""
        try:
            from core.workflow_engine import get_workflow_engine
            
            engine = get_workflow_engine()
            log_files = engine.get_workflow_logs(workflow_id, limit=10)
            
            if not log_files:
                QMessageBox.information(self, "提示", "暂无执行日志")
                return
            
            # 读取最新的日志
            latest_log = log_files[0]
            log_content = engine.read_log_file(latest_log)
            
            # 显示日志对话框
            dialog = QDialog(self)
            dialog.setWindowTitle(f"任务流日志 - {os.path.basename(latest_log)}")
            dialog.resize(800, 600)
            
            layout = QVBoxLayout(dialog)
            
            # 日志列表选择
            if len(log_files) > 1:
                log_selector_layout = QHBoxLayout()
                log_selector_layout.addWidget(QLabel("选择日志:"))
                
                from PyQt5.QtWidgets import QComboBox
                log_combo = QComboBox()
                for log_file in log_files:
                    log_combo.addItem(os.path.basename(log_file), log_file)
                
                def on_log_changed(index):
                    selected_log = log_combo.itemData(index)
                    new_content = engine.read_log_file(selected_log)
                    text_browser.setPlainText(new_content)
                
                log_combo.currentIndexChanged.connect(on_log_changed)
                log_selector_layout.addWidget(log_combo)
                log_selector_layout.addStretch()
                
                layout.addLayout(log_selector_layout)
            
            # 日志内容
            text_browser = QTextEdit()
            text_browser.setReadOnly(True)
            text_browser.setPlainText(log_content)
            text_browser.setStyleSheet("font-family: 'Consolas', 'Monaco', monospace; font-size: 12px;")
            layout.addWidget(text_browser)
            
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dialog.close)
            layout.addWidget(close_btn)
            
            dialog.exec_()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"查看日志失败: {str(e)}")
    
    def load_tasks(self):
        """加载任务列表"""
        self.task_table.setRowCount(0)
        
        if not os.path.exists(self.tasks_file):
            return
        
        try:
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            
            for task in tasks:
                self.add_task_row(task)
                
        except Exception as e:
            print(f"加载任务失败: {e}")
    
    def add_task_row(self, task):
        """添加任务行"""
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)
        
        # 任务名称
        self.task_table.setItem(row, 0, QTableWidgetItem(task.get('name', '')))
        
        # 执行时间
        self.task_table.setItem(row, 1, QTableWidgetItem(task.get('time', '')))
        
        # 状态
        status = "启用" if task.get('enabled', True) else "禁用"
        self.task_table.setItem(row, 2, QTableWidgetItem(status))
        
        # 上次执行
        last_run = task.get('last_run', '从未执行')
        self.task_table.setItem(row, 3, QTableWidgetItem(last_run))
        
        # 操作按钮
        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(5, 2, 5, 2)
        btn_layout.setSpacing(5)
        
        toggle_btn = QPushButton("禁用" if task.get('enabled', True) else "启用")
        toggle_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #d9d9d9; border-radius: 3px;")
        toggle_btn.clicked.connect(lambda: self.toggle_task(row))
        btn_layout.addWidget(toggle_btn)
        
        delete_btn = QPushButton("删除")
        delete_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #ff4d4f; color: #ff4d4f; border-radius: 3px;")
        delete_btn.clicked.connect(lambda: self.delete_task(row))
        btn_layout.addWidget(delete_btn)
        
        self.task_table.setCellWidget(row, 4, btn_widget)
    
    def add_task(self):
        """添加新任务"""
        dialog = TaskDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            task = dialog.get_task()
            self.save_task(task)
            self.load_tasks()
    
    def save_task(self, new_task):
        """保存任务"""
        # 确保目录存在
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        
        tasks = []
        if os.path.exists(self.tasks_file):
            try:
                with open(self.tasks_file, 'r', encoding='utf-8') as f:
                    tasks = json.load(f)
            except:
                pass
        
        # 为新任务分配唯一ID
        if 'id' not in new_task:
            new_task['id'] = str(uuid.uuid4())
        
        tasks.append(new_task)
        
        with open(self.tasks_file, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        
        # 重新加载调度器任务
        if self.scheduler_service:
            self.scheduler_service.reload_tasks()
    
    def toggle_task(self, row):
        """启用/禁用任务"""
        try:
            if not os.path.exists(self.tasks_file):
                return
            
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            
            if row < len(tasks):
                # 切换状态
                tasks[row]['enabled'] = not tasks[row].get('enabled', True)
                
                # 保存
                with open(self.tasks_file, 'w', encoding='utf-8') as f:
                    json.dump(tasks, f, ensure_ascii=False, indent=2)
                
                # 重新加载调度器
                if self.scheduler_service:
                    self.scheduler_service.reload_tasks()
                
                status = "已启用" if tasks[row]['enabled'] else "已禁用"
                QMessageBox.information(self, "成功", f"任务{status}")
                self.load_tasks()
                
        except Exception as e:
            QMessageBox.critical(self, "错误", f"操作失败: {str(e)}")
    
    def delete_task(self, row):
        """删除任务"""
        reply = QMessageBox.question(
            self, '确认删除',
            "确定要删除这个任务吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                if not os.path.exists(self.tasks_file):
                    return
                
                with open(self.tasks_file, 'r', encoding='utf-8') as f:
                    tasks = json.load(f)
                
                if row < len(tasks):
                    # 删除任务
                    del tasks[row]
                    
                    # 保存
                    with open(self.tasks_file, 'w', encoding='utf-8') as f:
                        json.dump(tasks, f, ensure_ascii=False, indent=2)
                    
                    # 重新加载调度器
                    if self.scheduler_service:
                        self.scheduler_service.reload_tasks()
                    
                    QMessageBox.information(self, "成功", "任务已删除")
                    self.load_tasks()
                    
            except Exception as e:
                QMessageBox.critical(self, "错误", f"删除失败: {str(e)}")


class TaskDialog(QDialog):
    """任务编辑对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        
    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("新建定时任务")
        self.resize(400, 300)
        
        layout = QVBoxLayout(self)
        
        # 表单
        form = QFormLayout()
        
        # 任务名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：每日爬取")
        form.addRow("任务名称:", self.name_edit)
        
        # 执行时间
        self.time_edit = QTimeEdit()
        self.time_edit.setTime(QTime(9, 0))
        form.addRow("执行时间:", self.time_edit)
        
        # 滚动次数
        self.scroll_spin = QSpinBox()
        self.scroll_spin.setRange(1, 100)
        self.scroll_spin.setValue(36)
        form.addRow("滚动次数:", self.scroll_spin)
        
        # 等待时间
        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(1, 60)
        self.wait_spin.setValue(6)
        form.addRow("等待时间:", self.wait_spin)
        
        # 启用状态
        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(True)
        form.addRow("启用:", self.enabled_check)
        
        layout.addLayout(form)
        
        # 按钮
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)
    
    def get_task(self):
        """获取任务信息"""
        return {
            'name': self.name_edit.text() or "未命名任务",
            'time': self.time_edit.time().toString("HH:mm"),
            'scroll_times': self.scroll_spin.value(),
            'wait_seconds': self.wait_spin.value(),
            'enabled': self.enabled_check.isChecked(),
            'last_run': '从未执行'
        }


class WorkflowExecuteDialog(QDialog):
    """任务流执行对话框"""
    
    def __init__(self, workflow_info, parent=None):
        super().__init__(parent)
        self.workflow_info = workflow_info
        self.params = workflow_info['params'].copy()
        self.init_ui()
    
    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle(f"执行: {self.workflow_info['name']}")
        self.resize(500, 600)
        
        layout = QVBoxLayout(self)
        
        # 说明
        desc_label = QLabel(f"<b>{self.workflow_info['description']}</b>")
        layout.addWidget(desc_label)
        
        # 参数配置（使用QTabWidget分组）
        params_tab = QTabWidget()
        
        # 爬虫参数
        crawler_widget = QWidget()
        crawler_layout = QFormLayout(crawler_widget)
        
        self.scroll_spin = QSpinBox()
        self.scroll_spin.setRange(1, 100)
        self.scroll_spin.setValue(self.params['crawler'].get('scroll_times', 36))
        crawler_layout.addRow("滚动次数:", self.scroll_spin)
        
        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(1, 60)
        self.wait_spin.setValue(self.params['crawler'].get('wait_seconds', 6))
        crawler_layout.addRow("等待秒数:", self.wait_spin)
        
        self.auto_stop_check = QCheckBox()
        self.auto_stop_check.setChecked(self.params['crawler'].get('auto_stop', True))
        crawler_layout.addRow("自动停止:", self.auto_stop_check)
        
        params_tab.addTab(crawler_widget, "爬虫参数")
        
        # 清洗参数
        cleaner_widget = QWidget()
        cleaner_layout = QFormLayout(cleaner_widget)
        
        self.ai_provider_combo = QLineEdit()
        self.ai_provider_combo.setText(self.params['cleaner'].get('ai_provider', 'deepseek'))
        cleaner_layout.addRow("AI服务商:", self.ai_provider_combo)
        
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(100, 1000)
        self.batch_spin.setValue(self.params['cleaner'].get('batch_size', 100))
        cleaner_layout.addRow("批量大小:", self.batch_spin)
        
        params_tab.addTab(cleaner_widget, "清洗参数")
        
        # 导出参数
        export_widget = QWidget()
        export_layout = QFormLayout(export_widget)
        
        from PyQt5.QtWidgets import QComboBox
        self.export_source_combo = QComboBox()
        self.export_source_combo.addItems(["cleaned", "raw"])
        current_source = self.params['export'].get('source', 'cleaned')
        self.export_source_combo.setCurrentText(current_source)
        export_layout.addRow("数据源:", self.export_source_combo)
        
        self.export_format_combo = QComboBox()
        self.export_format_combo.addItems(["markdown", "json"])
        current_format = self.params['export'].get('format', 'markdown')
        self.export_format_combo.setCurrentText(current_format)
        export_layout.addRow("导出格式:", self.export_format_combo)
        
        self.time_range_spin = QSpinBox()
        self.time_range_spin.setRange(1, 168)  # 1小时到7天
        self.time_range_spin.setValue(self.params['export'].get('time_range_hours', 24))
        self.time_range_spin.setSuffix(" 小时")
        export_layout.addRow("时间范围:", self.time_range_spin)
        
        params_tab.addTab(export_widget, "导出参数")
        
        # 分析参数
        analyzer_widget = QWidget()
        analyzer_layout = QFormLayout(analyzer_widget)
        
        self.template_combo = QLineEdit()
        self.template_combo.setText(self.params['analyzer'].get('template', 'short_term'))
        analyzer_layout.addRow("分析模板:", self.template_combo)
        
        # 盘后总结
        summary_group = QGroupBox("盘后总结")
        summary_layout = QVBoxLayout()
        
        self.summary_auto_radio = QCheckBox("自动读取前日盘后总结")
        self.summary_auto_radio.setChecked(self.params['analyzer'].get('summary_mode', 'auto') == 'auto')
        summary_layout.addWidget(self.summary_auto_radio)
        
        manual_layout = QHBoxLayout()
        manual_layout.addWidget(QLabel("手动指定:"))
        self.summary_file_edit = QLineEdit()
        self.summary_file_edit.setPlaceholderText("留空则不使用盘后总结")
        manual_layout.addWidget(self.summary_file_edit)
        from PyQt5.QtWidgets import QFileDialog
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self.browse_summary_file)
        manual_layout.addWidget(browse_btn)
        summary_layout.addLayout(manual_layout)
        
        summary_group.setLayout(summary_layout)
        analyzer_layout.addRow(summary_group)
        
        params_tab.addTab(analyzer_widget, "分析参数")
        
        layout.addWidget(params_tab)
        
        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        default_btn = QPushButton("使用默认配置")
        default_btn.clicked.connect(self.use_defaults)
        btn_layout.addWidget(default_btn)
        
        execute_btn = QPushButton("立即执行")
        execute_btn.setStyleSheet(BUTTON_PRIMARY)
        execute_btn.clicked.connect(self.accept)
        btn_layout.addWidget(execute_btn)
        
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
    
    def browse_summary_file(self):
        """浏览盘后总结文件"""
        from PyQt5.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择盘后总结文件",
            "盘后总结",
            "Markdown Files (*.md);;All Files (*)"
        )
        if file_path:
            self.summary_file_edit.setText(file_path)
            self.summary_auto_radio.setChecked(False)
    
    def use_defaults(self):
        """使用默认配置"""
        # 爬虫参数
        self.scroll_spin.setValue(36)
        self.wait_spin.setValue(6)
        self.auto_stop_check.setChecked(True)
        # 清洗参数
        self.ai_provider_combo.setText('deepseek')
        self.batch_spin.setValue(100)
        # 导出参数
        self.export_source_combo.setCurrentText('cleaned')
        self.export_format_combo.setCurrentText('markdown')
        self.time_range_spin.setValue(24)
        # 分析参数
        self.template_combo.setText('short_term')
        self.summary_auto_radio.setChecked(True)
        self.summary_file_edit.clear()
    
    def get_params(self):
        """获取参数"""
        return {
            'crawler': {
                'scroll_times': self.scroll_spin.value(),
                'wait_seconds': self.wait_spin.value(),
                'auto_stop': self.auto_stop_check.isChecked()
            },
            'cleaner': {
                'ai_provider': self.ai_provider_combo.text(),
                'batch_size': self.batch_spin.value()
            },
            'export': {
                'source': self.export_source_combo.currentText(),
                'format': self.export_format_combo.currentText(),
                'time_range_hours': self.time_range_spin.value()
            },
            'analyzer': {
                'template': self.template_combo.text(),
                'summary_mode': 'auto' if self.summary_auto_radio.isChecked() else 'manual',
                'summary_file': self.summary_file_edit.text()
            }
        }







