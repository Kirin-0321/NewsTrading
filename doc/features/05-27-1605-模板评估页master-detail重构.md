# 模板评估页 master-detail 重构说明书

> **作者**: 辉夜
> **时间**: 2026-05-27 16:05
> **任务来源**: 主人原话「现在改造并优化模板评估页面，首先应该显示每个模板以及该模板下的每个输出文件，其次D135改为完整5日的每天分数，还有其他很多可改进的」

---

## 1. 背景与动机

### 1.1 改造前的痛点

| # | 问题 | 严重度 |
|---|------|--------|
| 1 | 评估页**全部跑桩数据**（`_load_stub_rows`），D+1/D+3/D+5 都是 `—` | High |
| 2 | 「重打分」按钮永远弹「plan M3 未上线」（实际后端 ready 半个月了） | High |
| 3 | 只显示 D+1/D+3/D+5 三列（缺 D+2/D+4） | Med |
| 4 | 没法看「某模板下每份报告」的详细表现 | **主人吐槽点** |
| 5 | 没有「真实/回测/全部」筛选（CLI 早有 `--backtest-only`） | Med |
| 6 | stub 用 `YYYY-MM-DD` 比库存 `YYYYMMDD`，时间筛选可能失效 | Low |
| 7 | 时间维度模糊（CLI=score_date / GUI 想要 report_date） | Med |

### 1.2 改造目标

* **主人显式需求**：
  - ✅ 显示每个模板 + 该模板下的每个输出文件
  - ✅ D+1/D+3/D+5 改为完整 5 列 D+1~D+5
* **辉夜延伸**：
  - ✅ 接通 `scoring_service.*` 真实数据（去掉桩）
  - ✅ 接通 `rescore_range` 真实重打分（去掉 M3 弹窗）
  - ✅ 时间维度 radio（report_date / score_date 二选）
  - ✅ 真实/回测/全部 三选 combo
  - ✅ 双击下表行 → 跳「题材预测」页查看该报告题材
  - ✅ 涨/跌染色（绿/红）+ 样本量低于 5 黄色预警
  - ✅ 重打分用 `QThread` worker 异步（不阻塞 UI）

---

## 2. 主人定的关键决策

| 问题 | 主人选择 | 影响 |
|---|---|---|
| 时间维度 | **两个都做**（radio 切换） | 加 `time_dim` 参数 + UI radio |
| 默认筛选 | **全部**（真实+回测合并） | combo 默认第 0 项 |
| 布局 | **master-detail 双表**（不是 tree） | 用 `QSplitter` 上下排 |
| 折线图 | 本次不做（M4.2 单独任务） | `chart_area` 干脆删了 |

---

## 3. 改造范围与文件清单

### 3.1 后端（`services/scoring/scoring_service.py`）

| 改动 | 函数 | 行号区段 |
|---|---|---|
| ➕ 加参数 | `get_template_eval(time_dim="score_date")` | 296-409 |
| ➕ 返回字段 | `last_report_date` | 407 |
| ➕ 新函数 | `get_report_eval(prompt_id, time_dim="report_date", ...)` | 412-518 |
| ➕ 参数校验 | `_validate_time_dim()` + `_ALLOWED_TIME_DIMS` 常量 | 553-563 |

#### `get_report_eval` 设计要点

* **关联键**：`ai_reports.file_path = theme_predictions.report_path`（两者均为相对 posix）
* **聚合粒度**：`GROUP BY ar.id`（一份报告一行）
* **过滤维度**：`days` × `time_dim` × `is_backtest_filter` × `prompt_id` × `prompt_version`
* **排序**：`report_date DESC, prompt_id ASC, id DESC`
* **关键字段**：`report_id / report_date / file_path / prompt_id / prompt_version / is_backtest / themes_count / d1~d5_avg / alpha_avg / hit_rate_avg / direction_correct_rate`

### 3.2 CLI（`tools/query_eval.py`）

| 改动 | 描述 |
|---|---|
| ➕ `--by {template,report}` | 模板汇总（默认）/ 报告级明细切换 |
| ➕ `--time-dim {score_date,report_date}` | 时间维度（默认 score_date 兼容老 CLI） |
| ➕ `--prompt-id` / `--prompt-version` | report 模式下钻过滤 |
| ➕ `_print_report_table()` | 报告级表格打印（含文件名截尾） |

