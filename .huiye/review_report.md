# 项目代码 Review 报告（第二轮）

> 评审者: 辉夜  
> 评审时间: 2026-05-22  
> 项目: A股新闻爬虫与 AI 分析系统 (`d:\爬虫`)  
> 评审范围: 全量代码扫描（架构 / 代码 / 安全 / 性能 / 维护性）  
> 代码规模: **35** 个 Python 文件，约 **9,291** 行  

---

## 🔥 摘要（TL;DR）

| 等级 | 数量 | 说明 |
|------|------|------|
| 🔴 严重 (Critical) | 0 | 上一轮密钥泄漏问题已修复 |
| 🟠 高危 (High) | 4 | 增量逻辑、命名误导、调度器、文档 |
| 🟡 中危 (Medium) | 8 | 日志、注释不一致、性能、依赖 |
| 🟢 建议 (Low) | 10 | 结构优化、测试、打包 |

**与上轮对比**：C1/C2/C3、H1/H2/H3 均已修复；H6 部分重构；死代码 `analyze_news_impact.py` 已删除；Git 历史已重建且初始提交不含真实密钥。

**当前最大风险**：`get_latest_news_from_db()` 用**文件修改时间**而非**新闻时间**判断增量边界，可能导致增量爬取漏采或重复采。

---

## ✅ 已修复（上轮 → 本轮）

| 编号 | 问题 | 现状 |
|------|------|------|
| C1 | API Key 泄漏到 Git | 历史重建；`git ls-files` 仅追踪 `ai_config.example.json` |
| C2 | `.gitignore` 缺 config | 已加 `config/ai_config.json`、`.env` |
| C3 | `get_default_config()` 重复 key | 已合并为单一 `prompt_templates` |
| H1 | 硬编码 `F:\爬虫` | 已改为相对项目根目录 |
| H2 | `headless` 参数无效 | 已传入 `fetch_page_with_scroll` / `crawl` |
| H3 | `CrawlerWorker.stop()` 暴力 terminate | 已改为协作式 `request_stop()` + `driver.quit()` |
| H6 | AI 调用 5 份重复代码 | `AINewsAnalyzer._stream_chat` / `NewsCleaner._call_provider` 已统一 |
| M4 | `_load_workflow` 只认直接父类 | 已改为 `issubclass(item, WorkflowBase)` |
| L4 | `analyze_news_impact.py` 死代码 | 已删除 |

新增改进：`core/env_loader.py` 支持 `.env` 优先读取密钥；`db_helper.py` 顶部已注明「非 SQLite」。

---

## 🟠 High（高危）

### H1. `get_latest_news_from_db()` 增量边界逻辑有误

**位置**: `core/db_helper.py:32-43`

```python
latest_file = max(files, key=os.path.getmtime)  # ← 按文件 mtime，不是新闻时间
latest_news = news_list[0]                       # ← 假设第一条最新
```

**问题**:
- 增量文件名如 `05-22-09——05-22-11.json` 的 mtime 不一定代表「最新新闻」
- 合并/编辑旧文件会改变 mtime，导致增量起点错误
- 自动停止可能过早结束或重复爬大量历史

**建议**: 遍历所有 `data/raw/*.json`，用 `parse_news_time()` 取全局最新一条。

### H2. `db_helper` 命名仍严重误导

文件注释已说明「非 SQLite」，但函数名仍是 `get_latest_news_from_db` / `load_all_db_news` / `deduplicate_with_db`；README 仍写 SQLAlchemy；`requirements.txt` 仍含 `sqlalchemy>=2.0.0` 但代码零引用。

**建议**: 重命名为 `news_storage.py` 或真正引入 SQLite 索引层。

### H3. 双调度器职责重叠

- `SchedulerService`（GUI 定时爬虫）：有 `start()` / `stop()`，1 秒轮询
- `WorkflowEngine.start_interval_schedule()`：`while True` 永真循环，无 stop，60 秒轮询

两者共用 `schedule` 库的全局 job 表，混用易出现任务重复或无法停止。

### H4. README 与项目现状严重脱节

| README 写法 | 实际 |
|-------------|------|
| `start.bat` / `install_pyqt.bat` | `启动.bat` / `install.bat` |
| `requirements_pyqt.txt` | `requirements.txt` |
| `F:\爬虫/` 目录结构 | 项目在 `D:\爬虫` |
| SQLAlchemy + SQLite | 纯 JSON 文件存储 |
| 多种调度模式 | 后端仅 `every().day.at()` |

新用户按 README 操作会直接失败。

---

## 🟡 Medium（中危）

### M1. 语义去重参数注释仍不一致

- `semantic_dedup.py` / `news_cleaner.py` / `data_merger.py`：已统一为 **30 分钟 + 50%**
- `gui/pages/data_page.py:376` 注释仍写「**10 分钟 + 80%**」，实际调用 `default_deduplicator`

用户按 GUI 注释理解会与真实行为不符。

