"""提示词管理页面 (Phase M0 引入)。

设计目标:
    - 统一管理 prompts/{category}/*.md 文件
    - 新建 / 编辑 / 复制 / 删除全部聚合在这里
    - 其它页面（如 AI 分析）只负责"选择"模板

布局::

    +-----------------------------------------------------------+
    | 📝 提示词管理 ｜ 类别: [analysis▼] ｜ [🔄 刷新][📁 打开目录]|
    +-----------------------+-----------------------------------+
    | 模板列表              | 标题 / 路径 / 大小                  |
    | * 标准分析            +-----------------------------------+
    | * 综合策略            | ## SYSTEM                         |
    | + 新闻分析            |  ......                           |
    | + 超级综合模板        +-----------------------------------+
    |                       | ## USER                           |
    |                       |  ......                           |
    +-----------------------+-----------------------------------+
    | [➕新建] [✏️编辑] [📋复制为新] [🗑️删除] [📂打开文件]        |
    +-----------------------------------------------------------+

提示符约定:
    * = 内置模板（不可删除，编辑时强制"另存为"）
    + = 自定义模板（可改可删）

页面状态变更后 emit ``prompts_changed`` 信号，主窗口转发给依赖方
（目前是 ``AIAnalysisPage.load_template_list``）。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from PyQt5.QtCore import QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.ai_config import AIConfig, BUILTIN_PROMPT_TEMPLATES
from core.prompt_loader import PromptError, PromptLoader
from gui.pages.prompt_template_dialog import PromptTemplateDialog
from gui.utils.styles import (
    BUTTON_DANGER,
    BUTTON_PRIMARY,
    BUTTON_SUCCESS,
    COMBOBOX_STYLE,
    TEXTBROWSER_STYLE,
)


_CATEGORY_LABELS = [
    ("analysis", "AI 分析模板"),
    ("theme_extraction", "题材抽取模板"),
]


class PromptManagerPage(QWidget):
    """提示词管理页面。"""

    # 模板被新建 / 修改 / 删除时发射，参数为类别名
    prompts_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._loader = PromptLoader()
        self._current_category = "analysis"
        self._current_id: Optional[str] = None
        self.init_ui()
        self.refresh()

    # ---- UI 构建 ----

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("📝 提示词管理")
        title.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #262626;"
        )
        layout.addWidget(title)

        hint = QLabel(
            "在此处统一管理所有 AI 提示词。其它页面只需要选择已存在的模板，"
            "新建 / 编辑 / 删除全部在本页面完成。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #595959;")
        layout.addWidget(hint)

        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_main_area(), 1)
        layout.addWidget(self._build_button_bar())

    def _build_toolbar(self) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: #ffffff; border-radius: 6px; }"
        )
        row = QHBoxLayout(wrap)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)

        row.addWidget(QLabel("类别:"))
        self.category_combo = QComboBox()
        for cat_id, label in _CATEGORY_LABELS:
            self.category_combo.addItem(label, cat_id)
        self.category_combo.setStyleSheet(COMBOBOX_STYLE)
        self.category_combo.currentIndexChanged.connect(
            self._on_category_changed
        )
        row.addWidget(self.category_combo)

        row.addStretch()

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)

        self.open_dir_btn = QPushButton("📁 打开目录")
        self.open_dir_btn.clicked.connect(self._open_category_dir)
        row.addWidget(self.open_dir_btn)

        return wrap

    def _build_main_area(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)

        # 左：模板列表
        left = QGroupBox("模板列表")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_double_clicked)
        left_layout.addWidget(self.list_widget)

        self.list_summary = QLabel("0 / 0")
        self.list_summary.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        left_layout.addWidget(self.list_summary)
        splitter.addWidget(left)

        # 右：详情
        right = QGroupBox("模板详情")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)

        self.meta_label = QLabel("（请在左侧选择一个模板）")
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet(
            "color: #262626; font-size: 13px; "
            "padding: 4px 6px; background: #fafafa; "
            "border-radius: 4px;"
        )
        right_layout.addWidget(self.meta_label)

        sys_header = QLabel("📌 SYSTEM Prompt")
        sys_header.setStyleSheet("font-weight: bold; color: #262626;")
        right_layout.addWidget(sys_header)
        self.system_view = QTextBrowser()
        self.system_view.setStyleSheet(TEXTBROWSER_STYLE)
        self.system_view.setOpenExternalLinks(False)
        self.system_view.setFont(self._mono_font())
        right_layout.addWidget(self.system_view, 1)

        usr_header = QLabel("📨 USER Prompt Template")
        usr_header.setStyleSheet("font-weight: bold; color: #262626;")
        right_layout.addWidget(usr_header)
        self.user_view = QTextBrowser()
        self.user_view.setStyleSheet(TEXTBROWSER_STYLE)
        self.user_view.setOpenExternalLinks(False)
        self.user_view.setFont(self._mono_font())
        right_layout.addWidget(self.user_view, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([260, 720])
        return splitter

    def _build_button_bar(self) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: #ffffff; border-radius: 6px; }"
        )
        row = QHBoxLayout(wrap)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)
        row.addStretch()

        self.new_btn = QPushButton("➕ 新建")
        self.new_btn.setStyleSheet(BUTTON_SUCCESS)
        self.new_btn.clicked.connect(self.new_template)
        row.addWidget(self.new_btn)

        self.edit_btn = QPushButton("✏️ 编辑")
        self.edit_btn.setStyleSheet(BUTTON_PRIMARY)
        self.edit_btn.clicked.connect(self.edit_template)
        row.addWidget(self.edit_btn)

        self.duplicate_btn = QPushButton("📋 复制为新")
        self.duplicate_btn.clicked.connect(self.duplicate_template)
        row.addWidget(self.duplicate_btn)

        self.delete_btn = QPushButton("🗑️ 删除")
        self.delete_btn.setStyleSheet(BUTTON_DANGER)
        self.delete_btn.clicked.connect(self.delete_template)
        row.addWidget(self.delete_btn)

        self.open_file_btn = QPushButton("📂 打开文件")
        self.open_file_btn.clicked.connect(self._open_current_file)
        row.addWidget(self.open_file_btn)

        return wrap

    # ---- 数据加载 ----

    def refresh(self) -> None:
        """重新读取磁盘并刷新列表。"""
        self._loader.reload()
        category = self.category_combo.currentData() or self._current_category
        self._current_category = category

        try:
            templates = self._loader.list_category(category)
        except PromptError as e:
            templates = []
            QMessageBox.warning(
                self, "加载失败", f"无法加载 {category} 下的 prompt: {e}"
            )

        previous = self._current_id
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        builtin_count = 0
        for t in templates:
            is_builtin = (
                category == "analysis" and t.id in BUILTIN_PROMPT_TEMPLATES
            )
            if is_builtin:
                builtin_count += 1
            mark = "★" if is_builtin else "＋"
            item = QListWidgetItem(f"{mark}  {t.name}    [{t.id}]")
            item.setData(Qt.UserRole, t.id)
            tooltip_lines = [
                f"ID: {t.id}",
                f"名称: {t.name}",
                f"版本: v{t.version}",
                f"文件: {t.file_path}",
            ]
            if t.description:
                tooltip_lines.append(f"说明: {t.description}")
            item.setToolTip("\n".join(tooltip_lines))
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

        self.list_summary.setText(
            f"共 {len(templates)} 个   |   "
            f"内置 {builtin_count}   |   自定义 {len(templates) - builtin_count}"
        )

        # 复位选中
        if previous:
            self._select_by_id(previous) or self._select_first()
        else:
            self._select_first()

    def _select_first(self) -> bool:
        if self.list_widget.count() == 0:
            self._on_selection_changed()
            return False
        self.list_widget.setCurrentRow(0)
        return True

    def _select_by_id(self, prompt_id: str) -> bool:
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            if it.data(Qt.UserRole) == prompt_id:
                self.list_widget.setCurrentRow(i)
                return True
        return False

    # ---- 信号槽 ----

    def _on_category_changed(self, _index: int) -> None:
        self._current_id = None
        self.refresh()

    def _on_double_clicked(self, _item) -> None:
        self.edit_template()

    def _on_selection_changed(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            self._current_id = None
            self.meta_label.setText("（请在左侧选择一个模板）")
            self.system_view.clear()
            self.user_view.clear()
            self._update_button_state()
            return

        prompt_id = item.data(Qt.UserRole)
        self._current_id = prompt_id
        try:
            tmpl = self._loader.get(self._current_category, prompt_id)
        except PromptError as e:
            self.meta_label.setText(f"❌ 加载失败: {e}")
            self.system_view.clear()
            self.user_view.clear()
            self._update_button_state()
            return

        is_builtin = (
            self._current_category == "analysis"
            and prompt_id in BUILTIN_PROMPT_TEMPLATES
        )
        type_tag = "内置（编辑时另存为新）" if is_builtin else "自定义（可编辑/删除）"
        path_str = str(tmpl.file_path) if tmpl.file_path else "(未知)"
        size = (
            os.path.getsize(tmpl.file_path)
            if tmpl.file_path and os.path.exists(tmpl.file_path)
            else 0
        )
        self.meta_label.setText(
            f"<b>{tmpl.name}</b>  <span style='color:#8c8c8c'>"
            f"[{tmpl.id}]</span><br>"
            f"类型: {type_tag}　|　版本: v{tmpl.version}　|　大小: "
            f"{size / 1024:.1f} KB<br>"
            f"<span style='color:#8c8c8c'>路径: {path_str}</span>"
        )
        self.system_view.setPlainText(tmpl.system_prompt)
        self.user_view.setPlainText(tmpl.user_prompt_template)
        self._update_button_state()

    def _update_button_state(self) -> None:
        has_selection = self._current_id is not None
        is_builtin = (
            has_selection
            and self._current_category == "analysis"
            and self._current_id in BUILTIN_PROMPT_TEMPLATES
        )
        self.edit_btn.setEnabled(has_selection)
        self.duplicate_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection and not is_builtin)
        self.open_file_btn.setEnabled(has_selection)

    # ---- 增删改 ----

    def new_template(self) -> None:
        dialog = PromptTemplateDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        result = dialog.get_result()
        if not result:
            return
        new_id = self._allocate_id()
        self._save_template(
            new_id,
            result["name"],
            result["system_prompt"],
            result["user_prompt_template"],
        )
        QMessageBox.information(self, "成功", f"已新建模板：{result['name']}")
        self.refresh()
        self._select_by_id(new_id)

    def edit_template(self) -> None:
        if not self._current_id:
            return
        category = self._current_category
        prompt_id = self._current_id
        is_builtin = (
            category == "analysis"
            and prompt_id in BUILTIN_PROMPT_TEMPLATES
        )

        try:
            tmpl = self._loader.get(category, prompt_id)
        except PromptError as e:
            QMessageBox.warning(self, "加载失败", str(e))
            return

        if is_builtin:
            reply = QMessageBox.question(
                self,
                "编辑内置模板",
                "内置模板不允许直接覆盖，是否要基于当前内容创建一个新模板？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        template_data = tmpl.to_dict()
        dialog = PromptTemplateDialog(self, prompt_id, template_data)
        if dialog.exec_() != QDialog.Accepted:
            return
        result = dialog.get_result()
        if not result:
            return

        if is_builtin:
            new_id = self._allocate_id()
            self._save_template(
                new_id,
                result["name"],
                result["system_prompt"],
                result["user_prompt_template"],
            )
            QMessageBox.information(
                self, "已另存", f"已新建自定义模板：{result['name']}"
            )
            self.refresh()
            self._select_by_id(new_id)
        else:
            self._save_template(
                prompt_id,
                result["name"],
                result["system_prompt"],
                result["user_prompt_template"],
            )
            QMessageBox.information(self, "成功", "模板已更新")
            self.refresh()
            self._select_by_id(prompt_id)

    def duplicate_template(self) -> None:
        if not self._current_id:
            return
        try:
            tmpl = self._loader.get(self._current_category, self._current_id)
        except PromptError as e:
            QMessageBox.warning(self, "加载失败", str(e))
            return

        template_data = {
            "name": tmpl.name + "（副本）",
            "system_prompt": tmpl.system_prompt,
            "user_prompt_template": tmpl.user_prompt_template,
        }
        dialog = PromptTemplateDialog(self, None, template_data)
        if dialog.exec_() != QDialog.Accepted:
            return
        result = dialog.get_result()
        if not result:
            return
        new_id = self._allocate_id()
        self._save_template(
            new_id,
            result["name"],
            result["system_prompt"],
            result["user_prompt_template"],
        )
        QMessageBox.information(
            self, "成功", f"已创建副本模板：{result['name']}"
        )
        self.refresh()
        self._select_by_id(new_id)

    def delete_template(self) -> None:
        if not self._current_id:
            return
        prompt_id = self._current_id
        category = self._current_category
        if (
            category == "analysis"
            and prompt_id in BUILTIN_PROMPT_TEMPLATES
        ):
            QMessageBox.warning(self, "提示", "内置模板不可删除")
            return

        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除模板【{prompt_id}】吗？该操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            ok = self._loader.delete(category, prompt_id)
        except PromptError as e:
            QMessageBox.warning(self, "删除失败", str(e))
            return
        if not ok:
            QMessageBox.warning(self, "失败", "未能删除该模板")
            return

        # 如果删的就是 AIConfig 当前选中的，回退到 standard
        if category == "analysis":
            cfg = AIConfig()
            if cfg.get_current_prompt_template() == prompt_id:
                cfg.set_current_prompt_template("standard")

        self.prompts_changed.emit(category)
        self._current_id = None
        QMessageBox.information(self, "成功", "已删除")
        self.refresh()

    # ---- 杂项 ----

    def _allocate_id(self) -> str:
        return f"custom_{int(time.time())}"

    def _save_template(
        self,
        prompt_id: str,
        name: str,
        system_prompt: str,
        user_prompt_template: str,
    ) -> None:
        if self._current_category == "analysis":
            # 走 AIConfig，统一兜底逻辑（含 frontmatter 渲染）
            cfg = AIConfig()
            cfg.save_template(
                prompt_id, name, system_prompt, user_prompt_template
            )
        else:
            # 其它 category 直接通过 PromptLoader 写
            from datetime import datetime
            fm = [
                "---",
                f"id: {prompt_id}",
                f"name: {AIConfig._quote_yaml(name)}",
                f"category: {self._current_category}",
                "version: '1.0'",
                f"updated_at: '{datetime.now().strftime('%Y-%m-%d')}'",
                "---",
                "",
                "## SYSTEM",
                "",
                (system_prompt or "").rstrip(),
                "",
                "## USER",
                "",
                (user_prompt_template or "").rstrip(),
                "",
            ]
            self._loader.save(
                self._current_category, prompt_id, "\n".join(fm)
            )
        self.prompts_changed.emit(self._current_category)

    def _open_category_dir(self) -> None:
        target = self._loader.root / self._current_category
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.resolve())))

    def _open_current_file(self) -> None:
        if not self._current_id:
            return
        try:
            tmpl = self._loader.get(self._current_category, self._current_id)
        except PromptError as e:
            QMessageBox.warning(self, "打开失败", str(e))
            return
        if not tmpl.file_path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(tmpl.file_path)))

    @staticmethod
    def _mono_font() -> QFont:
        font = QFont("Consolas")
        if not font.exactMatch():
            font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        return font
