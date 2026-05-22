"""
数据管理页面
浏览和管理已爬取的数据文件
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QHeaderView, QComboBox, QMessageBox, QTextBrowser,
                             QDialog, QDialogButtonBox)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor

from gui.utils.styles import *
import os
import json
import logging
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)


class DataPage(QWidget):
    """数据管理页面"""
    
    def __init__(self):
        super().__init__()
        self.current_type = 'raw'
        self.init_ui()
        
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # 标题和工具栏
        header_layout = QHBoxLayout()
        
        title = QLabel("📁 数据管理")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        header_layout.addWidget(title)
        
        header_layout.addStretch()
        
        # 文件类型选择
        header_layout.addWidget(QLabel("文件类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["原始数据 (JSON)", "摘要 (Markdown)", "标题 (TXT)", "分析报告", "清洗后数据"])
        self.type_combo.setStyleSheet(COMBOBOX_STYLE)
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        header_layout.addWidget(self.type_combo)
        
        # 合并去重按钮（仅对原始数据）
        self.merge_btn = QPushButton("🔀 合并去重")
        self.merge_btn.setStyleSheet(BUTTON_SUCCESS)
        self.merge_btn.setToolTip("合并同一天的多个文件并去重")
        self.merge_btn.clicked.connect(self.merge_and_deduplicate)
        header_layout.addWidget(self.merge_btn)
        
        # 清洗数据专用合并按钮
        self.merge_cleaned_btn = QPushButton("🔀 合并去重（清洗数据）")
        self.merge_cleaned_btn.setStyleSheet(BUTTON_SUCCESS)
        self.merge_cleaned_btn.setToolTip("合并清洗后的数据，仅合并_clear.json文件")
        self.merge_cleaned_btn.clicked.connect(self.merge_cleaned_data)
        self.merge_cleaned_btn.setVisible(False)
        header_layout.addWidget(self.merge_cleaned_btn)
        
        # 刷新按钮
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setStyleSheet(BUTTON_PRIMARY)
        refresh_btn.clicked.connect(self.refresh)
        header_layout.addWidget(refresh_btn)
        
        layout.addLayout(header_layout)
        
        # 文件表格
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(5)  # 增加一列用于显示新闻数量
        self.file_table.setHorizontalHeaderLabels(["文件名", "新闻数", "大小", "修改时间", "操作"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.file_table.setStyleSheet(TABLE_STYLE)
        self.file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.file_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.file_table)
        
        # 统计信息
        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("color: #8c8c8c; font-size: 12px;")
        layout.addWidget(self.stats_label)
        
        # 初始加载
        self.refresh()
    
    def on_type_changed(self, index):
        """文件类型改变"""
        type_map = {
            0: 'raw',
            1: 'summaries_md',
            2: 'summaries_txt',
            3: 'analysis',
            4: 'cleaned'
        }
        self.current_type = type_map.get(index, 'raw')
        # 只有原始数据类型才显示合并按钮
        self.merge_btn.setVisible(self.current_type == 'raw')
        # 只有清洗数据类型才显示清洗数据合并按钮
        self.merge_cleaned_btn.setVisible(self.current_type == 'cleaned')
        self.refresh()
    
    def refresh(self):
        """刷新文件列表"""
        # 确定目录
        if self.current_type == 'raw':
            directory = 'data/raw'
            extension = '.json'
        elif self.current_type == 'summaries_md':
            directory = 'data/summaries'
            extension = '.md'
        elif self.current_type == 'summaries_txt':
            directory = 'data/summaries'
            extension = '_titles.txt'
        elif self.current_type == 'cleaned':
            directory = 'data/cleaned'
            extension = '.json'
        else:  # analysis
            directory = 'data/analysis'
            extension = '.md'
        
        # 清空表格
        self.file_table.setRowCount(0)
        
        if not os.path.exists(directory):
            self.stats_label.setText("目录不存在")
            return
        
        # 获取文件列表
        files = []
        for filename in os.listdir(directory):
            if filename.endswith(extension):
                filepath = os.path.join(directory, filename)
                stat = os.stat(filepath)
                
                # 如果是JSON文件，获取新闻数量
                news_count = None
                if extension == '.json':
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            if isinstance(data, dict) and 'news' in data:
                                news_count = len(data['news'])
                            elif isinstance(data, list):
                                news_count = len(data)
                    except:
                        pass
                
                files.append({
                    'name': filename,
                    'path': filepath,
                    'size': stat.st_size,
                    'mtime': stat.st_mtime,
                    'news_count': news_count
                })
        
        # 按修改时间排序
        files.sort(key=lambda x: x['mtime'], reverse=True)
        
        # 填充表格
        for file in files:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)
            
            # 文件名
            self.file_table.setItem(row, 0, QTableWidgetItem(file['name']))
            
            # 文件大小
            size_str = self.format_size(file['size'])
            self.file_table.setItem(row, 1, QTableWidgetItem(size_str))
            
            # 修改时间
            mtime_str = datetime.fromtimestamp(file['mtime']).strftime('%Y-%m-%d %H:%M:%S')
            self.file_table.setItem(row, 2, QTableWidgetItem(mtime_str))
            
            # 操作按钮
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(5, 2, 5, 2)
            btn_layout.setSpacing(5)
            
            view_btn = QPushButton("查看")
            view_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #d9d9d9; border-radius: 3px;")
            view_btn.clicked.connect(lambda checked, p=file['path']: self.view_file(p))
            btn_layout.addWidget(view_btn)
            
            delete_btn = QPushButton("删除")
            delete_btn.setStyleSheet("padding: 3px 8px; border: 1px solid #ff4d4f; color: #ff4d4f; border-radius: 3px;")
            delete_btn.clicked.connect(lambda checked, p=file['path'], n=file['name']: self.delete_file(p, n))
            btn_layout.addWidget(delete_btn)
            
            self.file_table.setCellWidget(row, 3, btn_widget)
        
        # 更新统计
        total_size = sum(f['size'] for f in files)
        self.stats_label.setText(
            f"共 {len(files)} 个文件，总大小: {self.format_size(total_size)}"
        )
    
    def format_size(self, bytes):
        """格式化文件大小"""
        if bytes < 1024:
            return f"{bytes} B"
        elif bytes < 1024 * 1024:
            return f"{bytes / 1024:.2f} KB"
        else:
            return f"{bytes / (1024 * 1024):.2f} MB"
    
    def view_file(self, filepath):
        """查看文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                if filepath.endswith('.json'):
                    data = json.load(f)
                    content = json.dumps(data, ensure_ascii=False, indent=2)
                else:
                    content = f.read()
            
            # 创建对话框显示内容
            dialog = QDialog(self)
            dialog.setWindowTitle(f"查看文件 - {os.path.basename(filepath)}")
            dialog.resize(800, 600)
            
            layout = QVBoxLayout(dialog)
            
            browser = QTextBrowser()
            browser.setPlainText(content)
            browser.setStyleSheet(TEXTBROWSER_STYLE)
            layout.addWidget(browser)
            
            btn_box = QDialogButtonBox(QDialogButtonBox.Ok)
            btn_box.accepted.connect(dialog.accept)
            layout.addWidget(btn_box)
            
            dialog.exec_()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"读取文件失败: {str(e)}")
    
    def delete_file(self, filepath, filename):
        """删除文件"""
        reply = QMessageBox.question(
            self, '确认删除',
            f"确定要删除文件 '{filename}' 吗？\n此操作不可恢复！",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                os.remove(filepath)
                QMessageBox.information(self, "成功", "文件已删除")
                self.refresh()
            except Exception as e:
                QMessageBox.critical(self, "错误", f"删除文件失败: {str(e)}")
    
    def merge_and_deduplicate(self):
        """合并并去重同一天的文件"""
        from PyQt5.QtWidgets import QProgressDialog
        from collections import OrderedDict
        
        reply = QMessageBox.question(
            self, '确认操作',
            '此操作将:\n'
            '1. 按日期分组所有JSON文件\n'
            '2. 合并同一天的文件并去重\n'
            '3. 删除旧文件，保留合并后的文件\n\n'
            '确定继续吗？',
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # 创建进度对话框
        progress = QProgressDialog("正在处理...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        
        try:
            raw_dir = 'data/raw'
            if not os.path.exists(raw_dir):
                QMessageBox.warning(self, "提示", "没有找到原始数据目录！")
                progress.close()
                return
            
            # 读取所有JSON文件
            progress.setLabelText("正在读取文件...")
            progress.setValue(10)
            
            all_files = [f for f in os.listdir(raw_dir) if f.endswith('.json')]
            if not all_files:
                QMessageBox.warning(self, "提示", "没有找到JSON文件！")
                progress.close()
                return
            
            # 按日期分组
            progress.setLabelText("正在按日期分组...")
            progress.setValue(20)
            
            news_by_date = {}
            file_groups = {}  # 记录每个日期对应的文件列表
            
            for filename in all_files:
                filepath = os.path.join(raw_dir, filename)
                
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    # 处理不同的JSON结构
                    if isinstance(data, dict) and 'news' in data:
                        news_list = data['news']
                    elif isinstance(data, list):
                        news_list = data
                    else:
                        continue
                    
                    # 按日期分组新闻
                    for news in news_list:
                        datetime_str = news.get('datetime', '')
                        if datetime_str:
                            date = datetime_str.split()[0]  # 获取日期部分
                            
                            if date not in news_by_date:
                                news_by_date[date] = OrderedDict()
                                file_groups[date] = []
                            
                            # 使用 时间+title 作为唯一标识去重
                            # 优先使用datetime，其次使用time（与导出和清洗逻辑一致）
                            time_str = news.get('datetime') or news.get('time', '')
                            unique_key = f"{time_str}_{news.get('title', '')}"
                            if unique_key not in news_by_date[date]:
                                news_by_date[date][unique_key] = news
                            
                            # 记录这个日期包含哪些源文件
                            if filename not in file_groups[date]:
                                file_groups[date].append(filename)
                
                except Exception as e:
                    print(f"读取文件 {filename} 失败: {e}")
                    continue
            
            if not news_by_date:
                QMessageBox.warning(self, "提示", "没有找到有效的新闻数据！")
                progress.close()
                return
            
            # 合并并保存
            progress.setLabelText("正在合并并保存...")
            progress.setValue(50)
            
            merged_count = 0
            deleted_count = 0
            total_before_dedup = 0
            total_after_dedup = 0
            
            for date, news_dict in news_by_date.items():
                # 转换为列表
                news_list = list(news_dict.values())
                before_count = len(news_list)
                total_before_dedup += before_count
                
                # 应用语义去重（10分钟窗口 + 80%相似度）
                from core.semantic_dedup import semantic_deduplicate
                news_list = semantic_deduplicate(news_list)
                after_count = len(news_list)
                total_after_dedup += after_count
                
                # 按时间排序
                news_list.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                
                # 生成文件名（使用日期）
                date_obj = datetime.strptime(date, '%Y-%m-%d')
                filename = f"{date_obj.strftime('%m-%d')}.json"
                filepath = os.path.join(raw_dir, filename)
                
                # 构建JSON结构
                output_data = {
                    "date": date,
                    "merge_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "total": len(news_list),
                    "source_files": file_groups[date],
                    "time_range": {
                        "start": news_list[-1].get('datetime', '') if news_list else '',
                        "end": news_list[0].get('datetime', '') if news_list else ''
                    },
                    "news": news_list
                }
                
                # 保存合并后的文件
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(output_data, f, ensure_ascii=False, indent=2)
                
                merged_count += 1
                
                # 删除旧文件（如果有多个源文件）
                if len(file_groups[date]) > 1:
                    for old_file in file_groups[date]:
                        old_path = os.path.join(raw_dir, old_file)
                        # 只删除不是目标文件名的文件
                        if old_file != filename and os.path.exists(old_path):
                            try:
                                os.remove(old_path)
                                deleted_count += 1
                            except Exception as e:
                                print(f"删除文件 {old_file} 失败: {e}")
            
            progress.setValue(100)
            progress.close()
            
            # 刷新列表
            self.refresh()
            
            # 计算去重统计
            dedup_count = total_before_dedup - total_after_dedup
            dedup_rate = (dedup_count / total_before_dedup * 100) if total_before_dedup > 0 else 0
            
            QMessageBox.information(
                self, "成功",
                f"合并完成！\n\n"
                f"📊 合并统计：\n"
                f"  处理日期数: {len(news_by_date)} 天\n"
                f"  生成文件数: {merged_count} 个\n"
                f"  删除旧文件: {deleted_count} 个\n\n"
                f"📈 去重统计：\n"
                f"  去重前: {total_before_dedup} 条\n"
                f"  去重后: {total_after_dedup} 条\n"
                f"  去除重复: {dedup_count} 条\n"
                f"  去重率: {dedup_rate:.1f}%"
            )
            
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "错误", f"合并失败: {str(e)}\n\n详细错误请查看控制台")
            import traceback
            print(traceback.format_exc())
    
    def merge_cleaned_data(self):
        """合并清洗后的数据并按天分割（仅处理_clear.json文件）"""
        try:
            from PyQt5.QtWidgets import QProgressDialog
            from core.data_merger import DataMerger
            
            cleaned_dir = 'data/cleaned'
            if not os.path.exists(cleaned_dir):
                QMessageBox.warning(self, "提示", "没有找到清洗数据目录！")
                return
            
            # 获取所有_clear.json文件
            clear_files = []
            for filename in os.listdir(cleaned_dir):
                if filename.endswith('_clear.json'):
                    clear_files.append(os.path.join(cleaned_dir, filename))
            
            if not clear_files:
                QMessageBox.warning(self, "提示", "没有找到可合并的清洗数据文件（_clear.json）！")
                return
            
            # 确认对话框
            reply = QMessageBox.question(
                self, "确认操作",
                f"找到 {len(clear_files)} 个清洗数据文件\n\n"
                f"将执行以下操作：\n"
                f"  1. 合并所有清洗文件\n"
                f"  2. 全局去重（5分钟窗口 + 60%相似度）\n"
                f"  3. 按天分割保存（格式: MM-DD_clear.json）\n"
                f"  4. 删除旧的_clear.json文件（保留_removed.json）\n\n"
                f"确定要继续吗？",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply != QMessageBox.Yes:
                return
            
            # 显示进度对话框
            progress = QProgressDialog("正在合并并按天分割...", "取消", 0, 100, self)
            progress.setWindowTitle("处理进度")
            progress.setWindowModality(Qt.WindowModal)
            progress.setValue(10)
            
            # 调用DataMerger进行按天分割
            result = DataMerger.merge_and_split_by_date(clear_files, cleaned_dir)
            
            progress.setValue(90)
            
            # 如果处理成功，刷新列表
            if result['success']:
                progress.setValue(100)
                progress.close()
                
                # 刷新列表
                self.refresh()
                
                # 构建详细统计信息
                dedup_rate = (result['duplicates_removed'] / result['total_news_before'] * 100) if result['total_news_before'] > 0 else 0
                
                # 生成每日文件列表
                daily_files_str = "\n".join([
                    f"    {item['filename']}: {item['count']} 条"
                    for item in result['daily_files']
                ])
                
                # 覆盖和删除信息
                overwritten_info = ""
                if result.get('overwritten_files'):
                    overwritten_info = f"\n  覆盖文件: {len(result['overwritten_files'])} 个"
                
                deleted_info = ""
                if result.get('deleted_files'):
                    deleted_info = f"\n  删除旧文件: {len(result['deleted_files'])} 个"
                
                QMessageBox.information(
                    self, "✅ 按天分割完成",
                    f"📂 处理统计：\n"
                    f"  源文件数: {result['total_files']} 个\n"
                    f"  分割天数: {result['days_count']} 天{overwritten_info}{deleted_info}\n\n"
                    f"📈 去重统计：\n"
                    f"  去重前: {result['total_news_before']} 条\n"
                    f"  去重后: {result['total_news_after']} 条\n"
                    f"  去除重复: {result['duplicates_removed']} 条\n"
                    f"  去重率: {dedup_rate:.1f}%\n\n"
                    f"📄 生成文件：\n"
                    f"{daily_files_str}"
                )
            else:
                progress.close()
                QMessageBox.warning(self, "提示", result['message'])
                
        except Exception as e:
            QMessageBox.critical(self, "错误", f"合并失败: {str(e)}")
            import traceback
            print(traceback.format_exc())

