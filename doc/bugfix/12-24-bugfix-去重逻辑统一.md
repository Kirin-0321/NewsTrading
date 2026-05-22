# Bugfix - 去重逻辑统一

## 版本：v1.2
**修复日期：2025-12-23**

---

## 🐛 Bug描述

用户在数据管理中合并去重后导出文件，但在新闻清洗时仍然发现64条重复新闻。

### 用户反馈
```
已经在数据管理界面按过合并去重了
导出的是单一文件
为什么清洗时还显示：493 → 429 条（去除 64 条重复）？
```

---

## 🔍 问题分析

### 根本原因：不同模块的去重逻辑不一致

**1. 数据管理的合并去重** (`gui/pages/data_page.py`)
```python
unique_key = f"{news.get('datetime', '')}_{news.get('title', '')}"
```
❌ **只使用 `datetime` 字段**

**2. 数据导出功能** (`gui/pages/export_page.py`)
```python
time_str = news.get('datetime') or news.get('time', '')
unique_key = f"{time_str}_{news.get('title', '')}"
```
✅ **优先 `datetime`，其次 `time`**

**3. 新闻清洗功能** (`core/news_cleaner.py`)
```python
time = news.get('time', '')
key = f"{title}_{time}"
```
❌ **只使用 `time` 字段**

**4. 清洗数据合并** (`core/data_merger.py`)
```python
time = news.get('time', '')
key = f"{title}_{time}"
```
❌ **只使用 `time` 字段**

---

## 💥 问题场景

### 场景：部分新闻只有 `time` 字段，没有 `datetime` 字段

#### 步骤1：数据管理合并去重
```python
news1 = {"title": "标题A", "time": "2025-12-23 10:00:00"}
news2 = {"title": "标题A", "time": "2025-12-23 10:00:00"}

# 使用 datetime 字段去重
unique_key1 = "_标题A"  # datetime为空
unique_key2 = "_标题A"  # datetime为空

# 结果：被当作同一条新闻？不，因为key相同但实际上应该去重
# 但如果datetime都为空，可能判断逻辑有问题
```

**实际问题：**
- 当 `datetime` 为空时，`unique_key = "_标题"`
- 所有没有 `datetime` 的同标题新闻会有相同的key
- 但时间字段在 `time` 中，导致判断不准确

#### 步骤2：数据导出
```python
# 导出使用：datetime or time
time_str = news.get('datetime') or news.get('time', '')
# time_str = "2025-12-23 10:00:00"

# 可以正确区分，但上一步没去重，重复依然存在
```

#### 步骤3：新闻清洗
```python
# 清洗使用：time 字段
time = news.get('time', '')
key = "2025-12-23 10:00:00_标题A"

# 发现重复：493 → 429 条（64条重复）
```

---

## ✅ 解决方案

### 统一所有模块的时间字段获取逻辑

**统一规则：优先使用 `datetime`，如果没有就使用 `time`**

```python
time_str = news.get('datetime') or news.get('time', '')
unique_key = f"{time_str}_{news.get('title', '')}"
```

---

## 🔧 修改内容

### 1. 修改 `gui/pages/data_page.py`

**修改前：**
```python
# 使用 datetime+title 作为唯一标识去重
unique_key = f"{news.get('datetime', '')}_{news.get('title', '')}"
if unique_key not in news_by_date[date]:
    news_by_date[date][unique_key] = news
```

**修改后：**
```python
# 使用 时间+title 作为唯一标识去重
# 优先使用datetime，其次使用time（与导出和清洗逻辑一致）
time_str = news.get('datetime') or news.get('time', '')
unique_key = f"{time_str}_{news.get('title', '')}"
if unique_key not in news_by_date[date]:
    news_by_date[date][unique_key] = news
```

---

### 2. 修改 `core/news_cleaner.py`

**修改前：**
```python
def _deduplicate(self, news_list: List[Dict]) -> List[Dict]:
    """去重新闻（基于标题+时间）"""
    seen = set()
    unique = []
    
    for news in news_list:
        # 使用标题+时间作为唯一标识
        title = news.get('title', '')
        time = news.get('time', '')
        key = f"{title}_{time}"
        
        if key and key not in seen:
            seen.add(key)
            unique.append(news)
    
    return unique
```

**修改后：**
```python
def _deduplicate(self, news_list: List[Dict]) -> List[Dict]:
    """去重新闻（基于标题+时间）"""
    seen = set()
    unique = []
    
    for news in news_list:
        # 使用标题+时间作为唯一标识
        # 优先使用datetime，其次使用time（与导出和数据管理逻辑一致）
        title = news.get('title', '')
        time_str = news.get('datetime') or news.get('time', '')
        key = f"{time_str}_{title}"
        
        if key and key not in seen:
            seen.add(key)
            unique.append(news)
    
    return unique
```

