-- =====================================================================
-- 001_initial.sql  market.db 初始 Schema (Phase M1, 2026-05-26)
-- =====================================================================
-- 14 张表（13 业务 + 1 元数据）+ 索引；所有语句幂等。
-- 详细设计见 doc/design/05-26-2126-盘后数据自动化施工方案.md §3。
--
-- 约定:
--   * 日期字段统一 TEXT 格式 'YYYYMMDD'（Tushare 一致）
--   * 金额字段统一"亿元"为单位，浮点
--   * 涨跌幅字段统一"%"为单位，浮点
--   * 时间戳字段统一 ISO8601 'YYYY-MM-DD HH:MM:SS' 形式
--   * 每张 fact_* 表均带 raw_json 列，保留 Tushare 原始 dict 完整内容
-- =====================================================================

-- ---------- 维度表（4 张） ----------

CREATE TABLE IF NOT EXISTS dim_trade_calendar (
    trade_date    TEXT PRIMARY KEY,         -- YYYYMMDD
    is_open       INTEGER NOT NULL,         -- 1=交易日 0=休市
    pretrade_date TEXT,                     -- 上一交易日 YYYYMMDD
    cal_date      TEXT                      -- 自然日 YYYYMMDD（可与 trade_date 同）
);
CREATE INDEX IF NOT EXISTS idx_calendar_open
    ON dim_trade_calendar(is_open, trade_date DESC);

CREATE TABLE IF NOT EXISTS dim_sector (
    ts_code        TEXT PRIMARY KEY,        -- 例 'BK0917.DC'
    name           TEXT NOT NULL,
    idx_type       TEXT NOT NULL,           -- 概念板块/行业板块
    src            TEXT NOT NULL,           -- dc/ths/sw
    list_date      TEXT,
    last_seen_date TEXT,
    UNIQUE(name, src)
);
CREATE INDEX IF NOT EXISTS idx_sector_name ON dim_sector(name);

