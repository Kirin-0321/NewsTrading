-- 007_dim_sector_group.sql  (2026-05-28)
--
-- 给 dim_sector 引入「语义聚类组」概念，让 AI 看 Top N 板块时同主题
-- 板块自动合并去重（白酒/白酒Ⅱ/白酒Ⅲ/酿酒概念 → group="白酒"），
-- 同组内取中位涨幅板块。详细设计见
--   doc/design/05-28-1230-板块语义聚类与中位去重设计.md
--
-- 表关系：
--   dim_sector.group_id  ──┐
--                          ↓
--                 dim_sector_group.id  (PK)
--
-- group_id 可为 NULL（未聚类的板块走"自成一组"逻辑，SQL 端 fallback ts_code）
--
-- 幂等：CREATE TABLE IF NOT EXISTS + ALTER TABLE 加列前先检查列是否存在
-- （SQLite 不支持 IF NOT EXISTS for ADD COLUMN，由迁移 runner 容错执行）

CREATE TABLE IF NOT EXISTS dim_sector_group (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,        -- 组名（如 "白酒"）
    canonical_alias TEXT,                         -- 主人手动指定的"标准名"（可空）
    members_count   INTEGER NOT NULL DEFAULT 0,   -- 组内板块数（聚类时填入）
    created_by      TEXT NOT NULL DEFAULT 'auto', -- 'llm-v1' / 'rule' / 'manual'
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    notes           TEXT                          -- LLM 给的简短理由
);

CREATE INDEX IF NOT EXISTS idx_sector_group_name
    ON dim_sector_group(name);

-- dim_sector 加 group_id 字段（外键软引用，不加 FK 约束以便手工维护）
ALTER TABLE dim_sector ADD COLUMN group_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_sector_group_id
    ON dim_sector(group_id);
