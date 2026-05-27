# 施工文档·模板回测功能（A+B 并行 + AI 评分员全量 + 三库重构）

> 内部施工 checklist · 辉夜自用
> 文件名说明：原计划用 `_施工_模板回测功能.md`，但 Cursor Write 工具对该中文名有 bug，改用英文名
> 设计文档（主人通读版）：[doc/design/05-27-1140-模板回测功能设计.md](../doc/design/05-27-1140-模板回测功能设计.md)
> 设计文档（端到端流程）：[doc/design/05-27-1025-题材抽取打分完整流程.md](../doc/design/05-27-1025-题材抽取打分完整流程.md)
> **设计文档（三库 schema）**：[doc/design/05-27-1209-三库表结构详细设计.md](../doc/design/05-27-1209-三库表结构详细设计.md) ⭐
> 编写时间：2026-05-27 11:49
> v3 修订：12:18（插入 Phase -1 三库重构 + 修正 19 份 md + 改为虚拟回测堆样本路径 A）
> v3.1 修订：13:05（主人指定虚拟回测仅 3 个模板 + 文件名英文化恢复）
> 总工期：**~5.5-6 个工作日**
> 状态：⚪ 待启动

---

## 0. 主人决策落地（v3.1 终版）

| 决策项 | 主人选择 | 影响 |
|--------|---------|------|
| 实施顺序 | **A+B 并行** | 5 天，complexity 提升 |
| 脚本打分指标 | 6 个全做 | 一次 SQL 算齐，不增加工期 |
| 追踪期 | D+5 固定 | 一期不可配置 |
| ~~历史回填范围~~（已修正） | ~~"~200 份 md"是辉夜误估~~ | **实际仅 19 份**（5/22-5/27） |
| **样本积累路径** 🆕 | **路径 A：虚拟回测堆样本** | 不批量补抽 |
| **虚拟回测模板范围** 🆕 v3.1 | **只跑 3 个主人指定模板** | `custom_1770292858`（三位一体综合分析_2月5日）/ `custom_6`（新闻分析）/ `custom_7`（超级综合模板） |
| **样本规模** 🆕 v3.1 | **3 × 14 = 42 份** | 替代 v3 的 12×14=168 份 |
| **批量回测成本** 🆕 v3.1 | **~$0.42 + ~6 分钟** | LLM ~42 次 DeepSeek |
| **历史 md 处理** | **不批量补抽**，保留磁盘文件 | 主人决策"过去生成的报告先忽略" |
| fact_stock_daily 范围 | 全 A 股 ~5400 只 | ~50 MB / 30 天 |
| AI 评分员 | **一期就上**（与并行决策配套） | +0.5 天，月成本 ~$2.4 |
| **底层架构** 🆕 | **三库重构**：news / market / ai_inference | +0.5~1 天 |
| **新库命名** 🆕 | `data/ai_inference.db` | 抄 MarketDB 范式 |
| **theme_news 归属** 🆕 | 跟着 theme_* 进 ai_inference.db | news_id 跨库软引用 |
| **旧数据处理** 🆕 | **直接清空** news.db 的 4 张老表 | 不迁移、不留 48h 观察期 |
| **重抽 19 份历史** 🆕 | **不抽** | 路径 A 决策的延伸 |

辉夜碎碎念：v3.1 主人把模板范围收窄到 3 个，更聚焦了——只对自己常用的核心模板跑回测，避免给 12 个模板都堆样本浪费 LLM 调用。这次决策又准又省。

---

## 1. 核心架构决策（**写代码前必须先理解**）

### 1.1 三库分配方案（v3 重大调整）

> 详见 [三库表结构详细设计](../doc/design/05-27-1209-三库表结构详细设计.md)

| 库 | 表 | 职责 | 备份级别 |
|----|------|------|--------|
| **news.db**（瘦身） | raw_news / sync_meta | 爬虫产物，不可重建 | **严格备份** |
| **market.db**（追加 2 张） | 原有 dim_* / fact_* + 🆕 `fact_stock_daily` / `fact_sector_daily` | 接口产物，理论可重建 | 中等备份 |
| **ai_inference.db**（新建） | `ai_reports` / `theme_predictions` / `theme_stocks` / `theme_news` / 🆕 `theme_prediction_scores` / 🆕 `theme_stock_scores` | AI 生成 + 算法衍生，完全可重建 | 弱备份 |

**新表与字段速查**：

| 文件 | 表 | 主要新字段 |
|------|------|----------|
| `services/market/migrations/005_fact_daily_quotes.sql` | `fact_stock_daily` / `fact_sector_daily` | ts_code / trade_date / pct_chg / vol / amount 等 |
| `services/storage/ai_migrations/001_initial.sql`（新库基线） | `ai_reports`（含 `is_backtest`） / `theme_predictions`（含 `is_backtest`） / theme_stocks / theme_news / `theme_prediction_scores` / `theme_stock_scores` | 见 §3 详细设计 |

**关键约束**：
- 同库内 FK + CASCADE（题材删 → 标的/新闻/打分自动清）
- 跨库**软引用**（应用层维护，SQLite 不支持跨库 FK）：
  - `ai_inference.theme_news.news_id` → `news.raw_news.id`
  - `ai_inference.theme_stocks.normalized_code` → `market.dim_stock.ts_code`
  - `ai_inference.theme_predictions.sector_ts_code` → `market.dim_sector.ts_code`
  - `ai_inference.theme_prediction_scores` 跨库查 `market.fact_stock_daily` / `fact_sector_daily`

### 1.2 复用现有基础设施清单

| 复用对象 | 用在哪 | 原因 |
|---------|-------|------|
| `services.market.tushare_client.TushareClient` | 行情同步、AI 评分员前置 | 现成的 token / 重试 / 限频 |
| `services.market.trade_date.resolve_trade_date` | 打分时确定 score_date | 现成日历缓存 |
| `services.market.market_db.MarketDB` | 行情入库 + **被 AIInferenceDB 抄范式** | 现成的迁移 / 备份 / WAL |
| `MarketDB.attach_to(conn, alias)` | 跨库 JOIN 标准范式 | ATTACH/DETACH 已封装 |
| `services.scoring.matcher.normalize_stock_code` | 标的代码标准化 | v4 已落地 |
| `core.theme_extractor.ThemeExtractor` | 题材抽取主流程 | v4 已落地（需改连接对象） |
| `services.analysis_service.AnalysisService.analyze` | B 期虚拟 md 生成 | 加 start/end 参数即可时间截断 |
| `core.scheduler_service` + `services.scheduled_runner` | 4 个调度桩直接替换 | v4 已留好桩 |
| `gui.pages.prompt_eval_page.PromptEvalPage` | 真实数据替换桩 | v4 已落地骨架 |
| `services.storage.theme_store.ThemeStore` / `ai_reports_store.AIReportsStore` | 题材 / 报告 CRUD | v4 已落地，**Phase -1 仅改连接源** |

### 1.3 防穿越纪律（**A 期就要遵守**）

| 铁律 | 实现位置 |
|------|---------|
| 打分用的行情必须是 D+N 当天 | `script_scorer.py` 强约束 `score_date > report_date` |
| 跨周末/节假日跳过 | 新增 `next_trade_date(date, n)` 工具函数 |
| B 期快照只能看 cutoff 之前的新闻 | `snapshot.py` 内 assert 所有 published_ts < cutoff_ts |
| 自动化测试硬约束 | 每个 phase 的 CLI 用例都要有"穿越场景必须失败"的反向测试 |

### 1.4 v3.1 样本积累策略

| 策略 | 详情 |
|------|------|
| 19 份历史 md | **不批量补抽**，保留磁盘；以后主人愿意可单独跑 |
| 主样本来源 | B 期 `tools/backtest_prompt.py` 虚拟回测 |
| **回测模板范围**（v3.1） | **3 个主人指定模板**：`custom_1770292858` / `custom_6` / `custom_7` |
| 一次性堆样本（v3.1） | 14 交易日 × 3 模板 = **42 份** + 真实新增 |
| 成本（v3.1） | **~$0.42 LLM + ~6 分钟**（DeepSeek，并发 4） |
| 数据标记 | `ai_reports.is_backtest` + `theme_predictions.is_backtest` |
| GUI 体验 | 模板评估页加"真实/回测/全部"筛选 |

---

