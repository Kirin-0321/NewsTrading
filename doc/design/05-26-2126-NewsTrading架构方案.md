# 项目架构方案（三段式流水线 + 盘后数据模块）

> 记录时间: 2026-05-22（§1~14） · 更新: 2026-05-26（§15 盘后数据模块）  
> 状态: **现行版 — P0 清理已完成；盘后数据自动化 M0~M5 全部落地**  
> 关联:
> - [盘后数据自动化-功能说明.md](../features/05-26-2126-盘后数据自动化功能说明.md) · [盘后数据自动化-施工方案.md](./05-26-2126-盘后数据自动化施工方案.md)
> - [CLI评估回测-Agent优化初步设计.md](./05-26-2126-CLI评估回测Agent优化初步设计.md)（下一阶段）
> - [review_report.md](../reports/05-23-1800-第五轮代码评审-OBSOLETE.md)（OBSOLETE）

---

## 1. 业务目标

将整个系统划分为 **3 个独立阶段**，每阶段有明确的输入/输出「逻辑库」：

| 阶段 | 名称 | 触发方式 | 输入 | 输出 |
|------|------|----------|------|------|
| ① | 数据获取 | **定时自动** | 外部网站 | **原始库 (Raw)** |
| ② | 数据清理 | **定时自动** | 原始库 | **精选库 (Curated)** |
| ③ | 新闻分析 | GUI 手动 / GUI 定时 / **Agent** | 精选库（默认）或原始库 | **分析报告 (Analysis)** |

**设计原则**：
- ①② 无人值守，全自动
- ③ 按需触发，GUI 与 Agent **共用同一套 `AnalysisService`**
- 删除现有 `workflows/` + `WorkflowEngine`（假调度、与 GUI 重复）
- 统一一个 `SchedulerService` 管理所有定时任务

---

## 2. 数据流

```
[格隆汇等源站]
       │
       ▼ ① crawl_sync（定时）
  ┌─────────────┐
  │  原始库 Raw  │  ← 爬取 + 语义去重 + 合并，增量写入
  └──────┬──────┘
         │
         ▼ ② clean_sync（定时，仅处理未清洗增量）
  ┌─────────────┐
  │ 精选库 Curated│  ← AI 清洗 keep 项；remove 项可选写入 rejected
  └──────┬──────┘
         │
         ▼ ③ analyze（手动 / 定时 / Agent）
  ┌─────────────┐
  │ 分析报告      │  Markdown → data/AI_analysis/
  └─────────────┘
```

### ③ 分析双入口

| 入口 | 能力 |
|------|------|
| **GUI** | 选手动时间范围、数据源(raw/curated)、模板、模型；或配置定时分析任务 |
| **Agent** | MCP/API 调用同一 `AnalysisService`；默认读精选库，可指定时间范围或 `source=raw` |

---

## 3. 目标模块结构

```
services/
  crawl_sync_service.py     # ① 爬取 → 去重合并 → 写入原始库
  clean_sync_service.py     # ② 从未清洗增量 → AI 清洗 → 写入精选库
  analysis_service.py       # ③ 统一分析入口（GUI + Agent + 定时）
  storage/
    base.py                 # 存储抽象接口
    raw_store.py              # 原始库
    curated_store.py          # 精选库
    sqlite_backend.py         # SQLite 实现（Phase 2）
    json_backend.py           # JSON 兼容层（Phase 1 可选，便于迁移）

agent/
  mcp_server.py             # Agent tools → services（Phase 3）

core/                       # 保留现有底层能力，由 services 封装
  news_crawler_scroll.py
  news_cleaner.py
  data_merger.py
  ai_news_analyzer.py
  semantic_dedup.py
  ...

gui/                        # 页面/worker 改为调用 services，不再依赖 workflow
core/scheduler_service.py   # 扩展 task type: crawl_sync | clean_sync | analyze
```

### 删除清单（实施时）

| 删除 | 原因 |
|------|------|
| `workflows/` | 编排由调度器 + Agent 替代 |
| `core/workflow_engine.py` | 双调度器、假 schedule |
| `schedule_page.py` 任务流 Tab | 无实际价值 |
| 评估 `core/news_exporter.py` | 与 ExportPage 重复；分析不再依赖 export 步骤 |

---

## 4. 统一调度任务格式

扩展 `data/schedule_tasks.json`：

```json
[
  {
    "id": "uuid",
    "name": "早盘爬取",
    "type": "crawl_sync",
    "time": "09:00",
    "enabled": true,
    "params": {
      "scroll_times": 36,
      "wait_seconds": 6,
      "incremental": true
    }
  },
  {
    "id": "uuid",
    "name": "清洗增量",
    "type": "clean_sync",
    "time": "09:30",
    "enabled": true,
    "params": {
      "batch_size": 100,
      "provider": "deepseek"
    }
  },
  {
    "id": "uuid",
    "name": "盘后分析",
    "type": "analyze",
    "time": "16:00",
    "enabled": true,
    "params": {
      "source": "curated",
      "time_range_hours": 24,
      "template": "short_term",
      "provider": "deepseek"
    }
  }
]
```

`SchedulerService._schedule_task()` 按 `type` 分发到对应 `services.*_service.run()`。

---

## 5. Storage 抽象接口（与底层无关）

上层（services / GUI / Agent）**只依赖接口**，不直接 `glob('data/raw/*.json')`。

**v2 单表模型**：清洗结果不再拆 `curated_news` / `rejected_news` 两张子表，
而是给 `raw_news` 加 `clean_status` + `clean_reason` 两个字段，
所有「精选 / 剔除」筛选都退化为 `WHERE clean_status=?`，删除子表与 `CuratedStore` 兼容层。

