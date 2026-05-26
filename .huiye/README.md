# .huiye/ 文档总索引

> 辉夜的私有工作目录，按 [soul.md §记忆与文档归档](./soul.md) 收紧后**仅保留 4 类**：  
> ① 角色规约 `soul.md` ② 文档总索引 `README.md`（本文件）  
> ③ `_` 前缀的临时草稿 / 调试快照 / 字段审计 ④ 跨多份正式文档的"汇总索引"  
>  
> **其余一切正式文档已迁入 `doc/{子目录}/`**（迁移日志见 [doc/updates/05-26-2126-工作目录文档迁移.md](../doc/updates/05-26-2126-工作目录文档迁移.md)）  
> 最后更新: 2026-05-27 00:50（短线投机 prompt 双模板：scalper 实战派 + data_driven 数据派；DeepSeek V4 实跑对比 + 辉夜自评）

---

## 一、当前进行中 / 下一阶段

| 文档 | 角色 | 状态 |
|------|------|------|
| [盘后数据 GUI 优化效果方案](../doc/design/05-26-2116-盘后数据GUI优化效果方案.md) | **候选 #1（轻量）** — GUI 三层增强（A 必做 / B 建议 / C 选做） | 🟢 **Level A + Top20/Bottom10 + 4 字段补全已上线**，待主人验收 B/C |
| [盘后数据近 7 天补全 + 后端 4 字段派生](../doc/updates/05-26-2230-盘后数据近7天补全.md) | 本轮变更日志 — 后端实装 leaders/limit_up_count 派生 + pct_chg_5d 全板块覆盖 + Top 20 catalysts | 🟢 已上线，覆盖率：pct_5d 95%、catalysts 70%、leaders/lu_count 15%（受数据源限制） |
| [连板梯队 kpl 延迟全空修复 (v1+v2)](../doc/bugfix/05-26-2240-连板梯队kpl延迟全空修复.md) | v1 修 4 个 bug 让 ladder 不空；**v2 用历史 fact_limit_stock 递归推算连板数（T-1 仍涨停→2板）+ theme 兜底** | 🟢 已上线，46 只 → 首板 34/2连 8/3连 4，max_height=3 板 |
| [龙虎榜个股名称缺失修复](../doc/bugfix/05-26-2300-龙虎榜个股名称缺失修复.md) | `dim_stock` 表 0 行 + `_read_dragon_tiger` 写死 `name=None`；改 SQL 走 `raw_json.name` 兜底 | 🟢 已上线，93/93 全部带名称 |
| [dim_stock 维度表完整映射建立](../doc/features/05-26-2310-dim_stock维度表完整映射.md) | 新增 `fetch_dim_stock_full` + `tools/dim_stock_sync.py` CLI + `build()` 启动自动初始化；5522 只在市股一次入库 | 🟢 已上线，所有 ts_code 反查根治 |
| [export_market_summary CLI 上线](../doc/updates/05-26-2325-export-market-summary-CLI.md) | 纯导出工具，从 market_summaries 缓存读 + 用最新 renderer 重渲染，秒级、零 API；支持单日 / 批量 / MD/JSON/both / `--use-cached-md` 对比新旧 | 🟢 已上线，给辉夜边改 renderer 边验证用 |
| [AI 数据完整度 4 批次升级](../doc/updates/05-26-2355-AI数据完整度4批次升级.md) | summary 覆盖度 85% → 98%：Batch 1 纯渲染（Top 20/连板元信息/上榜原因/gaps bug）/ Batch 2 后端补字段（seal_rate_prev/main_net_yi/promotion_detail/sectors_bottom）/ Batch 3 板块异动加涨跌幅+龙虎榜其他席位 / Batch 4 advance/decline 全市场涨跌家数（新表 fact_market_breadth + Tushare daily 接口） | 🟢 7 天全量回填完成，所有新字段历史已覆盖 |
| [AI 分析页面盘后总结自动填入](../doc/features/05-27-0020-AI分析页面盘后总结自动填入.md) | showEvent 触发 → `last_settled_trade_date(now)` 算「下午 4 点分界 + 周末跳到周五」→ 读 market_summaries 自动 setPlainText；仅在为空时填，状态标志防覆盖 | 🟢 已上线，9 个时间边界 case 全部验证通过 |
| [短线投机 Prompt 双模板自评](../doc/reports/05-27-0050-短线投机Prompt双模板对比自评.md) | 新增 `speculator_scalper`（实战派，4225 字）+ `speculator_data_driven`（数据派，13056 字）两个模板；DeepSeek V4 deep_thinking 实跑对比 + `tools/compare_speculator_prompts.py` 一次性对比脚本 | 🟢 已上线，辉夜偏向 scalper 实战派（明牌六字段完整、风险点给具体盘中观察锚点） |
| [CLI 评估回测 / Agent 优化初步设计](../doc/design/05-26-2126-CLI评估回测Agent优化初步设计.md) | **候选 #2（重头）** — 把 30 天历史盘后数据用起来跑虚拟回测 | 🟡 设计已完成，**待主人决定是否开工** |

