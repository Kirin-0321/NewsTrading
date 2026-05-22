"""
AI分析页面
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QComboBox, QLineEdit,
                             QTextBrowser, QFileDialog, QMessageBox,
                             QRadioButton, QButtonGroup)
from PyQt5.QtGui import QTextCursor
from datetime import datetime
import os

from gui.utils.styles import (BUTTON_PRIMARY, BUTTON_SUCCESS, BUTTON_DANGER,
                              COMBOBOX_STYLE, INPUT_STYLE, TEXTBROWSER_STYLE)
from gui.workers.ai_analysis_worker import AIAnalysisWorker
from gui.pages.prompt_template_dialog import PromptTemplateDialog


class AIAnalysisPage(QWidget):
    """AI分析页面"""

    def __init__(self):
        super().__init__()
        self.worker = None
        self.last_report = None
        self.init_ui()

    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # 标题
        title = QLabel("🤖 AI分析")
        title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)

        # AI配置
        config_group = self.create_config_group()
        layout.addWidget(config_group)

        # 数据源选择
        source_group = self.create_source_group()
        layout.addWidget(source_group)

        # 分析参数
        params_group = self.create_params_group()
        layout.addWidget(params_group)

        # 提示词模板
        template_group = self.create_template_group()
        layout.addWidget(template_group)

        # 分析按钮
        self.analyze_btn = QPushButton("🚀 开始分析")
        self.analyze_btn.setStyleSheet(BUTTON_PRIMARY)
        self.analyze_btn.setMinimumHeight(45)
        self.analyze_btn.clicked.connect(self.start_analysis)
        layout.addWidget(self.analyze_btn)

        # 进度显示
        progress_group = self.create_progress_group()
        layout.addWidget(progress_group)

        # 结果显示
        result_group = self.create_result_group()
        layout.addWidget(result_group)

        layout.addStretch()
        
        # 加载配置和模板数据（必须在所有UI组件创建完成后）
        self.load_config()
        self.load_template_list()

    def create_config_group(self):
        """创建AI配置组"""
        group = QGroupBox("⚙️ AI配置")
        layout = QVBoxLayout()

        # AI服务商选择
        provider_layout = QHBoxLayout()
        provider_label = QLabel("AI服务:")
        provider_label.setMinimumWidth(80)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems([
            'OpenAI', 'DeepSeek', '智谱AI', '通义千问', '火山引擎'
        ])
        self.provider_combo.setStyleSheet(COMBOBOX_STYLE)
        self.provider_combo.currentTextChanged.connect(
            self.on_provider_changed)
        provider_layout.addWidget(provider_label)
        provider_layout.addWidget(self.provider_combo)
        layout.addLayout(provider_layout)

        # 模型选择
        model_layout = QHBoxLayout()
        model_label = QLabel("模型:")
        model_label.setMinimumWidth(80)
        self.model_combo = QComboBox()
        self.model_combo.setStyleSheet(COMBOBOX_STYLE)
        self.update_model_list()
        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_combo)
        layout.addLayout(model_layout)

        # API Key
        key_layout = QHBoxLayout()
        key_label = QLabel("API Key:")
        key_label.setMinimumWidth(80)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setStyleSheet(INPUT_STYLE)
        self.api_key_input.setPlaceholderText("请输入API Key")
        self.save_key_btn = QPushButton("💾 保存")
        self.save_key_btn.clicked.connect(self.save_api_key)
        self.test_btn = QPushButton("🔌 测试连接")
        self.test_btn.clicked.connect(self.test_connection)
        key_layout.addWidget(key_label)
        key_layout.addWidget(self.api_key_input, 1)
        key_layout.addWidget(self.save_key_btn)
        key_layout.addWidget(self.test_btn)
        layout.addLayout(key_layout)

        # 加载已保存的配置
        self.load_config()

        group.setLayout(layout)
        return group

    def create_template_group(self):
        """创建提示词模板组"""
        group = QGroupBox("📝 提示词模板")
        layout = QVBoxLayout()

        # 模板选择
        template_layout = QHBoxLayout()
        template_label = QLabel("选择模板:")
        template_label.setMinimumWidth(120)
        self.template_combo = QComboBox()
        self.template_combo.setStyleSheet(COMBOBOX_STYLE)
        self.load_templates()
        template_layout.addWidget(template_label)
        template_layout.addWidget(self.template_combo, 1)
        layout.addLayout(template_layout)

        # 模板管理按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.new_template_btn = QPushButton("➕ 新建")
        self.new_template_btn.clicked.connect(self.new_template)
        btn_layout.addWidget(self.new_template_btn)

        self.edit_template_btn = QPushButton("✏️ 编辑")
        self.edit_template_btn.clicked.connect(self.edit_template)
        btn_layout.addWidget(self.edit_template_btn)

        self.delete_template_btn = QPushButton("🗑️ 删除")
        self.delete_template_btn.setStyleSheet(BUTTON_DANGER)
        self.delete_template_btn.clicked.connect(self.delete_template)
        btn_layout.addWidget(self.delete_template_btn)

        layout.addLayout(btn_layout)

        group.setLayout(layout)
        return group

    def load_templates(self):
        """加载模板列表"""
        from core.ai_config import AIConfig
        config = AIConfig()

        self.template_combo.clear()
        templates = config.get_template_names()

        for template_id, name in templates:
            self.template_combo.addItem(name, template_id)

        # 选中当前模板
        current = config.get_current_template()
        for i in range(self.template_combo.count()):
            if self.template_combo.itemData(i) == current:
                self.template_combo.setCurrentIndex(i)
                break

    def new_template(self):
        """新建模板"""
        from PyQt5.QtWidgets import QDialog
        dialog = PromptTemplateDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            result = dialog.get_result()
            if result:
                from core.ai_config import AIConfig
                import time

                config = AIConfig()
                template_id = f"custom_{int(time.time())}"
                config.save_template(
                    template_id,
                    result['name'],
                    result['system_prompt'],
                    result['user_prompt_template']
                )
                self.load_templates()
                QMessageBox.information(self, "成功", "模板已保存")

    def edit_template(self):
        """编辑模板"""
        from PyQt5.QtWidgets import QDialog
        
        template_id = self.template_combo.currentData()
        if not template_id:
            return

        from core.ai_config import AIConfig
        import time
        
        config = AIConfig()
        template_data = config.get_template(template_id)

        # 内置模板编辑时提示另存为
        is_builtin = template_id in ['default', 'conservative', 'aggressive']
        
        if is_builtin:
            reply = QMessageBox.question(
                self, 
                "编辑内置模板", 
                "内置模板不可直接修改，是否要另存为新模板？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        dialog = PromptTemplateDialog(self, template_id, template_data)
        if dialog.exec_() == QDialog.Accepted:
            result = dialog.get_result()
            if result:
                # 内置模板另存为新模板
                if is_builtin:
                    new_template_id = f"custom_{int(time.time())}"
                    config.save_template(
                        new_template_id,
                        result['name'],
                        result['system_prompt'],
                        result['user_prompt_template']
                    )
                    QMessageBox.information(
                        self, "成功", f"已另存为新模板: {result['name']}")
                else:
                    # 自定义模板直接更新
                    config.save_template(
                        template_id,
                        result['name'],
                        result['system_prompt'],
                        result['user_prompt_template']
                    )
                    QMessageBox.information(self, "成功", "模板已更新")
                
                self.load_templates()

    def delete_template(self):
        """删除模板"""
        template_id = self.template_combo.currentData()
        if not template_id:
            return

        # 内置模板不允许删除
        if template_id in ['default', 'conservative', 'aggressive']:
            QMessageBox.warning(self, "提示", "内置模板不可删除")
            return

        reply = QMessageBox.question(
            self, "确认", "确定要删除这个模板吗？",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            from core.ai_config import AIConfig
            config = AIConfig()
            if config.delete_template(template_id):
                self.load_templates()
                QMessageBox.information(self, "成功", "模板已删除")
            else:
                QMessageBox.warning(self, "失败", "删除失败")

    def create_source_group(self):
        """创建数据源选择组"""
        group = QGroupBox("📂 数据源")
        layout = QVBoxLayout()

        # 单选按钮组
        self.source_group = QButtonGroup()

        # 选项1：使用导出文件
        self.export_radio = QRadioButton("使用导出文件（推荐）")
        self.export_radio.setChecked(True)
        self.source_group.addButton(self.export_radio, 1)
        layout.addWidget(self.export_radio)

        export_layout = QHBoxLayout()
        export_layout.setContentsMargins(30, 0, 0, 0)
        self.export_path_input = QLineEdit()
        self.export_path_input.setStyleSheet(INPUT_STYLE)
        self.export_path_input.setPlaceholderText(
            "选择导出目录中的JSON/MD/TXT文件...")
        self.export_browse_btn = QPushButton("📁 浏览...")
        self.export_browse_btn.clicked.connect(self.browse_export_file)
        export_layout.addWidget(self.export_path_input, 1)
        export_layout.addWidget(self.export_browse_btn)
        layout.addLayout(export_layout)

        # 选项2：使用影响分析结果
        self.impact_radio = QRadioButton("使用影响分析结果（节省token）")
        self.source_group.addButton(self.impact_radio, 2)
        layout.addWidget(self.impact_radio)

        impact_layout = QHBoxLayout()
        impact_layout.setContentsMargins(30, 0, 0, 0)
        self.impact_path_input = QLineEdit()
        self.impact_path_input.setStyleSheet(INPUT_STYLE)
        self.impact_path_input.setPlaceholderText("选择影响分析报告文件...")
        self.impact_browse_btn = QPushButton("📁 浏览...")
        self.impact_browse_btn.clicked.connect(self.browse_impact_file)
        impact_layout.addWidget(self.impact_path_input, 1)
        impact_layout.addWidget(self.impact_browse_btn)
        layout.addLayout(impact_layout)

        group.setLayout(layout)
        return group

    def create_params_group(self):
        """创建分析参数组"""
        group = QGroupBox("🎯 分析参数")
        layout = QVBoxLayout()

        # 盘后总结（可选）
        summary_label = QLabel("📝 盘后总结（可选）:")
        layout.addWidget(summary_label)
        
        from PyQt5.QtWidgets import QTextEdit
        self.summary_input = QTextEdit()
        self.summary_input.setPlaceholderText(
            "输入您的盘后总结，AI将结合新闻和您的观察进行分析...\n\n"
            "例如：\n"
            "- 今日大盘下跌2.3%，创业板跌幅更大\n"
            "- 半导体板块逆势上涨，资金抱团明显\n"
            "- 北向资金净流出50亿，外资观望情绪浓厚\n"
            "- 新能源汽车板块调整，但龙头股仍有支撑"
        )
        self.summary_input.setStyleSheet("""
            QTextEdit {
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                padding: 8px;
                background-color: white;
                font-size: 13px;
            }
            QTextEdit:focus {
                border-color: #1890ff;
            }
        """)
        self.summary_input.setMaximumHeight(120)
        layout.addWidget(self.summary_input)

        group.setLayout(layout)
        
        return group

    def create_progress_group(self):
        """创建进度显示组"""
        group = QGroupBox("📊 分析进度")
        layout = QVBoxLayout()

        self.progress_browser = QTextBrowser()
        self.progress_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.progress_browser.setMaximumHeight(150)
        layout.addWidget(self.progress_browser)

        group.setLayout(layout)
        return group

    def create_result_group(self):
        """创建结果显示组"""
        group = QGroupBox("📝 分析结果")
        layout = QVBoxLayout()

        self.result_browser = QTextBrowser()
        self.result_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.result_browser.setMinimumHeight(200)
        layout.addWidget(self.result_browser)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.open_report_btn = QPushButton("📄 打开报告")
        self.open_report_btn.setStyleSheet(BUTTON_SUCCESS)
        self.open_report_btn.setEnabled(False)
        self.open_report_btn.clicked.connect(self.open_report)
        btn_layout.addWidget(self.open_report_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        group.setLayout(layout)
        return group

    def load_config(self):
        """加载配置（初始化时调用）"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()

            # 加载当前服务商
            current = config.get_current_provider()
            if current == 'openai':
                self.provider_combo.setCurrentText('OpenAI')
            elif current == 'deepseek':
                self.provider_combo.setCurrentText('DeepSeek')
            elif current == 'zhipu':
                self.provider_combo.setCurrentText('智谱AI')
            elif current == 'qwen':
                self.provider_combo.setCurrentText('通义千问')
            elif current == 'volcengine':
                self.provider_combo.setCurrentText('火山引擎')

            # 加载对应服务商的配置
            self.load_provider_config()

        except Exception as e:
            print(f"加载配置失败: {e}")

    def load_provider_config(self):
        """加载当前选中服务商的配置"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()

            # 获取当前选中的服务商
            provider_map = {
                'OpenAI': 'openai',
                'DeepSeek': 'deepseek',
                '智谱AI': 'zhipu',
                '通义千问': 'qwen',
                '火山引擎': 'volcengine'
            }
            provider = provider_map.get(self.provider_combo.currentText())

            if provider:
                # 加载该服务商的API Key
                api_key = config.get_api_key(provider)
                if api_key:
                    self.api_key_input.setText(api_key)
                else:
                    self.api_key_input.clear()

                # 加载该服务商的模型
                model = config.get_model(provider)
                index = self.model_combo.findText(model)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)

        except Exception as e:
            print(f"加载服务商配置失败: {e}")

    def on_provider_changed(self, text):
        """服务商切换"""
        self.update_model_list()
        # 加载对应服务商的API Key和模型
        self.load_provider_config()

    def update_model_list(self):
        """更新模型列表（从AIConfig动态获取）"""
        from core.ai_config import AIConfig
        
        # 中文名称到英文key的映射
        provider_name_map = {
            'OpenAI': 'openai',
            'DeepSeek': 'deepseek',
            '智谱AI': 'zhipu',
            '通义千问': 'qwen',
            '火山引擎': 'volcengine'
        }
        
        current_provider = self.provider_combo.currentText()
        provider_key = provider_name_map.get(current_provider, 'openai')
        
        # 从AIConfig动态获取模型列表
        config = AIConfig()
        models = config.get_available_models(provider_key)
        
        self.model_combo.clear()
        self.model_combo.addItems(models)

    def save_api_key(self):
        """保存API Key"""
        try:
            from core.ai_config import AIConfig

            provider_map = {
                'OpenAI': 'openai',
                'DeepSeek': 'deepseek',
                '智谱AI': 'zhipu',
                '通义千问': 'qwen',
                '火山引擎': 'volcengine'
            }

            provider = provider_map.get(self.provider_combo.currentText())
            api_key = self.api_key_input.text().strip()
            model = self.model_combo.currentText()

            if not api_key:
                QMessageBox.warning(self, "警告", "请输入API Key")
                return

            config = AIConfig()
            config.set_api_key(provider, api_key)
            config.set_model(provider, model)
            config.set_current_provider(provider)

            QMessageBox.information(self, "成功", "配置已保存")

        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {str(e)}")

    def test_connection(self):
        """测试连接"""
        try:
            from core.ai_news_analyzer import AINewsAnalyzer

            # 检查API Key
            api_key = self.api_key_input.text().strip()
            if not api_key:
                QMessageBox.warning(self, "警告", "请先输入API Key")
                return

            # 先保存配置
            self.save_api_key()

            self.add_progress("正在测试连接...")

            # 获取当前服务商
            provider_map = {
                'OpenAI': 'openai',
                'DeepSeek': 'deepseek',
                '智谱AI': 'zhipu',
                '通义千问': 'qwen',
                '火山引擎': 'volcengine'
            }
            provider = provider_map.get(self.provider_combo.currentText())

            analyzer = AINewsAnalyzer()
            result = analyzer.test_connection(provider)

            if result.get('success'):
                QMessageBox.information(
                    self, "成功", "连接测试成功！\n" + result.get('message', ''))
                self.add_progress("✅ 连接测试成功")
            else:
                QMessageBox.warning(
                    self, "失败", "连接测试失败！\n" + result.get('message', ''))
                self.add_progress("❌ 连接测试失败: " + result.get('message', ''))

        except Exception as e:
            QMessageBox.critical(self, "错误", f"测试失败: {str(e)}")
            self.add_progress(f"❌ 测试失败: {str(e)}")

    def browse_export_file(self):
        """浏览导出文件"""
        default_dir = os.path.join(os.getcwd(), 'data', 'exports')
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择导出文件",
            default_dir,
            "数据文件 (*.json *.md *.txt);;所有文件 (*.*)"
        )
        if file_path:
            self.export_path_input.setText(file_path)

    def browse_impact_file(self):
        """浏览影响分析文件"""
        default_dir = os.path.join(os.getcwd(), 'data', 'analysis')
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择影响分析报告",
            default_dir,
            "Markdown文件 (*.md);;所有文件 (*.*)"
        )
        if file_path:
            self.impact_path_input.setText(file_path)

    def start_analysis(self):
        """开始分析"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "警告", "分析正在进行中！")
            return

        # 获取数据源
        if self.export_radio.isChecked():
            file_path = self.export_path_input.text().strip()
        elif self.impact_radio.isChecked():
            file_path = self.impact_path_input.text().strip()
        else:
            QMessageBox.warning(self, "警告", "请选择数据源")
            return

        if not file_path or not os.path.exists(file_path):
            QMessageBox.warning(self, "警告", "请选择有效的数据文件")
            return

        # 检查配置
        api_key = self.api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "警告", "请先配置API Key")
            return

        # 禁用按钮
        self.analyze_btn.setEnabled(False)
        self.open_report_btn.setEnabled(False)

        # 清空显示
        self.progress_browser.clear()
        self.result_browser.clear()

        # 添加日志
        self.add_progress("=" * 60)
        self.add_progress(
            f"开始分析 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.add_progress("=" * 60)
        self.add_progress(f"数据文件: {os.path.basename(file_path)}")

        # 获取参数
        provider_map = {
            'OpenAI': 'openai',
            'DeepSeek': 'deepseek',
            '智谱AI': 'zhipu',
            '通义千问': 'qwen',
            '火山引擎': 'volcengine'
        }
        provider = provider_map.get(self.provider_combo.currentText())
        
        template_id = self.template_combo.currentData()
        
        # 获取盘后总结（可选）
        market_summary = self.summary_input.toPlainText().strip()

        # 保存当前模板为默认
        if template_id:
            from core.ai_config import AIConfig
            config = AIConfig()
            config.set_current_template(template_id)

        # 创建工作线程（参数已在模板中定义）
        self.worker = AIAnalysisWorker(
            file_path=file_path,
            provider=provider,
            max_sectors='auto',
            stocks_per_sector='auto',
            max_news=5000,
            template_id=template_id,
            market_summary=market_summary
        )

        # 连接信号
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.error.connect(self.on_analysis_error)
        self.worker.progress.connect(self.add_progress)
        self.worker.streaming.connect(self.add_streaming_content)

        # 启动线程
        self.worker.start()

    def on_analysis_finished(self, result):
        """分析完成"""
        self.add_progress("=" * 60)
        self.add_progress("✅ 分析完成！")
        self.add_progress("=" * 60)

        # 显示结果摘要
        time_range = result.get('time_range', {})
        start_time = time_range.get('start', '未知')
        end_time = time_range.get('end', '未知')
        summary = f"""
分析完成！

新闻数量: {result.get('news_count', 0)} 条
时间范围: {start_time} 至 {end_time}
报告文件: {result.get('report_file', '未知')}
        """.strip()

        self.add_progress(summary)

        # 显示完整结果
        self.result_browser.setPlainText(result.get('result', ''))

        # 保存报告路径
        self.last_report = result.get('report_file')

        # 启用按钮
        self.analyze_btn.setEnabled(True)
        if self.last_report and os.path.exists(self.last_report):
            self.open_report_btn.setEnabled(True)

    def on_analysis_error(self, error_msg):
        """分析失败"""
        self.add_progress("=" * 60)
        self.add_progress(f"❌ 分析失败: {error_msg}")
        self.add_progress("=" * 60)

        self.analyze_btn.setEnabled(True)
        QMessageBox.critical(self, "错误", error_msg)

    def add_progress(self, message):
        """添加进度信息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_browser.append(f"[{timestamp}] {message}")
        self.progress_browser.moveCursor(QTextCursor.End)

    def add_streaming_content(self, content):
        """添加流式内容"""
        self.result_browser.insertPlainText(content)
        self.result_browser.moveCursor(QTextCursor.End)

    def open_report(self):
        """打开报告（使用Typora）"""
        if self.last_report and os.path.exists(self.last_report):
            import subprocess
            import sys

            # 智能获取Typora路径（兼容打包后的exe）
            if getattr(sys, 'frozen', False):
                # 打包后的程序
                exe_dir = os.path.dirname(sys.executable)
                typora_path = os.path.join(exe_dir, 'Typora', 'Typora.exe')
            else:
                # 开发环境
                typora_path = os.path.join(os.getcwd(), 'Typora', 'Typora.exe')

            # 如果Typora存在，使用Typora打开
            if os.path.exists(typora_path):
                try:
                    subprocess.Popen(
                        [typora_path, os.path.abspath(self.last_report)])
                    self.add_progress(
                        f"已使用Typora打开报告: {os.path.basename(self.last_report)}")
                except Exception as e:
                    QMessageBox.warning(self, "警告", f"使用Typora打开失败: {str(e)}")
            else:
                # Typora不存在，使用系统默认程序
                try:
                    os.startfile(self.last_report)
                    self.add_progress(
                        f"已打开报告: {os.path.basename(self.last_report)}")
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"打开报告失败: {str(e)}")
        else:
            QMessageBox.warning(self, "警告", "报告文件不存在！")

    def load_template_list(self):
        """加载模板列表"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            templates = config.get_prompt_templates()
            
            self.template_combo.clear()
            for key, template in templates.items():
                display_name = template.get('name', key)
                self.template_combo.addItem(display_name, key)
            
            # 设置当前选中的模板
            current = config.get_current_prompt_template()
            for i in range(self.template_combo.count()):
                if self.template_combo.itemData(i) == current:
                    self.template_combo.setCurrentIndex(i)
                    break
                    
        except Exception as e:
            print(f"加载模板列表失败: {e}")

    def update_template_description(self):
        """更新模板说明"""
        try:
            from core.ai_config import AIConfig
            config = AIConfig()
            
            template_key = self.template_combo.currentData()
            if template_key:
                template = config.get_prompt_template(template_key)
                system_prompt = template.get('system_prompt', '')
                # 提取第一段作为说明
                desc = system_prompt.split('\n\n')[0] if system_prompt else ''
                display_text = desc[:200] + '...' if len(desc) > 200 else desc
                self.template_desc_label.setText(display_text)
        except Exception as e:
            print(f"更新模板说明失败: {e}")

    def refresh(self):
        """刷新页面"""
        self.load_config()
        self.load_template_list()

