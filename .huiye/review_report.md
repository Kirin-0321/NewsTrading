# 项目代码 Review 报告（第五轮）

> ⚠️ **OBSOLETE — 仅作历史归档**
>
> 本文档写于 2026-05-23，是盘后数据自动化（M0~M5）开工**之前**的代码评审。
> 报告中描述的"工作区状态 / 模块结构 / 待办项"绝大多数已在 2026-05-26 的
> 11 天施工中被推翻或重写。
>
> 请改读现行版：
> - [README.md](./README.md) — 项目文档总索引 + 当前进度
> - [架构方案.md](./架构方案.md) — 三段式流水线 + §15 盘后数据模块
> - [盘后数据自动化-功能说明.md](./盘后数据自动化-功能说明.md) — 主人版
> - [盘后数据自动化-施工方案.md](./盘后数据自动化-施工方案.md) §20 实际交付回顾
>
> 本文档保留意义：记录 2026-05-23 时的代码债务感与作者视角，便于回溯演化轨迹。
>
> ---

> 评审者: 辉夜  
> 评审时间: 2026-05-23  
> 项目: A股新闻爬虫与 AI 分析系统 (`e:\NewsTrading`)  
> 评审范围: 全量 Python + `.huiye` + Git 工作区 + 新增 `doc/`  
> 代码规模: **45** 个 Python 文件（不含 `.huiye` 调试脚本）

---

## 摘要（TL;DR）

| 等级 | 数量 | 说明 |
|------|------|------|
| 🔴 严重 (Critical) | 0 | 密钥未进 Git；增量仍走 `published_ts` |
| 🟠 高危 (High) | 2 | 零测试；`schedule_tasks.json` 仍被追踪且工作区有修改 |
| 🟡 中危 (Medium) | 7 | print 日志、页面 eager load、文档真空缓解但未闭环等 |
| 🟢 建议 (Low) | 9 | MCP、工具链、大 CSV 未 ignore 等 |

**相对第四轮的主要变化**：
- ✅ **存储 v2 单表落地** — `curated_store.py` 已删（-210 行），`raw_news.clean_status` 统一 pending/curated/rejected；无残留 `import curated_store`
- ✅ **题材抽取模块成熟** — `theme_extractor.py` 模块级文档、4 级 JSON 容错、`parse_news_id_map` / `apply_priority_ranks_from_report` 完整
- ✅ **文档真空部分缓解** — 新增 `doc/其他/清洗规则.md`、`doc/reports/05-23-2100-Tushare题材数据源对比.md`；`doc/文档分类规范.md` 保留
- ✅ **Tushare 题材调研** — `tools/export_tushare_theme_daily.py` + `data/tushare_theme_compare/` 对比数据
- ⚠️ **工作区大 diff 未提交** — 26 文件 +865/-539 行，含存储重构；提交前需确认迁移说明
- ⚠️ **`.huiye/架构方案.md` §3 目标结构** 仍列 `curated_store.py`，与代码不一致（§5 已写 v2 单表，前后矛盾）

---

## 工作区状态（第五轮）

```
已修改（核心）:
  services/storage/raw_store.py      (+大幅扩展 clean_status API)
  services/storage/database.py     (迁移 + v2 题材表)
  services/storage/__init__.py     (移除 CuratedStore)
  D services/storage/curated_store.py
  core/theme_extractor.py, news_cleaner.py, ai_news_analyzer.py
  gui/pages/* (data, theme_prediction, cleaning, ai_analysis)
  agent/__init__.py, README.md, data/schedule_tasks.json

未跟踪:
  doc/reports/05-23-2100-Tushare题材数据源对比.md
  doc/其他/清洗规则.md
  data/tushare_theme_compare/*.csv
  tools/export_tushare_theme_daily.py
```

**提交建议**：单 commit 或拆为 `storage-v2-single-table` + `theme/doc/tools`；附 `doc/updates/05-23-xxxx-存储单表化与题材v2.md` 说明旧库 `clean_status` 补列后需重跑清洗。

---

## 已修复 / 已落地（累计 + 本轮新增）

| 项 | 现状 |
|----|------|
| 增量边界 | `RawStore.get_latest_news()` → `ORDER BY published_ts DESC` |
| 存储 v2 | 单表 `raw_news` + `clean_status`；`curated_store` 已移除 |
| 调度 | `SchedulerService` + `scheduled_runner` 兼容旧 JSON |
| AI 调用 | 分析 pro / 清洗·题材 flash 分流 |
| 题材管线 | 报告 → `ThemeExtractor` → `theme_predictions` 三表 |
| README | v2.0 与 SQLite + services 一致 |
| 安全 | `git ls-files` 仅 `data/schedule_tasks.json` 敏感；`.env` / `ai_config.json` 未追踪 |
| 文档 | 清洗规则、Tushare 对比报告已写（仍缺 features/guides 级操作文档） |

---

## 高危 (High)

### H1. 零自动化测试 — **未变**

无 `tests/`。本轮变更风险集中点：
- `RawStore.apply_clean_results` / `reset_clean_status_by_date`
- `parse_news_time` / 语义去重
- `ThemeExtractor._parse_response` 四级容错
- `database._migrate_theme_tables_v2` / `_ensure_clean_status_columns`

任一重构仍只能靠手点 GUI。

### H2. `data/schedule_tasks.json` 被 Git 追踪 — **未变，且工作区已修改**

含个人任务名、`last_run`、调度参数。README 已提醒脱敏，但仓库仍追踪。建议：
- `data/schedule_tasks.example.json`（脱敏模板）
- `.gitignore` 增加 `data/schedule_tasks.json`
- `git rm --cached data/schedule_tasks.json`