## 2. Phase 拆解（按依赖与并行性）

### 关键路径图（v3）

```
Phase -1 (三库重构，必须最先) ⭐ 新增
   ↓
Phase 0 (基础 schema 健康检查)
   ├──→ Phase 1 (行情同步)   ──→ Phase 2 (打分核心) ──→ Phase 4 (GUI 接通) ──→ Phase 8
   └──→ Phase 5 (快照重建) ─────────────────────────→ Phase 6 (虚拟回测+堆样本) ──→ Phase 8
        └──→ Phase 7 (AI 评分员)（依赖 Phase 2 打分输出） ──→ Phase 8

Phase 3 (批量补抽历史 md) — ❌ 取消（主人决策"过去报告先忽略"）
```

并行机会（v3）：
- **Day 1**：Phase -1 三库重构（独占）
- **Day 2 下午**：Phase 1（A 行情同步）与 Phase 5（B 快照重建）独立并行
- **Day 3 下午**：Phase 2 收尾 与 Phase 6 起步（B 虚拟回测）并行
- **Day 4 下午**：Phase 4（A GUI）与 Phase 7（AI 评分员）并行
- **Day 5 上午**：Phase 6 收尾（含 3 模板 × 14 天批量堆样本 ~6 分钟）
- **Day 5 下午**：Phase 8 联调 + GUI 验收
- **Day 6**：buffer / 文档同步

---

## Phase -1 · 三库重构（**0.5~1 天**，必须最先）⭐ v3 新增

> 本 Phase 是 v3 修订的核心：把 AI 衍生数据从 news.db 抽离到独立 ai_inference.db，
> 抓住"theme_* 已清空 + 打分系统未开工"的最佳窗口期。
> 详细 schema 见 [doc/design/05-27-1209-三库表结构详细设计.md](../doc/design/05-27-1209-三库表结构详细设计.md)

### Step -1.1 · AIInferenceDB 类（**完全照抄 MarketDB 范式**）

**文件**：`services/storage/ai_inference_db.py`（新建）

**接口**（与 MarketDB 对称）：
```python
class AIInferenceDB:
    def __init__(self, db_path=None, migrations_dir=None, backup_dir=None) -> None: ...

    @contextmanager
    def connect(self, *, readonly=False) -> Iterator[sqlite3.Connection]:
        """启用 WAL / foreign_keys / busy_timeout=30s / row_factory=Row。"""

    def attach_to(self, foreign_conn, alias='a') -> None:
        """把本库 ATTACH 到外部连接，跨库 JOIN 用。"""

    def ensure_schema(self, *, do_backup=True) -> List[int]:
        """跑迁移目录下所有 .sql；已应用版本记 schema_migrations。"""

    def backup_on_startup(self, *, tag='startup') -> Optional[Path]: ...
    def backup_to(self, target) -> Path: ...
    def verify_integrity(self) -> Tuple[bool, str]: ...
    def get_stats(self) -> dict: ...

def get_ai_inference_db() -> AIInferenceDB:
    """进程内单例。"""
```

**实现要点**：
- 抄 `services/market/market_db.py` 95% 代码，改 3 处：
  1. `DEFAULT_DB_PATH` → `_PROJECT_ROOT / "data" / "ai_inference.db"`
  2. `DEFAULT_MIGRATIONS_DIR` → `Path(__file__).parent / "ai_migrations"`
  3. 类名 / 全局单例名
- backup 文件命名 `ai_inference.db.{tag}.{ts}.bak`
- 默认 ATTACH alias 用 `'a'`（与 market 的 `'m'`、news 的 `'n'` 对称）

### Step -1.2 · 001_initial.sql 建 6 张表

**文件**：`services/storage/ai_migrations/001_initial.sql`（新建）

**包含**（详见三库表结构设计文档）：
- `ai_reports`（**含 `is_backtest` 字段**）
- `theme_predictions`（v5：**含 `is_backtest` 字段**，其余字段同 v4）
- `theme_stocks`（同 v4）
- `theme_news`（同 v4）
- `theme_prediction_scores` 🆕
- `theme_stock_scores` 🆕
- 共 17 个索引 + 6 个外键

**⚠️ 注意**：sql 文件**不要**自己写 `INSERT INTO schema_migrations`，该表由 `AIInferenceDB._apply_migration()` 自动建表 + INSERT 版本号（抄 MarketDB 范式）。

**⚠️ FK 启用约束**：001 sql 内部不写 `PRAGMA foreign_keys=ON`，MarketDB._apply_migration 已经临时关 FK 跑迁移；FK 由每次 `connect()` 启用。

### Step -1.3 · market.db 迁移 005 新表

**文件**：`services/market/migrations/005_fact_daily_quotes.sql`（新建）

**内容**：`fact_stock_daily` + `fact_sector_daily` + 4 个索引（详见三库表结构设计文档 §2.1 / §2.2）

### Step -1.4 · news.db 瘦身（清空 4 张老表）

**文件**：`services/storage/database.py`（改）

**改动**：
1. 从 `_SCHEMA` 字符串里**删除** 4 张表 + 它们的索引：
   - `theme_predictions` / `theme_stocks` / `theme_news` / `ai_reports`
2. 删除 `_THEME_V4_COLUMNS` 字典和 `_migrate_theme_tables_v4` 函数（迁库后用不上）
3. 新增 `_drop_legacy_ai_tables(conn)` 函数：在 `init_database` 入口处一次性 DROP 4 张老表（包含 sqlite_sequence 清理）
4. `_SCHEMA` 瘦身后只剩 `raw_news` + `sync_meta`

**幂等性**：DROP IF EXISTS 重复跑无害

### Step -1.5 · storage / GUI / 入口层迁移（**8 个文件**）⭐ v3 review 补全

> 第一轮拟定时漏了 GUI 两个页面 + main.py 启动入口，review 后补齐。

| 文件 | 改动 | review 来源 |
|------|------|------------|
| `services/storage/__init__.py` | 把 `init_database` 改为同时初始化 news.db + market.db + ai_inference.db；导出 `get_ai_inference_db` | 入口 |
| `services/storage/ai_reports_store.py` | `from services.storage.database import get_connection` 改为 `from services.storage.ai_inference_db import get_ai_inference_db`；所有 `with get_connection() as conn:` 改为 `with get_ai_inference_db().connect() as conn:`；`__init__` 改调 `get_ai_inference_db().ensure_schema()` | grep 命中 |
| `services/storage/theme_store.py` | 同上 | grep 命中 |
| `services/scoring/matcher.py` | `_lookup_dim_stock`：复查实现，应改为 `with get_market_db().connect(readonly=True) as conn:` 直查 dim_stock | 与 dim_stock 实际归属对齐 |
| `core/theme_extractor.py` | `parse_report_meta` 反查 ai_reports 的连接换 `get_ai_inference_db()` | grep 命中 |
| **`gui/pages/theme_prediction_page.py`** ⭐ | **L370 / L711** 两处直接用 `get_connection()` 查 theme_* 表，改为 `with get_ai_inference_db().connect() as conn:` | review 补 |
| **`gui/pages/prompt_eval_page.py`** ⭐ | **L231 / L260** 同理，模板评估页直接查 theme_predictions，改连接源 | review 补 |
| **`main.py`** ⭐ | **L18 / L20** 启动只调 `init_database()`（=news.db）；需追加：`get_market_db().ensure_schema()` 和 `get_ai_inference_db().ensure_schema()` | review 补 |

**确认无需改的地方**（review 时已 grep 全代码库，均确认 OK）：
- `core/ai_news_analyzer.py` L464 只用 `get_project_root`（路径工具，非数据库），不动
- `gui/workers/theme_extract_worker.py` 通过 theme_store 间接用，不动
- `services/crawl_sync_service.py` / `services/clean_sync_service.py` 操作 raw_news（仍在 news.db），不动
- `services/storage/raw_store.py` 操作 raw_news，不动
- `gui/pages/news_cleaning_page.py` / `gui/workers/news_cleaning_worker.py` 操作 raw_news，不动
- `gui/pages/data_page.py` / `gui/pages/ai_analysis_page.py` 无 get_connection 调用（间接走 store）
- `core/news_exporter.py` / `core/data_loader.py` 无 get_connection 调用
- `services/analysis_service.py` 通过 `record_report` 间接，跟 ai_reports_store 自动改
- `agent/__init__.py` 无数据库交互

