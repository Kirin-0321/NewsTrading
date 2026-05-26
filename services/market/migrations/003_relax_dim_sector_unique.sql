-- =====================================================================
-- 003_relax_dim_sector_unique.sql  放宽 dim_sector 唯一约束
-- =====================================================================
-- 原因:
--   001 里 dim_sector 加了 UNIQUE(name, src)，本意是「同源下板块名不重复」。
--   但 Tushare dc_index 实际返回里，同名 (name) 同源 (src='dc') 但
--   不同 ts_code / 不同 idx_type 的板块是常态——例如同一个题材名同时存在
--   "概念板块" 和 "行业板块" 两类。这个约束跟实测数据冲突，
--   会导致 dc_index 入库失败，进而 fact_sector_daily 外键也失败。
--
-- 解决:
--   重建 dim_sector，仅保留 PRIMARY KEY(ts_code)，去掉 UNIQUE(name, src)。
--   name 查找仍由 idx_sector_name 索引加速。
--
-- 注意:
--   - 这里涉及 DROP/RENAME 表，FOREIGN KEY 引用必须暂时关闭。
--     ``MarketDB._apply_migration`` 会在执行任何迁移前自动设置
--     ``PRAGMA foreign_keys=OFF`` 并在结束后恢复，故本脚本只关心 schema。
--   - 全量幂等：先检查列结构判断是否需要重建。
--   - 旧索引会随 DROP TABLE 自动消失，需要重建。
-- =====================================================================

-- 临时新表（仅当老表存在 UNIQUE(name, src) 时才需要走重建路径，
-- 但 SQLite 的 SQL 没有条件 DDL，干脆走幂等的 ALTER 流程）

CREATE TABLE IF NOT EXISTS dim_sector_v2 (
    ts_code        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    idx_type       TEXT NOT NULL,
    src            TEXT NOT NULL,
    list_date      TEXT,
    last_seen_date TEXT
);

-- 拷数据（已经在 v2 里的记录会因 PK 冲突而忽略，幂等）
INSERT OR IGNORE INTO dim_sector_v2
    (ts_code, name, idx_type, src, list_date, last_seen_date)
SELECT ts_code, name, idx_type, src, list_date, last_seen_date
FROM dim_sector;

-- 删旧表 + 改名
DROP TABLE dim_sector;
ALTER TABLE dim_sector_v2 RENAME TO dim_sector;

-- 重建索引
CREATE INDEX IF NOT EXISTS idx_sector_name ON dim_sector(name);
