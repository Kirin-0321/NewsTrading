# .huiye/ 文档总索引

> 辉夜的私有工作目录，按 [soul.md §记忆与文档归档](./soul.md) 收紧后**仅保留 4 类**：  
> ① 角色规约 `soul.md` ② 文档总索引 `README.md`（本文件）  
> ③ `_` 前缀的临时草稿 / 调试快照 / 字段审计 ④ 跨多份正式文档的"汇总索引"  
>  
> **其余一切正式文档已迁入 `doc/{子目录}/`**（迁移日志见 [doc/updates/05-26-2126-工作目录文档迁移.md](../doc/updates/05-26-2126-工作目录文档迁移.md)）  
> 最后更新: 2026-05-27 21:58（**题材页·板块走势 Tab 上线** 在「🎯 题材预测库」详情区「📊 打分明细」右边新增「📈 板块走势」Tab：选中题材 → 顶部 sparkline 折线图（QPainter 自绘、正红负绿、报告日橙竖线 + D+1~D+5 浅黄背景带）+ 底部 4 列明细表。新增 service `sector_daily_query.py` + CLI + widget `gui/widgets/sparkline.py`，无第三方依赖）

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
| [题材强度评分带符号化（v3）](../doc/updates/05-27-0949-题材强度评分带符号化.md) | strength_score 改为 -100~100 有符号；删除冗余 sentiment 字段；新增 利空/中性/利多 9 级映射 | 🟢 **已实施**（v3 内容已并入 v4 全部施工完成） |
| [题材抽取保存与 GUI 补全设计（v3+v4 合并）](../doc/design/05-27-1003-题材抽取保存与GUI补全设计.md) | 技术细节版 — 抽取层 / 保存层 / 题材显示 GUI / 打分显示 GUI 全链路设计；含 12 个决策点表 + §10 第一轮 11 项遗漏 + §10·B 第二轮 10 项 N1~N10 | 🟢 **已实施**（按本文档施工 9 Step 全部完成） |
| [⭐ 题材抽取打分完整流程·主人通读版](../doc/design/05-27-1025-题材抽取打分完整流程.md) | **给主人看** — 中文化无英文术语，7 阶段流程图 + 中英对照表 + Q&A + 数据库表全清单 + 施工里程碑；P1 已采纳"统一规范化" | 🟢 现行版，**v4 主链路已实施，等 plan M3 打分实施** |
| [_施工_抽取保存 GUI 补全](_施工_抽取保存GUI补全.md) | 给辉夜看 — 12 个 Step 行号级 diff + §2.X review 补丁（P1~P11） + 测试用例 / 回滚 / 风险 | 🟢 **已实施完成**（待主人验收后可删；旧 _施工_题材强度带符号化.md 已删除） |
| [tools/test_ai_sector_code_accuracy.py](../tools/test_ai_sector_code_accuracy.py) | AI 板块代码准确率回归测试（验证 A2 决策） — 首测 DeepSeek V4 Pro = 7.4%，未来模型升级时重跑 | 🟢 已上线，留作回归 |
| **v4 配套回归 CLI**（边写边测落地）| `tools/test_matcher.py` / `test_theme_normalize.py` / `test_theme_schema_v4.py` / `test_theme_store_v4.py` / `test_scoring_scheduled_stub.py` | 🟢 50+ 用例全绿 |
| [⭐ 模板回测功能·设计通读版](../doc/design/05-27-1140-模板回测功能设计.md) | **给主人看** — 两种回测形态对比（A 事后打分 / B 事前虚拟）+ 6 个决策点 + 一图流 + Q&A + 一期/二期里程碑 | 🟢 主人已决策 A+B 并行 + AI 评分员一期 |
| [⭐ 三库表结构详细设计](../doc/design/05-27-1209-三库表结构详细设计.md) | **辉夜+主人共看** — news / market / ai_inference 三库 10 张表逐字段中文化（v3 新增），跨库 JOIN 模式 | 🟢 **Phase -1 已完整实施**，fact_sector_daily 复用现有 schema |
| [⭐ 施工文档·模板回测功能 v3.1](_construction_backtest_v3_1.md) | **辉夜内部用** — 9 个 Phase 拆解 + 35+ 子任务 + 命名约定 + 风险登记 + 验收 checklist；总工期 5.5-6 天 ⚠️ 文件名用英文（中文名触发 Cursor Write bug） | 🟢 **Phase -1/0/1 已完成**（-1.7 NewsDB 类按"不过度设计"原则取消）；下一步 Phase 2 打分核心 |
| **Phase -1 实装产物（2026-05-27 13:45）** | services/storage/ai_inference_db.py + ai_migrations/001 + cross_db.py + market/migrations/005 + tools/test_three_db.py（10 用例全绿） | 🟢 已交付 |
| **Phase 0 实装产物（2026-05-27 13:50）** | tools/check_three_db_health.py（三库 schema 巡检，11 项检查全绿）+ main.py 启动卫兵集成 | 🟢 已交付 |
| **Phase 1 实装产物（2026-05-27 14:00）** | services/market/{stock,sector}_daily_sync.py + trade_date.py 追加 next_trade_date/trade_dates_between + tools/sync_daily_market.py CLI + tools/test_daily_sync.py（7 用例 mock 全绿）+ scheduled_runner 接通两个调度桩 | 🟢 **已交付 + 真实端到端验证**：5/19-26 共 6 个交易日 / 33005 行 fact_stock_daily 已入库；fact_sector_daily 6 天命中幂等跳过 |
| **Phase 2 实装产物（2026-05-27 14:20）** | services/scoring/script_scorer.py 算法核心（6 指标 + 防穿越 + UPSERT）+ scoring_service.py（run_daily_scoring/rescore_range/get_template_eval/get_theme_score_detail 4 API）+ tools/score_themes.py CLI + tools/test_script_scorer.py（10 用例一次过）+ scheduled_runner 接通 theme_score_daily；**benchmark 源修正**：fact_index_daily 而非过时设计文档说的 fact_stock_daily（daily 接口只返个股） | 🟢 已交付 |
| **Phase 5 实装产物（2026-05-27 14:25）** | services/scoring/snapshot.py 历史快照重建（cutoff_hour<16 自动回退前一交易日盘后 + 周末自动用 pretrade_date + 防穿越 assert）+ tools/test_snapshot.py（6 用例一次过 + 真实数据 5/26 16:00=268 条新闻+5/26 盘后 / 5/26 10:00=280 条新闻+5/25 盘后 / 5/24 周六=44 条+5/22 盘后）| 🟢 已交付 |
| **Phase 6 实装产物（2026-05-27 14:30）** | tools/backtest_prompt.py 虚拟 md 生成 CLI（snap → AnalysisService.analyze(market_summary=snap.md, auto_market=False) → rename _backtest_{date} → UPDATE is_backtest=1）；支持 --workers 并发 / --overwrite / --dry-run / --date-range（已 dry-run 烟测 5/26 268 条新闻通过） | 🟢 Step 6.1/6.2/6.3/6.4 已交付（含 GUI 真/回筛选） / **Step 6.5 批量 LLM 待主人授权** |
| **Phase 4 实装产物（2026-05-27 16:05）** | services/scoring/scoring_service.py 新增 get_report_eval + get_template_eval 加 time_dim 参数；tools/query_eval.py --by report/--time-dim；tools/test_scoring_eval.py（10 用例一次过）；gui/workers/rescore_worker.py（QThread 异步重打分）；gui/pages/prompt_eval_page.py master-detail 双表重写（14 列模板汇总 + 13 列报告明细 + 双击跳题材页 signal + D+N 5 列完整 + 涨/跌染色 + 样本量预警）；gui/main_window.py 接 theme_drilldown_requested signal；[详细说明书](../doc/features/05-27-1605-模板评估页master-detail重构.md) | 🟢 **已交付** + 52/52 全量回归绿 + GUI 烟测 OK |
| **评估页单报告打分按钮（2026-05-27 16:45）** | scoring_service.rescore_one_report（按 report_id 精准打分）+ tools/score_one_report.py CLI + 下表三态按钮（⚡初次/🔄续打/♻重打）+ get_report_eval LEFT JOIN 全量报告显示；[说明](../doc/features/05-27-1645-评估页单报告打分按钮.md) | 🟢 已交付 + 57/57 全量回归绿 |
| **手动回测覆盖语义（2026-05-27 16:35）** | `backtest_one(overwrite=True)` 真正删旧重跑（_delete_existing_backtest 删 5 表 + md）；GUI 加默认勾选的「覆盖已存在」开关；[bugfix](../doc/bugfix/05-27-1635-手动回测覆盖语义.md) | 🟢 已交付 + case_07/08 全绿 |
| **评估页单报告删除按钮（2026-05-27 17:10）** | services/storage/ai_reports_store.delete_report 通用 API（allow_real 防误删 + dry_run + DeleteResult dataclass）+ tools/delete_report.py CLI（--report-id/--allow-real/--dry-run/--list/--yes/--json）+ gui/workers/delete_report_worker.py + 评估页下表「删除」列（backtest 单确认 / real 双重确认输入末 4 位）+ backtest_prompt._delete_existing_backtest 重构为薄包装（60→30 行 DRY）；[说明](../doc/features/05-27-1710-评估页单报告删除按钮.md) | 🟢 已交付 + 60/60 全量回归绿（含 case_14/15/16 CASCADE/protect/dry_run） |
| **⭐ 涨停数据三源融合 v1（2026-05-27 18:10）** | 接入同花顺 `limit_list_ths` 主源（5 泳池：涨停/连扳/炸板/跌停/冲刺涨停）+ 三源 COALESCE 合并（ths > kpl > d）+ 8 个 Phase 全交付；新增 `fact_limit_sprint` 表 / `fact_limit_stock` 扩 8 列（lu_desc/封板率/market_type/tag/free_float/industry/total_mv/source）/ ladder Tab 6 列 / 新增 2 个 Tab（连扳池+冲刺涨停）/ markdown 章节「五·B 冲刺涨停」；7 天回填 5/19~5/27 全绿；5/27 ladder 全 0 → 61 只 + lu_desc 100% 命中；同时修复东财长期漏算 28% 涨停股的隐性 bug；[内容文档](../doc/design/05-27-1721-涨停数据三源融合设计.md) / [施工方案](../doc/design/05-27-1722-涨停数据三源融合施工方案.md) / [变更日志](../doc/updates/05-27-1810-涨停三源融合上线.md) | 🟢 已交付 + 70/70 全量回归绿（含 test_three_source_merge 10） |
| **手动回测流式输出（2026-05-27 17:57）** | progress_callback 贯穿四层（worker→backtest_one→analyze→_stream_chat/extract_from_file）+ ManualBacktestPage 新增 stream_browser（LLM/题材双阶段共用）+ CLI --verbose + 顺手修 `_maybe_extract_themes` 漏传 callback 的存量 bug（AI 分析页也受益）；[bugfix](../doc/bugfix/05-27-1757-手动回测流式输出.md) | 🟢 已交付 + dry-run 验证通过 + 静态导入信号齐全（stage/progress/streaming/finished_result/error） |
| **盘后页·全部个股 + 全部板块 Tab（2026-05-27 18:10）** | gui/utils/market_db_helper 加 `query_all_stocks/query_all_sectors`；market_summary_page 板块 tab 改 ComboBox 切换（Top20/Bottom10/全部 486 个）+ 搜索框；新增「全部个股」tab（5504 只 7 列 + _SortableNumItem 数值排序 + 涨停/跌停/炸板染色 + 模糊搜索）；CLI `tools/query_all_{stocks,sectors}.py` + 5 用例回归；[说明](../doc/features/05-27-1810-盘后页全部个股全部板块Tab.md) | 🟢 已交付 + 75/75 全量回归绿 + 5504 行填表 180ms 实测 |
| **⭐ 报告日期协议错位 + 回测命名重构（2026-05-27 18:30，含 20:00/20:30 双 hotfix）** | schema 协议 YYYYMMDD vs 实际入库 YYYY-MM-DD 长期错位 → GUI「单报告打分」ValueError；同时治理回测产物"假冒生成时间"。命名 4 版迭代：v1`5月27日_18时12分_..._backtest_20260522.md`（前缀真实时间）→ v2`5月22日_0时00分_..._backtest.md`（18:30 重构，治"假冒时间"但多模板覆盖）→ v3`..._backtest_{tpl}.md`（20:00 hotfix，治多模板覆盖但同模板多跑覆盖）→ **v4 终态** `..._backtest_{tpl}_{HHMMSS}.md`（20:30 hotfix2，治同模板同日多跑覆盖 + `backtest_prompt` 策略反转：默认追加不跳过，`--overwrite` 改为"删全部历史版本再写"）。改造点：①`AINewsAnalyzer.save_report/analyze` 加 naming_dt/backtest_suffix/extra_suffix ②`AnalysisService.analyze` 加 simulated_trade_date 透传 + `_sanitize_template_id` + HHMMSS 拼接 ③`parse_report_meta` 优先文件名前缀解析 → YYYYMMDD ④删 `backtest_prompt._rename_to_backtest`（80→30 行），`backtest_one` 覆盖策略反转 + GUI 同步删「已存在则覆盖」checkbox（避免主人误触发清空旧版本） ⑤`theme_store/ai_reports_store/_eligible_score_dates` 入口加 `_ensure_yyyymmdd` 防御 ⑥GUI 新建 `gui/utils/date_format.py` helper + 显示层适配 ⑦一次性迁移 CLI `tools/migrate_report_date_yyyymmdd.py`（已 apply：16 行 db 字段 + v1→v2→v3 共 2 次文件重命名；v3/v4 都视为合法终态不再动）⑧顺手治 rescore_range 隐藏 P1（区间重打分静默跑空）；[bugfix](../doc/bugfix/05-27-1830-报告日期与回测命名重构.md)（§七 hotfix1 / §八 hotfix2） | 🟢 已交付 + 单元验证全过（含同模板同日两跑文件名不冲突）+ GUI 手测交主人 |
| **⭐ 盘后数据「逐表幂等」缓存语义修订（2026-05-27 19:20）** | 修复主人发现的"按按钮 + 强制重拉后『全部个股 Tab』仍显示共 0 只"长期 bug；根因：双层早退（service 命中 market_summaries 直 return + fetcher 命中 fact_index_daily 跳 14 步）+ fact_stock_daily 根本不在 fetcher 14 个 step 清单（只能靠计划任务）。改造点：①FetchResult 加 force_refresh 字段 + 新增 _table_has_data/_purge_limit_stock helper ②删除 _has_index_data 一刀切早退 ③A 组 9 个 step 加表级自检（已有 → skipped 列表）④B 组 step 4 进入前 _purge_limit_stock（4 step 共写无 src 字段默认重拉，step 8 仅内存不参与）⑤新增 step 15 _fetch_stock_daily 复用 sync_stock_daily ⑥service.build() 缓存命中改 fall-through 跑 fetcher 自检补缺 ⑦summary.breadth 加 stock_daily_rows + schema.json 加权 ⑧_purge_trade_date 加 fact_stock_daily；[内容文档](../doc/design/05-27-1850-盘后数据逐表幂等缓存设计.md) / [施工方案](../doc/design/05-27-1900-盘后数据逐表幂等缓存施工方案.md) / [bugfix](../doc/bugfix/05-27-1920-逐表幂等缓存修复.md) | 🟢 已交付 + test_fetch_idempotent 4/4 + test_daily_sync 7/7 全绿，等主人 GUI 手测 |
| **⭐ 手动回测任务队列并行（2026-05-27 21:30，**21:35 hotfix 默认 3→8**）** | 把手动回测从「一次一条」改为「连点 N 次排队 + 后台 **8** 并发 + 任务列表联动三大块」。新增 BacktestTaskManager（队列 + 调度 + 状态机 5 态 + `__init__` 可注入 max_concurrent）+ relay signals 治理跨线程 race；改造 ManualBacktestPage（中段横向 splitter「时间预览/任务列表」+ 11 个槽函数联动下方流式/md/题材）；删除策略只删 GUI 行不动 md/db。21:35 hotfix：主人实测 3 路顺畅要求"更多路"→ 默认从 3 提到 8（不动 UI）。[内容文档](../doc/design/05-27-2101-手动回测任务队列并行设计.md) / [施工方案](../doc/design/05-27-2110-手动回测任务队列并行施工方案.md) / [上线变更日志](../doc/updates/05-27-2130-手动回测任务队列上线.md) | 🟢 **已交付** + test_manual_backtest_queue 11/11 全绿（3 并发上限显式注入/FIFO/状态机/删除策略/shutdown/默认 8 全覆盖），GUI 头像构造烟测通过，待主人手测 |
| **题材页·板块走势 Tab（2026-05-27 21:58）** | 在「🎯 题材预测库」详情区「📊 打分明细」右边新增「📈 板块走势」Tab：选中题材 → 顶部 sparkline 折线图（QPainter 自绘、正红负绿、报告日橙竖线 + D+1~D+5 浅黄背景带 + hover tooltip）+ 底部 4 列明细表（交易日 / 偏移 / 涨跌幅 / 主力净流入 / 当日排名，报告日行浅橙、评估窗口行浅黄染色）。新增 service `services/market/sector_daily_query.py` + CLI `tools/sector_daily_query.py` + widget `gui/widgets/sparkline.py`（无第三方依赖）；[功能说明](../doc/features/05-27-2158-题材页板块走势Tab.md) | 🟢 已交付 + CLI 烟测 31 行返回正常 + GUI 离屏构造冒烟 OK，待主人 GUI 手测 |
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