### Step -1.6 · 跨库 JOIN 工具上提

**文件**：`services/storage/cross_db.py`（新建）

**接口**：
```python
@contextmanager
def attached_dbs(primary_db, attach_specs: List[Tuple[Any, str]]) -> Iterator[sqlite3.Connection]:
    """统一的跨库 ATTACH 上下文，杜绝 DETACH 泄漏。

    Args:
        primary_db: 主库实例（如 get_ai_inference_db()）
        attach_specs: [(market_db_instance, 'm'), (news_db_instance, 'n')]

    Example:
        with attached_dbs(
            get_ai_inference_db(),
            [(get_market_db(), 'm'), (get_news_db(), 'n')]
        ) as conn:
            conn.execute("SELECT ... FROM theme_predictions JOIN m.dim_sector ...")
        # 退出时自动 DETACH 全部
    """
```

**实现要点**：
- 主库走 `connect()` 拿 conn
- 对每个 attach_spec 调 `attach_to(conn, alias)`
- yield conn
- finally 块逆序 `DETACH DATABASE {alias}`，包 try/except 防止单个 DETACH 失败导致后续泄漏

### Step -1.7 · news.db 包一个 NewsDB 类（可选但推荐）

**文件**：`services/storage/news_db.py`（新建）

**理由**：
- 当前 news.db 直接用 `sqlite3.connect()`，没有 attach_to / backup 等能力
- 跨库 JOIN 时 ATTACH 需要现成路径，包个类更优雅

**接口**：抄 MarketDB 范式但 schema_migrations 跳过（news.db 还是用原 `_SCHEMA` 一次性建）

**判定**：
- 如果 Phase -1 工期紧张，可以**跳过这一步**，跨库 JOIN 时直接传 `news.db` 路径字符串
- 如果一步到位推荐做

### Step -1.8 · 一次性测试 CLI

**文件**：`tools/test_three_db.py`（新建）

**用例**：
1. `AIInferenceDB().ensure_schema()` 成功，schema_migrations 记录版本 1
2. 6 张表全建出 + 17 个索引 + 6 个外键全启用
3. `_drop_legacy_ai_tables` 跑后 news.db 只剩 2 张表
4. theme_store.save_themes 写入 ai_inference.db（不是 news.db）
5. ai_reports_store.record 写入 ai_inference.db
6. 跨库 JOIN 烟测：ATTACH market.db + news.db，三库联查 dim_sector + raw_news 不报错
7. **CASCADE 测试**：删 theme_predictions 一行 → theme_stocks / theme_news / theme_prediction_scores / theme_stock_scores 全部级联清
8. matcher.normalize_stock_code 走 dim_stock 查询正常（不再误读 news.db）
9. backup 文件生成 + 7 份保留策略
10. `attached_dbs` 工具：连续 100 次进出无 attached db 泄漏

**验收**：10 用例全绿。

### Step -1.9 · v4 回归测试（**确保不破坏 v4 阶段功能**）

跑一遍 v4 阶段的 5 个 CLI，全部应通过：
```bash
python tools/test_matcher.py          # matcher 改了 dim_stock 来源
python tools/test_theme_normalize.py  # theme_extractor 改了反查连接
python tools/test_theme_schema_v4.py  # 这个会失败（v4 schema 已迁走）→ 改为 test_three_db.py 覆盖
python tools/test_theme_store_v4.py   # theme_store 改了连接
python tools/test_scoring_scheduled_stub.py  # 不受影响
```

**预期处理**：
- `test_theme_schema_v4.py` 主动重命名为 `test_three_db.py`（避免名字误导）
- 其他 4 个必须全绿

### Phase -1 验收

- [ ] `data/ai_inference.db` 文件创建 + 6 张表建出
- [ ] `data/news.db` 只剩 2 张表（raw_news + sync_meta）
- [ ] `data/market.db` 多了 fact_stock_daily / fact_sector_daily 两张空表
- [ ] `tools/test_three_db.py` 10 用例全绿
- [ ] v4 回归 4 个 CLI 全绿
- [ ] GUI 启动 main.py 不报错（题材预测页能开但表为空，符合预期）

---

## Phase 0 · 基础 schema 验收（**0.1 天**）⭐ v3 大幅瘦身

> **v3 重大变化**：v2 计划在 Phase 0 才建表，v3 已经把建表工作前置到 Phase -1。
> 本 Phase 仅做"schema 健康检查"——验证 Phase -1 落地的三库 schema 完整可用，作为后续 Phase 的入口卫兵。

### Step 0.1 · 健康检查脚本

**文件**：`tools/check_three_db_health.py`（新建，可重复跑）

**输出范例**：
```
[OK] news.db: 2 tables (raw_news, sync_meta), 19 news records
[OK] market.db: 12 tables (含 dim_*, fact_*, schema_migrations), 5 migrations applied
     - fact_stock_daily: 0 rows (待 Phase 1 填充)
     - fact_sector_daily: 0 rows (待 Phase 1 填充)
[OK] ai_inference.db: 6 tables, 1 migration applied
     - ai_reports: 0 rows
     - theme_predictions: 0 rows
     - theme_prediction_scores: 0 rows
[OK] 跨库 ATTACH 烟测通过
[OK] FK 启用：foreign_keys=ON
[OK] WAL 模式：journal_mode=WAL
```

### Step 0.2 · 入口卫兵集成

**改动**：在 `main.py` / `tools/*.py` 的启动早期调用 `check_three_db_health()`，缺数据库就友好提示主人先跑 Phase -1（而不是抛 sqlite 异常）。

**Phase 0 验收**：
- [ ] `python tools/check_three_db_health.py` 一行命令全绿
- [ ] 故意删除某张表后能给出"建议跑 ensure_schema()"的友好提示

---

## Phase 1 · 行情同步（A 主线，**1 天**）

### Step 1.1 · 工具函数 next_trade_date

**文件**：`services/market/trade_date.py`（在已有文件追加）

**接口**：
```python
def next_trade_date(date_str: str, n: int = 1, *,
                    db: Optional[MarketDB] = None,
                    client: Optional[TushareClient] = None) -> str:
    """从 date_str 开始向后数 n 个交易日。

    Args:
        date_str: YYYYMMDD 起始日（自身可以是非交易日）
        n: 偏移天数（1=下一个交易日；0=date_str 自身若开市则返回，否则向前找）
    Returns:
        目标交易日 YYYYMMDD
    Raises:
        TradeDateError: 30 天向后仍找不到第 n 个交易日
    """

def trade_dates_between(start: str, end: str, *,
                        db: Optional[MarketDB] = None,
                        client: Optional[TushareClient] = None) -> List[str]:
    """返回 [start, end] 闭区间内所有交易日（升序）。批量打分用。"""
```

**实现要点**：
- 优先查 `dim_trade_calendar`（已有缓存机制）
- 缺数据时调 `_refresh_calendar_window` 扩窗口
- assert 节假日跳过：测试用 2026-05-01 → +1 应该返回 2026-05-06（劳动节假期）

### Step 1.2 · 个股日行情同步

**文件**：`services/market/stock_daily_sync.py`（新建）

**接口**：
```python
@dataclass
class DailySyncResult:
    ok: bool
    trade_date: str
    rows_written: int
    api_calls: int
    elapsed_ms: int
    error: Optional[str] = None

def sync_stock_daily(trade_date: str, *,
                    db: Optional[MarketDB] = None,
                    client: Optional[TushareClient] = None,
                    force: bool = False) -> DailySyncResult:
    """同步指定交易日全 A 股 daily 行情 → fact_stock_daily。

    - 默认幂等：trade_date 已有数据则跳过（除非 force=True）
    - 接口：Tushare `daily`，trade_date=YYYYMMDD，一次拉全市场
    - 大小：~5400 行，单次调用 ~3s
    """

def sync_stock_daily_range(start: str, end: str, ...) -> List[DailySyncResult]:
    """批量同步区间。内部循环 trade_dates_between + sync_stock_daily。"""
```

### Step 1.3 · 板块日行情同步

**文件**：`services/market/sector_daily_sync.py`（新建）

**接口**：与 Step 1.2 镜像：`sync_sector_daily(trade_date, ...)`。

**实现要点**：
- 用 Tushare 接口（如 `kpl_concept_cons` + 自算板块涨幅，或直接 `kpl_concept`）
- 板块涨幅 = 成份股涨跌幅按市值/等权平均（一期等权即可）
- 写 `fact_sector_daily`

