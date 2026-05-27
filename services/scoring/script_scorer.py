"""脚本规则打分核心（Phase 2 Step 2.1）。

业务定位
--------
对每个题材在 D+1~D+5 的每一交易日跑一次脚本打分，落两张事实表：

* ``theme_prediction_scores`` ：单题材单日 1 行（6 个核心指标 + AI 复审栏）
* ``theme_stock_scores``      ：单标的单日 1 行（明细，给题材详情 Tab 用）

6 个核心指标
------------
| 字段 | 含义 |
|------|------|
| ``sector_pct`` | 题材对应板块当日涨跌幅（%） |
| ``stock_avg_pct`` | 标的算术平均涨幅（%） |
| ``stock_weighted_pct`` | 一期等权 = 算术平均；二期可按强度加权 |
| ``hit_rate`` | 涨幅 ≥ ``hit_threshold_pct`` 的标的占比 |
| ``alpha`` | ``stock_weighted_pct - benchmark_pct`` |
| ``direction_correct`` | 强度方向与板块涨跌方向同号则 1，反向 0，无法判定 NULL |

防穿越约束
----------
* ``score_date`` 必须 **严格大于** ``report_date``，否则 raise ``ValueError``
* 跨节假日由 :func:`trade_dates_between` 解析，不手算

数据源（v3 三库版）
------------------
* ai_inference.theme_predictions / theme_stocks    主库
* market.fact_stock_daily                          标的涨跌
* market.fact_sector_daily                         板块涨跌（字段名 ``ts_code``，
  不是设计文档过时的 ``sector_code``）
* market.fact_index_daily                          benchmark（设计文档过时地
  指向 fact_stock_daily，但 daily 接口只返指数不返指数；实测 ``000001.SH``
  在 fact_index_daily）

跨库 JOIN
----------
全部走 :func:`services.storage.cross_db.attached_dbs`，禁止裸 ATTACH。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from services.market.trade_date import trade_dates_between
from services.storage.cross_db import attached_dbs

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 标的命中阈值，默认涨幅 ≥ 3% 视为命中
DEFAULT_HIT_THRESHOLD_PCT = 3.0

#: benchmark 指数代码（上证综指）
DEFAULT_BENCHMARK_TS_CODE = "000001.SH"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ThemeDailyScore:
    """单题材单日打分结果。

    所有 ``Optional[float]`` 字段 None 表示当日缺数据（节假日 / 标的退市
    / 板块代码空 / benchmark 未同步等），不应被聚合为 0。
    """

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

    benchmark_pct: Optional[float]
    alpha: Optional[float]
    direction_correct: Optional[int]

    #: 每只标的明细 (theme_stock_id, normalized_code, pct_chg, is_hit)
    stock_rows: List[Tuple[int, str, Optional[float], Optional[int]]]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ScoringError(RuntimeError):
    """打分流程不可恢复错误（题材不存在、穿越等）。"""


# ---------------------------------------------------------------------------
# 单题材单日打分
# ---------------------------------------------------------------------------


def score_theme_on_date(
    theme_id: int,
    score_date: str,
    *,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
    write: bool = True,
) -> ThemeDailyScore:
    """对 ``theme_id`` 这个题材在 ``score_date`` 这一天打分。

    Args:
        theme_id: ``ai_inference.theme_predictions.id``。
        score_date: ``YYYYMMDD``，必须严格 > ``theme.report_date``。
        hit_threshold_pct: 标的"命中"阈值，默认 3%。
        benchmark_ts_code: 大盘指数，默认上证综指 ``000001.SH``
            （注意源表 ``fact_index_daily``，不是 ``fact_stock_daily``）。
        write: True（默认）写库；False 仅计算返回（dry-run）。

    Returns:
        :class:`ThemeDailyScore`。

    Raises:
        ScoringError: 题材不存在 / score_date <= report_date。
        ValueError: score_date 格式非法。
    """
    _ensure_yyyymmdd(score_date)

    with attached_dbs(primary="ai", attach=("market",)) as conn:
        theme = _load_theme(conn, theme_id)
        report_date = theme["report_date"]

        if score_date <= report_date:
            raise ScoringError(
                f"穿越保护：score_date={score_date} 必须严格晚于 "
                f"report_date={report_date}"
            )

        # 计算 days_offset = trade_dates_between(report_date, score_date) 数
        # 这里 trade_dates_between 内部已经会查/补 dim_trade_calendar
        # （score_date 必须是开市日，否则 days_offset 不准）
        trade_window = trade_dates_between(report_date, score_date)
        if score_date not in trade_window:
            raise ScoringError(
                f"score_date={score_date} 不是开市日 "
                f"(report_date={report_date} → 区间 {trade_window})"
            )
        # 区间 = [report_date, score_date]；
        #  - 若 report_date 本身开市，则 days_offset = len - 1
        #  - 若 report_date 是周末/节假日，days_offset = len（第 1 天就是
        #    第 1 个开市日 = D+1）
        if report_date in trade_window:
            days_offset = len(trade_window) - 1
        else:
            days_offset = len(trade_window)

        # 标的 JOIN 当日涨跌
        stock_rows = conn.execute(
            "SELECT ts.id AS theme_stock_id, "
            "       ts.normalized_code, "
            "       fsd.pct_chg "
            "FROM theme_stocks ts "
            "LEFT JOIN market.fact_stock_daily fsd "
            "    ON fsd.ts_code = ts.normalized_code "
            "    AND fsd.trade_date = ? "
            "WHERE ts.theme_id = ? "
            "  AND ts.normalized_code IS NOT NULL "
            "  AND ts.normalized_code != ''",
            (score_date, theme_id),
        ).fetchall()

        # 6 指标
        pcts = [
            float(r["pct_chg"]) for r in stock_rows
            if r["pct_chg"] is not None
        ]
        total_count = len(stock_rows)
        stock_avg_pct = (sum(pcts) / len(pcts)) if pcts else None
        # 一期等权 = 算术平均；二期可按强度分加权
        stock_weighted_pct = stock_avg_pct
        hit_count = sum(1 for p in pcts if p >= hit_threshold_pct)
        hit_rate = (hit_count / total_count) if total_count > 0 else 0.0

        sector_pct = _query_sector_pct(
            conn, theme["sector_ts_code"], score_date,
        )
        benchmark_pct = _query_benchmark_pct(
            conn, benchmark_ts_code, score_date,
        )

        if stock_weighted_pct is not None and benchmark_pct is not None:
            alpha: Optional[float] = stock_weighted_pct - benchmark_pct
        else:
            alpha = None

        direction_correct = _compute_direction(
            theme["strength_score"], sector_pct,
        )

        # 明细行（写 theme_stock_scores 用）
        stock_score_rows: List[
            Tuple[int, str, Optional[float], Optional[int]]
        ] = []
        for r in stock_rows:
            pct = r["pct_chg"]
            is_hit: Optional[int]
            if pct is None:
                is_hit = None
            else:
                is_hit = 1 if float(pct) >= hit_threshold_pct else 0
            stock_score_rows.append(
                (
                    int(r["theme_stock_id"]),
                    str(r["normalized_code"]),
                    float(pct) if pct is not None else None,
                    is_hit,
                )
            )

        result = ThemeDailyScore(
            theme_id=theme_id,
            prompt_id=theme["prompt_id"],
            prompt_version=theme["prompt_version"],
            report_date=report_date,
            score_date=score_date,
            days_offset=days_offset,
            sector_pct=sector_pct,
            stock_avg_pct=stock_avg_pct,
            stock_weighted_pct=stock_weighted_pct,
            hit_count=hit_count,
            total_count=total_count,
            hit_rate=hit_rate,
            benchmark_pct=benchmark_pct,
            alpha=alpha,
            direction_correct=direction_correct,
            stock_rows=stock_score_rows,
        )

        if write:
            _write_scores(conn, result)

    return result


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------


@dataclass
class BatchScoreResult:
    """批量打分聚合结果。"""

    ok: bool
    themes_scored: int
    rows_written: int
    days_covered: int
    elapsed_ms: int
    errors: List[str]


def score_themes_batch(
    theme_ids: List[int],
    score_dates: List[str],
    *,
    hit_threshold_pct: float = DEFAULT_HIT_THRESHOLD_PCT,
    benchmark_ts_code: str = DEFAULT_BENCHMARK_TS_CODE,
    write: bool = True,
) -> BatchScoreResult:
    """笛卡尔积式批量打分：``theme_ids`` × ``score_dates`` 全部跑一次。

    内部按 (theme, date) 顺序串行，每对触发一次 attached_dbs。
    后续 Step 2.2 ``scoring_service`` 会在更高层做日期/题材集合的智能裁剪。

    Args:
        theme_ids: 题材主键列表。
        score_dates: 打分日列表（YYYYMMDD），必须是开市日。
        write: False = dry-run。
    """
    import time

    t0 = time.perf_counter()
    rows_written = 0
    errors: List[str] = []
    scored_pairs = 0

    for tid in theme_ids:
        for sd in score_dates:
            try:
                res = score_theme_on_date(
                    tid, sd,
                    hit_threshold_pct=hit_threshold_pct,
                    benchmark_ts_code=benchmark_ts_code,
                    write=write,
                )
                scored_pairs += 1
                rows_written += 1 + len(res.stock_rows)  # 主表 1 行 + 明细 N
            except (ScoringError, ValueError) as exc:
                errors.append(f"theme={tid} score_date={sd}: {exc}")

    return BatchScoreResult(
        ok=(len(errors) == 0),
        themes_scored=scored_pairs,
        rows_written=rows_written,
        days_covered=len(score_dates),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _load_theme(conn: sqlite3.Connection, theme_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, report_date, prompt_id, prompt_version, "
        "       sector_ts_code, strength_score "
        "FROM theme_predictions WHERE id = ?",
        (theme_id,),
    ).fetchone()
    if row is None:
        raise ScoringError(f"theme_id={theme_id} 不存在")
    return row


def _query_sector_pct(
    conn: sqlite3.Connection,
    sector_ts_code: Optional[str],
    score_date: str,
) -> Optional[float]:
    """查板块当日涨跌幅（fact_sector_daily 字段名为 ts_code）。"""
    if not sector_ts_code:
        return None
    row = conn.execute(
        "SELECT pct_chg FROM market.fact_sector_daily "
        "WHERE ts_code = ? AND trade_date = ?",
        (sector_ts_code, score_date),
    ).fetchone()
    if row is None or row["pct_chg"] is None:
        return None
    return float(row["pct_chg"])


def _query_benchmark_pct(
    conn: sqlite3.Connection,
    ts_code: str,
    score_date: str,
) -> Optional[float]:
    """查 benchmark 指数当日涨跌幅（fact_index_daily，不是 fact_stock_daily）。"""
    row = conn.execute(
        "SELECT pct_chg FROM market.fact_index_daily "
        "WHERE ts_code = ? AND trade_date = ?",
        (ts_code, score_date),
    ).fetchone()
    if row is None or row["pct_chg"] is None:
        return None
    return float(row["pct_chg"])


def _compute_direction(
    strength_score: Optional[int],
    sector_pct: Optional[float],
) -> Optional[int]:
    """方向正确：(强度>0 ∧ 板块涨) ∨ (强度<0 ∧ 板块跌) → 1，反向 → 0。

    强度 = 0 或 sector_pct = None 或 sector_pct = 0 → None（不可判定）。
    """
    if strength_score is None or strength_score == 0:
        return None
    if sector_pct is None or sector_pct == 0.0:
        return None
    if (strength_score > 0 and sector_pct > 0) or (
        strength_score < 0 and sector_pct < 0
    ):
        return 1
    return 0


_UPSERT_TPS = """
INSERT INTO theme_prediction_scores
    (theme_id, prompt_id, prompt_version, report_date, score_date,
     days_offset, sector_pct, stock_avg_pct, stock_weighted_pct,
     hit_count, total_count, hit_rate,
     benchmark_pct, alpha, direction_correct,
     created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(theme_id, score_date) DO UPDATE SET
    days_offset = excluded.days_offset,
    sector_pct = excluded.sector_pct,
    stock_avg_pct = excluded.stock_avg_pct,
    stock_weighted_pct = excluded.stock_weighted_pct,
    hit_count = excluded.hit_count,
    total_count = excluded.total_count,
    hit_rate = excluded.hit_rate,
    benchmark_pct = excluded.benchmark_pct,
    alpha = excluded.alpha,
    direction_correct = excluded.direction_correct,
    created_at = excluded.created_at
""".strip()


_UPSERT_TSS = """
INSERT INTO theme_stock_scores
    (theme_stock_id, theme_id, normalized_code, report_date, score_date,
     days_offset, pct_chg, is_hit, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(theme_stock_id, score_date) DO UPDATE SET
    days_offset = excluded.days_offset,
    pct_chg = excluded.pct_chg,
    is_hit = excluded.is_hit,
    created_at = excluded.created_at
""".strip()


def _write_scores(
    conn: sqlite3.Connection, result: ThemeDailyScore,
) -> None:
    """把主表 + 明细表的打分结果 upsert 到 ai_inference.db。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        _UPSERT_TPS,
        (
            result.theme_id, result.prompt_id, result.prompt_version,
            result.report_date, result.score_date, result.days_offset,
            result.sector_pct, result.stock_avg_pct,
            result.stock_weighted_pct,
            result.hit_count, result.total_count, result.hit_rate,
            result.benchmark_pct, result.alpha,
            result.direction_correct,
            now,
        ),
    )
    if result.stock_rows:
        conn.executemany(
            _UPSERT_TSS,
            [
                (
                    sid, result.theme_id, code,
                    result.report_date, result.score_date,
                    result.days_offset,
                    pct, is_hit,
                    now,
                )
                for sid, code, pct, is_hit in result.stock_rows
            ],
        )


def _ensure_yyyymmdd(s: str) -> None:
    if not isinstance(s, str) or len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD，得到 {s!r}")
