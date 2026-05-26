"""游资席位别名编辑对话框（Phase M3）。

对 ``dim_trader_alias`` 表做 CRUD，让主人能在 GUI 维护「营业部全名 → 简称」
映射。保存后会自动 ``TraderAliasMatcher.reload()``，下次 hybrid 模式重读
``dragon_tiger`` 时立刻命中新增/修改的别名。

布局::

    顶部: 搜索框（按 alias / exalter 实时过滤）
    中部: QTableWidget — exalter / alias / 知名? / 备注 / 更新时间
    右栏: ➕ 新增 / ✏️ 编辑 / 🗑️ 删除
    底部: ✅ 保存关闭 / ❌ 取消
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.market.trader_aliases import TraderAlias, TraderAliasMatcher

_FAMOUS_BADGE = "知名 ⭐"
_PLAIN_BADGE = "—"


class TraderAliasDialog(QDialog):
    """游资别名编辑器。"""

    _COLS = [
        ("营业部全名", 360),
        ("简称", 120),
        ("是否知名", 90),
        ("备注", 220),
        ("更新时间", 140),
    ]

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        matcher: Optional[TraderAliasMatcher] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("游资席位别名管理")
        self.setMinimumSize(960, 600)
        self.matcher = matcher or TraderAliasMatcher()
        self._dirty = False

        self._build_ui()
        self.reload_table()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # 顶部：标题 + 搜索
        head = QHBoxLayout()
        head.addWidget(
            QLabel(
                "<b>游资席位别名</b>　"
                "<span style='color:#8c8c8c'>"
                "（保存后立刻生效，无需重启）</span>"
            )
        )
        head.addStretch()
        head.addWidget(QLabel("🔎"))
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("输入营业部 / 简称 / 备注关键字…")
        self.filter_input.setFixedWidth(280)
        self.filter_input.textChanged.connect(self._apply_filter)
        head.addWidget(self.filter_input)
        outer.addLayout(head)

        # 中部：表 + 右侧按钮
        mid = QHBoxLayout()

        self.table = QTableWidget()
        self.table.setColumnCount(len(self._COLS))
        self.table.setHorizontalHeaderLabels(
            [c[0] for c in self._COLS]
        )
        for i, (_, w) in enumerate(self._COLS):
            self.table.setColumnWidth(i, w)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self._on_edit)
        mid.addWidget(self.table, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.btn_add = QPushButton("➕ 新增")
        self.btn_edit = QPushButton("✏️ 编辑")
        self.btn_del = QPushButton("🗑️ 删除")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_del.clicked.connect(self._on_delete)
        for b in (self.btn_add, self.btn_edit, self.btn_del):
            b.setFixedWidth(110)
            side.addWidget(b)
        side.addStretch()
        self.count_label = QLabel("共 0 条 / 知名 0")
        self.count_label.setStyleSheet("color:#8c8c8c")
        side.addWidget(self.count_label)
        mid.addLayout(side, 0)
        outer.addLayout(mid, 1)

        # 底部：确认/取消
        bb = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        bb.button(QDialogButtonBox.Ok).setText("✅ 保存关闭")
        bb.button(QDialogButtonBox.Cancel).setText("❌ 取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def reload_table(self) -> None:
        self.matcher.reload()
        rows = self.matcher.list_all()
        self._all_rows = rows
        self._render_rows(rows)

    def _render_rows(self, rows) -> None:
        self.table.setRowCount(len(rows))
        famous_n = 0
        for i, t in enumerate(rows):
            self._set_row(i, t)
            if t.is_famous:
                famous_n += 1
        self.count_label.setText(
            f"共 {len(rows)} 条 / 知名 {famous_n}"
        )

    def _set_row(self, row: int, t: TraderAlias) -> None:
        items = [
            QTableWidgetItem(t.exalter),
            QTableWidgetItem(t.alias),
            QTableWidgetItem(_FAMOUS_BADGE if t.is_famous else _PLAIN_BADGE),
            QTableWidgetItem(t.notes or ""),
            QTableWidgetItem(t.updated_at or ""),
        ]
        if t.is_famous:
            items[2].setForeground(Qt.darkMagenta)
            items[1].setForeground(Qt.darkMagenta)
        for col, it in enumerate(items):
            it.setData(Qt.UserRole, t.exalter)
            self.table.setItem(row, col, it)

    def _apply_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        if not text:
            self._render_rows(self._all_rows)
            return
        out = [
            t for t in self._all_rows
            if text in (t.exalter or "").lower()
            or text in (t.alias or "").lower()
            or text in (t.notes or "").lower()
        ]
        self._render_rows(out)

    def _selected_exalter(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        if not it:
            return None
        return it.text()

    # ------------------------------------------------------------------
    # 增 / 改 / 删
    # ------------------------------------------------------------------

    def _on_add(self) -> None:
        dlg = _AliasEditor(self, title="新增席位")
        if dlg.exec_() != QDialog.Accepted:
            return
        data = dlg.payload()
        if not data:
            return
        try:
            self.matcher.add(**data)
            self._dirty = True
        except Exception as e:
            QMessageBox.critical(self, "失败", f"写入失败: {e}")
            return
        self.reload_table()

    def _on_edit(self) -> None:
        exalter = self._selected_exalter()
        if not exalter:
            QMessageBox.information(
                self, "提示", "请先选中一行"
            )
            return
        original = next(
            (t for t in self._all_rows if t.exalter == exalter),
            None,
        )
        if original is None:
            return
        dlg = _AliasEditor(self, title="编辑席位", initial=original)
        if dlg.exec_() != QDialog.Accepted:
            return
        data = dlg.payload()
        if not data:
            return
        # 用户可能改了 exalter（即换主键）—— 先删旧再加新
        if data["exalter"] != original.exalter:
            try:
                self.matcher.delete(original.exalter)
            except Exception:
                pass
        try:
            self.matcher.add(**data)
            self._dirty = True
        except Exception as e:
            QMessageBox.critical(self, "失败", f"写入失败: {e}")
            return
        self.reload_table()

    def _on_delete(self) -> None:
        exalter = self._selected_exalter()
        if not exalter:
            QMessageBox.information(
                self, "提示", "请先选中一行"
            )
            return
        ret = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除该席位别名？\n\n{exalter}",
        )
        if ret != QMessageBox.Yes:
            return
        try:
            ok = self.matcher.delete(exalter)
        except Exception as e:
            QMessageBox.critical(self, "失败", f"删除失败: {e}")
            return
        if ok:
            self._dirty = True
        self.reload_table()

    # ------------------------------------------------------------------
    # 接受时的回调
    # ------------------------------------------------------------------

    def accept(self) -> None:
        # 表里 CRUD 是即时落库的，关闭前不用再做事情
        self.matcher.reload()
        super().accept()

    @property
    def dirty(self) -> bool:
        return self._dirty


class _AliasEditor(QDialog):
    """单条记录的小编辑窗口。"""

    def __init__(
        self,
        parent: QWidget,
        *,
        title: str,
        initial: Optional[TraderAlias] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        layout = QFormLayout(self)
        layout.setLabelAlignment(Qt.AlignRight)
        layout.setFormAlignment(Qt.AlignTop)

        self.exalter_input = QLineEdit()
        self.exalter_input.setPlaceholderText("营业部全名（必填）")
        self.alias_input = QLineEdit()
        self.alias_input.setPlaceholderText("简称，如『拉萨东环二』")
        self.alias_input.setMaxLength(16)
        self.famous_check = QCheckBox("标记为知名游资 ⭐")
        self.notes_input = QTextEdit()
        self.notes_input.setPlaceholderText("可选备注，≤ 200 字")
        self.notes_input.setMaximumHeight(80)

        if initial is not None:
            self.exalter_input.setText(initial.exalter)
            self.alias_input.setText(initial.alias)
            self.famous_check.setChecked(initial.is_famous)
            if initial.notes:
                self.notes_input.setPlainText(initial.notes)

        layout.addRow("营业部:", self.exalter_input)
        layout.addRow("简称:", self.alias_input)
        layout.addRow("", self.famous_check)
        layout.addRow("备注:", self.notes_input)

        bb = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        bb.accepted.connect(self._try_accept)
        bb.rejected.connect(self.reject)
        layout.addRow(bb)

    def _try_accept(self) -> None:
        exalter = self.exalter_input.text().strip()
        alias = self.alias_input.text().strip()
        if not exalter or not alias:
            QMessageBox.warning(
                self, "字段不全", "营业部全名 / 简称 都必须填写"
            )
            return
        self.accept()

    def payload(self) -> Optional[dict]:
        exalter = self.exalter_input.text().strip()
        alias = self.alias_input.text().strip()
        if not exalter or not alias:
            return None
        notes_raw = self.notes_input.toPlainText().strip() or None
        if notes_raw and len(notes_raw) > 200:
            notes_raw = notes_raw[:200]
        return {
            "exalter": exalter,
            "alias": alias,
            "is_famous": self.famous_check.isChecked(),
            "notes": notes_raw,
        }