### Step 1.4 · CLI 工具

**文件**：`tools/sync_daily_market.py`（新建）

**参数**：
```bash
python tools/sync_daily_market.py --days 30           # 回填最近 30 个交易日
python tools/sync_daily_market.py --date 20260527     # 指定单日
python tools/sync_daily_market.py --start 20260501 --end 20260527
python tools/sync_daily_market.py --resume            # 断点续传（跳过已同步日期）
python tools/sync_daily_market.py --only stock        # 只同步个股
python tools/sync_daily_market.py --only sector       # 只同步板块
```

**输出**：
- 实时进度（每天一行：`[OK] 20260527 stock=5421 sector=489 elapsed=3.2s`）
- 总耗时 + API 调用次数（监控 Tushare 配额）
- 失败日落 `data/sync_failures.log`

### Step 1.5 · 测试 CLI

**文件**：`tools/test_daily_sync.py`（新建）

**用例**：
1. `next_trade_date` 节假日跳过（劳动节、国庆、春节）
2. `trade_dates_between` 区间正确性
3. `sync_stock_daily` 单日同步行数 > 5000
4. `sync_stock_daily` 幂等（重复跑不增行数）
5. `sync_stock_daily` `force=True` 重写已有数据
6. `sync_sector_daily` 单日行数 > 400
7. 跨库 JOIN 烟测：用 `attached_dbs(get_ai_inference_db(), [(get_market_db(), 'm')])` + `SELECT * FROM m.fact_stock_daily LIMIT 1` 不报错

**验收**：7 用例全绿；30 天回填总耗时 < 3 分钟。

### Step 1.6 · 接入 scheduled_runner

**文件**：`services/scheduled_runner.py`（改）

**改动**：把 `stock_daily_sync` / `sector_daily_sync` 两个分支的 `_stub_response` 替换为：
```python
if task_type == "stock_daily_sync":
    from services.market.stock_daily_sync import sync_stock_daily
    from services.market.trade_date import resolve_trade_date
    from services.market.tushare_client import TushareClient
    cli = TushareClient()
    td, _ = resolve_trade_date(cli, params.get("trade_date"))
    result = sync_stock_daily(td, client=cli, force=params.get("force", False))
    return {"ok": result.ok, "type": task_type,
            "result": {"trade_date": td, "rows_written": result.rows_written,
                       "api_calls": result.api_calls, "elapsed_ms": result.elapsed_ms},
            "error": result.error}
```

镜像处理 `sector_daily_sync`。

**默认任务种子**：在 `core/scheduler_service.py` 检查是否要预置 4 个新任务（默认 disabled，主人手动开）—— 视现有代码风格决定，不强制。

---

## Phase 2 · 打分核心（A 主线，**1.5 天**）

### Step 2.1 · script_scorer.py 算法核心

**文件**：`services/scoring/script_scorer.py`（新建）

**接口**：
```python
@dataclass
class ThemeDailyScore:
    """单题材单日打分结果（一行数据）。"""
    theme_id: int
    prompt_id: Optional[str]
    prompt_version: Optional[str]
    report_date: str
    score_date: str
    days_offset: int
    sector_pct: Optional[float]
    stock_avg_pct: Optional[float]
    stock_weighted_pct: Optional[float]
    hit_count: int
    total_count: int
    hit_rate: float
    benchmark_pct: Optional[float]      # 大盘涨幅，默认上证综指
    alpha: Optional[float]
    direction_correct: Optional[int]

def score_theme_on_date(
    theme_id: int,
    score_date: str,
    *,
    hit_threshold_pct: float = 3.0,     # 命中阈值，默认 3%
    benchmark_ts_code: str = "000001.SH",
    ai_db=None,                         # 测试可注入 AIInferenceDB
    market_db=None,                     # 测试可注入 MarketDB
) -> ThemeDailyScore:
    """计算单个题材在 score_date 这一天的得分。

    防穿越约束：score_date 必须 > theme.report_date，否则 raise。
    """

def score_themes_batch(
    report_date_range: Tuple[str, str],  # (start, end) 题材生成日范围
    *,
    days_offsets: Sequence[int] = (1, 2, 3, 4, 5),
    ...
) -> List[ThemeDailyScore]:
    """批量打分：对 report_date_range 内所有题材跑 D+1~D+5 全套。"""
```

**核心算法**（伪代码 v3：三库版）：
```python
def score_theme_on_date(theme_id, score_date, ...):
    # 1. 用 attached_dbs 工具一次性挂好三库
    with attached_dbs(
        get_ai_inference_db(),
        [(get_market_db(), 'm')]      # 只需 ai_inference + market 两库
    ) as conn:
        # 2. 取题材元数据（主库 ai_inference）
        theme = conn.execute("""
            SELECT * FROM theme_predictions WHERE id = ?
        """, (theme_id,)).fetchone()

        if score_date <= theme['report_date']:
            raise ValueError("穿越：score_date 必须晚于 report_date")

        # 3. 计算 days_offset
        days_offset = count(trade_dates_between(theme.report_date, score_date)) - 1

        # 4. 联查标的当日涨跌
        rows = conn.execute("""
            SELECT ts.id AS stock_id, ts.normalized_code, fsd.pct_chg
            FROM theme_stocks ts
            LEFT JOIN m.fact_stock_daily fsd
                ON fsd.ts_code = ts.normalized_code
                AND fsd.trade_date = ?
            WHERE ts.theme_id = ? AND ts.normalized_code IS NOT NULL
        """, (score_date, theme_id)).fetchall()

        # 5. 算 6 个指标
        pcts = [r['pct_chg'] for r in rows if r['pct_chg'] is not None]
        stock_avg_pct = mean(pcts) if pcts else None
        stock_weighted_pct = stock_avg_pct  # 单题材内等权
        hit_count = sum(1 for p in pcts if p >= hit_threshold_pct)

        # 6. 板块涨幅
        sector_pct = conn.execute("""
            SELECT pct_chg FROM m.fact_sector_daily
            WHERE sector_code = ? AND trade_date = ?
        """, (theme['sector_ts_code'], score_date)).fetchone()

        # 7. 大盘 benchmark（一期固定用上证综指）
        benchmark_pct = conn.execute("""
            SELECT pct_chg FROM m.fact_stock_daily
            WHERE ts_code = ? AND trade_date = ?
        """, (benchmark_ts_code, score_date)).fetchone()
        alpha = stock_weighted_pct - benchmark_pct if (...) else None

        # 8. 方向正确判定
        direction_correct = 1 if (theme.strength_score > 0 and sector_pct > 0) or \
                                  (theme.strength_score < 0 and sector_pct < 0) else 0

        # 9. 同时写两张表（同库 ai_inference，幂等 ON CONFLICT）
        conn.execute("""
            INSERT INTO theme_prediction_scores (...) VALUES (...)
            ON CONFLICT (theme_id, score_date) DO UPDATE SET ...
        """, (...))
        for stock_row in rows:
            conn.execute("INSERT INTO theme_stock_scores ...", (...))

        return ThemeDailyScore(...)
    # attached_dbs 退出时自动 DETACH 'm'
```

**易踩坑点（写代码时一定要意识到）**：
- v3 数据已清空，**所有题材都是新抽的 v5 schema**（带 normalized_code / sector_ts_code），不再需要历史兼容
- `report_date` 字段格式：v5 统一 YYYYMMDD（在 theme_extractor 入口已规范化，本算法直接用）
- **跨库 JOIN 必走 `attached_dbs`** 工具，禁止裸 ATTACH
- benchmark_ts_code='000001.SH' 必须在 fact_stock_daily 里能查到（Phase 1 同步时确认大盘指数也入库）

### Step 2.2 · scoring_service.py 服务入口

**文件**：`services/scoring/scoring_service.py`（新建）