---

## 二、盘后数据自动化（M1~M5 已完成 / M0 部分未实施 ⚠️）

> Phase M1~M5 全部交付，主线 11 天 + 加分 M5 半天，2026-05-26 收官。  
> Phase M0「Prompt 文件化」：迁移 / PromptLoader / 题材抽取 prompt 提取 ✅；  
> 但 `_shared/` 复用片段 / 4 个 custom_* 重命名 / `_index.json` 三件事 **未实施**  
> （主人 2026-05-26 决议不补做，详见施工方案 §4.4 / §4.7 / §14.1 顶部红框）。

| 文档 | 适合谁看 | 当前状态 |
|------|----------|----------|
| [盘后数据自动化-功能说明](../doc/features/05-26-2126-盘后数据自动化功能说明.md) | **主人** — 用主人的语言讲"做了什么、为什么、长什么样" | 🟢 现行版，**已实装** |
| [盘后数据自动化-施工方案](../doc/design/05-26-2126-盘后数据自动化施工方案.md) | 辉夜自己 — 14 张表、6 个 Phase、文件清单、§17 调用链速查 | 🟡 现行版，**M1~M5 ☑ / M0 部分 ☑**，已于 §18.1 标注真实状态 |
| [_tushare_field_audit.md](./_tushare_field_audit.md) | 辉夜自己 — Tushare 各接口的实测字段 / 单位 / 边界 | 🟢 永久参考（字段层依据，符合白名单第 ③ 类） |

历史归档（已被现行版取代）：

| 文档 | 状态 |
|------|------|
| [盘后数据获取-初步设计-OBSOLETE](../doc/design/05-26-2126-盘后数据获取初步设计-OBSOLETE.md) | ⚪ OBSOLETE — 已被「功能说明 + 施工方案」取代 |

---

## 三、整体架构 / 既有功能

| 文档 | 内容 |
|------|------|
| [NewsTrading 架构方案](../doc/design/05-26-2126-NewsTrading架构方案.md) | 三段式流水线（crawl → clean → analyze）+ 模型分流（§13）+ 题材预测库（§14）+ 盘后数据模块（§15） |
| [CLI 使用文档](../doc/guides/05-26-2126-CLI使用文档.md) | 12 个 `tools/*.py` 命令行入口的速查 — 用途、参数、示例、退出码、典型工作流 |
| [soul.md](./soul.md) | 辉夜与主人协作的规约（角色 / 工作流 / 文档归档边界） |

---

## 四、历史快照 / 评审记录

| 文档 | 状态 |
|------|------|
| [第六轮项目评审 (2026-05-26)](../doc/reports/05-26-2100-第六轮项目评审.md) | 🟢 **现行版** — 发现 15 项（3 严重 / 4 高 / 6 中 / 2 低）；本轮已修文档，待主人决定其他项 |
| [第五轮代码评审 (2026-05-23) - OBSOLETE](../doc/reports/05-23-1800-第五轮代码评审-OBSOLETE.md) | ⚪ OBSOLETE — 内容已被现状版覆盖 |
| [_post_migration_sample.md](./_post_migration_sample.md) | ⚪ ARCHIVE — M0 prompt 文件化迁移后的 system+user prompt 基线快照，可用于未来对比是否漂移（符合白名单第 ③ 类） |
| [工作目录文档迁移变更日志 (2026-05-26)](../doc/updates/05-26-2126-工作目录文档迁移.md) | 🟢 归档 — 本次 8 份文档从 `.huiye/` 迁入 `doc/` 的完整记录 |

---

## 五、文件命名约定

### `.huiye/` 内（白名单）

- `soul.md` / `README.md` — 固定文件名
- `_xxx.md`（下划线前缀）— 临时 / 快照 / 调试参考 / 字段审计

### `doc/` 内（按 [doc/文档分类规范.md](../doc/文档分类规范.md)）