```python
# services/storage/raw_store.py（实际接口）

CLEAN_PENDING  = "pending"   # 未清洗
CLEAN_CURATED  = "curated"   # AI 保留
CLEAN_REJECTED = "rejected"  # AI 剔除

class RawStore:
    # 读
    def get_latest_news(self) -> NewsItem | None: ...
    def get_news_since(self, since, status=None) -> list[NewsItem]: ...
    def get_news_in_range(self, start, end, status=None) -> list[NewsItem]: ...
    def get_news_for_date(self, date_str, status=None) -> list[NewsItem]: ...
    def get_daily_stats(self, status=None) -> list[dict]: ...
    def get_time_bounds(self, status=None) -> tuple[datetime|None, datetime|None]: ...
    def count(self, status=None) -> int: ...
    def count_uncleaned(self) -> int: ...     # 等价 count(CLEAN_PENDING)
    def count_curated(self) -> int: ...
    def count_rejected(self) -> int: ...

    # 写
    def upsert_news(self, items) -> UpsertResult: ...               # 爬取入库，强制 pending
    def get_uncleaned_news(self, limit=500) -> list[NewsItem]: ...  # status=pending 的批量取出
    def apply_clean_results(self, kept, removed, provider) \
        -> tuple[int, int]: ...                                     # 单事务更新 status + reason

    # 维护
    def reset_clean_status_by_date(self, date_str, from_status) -> int: ...  # 重置为 pending
    def delete_by_date(self, date_str) -> int: ...                            # 物理删除

    # 元数据
    def get_meta(self, key): ...
    def set_meta(self, key, value): ...
```

**增量判定**：`RawStore.get_uncleaned_news()` 直接 `WHERE clean_status='pending'`，零额外状态依赖。

---

## 6. SQLite 方案详解

### 6.1 为什么适合本项目

| 需求 | JSON 目录（现状） | SQLite |
|------|-------------------|--------|
| 按时间范围查询 | 扫全部文件 + 解析 | `WHERE published_at BETWEEN ? AND ?` |
| 增量爬取（取最新一条） | 误用文件 mtime，易错 | `ORDER BY published_at DESC LIMIT 1` |
| 清洗水位线 / 已处理标记 | 需额外 JSON 状态文件 | `clean_status` 字段或 `sync_meta` 表 |
| 去重（按 id） | 全量加载到内存 | `UNIQUE(id)` + `INSERT OR IGNORE` |
| 数据量增长 | O(文件数 × 条数) 全量扫描 | 索引查询，万级、十万级仍可用 |
| 单文件备份 | 拷目录 | 拷 `data/news.db` 一个文件 |
| PyInstaller 打包 | 路径分散 | 单库文件，路径固定 |
| Agent 查询 | 需封装复杂 glob 逻辑 | 标准 SQL，tool 实现简单 |

**规模预估**：A 股新闻爬虫，单日数百～数千条，一年约 10～50 万条。SQLite **完全够用**（单表百万行无压力）。

**不适合 SQLite 的场景**（本项目不涉及）：高并发多写、多进程同时写、分布式集群。

### 6.2 好不好用？

**好用**，对此类桌面单机应用是常见选择：

- Python 标准库 `sqlite3`，**零额外服务**（不像 MySQL/PostgreSQL 要装服务器）
- `requirements.txt` 里已有 `sqlalchemy>=2.0.0`（当前未使用），可直接启用
- 也可用纯 `sqlite3` 保持轻量；SQLAlchemy 适合 ORM + 迁移脚本
- 一条连接、WAL 模式，爬虫写 + GUI 读可共存（注意：写操作串行化，对本项目足够）

**推荐配置**（首次连接时）：

```python
conn.execute("PRAGMA journal_mode=WAL")   # 读写并发更好
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")  # 锁等待 5s
```

### 6.3 建议表结构（草案）

```sql
-- 单表：原始 + 清洗状态合并
CREATE TABLE raw_news (
    id              TEXT PRIMARY KEY,          -- 源站 id 或 hash
    title           TEXT NOT NULL,
    content         TEXT,
    source          TEXT,
    published_at    TEXT NOT NULL,             -- ISO8601: 2026-05-22 12:48:03
    published_ts    INTEGER NOT NULL,          -- Unix 时间戳，便于排序/索引
    crawled_at      TEXT NOT NULL,
    clean_status    TEXT NOT NULL DEFAULT 'pending',  -- pending / curated / rejected
    clean_reason    TEXT,                      -- 精选→keep_reason；剔除→removal_reason
    extra_json      TEXT                       -- 其它字段 JSON 扩展
);
CREATE INDEX idx_raw_published     ON raw_news(published_ts DESC);
CREATE INDEX idx_raw_clean_status  ON raw_news(clean_status);

-- 同步元数据（水位线、爬虫游标等）
CREATE TABLE sync_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
```

> v2 单表化前置版本：曾拆出 `curated_news` / `rejected_news` 两张子表，导致
> 「同一条新闻三处出现，删一处忘删另一处」「reason 链路要跨表搜」等隐性 bug。
> 现已 DROP，全部回到 `clean_status` 字段，状态切换只是一条 `UPDATE`。

### 6.4 方便迁移吗？—— **分阶段，可控**

#### 策略：**Storage 抽象 + 双写过渡 + 一次性导入**

```
Phase 1（1～2 周）
  ├── 定义 RawStore / CuratedStore 接口
  ├── 实现 JsonBackend（包装现有 data/raw、data/cleaned 逻辑，修正 mtime bug）
  └── services 只调接口，业务代码不碰 JSON 路径

Phase 2（1 周）
  ├── 实现 SqliteBackend（同上接口）
  ├── 编写 migrate_json_to_sqlite.py 一次性导入历史 JSON
  └── 配置开关 storage.backend = "json" | "sqlite"（默认 sqlite）

Phase 3（稳定后）
  ├── 新数据只写 SQLite
  ├── JSON 目录改为可选导出/归档（export 仍可按时间 dump 成 JSON/Markdown）
  └── 删除 JsonBackend 或保留只读兼容
```

#### 迁移脚本要做的事

1. 扫描 `data/raw/*.json`，解析 `news[]`（兼容 envelope 与纯数组）
2. 扫描 `data/cleaned/*_clear.json`、`*_removed.json`
3. `INSERT OR IGNORE` 写入 SQLite（按 `id` 去重）
4. 输出报告：导入条数、跳过重复、解析失败文件列表
5. **不删除原 JSON**（迁移验证通过后再手动归档）