**接口**：
```python
@dataclass
class BatchScoringResult:
    ok: bool
    themes_scored: int
    days_covered: int
    elapsed_ms: int
    errors: List[str]

def run_daily_scoring(score_date: Optional[str] = None, *,
                     days_back: int = 5) -> BatchScoringResult:
    """每日打分入口（定时任务调）。

    扫描 [score_date - days_back, score_date - 1] 区间内所有 report_date 的题材，
    对应到 D+1~D+5 给当天打分。
    """

def rescore_range(report_date_start: str, report_date_end: str) -> BatchScoringResult:
    """重打分（不重跑 AI 评分员，只重算脚本分）。

    模板评估页的「重打分」按钮调这里。
    """

def get_template_eval(
    days: int = 30,
    *,
    ignore_version: bool = True,
    group_by: str = "prompt_id",       # 或 ('prompt_id', 'prompt_version')
) -> List[Dict]:
    """聚合查询：模板评估页主表数据。

    返回每行：{prompt_id, prompt_version, sample_count, d1_avg, d3_avg, d5_avg,
              alpha_avg, hit_rate_avg, direction_correct_rate, ...}
    """

def get_theme_score_detail(theme_id: int) -> List[Dict]:
    """单题材打分明细（题材预测页第 4 个 Tab 调）。
    返回 D+1~D+5 逐日数据。"""
```

### Step 2.3 · CLI 工具

**文件**：`tools/score_themes.py`（新建）

**参数**：
```bash
python tools/score_themes.py --today                    # 给昨天到 5 天前的题材打今日分
python tools/score_themes.py --date 20260527            # 指定打分日
python tools/score_themes.py --range 20260501 20260527  # 批量重算区间
python tools/score_themes.py --theme-id 123             # 单题材调试
python tools/score_themes.py --dry-run                  # 只算不写库
```

### Step 2.4 · 测试 CLI

**文件**：`tools/test_script_scorer.py`（新建）

**用例**：
1. **穿越保护**：score_date <= report_date 必须 raise
2. 单题材 D+1 打分：手动插测试数据 + fact_stock_daily 数据 → 算法结果正确
3. `direction_correct`：strength_score=80 板块跌 → 0；strength_score=-50 板块跌 → 1
4. `hit_rate` 阈值：3 只标的涨幅 [5%, 1%, -2%]，阈值 3% → hit_count=1
5. 跨节假日：5/1 生成的题材 D+1 = 5/6
6. 缺数据防御：theme_stocks 为空 → 返回 stock_avg_pct=None 但不 crash
7. 缺数据防御：sector_ts_code 为 NULL → sector_pct=None
8. 幂等：同 (theme_id, score_date) 重跑 → UPDATE 而非新增
9. ATTACH/DETACH：连续 100 次调用不留 attached db
10. CASCADE：删 theme_predictions 一行 → theme_prediction_scores / theme_stock_scores 对应行自动清

**验收**：10 用例全绿；预期 Phase 6 堆样本后 ~42 题材 × 5 天 = ~210 行批量打分耗时 < 10s。

### Step 2.5 · 接入 scheduled_runner

**文件**：`services/scheduled_runner.py`（改）

**改动**：替换 `theme_score_daily` 桩为 `run_daily_scoring(...)` 调用。

---

## Phase 3 · ❌ 取消（v3 主人决策）

> v2 的"批量补抽 200 份历史 md"已取消。
> **理由**：实测仅 19 份 md（差一个数量级），且主人决策"过去的报告先忽略"。
> **替代方案**：B 期 Phase 6 通过虚拟回测堆 42 份样本（3 模板 × 14 天，v3.1 主人指定模板范围）。
>
> **如果主人未来想反悔补抽**：可单独跑一次性 `tools/batch_extract_themes.py`（不在本期施工范围）。

---

## Phase 4 · A 主线 GUI 接通（**0.5 天**）

### Step 4.1 · prompt_eval_page 桩数据替换

**文件**：`gui/pages/prompt_eval_page.py`（改）

**改动**：
- `_load_eval_data()` 方法：原 stub 改为 `scoring_service.get_template_eval(days, ignore_version)`
- 数据为空时显示"暂无评估数据，请先跑 `python tools/score_themes.py --range ...`"
- 「重打分」按钮：弹确认框→ 调 `scoring_service.rescore_range(...)`
- 「导出 CSV」按钮：实做导出

### Step 4.2 · 折线图实现

**文件**：`gui/pages/prompt_eval_page.py`（同一文件）

**实现要点**：
- 表格行多选（最多 3 个模板）→ 触发 `_update_chart()` 重绘
- 用 PyQtChart（已有依赖，零成本）
- 横轴 = 报告日期，纵轴 = D+5 平均分
- 数据源：`SELECT prompt_id, report_date, AVG(...) FROM theme_prediction_scores WHERE ... GROUP BY ...`

### Step 4.3 · 题材预测页第 4 个 Tab

**文件**：`gui/pages/theme_prediction_page.py`（改）

**改动**：
- 找到第 4 个 Tab「打分明细」的占位代码，替换为：
  - 上方表格：D+1~D+5 逐日数据（调 `scoring_service.get_theme_score_detail(theme_id)`）
  - 下方 PyQtChart 折线图：板块涨幅 vs 标的平均涨幅 vs 大盘涨幅
- 选中题材切换时刷新数据

### Step 4.4 · GUI 烟测

**手动跑**：
1. 启动 main_window，点开模板评估页 → 看到真实数据（非桩）
2. 点折线图选 2 个模板 → 图刷新
3. 点导出 CSV → 文件生成
4. 切到题材预测页 → 选一个题材 → 第 4 个 Tab 显示真实打分

---

## Phase 5 · 快照重建（B 支线，**1 天**，可与 Phase 1-3 并行启动）

### Step 5.1 · snapshot.py

**文件**：`services/scoring/snapshot.py`（新建）

**接口**：
```python
@dataclass
class HistoricalSnapshot:
    """某一历史时间点能看到的全部信息。"""
    cutoff_ts: int                      # 截止 UNIX 时间戳
    trade_date: str                     # 锚定的交易日
    news: List[Dict]                    # 截止前的新闻
    market_summary_date: Optional[str]  # 可用的最新盘后数据日期（必须 < trade_date）
    market_summary_md: Optional[str]    # 渲染好的盘后总结

def build_snapshot(
    trade_date: str,                    # YYYYMMDD
    cutoff_hour: int = 16,              # 当天几点截止（默认 16:00 收盘后）
    *,
    news_lookback_hours: int = 26,      # 拉多少小时新闻
) -> HistoricalSnapshot:
    """重建 trade_date {cutoff_hour}:00 时能看到的快照。

    防穿越铁律：
    - 所有 news.published_ts < cutoff_ts
    - market_summary 必须用 trade_date 之前的（不能是 trade_date 当天）
    """
```

**核心实现**：
```python
def build_snapshot(trade_date, cutoff_hour=16, *, news_lookback_hours=26):
    cutoff_dt = datetime.strptime(trade_date, "%Y%m%d").replace(hour=cutoff_hour)
    cutoff_ts = int(cutoff_dt.timestamp())

    # 1. 拉新闻（截止前 N 小时）
    start_ts = cutoff_ts - news_lookback_hours * 3600
    news = SELECT * FROM raw_news
           WHERE published_ts >= ? AND published_ts < ?
             AND clean_status = 'curated'
           ORDER BY published_ts ASC
    # 防穿越 assert
    for n in news:
        assert n['published_ts'] < cutoff_ts, "穿越：新闻晚于 cutoff"

    # 2. 找可用的盘后数据：cutoff_hour < 16 → 用前一交易日；>= 16 → 用 trade_date 当天
    if cutoff_hour < 16:
        market_date = prev_trade_date(trade_date)
    else:
        market_date = trade_date

    # 3. 渲染盘后 summary
    from services.market.service import MarketSummaryService
    ms = MarketSummaryService().build(trade_date=market_date, mode='hybrid')

    return HistoricalSnapshot(...)
```

### Step 5.2 · 测试 CLI

**文件**：`tools/test_snapshot.py`（新建）

**用例**：
1. 正常重建：build_snapshot('20260520', 16) 返回非空新闻
2. **穿越测试**：mock 一条 published_ts > cutoff_ts 的新闻 → assert 失败必须触发
3. cutoff_hour=10：market_date = 前一交易日
4. cutoff_hour=16：market_date = 当天
5. 节假日：build_snapshot('20260501')（劳动节）→ news 为空但不 crash
6. 重建结果稳定：同参数两次调用结果必须完全一样（hash 对比）

**验收**：6 用例全绿；穿越测试必须能捕获。

---

## Phase 6 · 虚拟回测 CLI（B 支线，**1~1.5 天**）⭐ v3 升级为关键路径

