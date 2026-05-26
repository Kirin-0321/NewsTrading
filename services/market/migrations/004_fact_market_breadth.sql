-- =====================================================================
-- 004_fact_market_breadth.sql  全市场涨跌家数（advance/decline）
-- =====================================================================
-- 背景:
--   M1b 起，summary.breadth 长期只有 limit_up/limit_down/failed_limit（口径来自
--   fact_limit_stock），advance/decline（涨/跌家数）始终为 None。AI 分析
--   缺少「市场广度」的关键信号——市场是普涨还是少数股扛指数？
--
-- 设计:
--   单行/天，pretty-tiny 聚合表，避免把 5400+ 行 daily 全量入库。
--   每次 force-refresh 调 tushare.daily(trade_date=...) 一次拿全市场，
--   按 pct_chg 分桶聚合后 INSERT OR REPLACE 一行。
--
-- 字段:
--   advance      涨家数 (pct_chg > 0)
--   decline      跌家数 (pct_chg < 0)
--   unchanged    平盘家数 (pct_chg == 0)
--   advance_5    大涨家数 (pct_chg >= 5)
--   decline_5    大跌家数 (pct_chg <= -5)
--   advance_pct  涨家数占比 (advance / total)
--   total        当日有效样本（pct_chg 非 NULL）
-- =====================================================================

CREATE TABLE IF NOT EXISTS fact_market_breadth (
    trade_date   TEXT PRIMARY KEY,
    advance      INTEGER,
    decline      INTEGER,
    unchanged    INTEGER,
    advance_5    INTEGER,
    decline_5    INTEGER,
    advance_pct  REAL,
    total        INTEGER,
    raw_json     TEXT
);