#### 迁移风险与对策

| 风险 | 对策 |
|------|------|
| 旧 JSON 格式不统一 | 迁移脚本复用 `DataLoader` / `NewsCleaner._load_files` 的解析逻辑 |
| id 缺失或重复 | 迁移时用 `hash(title + published_at)` 补 id |
| 迁移中途失败 | 单事务批量提交；支持 `--resume` |
| 用户习惯看 JSON 文件 | 保留「导出为 JSON」功能；SQLite 是主库，JSON 是导出格式 |

**结论：迁移方便**，因为关键是 **先抽 storage 接口**，底层从 JSON 换 SQLite 对上层透明。

---

## 7. JSON vs SQLite 决策建议

| 阶段 | 建议 |
|------|------|
| **现在（写方案、删 workflow）** | 先定接口，可不立刻上 SQLite |
| **实施 services 时** | **同步上 SQLite**（改动集中，长痛不如短痛） |
| **若赶时间** | Phase 1 用 JsonBackend 跑通三段流水线，Phase 2 再迁 SQLite |

**辉夜倾向**：实施 `services/` 时 **直接 SQLite**。现有 JSON 逻辑分散在 6+ 文件里，修 watermark / 时间查询时要全改一遍；不如一次到位。

---

## 8. AnalysisService 接口（③ 统一入口）

```python
def analyze(
    source: Literal["curated", "raw"] = "curated",
    start: datetime | None = None,
    end: datetime | None = None,
    template_id: str | None = None,
    provider: str | None = None,
    market_summary: str | None = None,
    progress_callback: Callable | None = None,
) -> AnalysisResult:
    """
    返回:
      ok: bool
      report_path: str | None
      news_count: int
      time_range: tuple
      error: str | None
    """
```

Agent MCP tool `analyze_news` 直接映射此函数。

---

## 9. 实施顺序

```
Step 0  本文档定稿 ✅
Step 1  删除 workflow ✅
Step 2  services/storage + SQLite + 迁移脚本 ✅
Step 3  crawl_sync / clean_sync / analysis service ✅
Step 4  GUI Worker / 定时任务部分接入 ✅（分析/数据/清洗三页已接 SQLite）
Step 5  SchedulerService 三种 task type ✅
Step 6  agent/mcp_server.py ⏳（见 [CLI评估回测-Agent优化初步设计.md](./05-26-2126-CLI评估回测Agent优化初步设计.md)；Phase 0 CLI → Phase 1 Eval → Phase 2 Backtest → Phase 3 MCP）
Step 7  历史 JSON 迁移 ✅（7218 条原始，29 条精选）
Step 8  P0 死代码清理 ✅（2026-05-22）
        - 删除 workflows/ 目录、core/db_helper.py、watermark 函数
        - news_crawler_scroll.py 从 974 行瘦至 460 行
        - data_loader.py 从 240 行瘦至 105 行
        - requirements.txt 移除 sqlalchemy / openpyxl / lxml / requests / python-dateutil
Step 9  清洗结果单表化 ✅（2026-05-23）
        - DROP curated_news / rejected_news；raw_news 增 clean_status + clean_reason
        - 删 services/storage/curated_store.py 整个兼容层
        - 上层（GUI / analysis / exporter / agent）改走 RawStore + status 参数
        - 旧库 raw_news 不丢，全部回 pending，重新跑一次清洗即可
```

---

## 10. 待主人确认

- [x] 精选/剔除是否拆表 → **不拆**，2026-05-23 起统一为 `raw_news.clean_status`
- [ ] 分析默认定时任务参数（模板、时间窗口）
- [ ] 迁移后是否保留 `data/raw/*.json` 作为并行备份（建议：迁移期双写，稳定后仅 SQLite + 按需导出）

---

## 11. 一句话总结

**三段架构定稿**；SQLite 对此项目 **合适、好用、迁移可控**——前提是先做 **Storage 抽象**，再写迁移脚本，不要直接在业务代码里散落 SQL。

---

## 12. 清洗标准 & NewsCleaner 升级（v1.4，2026-05-22）

> 触发：2026-05-22 抽样评估（60 条样本：假阳性 16.7% / 假阴性 13.3%），发现两个硬伤——剔除 reason 全是默认值、政策类与钝化题材边界混淆。

### 12.1 改动一览

| 文件 | 改动 | 目的 |
|------|------|------|
| `config/cleaning_criteria.json` | 新增「硬规则」段（发布主体白名单 / 宏观数据 / 美政府关键技术投资）；钝化题材改为「默认 remove」；删末尾重复输出格式；example_prompt 加入 8 条边界 few-shot | 修复政策类被误删（B12 发改委成品油调价）、钝化题材漏过滤（A19/A20 美伊口水战） |
| `core/news_cleaner.py::_build_batch_prompt` | content 不截断（库内 p99=493 字，最长 1705 字）；强制要求 AI 输出 `keep\|理由` / `remove\|理由` | 修复正文被截 + 给出剔除理由 |
| `core/news_cleaner.py::_parse_decisions` | 返回类型 `List[str]` → `List[tuple[str, str]]`，每项为 (decision, reason)；**兜底从 `keep` 改 `remove`**（'AI解析丢失-保守剔除'） | 避免 AI 解析丢失时假阴性流入精选库 |
| `core/news_cleaner.py::_process_batch_resilient` | 将 reason 填到 `news['removal_reason']` 与 `news['keep_reason']` | 真正打通 reason 到 raw_news.clean_reason 的链路 |
| `core/news_cleaner.py::_call_provider` | `temperature` 0.3 → 0.1 | 清洗本质是分类任务，降低随机性 |

### 12.2 调用链（数据流）

