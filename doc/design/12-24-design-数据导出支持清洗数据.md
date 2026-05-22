# 需求确认 - 数据导出支持清洗数据

## 📋 需求背景

用户在清洗新闻后，希望能在数据导出界面中：
- 选择清洗后的数据作为数据源
- 按时间范围筛选清洗后的新闻
- 导出为 JSON/Markdown/TXT 格式

---

## 🎯 功能需求

### 1. 新增数据源选择

**位置：** 导出参数组中，时间范围选择之前

**UI设计：**
```
┌─ 导出参数 ───────────────────────────────────┐
│                                              │
│  📂 数据来源:                                │
│  ( ) 原始数据 (data/raw)                     │
│  (•) 清洗后数据 (data/cleaned)               │
│                                              │
│  ⏰ 时间范围:                                │
│  开始时间: [2025-12-19 13:00]               │
│  结束时间: [2025-12-23 20:00]               │
│  ...                                         │
└──────────────────────────────────────────────┘
```

**默认选项：** 原始数据（保持现有行为）

---

### 2. 数据加载逻辑

#### 原始数据模式（现有逻辑）
- 目录：`data/raw/`
- 文件类型：所有 `.json` 文件
- 筛选逻辑：按新闻的 `time` 字段筛选
- 去重逻辑：基于 `时间+标题`

#### 清洗数据模式（新增逻辑）
- 目录：`data/cleaned/`
- 文件类型：仅 `_clear.json` 文件（不读取 `_removed.json`）
- 筛选逻辑：按新闻的 `time` 字段筛选
- 去重逻辑：基于 `时间+标题`
- JSON结构：需处理带 `metadata` 的结构

---

### 3. JSON结构差异处理

**原始数据结构：**
```json
[
  {
    "title": "...",
    "time": "2025-12-23 20:00:00",
    "content": "...",
    ...
  }
]
```

**清洗数据结构：**
```json
{
  "metadata": {
    "type": "cleaned_single",
    "source_count": 2269,
    "kept_count": 1039,
    ...
  },
  "news": [
    {
      "title": "...",
      "time": "2025-12-23 20:00:00",
      "content": "...",
      ...
    }
  ]
}
```

**处理方案：**
- 检测JSON是否有 `metadata` 和 `news` 字段
- 如果有，读取 `news` 数组
- 如果没有，按原逻辑处理

---

### 4. 文件名生成

**原始数据导出：**
```
export_12-19_13_12-23_20/
  ├── 12-19_13_12-23_20.json
  ├── 12-19_13_12-23_20_summary.md
  └── 12-19_13_12-23_20_titles.txt
```

**清洗数据导出：**
```
export_12-19_13_12-23_20_cleaned/
  ├── 12-19_13_12-23_20_cleaned.json
  ├── 12-19_13_12-23_20_cleaned_summary.md
  └── 12-19_13_12-23_20_cleaned_titles.txt
```

**区别：**
- 目录名后缀：`_cleaned`
- 文件名后缀：`_cleaned`

---

### 5. 导出格式（保持不变）

#### JSON格式
```json
[
  {
    "title": "新闻标题",
    "time": "2025-12-23 20:00:00",
    "content": "新闻内容...",
    "source": "来源",
    "url": "链接"
  }
]
```

#### Markdown格式
```markdown
# 新闻摘要
**时间范围:** 2025-12-19 13:00 至 2025-12-23 20:00
**新闻数量:** 1039 条

---

## 新闻 1
**标题:** XXX
**时间:** 2025-12-23 20:00:00
**来源:** XXX
**内容:** ...
```

#### TXT格式
```
新闻标题 1
新闻标题 2
...
```

---

## 🔧 实现细节

### 修改文件
- `gui/pages/export_page.py` - 主要修改

### 修改点

#### 1. UI添加数据源选择
```python
# 在 create_params_group() 中添加
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
```

#### 2. 修改 load_and_merge_json()
```python
def load_and_merge_json(self, start_datetime, end_datetime):
    """读取并合并时间范围内的所有JSON数据"""
    
    # 根据选择确定数据源目录
    if self.cleaned_radio.isChecked():
        source_dir = 'data/cleaned'
        file_pattern = '_clear.json'  # 只读取_clear文件
    else:
        source_dir = 'data/raw'
        file_pattern = '.json'  # 所有json文件
    
    if not os.path.exists(source_dir):
        return []
    
    news_dict = OrderedDict()
    
    for filename in os.listdir(source_dir):
        if not filename.endswith(file_pattern):
            continue
        
        filepath = os.path.join(source_dir, filename)
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 处理清洗数据的特殊结构
            if isinstance(data, dict) and 'metadata' in data and 'news' in data:
                news_list = data['news']
            elif isinstance(data, list):
                news_list = data
            else:
                news_list = data.get('news', [])
            
            # 后续处理逻辑保持不变...
```

