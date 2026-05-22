"""
新闻清洗页面
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QSpinBox, QTextBrowser,
                             QMessageBox, QProgressBar, QCheckBox)
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QTextCursor
from datetime import datetime
import os
import json

from gui.utils.styles import (BUTTON_PRIMARY, BUTTON_SUCCESS,
                              INPUT_STYLE, TEXTBROWSER_STYLE)
from gui.workers.news_cleaning_worker import NewsCleaningWorker


class NewsCleaningPage(QWidget):
    """新闻清洗页面"""
    
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
        title = QLabel("🧹 新闻清洗")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)
        
        # 数据源选择
        source_group = self.create_source_group()
        layout.addWidget(source_group)
        
        # 清洗配置
        config_group = self.create_config_group()
        layout.addWidget(config_group)
        
        # 清洗按钮
        self.clean_btn = QPushButton("🚀 开始清洗")
        self.clean_btn.setStyleSheet(BUTTON_PRIMARY)
        self.clean_btn.setMinimumHeight(45)
        self.clean_btn.clicked.connect(self.start_cleaning)
        layout.addWidget(self.clean_btn)
        
        # 进度显示
        progress_group = self.create_progress_group()
        layout.addWidget(progress_group)
        
        # 结果显示
        result_group = self.create_result_group()
        layout.addWidget(result_group)
        
        layout.addStretch()
    
    def create_source_group(self):
        """创建数据源选择组"""
        group = QGroupBox("📂 数据源（SQLite 原始库）")
        layout = QVBoxLayout()

        sqlite_row = QHBoxLayout()
        sqlite_row.addWidget(QLabel("最多处理:"))
        self.sqlite_limit_spin = QSpinBox()
        self.sqlite_limit_spin.setRange(10, 2000)
        self.sqlite_limit_spin.setValue(500)
        self.sqlite_limit_spin.setSuffix(" 条")
        sqlite_row.addWidget(self.sqlite_limit_spin)
        self.uncleaned_label = QLabel("")
        self.uncleaned_label.setStyleSheet("color: #8c8c8c;")
        sqlite_row.addWidget(self.uncleaned_label)
        sqlite_row.addStretch()
        layout.addLayout(sqlite_row)

        self.auto_merge_check = QCheckBox("自动合并去重（批次内）")
        self.auto_merge_check.setChecked(True)
        layout.addWidget(self.auto_merge_check)

        group.setLayout(layout)
        return group
    
    def create_config_group(self):
        """创建清洗配置组"""
        group = QGroupBox("⚙️ 清洗配置")
        layout = QVBoxLayout()
        
        # AI服务商（从已保存的配置读取）
        ai_layout = QHBoxLayout()
        ai_label = QLabel("AI服务商:")
        ai_label.setMinimumWidth(80)
        self.ai_provider_label = QLabel()
        self.ai_provider_label.setStyleSheet("color: #1890ff; font-weight: bold;")
        ai_layout.addWidget(ai_label)
        ai_layout.addWidget(self.ai_provider_label)
        ai_layout.addStretch()
        layout.addLayout(ai_layout)
        
        # 每批数量
        batch_layout = QHBoxLayout()
        batch_label = QLabel("每批数量:")
        batch_label.setMinimumWidth(80)
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(100, 1000)
        self.batch_spin.setValue(100)
        self.batch_spin.setSingleStep(100)
        self.batch_spin.setSuffix(" 条")
        self.batch_spin.setStyleSheet(INPUT_STYLE)
        self.batch_spin.setToolTip("每批发送给AI的新闻数量，建议100条")
        batch_layout.addWidget(batch_label)
        batch_layout.addWidget(self.batch_spin)
        batch_layout.addStretch()
        layout.addLayout(batch_layout)
        
        # 清洗标准说明
        criteria_label = QLabel("📋 清洗标准:")
        layout.addWidget(criteria_label)
        
        self.criteria_text = QTextBrowser()
        self.criteria_text.setMinimumHeight(400)  # 设置足够的最小高度
        # 不设置最大高度限制，让它完全显示所有内容
        self.criteria_text.setStyleSheet(TEXTBROWSER_STYLE)
        layout.addWidget(self.criteria_text)
        
        group.setLayout(layout)
        return group
    
    def create_progress_group(self):
        """创建进度显示组"""
        group = QGroupBox("📊 清洗进度")
        layout = QVBoxLayout()
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p% (%v/%m)")
        layout.addWidget(self.progress_bar)
        
        # 进度信息
        self.progress_browser = QTextBrowser()
        self.progress_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.progress_browser.setMaximumHeight(150)
        layout.addWidget(self.progress_browser)
        
        group.setLayout(layout)
        return group
    
    def create_result_group(self):
        """创建结果显示组"""
        group = QGroupBox("✅ 清洗结果")
        layout = QVBoxLayout()
        
        self.result_browser = QTextBrowser()
        self.result_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.result_browser.setMinimumHeight(150)
        layout.addWidget(self.result_browser)
        
        # 操作按钮
        btn_layout = QHBoxLayout()
        self.view_kept_btn = QPushButton("📄 查看精选库")
        self.view_kept_btn.setStyleSheet(BUTTON_SUCCESS)
        self.view_kept_btn.setEnabled(False)
        self.view_kept_btn.clicked.connect(self.view_kept_news)

        self.view_removed_btn = QPushButton("📄 查看剔除记录")
        self.view_removed_btn.setEnabled(False)
        self.view_removed_btn.clicked.connect(self.view_removed_news)

        self.use_for_analysis_btn = QPushButton("🤖 用于 AI 分析")
        self.use_for_analysis_btn.setStyleSheet(BUTTON_PRIMARY)
        self.use_for_analysis_btn.setEnabled(False)
        self.use_for_analysis_btn.clicked.connect(self.use_for_analysis)
        
        btn_layout.addWidget(self.view_kept_btn)
        btn_layout.addWidget(self.view_removed_btn)
        btn_layout.addWidget(self.use_for_analysis_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        group.setLayout(layout)
        return group
    
    def showEvent(self, event):
        """页面显示时加载配置"""
        super().showEvent(event)
        self.load_config()
        self._refresh_uncleaned_count()

    def _refresh_uncleaned_count(self):
        try:
            from services.storage import get_raw_store
            n = get_raw_store().count_uncleaned()
            self.uncleaned_label.setText(f"当前待清洗: {n} 条")
        except Exception as e:
            self.uncleaned_label.setText(str(e))
    
    def load_config(self):
        """加载配置（使用AI分析中保存的配置）"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            
            # 获取当前AI服务商
            current_provider = config.get_current_provider()
            provider_names = {
                'openai': 'OpenAI',
                'deepseek': 'DeepSeek',
                'zhipu': '智谱AI',
                'qwen': '通义千问',
                'volcengine': '火山引擎'
            }
            provider_display = provider_names.get(current_provider, current_provider)
            
            # 检查是否配置了API Key
            api_key = config.get_api_key(current_provider)
            if api_key:
                self.ai_provider_label.setText(f"{provider_display} (已配置)")
            else:
                self.ai_provider_label.setText(f"{provider_display} (未配置API Key)")
                self.ai_provider_label.setStyleSheet("color: #ff4d4f; font-weight: bold;")
            
            # 加载清洗标准
            self.load_criteria()
            
        except Exception as e:
            print(f"加载配置失败: {e}")
            self.ai_provider_label.setText("配置加载失败")
    
    def load_criteria(self):
        """加载清洗标准"""
        try:
            criteria_file = 'config/cleaning_criteria.json'
            if os.path.exists(criteria_file):
                with open(criteria_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    default_criteria = data.get('default', {})
                    criteria_text = default_criteria.get('criteria', '')
                    
                    # 显示完整清洗标准
                    self.criteria_text.setPlainText(criteria_text)
                    
                    # 保存完整标准供后续使用
                    self.current_criteria = criteria_text
            else:
                self.criteria_text.setPlainText("清洗标准文件不存在")
                self.current_criteria = ""
                
        except Exception as e:
            print(f"加载清洗标准失败: {e}")
            self.criteria_text.setPlainText(f"加载失败: {e}")
            self.current_criteria = ""

    def start_cleaning(self):
        """开始清洗"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "警告", "清洗正在进行中！")
            return

        from services.storage import get_raw_store
        if get_raw_store().count_uncleaned() == 0:
            QMessageBox.information(self, "提示", "没有待清洗的原始新闻")
            return
        
        # 检查AI配置
        from core.ai_config import AIConfig
        config = AIConfig()
        current_provider = config.get_current_provider()
        api_key = config.get_api_key(current_provider)
        
        if not api_key:
            QMessageBox.warning(
                self, "警告",
                f"未配置{current_provider}的API Key\n\n请先到【AI分析】页面配置"
            )
            return
        
        # 检查清洗标准
        if not hasattr(self, 'current_criteria') or not self.current_criteria:
            QMessageBox.warning(self, "警告", "清洗标准加载失败")
            return
        
        # 禁用按钮
        self.clean_btn.setEnabled(False)

        # 清空显示
        self.progress_browser.clear()
        self.result_browser.clear()
        self.progress_bar.setValue(0)
        
        # 添加日志
        self.add_progress("=" * 60)
        self.add_progress(f"开始清洗 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.add_progress("=" * 60)
        self.add_progress(f"AI服务商: {current_provider}")
        self.add_progress(f"每批数量: {self.batch_spin.value()} 条")
        self.add_progress(f"数据源: SQLite 原始库（最多 {self.sqlite_limit_spin.value()} 条）")

        self.worker = NewsCleaningWorker(
            criteria=self.current_criteria,
            ai_provider=current_provider,
            batch_size=self.batch_spin.value(),
            auto_merge=self.auto_merge_check.isChecked(),
            sqlite_limit=self.sqlite_limit_spin.value(),
        )
        
        # 连接信号
        self.worker.finished.connect(self.on_cleaning_finished)
        self.worker.error.connect(self.on_cleaning_error)
        self.worker.progress.connect(self.add_progress)
        self.worker.batch_progress.connect(self.on_batch_progress)
        
        # 启动线程
        self.worker.start()
    
    @pyqtSlot(str, int, int, int, int)
    def on_batch_progress(self, batch_info, current, total, kept_count, removed_count):
        """批次进度更新"""
        # 更新进度条
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        
        # 显示批次信息
        self.add_progress(
            f"{batch_info}: 已处理 {current}/{total}，"
            f"本批保留 {kept_count} 条，去除 {removed_count} 条"
        )
    
    @pyqtSlot(dict)
    def on_cleaning_finished(self, result):
        """清洗完成"""
        self.add_progress("=" * 60)
        self.add_progress("✅ 清洗完成！")
        self.add_progress("=" * 60)
        
        statistics = result["statistics"]
        result_text = f"""
清洗完成！（已写入 SQLite）

📊 统计信息
────────────────────────
处理数量: {statistics['source_count']} 条
AI 保留:   {statistics['kept_count']} 条 ({statistics['kept_percent']}%)
AI 剔除:   {statistics['removed_count']} 条 ({statistics['removed_percent']}%)

💾 入库
────────────────────────
精选库新增: {statistics.get('inserted_curated', 0)} 条
剔除记录新增: {statistics.get('inserted_rejected', 0)} 条
        """.strip()
        self.result_browser.setPlainText(result_text)

        self.clean_btn.setEnabled(True)
        self.view_kept_btn.setEnabled(True)
        self.view_removed_btn.setEnabled(True)
        self.use_for_analysis_btn.setEnabled(True)
        self._refresh_uncleaned_count()
    
    @pyqtSlot(str)
    def on_cleaning_error(self, error_msg):
        """清洗失败"""
        self.add_progress("=" * 60)
        self.add_progress(f"❌ 清洗失败: {error_msg}")
        self.add_progress("=" * 60)
        
        self.clean_btn.setEnabled(True)

        QMessageBox.critical(self, "错误", error_msg)

    def add_progress(self, message):
        """添加进度信息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_browser.append(f"[{timestamp}] {message}")
        self.progress_browser.moveCursor(QTextCursor.End)
    
    def view_kept_news(self):
        """提示前往数据管理查看精选库"""
        QMessageBox.information(
            self, "提示", "保留新闻已写入 SQLite 精选库，请前往【数据管理】→ 精选库查看。"
        )

    def view_removed_news(self):
        """提示前往数据管理查看剔除记录"""
        QMessageBox.information(
            self, "提示", "剔除记录已写入 SQLite，请前往【数据管理】→ 剔除记录查看。"
        )

    def use_for_analysis(self):
        """提示前往 AI 分析"""
        QMessageBox.information(
            self, "提示",
            "请前往【AI 分析】页面，选择 SQLite 精选库作为数据源。"
        )

    def refresh(self):
        self._refresh_uncleaned_count()

