"""打分服务入口（Phase 2 Step 2.2 + 2026-05-27 评估页改造 + 2026-05-28 树形展开扩展
+ 2026-05-28 11:30 板块行情接入 + 加权平均改造 + 2026-05-28 17:15 报告级题材综合涨幅切换
+ 2026-05-28 22:30 α 体系金字塔重构）。

把 :mod:`services.scoring.script_scorer` 单题材单日的算法包装成 7 个对外
API，给 CLI / scheduled_runner / GUI 复用：

D+N 列口径
------------------------------------------------------
* **标的级**（第 3 层 ``get_stock_scores_for_theme``）：``theme_stock_scores.pct_chg``
  即个股当日涨跌幅
* **题材级**（第 2 层 ``get_theme_eval_for_report``）：``sector_pct`` 题材绑定板块当日涨跌幅
* **报告级**（``get_report_eval``）：``theme_pct``（= 0.6·sector + 0.4·标的均值，无标的
  兜底 1.0·sector），按 ``|strength_score|`` 加权，仅 strength > 0 题材参与
* **模板级**（``get_template_eval``）：题材→报告 ``|strength|`` 加权 ``sector_pct``
  → 报告→模板**简单 AVG over reports**（2026-05-28 22:30 改，每份报告等权）

α / α-1 / α+N 金字塔（2026-05-28 22:30 重构，统一中证1000 基准）
------------------------------------------------------
::

    报告级（per_report 加权聚合，仅 strength > 0）
        ├── 报告 α+N = SUM(|str|·(theme_pct − zz1000)) / SUM(|str|)
        ├── 报告 α   = AVG(报告 α+1..+5)，忽略 NULL
        └── 报告 α-1 = AVG(报告 α+2..+5)，忽略 NULL

    模板级（per_template 简单 AVG over reports）
        ├── 模板 α+N = AVG over reports of (报告 α+N)
        ├── 模板 α   = AVG(模板 α+1..+5)，忽略 NULL
        └── 模板 α-1 = AVG(模板 α+2..+5)，忽略 NULL

    题材级（独立体系，与该层 D+N 同基础 sector_pct）
        ├── α+N = sector_pct − zz1000（逐日）
        ├── α   = AVG over 5 days of α+N
        └── α-1 = AVG over D+2..D+5 of α+N

    标的级
        └── α+N = pct_chg − zz1000（join tps 取同 score_date 的 zz1000）

* 单日 ``tps.alpha`` 字段已废（迁移 004 ``DROP COLUMN``），不再读不再写

| API | 用途 |
|------|------|
| :func:`run_daily_scoring` | 调度器每日入口：扫追踪窗口内所有题材跑 D+1~D+5 |
| :func:`rescore_range` | GUI「重打分（区间）」按钮：指定 ``report_date`` 区间重算 |
| :func:`rescore_one_report` | 评估页「单报告打分」按钮：按 ``ai_reports.id`` 精准打 |
| :func:`get_template_eval` | 模板评估页**上表**（按 prompt_id 聚合 D+1~D+5 均值） |
| :func:`get_report_eval` | 模板评估页**树第 1 层**（按 ai_reports.id 聚合，每份报告一行） |
| :func:`get_theme_eval_for_report` | 评估页**树第 2 层**（一份报告下每个题材的 D+1~D+5 透视）|
| :func:`get_stock_scores_for_theme` | 评估页**树第 3 层**（一个题材下每只标的的逐日涨跌）|
| :func:`get_theme_score_detail` | 题材详情第 4 个 Tab（单题材 D+1~D+5 明细） |

时间维度
--------
* ``get_template_eval`` 和 ``get_report_eval`` 都支持 ``time_dim`` 参数：
    * ``"score_date"``（默认 / 兼容老 CLI）：最近 N 天**发生过打分**的题材
    * ``"report_date"``（评估页推荐）：最近 N 天**生成**的报告

数据库：均通过 :mod:`services.storage.ai_inference_db` 访问；不跨库。

防穿越：均委托 :func:`services.scoring.script_scorer.score_theme_on_date`
内部检查，本层不重复。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from services.market.market_db import get_market_db
from services.market.trade_date import (
    TradeDateError,
    next_trade_date,
)
from services.scoring.script_scorer import (
    DEFAULT_BENCHMARK_TS_CODE,
    DEFAULT_HIT_THRESHOLD_PCT,
    ScoringError,
    SkippedTheme,
    score_theme_on_date,
)
from services.storage.ai_inference_db import get_ai_inference_db

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class BatchScoringResult:
    """批量打分聚合结果。"""

    ok: bool
    themes_total: int
    themes_scored: int
    pairs_attempted: int  # (theme, date) 对总数
    pairs_succeeded: int
    days_covered: int
    elapsed_ms: int
    errors: List[str] = field(default_factory=list)


@dataclass
class BatchUnfinishedResult(BatchScoringResult):
    """「一键打分未完成报告」聚合结果。

    在 :class:`BatchScoringResult` 基础上加 3 个 report 维度计数，
    便于 GUI 状态栏与 CLI 输出告知主人：「扫了多少 / 真正动手多少 /
    跳过多少」。``BatchScoringResult`` 自带的 themes_* / pairs_* 字段
    继续累计跨报告的合计值。
    """

    reports_targeted: int = 0  # 命中筛选条件的 (none + partial) 报告数
    reports_done: int = 0      # 实际成功打分的报告数
    reports_skipped: int = 0   # 命中但被跳过（无题材 / 等）的报告数


# ---------------------------------------------------------------------------
# 1. 每日入口（调度器调）
# ---------------------------------------------------------------------------


def run_daily_scoring(
    score_date: Optional[str] = None,
    *,
    days_back: int = 5,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
) -> BatchScoringResult:
    """每日打分入口。

    业务语义：
        在 ``score_date`` 这天，对过去 ``days_back`` 天生成的所有题材打
        当日分。即 ``report_date ∈ [score_date - days_back, score_date - 1]``
        范围内每个题材都会落 1 行 ``theme_prediction_scores``。

    Args:
        score_date: 打分日 ``YYYYMMDD``，默认今天（按本地时间）。
        days_back: 追踪期长度，默认 5（D+1 ~ D+5）。
        hit_threshold_pct / benchmark_ts_code: 透传给 script_scorer。

    Returns:
        :class:`BatchScoringResult`。
    """
    t0 = time.perf_counter()
    sd = (score_date or datetime.now().strftime("%Y%m%d")).strip()
    if not _is_yyyymmdd(sd):
        return BatchScoringResult(
            ok=False, themes_total=0, themes_scored=0,
            pairs_attempted=0, pairs_succeeded=0, days_covered=0,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            errors=[f"score_date 必须是 YYYYMMDD，得到 {sd!r}"],
        )

    # 反推 report_date 上下界：[sd - 2*days_back 自然日, sd - 1 自然日]
    # 取 2 * days_back 是 buffer，确保覆盖周末/节假日
    sd_dt = datetime.strptime(sd, "%Y%m%d")
    start_natural = (
        sd_dt - timedelta(days=days_back * 2 + 3)
    ).strftime("%Y%m%d")
    end_natural = (sd_dt - timedelta(days=1)).strftime("%Y%m%d")

    return _run_scoring_for_range(
        score_date=sd,
        report_start=start_natural,
        report_end=end_natural,
        days_back=days_back,
        hit_threshold_pct=hit_threshold_pct,
        benchmark_ts_code=benchmark_ts_code,
        t0=t0,
    )


# ---------------------------------------------------------------------------
# 2. GUI 重打分入口
# ---------------------------------------------------------------------------


def rescore_range(
    report_date_start: str,
    report_date_end: str,
    *,
    days_back: int = 5,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
    score_date_override: Optional[str] = None,
) -> BatchScoringResult:
    """指定 ``report_date`` 区间重算 D+1~D+days_back 全套分数。

    Args:
        report_date_start / report_date_end: ``YYYYMMDD`` 闭区间。
        days_back: 每个题材回算几天（默认 5 = D+1~D+5）。
        score_date_override: 覆盖每个 D+N 的"截止时间"，默认 = 今天。
            指定可避免给未来日打分（防穿越的兜底）。
    """
    t0 = time.perf_counter()
    _ensure_yyyymmdd(report_date_start)
    _ensure_yyyymmdd(report_date_end)
    cutoff = (
        score_date_override
        or datetime.now().strftime("%Y%m%d")
    )

    return _run_scoring_for_range(
        score_date=cutoff,
        report_start=report_date_start,
        report_end=report_date_end,
        days_back=days_back,
        hit_threshold_pct=hit_threshold_pct,
        benchmark_ts_code=benchmark_ts_code,
        t0=t0,
        force_full_window=True,
    )


# ---------------------------------------------------------------------------
# 共享：核心调度
# ---------------------------------------------------------------------------


def _run_scoring_for_range(
    *,
    score_date: str,
    report_start: str,
    report_end: str,
    days_back: int,
    hit_threshold_pct: float,
    benchmark_ts_code: str,
    t0: float,
    force_full_window: bool = False,
) -> BatchScoringResult:
    """核心调度：扫题材 → 对每个题材跑 D+1~D+N。

    Args:
        score_date: cutoff，超过这天的 D+N 都跳过（防穿越）。
        force_full_window: True 时不论今天到 D+N 几天，全部尝试到 D+days_back
            （重打分场景）；False 时只算 ``score_date`` 当天对应的 D+N。
    """
    ai = get_ai_inference_db()
    ai.ensure_schema()

    with ai.connect() as conn:
        themes = conn.execute(
            "SELECT id, report_date FROM theme_predictions "
            "WHERE report_date BETWEEN ? AND ? "
            "ORDER BY report_date ASC, id ASC",
            (report_start, report_end),
        ).fetchall()
    themes_total = len(themes)

    pairs_attempted = 0
    pairs_succeeded = 0
    days_covered_set: set[str] = set()
    errors: List[str] = []
    scored_themes: set[int] = set()

    market_db = get_market_db()
    market_db.ensure_schema()

    for row in themes:
        theme_id = int(row["id"])
        report_date = str(row["report_date"])
        try:
            score_dates = _eligible_score_dates(
                report_date=report_date,
                score_date_cutoff=score_date,
                days_back=days_back,
                force_full_window=force_full_window,
            )
        except TradeDateError as exc:
            errors.append(
                f"theme={theme_id} 解析交易日窗口失败: {exc}"
            )
            continue

        if not score_dates:
            continue

        for sd in score_dates:
            pairs_attempted += 1
            try:
                score_theme_on_date(
                    theme_id, sd,
                    hit_threshold_pct=hit_threshold_pct,
                    benchmark_ts_code=benchmark_ts_code,
                    write=True,
                )
                pairs_succeeded += 1
                days_covered_set.add(sd)
                scored_themes.add(theme_id)
            except SkippedTheme:
                # 利空 / 中性题材业务规则跳过，不计入失败
                pairs_attempted -= 1  # 回退：不视为尝试
                continue
            except (ScoringError, ValueError) as exc:
                errors.append(
                    f"theme={theme_id} score_date={sd}: {exc}"
                )

    return BatchScoringResult(
        ok=(len(errors) == 0),
        themes_total=themes_total,
        themes_scored=len(scored_themes),
        pairs_attempted=pairs_attempted,
        pairs_succeeded=pairs_succeeded,
        days_covered=len(days_covered_set),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        errors=errors,
    )


def _eligible_score_dates(
    *,
    report_date: str,
    score_date_cutoff: str,
    days_back: int,
    force_full_window: bool,
) -> List[str]:
    """计算给定 ``report_date`` 应该打分的 score_date 列表。

    * 调度器场景（force_full_window=False）：
        只算 cutoff 当天对应的 D+N（如果 cutoff 是 D+3 就只算 D+3）
    * 重打分场景（force_full_window=True）：
        全量 D+1 ~ D+days_back，但跳过 cutoff 之后的日期

    协议（2026-05-27 18:30 入口防御）：
        ``report_date`` / ``score_date_cutoff`` 必须 YYYYMMDD；
        历史曾因 theme_predictions.report_date 误存 YYYY-MM-DD 导致
        next_trade_date 在循环深处才炸（调用栈难定位），现在前置强校验。
    """
    _ensure_yyyymmdd(report_date)
    _ensure_yyyymmdd(score_date_cutoff)
    # 推导出全套 D+1 ~ D+days_back
    full: List[str] = []
    for n in range(1, days_back + 1):
        try:
            sd = next_trade_date(report_date, n)
        except TradeDateError:
            break
        if sd > score_date_cutoff:
            break
        full.append(sd)

    if not full:
        return []

    if force_full_window:
        return full
    # 调度器只算 cutoff 当天
    return [sd for sd in full if sd == score_date_cutoff]


# ---------------------------------------------------------------------------
# 3. 模板评估页主表
# ---------------------------------------------------------------------------


def get_template_eval(
    days: int = 30,
    *,
    ignore_version: bool = True,
    is_backtest_filter: Optional[int] = None,
    time_dim: str = "report_date",
) -> List[Dict]:
    """聚合查询：按 prompt_id（或 prompt_id+version）算题材数 / 各日均值。

    Args:
        days: 取最近 N 个**自然日**内的样本（time_dim 决定按哪个日期字段）。
        ignore_version: True 时按 prompt_id 聚合，False 按 (prompt_id, version)。
        is_backtest_filter: None=不限，0=只看真实，1=只看回测。
        time_dim:
            * ``"report_date"``（默认，GUI 推荐）：按 ``tp.report_date`` 过滤
              ——**未打分的模板也会出现**，``d1~d5_avg`` 全 NULL
            * ``"score_date"``：按 ``tps.score_date`` 过滤——仅最近 N 天发生
              过打分的模板

    Returns:
        每行 dict（按 ``themes_total`` DESC 排序）::

            {
                "prompt_id": "speculator_scalper",
                "prompt_version": "1.0" or None,
                "themes_total": 14,              # 该模板下题材总数（含未打）
                "scored_themes": 0,              # 至少有一行 scores 的题材数
                "sample_count": 14,              # 兼容老字段 = themes_total
                "d1_avg": None, ..., "d5_avg": None,
                "a1_avg": None, ..., "a5_avg": None,   # 模板 α+N = AVG over reports of (报告 α+N)
                "alpha_avg": None,                     # 模板 α = AVG(模板 α+1..+5)（忽略 NULL）
                "alpha_avg_excl_d1": None,             # 模板 α-1 = AVG(模板 α+2..+5)（忽略 NULL）
                "hit_rate_avg": None,
                "direction_correct_rate": None,
                "last_report_date": "20260522",  # YYYYMMDD 8 字符（schema 协议）
            }

    设计要点：
        * 主表 ``theme_predictions``（LEFT JOIN scores），无打分模板也露面
        * ``themes_total`` 区别于旧版的 ``sample_count``——旧版只数有打分的，
          新版数所有题材。``sample_count`` 字段保留同值用于 GUI 老代码兼容
    """
    _validate_time_dim(time_dim)
    # 2026-05-27 18:30：report_date / score_date 在 schema 协议下统一 YYYYMMDD
    cutoff_compact = (
        datetime.now() - timedelta(days=days)
    ).strftime("%Y%m%d")

    group_cols = (
        "tp.prompt_id"
        if ignore_version
        else "tp.prompt_id, tp.prompt_version"
    )
    backtest_clause = ""
    extra_params: List = []
    if is_backtest_filter is not None:
        backtest_clause = "AND tp.is_backtest = ?"
        extra_params.append(int(is_backtest_filter))

    if time_dim == "report_date":
        time_where = "tp.report_date >= ?"
        time_params: List = [cutoff_compact]
    else:  # score_date
        time_where = (
            "EXISTS (SELECT 1 FROM theme_prediction_scores tps2 "
            "        WHERE tps2.theme_id = tp.id "
            "          AND tps2.score_date >= ?)"
        )
        time_params = [cutoff_compact]

    # 2026-05-28 22:30 α 体系金字塔重构（题材→报告→模板 严格两步聚合）：
    #   * 模板级 D+N 用 sector_pct 加权（v2 视角，与报告级故意差异化）
    #     —— 题材→报告 |strength| 加权 → 报告→模板 简单 AVG over reports
    #   * 模板级 α+N 用 theme_pct-zz1000 加权
    #     —— 题材→报告 |strength| 加权 → 报告→模板 简单 AVG over reports
    #   * 模板级 α   = AVG over 5 days of (模板 α+N)，忽略 NULL
    #   * 模板级 α-1 = AVG over D+2..D+5 of (模板 α+N)，忽略 NULL
    #   * 不再读 tps.alpha 单日字段（已 DROP COLUMN，迁移 004）
    # 4 层 CTE 结构：
    #   per_theme: 题材内透视 sector_pct（D 视角）+ theme_pct-zz1000（α 视角）
    #   per_report: 题材→报告 |strength| 加权（仅 strength>0）
    #   tpl_basics: 题材级聚合（themes_total / scored_themes / last_report_date / hit_rate / direction）
    #   tpl_aggs:   报告级聚合（D+N / α+N 简单 AVG over reports）
    #   外层 LEFT JOIN basics + aggs，再派生 alpha_avg / alpha_avg_excl_d1
    join_keys = (
        "prompt_id" if ignore_version else "prompt_id, prompt_version"
    )
    pv_select = (
        "NULL AS prompt_version" if ignore_version else "prompt_version"
    )
    sql = f"""
    WITH stock_hit AS (
        -- 2026-05-29 标的级累计命中率（命中天数 / 有效天数）
        SELECT
            theme_id,
            theme_stock_id,
            CAST(SUM(CASE WHEN is_hit IS NOT NULL THEN is_hit END) AS REAL)
              / NULLIF(SUM(CASE WHEN is_hit IS NOT NULL THEN 1 END), 0)
              AS stock_hit_rate
        FROM theme_stock_scores
        GROUP BY theme_id, theme_stock_id
    ),
    theme_hit AS (
        -- 2026-05-29 题材命中率 = 标的命中率均值
        SELECT theme_id, AVG(stock_hit_rate) AS theme_hit_rate
        FROM stock_hit
        WHERE stock_hit_rate IS NOT NULL
        GROUP BY theme_id
    ),
    theme_direction AS (
        -- 2026-05-29 题材累计方向：取最大 days_offset 行的 direction_correct
        SELECT theme_id,
               (SELECT direction_correct FROM theme_prediction_scores t2
                 WHERE t2.theme_id = theme_prediction_scores.theme_id
                 ORDER BY t2.days_offset DESC LIMIT 1) AS theme_dir_cum
        FROM theme_prediction_scores
        GROUP BY theme_id
    ),
    per_theme AS (
        SELECT
            tp.id AS theme_id,
            tp.report_path,
            tp.prompt_id,
            tp.prompt_version,
            tp.strength_score,
            tp.report_date AS theme_report_date,
            CASE WHEN COUNT(tps.id) > 0 THEN 1 ELSE 0 END AS is_scored,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.sector_pct END) AS sd1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.sector_pct END) AS sd2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.sector_pct END) AS sd3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.sector_pct END) AS sd4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.sector_pct END) AS sd5,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta5,
            MAX(th.theme_hit_rate) AS theme_hit_rate,
            MAX(td.theme_dir_cum) AS theme_dir_cum
        FROM theme_predictions tp
        LEFT JOIN theme_prediction_scores tps
               ON tps.theme_id = tp.id
        LEFT JOIN theme_hit th ON th.theme_id = tp.id
        LEFT JOIN theme_direction td ON td.theme_id = tp.id
        WHERE {time_where} {backtest_clause}
        GROUP BY tp.id, tp.prompt_id, tp.prompt_version,
                 tp.strength_score, tp.report_date, tp.report_path
    ),
    per_report AS (
        SELECT
            report_path,
            prompt_id,
            prompt_version,
            SUM(CASE WHEN strength_score > 0 AND sd1 IS NOT NULL
                     THEN sd1 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND sd1 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS rd1,
            SUM(CASE WHEN strength_score > 0 AND sd2 IS NOT NULL
                     THEN sd2 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND sd2 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS rd2,
            SUM(CASE WHEN strength_score > 0 AND sd3 IS NOT NULL
                     THEN sd3 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND sd3 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS rd3,
            SUM(CASE WHEN strength_score > 0 AND sd4 IS NOT NULL
                     THEN sd4 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND sd4 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS rd4,
            SUM(CASE WHEN strength_score > 0 AND sd5 IS NOT NULL
                     THEN sd5 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND sd5 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS rd5,
            SUM(CASE WHEN strength_score > 0 AND ta1 IS NOT NULL
                     THEN ta1 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND ta1 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS ra1,
            SUM(CASE WHEN strength_score > 0 AND ta2 IS NOT NULL
                     THEN ta2 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND ta2 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS ra2,
            SUM(CASE WHEN strength_score > 0 AND ta3 IS NOT NULL
                     THEN ta3 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND ta3 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS ra3,
            SUM(CASE WHEN strength_score > 0 AND ta4 IS NOT NULL
                     THEN ta4 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND ta4 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS ra4,
            SUM(CASE WHEN strength_score > 0 AND ta5 IS NOT NULL
                     THEN ta5 * ABS(strength_score) END)
              / NULLIF(SUM(CASE WHEN strength_score > 0 AND ta5 IS NOT NULL
                                THEN ABS(strength_score) END), 0) AS ra5,
            -- 2026-05-29 报告命中率 = 报告内 strength>0 题材命中率均值
            AVG(CASE WHEN strength_score > 0
                     THEN theme_hit_rate END) AS report_hit_rate,
            -- 2026-05-29 报告方向准确性 = 报告内 strength>0 题材累计方向均值
            AVG(CASE WHEN strength_score > 0
                     THEN CAST(theme_dir_cum AS REAL) END) AS report_dir
        FROM per_theme
        GROUP BY report_path, prompt_id, prompt_version
    ),
    tpl_basics AS (
        SELECT
            prompt_id,
            {pv_select},
            COUNT(*) AS themes_total,
            SUM(is_scored) AS scored_themes,
            MAX(theme_report_date) AS last_report_date
        FROM per_theme
        GROUP BY {join_keys}
    ),
    tpl_aggs AS (
        SELECT
            prompt_id,
            {pv_select},
            AVG(rd1) AS d1_avg, AVG(rd2) AS d2_avg, AVG(rd3) AS d3_avg,
            AVG(rd4) AS d4_avg, AVG(rd5) AS d5_avg,
            AVG(ra1) AS a1_avg, AVG(ra2) AS a2_avg, AVG(ra3) AS a3_avg,
            AVG(ra4) AS a4_avg, AVG(ra5) AS a5_avg,
            AVG(report_hit_rate) AS hit_rate_avg,
            AVG(report_dir) AS direction_correct_rate
        FROM per_report
        GROUP BY {join_keys}
    )
    SELECT
        b.prompt_id,
        b.prompt_version,
        b.themes_total,
        b.scored_themes,
        a.d1_avg, a.d2_avg, a.d3_avg, a.d4_avg, a.d5_avg,
        a.a1_avg, a.a2_avg, a.a3_avg, a.a4_avg, a.a5_avg,
        (COALESCE(a.a1_avg, 0) + COALESCE(a.a2_avg, 0)
         + COALESCE(a.a3_avg, 0) + COALESCE(a.a4_avg, 0)
         + COALESCE(a.a5_avg, 0))
          / NULLIF(
                (CASE WHEN a.a1_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a2_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a3_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a4_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a5_avg IS NOT NULL THEN 1 ELSE 0 END), 0)
          AS alpha_avg,
        (COALESCE(a.a2_avg, 0) + COALESCE(a.a3_avg, 0)
         + COALESCE(a.a4_avg, 0) + COALESCE(a.a5_avg, 0))
          / NULLIF(
                (CASE WHEN a.a2_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a3_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a4_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a.a5_avg IS NOT NULL THEN 1 ELSE 0 END), 0)
          AS alpha_avg_excl_d1,
        a.hit_rate_avg,
        a.direction_correct_rate,
        b.last_report_date
    FROM tpl_basics b
    LEFT JOIN tpl_aggs a USING ({join_keys})
    ORDER BY b.themes_total DESC
    """

    params: List = time_params + extra_params

    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()

    result: List[Dict] = []
    for r in rows:
        d = dict(r)
        # 兼容旧调用方
        d["sample_count"] = int(d.get("themes_total") or 0)
        d["scored_themes"] = int(d.get("scored_themes") or 0)
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# 4. 报告级评估（评估页下表 master-detail 的 detail）
# ---------------------------------------------------------------------------


def get_report_eval(
    days: int = 30,
    *,
    prompt_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    is_backtest_filter: Optional[int] = None,
    time_dim: str = "report_date",
) -> List[Dict]:
    """聚合查询：按 ``ai_reports.id`` 算每份报告的 D+1~D+5 均值 + 题材数。

    Args:
        days: 取最近 N 个**自然日**内的样本。
        prompt_id: 可选过滤（评估页选中某模板后下钻用）。
        prompt_version: 可选过滤（搭配 prompt_id 用，None=不限版本）。
        is_backtest_filter: None=不限，0=只看真实，1=只看回测。
        time_dim:
            * ``"report_date"``（默认 / 评估页推荐）：按 ``ar.report_date``
              过滤——**未打分的报告也会出现**，``d1~d5_avg / *_rate``
              全 NULL，``score_status='none'``
            * ``"score_date"``：按 ``tps.score_date`` 过滤——仅最近 N 天
              内**发生过打分**的报告

    Returns:
        每行 dict（按 ``report_date`` DESC, prompt_id ASC 排序）::

            {
                "report_id": 123,                # ai_reports.id（按钮回调用）
                "report_date": "20260522",       # YYYYMMDD 8 字符（schema 协议）
                "file_path": "data/AI_analysis/.../xxx.md",
                "prompt_id": "custom_6",
                "prompt_version": "1.0",
                "is_backtest": 1,
                "themes_count": 14,              # theme_predictions 实际行数
                "scored_pairs": 0,               # 已写入 scores 的 (theme,sd)
                "expected_pairs": 70,            # = themes_count * 5
                "score_status": "none",          # none / partial / full
                "d1_avg": None, ..., "d5_avg": None,
                "a1_avg": None, ..., "a5_avg": None,  # 报告 α+N = SUM(|str|·(theme_pct−zz1000))/SUM(|str|)
                "alpha_avg": None,                    # 报告 α = AVG(报告 α+1..+5)（忽略 NULL）
                "alpha_avg_excl_d1": None,            # 报告 α-1 = AVG(报告 α+2..+5)（忽略 NULL）
                "hit_rate_avg": None,
                "direction_correct_rate": None,
            }

    设计要点：
        * 主表 ``ai_reports``，LEFT JOIN theme_predictions LEFT JOIN scores，
          未抽题材或未打分的报告也会出现
        * 关联：``ar.file_path = tp.report_path``（两者均相对 posix）
        * 日期格式：``ar.report_date / tp.report_date / tps.report_date /
          tps.score_date`` 全部统一 YYYYMMDD（2026-05-27 18:30 后符合 schema 协议）
        * ``score_status``：``scored_pairs / expected_pairs``
          - 0   → "none"
          - 100% → "full"
          - 其他 → "partial"
    """
    _validate_time_dim(time_dim)
    # 2026-05-27 18:30：report_date / score_date 协议统一 YYYYMMDD
    cutoff_compact = (
        datetime.now() - timedelta(days=days)
    ).strftime("%Y%m%d")

    where_extra = ""
    extra_params: List = []
    if is_backtest_filter is not None:
        where_extra += " AND ar.is_backtest = ?"
        extra_params.append(int(is_backtest_filter))
    if prompt_id:
        where_extra += " AND ar.prompt_id = ?"
        extra_params.append(prompt_id)
    if prompt_version:
        where_extra += " AND ar.prompt_version = ?"
        extra_params.append(prompt_version)

    if time_dim == "report_date":
        # 直接按 ar.report_date 过滤，未打分报告也保留
        time_where = "ar.report_date >= ?"
        time_params: List = [cutoff_compact]
    else:  # score_date：必须有至少一条 scores 行，否则不出现
        time_where = (
            "EXISTS (SELECT 1 FROM theme_prediction_scores tps2 "
            "        JOIN theme_predictions tp2 "
            "          ON tp2.id = tps2.theme_id "
            "        WHERE tp2.report_path = ar.file_path "
            "          AND tps2.score_date >= ?)"
        )
        time_params = [cutoff_compact]

    # 2026-05-28 22:30 α 体系金字塔重构：
    #   * 报告级 α+N = 报告内按 |strength| 加权（仅 strength>0）的
    #     (theme_pct - benchmark_zz1000_pct) 逐日值
    #   * 报告级 α   = AVG over 5 days of (报告 α+N)，忽略 NULL
    #   * 报告级 α-1 = AVG over D+2..D+5 of (报告 α+N)，忽略 NULL
    #   * 不再读 tps.alpha 单日字段（已 DROP COLUMN，迁移 004）
    # 2026-05-29 命中率与方向算法重设：
    #   * 题材命中率 theme_hit_rate = 标的级累计命中率（命中天数/有效天数）均值
    #   * 题材方向 theme_dir_cum = 取最大 days_offset 行的 direction_correct
    #   * 报告命中率 hit_rate_avg = 报告内 strength>0 题材命中率均值
    #   * 报告方向 direction_correct_rate = 报告内 strength>0 题材累计方向均值
    sql = f"""
    WITH stock_hit AS (
        SELECT
            theme_id,
            theme_stock_id,
            CAST(SUM(CASE WHEN is_hit IS NOT NULL THEN is_hit END) AS REAL)
              / NULLIF(SUM(CASE WHEN is_hit IS NOT NULL THEN 1 END), 0)
              AS stock_hit_rate
        FROM theme_stock_scores
        GROUP BY theme_id, theme_stock_id
    ),
    theme_hit AS (
        SELECT theme_id, AVG(stock_hit_rate) AS theme_hit_rate
        FROM stock_hit
        WHERE stock_hit_rate IS NOT NULL
        GROUP BY theme_id
    ),
    theme_direction AS (
        SELECT theme_id,
               (SELECT direction_correct FROM theme_prediction_scores t2
                 WHERE t2.theme_id = theme_prediction_scores.theme_id
                 ORDER BY t2.days_offset DESC LIMIT 1) AS theme_dir_cum
        FROM theme_prediction_scores
        GROUP BY theme_id
    ),
    per_theme AS (
        SELECT
            tp.id AS theme_id,
            tp.report_path,
            tp.strength_score,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.theme_pct END) AS d1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.theme_pct END) AS d2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.theme_pct END) AS d3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.theme_pct END) AS d4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.theme_pct END) AS d5,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.theme_pct - tps.benchmark_zz1000_pct END) AS ta5,
            MAX(th.theme_hit_rate) AS theme_hit_rate,
            MAX(td.theme_dir_cum) AS theme_dir_cum,
            COUNT(tps.id) AS scored_pairs_one
        FROM theme_predictions tp
        LEFT JOIN theme_prediction_scores tps
               ON tps.theme_id = tp.id
        LEFT JOIN theme_hit th ON th.theme_id = tp.id
        LEFT JOIN theme_direction td ON td.theme_id = tp.id
        GROUP BY tp.id, tp.report_path, tp.strength_score
    ),
    per_report AS (
        SELECT
            ar.id AS report_id,
            ar.report_date,
            ar.file_path,
            ar.prompt_id,
            ar.prompt_version,
            ar.is_backtest,
            COALESCE(SUM(CASE WHEN per_theme.theme_id IS NOT NULL
                              THEN 1 ELSE 0 END), 0) AS themes_count,
            COALESCE(SUM(per_theme.scored_pairs_one), 0) AS scored_pairs,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.d1 IS NOT NULL
                     THEN per_theme.d1 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.d1 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS d1_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.d2 IS NOT NULL
                     THEN per_theme.d2 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.d2 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS d2_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.d3 IS NOT NULL
                     THEN per_theme.d3 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.d3 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS d3_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.d4 IS NOT NULL
                     THEN per_theme.d4 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.d4 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS d4_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.d5 IS NOT NULL
                     THEN per_theme.d5 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.d5 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS d5_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.ta1 IS NOT NULL
                     THEN per_theme.ta1 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.ta1 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS a1_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.ta2 IS NOT NULL
                     THEN per_theme.ta2 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.ta2 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS a2_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.ta3 IS NOT NULL
                     THEN per_theme.ta3 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.ta3 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS a3_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.ta4 IS NOT NULL
                     THEN per_theme.ta4 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.ta4 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS a4_avg,
            SUM(CASE WHEN per_theme.strength_score > 0
                      AND per_theme.ta5 IS NOT NULL
                     THEN per_theme.ta5 * ABS(per_theme.strength_score) END)
              / NULLIF(SUM(CASE WHEN per_theme.strength_score > 0
                                  AND per_theme.ta5 IS NOT NULL
                                THEN ABS(per_theme.strength_score) END), 0)
              AS a5_avg,
            -- 2026-05-29 报告命中率 = 报告内 strength>0 题材命中率均值
            AVG(CASE WHEN per_theme.strength_score > 0
                     THEN per_theme.theme_hit_rate END) AS hit_rate_avg,
            -- 2026-05-29 报告方向准确性 = 报告内 strength>0 题材累计方向均值
            AVG(CASE WHEN per_theme.strength_score > 0
                     THEN CAST(per_theme.theme_dir_cum AS REAL) END)
              AS direction_correct_rate
        FROM ai_reports ar
        LEFT JOIN per_theme
               ON per_theme.report_path = ar.file_path
        WHERE {time_where} {where_extra}
        GROUP BY ar.id
    )
    SELECT
        report_id, report_date, file_path,
        prompt_id, prompt_version, is_backtest,
        themes_count, scored_pairs,
        d1_avg, d2_avg, d3_avg, d4_avg, d5_avg,
        a1_avg, a2_avg, a3_avg, a4_avg, a5_avg,
        -- 报告级 α = AVG over 5 days of a_n_avg（忽略 NULL）
        (COALESCE(a1_avg, 0) + COALESCE(a2_avg, 0)
         + COALESCE(a3_avg, 0) + COALESCE(a4_avg, 0)
         + COALESCE(a5_avg, 0))
          / NULLIF(
                (CASE WHEN a1_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a2_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a3_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a4_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a5_avg IS NOT NULL THEN 1 ELSE 0 END), 0)
          AS alpha_avg,
        -- 报告级 α-1 = AVG over D+2..D+5 of a_n_avg（忽略 NULL）
        (COALESCE(a2_avg, 0) + COALESCE(a3_avg, 0)
         + COALESCE(a4_avg, 0) + COALESCE(a5_avg, 0))
          / NULLIF(
                (CASE WHEN a2_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a3_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a4_avg IS NOT NULL THEN 1 ELSE 0 END)
              + (CASE WHEN a5_avg IS NOT NULL THEN 1 ELSE 0 END), 0)
          AS alpha_avg_excl_d1,
        hit_rate_avg, direction_correct_rate
    FROM per_report
    ORDER BY report_date DESC, prompt_id ASC, report_id DESC
    """

    params: List = time_params + extra_params

    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()

    result: List[Dict] = []
    for r in rows:
        d = dict(r)
        themes_n = int(d.get("themes_count") or 0)
        scored_n = int(d.get("scored_pairs") or 0)
        expected_n = themes_n * 5
        d["expected_pairs"] = expected_n
        if expected_n == 0 or scored_n == 0:
            d["score_status"] = "none"
        elif scored_n >= expected_n:
            d["score_status"] = "full"
        else:
            d["score_status"] = "partial"
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# 5. 单题材打分明细
# ---------------------------------------------------------------------------


def rescore_one_report(
    report_id: int,
    *,
    days_back: int = 5,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
    score_date_override: Optional[str] = None,
) -> BatchScoringResult:
    """按 ``ai_reports.id`` 单报告打分（评估页打分按钮入口）。

    与 :func:`rescore_range` 区别：
        * rescore_range 按 ``report_date`` 区间扫**所有模板**的题材
        * rescore_one_report 只动**该 report_id 对应 ai_reports 行**下属
          theme_predictions（按 ``report_path = ar.file_path`` 关联），
          其他报告完全不波及

    流程：
        1. 查 ``ai_reports`` 拿到 ``report_date`` / ``file_path``
        2. 查所有 ``theme_predictions WHERE report_path = file_path``
        3. 对每个 theme 跑 D+1~D+days_back（force_full_window=True）
        4. 返回 :class:`BatchScoringResult`

    Args:
        report_id: ``ai_reports.id``
        days_back: 默认 5 = D+1~D+5
        score_date_override: 覆盖 cutoff，默认 = 今天

    Raises:
        ValueError: report_id 不存在
    """
    t0 = time.perf_counter()
    cutoff = (
        score_date_override
        or datetime.now().strftime("%Y%m%d")
    )

    ai = get_ai_inference_db()
    ai.ensure_schema()

    with ai.connect(readonly=True) as conn:
        ar_row = conn.execute(
            "SELECT id, report_date, file_path "
            "FROM ai_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if ar_row is None:
            raise ValueError(f"ai_reports.id={report_id} 不存在")
        report_date = str(ar_row["report_date"])
        file_path = str(ar_row["file_path"])
        themes = conn.execute(
            "SELECT id, report_date FROM theme_predictions "
            "WHERE report_path = ? ORDER BY id ASC",
            (file_path,),
        ).fetchall()

    themes_total = len(themes)
    if themes_total == 0:
        return BatchScoringResult(
            ok=True, themes_total=0, themes_scored=0,
            pairs_attempted=0, pairs_succeeded=0, days_covered=0,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            errors=[
                f"report_id={report_id} 无题材可打分 "
                f"(theme_predictions WHERE report_path={file_path} 为空)"
            ],
        )

    pairs_attempted = 0
    pairs_succeeded = 0
    days_covered_set: set[str] = set()
    errors: List[str] = []
    scored_themes: set[int] = set()

    market_db = get_market_db()
    market_db.ensure_schema()

    for row in themes:
        theme_id = int(row["id"])
        theme_report_date = str(row["report_date"])
        try:
            score_dates = _eligible_score_dates(
                report_date=theme_report_date,
                score_date_cutoff=cutoff,
                days_back=days_back,
                force_full_window=True,
            )
        except TradeDateError as exc:
            errors.append(
                f"theme={theme_id} 解析交易日窗口失败: {exc}"
            )
            continue

        if not score_dates:
            continue

        for sd in score_dates:
            pairs_attempted += 1
            try:
                score_theme_on_date(
                    theme_id, sd,
                    hit_threshold_pct=hit_threshold_pct,
                    benchmark_ts_code=benchmark_ts_code,
                    write=True,
                )
                pairs_succeeded += 1
                days_covered_set.add(sd)
                scored_themes.add(theme_id)
            except SkippedTheme:
                pairs_attempted -= 1
                continue
            except (ScoringError, ValueError) as exc:
                errors.append(
                    f"theme={theme_id} score_date={sd}: {exc}"
                )

    _log.info(
        "rescore_one_report id=%s date=%s themes=%d "
        "scored=%d pairs=%d/%d days=%d",
        report_id, report_date, themes_total, len(scored_themes),
        pairs_succeeded, pairs_attempted, len(days_covered_set),
    )

    return BatchScoringResult(
        ok=(len(errors) == 0),
        themes_total=themes_total,
        themes_scored=len(scored_themes),
        pairs_attempted=pairs_attempted,
        pairs_succeeded=pairs_succeeded,
        days_covered=len(days_covered_set),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        errors=errors,
    )


def rescore_unfinished_reports(
    *,
    days: int = 30,
    time_dim: str = "report_date",
    is_backtest_filter: Optional[int] = None,
    days_back: int = 5,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
    score_date_override: Optional[str] = None,
) -> BatchUnfinishedResult:
    """批量给「未打完」的报告补齐 D+1~D+days_back 打分。

    业务定位：
        评估页「⚡ 一键打分未完成」按钮 / 调度补齐 CLI 共用入口。
        与 :func:`rescore_range` 的区别——区间会无差别覆盖**所有**
        题材（包括已 ``full`` 的报告），本函数只挑当前筛选范围内
        ``score_status ∈ {none, partial}`` 且 ``themes_count > 0``
        的报告，逐个调 :func:`rescore_one_report` 增量补齐。

    Args:
        days / time_dim / is_backtest_filter:
            与 :func:`get_report_eval` 同名参数完全等价，决定"哪些
            报告会被纳入候选"。
        days_back: 每个报告回算几天 D+N（默认 5）。
        hit_threshold_pct / benchmark_ts_code / score_date_override:
            透传给 :func:`rescore_one_report`。

    Returns:
        :class:`BatchUnfinishedResult`：themes_* / pairs_* 是跨报告
        的合计值；reports_targeted / reports_done / reports_skipped
        对应"命中候选 / 实际打分成功 / 跳过（无题材或调用异常）"。

    设计要点：
        * 候选名单一次性算完（避免长事务期间数据漂移）
        * 单报告失败不阻断后续：异常计入 errors，继续下一个
        * 已 ``full`` 的报告完全不动，不浪费市场 API 配额
    """
    t0 = time.perf_counter()
    candidates = get_report_eval(
        days=days,
        prompt_id=None,
        is_backtest_filter=is_backtest_filter,
        time_dim=time_dim,
    )
    targets = [
        r for r in candidates
        if (r.get("score_status") in ("none", "partial"))
        and int(r.get("themes_count") or 0) > 0
    ]
    reports_targeted = len(targets)

    themes_total_acc = 0
    themes_scored_acc = 0
    pairs_attempted_acc = 0
    pairs_succeeded_acc = 0
    days_covered_acc = 0  # 简单累加（跨报告允许重复天数）
    errors_acc: List[str] = []
    reports_done = 0
    reports_skipped = 0

    for r in targets:
        rid = int(r.get("report_id") or 0)
        if rid <= 0:
            reports_skipped += 1
            continue
        try:
            sub = rescore_one_report(
                rid,
                days_back=days_back,
                hit_threshold_pct=hit_threshold_pct,
                benchmark_ts_code=benchmark_ts_code,
                score_date_override=score_date_override,
            )
        except (ValueError, ScoringError) as exc:
            reports_skipped += 1
            errors_acc.append(f"report_id={rid}: {exc}")
            continue

        themes_total_acc += sub.themes_total
        themes_scored_acc += sub.themes_scored
        pairs_attempted_acc += sub.pairs_attempted
        pairs_succeeded_acc += sub.pairs_succeeded
        days_covered_acc += sub.days_covered
        if sub.errors:
            errors_acc.extend(sub.errors)
        if sub.themes_scored > 0:
            reports_done += 1
        else:
            reports_skipped += 1

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    _log.info(
        "rescore_unfinished_reports targeted=%d done=%d skipped=%d "
        "themes=%d pairs=%d/%d elapsed=%dms",
        reports_targeted, reports_done, reports_skipped,
        themes_scored_acc, pairs_succeeded_acc, pairs_attempted_acc,
        elapsed_ms,
    )

    return BatchUnfinishedResult(
        ok=(len(errors_acc) == 0),
        themes_total=themes_total_acc,
        themes_scored=themes_scored_acc,
        pairs_attempted=pairs_attempted_acc,
        pairs_succeeded=pairs_succeeded_acc,
        days_covered=days_covered_acc,
        elapsed_ms=elapsed_ms,
        errors=errors_acc,
        reports_targeted=reports_targeted,
        reports_done=reports_done,
        reports_skipped=reports_skipped,
    )


def get_theme_score_detail(theme_id: int) -> List[Dict]:
    """返回单题材 D+1~D+5 逐日打分行（升序）。"""
    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM theme_prediction_scores "
            "WHERE theme_id = ? "
            "ORDER BY score_date ASC, days_offset ASC",
            (theme_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 6. 评估页树形第 2 层：一份报告下每个题材的聚合打分
# ---------------------------------------------------------------------------


def get_theme_eval_for_report(report_id: int) -> List[Dict]:
    """按 ``ai_reports.id`` 取该报告下每个题材的聚合打分。

    业务定位：
        评估页 QTreeWidget 第 2 层（点击 📄 报告 ▶ 展开后调）。
        每行 1 个题材，已按 D+1~D+5 透视 + 跨 score_date 聚合，
        与 :func:`get_report_eval` 的均值口径一致但更细粒度。

    Args:
        report_id: ``ai_reports.id``。不存在则返回空列表（不抛错）。

    Returns:
        每行 dict（按 theme_id ASC 排序）::

            {
                "theme_id": 999,
                "theme_name": "人形机器人",
                "theme_category": "硬科技",
                "strength_score": 75,        # -100~+100
                "strength_level": "较强利多",
                "priority_rank": 1,
                "duration": "中线",
                "expectation_gap": "高",
                "is_cold": 0,
                "sector_ts_code": "BK0871.DC",
                "stocks_count": 6,           # theme_stocks 行数（懒加载下一层用）
                "scored_pairs": 5,           # theme_prediction_scores 行数
                "d1": ..., "d5": ...,        # sector_pct 逐日透视
                "a1": ..., "a5": ...,        # α+N = sector_pct - 中证1000 逐日
                "alpha_avg": None,           # 题材 α = AVG(α+1..+5)（sector-zz1000）
                "alpha_avg_excl_d1": None,   # 题材 α-1 = AVG(α+2..+5)（sector-zz1000）
                "hit_rate_avg": None,
                "direction_correct_rate": None,
                "sector_pct_avg": None,      # 板块涨跌幅均值
            }

    SQL 性能：
        命中 ``theme_predictions(report_path)`` 索引 + 子查询 GROUP BY
        ``theme_id``，单报告（5~20 题材）耗时 < 50ms。
    """
    # 2026-05-29 命中率与方向算法重设：
    #   * D+N = sector_pct（保持 v2）
    #   * α+N = sector_pct - benchmark_zz1000_pct（与 D+N 同基础）
    #   * α   = AVG over 5 days of α+N
    #   * α-1 = AVG over D+2..D+5 of α+N
    #   * **hit_rate_avg**：标的级累计命中率（命中天数/有效天数）跨题材均值
    #   * **direction_correct_rate**：取最大 days_offset 行的 direction_correct
    #     （累乘累计判定 1/0），无打分行时 NULL
    #   * WHERE tp.strength_score > 0 过滤利空/中性题材
    #   * ORDER BY strength DESC, priority_rank ASC NULLS LAST
    sql = """
    WITH per_theme_scores AS (
        SELECT
            tps.theme_id,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.sector_pct END) AS d1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.sector_pct END) AS d2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.sector_pct END) AS d3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.sector_pct END) AS d4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.sector_pct END) AS d5,
            MAX(CASE WHEN tps.days_offset=1
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END) AS a1,
            MAX(CASE WHEN tps.days_offset=2
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END) AS a2,
            MAX(CASE WHEN tps.days_offset=3
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END) AS a3,
            MAX(CASE WHEN tps.days_offset=4
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END) AS a4,
            MAX(CASE WHEN tps.days_offset=5
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END) AS a5,
            AVG(CASE WHEN tps.days_offset BETWEEN 1 AND 5
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END)
                 AS alpha_avg,
            AVG(CASE WHEN tps.days_offset BETWEEN 2 AND 5
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END)
                 AS alpha_avg_excl_d1,
            -- 题材级方向：取最大 days_offset 行的 direction_correct
            -- （累乘累计判定，演进最后一帧即题材最终判定）
            (SELECT direction_correct FROM theme_prediction_scores t2
              WHERE t2.theme_id = tps.theme_id
              ORDER BY t2.days_offset DESC LIMIT 1) AS direction_cumulative,
            AVG(tps.sector_pct) AS sector_pct_avg,
            COUNT(tps.id) AS scored_pairs
        FROM theme_prediction_scores tps
        GROUP BY tps.theme_id
    ),
    stock_count AS (
        SELECT theme_id, COUNT(*) AS n
        FROM theme_stocks
        GROUP BY theme_id
    ),
    -- 2026-05-29 标的级累计命中率（命中天数/有效天数）→ 题材内均值
    per_theme_hit_rate AS (
        SELECT
            theme_id,
            AVG(stock_hit_rate) AS hit_rate_avg
        FROM (
            SELECT
                theme_id,
                theme_stock_id,
                CAST(SUM(CASE WHEN is_hit IS NOT NULL THEN is_hit END)
                     AS REAL)
                  / NULLIF(SUM(CASE WHEN is_hit IS NOT NULL THEN 1 END), 0)
                  AS stock_hit_rate
            FROM theme_stock_scores
            GROUP BY theme_id, theme_stock_id
        )
        WHERE stock_hit_rate IS NOT NULL
        GROUP BY theme_id
    )
    SELECT
        tp.id AS theme_id,
        tp.theme_name,
        tp.theme_category,
        tp.strength_score,
        tp.strength_level,
        tp.priority_rank,
        tp.duration,
        tp.expectation_gap,
        tp.is_cold,
        tp.sector_ts_code,
        tp.sector_match_conf,
        COALESCE(sc.n, 0) AS stocks_count,
        COALESCE(pts.scored_pairs, 0) AS scored_pairs,
        pts.d1, pts.d2, pts.d3, pts.d4, pts.d5,
        pts.a1, pts.a2, pts.a3, pts.a4, pts.a5,
        pts.alpha_avg,
        pts.alpha_avg_excl_d1,
        phr.hit_rate_avg,
        pts.direction_cumulative AS direction_correct_rate,
        pts.sector_pct_avg
    FROM ai_reports ar
    INNER JOIN theme_predictions tp
            ON tp.report_path = ar.file_path
    LEFT JOIN per_theme_scores pts ON pts.theme_id = tp.id
    LEFT JOIN per_theme_hit_rate phr ON phr.theme_id = tp.id
    LEFT JOIN stock_count       sc  ON sc.theme_id = tp.id
    WHERE ar.id = ?
      AND tp.strength_score > 0
    ORDER BY tp.strength_score DESC,
             CASE WHEN tp.priority_rank IS NULL THEN 1 ELSE 0 END,
             tp.priority_rank ASC,
             tp.id ASC
    """

    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(sql, (int(report_id),)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 6B. 题材预测页筛选用：按 theme_ids 批量取聚合打分
# ---------------------------------------------------------------------------


def get_score_aggregates_by_theme_ids(
    theme_ids: List[int],
) -> Dict[int, Dict]:
    """按题材 id 列表批量取聚合打分（命中率 / 累计方向 / α / 已打分天数）。

    业务定位：
        题材预测页（v3）筛选条 "命中率下限 / 方向准确性 / α" 需要在主表
        渲染前一次性取回所有题材的聚合打分，本函数提供。

    实现要点：
        * 复用 :func:`get_theme_eval_for_report` 的 ``per_theme_scores`` /
          ``per_theme_hit_rate`` 两个 CTE 结构
        * 与 ``get_theme_eval_for_report`` 的差异：
          - 不 JOIN ``theme_predictions`` / ``ai_reports``（主数据已在 GUI 手里）
          - 不强制 ``strength_score > 0`` 过滤（让 GUI 自己决定显不显示利空）
          - 入参是 ID 列表（IN 查询），不是 ai_reports.id 单 ID
        * 返回字典，未打分的题材**不放进字典**（上层据此判定 None）

    Args:
        theme_ids: ``theme_predictions.id`` 列表（int），允许空 / 含重复

    Returns:
        ``{theme_id: {"hit_rate_avg": float, "direction_correct": int,
        "alpha_avg": float, "scored_pairs": int}}``

    SQL 性能:
        单次 IN 查询 + 两个 CTE，百级 theme_ids < 50ms。
    """
    cleaned = sorted({int(t) for t in theme_ids if t is not None})
    if not cleaned:
        return {}

    placeholders = ",".join("?" * len(cleaned))
    sql = f"""
    WITH per_theme_scores AS (
        SELECT
            tps.theme_id,
            (SELECT direction_correct FROM theme_prediction_scores t2
              WHERE t2.theme_id = tps.theme_id
              ORDER BY t2.days_offset DESC LIMIT 1) AS direction_cumulative,
            AVG(CASE WHEN tps.days_offset BETWEEN 1 AND 5
                     THEN tps.sector_pct - tps.benchmark_zz1000_pct END)
                AS alpha_avg,
            COUNT(tps.id) AS scored_pairs
        FROM theme_prediction_scores tps
        WHERE tps.theme_id IN ({placeholders})
        GROUP BY tps.theme_id
    ),
    per_theme_hit_rate AS (
        SELECT
            theme_id,
            AVG(stock_hit_rate) AS hit_rate_avg
        FROM (
            SELECT
                theme_id,
                theme_stock_id,
                CAST(SUM(CASE WHEN is_hit IS NOT NULL THEN is_hit END)
                     AS REAL)
                  / NULLIF(SUM(CASE WHEN is_hit IS NOT NULL THEN 1 END), 0)
                  AS stock_hit_rate
            FROM theme_stock_scores
            WHERE theme_id IN ({placeholders})
            GROUP BY theme_id, theme_stock_id
        )
        WHERE stock_hit_rate IS NOT NULL
        GROUP BY theme_id
    )
    SELECT
        pts.theme_id,
        phr.hit_rate_avg,
        pts.direction_cumulative AS direction_correct,
        pts.alpha_avg,
        pts.scored_pairs
    FROM per_theme_scores pts
    LEFT JOIN per_theme_hit_rate phr ON phr.theme_id = pts.theme_id
    """

    params = list(cleaned) + list(cleaned)  # 两处 IN 占位
    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {int(r["theme_id"]): dict(r) for r in rows}


# ---------------------------------------------------------------------------
# 7. 评估页树形第 3 层：一个题材下每只标的的逐日涨跌
# ---------------------------------------------------------------------------


def get_stock_scores_for_theme(theme_id: int) -> List[Dict]:
    """按 ``theme_id`` 取该题材下每只标的的逐日涨跌 + 命中标记。

    业务定位：
        评估页 QTreeWidget 第 3 层（点击 🎯 题材 ▶ 展开后调）。
        每行 1 只标的，已按 D+1~D+5 透视。

    Args:
        theme_id: ``theme_predictions.id``。不存在则返回空列表（不抛错）。

    Returns:
        每行 dict（按 theme_stock_id ASC 排序）::

            {
                "theme_stock_id": 1234,
                "stock_name": "XX龙头",
                "stock_code": "600172",        # AI 原始
                "normalized_code": "600172.SH", # matcher 后
                "role": "核心",                  # 核心 / 辐射 / 受益 / ...
                "reason": "AI 给的理由",
                "d1_pct": 5.2, ..., "d5_pct": ...,    # 涨跌幅 %
                "a1_pct": ..., "a5_pct": ...,         # α+N：pct_chg - 中证1000 当日 pct
                "d1_hit": 1, ..., "d5_hit": 0,        # 1=命中, 0=未命中, None=未打分
                "scored_days": 3,              # theme_stock_scores 行数
            }

    边界情况：
        * 题材无任何 theme_stocks → 返回空列表
        * 题材有 stocks 但没打过分 → 返回 stocks 但 d*_pct / d*_hit 全 None
        * scored_days=0 标识"未打分"，GUI 据此显示空态

    SQL 性能：
        命中 ``theme_stocks(theme_id)`` + ``theme_stock_scores(theme_id)``
        索引，单题材（3~8 只）耗时 < 30ms。
    """
    # 2026-05-28 22:00 加入 a_n_pct（pct_chg - 中证1000 当日 pct）
    # 标的本身没存 zz1000，借同一 theme_id+score_date 的 theme_prediction_scores
    # 行 join 取 benchmark_zz1000_pct
    # 2026-05-29 加入 stock_hit_rate / valid_days：标的级累计命中率
    sql = """
    WITH per_stock_pivot AS (
        SELECT
            tss.theme_stock_id,
            MAX(CASE WHEN tss.days_offset=1
                     THEN tss.pct_chg END) AS d1_pct,
            MAX(CASE WHEN tss.days_offset=2
                     THEN tss.pct_chg END) AS d2_pct,
            MAX(CASE WHEN tss.days_offset=3
                     THEN tss.pct_chg END) AS d3_pct,
            MAX(CASE WHEN tss.days_offset=4
                     THEN tss.pct_chg END) AS d4_pct,
            MAX(CASE WHEN tss.days_offset=5
                     THEN tss.pct_chg END) AS d5_pct,
            MAX(CASE WHEN tss.days_offset=1
                     THEN tss.pct_chg - tps.benchmark_zz1000_pct END) AS a1_pct,
            MAX(CASE WHEN tss.days_offset=2
                     THEN tss.pct_chg - tps.benchmark_zz1000_pct END) AS a2_pct,
            MAX(CASE WHEN tss.days_offset=3
                     THEN tss.pct_chg - tps.benchmark_zz1000_pct END) AS a3_pct,
            MAX(CASE WHEN tss.days_offset=4
                     THEN tss.pct_chg - tps.benchmark_zz1000_pct END) AS a4_pct,
            MAX(CASE WHEN tss.days_offset=5
                     THEN tss.pct_chg - tps.benchmark_zz1000_pct END) AS a5_pct,
            MAX(CASE WHEN tss.days_offset=1
                     THEN tss.is_hit END) AS d1_hit,
            MAX(CASE WHEN tss.days_offset=2
                     THEN tss.is_hit END) AS d2_hit,
            MAX(CASE WHEN tss.days_offset=3
                     THEN tss.is_hit END) AS d3_hit,
            MAX(CASE WHEN tss.days_offset=4
                     THEN tss.is_hit END) AS d4_hit,
            MAX(CASE WHEN tss.days_offset=5
                     THEN tss.is_hit END) AS d5_hit,
            COUNT(tss.id) AS scored_days,
            -- 2026-05-29 标的级累计命中率（命中天数 / 有效天数）
            SUM(CASE WHEN tss.is_hit IS NOT NULL THEN 1 END) AS valid_days,
            CAST(SUM(CASE WHEN tss.is_hit IS NOT NULL THEN tss.is_hit END)
                 AS REAL)
              / NULLIF(SUM(CASE WHEN tss.is_hit IS NOT NULL THEN 1 END), 0)
              AS stock_hit_rate
        FROM theme_stock_scores tss
        LEFT JOIN theme_prediction_scores tps
               ON tps.theme_id = tss.theme_id
              AND tps.score_date = tss.score_date
        WHERE tss.theme_id = ?
        GROUP BY tss.theme_stock_id
    )
    SELECT
        ts.id AS theme_stock_id,
        ts.stock_name,
        ts.stock_code,
        ts.normalized_code,
        ts.role,
        ts.reason,
        psp.d1_pct, psp.d2_pct, psp.d3_pct, psp.d4_pct, psp.d5_pct,
        psp.a1_pct, psp.a2_pct, psp.a3_pct, psp.a4_pct, psp.a5_pct,
        psp.d1_hit, psp.d2_hit, psp.d3_hit, psp.d4_hit, psp.d5_hit,
        COALESCE(psp.scored_days, 0) AS scored_days,
        COALESCE(psp.valid_days, 0) AS valid_days,
        psp.stock_hit_rate
    FROM theme_stocks ts
    LEFT JOIN per_stock_pivot psp ON psp.theme_stock_id = ts.id
    WHERE ts.theme_id = ?
    ORDER BY ts.id ASC
    """

    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(
            sql, (int(theme_id), int(theme_id))
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


_ALLOWED_TIME_DIMS = ("score_date", "report_date")


@dataclass
class PurgeResult:
    """:func:`purge_scoring_data` 的返回结构。

    Attributes:
        rows_main: 待删 / 已删的 ``theme_prediction_scores`` 行数。
        rows_detail: 待删 / 已删的 ``theme_stock_scores`` 行数。
        backup_path: 真删模式下创建的备份文件路径（dry_run=True 时为 None）。
        dry_run: True 表示只统计未实际删除。
    """

    rows_main: int
    rows_detail: int
    backup_path: Optional[str]
    dry_run: bool


def purge_scoring_data(*, dry_run: bool = False) -> PurgeResult:
    """整库清空 ``theme_prediction_scores`` + ``theme_stock_scores``。

    用于 2026-05-29 打分算法重设上线：旧口径打分行必须全部清空避免
    新旧口径混算。本函数不动 ``theme_predictions`` / ``ai_reports`` /
    ``theme_stocks`` 三张表——题材抽取数据完好保留，调度器跑下次
    打分时按新口径自动重写。

    Args:
        dry_run: True 时只统计待删行数，不实际删除；False 时先备份再删。

    Returns:
        :class:`PurgeResult`。

    Raises:
        AIInferenceDBError: 备份失败或 DDL 异常。
    """
    ai = get_ai_inference_db()
    ai.ensure_schema()
    # 1. 先统计
    with ai.connect(readonly=True) as conn:
        n_main = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_prediction_scores"
        ).fetchone()["c"]
        n_detail = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stock_scores"
        ).fetchone()["c"]

    if dry_run:
        return PurgeResult(
            rows_main=int(n_main),
            rows_detail=int(n_detail),
            backup_path=None,
            dry_run=True,
        )

    # 2. 备份再删除
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    backup_dir = _Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    backup_path = (
        backup_dir / f"ai_inference.db.purge_scoring.{stamp}.bak"
    )
    ai.backup_to(backup_path)

    with ai.connect() as conn:
        # 子表先删（虽有 CASCADE，但显式更可控）
        conn.execute("DELETE FROM theme_stock_scores")
        conn.execute("DELETE FROM theme_prediction_scores")

    _log.warning(
        "purge_scoring_data 已清空 theme_prediction_scores=%d 行 + "
        "theme_stock_scores=%d 行，备份 -> %s",
        n_main, n_detail, backup_path,
    )

    return PurgeResult(
        rows_main=int(n_main),
        rows_detail=int(n_detail),
        backup_path=str(backup_path),
        dry_run=False,
    )


def _validate_time_dim(time_dim: str) -> None:
    if time_dim not in _ALLOWED_TIME_DIMS:
        raise ValueError(
            f"time_dim 必须是 {_ALLOWED_TIME_DIMS}，得到 {time_dim!r}"
        )


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()


def _ensure_yyyymmdd(s: str) -> None:
    if not _is_yyyymmdd(s):
        raise ValueError(f"日期格式应为 YYYYMMDD，得到 {s!r}")