### M2. 裸 `except:` 仍有多处（约 15 处）

会吞掉 `KeyboardInterrupt`，调试困难。重灾区：`news_crawler_scroll.py`、`db_helper.py`、`semantic_dedup.py`、`crawler_worker.py:221`。

### M3. `core/` 大量使用 `print()` 而非 logging

`workflows/base.py` 已有 logging 体系，但 `news_crawler_scroll.py`（81 处 print）等未接入。打包 EXE 无控制台时日志丢失。

### M4. `MainWindow` 启动时同步创建 6 个 Page

`DataPage` 等会在 init 时扫磁盘，拖慢冷启动。建议懒加载。

### M5. `EnhancedNewsFlow.execute()` 与父类大量重复

虽已继承 `DailyNewsFlow`，但 `execute()` 几乎完整复制 4 步流程，仅多 `_run_merger`。应改为 `super().execute()` 中间插入合并，或模板方法模式。

### M6. `AIConfig` 模块级单例

`core/ai_config.py` 末尾 `ai_config = AIConfig()`，import 即读盘写盘；配置损坏时 fallback 可能覆盖用户配置。

### M7. `set_api_key()` 仍明文写入 JSON

环境变量优先读取已实现，但 GUI 保存 Key 时仍会落盘到 `config/ai_config.json`。本地使用可接受，分享/备份时有泄漏风险。

### M8. `requirements.txt` 含未使用依赖

`sqlalchemy>=2.0.0` 无任何 import，增加安装体积与误导。

---

## 🟢 Low（建议）

| # | 问题 | 位置 |
|---|------|------|
| L1 | 缺少 `core/__init__.py`，靠 `sys.path` 注入 | `core/` |
| L2 | 打包路径逻辑分散在 `main.py`、`main_window.py`、`news_crawler_scroll.py` | 多处 `sys.frozen` |
| L3 | `data/schedule_tasks.json` 被 git 追踪（含个人任务配置） | `data/` |
| L4 | `chrome-win64/` 体积大，未在 `.gitignore`（若需分发应文档说明） | 根目录 |
| L5 | 无 `tests/`、无 pytest、无 CI | 全项目 |
| L6 | Windows 控制台 emoji 可能 UnicodeEncodeError | 多处 print |
| L7 | `tools/` 脚本非正式测试，且与主流程耦合 | `tools/` |
| L8 | 文档极多（`doc/` 60+ 文件）但与 README 不同步 | `doc/` vs `README.md` |
| L9 | 增量文件命名规则多样（`05-22.json` vs `05-22-09——05-22-11.json` vs `*_new.json`） | `data/raw/` |
| L10 | `WorkflowEngine` 与 GUI 定时任务 UI 能力不对等 | `schedule_page.py` |

---

## 📐 架构评价

### 优点

1. **三层分离清晰**：`gui/` ↔ `core/` ↔ `workflows/`
2. **PyQt5 + QThread + Signal** 后台任务模式正确
3. **WorkflowBase** 抽象合理（logger / history / params）
4. **多 AI 服务商** 配置与调用已初步统一
5. **安全基线改善**：`.env` + example 配置 + gitignore + 历史重建

### 缺点

1. **存储层薄弱**：JSON 文件当数据库，每次去重全量加载所有 raw 文件，数据量增长后 O(n) 恶化
2. **配置体系四套并存**：`core/config.py` + `ai_config.json` + `cleaning_criteria.json` + `workflows/configs/`
3. **文档债务**：README 过时，`doc/` 丰富但无人维护索引
4. **测试为零**：核心逻辑（增量、去重、时间解析）无自动化保障

### 数据流（简图）

```
GUI CrawlerPage
  → CrawlerWorker (QThread)
    → NewsCrawler.run()
      → GuZhangNewsCrawlerScroll.crawl()
        → data/raw/*.json

增量模式:
  get_latest_news_from_db() → set_auto_stop() → 滚动中检测 → deduplicate_with_db()

工作流:
  WorkflowEngine → DailyNewsFlow / EnhancedNewsFlow
    → crawler → cleaner → [merger] → exporter → AINewsAnalyzer
```

---

## 🎯 建议修复顺序

**优先（影响正确性）**:
1. H1 — 修复 `get_latest_news_from_db` 按新闻时间取最新
2. M1 — 统一 `data_page` 去重注释或暴露可配置参数

**本周（维护性）**:
3. H2 + M8 — 重命名 storage 层 / 移除 sqlalchemy
4. H4 — 更新 README 与真实文件名、架构描述
5. H3 — 合并或明确分离两套调度器

**有空再做**:
6. M3 — core 模块接入 logging
7. M4 — GUI 页面懒加载
8. M5 — 精简 EnhancedNewsFlow
9. L5 — 补核心路径单元测试（`parse_news_time`、去重、增量截断）

---

## 修复优先级

```
H1 → M1 → H4 → H2/H3 → M2/M3 → M4/M5 → L系列
```
