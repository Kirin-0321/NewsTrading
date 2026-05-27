-- market.db 005: 个股日行情建表 + 索引 (Phase -1 三库重构)
-- 编写时间: 2026-05-27 13:35
-- 编写人: 辉夜
-- 参考: doc/design/05-27-1209-三库表结构详细设计.md §2
--
-- 关键发现 (Phase -1 调研):
--   1. fact_sector_daily 已在 001_initial.sql 建好 (14660 行数据,
--      字段名用 ts_code 而非 sector_code, 还有 main_net_yi /
--      pct_chg_5d / rank_today 等盘后数据自动化模块用到的字段),
--      本迁移 **不重复创建**, 直接复用现有 schema.
--   2. fact_stock_daily 历史上从未在任何 migration 里建过,
--      但 market.db 里已有该表 (可能是开发期手动建的, 空表),
--      schema 与本设计完全一致, IF NOT EXISTS 幂等通过.
--
-- 业务定位: 模板回测打分模块的数据底座
--   - fact_stock_daily : 全 A 股 ~5400 只 x 每天, 算"标的平均涨幅"用
--
-- 注意: schema_migrations 由 MarketDB._apply_migration() 自动维护, 本文件无需写

-- =============================================================
-- fact_stock_daily  个股日行情表
-- =============================================================
CREATE TABLE IF NOT EXISTS fact_stock_daily (
    ts_code      TEXT NOT NULL,                 -- 股票代码带后缀, 如 600172.SH
    trade_date   TEXT NOT NULL,                 -- YYYYMMDD
    open         REAL,                          -- 开盘价 (元)
    high         REAL,                          -- 最高价 (元)
    low          REAL,                          -- 最低价 (元)
    close        REAL,                          -- 收盘价 (元)
    pre_close    REAL,                          -- 前收盘价 (算涨跌幅用)
    pct_chg      REAL,                          -- 涨跌幅 (%) ★ 打分核心字段
    vol          REAL,                          -- 成交量 (手)
    amount       REAL,                          -- 成交额 (千元)
    PRIMARY KEY (ts_code, trade_date)           -- 同股同日唯一
);
CREATE INDEX IF NOT EXISTS idx_fsd_trade_date   ON fact_stock_daily(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_fsd_code_date    ON fact_stock_daily(ts_code, trade_date DESC);

-- =============================================================
-- fact_sector_daily  板块日行情表 (复用现有)
-- =============================================================
-- 已在 001_initial.sql 第 70-86 行创建, 字段更丰富, 数据已积累.
-- 现有 schema:
--   trade_date / ts_code (板块代码) / pct_chg ★ / main_net_yi /
--   main_elg_yi / main_lg_yi / limit_up_count / pct_chg_5d /
--   rank_today / raw_json
-- 索引: idx_sector_pct (trade_date, pct_chg DESC),
--       idx_sector_code (ts_code, trade_date DESC)
-- 打分模块 SQL 中需用 ts_code 字段而非 sector_code, 跟现有惯例对齐.