> **v3 升级理由**：原 v2 仅做"主人写新模板立刻验证"用，v3 升级为"批量堆 42 份样本"（v3.1 主人指定 3 模板），
> 解决真实 md 仅 19 份导致模板评估页无数据可看的问题。

### Step 6.1 · record_report 接口对齐

**文件**：`services/storage/ai_reports_store.py`（改）

**改动**：
- `record_report()` 函数签名加 `is_backtest: bool = False` 参数
- `AIReportRecord` 数据类加 `is_backtest: int = 0` 字段
- INSERT SQL 加 `is_backtest` 列
- `ThemeStore.save_themes` 接收 meta 时透传 `is_backtest` 到 theme_predictions

### Step 6.2 · backtest_prompt.py 虚拟 md 生成

**文件**：`tools/backtest_prompt.py`（新建）

**参数**：
```bash
python tools/backtest_prompt.py \
    --template custom_1770292858 \
    --date 20260520 \
    --provider deepseek

python tools/backtest_prompt.py \
    --template custom_1770292858 \
    --date-range 20260513..20260526 \
    --provider deepseek \
    --workers 2

python tools/backtest_prompt.py \
    --template custom_1770292858,custom_6,custom_7 \
    --date 20260520
```

**实现要点**：
```python
def backtest_one(template_id, trade_date, provider='deepseek', cutoff_hour=16):
    # 1. 重建快照
    snap = build_snapshot(trade_date, cutoff_hour)

    # 2. 走 AnalysisService（但参数指向快照）
    from services.analysis_service import AnalysisService
    svc = AnalysisService()

    # 关键：把快照的新闻 dump 成临时 JSON，然后调 analyze 时 start/end 截断
    # 或者更彻底：扩展 AnalysisService 加 news_list 直接传入参数（避免重复查库）
    result = svc.analyze(
        source='curated',
        start=datetime.fromtimestamp(snap.cutoff_ts - 26*3600),
        end=datetime.fromtimestamp(snap.cutoff_ts),
        template_id=template_id,
        provider=provider,
        market_summary=snap.market_summary_md,  # 用快照里的盘后，不再 auto
        auto_market=False,
    )

    # 3. report_path 重命名加 _backtest_{trade_date} 后缀，避免与真实报告冲突
    new_path = result.report_path.replace('.md', f'_backtest_{trade_date}.md')
    os.rename(result.report_path, new_path)

    # 4. 改 ai_reports 的 is_backtest=1
    UPDATE ai_reports SET is_backtest=1, file_path=? WHERE id=?

    # 5. 触发题材抽取（自动走 v4 流程，is_backtest 透传到 theme_predictions）
```

**注意**：`is_backtest` 字段已在 Phase -1 一次性落入 ai_inference.db 的 ai_reports / theme_predictions 两张表（v3 不再像 v2 那样分阶段加列）。

### Step 6.3 · 测试 CLI

**文件**：`tools/test_backtest_e2e.py`（新建）

**用例**：
1. 单日回测 → md 文件生成 + ai_reports 入库带 is_backtest=1
2. 题材抽取自动触发 → theme_predictions 入库带 is_backtest=1
3. 日期范围回测 → 多日成功
4. 穿越保护：传未来日期 → assert 失败
5. **关键**：回测 md 进入打分链路 → score_themes 能算出虚拟题材的分数
6. **并发安全**：4 workers 跑 8 个回测任务，全成功不串数据

**验收**：6 用例全绿；单日单模板回测耗时 < 60s。

### Step 6.4 · GUI 加「真实/回测/全部」筛选

**文件**：`gui/pages/prompt_eval_page.py`（改）

**改动**：
- 控制栏加单选按钮组：`[全部] [仅真实] [仅回测]`
- 默认 `[全部]`
- 传给 `scoring_service.get_template_eval(..., backtest_filter=...)`
- 表格行尾加列「样本构成」：`真实 5 / 回测 14`（让主人一眼看出哪些模板靠真实数据）

### Step 6.5 · 批量堆样本（**v3 新增，关键步骤；v3.1 模板范围收窄到 3 个**）

**文件**：`tools/backfill_backtest_samples.py`（新建）

**目的**：一次性给主人指定的 3 个核心模板跑过去 14 个交易日的虚拟回测，堆出 42 份样本作为模板评估的初始数据。

**用法**：
```bash
# v3.1：3 个主人指定模板 × 14 天 = 42 次 LLM
python tools/backfill_backtest_samples.py \
    --templates custom_1770292858,custom_6,custom_7 \
    --days 14 \
    --provider deepseek \
    --workers 4

# 或用别名（CLI 内置 BUILTIN_TEMPLATES=['custom_1770292858','custom_6','custom_7']）
python tools/backfill_backtest_samples.py --templates builtin --days 14
```

**主人指定模板对照**：

| prompt_id | 中文名 | 文件 |
|-----------|--------|------|
| `custom_1770292858` | 三位一体综合分析_2月5日 | `prompts/analysis/custom_1770292858.md` |
| `custom_6` | 新闻分析 | `prompts/analysis/custom_6.md` |
| `custom_7` | 超级综合模板 | `prompts/analysis/custom_7.md` |

**实现要点**：
```python
# v3.1：主人指定模板范围
BUILTIN_TEMPLATES = ['custom_1770292858', 'custom_6', 'custom_7']

def backfill_all(template_ids=None, days=14):
    templates = template_ids or BUILTIN_TEMPLATES

    # 取过去 14 个交易日（含今日）
    dates = trade_dates_between(
        next_trade_date(today_str(), -days),
        today_str()
    )

    # 笛卡尔积 = 3 × 14 = 42 个回测任务
    tasks = [(t, d) for t in templates for d in dates]

    # 并发跑（每个任务 30~60s，4 workers 总耗时 ~6 分钟）
    with ThreadPoolExecutor(4) as pool:
        for tmpl, date in tasks:
            pool.submit(backtest_one, tmpl, date, provider='deepseek')

    # 跑完后立刻触发打分（这一步会重用 Phase 2 的 scoring_service）
    scoring_service.rescore_range(dates[0], dates[-1])

    # 输出统计：模板/样本数表格
    print_summary()
```

**输出范例（v3.1）**：
```
[OK] custom_1770292858 @ 20260513 (12.3s, 4 themes extracted)
[OK] custom_1770292858 @ 20260514 (15.2s, 6 themes)
[OK] custom_6 @ 20260513 (10.1s, 3 themes)
... 共 42 个回测任务
[SUMMARY] 成功 40 / 失败 2
  - 失败列表: custom_7@20260520 (LLM timeout)...
[NEXT] 触发批量打分 ... 完成 (~10s)
[DONE] 总耗时 6m18s, LLM 调用 40 次, 约 $0.40
```

**验收**：
- [ ] 42 任务成功率 > 95%
- [ ] 模板评估页 3 个指定模板都有 ≥ 5 个样本
- [ ] 失败任务进日志可重试

---

## Phase 7 · AI 评分员（**0.5-1 天**）

### Step 7.1 · 复审 prompt

**文件**：`prompts/theme_review/theme_d5_review.md`（新建）

**内容框架**：
```markdown
---
id: theme_d5_review
version: "1.0"
category: theme_review
provider_hint: deepseek
---

# 题材 D+5 复审专家

你是资深 A 股短线分析师，专门复盘 AI 题材预测的质量。

## 输入

【原始预测】（5 天前）
- 题材名：{theme_name}
- 强度分：{strength_score}
- 理由：{reason}
- 标的：{stocks}

【5 天实际表现】
| 日期 | 板块涨幅 | 标的平均涨幅 | 命中率 |
|------|---------|-------------|--------|
| D+1 | {d1_sector} | {d1_stock_avg} | {d1_hit_rate} |
| ... | ... | ... | ... |
| D+5 | {d5_sector} | {d5_stock_avg} | {d5_hit_rate} |

【同期大盘】上证综指 5 天 {benchmark_5d_pct}

## 任务

请从三个标签中选一个：
- **right**: 理由扎实 + 数据支撑 + 5 天验证（说明 AI 真的看懂了）
- **lucky**: 方向蒙对但理由站不住 / 方向错但巧合反弹
- **wrong**: 理由有事实错误 / 忽视了关键风险点

## 输出 JSON

{"label": "right|lucky|wrong", "comment": "50 字以内简评"}
```