CREATE TABLE IF NOT EXISTS dim_stock (
    ts_code   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    market    TEXT,                          -- 主板/创业板/科创板/北证
    industry  TEXT,
    list_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_name ON dim_stock(name);

-- 游资席位别名（取代旧 config/trader_seat_aliases.json）
CREATE TABLE IF NOT EXISTS dim_trader_alias (
    exalter    TEXT PRIMARY KEY,            -- 营业部全名（top_inst.exalter）
    alias      TEXT NOT NULL,               -- 简称如 "拉萨天团"
    is_famous  INTEGER NOT NULL DEFAULT 0,  -- 1=知名游资 0=普通
    notes      TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_famous
    ON dim_trader_alias(is_famous, alias);

-- ---------- 事实表（8 张） ----------

CREATE TABLE IF NOT EXISTS fact_index_daily (
    trade_date TEXT NOT NULL,
    ts_code    TEXT NOT NULL,                -- 000001.SH / 399001.SZ / 399006.SZ / 000688.SH
    close      REAL,
    pct_chg    REAL,                         -- %
    amount_yi  REAL,                         -- 亿元
    vol        REAL,                         -- 手
    raw_json   TEXT,
    PRIMARY KEY(trade_date, ts_code)
);

CREATE TABLE IF NOT EXISTS fact_sector_daily (
    trade_date     TEXT NOT NULL,
    ts_code        TEXT NOT NULL REFERENCES dim_sector(ts_code),
    pct_chg        REAL,                     -- %
    main_net_yi    REAL,                     -- 主力净流入（亿）
    main_elg_yi    REAL,                     -- 超大单净流入（亿）
    main_lg_yi     REAL,                     -- 大单净流入（亿）
    limit_up_count INTEGER,
    pct_chg_5d     REAL,                     -- 派生：近 5 日累计涨幅（%）
    rank_today     INTEGER,                  -- 当日涨幅排名
    raw_json       TEXT,
    PRIMARY KEY(trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_sector_pct
    ON fact_sector_daily(trade_date, pct_chg DESC);
CREATE INDEX IF NOT EXISTS idx_sector_code
    ON fact_sector_daily(ts_code, trade_date DESC);

CREATE TABLE IF NOT EXISTS fact_limit_stock (
    trade_date    TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    limit_type    TEXT NOT NULL,             -- U=涨停 / Z=炸板 / D=跌停
    status        TEXT,                      -- "首板" / "2连板" / ...
    cons_nums     INTEGER,                   -- 连板数
    theme         TEXT,
    limit_up_time TEXT,                      -- 涨停时刻
    open_times    INTEGER,                   -- 炸板次数
    close         REAL,
    pct_chg       REAL,
    fd_amount_yi  REAL,                      -- 封单金额（亿）
    raw_json      TEXT,
    PRIMARY KEY(trade_date, ts_code, limit_type)
);
CREATE INDEX IF NOT EXISTS idx_limit_cons
    ON fact_limit_stock(trade_date, cons_nums DESC);
CREATE INDEX IF NOT EXISTS idx_limit_theme
    ON fact_limit_stock(theme, trade_date);

-- 北向资金（T 日可能空，回退到 pretrade_date，actual_date 记实际取到的日期）
CREATE TABLE IF NOT EXISTS fact_hsgt_daily (
    trade_date    TEXT PRIMARY KEY,           -- 请求的日期 YYYYMMDD
    actual_date   TEXT,                       -- 实际取到数据的日期 YYYYMMDD
    is_delayed    INTEGER NOT NULL DEFAULT 0, -- 1=取的是 pretrade_date 的数据
    north_net_yi  REAL,                       -- 北向净流入（亿）
    south_net_yi  REAL,                       -- 南向净流入（亿）
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS fact_top_list (
    trade_date    TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    rank_reason   TEXT NOT NULL,             -- 上榜原因
    net_amount_yi REAL,                      -- 龙虎榜净买入（亿）
    pct_chg       REAL,
    raw_json      TEXT,
    PRIMARY KEY(trade_date, ts_code, rank_reason)
);

CREATE TABLE IF NOT EXISTS fact_top_inst (
    trade_date     TEXT NOT NULL,
    ts_code        TEXT NOT NULL,
    exalter        TEXT NOT NULL,            -- 营业部全名
    side           TEXT NOT NULL,            -- "buy" / "sell"（已从 0/1 标准化）
    net_buy_yi     REAL,                     -- 净买入（亿）
    buy_amount_yi  REAL,                     -- 买入额（亿）
    sell_amount_yi REAL,                     -- 卖出额（亿）
    raw_json       TEXT,
    PRIMARY KEY(trade_date, ts_code, exalter, side)
);
CREATE INDEX IF NOT EXISTS idx_top_inst_exalter
    ON fact_top_inst(exalter, trade_date DESC);

-- 财联社：个股异动（catalysts 来源）
CREATE TABLE IF NOT EXISTS fact_cls_stock_shock (
    trade_date TEXT NOT NULL,
    ts_code    TEXT NOT NULL,
    reason     TEXT,
    plate_json TEXT,                         -- 关联板块 JSON 数组字符串
    shock_time TEXT,                         -- HH:MM:SS
    raw_json   TEXT,
    PRIMARY KEY(trade_date, ts_code, shock_time)
);

-- 财联社：板块异动时间线
CREATE TABLE IF NOT EXISTS fact_cls_market_shock (
    trade_date       TEXT NOT NULL,
    sector_name      TEXT NOT NULL,
    first_shock_time TEXT,                   -- 首次异动时刻
    shock_count      INTEGER,
    status           TEXT,                   -- "up" / "down"
    raw_json         TEXT,
    PRIMARY KEY(trade_date, sector_name, status)
);

-- ---------- 汇总表（2 张） ----------

-- 盘后总结主表：每天一行；canonical JSON + compact MD 都内联
CREATE TABLE IF NOT EXISTS market_summaries (
    trade_date        TEXT PRIMARY KEY,
    prev_trade_date   TEXT,
    schema_version    TEXT NOT NULL,         -- 例 "1.0"
    mode              TEXT NOT NULL,         -- hybrid/tushare-only/ai-full
    generated_at      TEXT NOT NULL,         -- 生成时间 ISO
    elapsed_ms        INTEGER,
    api_call_count    INTEGER,
    completeness      REAL,                  -- 0.0~1.0

    -- 常用 KPI 冗余列（避免每次解 JSON）
    limit_up_count    INTEGER,
    limit_down_count  INTEGER,
    seal_rate         REAL,                  -- 封板率 %
    promotion_rate    REAL,                  -- 晋级率 %
    max_height        INTEGER,               -- 市场最高板
    max_stock_name    TEXT,                  -- 最高板对应股票名
    north_net_yi      REAL,                  -- 北向净流入（亿）
    total_turnover_yi REAL,                  -- 两市成交额（亿）

    summary_json      TEXT NOT NULL,         -- canonical JSON 全文
    summary_md        TEXT,                  -- compact Markdown 渲染产物

    warnings_json     TEXT,                  -- ["msg1","msg2"]
    gaps_json         TEXT,                  -- [{"field":"...","reason":"..."}]
    conflicts_json    TEXT                   -- 合并冲突记录
);
CREATE INDEX IF NOT EXISTS idx_summaries_date
    ON market_summaries(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_completeness
    ON market_summaries(completeness);

-- AI 兜底 patch 历史（用于后续 Eval AI 行为）
CREATE TABLE IF NOT EXISTS ai_enrich_patches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date     TEXT NOT NULL,
    provider       TEXT,
    model          TEXT,
    prompt_id      TEXT,                     -- 引用 prompts/market_fetch/{id}.md
    prompt_version TEXT,
    patch_json     TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    elapsed_ms     INTEGER,
    created_at     TEXT NOT NULL,
    FOREIGN KEY(trade_date) REFERENCES market_summaries(trade_date)
);
CREATE INDEX IF NOT EXISTS idx_patch_date ON ai_enrich_patches(trade_date);
CREATE INDEX IF NOT EXISTS idx_patch_prompt
    ON ai_enrich_patches(prompt_id, prompt_version);

-- ---------- 元数据表 ----------

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,          -- 例 1
    name       TEXT NOT NULL,                -- 例 '001_initial'
    applied_at TEXT NOT NULL                 -- ISO8601
);
