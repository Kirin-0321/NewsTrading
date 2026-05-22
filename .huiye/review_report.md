# 项目代码 Review 报告（第四轮）

> 评审者: 辉夜  
> 评审时间: 2026-05-23  
> 项目: A股新闻爬虫与 AI 分析系统 (`e:\NewsTrading`)  
> 评审范围: 全量 Python 代码 + `.huiye` 记忆 + Git 工作区状态  
> 代码规模: **44** 个 Python 文件（不含 `.huiye` 调试脚本）

---

## 摘要（TL;DR）

| 等级 | 数量 | 说明 |
|------|------|------|
| 🔴 严重 (Critical) | 0 | 密钥未进 Git；增量逻辑走 `published_ts` |
| 🟠 高危 (High) | 2 | 零测试；`schedule_tasks.json` 仍被 Git 追踪 |
| 🟡 中危 (Medium) | 6 | `doc/` 大规模删除未提交、print 日志、页面 eager load 等 |
| 🟢 建议 (Low) | 8 | MCP、LICENSE、懒加载、工具链等 |

**相对第三轮的变化**：
- ✅ **H1 README 脱节** — 已修复，`README.md` 与 v2.0 SQLite + services 架构一致
- ⚠️ **H2 旧 doc 误导** — 工作区已**物理删除** 40+ 篇旧文档，仅留分类目录 `.gitkeep` + 修改中的 `文档分类规范.md`（**未提交**）；旧内容仍在 Git 索引，恢复容易，但若误提交删除则历史文档从仓库消失
- 其余工程化债务（测试、日志、MCP）基本不变

---

## 工作区特别说明（第四轮新发现）

`git status` 显示 `doc/` 下大量文件为 **`D`（已删）**，磁盘上各子目录仅剩 `.gitkeep`：

```
doc/bugfix/   doc/design/   doc/features/   doc/guides/
doc/reports/  doc/updates/  doc/其他/         → 仅 .gitkeep
doc/文档分类规范.md  → 已修改（MM-DD-HHmm 新命名规范等）
```

**含义**：
1. 主人正在按新《文档分类规范》重组文档，方向正确
2. 在提交前需决定：是**确认删除旧文档**并后续按新规范重写，还是**先 `git restore doc/` 再分批迁移归档**
3. 第三轮担心的「任务流 / workflows 文档误导」——若最终提交删除，问题以另一种方式「解决」（文档没了），但**知识未迁移到新 doc**

---

## 已修复 / 已落地（持续有效）

| 项 | 现状 |
|----|------|
| 增量边界 | `RawStore.get_latest_news()` → `ORDER BY published_ts DESC` |
| 存储层 | `services/storage/*`，SQLite `data/news.db`（gitignore） |
| 调度 | 单一 `SchedulerService` + `scheduled_runner` |
| AI 调用 | `_stream_chat` / `_call_provider` 统一 |
| 假 workflows | 代码目录已删；三段式 `crawl_sync` / `clean_sync` / `analyze` |
| README | v2.0，启动/配置/结构描述与代码一致 |
| 安全 | `.env` / `ai_config.json` 未追踪；`git ls-files` 无密钥 |

**能力概览**：题材预测表、清洗 v1.4 + DeepSeek V4 分流、间隔调度、`agent` 函数级 API。

---

## 高危 (High)

### H1. 零自动化测试 — **未变**

无 `tests/` 目录。`parse_news_time`、语义去重、增量截断、`ThemeExtractor` JSON 修复、清洗 `_parse_decisions` 等核心逻辑均无 pytest 覆盖。重构风险仍靠人工点 GUI。

### H2. `data/schedule_tasks.json` 被 Git 追踪 — **未变**

文件含个人任务名、`last_run`、调度参数。建议：
- 新增 `data/schedule_tasks.example.json`（脱敏模板）
- `.gitignore` 忽略 `data/schedule_tasks.json`
- `git rm --cached data/schedule_tasks.json`

---

## 中危 (Medium)

### M1. `core/` 仍以 `print()` 为主（约 72 处，爬虫 60 处）

`news_crawler_scroll.py` 占大头。打包 EXE 无控制台时日志丢失；与 `SchedulerService` 的 logging 割裂。