### Step 7.2 · AI 评分员服务

**文件**：`services/scoring/ai_reviewer.py`（新建）

**接口**：
```python
@dataclass
class AIReviewResult:
    ok: bool
    label: Optional[str]                # right/lucky/wrong
    comment: Optional[str]
    error: Optional[str] = None

def review_theme(theme_id: int, *, provider: str = 'deepseek') -> AIReviewResult:
    """对指定题材调 AI 评分员。

    前置：theme_prediction_scores 必须有 D+1~D+5 数据，否则 raise。
    幂等：同一 theme_id 已有 ai_review_label → 跳过（除非 force=True）。
    """

def review_due_themes(score_date: str, *, provider: str = 'deepseek') -> Dict:
    """扫今日满 D+5 的所有题材，去重批量评。

    去重逻辑：同一 (theme_name, report_date) 多份报告 → 只调一次，结果共享
    （review 阶段补丁 P9）
    """
```

### Step 7.3 · 测试 CLI

**文件**：`tools/test_ai_reviewer.py`（新建）

**用例**：
1. 单题材正常评 → label/comment 非空
2. 缺 D+1~D+5 数据 → raise
3. 幂等：第二次调跳过
4. 去重：模拟同 (theme_name, report_date) 多份 → 只调一次 LLM（mock 计数）
5. JSON 解析容错：mock LLM 返回非标准 JSON → 仍能提取 label

**验收**：5 用例全绿；单次评分耗时 < 10s。

### Step 7.4 · 接入 scheduled_runner

**文件**：`services/scheduled_runner.py`（改）

**改动**：替换 `theme_ai_review` 桩为 `review_due_themes(score_date)` 调用。

### Step 7.5 · GUI 显示 AI 评语

**文件**：`gui/pages/theme_prediction_page.py`（改）

**改动**：
- 第 4 个 Tab 表格 D+5 行加列「AI 评」（🟢/🟡/🔴 + 悬停看 comment 全文）
- 题材列表加列「AI评」（同上颜色，紧凑显示）

---

## Phase 8 · 联调 + 回填 + 文档（**0.5 天**）

### Step 8.1 · 全链路冷启动跑通

按顺序跑：
```bash
# 1. 三库 schema 升级 + 健康检查
python -c "from services.market.market_db import get_market_db; get_market_db().ensure_schema()"
python -c "from services.storage.ai_inference_db import get_ai_inference_db; get_ai_inference_db().ensure_schema()"
python tools/check_three_db_health.py     # 一行验证三库就绪

# 2. 回填 30 天行情（5400 个股 × 30 天 + 480 板块 × 30 天）
python tools/sync_daily_market.py --days 30

# 3. ⚡ 关键：批量虚拟回测堆样本（v3.1：3 模板 × 14 天 = 42 份）
python tools/backfill_backtest_samples.py --templates builtin --days 14 \
    --provider deepseek --workers 4

# 4. 批量打分（covers 14 天 × 5 天追踪）
python tools/score_themes.py --range $(date -d "-19 days" +%Y%m%d) $(date +%Y%m%d)

# 5. AI 复审（仅当前 D+5 已满的题材）
python -c "from services.scoring.ai_reviewer import review_due_themes; review_due_themes()"
```

**预期总耗时**（v3.1 因模板缩减大幅下降）：
- Step 1: < 30s
- Step 2: ~3 分钟
- Step 3: **~6 分钟**（v3.1 从 25 分钟降至 6 分钟）
- Step 4: ~10s
- Step 5: ~1 分钟（D+5 已满样本较少）
- **总计：~11 分钟**（v3 是 ~35 分钟）

### Step 8.2 · GUI 端到端验收

启动 `python main.py`，按 checklist：
- [ ] 模板评估页：表格显示 3 个指定模板真实数据
- [ ] 模板评估页：样本数列显示「真实 N / 回测 M」
- [ ] 模板评估页：折线图能选 2-3 个模板对比
- [ ] 模板评估页：「重打分」按钮工作
- [ ] 模板评估页：「导出 CSV」工作
- [ ] 模板评估页：「真实/回测/全部」筛选切换
- [ ] 题材预测页：模板下拉筛选生效
- [ ] 题材预测页：第 4 个 Tab 显示真实 D+1~D+5
- [ ] 题材预测页：D+5 行显示 AI 评语（如有）
- [ ] 定时任务页：4 个新任务类型可创建/启用/查看上次执行结果

### Step 8.3 · 单模板增量回测验证

主人有新模板时的典型用法：
```bash
python tools/backtest_prompt.py \
    --template my_new_idea \
    --date-range 20260520..20260526 \
    --provider deepseek
```

期望：7 份虚拟 md 生成 + 7 份题材入库带 backtest=1 + 模板评估页筛选「仅回测」能看到。

### Step 8.4 · 文档同步

- [ ] `.huiye/README.md` 任务索引更新（标记 9 个 phase 完成）
- [ ] `doc/design/05-26-2126-NewsTrading架构方案.md` §15 加三库 + 打分系统章节
- [ ] `doc/updates/05-DD-HHMM-模板回测功能上线.md` 写一份更新日志
- [ ] 本施工文档全部 Phase 状态改 🟢

---

## 3. 并行调度建议（v3.1：5~6 天甘特）

| Day | 上午（4h） | 下午（4h） |
|-----|----------|----------|
| **Day 1** | **Phase -1 起步**（AIInferenceDB 类 + 001_initial.sql + market 005） | Phase -1 收尾（storage 迁移 + 跨库工具 + test_three_db.py + v4 回归） |
| **Day 2** | Phase 0 健康检查 + Phase 1 起步（next_trade_date + 个股同步） | Phase 1 收尾（板块同步 + CLI + scheduler 接入）/ Phase 5 起步（snapshot 接口） |
| **Day 3** | Phase 2 起步（script_scorer 算法 + 跨库 SQL）/ Phase 5 收尾（防穿越测试） | Phase 2 继续（CLI + 测试）/ Phase 6 起步（backtest_prompt） |
| **Day 4** | Phase 2 收尾 + Phase 6 收尾（含批量堆样本骨架） | Phase 4（GUI 接通 A 主线）/ Phase 7 起步（AI 评分员 prompt + service） |
| **Day 5** | Phase 7 收尾 + scheduler 接入 / **Phase 6.5 实际跑 3×14 = 42 任务（~6 分钟）** | Phase 8 联调 + GUI 验收 |
| **Day 6** | buffer / 文档同步 / 主人验收 | - |

**关键里程碑节点**：
- Day 1 EOD：三库就绪，v4 回归全绿
- Day 2 EOD：行情同步可用 + 快照重建可用
- Day 3 EOD：脚本打分能跑 + 虚拟回测能跑单个
- Day 4 EOD：GUI 能看真分 + AI 评分员可用
- Day 5 EOD：42 份样本入库 + 端到端验收
- Day 6 EOD：完工 + 文档闭环

---

## 4. 风险登记 + 应对（v3.1）

