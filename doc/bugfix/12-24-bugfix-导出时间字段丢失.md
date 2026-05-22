# 🐛 修复：导出文件时间字段丢失

## 问题描述

导出的文件中时间字段丢失：
- **TXT文件**: 只显示 `| 标题`，前面的时间为空
- **Markdown文件**: 时间显示为"未知"
- **JSON文件**: 正常（因为直接复制原始数据）

### 问题示例

**导出的TXT文件**:
```
 | Instacart就FTC案件达成6000万美元和解
 | 罔顾民众反对，密歇根州批准DTE能源公司...
 | 意大利财政部表示：2026年总体债券发行...
```

**期望结果**:
```
2025-12-19 23:59:38 | Instacart就FTC案件达成6000万美元和解
2025-12-19 23:58:14 | 罔顾民众反对，密歇根州批准...
2025-12-19 23:56:41 | 意大利财政部表示：2026年...
```

---

## 根本原因

### 字段名不匹配

**原始JSON数据结构**:
```json
{
  "news": [
    {
      "id": "456722810",
      "timestamp": 1766159978,
      "datetime": "2025-12-19 23:59:38",  ← 实际字段名
      "title": "...",
      "content": "...",
      "source": ""
    }
  ]
}
```

**代码中使用的字段**:
```python
time_str = news.get('time', '')  # ❌ 错误！字段名是 'datetime'
```

### 问题所在

1. 爬虫保存的时间字段名是 `datetime`
2. 但导出代码使用的字段名是 `time`
3. 导致读取时返回空字符串 `''`

---

## 解决方案

### 1. 新增辅助方法

```python
def get_news_time_str(self, news):
    """获取新闻的时间字符串（优先使用datetime，其次time）"""
    return news.get('datetime') or news.get('time', '')
```

**优势**:
- ✅ 优先使用 `datetime` 字段（爬虫实际使用的）
- ✅ 兼容旧数据（如果有 `time` 字段也能读取）
- ✅ 统一管理，避免多处重复代码

---

### 2. 修改所有相关代码

#### 修改点1: 数据读取和去重
```python
# 修改前
news_time = self.parse_news_time(news.get('time', ''))
unique_key = f"{news.get('time', '')}_{news.get('title', '')}"

# 修改后
time_str = self.get_news_time_str(news)
news_time = self.parse_news_time(time_str)
unique_key = f"{time_str}_{news.get('title', '')}"
```

#### 修改点2: 数据排序
```python
# 修改前
merged_news.sort(key=lambda x: self.parse_news_time(x.get('time', '')))

# 修改后
merged_news.sort(key=lambda x: self.parse_news_time(self.get_news_time_str(x)) or datetime.min)
```

#### 修改点3: 保存Markdown
```python
# 修改前
content.append(f"\n**时间**: {news.get('time', '未知')}")

# 修改后
content.append(f"\n**时间**: {self.get_news_time_str(news) or '未知'}")
```

#### 修改点4: 保存TXT
```python
# 修改前
time_str = news.get('time', '')

# 修改后
time_str = self.get_news_time_str(news)
```

---

## 修复效果

### 修复前
```txt
 | Instacart就FTC案件达成6000万美元和解
 | 罔顾民众反对，密歇根州批准...
```

### 修复后
```txt
2025-12-19 23:59:38 | Instacart就FTC案件达成6000万美元和解
2025-12-19 23:58:14 | 罔顾民众反对，密歇根州批准...
2025-12-19 23:56:41 | 意大利财政部表示：2026年...
```

---

## Markdown文件修复

### 修复前
```markdown
## 1. Instacart就FTC案件达成6000万美元和解

**时间**: 未知
**来源**: 
```

### 修复后
```markdown
## 1. Instacart就FTC案件达成6000万美元和解

**时间**: 2025-12-19 23:59:38
**来源**: 
```

---

## 兼容性保障

### 优先级机制
```python
return news.get('datetime') or news.get('time', '')
```

这种写法保证：
1. ✅ **优先使用 `datetime`** - 适配当前爬虫
2. ✅ **兼容 `time` 字段** - 如果将来字段名改变
3. ✅ **降级到空字符串** - 避免程序崩溃

### 向后兼容

| 数据来源 | datetime字段 | time字段 | 结果 |
|---------|-------------|---------|------|
| 当前爬虫 | ✅ 有 | ❌ 无 | 使用datetime ✅ |
| 旧版本 | ❌ 无 | ✅ 有 | 使用time ✅ |
| 未来版本 | ✅ 有 | ✅ 有 | 优先datetime ✅ |
| 异常数据 | ❌ 无 | ❌ 无 | 返回空字符串 ✅ |

---

## 测试验证

### 测试1: TXT文件
```bash
# 导出后检查
head -n 5 12-14_13_12-21_13_titles.txt

# 期望输出
2025-12-19 23:59:38 | Instacart就FTC案件达成6000万美元和解
2025-12-19 23:58:14 | 罔顾民众反对，密歇根州批准...
...
```

### 测试2: Markdown文件
```bash
# 检查时间字段
grep "**时间**:" 12-14_13_12-21_13_summary.md | head -n 3

# 期望输出
**时间**: 2025-12-19 23:59:38
**时间**: 2025-12-19 23:58:14
**时间**: 2025-12-19 23:56:41
```

### 测试3: 重新导出
1. 打开导出页面
2. 选择任意时间范围
3. 导出所有格式
4. 检查TXT和Markdown中是否有时间

---

## 相关文件

**修改文件**: `gui/pages/export_page.py`

**修改位置**:
- 第352-354行: 新增 `get_news_time_str` 方法
- 第256-280行: 修改数据读取逻辑
- 第290-292行: 修改排序逻辑
- 第315-319行: 修改时间处理逻辑
- 第396行: 修改Markdown保存
- 第412行: 修改TXT保存

---

## 学到的教训

### 1. 字段名规范
- 不同模块使用统一的字段名
- 建立数据字典文档
- 代码中使用常量定义字段名

### 2. 数据验证
- 导出后应该验证数据完整性
- 添加单元测试检查字段映射
- 记录数据结构的变化历史

### 3. 兼容性设计
- 使用优先级机制读取字段
- 提供降级方案
- 避免硬编码字段名

---

## 后续改进建议

### 建议1: 统一字段名
```python
# 在 core/config.py 中定义
NEWS_FIELD_NAMES = {
    'time': 'datetime',  # 时间字段
    'title': 'title',     # 标题字段
    'content': 'content', # 内容字段
    'source': 'source'    # 来源字段
}
```

### 建议2: 数据验证
```python
def validate_news_data(news):
    """验证新闻数据完整性"""
    required_fields = ['datetime', 'title']
    for field in required_fields:
        if not news.get(field):
            print(f"警告: 新闻缺少 {field} 字段")
    return True
```

### 建议3: 单元测试
```python
def test_export_time_field():
    """测试导出时间字段"""
    news = {
        'datetime': '2025-12-19 23:59:38',
        'title': '测试新闻'
    }
    exporter = ExportPage()
    time_str = exporter.get_news_time_str(news)
    assert time_str == '2025-12-19 23:59:38'
```

---

## ✅ 修复完成

**问题**: 导出文件时间丢失
**原因**: 字段名不匹配（`time` vs `datetime`）
**解决**: 新增统一方法获取时间字段
**效果**: 所有导出格式正常显示时间

**重新导出即可看到完整时间！** 🎉

