-- ai_inference.db 迁移 002：theme_prediction_scores 加列 theme_pct
-- 编写时间: 2026-05-28 17:15
-- 编写人: 辉夜
-- 参考: doc/design/05-28-1715-报告评分题材加权改造施工方案.md
--
-- 改造目的：
--   把"报告级评分"的 D+N 数值口径从纯板块涨幅 sector_pct 改为
--   「板块涨幅 0.6 + 题材内标的均值涨幅 0.4」的加权合成涨幅 theme_pct；
--   当 stock_avg_pct IS NULL 时，theme_pct = sector_pct（系数 1.0 兜底）。
--
-- 计算公式（落库由 services/scoring/script_scorer.py::_compute_theme_pct 实现）：
--   sector_pct IS NULL                      → theme_pct = NULL
--   stock_avg_pct IS NULL                   → theme_pct = sector_pct
--   else                                    → theme_pct = 0.6 * sector_pct + 0.4 * stock_avg_pct
--
-- 同时修改 alpha 字段语义：alpha = theme_pct - benchmark_pct（替换原 stock_weighted_pct - benchmark_pct）
--
-- 历史数据：本迁移仅加列，老行 theme_pct 保持 NULL；需配合 tools/score_themes.py
--           --rescore-range 30 天回填重打分一次。

ALTER TABLE theme_prediction_scores
    ADD COLUMN theme_pct REAL;