- 命名格式 `MM-DD-HHmm-简述.md`
- 分类靠目录（`design/` / `features/` / `guides/` / `reports/` / `updates/` / `bugfix/` / `其他/`）
- 文件名**不写**类型前缀（如不要 `design-xxx.md`）

> ⚠️ 自 2026-05-26 22:00 起 [soul.md §记忆与文档归档](./soul.md) 已收紧 `.huiye/` 白名单。  
> 新增文档若不符合 4 类白名单 → 直接去 `doc/{子目录}/`；不确定时找主人确认，不要往 `.huiye/` 塞。

---

## 六、各项目当前进度（截至 2026-05-26 21:26）

```
盘后数据自动化
  M0 ████████████░░░░░░░░  60%   Prompt 文件化（迁移 + Loader ✅；_shared/重命名/_index ❌ 决议不做）
  M1 ████████████████████ 100%   data/market.db + Tushare 数据层
  M2 ████████████████████ 100%   CLS + AI 兜底
  M3 ████████████████████ 100%   GUI 独立页面 + 7 个 Tab
  M4 ████████████████████ 100%   AnalysisService 集成 + 定时任务 + ai_reports 索引
  M5 ████████████████████ 100%   历史回填（30 天 hybrid 全跑通，平均完整度 95.24%）

  ➕ GUI 优化（M6 候选）🟢 98%
     方案见 doc/design/05-26-2116-盘后数据GUI优化效果方案.md
     A 字段补全 ✅ 已上线
       ├─ A1 7 大指数 3x3 卡片
       ├─ A2 涨/跌家数 + 北向 + 五日累计 + 涨停标杆等 5 行 KPI
       ├─ A3 板块表加「龙头股 Top3」列 + 高/中风险底色
       ├─ A4 龙虎榜增「其他席位」面板（QSplitter 三栏）
       └─ A5 数据质量明细组（L1/L2/L3 + numeric_field + gaps/conflicts）
     A+ 板块行情扩展 ✅ 已上线（Top 10 → Top 20 + Bottom 10）
     ➕ 后端 4 字段补全 ✅ 已上线（pct_5d 全板块 / leaders / lu_count / catalysts × 2）
     ➕ AI 数据 4 批次升级 ✅ 已上线（2026-05-26 23:55，详见 doc/updates/05-26-2355-）
       ├─ Batch 1 纯渲染：Top 20 / 连板 N板·题材·炸N次 / 龙虎榜上榜原因 / gaps bug
       ├─ Batch 2 后端补字段：seal_rate_prev / main_net_yi / promotion_detail / sectors_bottom
       ├─ Batch 3 板块异动加涨跌幅 + 龙虎榜其他席位（沪深股通/机构/大券商）
       └─ Batch 4 advance/decline 全市场涨跌家数（新表 fact_market_breadth）
     B 新增 Tab（建议，~2.5d）  ⚪ 待主人勾选
     C 交互下钻（选做，~2d）  ⚪ 待主人勾选

数据：data/market.db 44 MB / 30 天 market_summaries / 4888 条 CLS 异动
     / 21660 条龙虎榜机构席位 / 46 个知名游资别名
     ➕ 2026-05-26 22:30 force-refresh 7 天数据：sectors_top 20 条/天，
       pct_chg_5d 460/486 板块/天，catalysts ~17/天（CLS+AI 合计）
     ➕ 2026-05-26 23:55 再次 force-refresh 7 天（4 批次升级合入）：
       新增 fact_market_breadth 单行表（advance/decline/大涨/大跌/样本）
       新增 sectors_bottom + market_shock 加涨跌幅 + dragon_tiger.other_traders
       7 天平均完整度 95.5%，平均 62.9s/天，累计 API 148 次

CLI 评估回测 / Agent 优化
  ⚪ 0% — 设计已完成，未开工

文档目录治理（本轮 2026-05-26 21:26 完成）
  ✅ 100% — 8 份正式文档从 .huiye/ 迁入 doc/，链接全部修复
  ✅ soul.md / 文档分类规范.md 已同步边界规则
  ✅ 项目根 README.md / SQL 注释 / Python 注释 引用已更新

第六轮 Review（2026-05-26 21:00）发现的其他待处理项
  🟠 4 个高危 — API Key 明文 / market_fetch 段缺失 / theme_extraction 模型矛盾 / current_prompt_template 重复
  🟡 6 个中危 — README / agent docstring / GUI 死注释等
  🟢 1 个低危 — _post_migration_sample 待整理（doc/ 归档问题本轮已修，剩 1 项）
  详见 doc/reports/05-26-2100-第六轮项目评审.md §2~§4，主人审阅后另行决定
```
