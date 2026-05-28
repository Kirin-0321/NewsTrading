-- 009_dim_sector_stock.sql  (2026-05-28)
--
-- 板块 ↔ 成员股关联表。给「板块领涨股 Top3」+ GUI 点击板块展开看成员
-- 两个功能用。详细设计见：
--   doc/design/05-28-1625-板块领涨股与GUI展开施工方案.md
--
-- 数据来源：Tushare dc_member 接口（东方财富板块成份股）
-- 入参：ts_code + trade_date
-- 出参：con_code（成员股代码）+ name
--
-- 同步策略：
--   * 首次：CLI tools/dim_sector_stock_sync.py 全量灌入 ~1013 个 dc 板块
--     × 平均 50 成员 ≈ 5-10 万行
--   * 周更：每周日跑同 CLI 覆盖更新（成员变化慢，不做差分）
--
-- 限制：
--   * 仅 dc 源板块（src='dc'）；ths 源板块 dc_member 不返回，靠 grouped
--     视图的 dc_fallback 兜底（取同组 dc 板块的成员）
--   * 只存"最新快照"，不按 trade_date 切分；若未来需要历史成员关系，
--     另建 fact_sector_stock_history（成本高，暂不做）

CREATE TABLE IF NOT EXISTS dim_sector_stock (
    sector_ts_code TEXT NOT NULL,    -- 板块代码 BK0896.DC
    stock_ts_code  TEXT NOT NULL,    -- 成员股代码 600519.SH
    stock_name     TEXT,             -- 成员股名称（冗余存，避 JOIN dim_stock）
    updated_at     TEXT NOT NULL,    -- 最后同步时间 ISO8601
    PRIMARY KEY (sector_ts_code, stock_ts_code)
);

CREATE INDEX IF NOT EXISTS idx_secstock_stock
    ON dim_sector_stock(stock_ts_code);
