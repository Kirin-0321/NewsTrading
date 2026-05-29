"""
数据管理页面 — SQLite 原始库（按 clean_status 分视图: 全部/精选/剔除）+ 报告文件
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QMessageBox, QTextBrowser, QDialog, QDialogButtonBox,
    QTreeWidget, QTreeWidgetItem, QSplitter,
)
from PyQt5.QtCore import Qt
import os
from datetime import datetime

from gui.utils.styles import *


class NewsDayViewDialog(QDialog):
    """按日查看新闻，支持点击展开详情。"""

    def __init__(self, parent, title, news_list, kind="raw"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        hint = QLabel("点击左侧 ▶ 展开/收起单条新闻详情")
        hint.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["新闻列表"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setStyleSheet(TABLE_STYLE)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemClicked.connect(self._on_item_clicked)

        self.detail = QTextBrowser()
        self.detail.setStyleSheet(TEXTBROWSER_STYLE)
        self.detail.setPlaceholderText("选中一条新闻后在此显示完整内容")

        self._news_list = news_list or []
        self._kind = kind
        self._populate_tree()

        splitter.addWidget(self.tree)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        btn = QDialogButtonBox(QDialogButtonBox.Ok)
        btn.accepted.connect(self.accept)
        layout.addWidget(btn)

        if self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _populate_tree(self):
        self.tree.clear()
        if not self._news_list:
            empty = QTreeWidgetItem(["无数据"])
            empty.setFlags(Qt.NoItemFlags)
            self.tree.addTopLevelItem(empty)
            return

        for idx, news in enumerate(self._news_list, 1):
            summary = self._summary_text(news)
            top = QTreeWidgetItem([f"{idx}. {summary}"])
            top.setData(0, Qt.UserRole, idx - 1)
            top.setToolTip(0, summary)

            detail_item = QTreeWidgetItem(["查看详情…"])
            detail_item.setData(0, Qt.UserRole, idx - 1)
            top.addChild(detail_item)
            self.tree.addTopLevelItem(top)

    def _summary_text(self, news):
        ts = (
            news.get("datetime")
            or news.get("time")
            or news.get("published_at")
            or ""
        )
        title = news.get("title", "") or "（无标题）"
        return f"[{ts}] {title}"

    def _detail_html(self, news):
        if self._kind == "rejected":
            news_time = (
                news.get("datetime")
                or news.get("time")
                or news.get("published_at")
                or ""
            )
            rows = [
                ("新闻时间", news_time),
                ("标题", news.get("title", "")),
                ("原因", news.get("reason") or news.get("clean_reason", "")),
                ("ID", news.get("id", "")),
            ]
            content = (news.get("content") or "").strip()
            if content:
                rows.insert(3, ("正文", content))
            if news.get("source"):
                rows.insert(3, ("来源", news.get("source")))
        else:
            rows = [
                ("时间", news.get("datetime") or news.get("time", "")),
                ("来源", news.get("source", "")),
                ("标题", news.get("title", "")),
            ]
            if self._kind == "curated":
                rows.append((
                    "保留原因",
                    news.get("keep_reason") or news.get("clean_reason") or "",
                ))
            content = (news.get("content") or "").strip()
            if content:
                rows.append(("正文", content))

        parts = ['<div style="font-family: sans-serif; line-height: 1.6;">']
        for label, value in rows:
            safe = (
                str(value or "")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            parts.append(
                f'<p><b>{label}</b><br>{safe}</p>'
            )
        parts.append("</div>")
        return "".join(parts)

    def _show_news(self, index):
        if index is None or index < 0 or index >= len(self._news_list):
            self.detail.clear()
            return
        self.detail.setHtml(self._detail_html(self._news_list[index]))

    def _on_selection_changed(self):
        items = self.tree.selectedItems()
        if not items:
            return
        self._show_item_news(items[0])

    def _on_item_expanded(self, item):
        if item.parent() is None:
            self._show_item_news(item)

    def _on_item_clicked(self, item, _column):
        self._show_item_news(item)

    def _show_item_news(self, item):
        index = item.data(0, Qt.UserRole)
        if index is None:
            parent = item.parent()
            if parent is not None:
                index = parent.data(0, Qt.UserRole)
        self._show_news(index)


class DataPage(QWidget):
    """数据管理页面"""

    def __init__(self):
        super().__init__()
        self.current_type = "raw_db"
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        header = QHBoxLayout()
        title = QLabel("📁 数据管理")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        header.addWidget(title)
        header.addStretch()

        header.addWidget(QLabel("视图:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems([
            "原始库 (全部 raw_news)",
            "精选 (clean_status=curated)",
            "剔除 (clean_status=rejected)",
            "摘要 (Markdown)",
            "分析报告",
        ])
        self.type_combo.setStyleSheet(COMBOBOX_STYLE)
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        header.addWidget(self.type_combo)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setStyleSheet(BUTTON_PRIMARY)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self.file_table = QTableWidget()
        self.file_table.setColumnCount(5)
        self.file_table.setHorizontalHeaderLabels(
            ["日期/文件名", "条数", "时间范围", "说明", "操作"]
        )
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.file_table.setStyleSheet(TABLE_STYLE)
        self.file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.file_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.file_table)

        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        layout.addWidget(self.stats_label)

        self.refresh()

    def on_type_changed(self, index):
        type_map = {
            0: "raw_db",
            1: "curated_db",
            2: "rejected_db",
            3: "summaries_md",
            4: "analysis",
        }
        self.current_type = type_map.get(index, "raw_db")
        self.refresh()

    def refresh(self):
        self.file_table.setRowCount(0)
        if self.current_type == "raw_db":
            self._refresh_raw_db()
        elif self.current_type == "curated_db":
            self._refresh_curated_db()
        elif self.current_type == "rejected_db":
            self._refresh_rejected_db()
        elif self.current_type == "summaries_md":
            self._refresh_md_files("data/summaries", ".md", recursive=False)
        else:
            # AI 分析报告实际产出位置：data/AI_analysis/{月日}/*.md
            # 需要递归扫描子目录（原代码写成 data/analysis 且不递归，长期无数据）
            self._refresh_md_files("data/AI_analysis", ".md", recursive=True)

    def _refresh_raw_db(self):
        from services.storage import get_raw_store

        store = get_raw_store()
        stats = store.get_daily_stats()
        for item in stats:
            self._add_db_row(
                item["date"],
                item["count"],
                f"{item.get('time_start', '')} ~ {item.get('time_end', '')}",
                "原始库",
                "raw",
            )
        self.stats_label.setText(
            f"SQLite 原始库共 {store.count()} 条 | 待清洗 {store.count_uncleaned()} 条 | "
            f"按 {len(stats)} 天分布"
        )

    def _refresh_curated_db(self):
        from services.storage import CLEAN_CURATED, get_raw_store

        store = get_raw_store()
        stats = store.get_daily_stats(status=CLEAN_CURATED)
        for item in stats:
            self._add_db_row(
                item["date"],
                item["count"],
                f"{item.get('time_start', '')} ~ {item.get('time_end', '')}",
                "精选（clean_status=curated）",
                "curated",
            )
        self.stats_label.setText(
            f"精选共 {store.count_curated()} 条 | 按 {len(stats)} 天分布"
        )

    def _refresh_rejected_db(self):
        from services.storage import CLEAN_REJECTED, get_raw_store

        store = get_raw_store()
        stats = store.get_daily_stats(status=CLEAN_REJECTED)
        for item in stats:
            self._add_db_row(
                item["date"],
                item["count"],
                f"{item.get('time_start', '')} ~ {item.get('time_end', '')}",
                "剔除（clean_status=rejected）",
                "rejected",
            )
        self.stats_label.setText(
            f"剔除共 {store.count_rejected()} 条 | 按 {len(stats)} 天分布"
        )

    def _add_db_row(self, label, count, time_range, note, db_kind):
        row = self.file_table.rowCount()
        self.file_table.insertRow(row)
        self.file_table.setItem(row, 0, QTableWidgetItem(str(label)))
        self.file_table.setItem(row, 1, QTableWidgetItem(str(count)))
        self.file_table.setItem(row, 2, QTableWidgetItem(str(time_range)))
        self.file_table.setItem(row, 3, QTableWidgetItem(note))
        self.file_table.setCellWidget(
            row, 4, self._db_action_buttons(db_kind, str(label))
        )

    def _db_action_buttons(self, db_kind, date_str):
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(5, 2, 5, 2)
        view_btn = QPushButton("查看")
        # clicked 会传入 bool，不能直接用 lambda d=date 否则 d 会被覆盖为 False
        view_btn.clicked.connect(
            lambda _checked=False, k=db_kind, d=date_str: self._view_db_day(k, d)
        )
        del_btn = QPushButton("删除")
        del_btn.setStyleSheet("color: #ff4d4f;")
        del_btn.clicked.connect(
            lambda _checked=False, k=db_kind, d=date_str: self._delete_db_day(k, d)
        )
        layout.addWidget(view_btn)
        layout.addWidget(del_btn)
        return w

    def _refresh_md_files(self, directory, extension, recursive: bool = False):
        """扫描目录下的 .md 文件填充列表。

        Args:
            directory: 扫描根目录（项目相对路径）
            extension: 文件名后缀，如 ``.md``
            recursive: 是否递归子目录。AI 分析报告按 ``{月日}/`` 分子目录存放，
                必须 ``True``；summaries 单层平铺，``False`` 即可。
        """
        self.file_table.setRowCount(0)
        if not os.path.exists(directory):
            self.stats_label.setText("目录不存在")
            return

        files = []
        if recursive:
            for root, _dirs, filenames in os.walk(directory):
                for filename in filenames:
                    if not filename.endswith(extension):
                        continue
                    path = os.path.join(root, filename)
                    stat = os.stat(path)
                    rel = os.path.relpath(path, directory).replace("\\", "/")
                    files.append({
                        "name": rel,
                        "path": path,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "count": "-",
                    })
        else:
            for filename in os.listdir(directory):
                if not filename.endswith(extension):
                    continue
                path = os.path.join(directory, filename)
                stat = os.stat(path)
                files.append({
                    "name": filename,
                    "path": path,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "count": "-",
                })

        files.sort(key=lambda x: x["mtime"], reverse=True)
        for f in files:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)
            self.file_table.setItem(row, 0, QTableWidgetItem(f["name"]))
            self.file_table.setItem(row, 1, QTableWidgetItem(str(f["count"])))
            mtime = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            self.file_table.setItem(row, 2, QTableWidgetItem(mtime))
            self.file_table.setItem(row, 3, QTableWidgetItem(self._format_size(f["size"])))
            self.file_table.setCellWidget(
                row, 4,
                self._file_action_buttons(f["path"], f["name"]),
            )
        total = sum(f["size"] for f in files)
        self.stats_label.setText(
            f"文件 {len(files)} 个，总大小 {self._format_size(total)}"
        )

    def _file_action_buttons(self, file_path, file_name):
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(5, 2, 5, 2)
        view_btn = QPushButton("查看")
        view_btn.clicked.connect(
            lambda _checked=False, p=file_path: self._view_md_file(p)
        )
        del_btn = QPushButton("删除")
        del_btn.setStyleSheet("color: #ff4d4f;")
        del_btn.clicked.connect(
            lambda _checked=False, p=file_path, n=file_name: self._delete_md_file(p, n)
        )
        layout.addWidget(view_btn)
        layout.addWidget(del_btn)
        return w

    def _view_db_day(self, kind, date_str):
        try:
            from services.storage import (
                CLEAN_CURATED,
                CLEAN_REJECTED,
                get_raw_store,
            )

            store = get_raw_store()
            status_map = {"curated": CLEAN_CURATED, "rejected": CLEAN_REJECTED}
            title_map = {
                "raw": "原始库",
                "curated": "精选",
                "rejected": "剔除",
            }
            status = status_map.get(kind)
            news = store.get_news_for_date(date_str, status=status)
            title = f"{title_map.get(kind, kind)} {date_str} ({len(news)} 条)"

            dialog = NewsDayViewDialog(self, title, news, kind=kind)
            dialog.exec_()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _delete_db_day(self, kind, date_str):
        action_text = {
            "raw": "原始（物理删除当天全部新闻）",
            "curated": "精选标记（重置为未清洗，原文保留）",
            "rejected": "剔除标记（重置为未清洗，原文保留）",
        }.get(kind, "")
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定对 {date_str} 执行「{action_text}」吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from services.storage import (
                CLEAN_CURATED,
                CLEAN_REJECTED,
                get_raw_store,
            )

            store = get_raw_store()
            if kind == "raw":
                n = store.delete_by_date(date_str)
            elif kind == "curated":
                n = store.reset_clean_status_by_date(date_str, CLEAN_CURATED)
            else:
                n = store.reset_clean_status_by_date(date_str, CLEAN_REJECTED)
            QMessageBox.information(self, "成功", f"已处理 {n} 条")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _view_md_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            dialog = QDialog(self)
            dialog.setWindowTitle(os.path.basename(path))
            dialog.resize(900, 700)
            v = QVBoxLayout(dialog)
            b = QTextBrowser()
            b.setOpenExternalLinks(True)
            b.setMarkdown(content)
            b.setStyleSheet(TEXTBROWSER_STYLE)
            v.addWidget(b)
            btn = QDialogButtonBox(QDialogButtonBox.Ok)
            btn.accepted.connect(dialog.accept)
            v.addWidget(btn)
            dialog.exec_()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _delete_md_file(self, path, name):
        if QMessageBox.question(
            self, "确认", f"删除文件 {name}？", QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return
        try:
            os.remove(path)
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    @staticmethod
    def _format_size(num_bytes):
        if num_bytes < 1024:
            return f"{num_bytes} B"
        if num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.2f} KB"
        return f"{num_bytes / (1024 * 1024):.2f} MB"