#### 3. 修改文件名生成
```python
def export_data(self, save_dir):
    """导出数据"""
    # ...
    
    # 确定文件名后缀
    if self.cleaned_radio.isChecked():
        suffix = "_cleaned"
    else:
        suffix = ""
    
    # 创建导出目录
    export_name = f"export_{start.strftime('%m-%d_%H')}_{end.strftime('%m-%d_%H')}{suffix}"
    export_path = os.path.join(save_dir, export_name)
    
    # 生成文件名
    filename_base = f"{start.strftime('%m-%d_%H')}_{end.strftime('%m-%d_%H')}{suffix}"
    
    # 后续保存逻辑保持不变...
```

---

## ✅ 测试用例

### 测试1：导出原始数据（现有功能）
**步骤：**
1. 选择"原始数据"
2. 设置时间范围：2025-12-19 13:00 ~ 2025-12-23 20:00
3. 勾选 JSON + Markdown + TXT
4. 点击"一键导出"

**预期：**
- 生成目录：`export_12-19_13_12-23_20/`
- 生成文件：3个（json, md, txt）
- 行为与现有功能完全一致

### 测试2：导出清洗数据（新功能）
**步骤：**
1. 选择"清洗后数据"
2. 设置时间范围：2025-12-19 13:00 ~ 2025-12-23 20:00
3. 勾选 JSON + Markdown + TXT
4. 点击"一键导出"

**预期：**
- 生成目录：`export_12-19_13_12-23_20_cleaned/`
- 生成文件：3个（json, md, txt）
- 仅包含 `_clear.json` 中的新闻
- 按时间范围正确筛选

### 测试3：清洗数据时间范围筛选
**场景：**
- 清洗文件时间范围：12-19 13:00 ~ 12-23 20:00
- 导出时间范围：12-20 00:00 ~ 12-22 23:59

**预期：**
- 仅导出 12-20 和 12-21 两天的新闻
- 正确统计新闻数量

### 测试4：清洗数据为空
**场景：**
- data/cleaned 目录为空或无 _clear.json

**预期：**
- 提示："没有找到任何新闻数据！请先进行新闻清洗。"

---

## 🚀 后续增强（可选）

### 增强1：数据源提示
```
□ 原始数据 (data/raw) - 共 2420 条新闻
□ 清洗后数据 (data/cleaned) - 共 1039 条新闻
```

### 增强2：导出统计信息
```
导出完成！

数据来源: 清洗后数据
原始数量: 1039 条
时间筛选: 845 条
去重后: 845 条

已生成 3 个文件：
✓ 12-20_00_12-22_23_cleaned.json
✓ 12-20_00_12-22_23_cleaned_summary.md
✓ 12-20_00_12-22_23_cleaned_titles.txt
```

### 增强3：Markdown头部添加来源标记
```markdown
# 新闻摘要

**数据来源:** 清洗后数据（AI筛选）
**时间范围:** 2025-12-20 00:00 至 2025-12-22 23:59
**新闻数量:** 845 条
```

---

## ❓ 需要确认的问题

### 问题1：是否需要合并多个清洗文件？
- **场景：** data/cleaned 中有多个 _clear.json 文件
- **选项A：** 自动合并所有 _clear.json 文件，然后按时间筛选
- **选项B：** 仅读取时间范围内的 _clear.json 文件

**建议：选项A** - 与原始数据逻辑一致，用户体验更好

### 问题2：文件名后缀是否需要？
- **选项A：** 加后缀 `_cleaned`，区分数据来源
- **选项B：** 不加后缀，保持一致

**建议：选项A** - 便于用户区分不同来源的导出文件

### 问题3：是否读取 _removed.json？
- **选项A：** 仅读取 _clear.json（被保留的新闻）
- **选项B：** 同时提供选项读取 _removed.json

**建议：选项A** - removed 的新闻已被AI判定为无价值，不应导出

### 问题4：导出按钮文案是否需要调整？
- **现有：** "⚡ 一键导出" / "📁 选择位置导出"
- **调整：** 根据数据源动态变化？

**建议：保持不变** - 功能一致，无需调整

---

## 📝 实现计划

### 阶段1：基础功能（预计30分钟）
1. ✅ 添加数据源选择UI
2. ✅ 修改 load_and_merge_json() 支持清洗数据
3. ✅ 修改文件名生成逻辑
4. ✅ 基础测试

### 阶段2：优化增强（预计15分钟）
1. ✅ 添加数据来源标记
2. ✅ 优化提示信息
3. ✅ 完整测试

### 阶段3：文档更新（预计10分钟）
1. ✅ 更新使用说明
2. ✅ 创建更新日志

---

## 🎯 总结

这是一个相对简单的功能增强，主要工作是：
1. **UI层面：** 添加单选按钮选择数据源
2. **逻辑层面：** 根据选择读取不同目录的文件
3. **兼容性：** 处理清洗数据的特殊JSON结构
4. **命名规范：** 文件名加上 `_cleaned` 后缀

**核心原则：**
- 保持现有功能不变
- 新增功能逻辑与现有一致
- 用户体验流畅自然

---

**请确认以上需求是否理解正确？有任何调整或补充请告知！**