```
config/cleaning_criteria.json (default.criteria)
   │
   ▼ services/clean_sync_service._load_cleaning_criteria()
NewsCleaner(criteria).clean_news_list()
   │
   ├── _deduplicate()  ← 30 分钟 + 30% 相似度去重
   │
   ├── _ai_clean_batches()
   │     └── _process_batch_resilient()
   │           ├── _build_batch_prompt()    [v1.4: content 不截断 + 要求 reason]
   │           ├── _call_ai_judge()
   │           │     └── _call_provider()   [v1.4: temperature=0.1]
   │           │           └── _parse_decisions()  [v1.4: 返回 (decision, reason) tuple]
   │           └── news['removal_reason'] / news['keep_reason']  [v1.4: 填实]
   │
   ▼ 返回 {'kept': [...], 'removed': [...], 'metadata': {...}}
services/storage/raw_store.apply_clean_results(kept, removed)
   │
   └─→ raw_news.clean_status / clean_reason
        ← reason 不再是 "不符合保留标准"，而是 "钝化-美伊口水" 等具体分类
```

**关键事实**：`RawStore.apply_clean_results` 按 `news.get('removal_reason')` / `news.get('keep_reason')` 读取，
单事务 UPDATE 完成状态切换，下游零额外写。

### 12.3 解析兜底语义变更（行为差异）

| 场景 | v1.3 行为 | v1.4 行为 |
|------|----------|----------|
| AI 返回数量不足 | 缺失条目默认 `keep` | 缺失条目默认 `remove`（reason='AI解析丢失-保守剔除'） |
| AI 返回纯 keep/remove 无 reason | 正常解析为 keep/remove | 正常解析为 (keep, '') / (remove, '')；下游回退到默认 reason |
| AI 解析数量过多 | 截断到 expected_count | 同 v1.3 |

兼容性：`_parse_decisions` 的返回类型从 `List[str]` → `List[tuple[str, str]]`，**调用者只有 `_call_ai_judge` 一处**，已同步更新。

### 12.4 预期收益（基于 60 条样本）

| 指标 | v1.3（评估值） | v1.4（预期） |
|------|--------------|------------|
| 假阳性率（误保留） | 16.7% | < 10% |
| 假阴性率（误剔除） | 13.3% | < 5% |
| 保留率 | 31.6% | 18-25%（趋近合理区间） |
| rejected.reason 可读性 | 0%（全是默认值） | ≈ 100%（AI 给出分类标签） |

### 12.5 后续待办（暂缓）

- [ ] P2：抓取层补充 `market_cap` / `industry` / `amount` 字段，让 AI 不靠猜判断市值/订单门槛
- [ ] P2：2 段 pipeline——规则前置过滤明显条目，仅灰区送 AI，降 token 成本
- [ ] P2：抓取层去重失败排查（A27/A28 同事件两个来源各保留一次）
- [ ] 一周后回归：再抽 60 条样本对比 v1.3/v1.4 精度

---

## 13. AI 模型场景分流（2026-05-26 修订：全链路统一 v4-pro）

> 触发：DeepSeek 2026-04-24 发布 V4 系列（`deepseek-v4-pro` / `deepseek-v4-flash`，1M 上下文），
> 旧别名 `deepseek-chat` / `deepseek-reasoner` 将于 2026-07-24 停用。
> 2026-05-26 起：评估 pro 成本可控（清洗 batch 单次 ~3k token、约 ¥0.01），
> **全链路统一 `deepseek-v4-pro`**，靠 `thinking=enabled/disabled` 区分场景。

### 13.1 分流决策

| 任务 | provider | model | 思考模式 | 理由 |
|------|----------|-------|----------|------|
| **AI 分析（盘后总结）** | deepseek | `deepseek-v4-pro` | ✅ `reasoning_effort=max` | 长上下文 + 推理密集 + 单日调用次数少，质量优先 |
| **新闻清洗** | deepseek | `deepseek-v4-pro` | ❌ `thinking=disabled` | 分类任务（keep/remove）；用 pro 提升 JSON 严格性，禁 thinking 控延迟 |
| **题材抽取** | deepseek | `deepseek-v4-pro` | ❌ `thinking=disabled` | 结构化 JSON 抽取，pro 字段还原度更高 |
| **盘后 catalysts / trader 兜底** | deepseek | `deepseek-v4-pro` | ❌ `thinking=disabled` | 强约束 JSON-mode，pro 更稳 |

### 13.2 实施位置

| 位置 | 改动 |
|------|------|
| `config/ai_config.json` | `deepseek.model = deepseek-v4-pro` |
| `core/ai_config.py::get_available_models` | 模型列表保留 V4 系列（pro / flash），默认 pro |
| `core/ai_config.py::get_theme_extraction_config` | 默认 `model = deepseek-v4-pro` |
| `core/ai_news_analyzer.py::_stream_chat` | deepseek + v4 开头时注入 `extra_body={"thinking":{"type":"enabled"},"reasoning_effort":"max"}` |
| `core/news_cleaner.py::_call_provider` | provider=deepseek 时硬编码 `model="deepseek-v4-pro"`（不开 thinking） |
| `core/theme_extractor.py::__init__` | 默认 `model = deepseek-v4-pro`，stream=True + response_format=json_object |
| `services/market/ai_enricher.py::_call_chat_json` | `DEFAULT_MODEL=deepseek-v4-pro`，强制 `thinking=disabled` |
| `gui/pages/news_cleaning_page.py` | 标签显示「清洗模型: deepseek-v4-pro」 |

### 13.3 调用链

```
AI 分析:
    GUI/AIAnalysisPage -> AIAnalysisWorker
        -> AnalysisService.analyze()
            -> AINewsAnalyzer._stream_chat(provider="deepseek")
                -> model = config.deepseek.model  # = deepseek-v4-pro
                -> extra_body = {thinking: enabled, reasoning_effort: max}

新闻清洗 / 题材抽取 / 盘后兜底:
    各自的 service / extractor / enricher
        -> client.chat.completions.create
            -> model = "deepseek-v4-pro"
            -> extra_body = {thinking: disabled}   # 关键：禁思考链
```

### 13.4 兼容性

- 其他 provider（openai / qwen / zhipu / volcengine）逻辑不变，沿用 config 中的 model
- 用户在 GUI 切换 model 影响 **AI 分析**；清洗 / 题材 / 盘后兜底仍硬编码 `deepseek-v4-pro`
- 旧别名 `deepseek-chat` / `deepseek-reasoner` 仍能跑（2026-07-24 前），但不再是默认值
- 想退回旧的「清洗用 flash」模式时，只需把对应模块的硬编码改回 flash，无需改 config