---

### 3. 修改 `core/data_merger.py`

**修改前：**
```python
@staticmethod
def _deduplicate_news(news_list: List[Dict]) -> List[Dict]:
    """去重新闻（基于标题+时间）"""
    seen = set()
    unique = []
    
    for news in news_list:
        # 使用标题+时间作为唯一标识
        title = news.get('title', '')
        time = news.get('time', '')
        key = f"{title}_{time}"
        
        if key and key not in seen:
            seen.add(key)
            unique.append(news)
    
    return unique
```

**修改后：**
```python
@staticmethod
def _deduplicate_news(news_list: List[Dict]) -> List[Dict]:
    """去重新闻（基于标题+时间）"""
    seen = set()
    unique = []
    
    for news in news_list:
        # 使用标题+时间作为唯一标识
        # 优先使用datetime，其次使用time（与导出和数据管理逻辑一致）
        title = news.get('title', '')
        time_str = news.get('datetime') or news.get('time', '')
        key = f"{time_str}_{title}"
        
        if key and key not in seen:
            seen.add(key)
            unique.append(news)
    
    return unique
```

---

## ✅ 修复验证

### 测试步骤

1. **重新合并去重**
   - 打开【📁 数据管理】
   - 点击【🔀 合并去重】
   - 统计去重效果

2. **重新导出**
   - 打开【📤 数据导出】
   - 选择时间范围
   - 导出为单一文件

3. **清洗验证**
   - 打开【🧹 新闻清洗】
   - 选择导出的文件
   - 开始清洗

### 预期结果

**修复前：**
```
[21:50:03] 已加载 493 条新闻
[21:50:03] 正在合并去重...
[21:50:03] 去重完成：493 → 429 条（去除 64 条重复）
```
❌ 虽然已经合并去重，但还有64条重复

**修复后：**
```
[XX:XX:XX] 已加载 493 条新闻
[XX:XX:XX] 正在合并去重...
[XX:XX:XX] 去重完成：493 → 493 条（去除 0 条重复）
```
✅ 或者只有极少量重复（<5条）

---

## 📊 影响范围

### 受影响的功能
- ✅ 数据管理 - 合并去重
- ✅ 数据导出 - 自动去重
- ✅ 新闻清洗 - 预处理去重
- ✅ 清洗数据合并 - 去重

### 不受影响的功能
- ✅ 爬虫功能
- ✅ 新闻分析
- ✅ AI分析
- ✅ 定时任务

---

## 🔄 兼容性

### 向后兼容
- ✅ 不影响已有数据结构
- ✅ 新旧数据都能正确处理
- ✅ 用户无需重新爬取数据

### 建议操作
**对于已导出的文件：**
- 建议重新执行：合并去重 → 导出 → 清洗
- 确保数据完全去重

---

## 📝 技术细节

### 时间字段说明

**新闻数据中的时间字段：**
```json
{
  "title": "新闻标题",
  "datetime": "2025-12-23 10:00:00",  // 优先字段
  "time": "2025-12-23 10:00:00",      // 备用字段
  "content": "..."
}
```

**原因：**
- 早期版本使用 `time` 字段
- 后期版本改用 `datetime` 字段
- 系统中同时存在两种格式

**统一方案：**
```python
# 优先datetime，确保向后兼容
time_str = news.get('datetime') or news.get('time', '')
```

---

## 🎯 最佳实践

### 推荐工作流

```
爬取新闻
  ↓
【📁 数据管理】合并去重
  ↓
【📤 数据导出】导出单一文件
  ↓
【🧹 新闻清洗】AI筛选
  ↓
【🤖 AI分析】生成报告
```

**优势：**
- ✅ 每一步都正确去重
- ✅ 避免重复浪费资源
- ✅ 数据质量最高

---

## 🔮 未来优化

### 可能的改进
- [ ] 添加去重日志，记录去重详情
- [ ] 支持自定义去重规则
- [ ] 提供去重报告和统计
- [ ] 检测并修复历史重复数据

---

## 📚 相关文档

- [新闻清洗功能-使用指南](./新闻清洗功能-使用指南.md)
- [数据导出支持清洗数据-更新日志](./更新日志-数据导出支持清洗数据.md)

---

**开发者：** NewsBot Team  
**版本：** v1.2  
**日期：** 2025-12-23  
**Bug编号：** #001

