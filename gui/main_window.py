"""
主窗口
包含侧边栏导航和内容区域
"""

from PyQt5.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                             QPushButton, QStackedWidget, QLabel, QSizePolicy,
                             QMessageBox)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QIcon
import sys
import os

from gui.utils.styles import *
from gui.pages.crawler_page import CrawlerPage
from gui.pages.data_page import DataPage
from gui.pages.export_page import ExportPage
from gui.pages.ai_analysis_page import AIAnalysisPage
from gui.pages.theme_prediction_page import ThemePredictionPage
from gui.pages.schedule_page import SchedulePage
from gui.pages.news_cleaning_page import NewsCleaningPage
from gui.pages.prompt_manager_page import PromptManagerPage
from core.scheduler_service import SchedulerService


class MainWindow(QMainWindow):
    """主窗口类"""

    def __init__(self):
        super().__init__()
        # 设置窗口图标
        self.set_window_icon()
        # 创建定时任务调度服务
        self.scheduler_service = SchedulerService()
        self.scheduler_service.set_task_callback(self.on_scheduled_task)
        self.init_ui()
        # 启动定时任务服务
        self.scheduler_service.start()
    
    def set_window_icon(self):
        """设置窗口图标（兼容打包后的exe）"""
        # 智能获取图标路径
        if getattr(sys, 'frozen', False):
            # 打包后的程序
            exe_dir = os.path.dirname(sys.executable)
            icon_path = os.path.join(exe_dir, '图标.ico')
        else:
            # 开发环境
            icon_path = os.path.join(os.getcwd(), '图标.ico')
        
        # 设置图标
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            print(f"⚠ 图标文件未找到: {icon_path}")

    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("新闻爬虫管理系统")
        self.setMinimumSize(1200, 800)
        self.resize(1280, 900)

        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 创建侧边栏
        sidebar = self.create_sidebar()
        main_layout.addWidget(sidebar)

        # 创建内容区域
        self.content_stack = QStackedWidget()
        self.content_stack.setStyleSheet("background-color: #f0f2f5;")
        main_layout.addWidget(self.content_stack)

        # 添加各个页面
        self.pages = {
            'crawler': CrawlerPage(),
            'data': DataPage(),
            'export': ExportPage(),
            'ai_analysis': AIAnalysisPage(),
            'theme_prediction': ThemePredictionPage(),
            'cleaning': NewsCleaningPage(),
            'schedule': SchedulePage(scheduler_service=self.scheduler_service),
            'prompt_manager': PromptManagerPage(),
        }

        for page in self.pages.values():
            self.content_stack.addWidget(page)

        # 连接"提示词管理"页面信号 -> 让依赖方刷新模板下拉
        prompt_mgr = self.pages.get('prompt_manager')
        if prompt_mgr is not None:
            prompt_mgr.prompts_changed.connect(self._on_prompts_changed)

        # 默认显示爬虫页面
        self.show_page('crawler')

        # 应用样式
        self.setStyleSheet(MAIN_WINDOW_STYLE)

    def create_sidebar(self):
        """创建侧边栏"""
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet(SIDEBAR_STYLE)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Logo/标题
        title = QLabel("爬虫系统")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # 导航按钮
        nav_items = [
            ('crawler', '🕷️ 爬虫管理'),
            ('data', '📁 数据管理'),
            ('cleaning', '🧹 新闻清洗'),
#            ('analysis', '📊 新闻分析（弃用）'),
            ('export', '📤 数据导出'),
            ('ai_analysis', '🤖 AI分析'),
            ('theme_prediction', '🎯 预测题材'),
            ('schedule', '⏰ 定时任务'),
            ('prompt_manager', '📝 提示词管理'),
        ]

        self.nav_buttons = {}
        for page_id, text in nav_items:
            btn = QPushButton(text)
            btn.clicked.connect(lambda checked, pid=page_id: self.show_page(pid))
            btn.setCheckable(True)
            layout.addWidget(btn)
            self.nav_buttons[page_id] = btn

        layout.addStretch()

        # 版本信息
        version = QLabel("v2.0")
        version.setAlignment(Qt.AlignCenter)
        version.setStyleSheet("color: #8c8c8c; padding: 10px;")
        layout.addWidget(version)

        return sidebar

    def show_page(self, page_id):
        """显示指定页面"""
        if page_id in self.pages:
            page = self.pages[page_id]
            self.content_stack.setCurrentWidget(page)

            # 更新导航按钮状态
            for pid, btn in self.nav_buttons.items():
                btn.setChecked(pid == page_id)

            # 如果页面有refresh方法，调用它
            if hasattr(page, 'refresh'):
                page.refresh()

    def _on_prompts_changed(self, category: str) -> None:
        """提示词管理页保存/删除后，刷新依赖该 category 的页面。"""
        if category == "analysis":
            ai_page = self.pages.get("ai_analysis")
            if ai_page is not None and hasattr(ai_page, "load_template_list"):
                try:
                    ai_page.load_template_list()
                except Exception as e:
                    print(f"[MainWindow] 刷新 AI 分析模板列表失败: {e}")
    
    def on_scheduled_task(self, task):
        """定时任务触发：按 type 分发到 services 或 GUI"""
        from services.scheduled_runner import normalize_task, run_task_async

        task = normalize_task(task)
        task_type = task.get("type", "crawl_sync")

        if task_type == "crawl_sync":
            crawler_page = self.pages.get("crawler")
            if crawler_page:
                params = task.get("params") or {}
                from PyQt5.QtCore import QMetaObject, Q_ARG, Qt
                QMetaObject.invokeMethod(
                    crawler_page,
                    "start_crawler_from_schedule",
                    Qt.QueuedConnection,
                    Q_ARG(int, params.get("scroll_times", 36)),
                    Q_ARG(int, params.get("wait_seconds", 6)),
                    Q_ARG(int, params.get("max_no_change", 3)),
                )
            return

        def _on_done(outcome):
            name = task.get("name", "任务")
            if outcome.get("ok"):
                print(f"[定时任务] {name} 完成: {outcome.get('result')}")
            else:
                print(f"[定时任务] {name} 失败: {outcome.get('error')}")

        run_task_async(task, on_complete=_on_done)
    
    def closeEvent(self, event):
        """窗口关闭事件"""
        # 停止定时任务服务
        if hasattr(self, 'scheduler_service'):
            self.scheduler_service.stop()
        event.accept()