### 13.5 注意事项

- `AINewsAnalyzer.test_connection()` 内部走 `_stream_chat`，连接测试也会开 thinking max——比普通 ping 慢，但能跑通
- DeepSeek V4 max_tokens 上限较高（V4-Pro 支持 64K 输出），当前 config 默认 8192 够用，必要时主人可上调

### 13.6 思考过程输出到报告

> 2026-05-22 补充

V4-Pro 思考模式下，DeepSeek API 流式返回中：
- `chunk.choices[0].delta.reasoning_content` —— 思考过程（先到，长链推理）
- `chunk.choices[0].delta.content` —— 正式分析（后到，最终结论）

`_stream_chat` 流式接收两段，**直接拼接为一个字符串**：

```text
## 🧠 模型思考过程
<reasoning 全文>

---

## 📋 主要分析
<content 正式分析>
```

返回字符串原样给 `save_report` 写入 md → 思考过程出现在报告最前部（标题/元数据之后）。GUI 端流式时也是「思考先显示，正式分析后显示」，无需折叠 / 单独控件，最简单的实现路径。

调用方影响（兼容性）：
- `_stream_chat` 返回类型仍是 `str` ✅
- `analyze()`、`analyze_with_xxx()` 等公开方法签名不变
- `save_report` / `_extract_referenced_news` 不需要改（reasoning 中若提到「新闻X」会被一起收录到底部引用区，更便于读者追溯思考链）

---

## 14. 题材预测库（2026-05-22）

> 触发：主人需求——分析报告生成后用另一个 AI 抽取出结构化题材表，落 SQLite 便于跨日跟踪、反查与画热度曲线。

### 14.1 设计目标

| 目标 | 实现 |
|------|------|
| 不污染主分析链路 | 题材抽取在 `analyze()` 末尾追加，失败不抛、只填 `theme_error` |
| 不浪费 token | 抽取走 `deepseek-v4-pro` 非思考模式（thinking=disabled），分类任务 |
| 跨日可追踪 | 快照式入库：同题材每份报告一条独立行，可画 strength 时序图 |
| 支持双向查询 | 题材→标的、标的→题材 都有索引 |
| 等级/分数自洽 | `ThemeStore._normalize_theme()` 按 score 区间覆写 level，避免脏数据 |

### 14.2 数据流

```
AnalysisService.analyze()
       │  报告.md 已存
       ▼
_maybe_extract_themes()             ← 失败不抛，主流程稳如老狗
       │
       ▼
ThemeExtractor.extract_from_file(report_path)
       ├── _read_report()           ← 截掉末尾「📰 引用新闻详情」节省 token
       ├── _build_prompt()          ← 系统提示词内嵌 JSON schema
       ├── _call_llm()              ← OpenAI 兼容 SDK，response_format=json_object
       ├── _parse_response()        ← 容错：json.loads → 正则提 {} 兜底
       └── _sanitize_theme()        ← 枚举值/数值边界限制
       │
       ▼  返回 List[Dict]
ThemeStore.save_themes(report_meta, themes)
       ├── _normalize_theme()       ← score/level 自洽校正
       ├── INSERT theme_predictions
       ├── INSERT theme_stocks (batch)
       └── INSERT theme_news (batch)
```

### 14.3 表结构（已写入 `services/storage/database.py`，**v4 2026-05-27**）

**主表 `theme_predictions`** —— 20 列，按报告快照存储：
- 来源元数据：`report_id`（文件名 string，**非 ai_reports.id**）/ `report_date` / `report_time` / `report_path`（相对 posix）
- 题材属性：`theme_name` / `theme_category` / `strength_score` (**-100~+100 带符号**) / `strength_level`（**9 档**）
- 业务属性：`priority_rank` / `duration` / `expectation_gap` / `is_cold`
- 内容：`reason` / `risk_note`
- **v4 新增**：`prompt_id` / `prompt_version`（从 ai_reports 反查冗余）；`sector_ts_code` / `sector_match_conf`（matcher 富化结果）
- 索引：`(report_date DESC)`、`(theme_name)`、`(report_date, strength_score DESC)`、`(theme_category)`、`(report_id)`、`(prompt_id, prompt_version)`、`(sector_ts_code)`

**关联表 `theme_stocks`** —— `theme_id` 外键 + CASCADE：
- `stock_name` (必) / `stock_code` (可空，AI 原始) / **`normalized_code` (v4 新增，matcher 规范化如 600172.SH)** / `role` / `reason`
- 索引：`(theme_id)`、`(stock_name)`、`(stock_code)`、`(normalized_code)`，支持反查"某只票踩了哪些题材"

**关联表 `theme_news`** —— `theme_id` 外键 + CASCADE：
- `news_ref` (报告锚点如"新闻107") / `news_id` (可关联 raw_news.id) / `relation_type` (主因/共振/风险/背景)

### 14.4 强度评分自洽规则（v4：带符号 + 9 档）

`strength_score` 改为 **-100 ~ +100 带符号整数**：正数 = 利多（绝对值越大越强），负数 = 利空（绝对值越大越强），0 = 中性。**v3 (2026-05-27) 起删除冗余 `sentiment` 字段，由 score 正负号承载方向。**

| 输入 `strength_score` | 输入 `strength_level` | 入库结果 |
|---------------------|---------------------|----------|
| 85 | 重大利多 | score=85, level=重大利多 ✅ 自洽 |
| 95 | 弱利空（冲突） | **score=95 优先**，level=重大利多（覆写） |
| -65 | 较强利空 | score=-65, level=较强利空 ✅ 自洽 |
| -50 | None | score=-50, level=弱利空（按 score 推断） |
| None | None | score=0, level=中性（兜底） |
| 999 | * | score=100（clamp 上限），level=重大利多 |

**9 档对照区间**（由 `_score_to_level()` 派生）：

