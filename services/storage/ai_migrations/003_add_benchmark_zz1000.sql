-- ai_inference.db 迁移 003：theme_prediction_scores 加列 benchmark_zz1000_pct
-- 编写时间: 2026-05-28 18:40
-- 编写人: 辉夜
--
-- 改造目的：
--   原 alpha = theme_pct - benchmark_pct(上证综指 000001.SH)，单日值。
--   主人提出 D+1 容易因开盘竞价被挂高/挂低导致 α 失真，新增 α-1 指标：
--     题材级在 D+2~D+5 各自算 (theme_pct - 中证1000 pct)，再对 4 天求均值。
--   单日表多存一个原始基准列 benchmark_zz1000_pct（中证1000 = 000852.SH 当日 pct_chg），
--   衍生 α-1 完全由聚合层 SQL 推出，单日表不冗余存衍生值。
--
-- 与 002_add_theme_pct.sql 的并列关系：
--   * theme_pct        (002): 题材综合涨幅 = 0.6 sector + 0.4 stock_avg
--   * benchmark_pct    (001): 大盘指数 pct（默认上证综指 000001.SH）
--   * benchmark_zz1000_pct (本迁移): 中证1000 当日 pct_chg
--
-- 历史数据：本迁移仅加列，老行 benchmark_zz1000_pct = NULL；需配合
--           tools/backfill_benchmark_zz1000.py 一次性回填。

ALTER TABLE theme_prediction_scores
    ADD COLUMN benchmark_zz1000_pct REAL;