### M2. `MainWindow` 启动时同步创建 7 个 Page — **未变**

```python
# gui/main_window.py — 全部在 __init__ 实例化
'crawler', 'data', 'export', 'ai_analysis', 'theme_prediction', 'cleaning', 'schedule'
```

`DataPage` 等可能在首次展示前即连接 SQLite，拖慢冷启动。建议 `QStackedWidget` 懒加载。

### M3. Agent 仅函数导出，无 MCP Server — **未变**

`agent/__init__.py` 导出 `crawl_sync` / `clean_sync` / `analyze_news` / `get_db_stats`；`mcp_server.py` 不存在。外部 Agent 无法标准 MCP 接入。

### M4. 裸 `except:` 残留 3 处 — **未变**

- `gui/pages/export_page.py`（2）
- `core/news_exporter.py`（1）

会吞 `KeyboardInterrupt`，应改为 `except Exception:`。

### M5. `AIConfig` 模块级单例 + GUI 可写本地 Key — **未变**

`.env` 优先已做；GUI 保存仍可能写入 `config/ai_config.json`。

### M6. `doc/` 重组进行中，提交策略未定 — **第四轮新增**

- 旧文档（含 `12-24-*` 命名）与新版 `MM-DD-HHmm-简述` 规范并存于 Git 历史
- 工作区删除尚未 commit：误操作可能导致团队/未来自己丢失排障记录
- **建议**：提交前写 `doc/updates/05-23-xxxx-文档目录重组说明.md`，列出迁移/废弃清单

### M7. 题材表 v2 迁移 = DROP 重建 — **未变**

`database.py::_migrate_theme_tables_v2()` schema 不一致时清空题材相关表。需在 UI 或 guides 标明「大版本升级可能丢历史题材」。

---

## 建议 (Low)

| # | 问题 |
|---|------|
| L1 | 缺少 `core/__init__.py`，依赖 `sys.path.insert` |
| L2 | 打包路径逻辑分散（`main.py` / `main_window.py` / crawler） |
| L3 | `chrome-win64/` 体积大，README 已提，分发策略可再写 guides |
| L4 | `tools/` 为手工脚本，非 pytest |
| L5 | `.huiye/_debug_*` 调试产物可考虑 gitignore |
| L6 | Windows 控制台 emoji 可能 `UnicodeEncodeError` |
| L7 | `news_exporter.py` 与 ExportPage 功能重叠 |
| L8 | 无 `pyproject.toml` / ruff / mypy |
| L9 | 无 `LICENSE` 文件 |

---

## 架构评价

### 数据流（当前准确）

```
[GUI / SchedulerService / agent API]
        ↓
crawl_sync_service → news_crawler_scroll → raw_news
clean_sync_service → news_cleaner → curated_news / rejected_news
analysis_service → ai_news_analyzer + theme_extractor → 报告 + theme_predictions
```

### 优点

1. 三段流水线职责清晰，GUI 与定时任务共用 `services/`
2. Storage 抽象落地，增量查询正确
3. 安全基线合格（`.env` + gitignore）
4. AI 场景分流合理（分析 pro / 清洗 flash）

### 缺点

1. **文档层处于真空期**：旧 doc 已删、新 doc 未写，仅靠 README + `.huiye/架构方案.md`
2. **可观测性弱**：print 与 logging 混用
3. **Agent / 测试 / CI 空白**

---

## 建议修复顺序

```
确认 doc 删除/迁移策略并提交规范重组
→ H2 schedule_tasks 示例化 + gitignore
→ H1 核心路径 pytest（parse_news_time / 去重 / 增量）
→ M1 crawler 接 logging
→ M2 页面懒加载
→ M4 裸 except 修复
→ M3 MCP（按需）
→ L 系列
```

---

## 一句话（辉夜）

**代码已经是能跑的三段式 SQLite 流水线；README 也对上了。** 当前最大风险不是架构，而是 **`doc/` 整目录删除悬而未决** + **仍然没有测试**。主人要是 commit 了删除却不补新文档，以后排查问题只能翻 Git 考古——强迫症会发作的。