| 分数区间 | 等级标签 |
|---------|---------|
| score ≥ +80 | 重大利多 |
| +60 ~ +79 | 较强利多 |
| +40 ~ +59 | 弱利多 |
| +1 ~ +39 | 中性偏多 |
| 0 | 中性 |
| -39 ~ -1 | 中性偏空 |
| -59 ~ -40 | 弱利空 |
| -79 ~ -60 | 较强利空 |
| score ≤ -80 | 重大利空 |

**`strength_score` 永远是裁判**。

### 14.5 v4 配套修复点（review 发现）

- **P1 路径规范化**：`report_path` 入库前统一 `_to_relative_posix`，与 `ai_reports.file_path` 对齐，让下游 `get_by_path` 反查 prompt_id 能命中
- **P2 重复入库防护**：`save_themes` 入口先 `DELETE WHERE report_id = ?`，同 md 抽两次自动幂等
- **P6 组合筛选**：`get_by_date(date, prompt_id=None)` 双维过滤 + `list_distinct_prompts()` 给 GUI 下拉
- **N1 路径反查约定**：`theme_predictions.report_id`（文件名）≠ `ai_reports.id`（自增 INT），不能 join，必须走 `report_path` 反查
- **N2 调度接入**：4 个新任务类型（`stock_daily_sync` / `sector_daily_sync` / `theme_score_daily` / `theme_ai_review`）已加入 `scheduled_runner.py` 桩，等 plan M3 真实逻辑

详见 [doc/design/05-27-1003-题材抽取保存与GUI补全设计.md §10 / §10·B](05-27-1003-题材抽取保存与GUI补全设计.md)。

### 14.5 配置项（`config/ai_config.json`）

```json
"theme_extraction": {
  "enabled": true,        // 总开关
  "auto_run": true,       // 分析后是否自动跑
  "provider": "deepseek",
  "model": "deepseek-v4-pro",     // 2026-05-26 起统一 pro，禁 thinking
  "temperature": 0.2,             // 抽取任务，降随机性
  "max_tokens": 8192
}
```

`AIConfig.get_theme_extraction_config()` 内置默认值，老配置缺该段也不会崩。

### 14.6 调用 / 反查 API

```python
from services.storage import get_theme_store

store = get_theme_store()

# 按报告查（含 stocks/news 展开）
store.get_by_report("5月22日_18时25分_盘后总结分析报告")

# 按日查（同日多次分析全部返回）
store.get_by_date("2026-05-22")

# 题材跨日历史（画热度曲线）
store.get_theme_history("具身智能/人形机器人", limit=30)

# 反查：某只票踩过哪些题材
store.get_stock_themes("拓普集团", limit=50)

# 删除指定报告的全部题材（CASCADE 子表）
store.delete_by_report("5月22日_18时25分_盘后总结分析报告")
```

### 14.7 失败容错

| 场景 | 行为 |
|------|------|
| `enabled=false` 或 `auto_run=false` | 跳过，不报错 |
| LLM 调用异常 | `result.theme_error = '调用 LLM 失败: ...'`，主报告正常 |
| AI 输出被截断（`finish_reason=length`） | 自动启用 JSON 修复 + 部分救援；提示主人调大 `max_tokens` |
| JSON 解析失败 | 四级兜底：`json.loads` → 正则提 `{}` → 截断修复（补 `}` `]` + 删尾逗号） → 字符级扫描"已完成对象"救援 |
| AI 返回脏数据 | `_sanitize_theme()` 枚举值/数值越界归零，缺 `theme_name` 或 `reason` 直接丢弃 |
| 全部解析失败 | 把报告长度 + 原始 LLM 输出转储到 `.huiye/_last_theme_extract_failure.txt` 便于排查 |
| 主表入库失败 | 整批回滚（事务），不会有半截数据 |

**截断修复算法核心**（`_repair_truncated_json` + `_salvage_theme_objects`）：
1. 状态机扫描，区分"字符串内/外"——这是 JSON 修复的关键
2. 末尾停在未闭合字符串里 → 砍到上一个安全位置
3. 删除尾随空白 + 悬挂逗号
4. 按栈序补 `}` `]`
5. 仍失败则切到字符级救援：从 `"themes": [` 开始逐个对象做花括号平衡，能 parse 的就收，截断的对象丢弃

### 14.8 关键事实

- **不复用 `AINewsAnalyzer._stream_chat`**：避免被思考模式 + 流式拖累。题材抽取自己拿 OpenAI SDK 走 `chat.completions.create(stream=False, response_format=json_object)`
- **报告原文截断**：`_read_report()` 砍掉「📰 引用新闻详情」节，降低 token 30-50%
- **`raw_excerpt` 字段保留**：方便人工抽检 AI 幻觉，预留二期"AI 抽取质量评估"用
- **CASCADE 删除**：删主表自动清子表，无需手动维护

### 14.9 GUI 接入（2026-05-22）

**新增页面**：`gui/pages/theme_prediction_page.py`（侧栏 `🎯 预测题材`，位于 AI 分析与定时任务之间）

```
ThemePredictionPage
 ├── 上半区：手动抽取
 │     ├── _on_browse()         QFileDialog 选 .md
 │     └── start_extract()
 │           └→ ThemeExtractWorker (QThread)
 │                 ├→ ThemeExtractor.extract_from_file()
 │                 ├→ parse_report_meta()
 │                 └→ ThemeStore.save_themes()
 │           └→ _on_extract_finished() 自动刷下半区到对应日期
 │
 └── 下半区：查看
       ├── date_combo                按日期分组（distinct report_date）
       ├── theme_table (主表)        时间/题材/等级/分数/情绪/冷处理/持续性/预期差/排名/催化
       │     └→ _on_theme_selected() → _show_theme_detail()
       └── detail_tab
             ├── reason_browser      reason / risk_note / raw_excerpt / 共振数 / 大类
             ├── stock_table         标的/代码/角色/弹性(⭐)/理由
             └── news_table          锚点/关联类型/标题
```