---

## 中危 (Medium)

### M1. `core/` 仍以 `print()` 为主

`news_crawler_scroll.py` 约 60 处；打包 EXE 无控制台时日志丢失。`database.py` / `scheduler_service` 已用 `logging`，爬虫未对齐。

### M2. `MainWindow` 启动时同步创建 7 个 Page — **未变**

```python
# gui/main_window.py
'crawler', 'data', 'export', 'ai_analysis', 'theme_prediction', 'cleaning', 'schedule'
```

`DataPage` / `ThemePredictionPage` 可能在首次展示前即 `init_database()`。

### M3. Agent 无 MCP Server — **未变**

`agent/__init__.py` 导出 `crawl_sync` / `clean_sync` / `analyze_news` / `get_db_stats`；`mcp_server.py` 不存在。

### M4. 裸 `except:` 残留 3 处 — **未变**

- `gui/pages/export_page.py`（2）
- `core/news_exporter.py`（1）

### M5. `AIConfig` 模块级单例 + GUI 可写本地 Key — **未变**

`.env` 优先；GUI 保存仍可能写入 `config/ai_config.json`。

### M6. 题材表 v2 迁移 = DROP 重建 — **未变**

`database._migrate_theme_tables_v2()` schema 不一致时清空三表。需在 UI 或 guides 标明。

### M7. `data/tushare_theme_compare/` 大 CSV 未 ignore — **第五轮新增**

对比导出 CSV 体积大、易变，不宜进 Git。建议 `.gitignore`：

```
data/tushare_theme_compare/
!data/tushare_theme_compare/.gitkeep
```

保留 `tools/export_tushare_theme_daily.py` + `doc/reports/` 结论即可。

### M8. 内部文档与代码不同步 — **第五轮新增**

| 文档 | 问题 |
|------|------|
| `.huiye/架构方案.md` §3 | 仍画 `curated_store.py` |
| 第四轮 `review_report` 数据流图 | 仍写 `curated_news / rejected_news` 子表 |
| `requirements.txt` | 架构方案提 `sqlalchemy`，依赖文件未列（代码用 stdlib `sqlite3`，可删文档或补依赖） |

---

## 建议 (Low)

| # | 问题 |
|---|------|
| L1 | 缺少 `core/__init__.py`，依赖 `main.py` 的 `sys.path.insert` |
| L2 | 打包路径逻辑分散（`main.py` / `main_window.py` / crawler） |
| L3 | `chrome-win64/` 体积大，分发策略可写 guides |
| L4 | `tools/` 为手工脚本，非 pytest |
| L5 | `.huiye/_debug_*` 已 gitignore ✓ |
| L6 | Windows 控制台 emoji 可能 `UnicodeEncodeError` |
| L7 | `news_exporter.py` 与 ExportPage 功能重叠 |
| L8 | 无 `pyproject.toml` / ruff / mypy |
| L9 | 无 `LICENSE` |
| L10 | `theme_extractor.py` 717 行，可考虑拆 `json_repair` / `report_parsers` 子模块（非必须） |

---

## 架构评价

### 数据流（第五轮 — 已更正）

```
[GUI / SchedulerService / agent API]
        ↓
crawl_sync → raw_news (clean_status=pending)
clean_sync → news_cleaner → raw_news (curated | rejected)
analyze    → ai_news_analyzer → data/AI_analysis/*.md
           → theme_extractor → theme_predictions + theme_stocks + theme_news
```

### 优点

1. **v2 单表** 消灭双表同步与 `CuratedStore` 兼容层，查询语义清晰
2. `theme_extractor.py` 文档与容错达到项目内最佳实践水准
3. `RawStore` API 完整（按日统计、按日重置清洗、批量 apply_clean_results）
4. Tushare 题材数据源对比有报告支撑后续选型
5. `doc/其他/清洗规则.md` 与 `config/cleaning_criteria.json` 形成「人读 + 机读」双轨

### 缺点

1. **工程化债务未减**：测试、schedule_tasks、日志、MCP
2. **大 diff 悬而未决**：单表迁移若未写迁移说明，未来自己也会骂
3. **doc/** 仍偏薄：缺 `features/` 下各页面操作说明、`guides/` 下升级指南

---

## 代码质量抽样（本轮亮点）

**`theme_extractor.py`** — 模块 docstring 含调用链、设计要点；`_parse_response` 四级兜底；流式 JSON 避免 GUI 假死。可作为其他 `core/` 模块的注释模板。

**`services/storage/raw_store.py`** — `apply_clean_results` 单事务更新；`get_daily_stats` 支持按 `clean_status` 聚合。与 `database._ensure_clean_status_columns` 配合完成旧库升级。

**`clean_sync_service.py`** — `progress_callback` 双签名适配器，避免 `NewsCleaner` 5 参回调再次 TypeError。

---

## 建议修复顺序

```
提交/拆分当前 v2 存储大 diff + 写 updates 迁移说明
→ H2 schedule_tasks 示例化 + gitignore
→ M7 tushare CSV gitignore
→ 同步 .huiye/架构方案 §3 与 review 数据流图
→ H1 pytest（parse_news_time / JSON 容错 / clean_status）
→ M1 crawler 接 logging
→ M2 页面懒加载
→ M4 裸 except
→ M3 MCP（按需）
```

---

## 一句话（辉夜）

**单表存储和题材管线这次是真的落地了，`theme_extractor` 写得甚至让我少骂两句。** 但主人再不 commit、不补测试、`schedule_tasks.json` 还躺在 Git 里，第五轮和第四轮的区别就只是「又多写了一堆没入库的好代码」——强迫症会发作的。