### 3.3 GUI

#### 新增 `gui/workers/rescore_worker.py`

`QThread` 子类，包装 `scoring_service.rescore_range()`，emit 三个信号：
- `stage(str)` — 进度文案
- `finished_result(dict)` — `BatchScoringResult` 字段字典
- `error(str)` — 异常 traceback

#### 重写 `gui/pages/prompt_eval_page.py`（365 → 449 行）

**控件层次**：
```
QVBoxLayout
├── 标题 + 副标题
├── 控制条 QGroupBox
│   ├── 第1行: time_dim radio + range_combo + backtest_combo + ignore_version_check
│   └── 第2行: status_label + rescore_btn + export_btn + refresh_btn
└── QSplitter (Vertical, 3:4)
    ├── 上 QGroupBox 「📈 模板汇总」
    │   ├── tpl_table (14 列)
    │   └── tpl_stats_label
    └── 下 QGroupBox 「📄 选中模板的报告明细」
        ├── report_table (13 列)
        └── report_stats_label
```

**14 列模板汇总**：
| 列 | 来源 |
|---|---|
| 模板 | `friendly_prompt_name(prompt_id)` |
| 版本 | `prompt_version` |
| 真/回测 | （汇总行不分，固定 `—`；分维度走筛选条） |
| 样本 | `sample_count` |
| D+1 ~ D+5 | `d1_avg ~ d5_avg` |
| α | `alpha_avg` |
| 命中率 | `hit_rate_avg` |
| 方向准 | `direction_correct_rate` |
| AI 高质量 | 占位 `—`（Phase 7 待实现） |
| 最近报告日 | `last_report_date` |

**13 列报告明细**：
| 列 | 来源 |
|---|---|
| 报告日期 | `report_date` |
| 真/回 | `is_backtest` → "真实"/"回测" |
| 模板 | `prompt_id` |
| 版本 | `prompt_version` |
| #题材 | `themes_count` |
| D+1 ~ D+5 | `d1_avg ~ d5_avg` |
| α | `alpha_avg` |
| 命中率 | `hit_rate_avg` |
| 文件名 | `Path(file_path).name` |

**交互**：
- 上表点行 → `_on_template_selected()` → 取 `Qt.UserRole` 里塞的 `prompt_id` → `_reload_report_table()`
- 下表**双击** → `_on_report_double_clicked()` → emit `theme_drilldown_requested(report_date, prompt_id)`
- 涨/跌染色：D+N / α 列 > 0 绿 / < 0 红
- 样本 < 5 染黄 + tooltip
- AI 高质量列灰色显示 + tooltip 「Phase 7 待实现」
- 重打分按钮 `disable` 期间所有控件 disable，状态 label 实时显示进度

#### 修改 `gui/main_window.py`

新增 signal 接驳 + handler：
```python
eval_page.theme_drilldown_requested.connect(self._on_eval_drilldown)

def _on_eval_drilldown(self, report_date: str, prompt_id: str) -> None:
    self.show_page('theme_prediction')
    theme_page._reload_dates(prefer_date=report_date)
    # 找到 prompt_combo 里对应的 prompt_id 选中
    ...
```

### 3.4 测试（`tools/test_scoring_eval.py` 新文件）

10 用例覆盖：

| # | 用例 | 验证点 |
|---|------|--------|
| 01 | `get_template_eval()` 默认 | 全部样本，TEST_EV_A=5 theme |
| 02 | `is_backtest_filter=1` | 只剩 R3 (TEST_EV_A v1 BT) |
| 03 | `is_backtest_filter=0` | A=3 B=1 |
| 04 | `time_dim=report_date` vs `score_date` 边界差异 | 一个过滤 R3 一个保留 D+3/D+5 |
| 05 | `ignore_version=False` | TEST_EV_A v1/v2 分两行 |
| 06 | `get_report_eval()` 默认 | 4 份报告各 1 行，themes_count 正确 |
| 07 | `prompt_id='TEST_EV_B'` | 只剩 R4 |
| 08 | `is_backtest_filter=1` | 只剩 R3 |
| 09 | `time_dim=report_date, days=7` | 过滤掉 today-10 的 R3 |
| 10 | 非法 `time_dim` | `ValueError` |