**AI 分析页改动**：`gui/pages/ai_analysis_page.py`
- 在「🎯 分析参数」组末尾新增 `QCheckBox`：`📌 分析完成后自动抽取题材并入库`，**默认勾选**
- `start_analysis()` 把 `self.extract_theme_check.isChecked()` 传给 `AIAnalysisWorker(extract_themes=...)`
- `on_analysis_finished()` 摘要里多打印一行题材入库数 / 错误

**调用链改动**：
```
GUI [QCheckBox] ──┐
                  ▼
AIAnalysisWorker(extract_themes=bool)
                  ▼
AnalysisService.analyze(extract_themes=bool)
                  ▼
_maybe_extract_themes(force: Optional[bool])
    force=True  → 强制跑（仍需 enabled=True 且 API Key）
    force=False → 强制跳过
    force=None  → 走 config.auto_run（Agent / 定时任务路径）
```

**关键 worker**：`gui/workers/theme_extract_worker.py`
- 信号：`progress(str)` / `finished(dict)` / `error(str)`
- `finished` payload：`{'ok': True, 'report_id', 'report_date', 'report_time', 'themes', 'stocks', 'news', 'warning'}`

**主窗口改动**：`gui/main_window.py`
- 新增 import `ThemePredictionPage`
- `self.pages['theme_prediction'] = ThemePredictionPage()`
- nav_items 在 `ai_analysis` 与 `schedule` 之间插入 `('theme_prediction', '🎯 预测题材')`

### 14.10 v2 schema 精简（2026-05-22）

> 触发：基于真实抽取数据（5月22日_18时48分 报告，输出 4818 tokens）的诊断——大量 null、与 reason 冗余的 raw_excerpt、永不填的 news.title 共烧掉约 1500 字符。主人确认精简方案后落地。

#### 14.10.1 schema diff

| 表 | 删除字段 | 保留要点 |
|----|---------|----------|
| `theme_predictions` | `catalyst`、`resonance_count`、`raw_excerpt` | `reason` 字段语义扩展为「核心逻辑 + 催化事件」融合；`risk_note` 保留单独字段 |
| `theme_stocks` | `elasticity` | 其他不变 |
| `theme_news` | `news_title` | **`news_id` 由"曾经一直为空"升级为"由脚本反查报告底部填入"** |

#### 14.10.2 反查机制（核心改动）

```
[1] AI 分析报告 .md 底部「📰 引用新闻详情」段
    每条新闻在 ### 新闻N 之后、**时间** 之前新增一行：
        **数据库ID**: `abc123def`         ← raw_news.id

[2] AI 题材抽取只看截断版报告（不见引用区）
    输出 news 字段精简为字符串数组：
        "news": ["新闻107", "新闻125"]

[3] ThemeExtractor.extract_from_file() 同时返回 news_id_map
    parse_news_id_map(report_path) 扫底部 <a id="新闻N"></a> + **数据库ID**: 
    生成 {"新闻107": "abc123...", "新闻125": "..."}

[4] ThemeStore.save_themes(themes, news_id_map=...) 入库时
    theme_news.news_ref = "新闻107"   (AI 给)
    theme_news.news_id  = "abc123..." (脚本反查)
    若 AI 直接给 id 则优先用 AI 的

[5] GUI 查看 → 反查 raw_news 拿 title/source/published_at
```

#### 14.10.3 prompt 关键改动

- 由"未知字段填 null"改为"**未知字段直接省略键名**"，AI 不再输出无意义 null
- `reason` 字段语义扩展：1-3 句话融合"利好/利空逻辑 + 催化事件"
- `news` 字段标准格式从对象数组改为字符串数组
- `relation_type`（主因/共振/风险/背景）变为可选——如果 AI 想标注则使用扩展形式 `[{"ref":"新闻107","rel":"主因"}]`

#### 14.10.4 收益预估

| 项目 | v1 | v2 | 节省 |
|------|---:|---:|-----:|
| 输出 tokens | 4818 | ≈ 3000 | -38% |
| 单次成本 | 0.022 元 | ≈ 0.014 元 | -36% |
| 调用耗时 | 86 秒 | ≈ 55-60 秒 | -30% |

#### 14.10.5 数据迁移

`database.py::_migrate_theme_tables_v2()` 自动迁移：
- `init_database()` 启动时检测 `theme_predictions`/`theme_stocks`/`theme_news` 列集合
- 若与 v2 定义不一致 → DROP 三张题材表 → 由 CREATE TABLE IF NOT EXISTS 重建
- 题材数据是衍生的（从分析报告 .md 抽取），重新跑就能再生，比写 ALTER TABLE 迁移更干净
- 启动时通过 `logging.warning` 输出"schema 不一致，已清空重建"提示

#### 14.10.6 兼容性

- `ThemeExtractor.extract_from_file()` 返回签名变为三元组 `(themes, news_id_map, err)`，**breaking change**
- `ThemeStore.save_themes()` 新增可选参数 `news_id_map=None`，向后兼容
- `_sanitize_theme._normalize_news_field()` 同时支持新格式（字符串数组）和老格式（对象数组），无迁移成本
- `ThemeStore` 内部 news 处理也加了 `isinstance(news, str)` 兜底，Agent 直接传字符串也不会崩

---

## 15. 盘后数据模块（2026-05-26，M0~M5 全部落地）

> 详细的施工 + 验收记录见 [盘后数据自动化-施工方案.md](./05-26-2126-盘后数据自动化施工方案.md)，
> 主人视角的功能描述见 [盘后数据自动化-功能说明.md](../features/05-26-2126-盘后数据自动化功能说明.md)。
> 本节只做"架构层面"的速查 —— 是什么、放在哪、跟既有模块如何衔接。

### 15.1 目标

让电脑每天 16:00 自动生成一份"硬数据为主、AI 兜底为辅"的盘后总结，
给主人手动复盘、给 AI 分析页带上下文、给后续 CLI 回测系统打底。

### 15.2 物理存储 — 双 SQLite 隔离

