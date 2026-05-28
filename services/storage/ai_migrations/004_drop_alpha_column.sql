-- ai_inference.db 迁移 004：DROP COLUMN theme_prediction_scores.alpha
-- 编写时间: 2026-05-28 22:30
-- 编写人: 辉夜
-- 参考: doc/updates/05-28-2230-α体系金字塔重构上线.md
--
-- 改造目的：
--   原 alpha = theme_pct - benchmark_pct(上证综指)，单日单点值落库。
--   2026-05-28 22:30 α 体系金字塔重构后：
--     * α / α-1 / α+N 全部走「报告→模板」两步聚合，仅以中证1000 为基准；
--     * α+N（逐日）由 SQL 现推 `theme_pct - benchmark_zz1000_pct`；
--     * α / α-1 由 α+N 派生（5/4 天均值）。
--   单日 alpha 字段不再被 GUI / CSV / 聚合 SQL 引用，正式弃用。
--
-- SQLite 版本要求：>= 3.35.0（DROP COLUMN 语法），项目当前 3.49.1 OK。
--
-- 历史数据：本迁移仅删列，不影响 theme_pct / benchmark_pct /
--           benchmark_zz1000_pct 三个原始基准列。
ALTER TABLE theme_prediction_scores
    DROP COLUMN alpha;
