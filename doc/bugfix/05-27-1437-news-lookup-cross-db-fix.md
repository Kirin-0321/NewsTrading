# 题材预测库「新闻反查失败」修复

> 报告时间：2026-05-27 14:37  
> 报告人：辉夜  
> 影响版本：Phase -1 三库重构完成后（2026-05-26 起）  
> 影响范围：GUI「题材预测库」页面下方的「新闻索引」面板  
> 严重程度：**High**（核心展示功能不可用，不影响落库与打分）

---

## 一、问题现象

主人在 GUI「题材预测库」选中某条题材后，下方「新闻索引」面板的所有行都显示：

| 新闻编号 | 关联类型 | 时间 | 来源 | 标题（反查 raw_news） |
|---------|---------|------|------|---------------------|
| 新闻186 |    -     |  -   |  -   | （未反查到，可能 news_id 缺失） |
| 新闻210 |    -     |  -   |  -   | （未反查到，可能 news_id 缺失） |
| 新闻228 |    -     |  -   |  -   | （未反查到，可能 news_id 缺失） |

但 `theme_news.news_id` 字段实际是有值的，比如：

```
news_id='490068310'        → 国家能源局召开全国"人工智能+"能源现场推进会
news_id='20260527375008394604' → AI眼镜新品频发 产业链追"光"逐"芯"
```

**右下角"原文/正文"区也全部空白。**

## 二、根因定位

Phase -1（2026-05-26）做三库重构时，把 `gui/pages/theme_prediction_page.py` 内
`_fetch_news_meta` 的连接从 `news.db` 误批量替换成了 `ai_inference.db`：

```python
# Bug 版（Phase -1 重构后误改）
from services.storage.ai_inference_db import get_ai_inference_db
with get_ai_inference_db().connect() as conn:
    rows = conn.execute(
        "SELECT id, title, content, source, published_at "
        "FROM raw_news WHERE id IN (...)",
        ids,
    ).fetchall()
```

但 `raw_news` 表**始终在 `news.db`**（属于原始新闻数据，归 news 库管），
`ai_inference.db` 里**根本不存在 `raw_news` 这张表**。

`_fetch_news_meta` 又用 `try/except Exception: return {}` 把 SQL 异常静默吞掉，
导致函数永远返回空 dict，UI 全部回退到「未反查到」文案。

## 三、修复方案

把连接改回 `get_connection()`（news.db）即可。raw_news 跨库 JOIN 在
backtest snapshot 等场景另有 `attached_dbs(...)` 工具，这里只是简单单表反查，
不需要 ATTACH。

```python
# 修复版（gui/pages/theme_prediction_page.py L704-L735）
@staticmethod
def _fetch_news_meta(news_ids: list) -> dict:
    """通过 news_id 从 raw_news 反查 title/source/published_at/content。

    注意：raw_news 表在 ``news.db``，不在 ``ai_inference.db``。
    Phase -1 三库重构曾把这里误改成 ai_inference 连接，导致 GUI
    新闻反查全部走空（2026-05-27 bug 修复）。
    """
    ids = [i for i in news_ids if i]
    if not ids:
        return {}
    try:
        from services.storage.database import get_connection
        placeholders = ",".join("?" for _ in ids)
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, content, source, published_at
                FROM raw_news
                WHERE id IN ({placeholders})
                """,
                ids,
            ).fetchall()
    except Exception:
        return {}
    return {
        r["id"]: {
            "title": r["title"],
            "content": r["content"],
            "source": r["source"],
            "published_at": r["published_at"],
        }
        for r in rows
    }
```

## 四、回归扫查

`Grep "FROM raw_news"` 全仓库共 8 个文件命中，逐个核对均使用 `get_connection()`，
**仅 `theme_prediction_page.py` 一处误连**：

| 文件 | 连接方式 | 状态 |
|------|---------|------|
| `gui/pages/theme_prediction_page.py` | ~~`get_ai_inference_db().connect()`~~ → `get_connection()` | **已修复** |
| `services/storage/raw_store.py` | `get_connection()` | ✅ 正确 |
| `services/scoring/snapshot.py` | `get_connection()` | ✅ 正确 |
| `tools/dedupe_sqlite_raw.py` | `get_connection()` | ✅ 正确 |
| `tools/test_snapshot.py` | `get_connection()` | ✅ 正确 |
| `tools/test_market_db.py` | `get_connection()` | ✅ 正确 |

## 五、验证结果

修复后用真实样本验证（脚本完成即删，避免污染 tools/）：

```
[1/3] 从 theme_news 拿 10 个真实 news_id ...
  样本 news_id 数量: 10
  样本前 3 个: ['1448842912', '202605261001427506', '20260526374960148604']
[2/3] 调 _fetch_news_meta 反查 ...
  反查到 10 / 10 条
[3/3] 展示前 3 条反查结果：
  [HIT]  1448842912         | 2026-05-27 07:32:22 | 界面网   | AI眼镜新品频发...
  [HIT]  202605261001427506 | 2026-05-26 20:18:58 | 中证快讯 | 国家能源局发布51个...
  [HIT]  20260526374960148604 | 2026-05-26 15:24:35 | 东方财富 | 英特尔押注下一代...

[OK] 反查成功率 10/10 = 100.0%
```

## 六、教训与防御

1. **教训**：批量 `replace_all` 式重构有盲区，跨库语义的 SQL 不应该被无脑替换连接源
2. **静默 try/except 是元凶**：`except Exception: return {}` 把 `no such table: raw_news`
   异常吃了，建议**保留** try（避免 GUI 崩溃），但在 DEBUG 模式 print 异常便于排查
3. **后续防御**（不在本次修复范围）：
   - 给 GUI 关键反查类函数加 `print(f"[news_meta] fetch failed: {e}")` 落到日志
   - 考虑在 `check_three_db_health.py` 加一条「ai_inference.db **不应**有 raw_news 表」
     的反向校验

## 七、变更清单

- ✏️ `gui/pages/theme_prediction_page.py` L704-L735：换连接 + 加注释
- 📄 本文档：`doc/bugfix/05-27-1437-news-lookup-cross-db-fix.md`

无新增依赖、无 schema 变动、无需重启服务（GUI 重启即生效）。
