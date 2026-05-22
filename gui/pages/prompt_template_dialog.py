"""
提示词模板编辑对话框
"""

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QLineEdit, QTextEdit, QMessageBox)
from PyQt5.QtCore import Qt
from gui.utils.styles import BUTTON_PRIMARY, BUTTON_DANGER, INPUT_STYLE


class PromptTemplateDialog(QDialog):
    """提示词模板编辑对话框"""

    def __init__(self, parent=None, template_id=None, template_data=None):
        super().__init__(parent)
        self.template_id = template_id
        self.template_data = template_data or {}
        self.init_ui()

    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("编辑提示词模板")
        self.setMinimumSize(800, 600)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # 模板名称
        name_layout = QHBoxLayout()
        name_label = QLabel("模板名称:")
        name_label.setMinimumWidth(100)
        self.name_input = QLineEdit()
        self.name_input.setStyleSheet(INPUT_STYLE)
        self.name_input.setPlaceholderText("例如：激进分析、保守分析")
        if self.template_data:
            self.name_input.setText(self.template_data.get('name', ''))
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)

        # 系统提示词
        layout.addWidget(QLabel("系统提示词（定义AI的角色和分析风格）:"))
        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlaceholderText(
            "例如：你是一位资深的A股投资分析师..."
        )
        if self.template_data:
            self.system_prompt_edit.setPlainText(
                self.template_data.get('system_prompt', ''))
        layout.addWidget(self.system_prompt_edit)

        # 用户提示词模板
        layout.addWidget(QLabel("用户提示词模板（分析要求，可使用变量）:"))
        tip_label = QLabel(
            "💡 可用变量: {news_data}, {max_sectors}, {stocks_per_sector}")
        tip_label.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        layout.addWidget(tip_label)
        self.user_prompt_edit = QTextEdit()
        self.user_prompt_edit.setPlaceholderText(
            "例如：请基于以下新闻数据...\n使用 {news_data} 插入新闻数据"
        )
        if self.template_data:
            self.user_prompt_edit.setPlainText(
                self.template_data.get('user_prompt_template', ''))
        layout.addWidget(self.user_prompt_edit)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setStyleSheet(BUTTON_PRIMARY)
        save_btn.clicked.connect(self.save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def save(self):
        """保存模板"""
        name = self.name_input.text().strip()
        system_prompt = self.system_prompt_edit.toPlainText().strip()
        user_prompt = self.user_prompt_edit.toPlainText().strip()

        if not name:
            QMessageBox.warning(self, "警告", "请输入模板名称")
            return

        if not system_prompt:
            QMessageBox.warning(self, "警告", "请输入系统提示词")
            return

        if not user_prompt:
            QMessageBox.warning(self, "警告", "请输入用户提示词模板")
            return

        self.result = {
            'name': name,
            'system_prompt': system_prompt,
            'user_prompt_template': user_prompt
        }
        self.accept()

    def get_result(self):
        """获取结果"""
        return getattr(self, 'result', None)
    
    def get_template_data(self):
        """获取模板数据（别名方法）"""
        return self.get_result()