**夹具特点**：
- 前缀 `TEST_EV_` 隔离，测试前清理 + 测试后清理
- 4 份报告 / 6 个题材 / 18 个打分行（每个题材 D+1/D+3/D+5）
- 所有 `stock_weighted_pct=2.0`，便于断言均值

---

## 4. 调用链一览

### 4.1 评估页打开 → 数据加载

```
PromptEvalPage.__init__
└── refresh()
    ├── _current_filters()           → {days, time_dim, is_backtest_filter, ignore_version}
    ├── get_template_eval(**filters) → tpl_rows
    │   └── _render_template_table(tpl_rows)
    └── _reload_report_table()
        ├── get_report_eval(prompt_id=selected, **filters) → report_rows
        └── _render_report_table(report_rows)
```

### 4.2 点击模板行 → 下钻下表

```
QTableWidget.itemSelectionChanged
└── _on_template_selected()
    ├── 取 Qt.UserRole → self._selected_prompt_id
    └── _reload_report_table()
        └── get_report_eval(prompt_id=self._selected_prompt_id, ...)
```

### 4.3 双击报告行 → 跳题材页

```
QTableWidget.itemDoubleClicked
└── _on_report_double_clicked()
    ├── 取 Qt.UserRole → {report_date, prompt_id, file_path}
    └── emit theme_drilldown_requested(report_date, prompt_id)
        ↓
MainWindow._on_eval_drilldown
    ├── show_page('theme_prediction')
    ├── theme_page._reload_dates(prefer_date=report_date)
    └── 遍历 prompt_combo 找 prompt_id 并 setCurrentIndex
```

### 4.4 点击重打分 → 异步打分

```
PromptEvalPage._on_rescore
├── QMessageBox.question 确认
├── _set_buttons_busy(True) 锁定全部控件
└── RescoreWorker(rd_start, rd_end, days_back=5).start()
    └── run() in QThread
        └── scoring_service.rescore_range(...)
            ↓
        emit finished_result / error
            ↓
PromptEvalPage._on_rescore_done / _on_rescore_error
    ├── _set_buttons_busy(False)
    ├── status_label 更新
    └── refresh()
```

---

## 5. CLI 对照样板

GUI 改造完后，CLI 也要保持等价能力（主人「CLI-First」原则）：

```bash
# 等价于 GUI 上表（最近 30 天，按 report_date，全部样本）
python tools/query_eval.py --time-dim report_date --days 30

# 等价于 GUI 下表（选中某模板后的报告明细）
python tools/query_eval.py --by report --prompt-id speculator_scalper --days 30

# 等价于 GUI「重打分」按钮（M3 时代等效命令）
python tools/score_themes.py --rescore --start 20260427 --end 20260527
```

---

## 6. 验证记录

### 6.1 单元/回归测试（6 套 / 52 用例全绿）

| 测试套 | 用例数 | 结果 |
|---|---|---|
| `tools/test_scoring_eval.py`（新） | 10 | 10/10 PASS |
| `tools/test_snapshot.py` | 9 | 9/9 PASS |
| `tools/test_backtest_e2e.py` | 6 | 6/6 PASS |
| `tools/test_three_db.py` | 10 | 10/10 PASS |
| `tools/test_script_scorer.py` | 10 | 10/10 PASS |
| `tools/test_daily_sync.py` | 7 | 7/7 PASS |
| **合计** | **52** | **52/52 ✅** |

### 6.2 烟测

1. **CLI 三模式**（`query_eval.py` template / report / time_dim 切换）
   - 全部 0 异常返回（当前数据库无打分样本，输出 "(空)" 正常）
2. **评估页构造**
   - `PromptEvalPage()` 无报错，`tpl_table` / `report_table` 行数 = 0（预期）
   - `dim_report_radio` 默认勾选 ✅
3. **MainWindow 集成**
   - 11 个 page 加载成功
   - `theme_drilldown_requested` signal 已连接 ✅

### 6.3 0 Lint

