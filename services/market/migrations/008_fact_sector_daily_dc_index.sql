-- 008_fact_sector_daily_dc_index.sql  (2026-05-28)
--
-- 给 fact_sector_daily 加 dc_index 接口的 4 个字段：总市值 / 换手率 / 涨跌家数。
-- 仅 dc 源板块（dc 概念/行业/地域）会填这 4 列；ths 源（同花顺行业/概念）
-- 不提供这些字段，保持 NULL；聚合表读取时用「同组 dc 源 fallback」补值。
--
-- 详细设计见：
--   doc/design/05-28-1530-盘后报告板块表字段扩充设计.md（v2 含数据陷阱）
--   doc/design/05-28-1550-盘后报告板块表字段扩充施工方案.md
--
-- 数据流：
--   moneyflow_ind_dc(3 类) → fact_sector_daily INSERT（含 pct_chg / main_*）
--   ↓ 同 trade_date 之后
--   dc_index(3 类)         → fact_sector_daily UPDATE（含本次新加 4 列）
--
-- 单位约定（与项目惯例一致）：
--   total_mv      单位：亿元（Tushare dc_index.total_mv 单位是万元，落库前 ÷ 1e4）
--   turnover_rate 单位：% （直接落库 Tushare 原值）
--
-- 幂等：SQLite 不支持 IF NOT EXISTS for ADD COLUMN，由 migrate runner 容错执行。

ALTER TABLE fact_sector_daily ADD COLUMN total_mv      REAL;     -- 总市值（亿元）
ALTER TABLE fact_sector_daily ADD COLUMN turnover_rate REAL;     -- 换手率（%）
ALTER TABLE fact_sector_daily ADD COLUMN up_num        INTEGER;  -- 板内上涨家数
ALTER TABLE fact_sector_daily ADD COLUMN down_num      INTEGER;  -- 板内下跌家数