| 风险 | 影响 | 应对 |
|------|------|------|
| **Phase -1 storage 层改连接遗漏某处** | 数据写错库 / 启动报错 | test_three_db.py 第 4/5 用例硬约束；v4 回归全跑一遍；Step -1.5 表已 review 补 GUI 两页面 + main.py |
| **跨库 ATTACH 泄漏** | 长期跑崩 | 强制走 `attached_dbs` 上下文工具；test_three_db.py 第 10 用例覆盖 100 次循环 |
| **matcher 改连接源（news.db → market.db）失败** | 标的标准化挂 | test_three_db.py 第 8 用例硬约束；改完跑 test_matcher.py 回归 |
| Tushare 积分不足 30 天回填 | A 链路停摆 | 启动前先 `python services/market/tushare_client.py` 验积分；不足则缩到 7 天 |
| **LLM 限流（批量虚拟回测 42 次）** | Phase 6.5 拖延 / 失败 | v3.1 收窄到 3 模板大幅缓解；CLI 内置 `--workers 4` 控速 + 失败重试日志 + 部分成功也算可用 |
| 板块涨幅算法分歧（等权 vs 市值加权） | Phase 1 决策点 | 一期固定等权，二期可改（不影响接口） |
| 19 份真实 md 全是同一段时间 | 模板评估页样本不均衡 | 靠虚拟回测的 14 天分散日期来平衡，覆盖 5/13~5/26 |
| **3 模板覆盖范围有限** | 主人其他 9 个模板无回测数据 | v3.1 主人决策接受；GUI 显示"未回测"标签提醒 |
| AI 评分员 prompt 调试不顺 | Phase 7 拖延 | 先用 mock LLM 跑通链路，prompt 优化推到 buffer |
| GUI 折线图 PyQtChart 在 Windows 兼容性 | Phase 4 阻塞 | 兜底：先用 QTableWidget 展示数据，图表延后 |
| 节假日跨越导致 D+N 计算错 | 打分错 | `next_trade_date` 单测必须覆盖劳动节/国庆/春节 |
| matcher 在新模板生成的题材上匹配失败率高 | 板块打分缺数据 | 接受 NULL 不 crash；GUI 给"待人工补"标记 |
| **批量回测产生大量 md 文件污染 data/AI_analysis/** | 目录混乱 | 文件名加 `_backtest_{date}.md` 后缀；GUI ai_analysis_page 区分显示 |

---

## 5. 验收 checklist（v3.1）

### 功能层
- [ ] 三库 schema 健康检查 `tools/check_three_db_health.py` 全绿
- [ ] 跑 `tools/sync_daily_market.py --days 30` 完成不报错
- [ ] **跑 `tools/backfill_backtest_samples.py --templates builtin --days 14` 成功率 > 95%**（v3.1 关键，3×14=42 份）
- [ ] 跑 `tools/score_themes.py --range ...` 打分入库
- [ ] 跑 `tools/backtest_prompt.py --date 20260520` 单模板虚拟回测成功
- [ ] 跑 `review_due_themes(...)` AI 评分员入库
- [ ] GUI 模板评估页 10 项验收全过
- [ ] GUI 题材预测页 D+5 评语显示

### 质量层
- [ ] **7 个 CLI 测试脚本全绿**（test_three_db / test_daily_sync / test_script_scorer / test_snapshot / test_backtest_e2e / test_ai_reviewer / + v4 回归 4 个，共 ~50+ 用例）
- [ ] 防穿越测试硬约束生效（穿越场景必须 raise）
- [ ] CASCADE 删除链路完整（删 theme → 4 张子表全清）
- [ ] **跨库 ATTACH 泄漏检测**（100 次循环无 attached db 残留）
- [ ] 全链路冷启动 **< 15 分钟**（v3.1 因模板缩减从 35 分钟下降）

### 文档层
- [ ] 本施工文档全部 Phase 状态 🟢
- [ ] `.huiye/README.md` 同步
- [ ] `doc/design/05-26-2126-NewsTrading架构方案.md` 加三库章节
- [ ] 通读版设计文档加「为什么改三库」章节
- [ ] 一份 `doc/updates/05-DD-HHMM-模板回测功能上线.md`

---

## 6. 实施 Phase 状态总表（v3.1）

| Phase | 子任务数 | 状态 | 完成日期 |
|-------|---------|------|---------|
| **Phase -1 · 三库重构** ⭐ | **9** | ⚪ | - |
| Phase 0 · schema 健康检查 | 2 | ⚪ | - |
| Phase 1 · 行情同步 | 6 | ⚪ | - |
| Phase 2 · 打分核心 | 5 | ⚪ | - |
| ~~Phase 3 · 批量补抽~~ ❌ | 0 | 🗑️ 取消 | v3 主人决策 |
| Phase 4 · A 主线 GUI 接通 | 4 | ⚪ | - |
| Phase 5 · 快照重建（B 支线） | 2 | ⚪ | - |
| **Phase 6 · 虚拟回测 + 3 模板堆样本** ⭐ | **5**（+ 6.5 堆样本，v3.1 缩窄到 3 模板） | ⚪ | - |
| Phase 7 · AI 评分员 | 5 | ⚪ | - |
| Phase 8 · 联调 + 回填 + 文档 | 4 | ⚪ | - |

> ⚪ = 未开工 / 🟡 = 进行中 / 🟢 = 完成 / 🔴 = 卡住

---

## 7. 命名约定（避免施工时随意命名）

| 类型 | 命名规范 | 示例 |
|------|---------|------|
| 服务模块 | `services/{domain}/{action}_*.py` | `services/market/stock_daily_sync.py` |
| 服务入口（带 service 字样） | `services/{domain}/{xxx}_service.py` | `services/scoring/scoring_service.py` |
| 工具脚本 | `tools/{verb}_{noun}.py` | `tools/sync_daily_market.py` / `tools/score_themes.py` |
| 测试脚本 | `tools/test_{module}.py` | `tools/test_script_scorer.py` |
| Migration | `services/market/migrations/{NNN}_{name}.sql` 或 `services/storage/ai_migrations/{NNN}_{name}.sql` | `005_fact_daily_quotes.sql` / `001_initial.sql` |
| Prompt | `prompts/{category}/{id}.md` | `prompts/theme_review/theme_d5_review.md` |
| GUI helper | `gui/utils/{name}_helper.py` | （沿用现有 `prompt_name_helper.py` 风格） |

---

## 8. cli-first 自检 checklist（每个 Phase 完成前必走）

- [ ] 本 Phase 的核心模块有对应 `tools/test_*.py` 测试 CLI
- [ ] 测试 CLI 覆盖 ≥ 5 个用例（含至少 1 个反向用例）
- [ ] 测试 CLI 不依赖 GUI、不依赖外部网络（必要时 mock）
- [ ] 测试 CLI 输出标记每条用例 PASS/FAIL，最后汇总
- [ ] 测试 CLI exit code 反映成功/失败（CI 友好）

---

## 9. 辉夜的施工心法（v3.1 增补）

1. **基础设施优先复用**：MarketDB / TushareClient / matcher / theme_extractor 都是现成的，**绝对不重写**
2. **schema 一次到位**：**Phase -1 把 8 张表 + is_backtest 字段一次性落**，后续 phase 不动 schema
3. **防穿越是底线**：每个测试 CLI 都要至少一个反向用例
4. **CLI 先于 GUI**：每个 service 模块先有 CLI 能跑通，再接 GUI；坏的话定位快
5. **失败容错而非 crash**：缺 normalized_code / sector_ts_code / 行情数据 → 返回 NULL 不 crash；上游展示时给"待补"提示
6. **幂等**：所有写入都要支持重跑（DELETE then INSERT 或 ON CONFLICT UPDATE）
7. **跨库 JOIN 必走 `attached_dbs` 上下文**：禁止裸 ATTACH；单测硬约束（100 次循环检漏）
8. **数据写库前先想"这数据归谁"**：原始新闻→news / 接口行情→market / AI 衍生→ai_inference，写错库未来一定踩坑
9. **死傲娇日志**：CLI 失败日志要写清"哪行 / 哪个题材 / 为什么"，方便主人吐槽
10. **修改文件用 StrReplace**：v3 修订时辉夜误用 PowerShell -replace+Set-Content 导致整个文件 UTF-8 编码被破坏，幸亏对话上下文还原 — **以后再也不用 shell 改源代码**

---

## 10. 待主人最终启动的确认（v3.1）

施工开始前，**主人最后再确认一次**：

- [ ] **Tushare 积分**：先 `python services/market/tushare_client.py` 跑一次确认 token 有效 + 积分够（>= 5000 / 分钟）
- [ ] **数据库可清**：主人确认 `theme_*` 三表已清 + ai_reports 历史 200 行也清（v3 主人决策"过去分析数据全清"）
- [ ] **DeepSeek API**：批量虚拟回测 **42** 次 ≈ **$0.42**，确认账户余额 ≥ $5（v3.1 大幅降低）
- [ ] **AI 评分员 provider**：默认 DeepSeek，主人有异议改 `prompts/theme_review/theme_d5_review.md` 的 `provider_hint`
- [ ] **disk space**：data/ 目录至少有 **200 MB** 富余（50 MB 行情 + 100 MB 虚拟回测 md + 50 MB ai_inference.db；v3.1 因模板缩减从 300 MB 下降）
- [ ] **现有 19 份 md 处理预期**：保留在磁盘但不抽题材（主人决策）
- [ ] **3 模板范围确认**（v3.1）：`custom_1770292858` / `custom_6` / `custom_7`，其他 9 个模板暂不堆样本

确认后辉夜按 Day 1 上午 Phase -1 三库重构开干~