| 文件 | 状态 |
|---|---|
| `services/scoring/scoring_service.py` | ✅ |
| `tools/query_eval.py` | ✅ |
| `tools/test_scoring_eval.py` | ✅ |
| `gui/workers/rescore_worker.py` | ✅ |
| `gui/pages/prompt_eval_page.py` | ✅ |
| `gui/main_window.py` | ✅ |

---

## 7. 未做事项（明确边界）

| # | 项 | 理由 |
|---|---|---|
| 1 | 折线图区 | 主人明确选「本次不做」，等 plan M4.2 |
| 2 | AI 高质量占比真数据 | 依赖 Phase 7 `ai_scorer.py`，目前列保留+灰色 + tooltip |
| 3 | 「真/回测」列在模板汇总表分维度 | 当前固定 `—`，由筛选条决定整体；如要在一行同时显示两种维度需要 SQL pivot，过度设计 |
| 4 | 导出 CSV 包含折线图数据 | 折线没做，自然不导出 |
| 5 | 双击上表行跳转 | 双击下表行已足够，上表汇总行没有具体 report_date 没法跳 |

---

## 8. 文件变更摘要

| 文件 | 类型 | 行数 | 备注 |
|---|---|---|---|
| `services/scoring/scoring_service.py` | 修改 | 413 → 567 | +get_report_eval, +time_dim |
| `tools/query_eval.py` | 重写 | 214 → 287 | +--by report, +--time-dim |
| `tools/test_scoring_eval.py` | 新建 | 360 | 10 用例 |
| `gui/workers/rescore_worker.py` | 新建 | 65 | 重打分 worker |
| `gui/pages/prompt_eval_page.py` | 重写 | 365 → 449 | master-detail |
| `gui/main_window.py` | 修改 | 223 → 250 | +signal handler |

**净增**：~ 700 行（多数是文档字符串与表格渲染）

---

## 9. 后续可继续优化方向

| 优先级 | 项 | 说明 |
|---|---|---|
| Low | 折线图区接 PyQtChart | M4.2 单独任务 |
| Low | 模板汇总加「真/回比例」列 | 显示 `BT:5 / Real:8` 类似 |
| Low | 报告明细加「最后打分日」列 | 现在隐藏在题材层 |
| Low | 重打分时进度条 + 当前题材编号 | `RescoreWorker` 加 `progress(cur, total)` signal |
| Med | 评估页支持「按 theme 详情」第三层下钻 | 现在第三层在「题材预测」页，跨页跳了 |

---

## 10. 改造前后对比示意

### 改造前

```
[控制条] 时间范围 | 忽略版本 | [重打分] [导出] [刷新]
[黄色横条] ⚠️ 打分系统未上线，仅展示桩数据
[单表] 模板 | 版本 | 样本 | D+1 D+3 D+5 (全是 —) | 平均 Alpha (—) | 命中率 (—) | AI 高质量 (—) | 最近报告日
[折线图区] 📊 占位 QLabel
```

### 改造后

```
[控制条 第1行] 时间维度⦿report_date○score_date | 时间范围▼ | 样本类型▼ | □忽略版本
[控制条 第2行] [✅ 加载完成 模板 N / 报告 M] [🔁 重打分] [⬇️ 导出 CSV] [🔄 刷新]

╔══[ 📈 模板汇总 ]═════════════════════════════════════════╗
║ 模板 | 版本 | 真/回 | 样本 | D+1 D+2 D+3 D+4 D+5 | α | 命中率 | 方向准 | AI高质量 | 最近报告日 ║
║ ...(真数据，涨绿跌红，样本<5 染黄)... ║
║ 共 N 个模板组 | 时间维度: report_date | 样本类型: 全部 ║
╚══════════════════════════════════════════════════════════╝
╔══[ 📄 选中模板的报告明细 (双击行→题材页) ]══════════════╗
║ 报告日期 | 真/回 | 模板 | 版本 | #题材 | D+1~D+5 | α | 命中率 | 文件名 ║
║ ...(真数据)... ║
║ 共 M 份报告 (prompt_id=xxx 或 全部模板) ║
╚══════════════════════════════════════════════════════════╝
```
