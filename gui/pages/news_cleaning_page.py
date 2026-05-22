"""
新闻清洗页面
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QListWidget, QComboBox,
                             QSpinBox, QTextBrowser, QFileDialog, QMessageBox,
                             QProgressBar, QListWidgetItem, QCheckBox,
                             QAbstractItemView)
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QTextCursor
from datetime import datetime
import os
import json

from gui.utils.styles import (BUTTON_PRIMARY, BUTTON_SUCCESS, BUTTON_DANGER,
                              COMBOBOX_STYLE, INPUT_STYLE, TEXTBROWSER_STYLE)
from gui.workers.news_cleaning_worker import NewsCleaningWorker


class NewsCleaningPage(QWidget):
    """新闻清洗页面"""
    
    def __init__(self):
        super().__init__()
        self.worker = None
        self.selected_files = []
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
        group = QGroupBox("📂 数据源选择")
        layout = QVBoxLayout()
        
        # 数据类型选择
        type_layout = QHBoxLayout()
        type_label = QLabel("数据类型:")
        type_label.setMinimumWidth(80)
        self.type_combo = QComboBox()
        self.type_combo.addItems(["原始数据", "导出数据"])
        self.type_combo.setStyleSheet(COMBOBOX_STYLE)
        type_layout.addWidget(type_label)
        type_layout.addWidget(self.type_combo)
        type_layout.addStretch()
        layout.addLayout(type_layout)
        
        # 文件选择
        btn_layout = QHBoxLayout()
        self.select_files_btn = QPushButton("📁 选择文件...")
        self.select_files_btn.clicked.connect(self.select_files)
        self.clear_files_btn = QPushButton("🗑️ 清空")
        self.clear_files_btn.clicked.connect(self.clear_files)
        btn_layout.addWidget(self.select_files_btn)
        btn_layout.addWidget(self.clear_files_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # 文件列表
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(150)
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.file_list)
        
        # 自动合并去重选项
        self.auto_merge_check = QCheckBox("☑ 自动合并去重")
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
        self.view_kept_btn = QPushButton("📄 查看保留新闻")
        self.view_kept_btn.setStyleSheet(BUTTON_SUCCESS)
        self.view_kept_btn.setEnabled(False)
        self.view_kept_btn.clicked.connect(self.view_kept_news)
        
        self.view_removed_btn = QPushButton("📄 查看去除新闻")
        self.view_removed_btn.setEnabled(False)
        self.view_removed_btn.clicked.connect(self.view_removed_news)
        
        self.use_for_analysis_btn = QPushButton("🤖 用于AI分析")
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
    
    def select_files(self):
        """选择文件"""
        data_type = self.type_combo.currentText()
        
        if data_type == "原始数据":
            default_dir = os.path.join(os.getcwd(), 'data', 'raw')
        else:
            default_dir = os.path.join(os.getcwd(), 'data', 'exports')
        
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择文件（可多选）",
            default_dir,
            "JSON文件 (*.json);;所有文件 (*.*)"
        )
        
        if file_paths:
            for file_path in file_paths:
                # 避免重复添加
                if file_path not in self.selected_files:
                    self.selected_files.append(file_path)
                    # 显示文件名和大小
                    filename = os.path.basename(file_path)
                    filesize = os.path.getsize(file_path) / (1024 * 1024)  # MB
                    item_text = f"{filename} ({filesize:.1f}MB)"
                    self.file_list.addItem(item_text)
    
    def clear_files(self):
        """清空文件列表"""
        self.selected_files.clear()
        self.file_list.clear()
    
    def start_cleaning(self):
        """开始清洗"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "警告", "清洗正在进行中！")
            return
        
        # 检查是否选择了文件
        if not self.selected_files:
            QMessageBox.warning(self, "警告", "请先选择要清洗的文件")
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
        self.select_files_btn.setEnabled(False)
        
        # 清空显示
        self.progress_browser.clear()
        self.result_browser.clear()
        self.progress_bar.setValue(0)
        
        # 添加日志
        self.add_progress("=" * 60)
        self.add_progress(f"开始清洗 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.add_progress("=" * 60)
        self.add_progress(f"文件数量: {len(self.selected_files)}")
        self.add_progress(f"AI服务商: {current_provider}")
        self.add_progress(f"每批数量: {self.batch_spin.value()} 条")
        
        # 创建工作线程
        self.worker = NewsCleaningWorker(
            file_paths=self.selected_files,
            criteria=self.current_criteria,
            ai_provider=current_provider,
            batch_size=self.batch_spin.value(),
            auto_merge=self.auto_merge_check.isChecked()
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
        
        statistics = result['statistics']
        
        # 显示结果
        result_text = f"""
清洗完成！

📊 统计信息
────────────────────────
原始数量: {statistics['source_count']} 条
已保留:   {statistics['kept_count']} 条 ({statistics['kept_percent']}%)
已去除:   {statistics['removed_count']} 条 ({statistics['removed_percent']}%)

💾 保存文件
────────────────────────
已保留: {os.path.basename(result['kept_file'])}
已去除: {os.path.basename(result['removed_file'])}

文件位置: data/cleaned/
        """.strip()
        
        self.result_browser.setPlainText(result_text)
        
        # 保存文件路径供后续使用
        self.last_kept_file = result['kept_file']
        self.last_removed_file = result['removed_file']
        
        # 启用按钮
        self.clean_btn.setEnabled(True)
        self.select_files_btn.setEnabled(True)
        self.view_kept_btn.setEnabled(True)
        self.view_removed_btn.setEnabled(True)
        self.use_for_analysis_btn.setEnabled(True)
    
    @pyqtSlot(str)
    def on_cleaning_error(self, error_msg):
        """清洗失败"""
        self.add_progress("=" * 60)
        self.add_progress(f"❌ 清洗失败: {error_msg}")
        self.add_progress("=" * 60)
        
        self.clean_btn.setEnabled(True)
        self.select_files_btn.setEnabled(True)
        
        QMessageBox.critical(self, "错误", error_msg)
    
    def add_progress(self, message):
        """添加进度信息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_browser.append(f"[{timestamp}] {message}")
        self.progress_browser.moveCursor(QTextCursor.End)
    
    def view_kept_news(self):
        """查看保留的新闻"""
        if hasattr(self, 'last_kept_file') and os.path.exists(self.last_kept_file):
            os.startfile(self.last_kept_file)
        else:
            QMessageBox.warning(self, "提示", "保留文件不存在")
    
    def view_removed_news(self):
        """查看去除的新闻"""
        if hasattr(self, 'last_removed_file') and os.path.exists(self.last_removed_file):
            os.startfile(self.last_removed_file)
        else:
            QMessageBox.warning(self, "提示", "去除文件不存在")
    
    def use_for_analysis(self):
        """将清洗后的数据用于AI分析"""
        if hasattr(self, 'last_kept_file') and os.path.exists(self.last_kept_file):
            # 切换到AI分析页面并自动填充文件路径
            try:
                main_window = self.window()
                if hasattr(main_window, 'switch_to_ai_analysis'):
                    main_window.switch_to_ai_analysis(self.last_kept_file)
                else:
                    QMessageBox.information(
                        self, "提示",
                        f"清洗文件已保存：\n{self.last_kept_file}\n\n"
                        "请前往【AI分析】页面，选择此文件进行分析"
                    )
            except Exception as e:
                QMessageBox.warning(self, "提示", f"跳转失败: {e}")
        else:
            QMessageBox.warning(self, "提示", "保留文件不存在")

