"""
数据导出页面
按时间范围导出数据（读取JSON、去重、合并、生成单一文件）
"""

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QGroupBox, QDateTimeEdit, QCheckBox,
                             QMessageBox, QFileDialog, QProgressDialog,
                             QRadioButton, QButtonGroup)
from PyQt5.QtCore import Qt, QDateTime, QTime

from gui.utils.styles import *
import os
import json
from datetime import datetime
from collections import OrderedDict


class ExportPage(QWidget):
    """数据导出页面"""
    
    def __init__(self):
        super().__init__()
        self.init_ui()
        
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # 标题
        title = QLabel("📤 数据导出")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)
        
        # 导出参数
        params_group = self.create_params_group()
        layout.addWidget(params_group)
        
        # 导出按钮
        export_layout = QHBoxLayout()
        
        # 一键导出按钮（使用默认位置）
        self.quick_export_btn = QPushButton("⚡ 一键导出")
        self.quick_export_btn.setStyleSheet(BUTTON_SUCCESS)
        self.quick_export_btn.setMinimumHeight(40)
        self.quick_export_btn.setToolTip("直接导出到 data/exports 目录")
        self.quick_export_btn.clicked.connect(self.quick_export)
        export_layout.addWidget(self.quick_export_btn)
        
        # 选择位置导出按钮
        self.export_btn = QPushButton("📁 选择位置导出")
        self.export_btn.setStyleSheet(BUTTON_PRIMARY)
        self.export_btn.setMinimumHeight(40)
        self.export_btn.setToolTip("选择自定义保存位置")
        self.export_btn.clicked.connect(self.export_data_with_dialog)
        export_layout.addWidget(self.export_btn)
        
        export_layout.addStretch()
        
        layout.addLayout(export_layout)
        
        # 默认导出位置提示
        default_path = os.path.abspath('data/exports')
        path_label = QLabel(f"💾 默认导出位置: <code>{default_path}</code>")
        path_label.setStyleSheet("""
            background-color: #f5f5f5;
            border: 1px solid #d9d9d9;
            border-radius: 4px;
            padding: 8px 12px;
            color: #595959;
            font-size: 12px;
        """)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(path_label)
        
        # 说明
        info_label = QLabel("""
        <b>使用说明：</b><br>
        1. 选择要导出的时间范围（精确到分钟，默认昨日14:30至当前时间+1小时）<br>
        2. 选择要导出的文件类型（可多选）<br>
        3. 点击"⚡一键导出"直接导出到默认位置（data/exports）<br>
        4. 或点击"📁选择位置导出"自定义保存位置<br>
        5. 系统会自动读取所有JSON，按新闻时间筛选、去重、合并为单一文件
        """)
        info_label.setStyleSheet("""
            background-color: #e6f7ff;
            border: 1px solid #91d5ff;
            border-radius: 4px;
            padding: 15px;
            color: #262626;
        """)
        layout.addWidget(info_label)
        
        layout.addStretch()
        
    def create_params_group(self):
        """创建参数设置组"""
        group = QGroupBox("导出参数")
        group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        layout.setSpacing(15)
        
        # 数据来源选择
        source_layout = QHBoxLayout()
        source_layout.addWidget(QLabel("数据来源:"))
        
        self.source_group = QButtonGroup()
        self.raw_radio = QRadioButton("原始数据 (data/raw)")
        self.cleaned_radio = QRadioButton("清洗后数据 (data/cleaned)")
        self.raw_radio.setChecked(True)
        
        self.source_group.addButton(self.raw_radio)
        self.source_group.addButton(self.cleaned_radio)
        
        source_layout.addWidget(self.raw_radio)
        source_layout.addWidget(self.cleaned_radio)
        source_layout.addStretch()
        layout.addLayout(source_layout)
        
        # 日期时间范围
        date_layout = QHBoxLayout()
        date_layout.addWidget(QLabel("时间范围:"))
        
        self.start_datetime = QDateTimeEdit()
        self.start_datetime.setCalendarPopup(True)
        self.start_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        # 默认：昨日14:30
        yesterday_1430 = QDateTime.currentDateTime().addDays(-1)
        yesterday_1430.setTime(QTime(14, 30))
        self.start_datetime.setDateTime(yesterday_1430)
        self.start_datetime.setStyleSheet(INPUT_STYLE)
        date_layout.addWidget(self.start_datetime)
        
        date_layout.addWidget(QLabel("至"))
        
        self.end_datetime = QDateTimeEdit()
        self.end_datetime.setCalendarPopup(True)
        self.end_datetime.setDisplayFormat("yyyy-MM-dd HH:mm")
        # 默认：当前时间+1小时
        self.end_datetime.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.end_datetime.setStyleSheet(INPUT_STYLE)
        date_layout.addWidget(self.end_datetime)
        
        date_layout.addStretch()
        layout.addLayout(date_layout)
        
        # 文件类型选择
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("导出类型:"))
        
        self.export_json = QCheckBox("JSON")
        self.export_json.setChecked(True)
        type_layout.addWidget(self.export_json)
        
        self.export_md = QCheckBox("Markdown")
        self.export_md.setChecked(True)
        type_layout.addWidget(self.export_md)
        
        self.export_txt = QCheckBox("TXT")
        self.export_txt.setChecked(True)
        type_layout.addWidget(self.export_txt)
        
        type_layout.addStretch()
        layout.addLayout(type_layout)
        
        group.setLayout(layout)
        return group
    
    def quick_export(self):
        """一键导出（使用默认位置）"""
        # 使用默认位置
        default_dir = os.path.abspath('data/exports')
        os.makedirs(default_dir, exist_ok=True)
        self.export_data(default_dir)
    
    def export_data_with_dialog(self):
        """导出数据（选择保存位置）"""
        # 选择保存目录
        default_dir = os.path.abspath('data/exports')
        save_dir = QFileDialog.getExistingDirectory(
            self,
            "选择导出目录",
            default_dir,
            QFileDialog.ShowDirsOnly
        )
        
        if not save_dir:
            return
        
        self.export_data(save_dir)
    
    def export_data(self, save_dir):
        """导出数据（读取JSON、去重、合并）"""
        # 检查至少选择一种类型
        if not (self.export_json.isChecked() or self.export_md.isChecked() or self.export_txt.isChecked()):
            QMessageBox.warning(self, "警告", "请至少选择一种导出类型！")
            return
        
        # 创建进度对话框
        progress = QProgressDialog("正在处理数据...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        
        try:
            # 获取日期时间范围
            start = self.start_datetime.dateTime().toPyDateTime()
            end = self.end_datetime.dateTime().toPyDateTime()
            
            progress.setLabelText("正在读取JSON文件...")
            progress.setValue(10)
            
            # 读取并合并所有JSON数据
            merged_news = self.load_and_merge_json(start, end)
            
            if not merged_news:
                if self.cleaned_radio.isChecked():
                    QMessageBox.warning(self, "提示", "没有找到任何清洗数据！\n\n请先在【🧹 新闻清洗】页面进行新闻清洗。")
                else:
                    QMessageBox.warning(self, "提示", "没有找到任何新闻数据！\n\n请先运行爬虫采集数据。")
                progress.close()
                return
            
            # 计算实际时间范围
            actual_times = [self.parse_news_time(self.get_news_time_str(n)) for n in merged_news]
            actual_times = [t for t in actual_times if t]
            
            if actual_times:
                actual_start = min(actual_times)
                actual_end = max(actual_times)
                print(f"实际导出时间范围: {actual_start} 至 {actual_end}")
            else:
                print("无法确定实际时间范围")
            
            progress.setLabelText(f"已读取 {len(merged_news)} 条新闻，正在生成文件...")
            progress.setValue(50)
            
            # 确定文件名后缀
            if self.cleaned_radio.isChecked():
                suffix = "_cleaned"
            else:
                suffix = ""
            
            # 创建导出目录
            export_name = f"export_{start.strftime('%m-%d_%H')}_{end.strftime('%m-%d_%H')}{suffix}"
            export_path = os.path.join(save_dir, export_name)
            os.makedirs(export_path, exist_ok=True)
            
            # 生成文件名
            filename_base = f"{start.strftime('%m-%d_%H')}_{end.strftime('%m-%d_%H')}{suffix}"
            
            exported_files = []
            
            # 导出 JSON
            if self.export_json.isChecked():
                json_file = os.path.join(export_path, f"{filename_base}.json")
                self.save_json(merged_news, json_file)
                exported_files.append(json_file)
            
            progress.setValue(70)
            
            # 导出 Markdown
            if self.export_md.isChecked():
                md_file = os.path.join(export_path, f"{filename_base}_summary.md")
                self.save_markdown(merged_news, md_file, start, end)
                exported_files.append(md_file)
            
            progress.setValue(85)
            
            # 导出 TXT
            if self.export_txt.isChecked():
                txt_file = os.path.join(export_path, f"{filename_base}_titles.txt")
                self.save_txt(merged_news, txt_file)
                exported_files.append(txt_file)
            
            progress.setValue(100)
            progress.close()
            
            # 构建成功消息
            data_source = "清洗后数据（AI筛选）" if self.cleaned_radio.isChecked() else "原始数据"
            success_msg = f"导出完成！\n\n数据来源: {data_source}\n共 {len(merged_news)} 条新闻\n生成 {len(exported_files)} 个文件\n"
            
            # 显示实际时间范围
            if actual_times:
                actual_start = min(actual_times)
                actual_end = max(actual_times)
                success_msg += f"\n目标时间: {start.strftime('%m-%d %H:%M')} 至 {end.strftime('%m-%d %H:%M')}"
                success_msg += f"\n实际时间: {actual_start.strftime('%m-%d %H:%M')} 至 {actual_end.strftime('%m-%d %H:%M')}"
            
            success_msg += f"\n\n保存位置: {export_path}"
            
            QMessageBox.information(self, "成功", success_msg)
            
            # 打开导出目录
            os.startfile(export_path)
            
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "错误", f"导出失败: {str(e)}\n\n详细错误请查看控制台")
            import traceback
            print(traceback.format_exc())
    
    def load_and_merge_json(self, start_datetime, end_datetime):
        """读取并合并时间范围内的所有JSON数据（去重，智能筛选）"""
        # 根据选择确定数据源目录和文件模式
        if self.cleaned_radio.isChecked():
            source_dir = 'data/cleaned'
            file_pattern = '_clear.json'  # 只读取_clear文件
        else:
            source_dir = 'data/raw'
            file_pattern = '.json'  # 所有json文件
        
        if not os.path.exists(source_dir):
            return []
        
        # 用于去重的字典（key: 新闻唯一标识, value: 新闻数据）
        news_dict = OrderedDict()
        all_news_dict = OrderedDict()  # 存储所有新闻（用于备选）
        
        # 遍历所有JSON文件
        for filename in os.listdir(source_dir):
            if not filename.endswith(file_pattern):
                continue
            
            filepath = os.path.join(source_dir, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 处理不同的JSON结构
                # 1. 清洗数据结构：{'metadata': {...}, 'news': [...]}
                # 2. 原始列表结构：[...]
                # 3. 原始字典结构：{'news': [...]}
                if isinstance(data, dict) and 'metadata' in data and 'news' in data:
                    news_list = data['news']
                elif isinstance(data, list):
                    news_list = data
                else:
                    news_list = data.get('news', [])
                
                for news in news_list:
                    # 获取时间字符串
                    time_str = self.get_news_time_str(news)
                    
                    # 解析新闻时间
                    news_time = self.parse_news_time(time_str)
                    if not news_time:
                        # 即使无法解析时间，也保存到all_news_dict
                        unique_key = f"unknown_{news.get('title', '')}"
                        if unique_key not in all_news_dict:
                            all_news_dict[unique_key] = news
                        continue
                    
                    # 生成唯一标识（时间+标题）
                    unique_key = f"{time_str}_{news.get('title', '')}"
                    
                    # 存储所有新闻（用于备选）
                    if unique_key not in all_news_dict:
                        all_news_dict[unique_key] = news
                    
                    # 检查是否在时间范围内（宽松匹配）
                    # 只要新闻时间与目标时间段有交集就包含
                    if start_datetime <= news_time <= end_datetime:
                        # 去重：只保留第一次出现的
                        if unique_key not in news_dict:
                            news_dict[unique_key] = news
                
            except Exception as e:
                print(f"读取文件 {filename} 失败: {e}")
                continue
        
        # 如果严格匹配有结果，返回严格匹配的
        if news_dict:
            merged_news = list(news_dict.values())
            # 应用语义去重（5分钟窗口 + 60%相似度）
            from core.semantic_dedup import semantic_deduplicate
            merged_news = semantic_deduplicate(merged_news)
            merged_news.sort(key=lambda x: self.parse_news_time(self.get_news_time_str(x)) or datetime.min, reverse=True)
            return merged_news
        
        # 如果严格匹配没有结果，使用宽松策略
        print(f"严格匹配没有结果，使用宽松策略...")
        
        # 策略1: 找出最接近的新闻（时间上最近的）
        news_with_time = []
        for news in all_news_dict.values():
            time_str = self.get_news_time_str(news)
            news_time = self.parse_news_time(time_str)
            if news_time:
                news_with_time.append((news, news_time))
        
        if not news_with_time:
            # 如果所有新闻都没有有效时间，返回所有新闻
            print("没有找到有效时间的新闻，返回所有数据")
            all_news = list(all_news_dict.values())
            # 应用语义去重（5分钟窗口 + 60%相似度）
            from core.semantic_dedup import semantic_deduplicate
            all_news = semantic_deduplicate(all_news)
            return all_news
        
        # 按时间排序
        news_with_time.sort(key=lambda x: x[1], reverse=True)
        
        # 找出在目标时间前后的新闻（扩展时间范围）
        result_news = []
        for news, news_time in news_with_time:
            # 如果新闻时间在目标时间段附近（前后扩展50%）
            time_range = end_datetime - start_datetime
            extended_start = start_datetime - time_range * 0.5
            extended_end = end_datetime + time_range * 0.5
            
            if extended_start <= news_time <= extended_end:
                result_news.append(news)
        
        # 如果扩展后还是没有，就返回最新的新闻
        if not result_news:
            print("扩展时间范围后仍无结果，返回最新的新闻")
            # 返回最新的一批新闻（最多100条）
            result_news = [news for news, _ in news_with_time[:100]]
        
        # 应用语义去重（5分钟窗口 + 60%相似度）
        from core.semantic_dedup import semantic_deduplicate
        result_news = semantic_deduplicate(result_news)
        
        print(f"宽松策略找到 {len(result_news)} 条新闻")
        return result_news
    
    def get_news_time_str(self, news):
        """获取新闻的时间字符串（优先使用datetime，其次time）"""
        return news.get('datetime') or news.get('time', '')
    
    def parse_news_time(self, time_str):
        """解析新闻时间字符串"""
        if not time_str:
            return None
        
        try:
            # 尝试多种时间格式
            formats = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%m-%d %H:%M',
                '%m月%d日 %H:%M',
            ]
            
            for fmt in formats:
                try:
                    dt = datetime.strptime(time_str, fmt)
                    # 如果没有年份，使用当前年份
                    if '%Y' not in fmt:
                        dt = dt.replace(year=datetime.now().year)
                    return dt
                except:
                    continue
            
            return None
        except:
            return None
    
    def save_json(self, news_list, filepath):
        """保存JSON文件"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
    
    def save_markdown(self, news_list, filepath, start_time, end_time):
        """保存Markdown摘要文件"""
        content = []
        content.append(f"# 新闻摘要")
        
        # 添加数据来源标记
        data_source = "清洗后数据（AI筛选）" if self.cleaned_radio.isChecked() else "原始数据"
        content.append(f"\n**数据来源**: {data_source}")
        content.append(f"\n**导出时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 从实际新闻中提取时间范围（第一条和最后一条新闻的时间）
        actual_times = [self.parse_news_time(self.get_news_time_str(n)) for n in news_list]
        actual_times = [t for t in actual_times if t]
        
        if actual_times:
            actual_start = min(actual_times)
            actual_end = max(actual_times)
            content.append(f"\n**时间范围**: {actual_start.strftime('%Y-%m-%d %H:%M')} 至 {actual_end.strftime('%Y-%m-%d %H:%M')}")
        else:
            # 如果无法获取实际时间，使用导出时间范围
            content.append(f"\n**时间范围**: {start_time.strftime('%Y-%m-%d %H:%M')} 至 {end_time.strftime('%Y-%m-%d %H:%M')}")
        
        content.append(f"\n**新闻总数**: {len(news_list)} 条")
        content.append("\n---\n")
        
        for idx, news in enumerate(news_list, 1):
            content.append(f"## {idx}. {news.get('title', '无标题')}")
            content.append(f"\n**时间**: {self.get_news_time_str(news) or '未知'}")
            content.append(f"\n**来源**: {news.get('source', '未知')}")
            
            content_text = news.get('content', '')
            if content_text:
                content.append(f"\n**内容**:\n{content_text}")
            
            content.append("\n---\n")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(content))
    
    def save_txt(self, news_list, filepath):
        """保存TXT标题文件（极简格式）"""
        lines = []
        for news in news_list:
            time_str = self.get_news_time_str(news)
            title = news.get('title', '')
            lines.append(f"{time_str} | {title}")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

