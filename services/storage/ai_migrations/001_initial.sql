-- ai_inference.db 初始化基线（Phase -1 三库重构）
-- 编写时间: 2026-05-27 13:30
-- 编写人: 辉夜
-- 参考: doc/design/05-27-1209-三库表结构详细设计.md §3
--
-- 包含 6 张表 + 17 个索引 + 6 个外键
-- 注意: 本文件不要手写 INSERT INTO schema_migrations,
--       该表由 AIInferenceDB._apply_migration() 自动建表 + 写入版本号

-- =============================================================
-- 3.1 ai_reports  AI 报告索引表
-- =============================================================
CREATE TABLE IF NOT EXISTS ai_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date       TEXT NOT NULL,                -- YYYYMMDD
    file_path         TEXT NOT NULL UNIQUE,          -- 相对仓库根的 posix 路径
    provider          TEXT,                          -- deepseek/openai/qwen/...
    model             TEXT,
    prompt_category   TEXT,                          -- analysis / theme_extraction / theme_review
    prompt_id         TEXT,                          -- 如 speculator_scalper
    prompt_version    TEXT,
    news_range_start  TEXT,                          -- 涉及新闻的起始时间
    news_range_end    TEXT,                          -- 涉及新闻的终止时间
    news_count        INTEGER,
    used_market_date  TEXT,                          -- 用到的盘后数据日期 YYYYMMDD
    theme_extracted   INTEGER NOT NULL DEFAULT 0,    -- 1=已抽过题材
    is_backtest       INTEGER NOT NULL DEFAULT 0,    -- v5 新增: 0=真实日常, 1=虚拟回测产物
    file_size         INTEGER,
    md5               TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reports_date         ON ai_reports(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_ai_reports_prompt       ON ai_reports(prompt_id, prompt_version);
CREATE INDEX IF NOT EXISTS idx_ai_reports_theme_flag   ON ai_reports(theme_extracted, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_ai_reports_is_backtest  ON ai_reports(is_backtest);


-- =============================================================
-- 3.2 theme_predictions  题材预测主表
-- =============================================================
CREATE TABLE IF NOT EXISTS theme_predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           TEXT NOT NULL,                 -- 文件名 string (非 ai_reports.id)
    report_date         TEXT NOT NULL,                 -- YYYYMMDD
    report_time         TEXT,
    report_path         TEXT NOT NULL,                 -- 相对 posix, 可 JOIN ai_reports.file_path
    theme_name          TEXT NOT NULL,
    theme_category      TEXT,
    strength_score      INTEGER NOT NULL,              -- v4: -100~+100, 负=利空 正=利多
    strength_level      TEXT NOT NULL,                 -- 9 档枚举
    priority_rank       INTEGER,
    duration            TEXT,
    expectation_gap     TEXT,
    is_cold             INTEGER NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL,
    risk_note           TEXT,
    -- 模板出身 (冗余自 ai_reports, 反查失败时 NULL)
    prompt_id           TEXT,
    prompt_version      TEXT,
    -- matcher 富化 (板块代码 + 置信度)
    sector_ts_code      TEXT,
    sector_match_conf   REAL,
    -- v5 新增: 跟着 ai_reports.is_backtest 透传
    is_backtest         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theme_report_date    ON theme_predictions(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_theme_name           ON theme_predictions(theme_name);
CREATE INDEX IF NOT EXISTS idx_theme_strength       ON theme_predictions(report_date, strength_score DESC);
CREATE INDEX IF NOT EXISTS idx_theme_category       ON theme_predictions(theme_category);
CREATE INDEX IF NOT EXISTS idx_theme_report_id      ON theme_predictions(report_id);
CREATE INDEX IF NOT EXISTS idx_theme_prompt         ON theme_predictions(prompt_id, prompt_version);
CREATE INDEX IF NOT EXISTS idx_theme_sector         ON theme_predictions(sector_ts_code);
CREATE INDEX IF NOT EXISTS idx_theme_is_backtest    ON theme_predictions(is_backtest);


-- =============================================================
-- 3.3 theme_stocks  题材标的表
-- =============================================================
CREATE TABLE IF NOT EXISTS theme_stocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    stock_name      TEXT NOT NULL,
    stock_code      TEXT,                              -- AI 原始代码 (可能不带后缀)
    normalized_code TEXT,                              -- matcher 规范化后, 如 600172.SH
    role            TEXT,
    reason          TEXT,
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_theme   ON theme_stocks(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_name    ON theme_stocks(stock_name);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_code    ON theme_stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_norm    ON theme_stocks(normalized_code);


-- =============================================================
-- 3.4 theme_news  题材新闻引用表
-- =============================================================
CREATE TABLE IF NOT EXISTS theme_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    news_ref        TEXT NOT NULL,                     -- AI 原文写法, 如 [news_xxx]
    news_id         TEXT,                              -- 跨库软引用 news.db.raw_news.id
    relation_type   TEXT,                              -- direct / related
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_theme_news_theme     ON theme_news(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_news_news_id   ON theme_news(news_id);


-- =============================================================
-- 3.5 theme_prediction_scores  题材每日打分表 (本期新增)
-- =============================================================
CREATE TABLE IF NOT EXISTS theme_prediction_scores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id            INTEGER NOT NULL,
    prompt_id           TEXT,                          -- 冗余, 聚合查询免 JOIN
    prompt_version      TEXT,
    report_date         TEXT NOT NULL,                 -- YYYYMMDD
    score_date          TEXT NOT NULL,                 -- YYYYMMDD, 实际打分用的交易日
    days_offset         INTEGER NOT NULL,              -- 1~5 (D+1 ~ D+5)
    sector_pct          REAL,                          -- 当日板块涨跌幅 (%)
    stock_avg_pct       REAL,                          -- 标的算术平均涨幅 (%)
    stock_weighted_pct  REAL,                          -- 按强度分加权平均
    hit_count           INTEGER,                       -- 涨幅>=阈值的标的数
    total_count         INTEGER,
    hit_rate            REAL,                          -- hit_count / total_count
    benchmark_pct       REAL,                          -- 大盘涨幅 (上证综指)
    alpha               REAL,                          -- stock_weighted_pct - benchmark_pct
    direction_correct   INTEGER,                       -- 1 / 0 / NULL
    ai_review_label     TEXT,                          -- right / lucky / wrong (D+5 才填)
    ai_review_comment   TEXT,                          -- AI 评语 (D+5 才填)
    created_at          TEXT NOT NULL,
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE,
    UNIQUE (theme_id, score_date)                      -- 幂等约束
);
CREATE INDEX IF NOT EXISTS idx_tps_theme            ON theme_prediction_scores(theme_id);
CREATE INDEX IF NOT EXISTS idx_tps_prompt_date      ON theme_prediction_scores(prompt_id, score_date);
CREATE INDEX IF NOT EXISTS idx_tps_score_date       ON theme_prediction_scores(score_date DESC);


-- =============================================================
-- 3.6 theme_stock_scores  标的每日打分表 (本期新增)
-- =============================================================
CREATE TABLE IF NOT EXISTS theme_stock_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_stock_id  INTEGER NOT NULL,
    theme_id        INTEGER NOT NULL,                  -- 双 FK 方便按题材聚合
    normalized_code TEXT NOT NULL,
    report_date     TEXT NOT NULL,
    score_date      TEXT NOT NULL,
    days_offset     INTEGER NOT NULL,
    pct_chg         REAL,                              -- 当日涨跌幅 (%)
    is_hit          INTEGER,                           -- pct_chg >= 阈值 (默认 3%)
    created_at      TEXT NOT NULL,
    FOREIGN KEY (theme_stock_id) REFERENCES theme_stocks(id) ON DELETE CASCADE,
    FOREIGN KEY (theme_id)       REFERENCES theme_predictions(id) ON DELETE CASCADE,
    UNIQUE (theme_stock_id, score_date)                -- 幂等约束
);
CREATE INDEX IF NOT EXISTS idx_tss_theme            ON theme_stock_scores(theme_id);
CREATE INDEX IF NOT EXISTS idx_tss_code_date        ON theme_stock_scores(normalized_code, score_date);
