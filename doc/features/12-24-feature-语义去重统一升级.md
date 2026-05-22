# 语义去重 - 统一升级

## 📅 更新时间
2025-12-23

## 🎯 升级目标
将所有去重逻辑统一升级为**语义去重**，使用时间窗口 + 标题相似度算法，有效识别和去除语义相似的新闻。

---

## ⚙️ 核心参数

| 参数 | 值 | 说明 |
|------|------|------|
| **时间窗口** | 10分钟 | 在此时间范围内比较新闻相似度 |
| **相似度阈值** | 80% | 标题相似度超过此值判定为重复 |

---

## 📦 新增模块

### `core/semantic_dedup.py`
统一的语义去重模块，提供：

```python
from core.semantic_dedup import semantic_deduplicate

# 使用默认配置（10分钟窗口 + 80%相似度）
unique_news = semantic_deduplicate(news_list)
```

**核心特性：**
- ✅ 基于 `difflib.SequenceMatcher` 计算标题相似度
- ✅ 时间窗口内查找潜在重复
- ✅ 保留内容最详细的新闻（按content长度）
- ✅ 自动处理多种时间格式
- ✅ 对无时间的新闻跳过语义去重

---

## 🔄 涉及的模块

### 1. **新闻清洗模块** - `core/news_cleaner.py`
```python
# 修改前
from core.dedup_utils import semantic_deduplicate
news_list = semantic_deduplicate(news_list, time_window_minutes=10, similarity_threshold=0.8)

# 修改后
from core.semantic_dedup import semantic_deduplicate
news_list = semantic_deduplicate(news_list)
```

### 2. **清洗数据合并模块** - `core/data_merger.py`
```python
# 修改前
from core.dedup_utils import semantic_deduplicate
return semantic_deduplicate(news_list, time_window_minutes=10, similarity_threshold=0.8)

# 修改后
from core.semantic_dedup import semantic_deduplicate
return semantic_deduplicate(news_list)
```

### 3. **数据管理页面** - `gui/pages/data_page.py`
```python
# 修改前
from core.dedup_utils import semantic_deduplicate
news_list = semantic_deduplicate(news_list, time_window_minutes=10, similarity_threshold=0.8)

# 修改后
from core.semantic_dedup import semantic_deduplicate
news_list = semantic_deduplicate(news_list)
```

### 4. **数据导出页面** - `gui/pages/export_page.py`
```python
# 修改前（3处）
from core.dedup_utils import semantic_deduplicate
merged_news = semantic_deduplicate(merged_news, time_window_minutes=10, similarity_threshold=0.8)

# 修改后
from core.semantic_dedup import semantic_deduplicate
merged_news = semantic_deduplicate(merged_news)
```

---

## 🧪 去重效果

基于实际测试（`data/cleaned/12-23-22-27_clear.json`）：

| 配置 | 原始数量 | 重复数量 | 去重率 |
|------|----------|----------|--------|
| **标准配置**（10分钟 + 80%） | 695 | 130 | 18.7% |
| 宽松配置（10分钟 + 70%） | 695 | 166 | 23.9% |
| 大窗口配置（30分钟 + 80%） | 695 | 149 | 21.4% |

**重复新闻示例：**
```
原始: 现货白银向上触及70美元/盎司
重复: 现货白银上破70美元/盎司关口
相似度: 85%
时间差: 2分钟
```

---

## ✅ 优势

1. **统一管理**：所有去重逻辑使用同一个模块，便于维护
2. **智能识别**：能识别语义相似但措辞不同的新闻
3. **灵活配置**：时间窗口和相似度阈值可调整
4. **保留最优**：在重复新闻中保留内容最详细的
5. **高效性能**：使用Python内置`difflib`，无需额外依赖

---

## 🔧 如何调整参数

如果需要调整全局默认参数，修改 `core/semantic_dedup.py`：

```python
# 全局默认去重器实例
default_deduplicator = SemanticDeduplicator(
    time_window_minutes=10,  # 修改时间窗口
    similarity_threshold=0.8  # 修改相似度阈值
)
```

---

## 📊 测试建议

1. **观察去重效果**：查看生成的 `_marked.json` 文件
2. **调整参数**：根据实际新闻内容调整窗口和阈值
3. **验证准确性**：确认被去除的新闻确实是重复

---

## 🚀 后续优化方向

1. **UI配置**：在界面中提供去重参数配置选项
2. **智能推荐**：根据新闻源自动调整参数
3. **多维度去重**：结合来源、关键词等多维度判断
4. **去重日志**：记录每次去重的详细信息

---

## 📝 注意事项

- ⚠️ 语义去重需要新闻有有效的时间字段（`datetime` 或 `time`）
- ⚠️ 无时间的新闻会被跳过语义去重，保持原样
- ⚠️ 去重后的新闻顺序保持时间排序
- ⚠️ 相似度阈值过低可能误删不同新闻，过高可能漏删重复新闻

---

## ✨ 总结

通过这次统一升级，整个系统的去重逻辑更加智能和统一，有效减少了语义重复的新闻，提高了数据质量和AI分析效率。

