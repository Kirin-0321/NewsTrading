"""
AI分析页面
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QComboBox, QLineEdit,
                             QTextBrowser, QMessageBox,
                             QDateTimeEdit, QCheckBox, QScrollArea, QFrame,
                             QSizePolicy)
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtGui import QTextCursor
from datetime import datetime, timedelta
import os

from gui.utils.styles import (BUTTON_PRIMARY, BUTTON_SUCCESS, BUTTON_DANGER,
                              COMBOBOX_STYLE, INPUT_STYLE, TEXTBROWSER_STYLE)
from gui.workers.ai_analysis_worker import AIAnalysisWorker
from gui.utils.paths import find_typora_executable, resolve_project_path


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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 上部配置区：可滚动，避免默认窗口高度下控件互相挤压
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        form_layout = QVBoxLayout(scroll_content)
        form_layout.setContentsMargins(20, 20, 20, 10)
        form_layout.setSpacing(15)

        title = QLabel("🤖 AI分析")
        title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #262626;")
        form_layout.addWidget(title)

        form_layout.addWidget(self.create_config_group())
        form_layout.addWidget(self.create_source_group())

        params_group = self.create_params_group()
        form_layout.addWidget(params_group)
        self.model_combo.currentTextChanged.connect(
            self._update_deep_thinking_availability
        )

        form_layout.addWidget(self.create_template_group())

        action_row = QHBoxLayout()
        self.analyze_btn = QPushButton("🚀 开始分析")
        self.analyze_btn.setStyleSheet(BUTTON_PRIMARY)
        self.analyze_btn.setMinimumHeight(45)
        self.analyze_btn.clicked.connect(self.start_analysis)
        action_row.addWidget(self.analyze_btn, 1)

        self.stop_btn = QPushButton("⏹ 终止分析")
        self.stop_btn.setStyleSheet(BUTTON_DANGER)
        self.stop_btn.setMinimumHeight(45)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_analysis)
        action_row.addWidget(self.stop_btn)
        form_layout.addLayout(action_row)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 3)

        # 下部日志/结果：占据剩余空间，随窗口伸缩
        bottom_wrap = QWidget()
        bottom_layout = QVBoxLayout(bottom_wrap)
        bottom_layout.setContentsMargins(20, 0, 20, 20)
        bottom_layout.setSpacing(10)

        progress_group = self.create_progress_group()
        bottom_layout.addWidget(progress_group, 1)

        result_group = self.create_result_group()
        bottom_layout.addWidget(result_group, 2)

        layout.addWidget(bottom_wrap, 2)

        # 加载配置和模板数据（必须在所有 UI 组件创建完成后）
        self.load_config()
        self.load_template_list()

    def showEvent(self, event):
        """页面显示时刷新统计 + 重新读取模板列表（防止管理页改完没同步）"""
        super().showEvent(event)
        if hasattr(self, "_refresh_db_stats"):
            self._refresh_db_stats()
        if hasattr(self, "template_combo"):
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

        group.setLayout(layout)
        return group

    def create_template_group(self):
        """创建提示词模板组（仅选择，新建/编辑请去"提示词管理"页）。"""
        group = QGroupBox("📝 提示词模板")
        layout = QVBoxLayout()

        # 模板选择
        template_layout = QHBoxLayout()
        template_label = QLabel("选择模板:")
        template_label.setMinimumWidth(120)
        self.template_combo = QComboBox()
        self.template_combo.setStyleSheet(COMBOBOX_STYLE)
        template_layout.addWidget(template_label)
        template_layout.addWidget(self.template_combo, 1)

        self.refresh_templates_btn = QPushButton("🔄")
        self.refresh_templates_btn.setToolTip("重新读取 prompts/ 目录")
        self.refresh_templates_btn.clicked.connect(self.load_template_list)
        self.refresh_templates_btn.setFixedWidth(36)
        template_layout.addWidget(self.refresh_templates_btn)

        self.manage_templates_btn = QPushButton("📝 管理模板…")
        self.manage_templates_btn.setToolTip(
            "跳转到'提示词管理'页面，在那里新建/编辑/删除"
        )
        self.manage_templates_btn.clicked.connect(self._goto_prompt_manager)
        template_layout.addWidget(self.manage_templates_btn)

        layout.addLayout(template_layout)

        hint = QLabel(
            "💡 新建 / 编辑 / 删除请在侧边栏【📝 提示词管理】中操作，"
            "保存后此处会自动刷新。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        layout.addWidget(hint)

        group.setLayout(layout)
        return group

    def load_template_list(self):
        """加载分析模板列表（从 prompts/analysis/*.md 读取）"""
        try:
            from core.ai_config import AIConfig

            config = AIConfig()
            templates = config.get_prompt_templates()

            previous = self.template_combo.currentData()

            self.template_combo.blockSignals(True)
            self.template_combo.clear()
            for key, template in templates.items():
                display_name = template.get("name", key)
                self.template_combo.addItem(display_name, key)
            self.template_combo.blockSignals(False)

            current = previous or config.get_current_prompt_template()
            for i in range(self.template_combo.count()):
                if self.template_combo.itemData(i) == current:
                    self.template_combo.setCurrentIndex(i)
                    break

        except Exception as e:
            print(f"加载分析模板失败: {e}")
            QMessageBox.critical(self, "错误", f"加载分析模板失败: {str(e)}")

    def load_templates(self):
        """兼容旧调用，统一走 load_template_list。"""
        self.load_template_list()

    def _goto_prompt_manager(self) -> None:
        """跳转到主窗口的'提示词管理'页面。"""
        try:
            window = self.window()
            if hasattr(window, "show_page"):
                window.show_page("prompt_manager")
        except Exception as e:
            print(f"[AIAnalysis] 跳转提示词管理失败: {e}")

    def create_source_group(self):
        """创建数据源选择组（SQLite）"""
        group = QGroupBox("📂 数据源")
        layout = QVBoxLayout()

        layout.addWidget(QLabel("SQLite 数据库"))

        sqlite_layout = QVBoxLayout()
        sqlite_layout.setContentsMargins(30, 0, 0, 0)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("数据表:"))
        self.db_source_combo = QComboBox()
        self.db_source_combo.addItem("精选库 (curated)", "curated")
        self.db_source_combo.addItem("原始库 (raw)", "raw")
        self.db_source_combo.setStyleSheet(COMBOBOX_STYLE)
        self.db_source_combo.currentIndexChanged.connect(self._on_db_source_changed)
        row1.addWidget(self.db_source_combo, 1)
        sqlite_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("快捷范围:"))
        self.range_preset_combo = QComboBox()
        # (显示文本, key) —— 用 key 判断分支，避免新增项时 index 错位
        for label, key in [
            ("最近 24 小时", "24h"),
            ("最近 48 小时", "48h"),
            ("最近 7 天", "7d"),
            ("上一收盘日 14:00 至今", "last_close_14"),
            ("早上 7:00 至今", "since_7am"),
            ("全部数据", "all"),
            ("自定义", "custom"),
        ]:
            self.range_preset_combo.addItem(label, key)
        self.range_preset_combo.setStyleSheet(COMBOBOX_STYLE)
        self.range_preset_combo.currentIndexChanged.connect(self._on_range_preset_changed)
        row2.addWidget(self.range_preset_combo, 1)
        sqlite_layout.addLayout(row2)

        start_row = QHBoxLayout()
        start_label = QLabel("起始:")
        start_label.setMinimumWidth(56)
        start_row.addWidget(start_label)
        self.start_datetime = QDateTimeEdit(
            QDateTime.currentDateTime().addSecs(-86400))
        self.start_datetime.setCalendarPopup(True)
        self.start_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        start_row.addWidget(self.start_datetime, 1)
        sqlite_layout.addLayout(start_row)

        end_row = QHBoxLayout()
        end_label = QLabel("结束:")
        end_label.setMinimumWidth(56)
        end_row.addWidget(end_label)
        self.end_datetime = QDateTimeEdit(QDateTime.currentDateTime())
        self.end_datetime.setCalendarPopup(True)
        self.end_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        end_row.addWidget(self.end_datetime, 1)
        sqlite_layout.addLayout(end_row)

        self.db_stats_label = QLabel("")
        self.db_stats_label.setWordWrap(True)
        self.db_stats_label.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        sqlite_layout.addWidget(self.db_stats_label)
        layout.addLayout(sqlite_layout)

        group.setLayout(layout)
        return group

    def _get_source_store(self, source=None):
        """统一返回 (RawStore, status)；status=None 表示不过滤。"""
        from services.storage import CLEAN_CURATED, get_raw_store
        source = source or self.db_source_combo.currentData()
        status = CLEAN_CURATED if source == "curated" else None
        return get_raw_store(), status

    def _get_source_time_bounds(self, source=None):
        store, status = self._get_source_store(source)
        return store.get_time_bounds(status=status)

    def _on_db_source_changed(self, _index=None):
        self._refresh_db_stats()

    def _on_range_preset_changed(self, _index=None):
        key = self.range_preset_combo.currentData()
        if key == "all":
            start, end = self._get_source_time_bounds()
            if start and end:
                self.start_datetime.setDateTime(QDateTime(start))
                self.end_datetime.setDateTime(QDateTime(end))
            return
        if key == "custom":
            return

        now = QDateTime.currentDateTime()
        self.end_datetime.setDateTime(now)

        if key == "24h":
            self.start_datetime.setDateTime(now.addSecs(-86400))
        elif key == "48h":
            self.start_datetime.setDateTime(now.addSecs(-86400 * 2))
        elif key == "7d":
            self.start_datetime.setDateTime(now.addDays(-7))
        elif key == "last_close_14":
            self.start_datetime.setDateTime(QDateTime(self._last_trading_close_14()))
        elif key == "since_7am":
            self.start_datetime.setDateTime(QDateTime(self._since_morning_7()))

    @staticmethod
    def _since_morning_7() -> datetime:
        """
        返回「最近一次早上 7:00」的时间点。

        规则：
            - 若当前时间已过今日 7:00，起点为今天 7:00
            - 否则起点为昨天 7:00（避免凌晨选此项时范围为负）
        """
        now = datetime.now()
        today_7 = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now < today_7:
            today_7 = today_7 - timedelta(days=1)
        return today_7

    @staticmethod
    def _last_trading_close_14() -> datetime:
        """
        返回"上一次收盘日下午 14:00"的时间。

        规则：
            - A 股交易日为周一(0) ~ 周五(4)
            - 若今天是交易日且当前时间已过 14:00，则起点为今天 14:00
            - 否则回溯到最近一个早于今天的交易日 14:00（自动跳过周末）

        示例（now → 起点）：
            周四 09:00 → 周三 14:00
            周一 09:00 → 上周五 14:00
            周四 16:00 → 周四 14:00
            周六 任意 → 周五 14:00
        """
        now = datetime.now()
        today_14 = now.replace(hour=14, minute=0, second=0, microsecond=0)

        if now.weekday() < 5 and now >= today_14:
            return today_14

        d = (now - timedelta(days=1)).date()
        while d.weekday() >= 5:  # 跳过周六(5)、周日(6)
            d = d - timedelta(days=1)
        return datetime(d.year, d.month, d.day, 14, 0, 0)

    def _refresh_db_stats(self):
        try:
            store, status = self._get_source_store()
            label = "精选库" if status else "原始库"
            start, end = store.get_time_bounds(status=status)
            count = store.count(status=status)
            if start and end:
                span = (
                    f"{label} {count} 条 | "
                    f"数据时间 {start.strftime('%m-%d %H:%M')} ~ "
                    f"{end.strftime('%m-%d %H:%M')}"
                )
            else:
                span = f"{label} 暂无数据"
            from services.storage import get_raw_store
            raw = get_raw_store()
            self.db_stats_label.setText(
                f"原始库 {raw.count()} 条 | 精选库 {raw.count_curated()} 条 | "
                f"待清洗 {raw.count_uncleaned()} 条 | {span}"
            )
        except Exception as e:
            self.db_stats_label.setText(f"数据库: {e}")

    def _format_time_bounds_hint(self, source):
        start, end = self._get_source_time_bounds(source)
        if not start or not end:
            return "当前数据源为空"
        name = "精选库" if source == "curated" else "原始库"
        return (
            f"{name} 实际数据时间：\n"
            f"{start.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{end.strftime('%Y-%m-%d %H:%M')}"
        )

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

        # 深度思考开关
        thinking_row = QHBoxLayout()
        self.deep_thinking_check = QCheckBox("🧠 深度思考（DeepSeek V4 推理模式）")
        self.deep_thinking_check.setChecked(True)
        self.deep_thinking_check.setStyleSheet(
            "QCheckBox { font-size: 13px; color: #262626; padding-top: 4px; }"
        )
        self.deep_thinking_check.setToolTip(
            "开启后 DeepSeek V4 系列模型将启用 thinking + reasoning_effort=max，"
            "分析质量更高但耗时更长、费用更高；其他服务商/模型忽略此选项。"
        )
        self.deep_thinking_check.toggled.connect(self._save_deep_thinking_pref)
        thinking_row.addWidget(self.deep_thinking_check)
        thinking_row.addStretch()
        layout.addLayout(thinking_row)

        # 题材抽取开关
        theme_row = QHBoxLayout()
        self.extract_theme_check = QCheckBox(
            "📌 分析完成后自动抽取题材并入库（题材预测库）"
        )
        self.extract_theme_check.setChecked(True)
        self.extract_theme_check.setStyleSheet(
            "QCheckBox { font-size: 13px; color: #262626; padding-top: 4px; }"
        )
        self.extract_theme_check.setToolTip(
            "勾选后分析报告生成完会调用另一个 AI（默认 deepseek-v4-pro）"
            "抽取结构化题材入 SQLite，可在【题材预测】页面查看。"
        )
        theme_row.addWidget(self.extract_theme_check)
        theme_row.addStretch()
        layout.addLayout(theme_row)

        group.setLayout(layout)

        return group

    def create_progress_group(self):
        """创建进度显示组"""
        group = QGroupBox("📊 分析进度")
        layout = QVBoxLayout()

        self.progress_browser = QTextBrowser()
        self.progress_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.progress_browser.setMinimumHeight(72)
        self.progress_browser.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.progress_browser)

        group.setLayout(layout)
        return group

    def create_result_group(self):
        """创建结果显示组"""
        group = QGroupBox("📝 分析结果")
        layout = QVBoxLayout()

        self.result_browser = QTextBrowser()
        self.result_browser.setStyleSheet(TEXTBROWSER_STYLE)
        self.result_browser.setMinimumHeight(96)
        self.result_browser.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
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

            params = config.get_analysis_params()
            if hasattr(self, "deep_thinking_check"):
                self.deep_thinking_check.setChecked(
                    params.get("deep_thinking_enabled", True)
                )
                self._update_deep_thinking_availability()

        except Exception as e:
            print(f"加载配置失败: {e}")

    def _supports_deep_thinking(self) -> bool:
        """当前选中的 DeepSeek V4 模型是否支持深度思考。"""
        if self.provider_combo.currentText() != "DeepSeek":
            return False
        model = self.model_combo.currentText().strip()
        return model.startswith("deepseek-v4")

    def _update_deep_thinking_availability(self, *_args):
        """根据服务商/模型更新深度思考开关可用状态。"""
        if not hasattr(self, "deep_thinking_check"):
            return
        supported = self._supports_deep_thinking()
        self.deep_thinking_check.setEnabled(supported)
        if supported:
            self.deep_thinking_check.setToolTip(
                "开启后 DeepSeek V4 系列模型将启用 thinking + reasoning_effort=max，"
                "分析质量更高但耗时更长、费用更高。"
            )
        else:
            self.deep_thinking_check.setToolTip(
                "仅 DeepSeek V4 系列模型（deepseek-v4-pro / deepseek-v4-flash）"
                "支持深度思考；当前选择不可用。"
            )

    def _save_deep_thinking_pref(self, checked: bool):
        """持久化深度思考开关偏好。"""
        try:
            from core.ai_config import AIConfig
            AIConfig().set_analysis_param("deep_thinking_enabled", checked)
        except Exception as e:
            print(f"保存深度思考配置失败: {e}")

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
        self._update_deep_thinking_availability()

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

    def start_analysis(self):
        """开始分析"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "警告", "分析正在进行中！")
            return

        api_key = self.api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "警告", "请先配置API Key")
            return

        sqlite_source = self.db_source_combo.currentData()
        sqlite_start = self.start_datetime.dateTime().toPyDateTime()
        sqlite_end = self.end_datetime.dateTime().toPyDateTime()
        if sqlite_start >= sqlite_end:
            QMessageBox.warning(self, "警告", "起始时间必须早于结束时间")
            return
        from services.analysis_service import AnalysisService
        count = len(AnalysisService().load_news(
            sqlite_source, sqlite_start, sqlite_end
        ))
        if count == 0:
            hint = self._format_time_bounds_hint(sqlite_source)
            QMessageBox.warning(
                self,
                "警告",
                f"选定时间范围内没有新闻数据。\n\n{hint}\n\n"
                "请将快捷范围改为「全部数据」，或手动调整起止时间。",
            )
            return

        self.analyze_btn.setEnabled(False)
        self.open_report_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_browser.clear()
        self.result_browser.clear()

        self.add_progress("=" * 60)
        self.add_progress(f"开始分析 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.add_progress("=" * 60)
        self.add_progress(
            f"数据源: SQLite {self.db_source_combo.currentText()} | "
            f"{sqlite_start.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{sqlite_end.strftime('%Y-%m-%d %H:%M')}"
        )
        thinking_on = (
            self.deep_thinking_check.isChecked()
            and self._supports_deep_thinking()
        )
        self.add_progress(
            f"深度思考: {'开启' if thinking_on else '关闭'}"
            + ("" if self._supports_deep_thinking() else "（当前模型不支持）")
        )

        provider_map = {
            'OpenAI': 'openai', 'DeepSeek': 'deepseek', '智谱AI': 'zhipu',
            '通义千问': 'qwen', '火山引擎': 'volcengine',
        }
        provider = provider_map.get(self.provider_combo.currentText())
        template_id = self.template_combo.currentData()
        market_summary = self.summary_input.toPlainText().strip()

        if template_id:
            from core.ai_config import AIConfig
            cfg = AIConfig()
            cfg.set_current_prompt_template(template_id)
            cfg.set_current_template(template_id)

        self.worker = AIAnalysisWorker(
            provider=provider,
            max_sectors='auto',
            stocks_per_sector='auto',
            max_news=5000,
            template_id=template_id,
            market_summary=market_summary,
            sqlite_source=sqlite_source,
            sqlite_start=sqlite_start,
            sqlite_end=sqlite_end,
            extract_themes=self.extract_theme_check.isChecked(),
            enable_deep_thinking=thinking_on,
        )

        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.error.connect(self.on_analysis_error)
        self.worker.cancelled.connect(self.on_analysis_cancelled)
        self.worker.progress.connect(self.add_progress)
        self.worker.streaming.connect(self.add_streaming_content)
        self.worker.start()

    def stop_analysis(self):
        """终止正在进行的分析"""
        if not self.worker or not self.worker.isRunning():
            return
        self.add_progress("正在终止分析，请稍候...")
        self.stop_btn.setEnabled(False)
        self.worker.request_cancel()

    def _reset_analysis_buttons(self):
        """恢复分析按钮状态"""
        self.analyze_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def on_analysis_finished(self, result):
        """分析完成"""
        self.add_progress("=" * 60)
        self.add_progress("✅ 分析完成！")
        self.add_progress("=" * 60)

        # 显示结果摘要
        time_range = result.get('time_range', {})
        start_time = time_range.get('start', '未知')
        end_time = time_range.get('end', '未知')
        theme_count = result.get('theme_count', 0)
        theme_error = result.get('theme_error')
        theme_line = ""
        if theme_count:
            theme_line = f"\n题材入库: {theme_count} 条 ✅"
        elif theme_error:
            theme_line = f"\n题材抽取: ⚠️ {theme_error}"
        elif self.extract_theme_check.isChecked():
            theme_line = "\n题材抽取: 已开启但未入库（可能未识别到题材）"

        summary = f"""
分析完成！

新闻数量: {result.get('news_count', 0)} 条
时间范围: {start_time} 至 {end_time}
报告文件: {result.get('report_file', '未知')}{theme_line}
        """.strip()

        self.add_progress(summary)

        # 显示完整结果
        self.result_browser.setPlainText(result.get('result', ''))

        # 保存报告路径
        self.last_report = resolve_project_path(result.get('report_file', ''))

        # 启用按钮
        self._reset_analysis_buttons()
        if self.last_report and os.path.exists(self.last_report):
            self.open_report_btn.setEnabled(True)

    def on_analysis_cancelled(self):
        """分析被用户终止"""
        self.add_progress("=" * 60)
        self.add_progress("⏹ 分析已终止")
        self.add_progress("=" * 60)
        self._reset_analysis_buttons()

    def on_analysis_error(self, error_msg):
        """分析失败"""
        self.add_progress("=" * 60)
        self.add_progress(f"❌ 分析失败: {error_msg}")
        self.add_progress("=" * 60)

        self._reset_analysis_buttons()
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
        """打开报告（优先使用 Typora）"""
        report_path = resolve_project_path(self.last_report or "")
        if not report_path or not os.path.exists(report_path):
            QMessageBox.warning(self, "警告", "报告文件不存在！")
            return

        import subprocess

        typora_path = find_typora_executable()
        if typora_path:
            try:
                subprocess.Popen([typora_path, report_path])
                self.add_progress(
                    f"已使用 Typora 打开报告: {os.path.basename(report_path)}"
                )
                return
            except Exception as e:
                QMessageBox.warning(self, "警告", f"使用 Typora 打开失败: {str(e)}")

        try:
            os.startfile(report_path)
            self.add_progress(f"已打开报告: {os.path.basename(report_path)}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"打开报告失败: {str(e)}")

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
        if hasattr(self, "_refresh_db_stats"):
            self._refresh_db_stats()

