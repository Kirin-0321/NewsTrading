-- market.db 006: 涨停数据三源融合 (Phase A0)
-- 编写时间: 2026-05-27 17:24
-- 编写人: 辉夜
-- 设计依据: doc/design/05-27-1722-涨停数据三源融合施工方案.md §3.1
--
-- 背景:
--   现状只接入 limit_list_d (东财) + kpl_list (开盘啦) 两个数据源;
--   limit_list_d 当日延迟严重 + 长期漏算 28% 涨停股 (5/26 实测 46/64).
--   接入同花顺 limit_list_ths 作为主源, 三源融合后字段更丰富 + 当日盘
--   后即可见数据.
--
-- 1. fact_limit_stock 扩 8 列 (ths 独家 5 列 + d 独家 2 列 + 数据源标记)
-- 2. 新建 fact_limit_sprint (冲刺涨停池, 不属于涨停股, 独立表)
--
-- 注意:
--   * SQLite ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS, 但 schema_migrations
--     已记账防重跑, 直接 ALTER 即可.
--   * 旧字段不动: trade_date / ts_code / limit_type / status / cons_nums /
--     theme / limit_up_time / open_times / close / pct_chg / fd_amount_yi /
--     raw_json 全部保留, 旧代码读取兼容.

-- =============================================================
-- 1. fact_limit_stock 扩列 (8 列)
-- =============================================================

-- 同花顺独家 (ths)
ALTER TABLE fact_limit_stock ADD COLUMN lu_desc            TEXT;       -- 涨停原因 "黄金+业绩暴增+国企"
ALTER TABLE fact_limit_stock ADD COLUMN limit_up_suc_rate  REAL;       -- 一年封板率 0~1
ALTER TABLE fact_limit_stock ADD COLUMN market_type        TEXT;       -- HS=沪深主板/GEM=创业板/STAR=科创板
ALTER TABLE fact_limit_stock ADD COLUMN tag                TEXT;       -- "首板"/"3天3板"/"7天5板"
ALTER TABLE fact_limit_stock ADD COLUMN free_float         REAL;       -- 实际流通市值 (元)

-- 东财独家 (d)
ALTER TABLE fact_limit_stock ADD COLUMN industry           TEXT;       -- 申万行业 "贵金属"
ALTER TABLE fact_limit_stock ADD COLUMN total_mv           REAL;       -- 总市值 (元)

-- 数据源标记
ALTER TABLE fact_limit_stock ADD COLUMN source             TEXT;       -- "d+kpl+ths"/"kpl+ths"/"d+ths"/"ths-only" 等

-- =============================================================
-- 2. fact_limit_sprint  冲刺涨停池 (盘中接近涨停未封, pct < 10%)
-- =============================================================
CREATE TABLE IF NOT EXISTS fact_limit_sprint (
    trade_date    TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    name          TEXT,
    close         REAL,                  -- price (元)
    pct_chg       REAL,                  -- 涨幅 (%) 通常 9~19%
    rise_rate     REAL,                  -- 涨速 (%/分钟)
    turnover_rate REAL,                  -- 换手率 (%)
    turnover      REAL,                  -- 成交额 (元)
    free_float    REAL,                  -- 实际流通市值 (元)
    lu_desc       TEXT,                  -- 涨停原因
    market_type   TEXT,                  -- HS / GEM / STAR
    raw_json      TEXT,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_sprint_pct
    ON fact_limit_sprint(trade_date, pct_chg DESC);