```
data/
├── news.db                       # 既有
│   └── 新增表 ai_reports          ← AI 分析 md 索引（M4.4 实装）
└── market.db                     # 🆕 全部盘后数据
    ├── dim_*                     维度表（4 张：trade_calendar / sector / stock / trader_alias）
    ├── fact_*                    事实表（10 张，覆盖 Tushare + 财联社）
    ├── market_summaries          每日 canonical JSON + compact Markdown + 完整度
    ├── ai_enrich_patches         AI 兜底产出（prompt_id / version / tokens / patch_json）
    └── schema_migrations         版本号（当前 3）
```

跨库访问通过 `ATTACH 'data/market.db' AS m` 实现，AI 分析报告 / CLI 回测均可 JOIN。

### 15.3 代码层 — `services/market/`

```
services/market/
├── market_db.py                  连接、迁移、备份
├── tushare_client.py             API 客户端（重试 / 超时 / 节流）
├── tushare_fetcher.py            11 个接口 + 11 张 fact_* 表的 fetch/ingest
├── trade_date.py                 交易日解析（带 dim_trade_calendar 缓存）
├── metrics.py                    派生指标（封板率/晋级率/最高板/连板梯队）
├── cls_enricher.py               CLS 异动数据 → sectors_top[].catalysts（三级匹配）
├── ai_enricher.py                AI 兜底（仅未命中板块 + 未识别游资）
├── merger.py                     Tushare > CLS > AI 优先级合并
├── validator.py                  schema-driven 完整度评分
├── renderer.py                   canonical JSON → compact Markdown
├── trader_aliases.py             dim_trader_alias 读写 / 三级匹配
├── service.py                    MarketSummaryService（主入口）
└── migrations/                   001_initial / 002_seed_traders / 003_relax_sector
```

### 15.4 GUI 集成 —— 在主导航插了一页

| 位置 | 页面 |
|-----|-----|
| 数据导出 ↔ AI 分析 之间 | **📊 盘后数据** (`gui/pages/market_summary_page.py`) |
| 该页内对话框 | 👥 编辑游资别名 (`gui/pages/trader_alias_dialog.py`) |
| 后台 worker | `gui/workers/market_fetch_worker.py` |
| 定时任务页 | 「⏰ 定时任务」新建任务 → 类型下拉新增「盘后数据拉取」 |
| AI 分析页 | 复用既有 `summary_input` 控件，**不动 ai_analysis_page** |

### 15.5 三种模式（main 取舍维度：API + AI 成本 vs 完整度）

| 模式 | Tushare | CLS | AI 兜底 | 完整度 | 单日耗时 |
|-----|:----:|:---:|:---:|:----:|:----:|
| `tushare-only` | ✅ | ❌ | ❌ | 60-75% | ~10s |
| `hybrid`（推荐） | ✅ | ✅ | ✅ 仅未命中 | 90-95% | 25-50s |
| `ai-full` | ≡ hybrid | ≡ hybrid | ≡ hybrid | 同左 | 同左 |

> M5 起 `ai-full` 占位，等同 `hybrid`；后续若引入"AI 主动文字摘要 / 异常解释 / 次日打板焦点"才会真正分化。

### 15.6 配置 —— `core/ai_config.py::get_market_fetch_config()`

新增独立配置段 `ai_config.json::market_fetch`，控制 AI 兜底环节：

```jsonc
{
  "market_fetch": {
    "enabled": true,
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "temperature": 0.2,
    "max_tokens": 2000,
    "timeout": 90,
    "enable_sectors": true,    // false → 板块催化只走 CLS
    "enable_traders": true     // false → 游资识别只读 dim_trader_alias
  }
}
```

优先级链：`market_fetch 配置 → provider 配置 → prompt frontmatter → 代码默认`，任一层缺失自动降级。

> ⚠️ **2026-05-26 21:00 review 发现**：当前 `config/ai_config.json` **完全没有 `market_fetch` 段**，
> 全靠 `get_market_fetch_config()` 内置默认值兜底运行（功能正常）。
> 如果主人想可视化调整 `enable_sectors / enable_traders / max_tokens` 等，
> 需把上面的 jsonc 段实际复制到 `ai_config.json`。详见 [review_report_2.md](../reports/05-26-2100-第六轮项目评审.md) §H2。

### 15.7 与既有模块的衔接

| 既有模块 | 衔接方式 |
|---------|----------|
| `services/analysis_service.py` | `analyze(auto_market=True)` 自动调 `MarketSummaryService.build()`，结果填入 `market_summary` 参数；失败仅 progress 提示不阻断主流程 |
| AI 报告生成后 | 调 `services.storage.record_report()` 写入 `news.db.ai_reports`（含 prompt_id / version / used_market_date 等） |
| `services/scheduled_runner.py` | 新分支 `task_type == "market_fetch"` |
| `core/scheduler_service.py` | 不动 |
| 题材抽取 | 不动；题材抽取完成后自动 `mark_theme_extracted(True)` 更新 ai_reports.theme_extracted=1 |

### 15.8 CLI 工具

```bash
# 单日拉取
python tools/market_fetch.py [date] [mode]

# 批量回填
python tools/market_fetch_backfill.py --days 30 --mode hybrid \
    --report data/backups/backfill_30d.csv
```

### 15.9 数据资产现状（截至 2026-05-26）

| 表 | 行数 |
|----|-----:|
| market_summaries | 30 天（20260410 ~ 20260526） |
| fact_top_inst | 21,660 |
| fact_cls_stock_shock | 4,888 |
| dim_trader_alias | 48 条 / 知名 46 个 |
| ai_enrich_patches | 27 条（M5 期间每天产出） |
| `market.db` 总体积 | ~44 MB |

### 15.10 模块边界 / 风险

- **不动** `core/ai_news_analyzer.py` 既有签名（M0 之后已稳定）
- **不动** `data/news.db` 老表 schema，只**新增** `ai_reports` 一张表
- 北向资金可能 T-1 延迟 → `capital_flow.north_is_delayed=True` + warning 提示
- Tushare 偶发 timed out → 内置重试 1 次；M5 期间观察到 30 天里 1 次重试，成功率 100%
- AI 兜底失败不阻塞 → patch 为空 + warning 提示，主流程仍出 summary
- `dim_sector` 唯一约束放宽（migration 003）以容忍 Tushare 数据中同名板块的 ts_code 漂移
