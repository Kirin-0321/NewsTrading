-- =====================================================================
-- 002_seed_trader_aliases.sql  游资席位别名种子数据（Phase M1, 2026-05-26）
-- =====================================================================
-- 来源：公开市场常见知名游资席位整理。共 30+ 条。
-- 写法：INSERT OR IGNORE，幂等；主键 exalter 未命中时插入，已存在时不动。
-- 后续主人可在 GUI 的"游资别名编辑"对话框增删改（M3 阶段实现）。
--
-- 字段说明:
--   exalter    : 营业部全名（与 Tushare top_inst.exalter 一致；Tushare 偶有微调，
--                未命中的将由 AI enricher 兜底，主人补录）
--   alias      : 显示用简称（"拉萨天团"/"章盟主"/"赵老哥"...）
--   is_famous  : 1=知名游资（GUI 紫色高亮），0=普通席位/机构席位
--   notes      : 说明（风格/历史/对应人物等，可选）
--   updated_at : 写入时间（CURRENT_TIMESTAMP）
-- =====================================================================

-- ---------- 拉萨天团（藏帮，借通道为主） ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('东方财富证券股份有限公司拉萨团结路第二证券营业部',         '拉萨团结路二',   1, '拉萨天团核心', CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨东环路第二证券营业部',         '拉萨东环二',     1, '拉萨天团',     CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨东环路证券营业部',             '拉萨东环',       1, '拉萨天团',     CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨金珠西路第一证券营业部',       '拉萨金珠西一',   1, '拉萨天团',     CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨金珠西路证券营业部',           '拉萨金珠西',     1, '拉萨天团',     CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨纳金路证券营业部',             '拉萨纳金路',     1, '拉萨天团',     CURRENT_TIMESTAMP),
('东方财富证券股份有限公司拉萨金融城北环路证券营业部',       '拉萨北环',       1, '拉萨天团',     CURRENT_TIMESTAMP);

-- ---------- 章盟主 / 赵老哥 / 陈小群 等顶级游资 ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('中信证券股份有限公司杭州延安路证券营业部',                 '章盟主',         1, '章建平大本营，超大资金低吸',         CURRENT_TIMESTAMP),
('国泰君安证券股份有限公司宁波彩虹北路证券营业部',           '彩虹北路',       1, '章盟主常用',                         CURRENT_TIMESTAMP),
('国泰君安证券股份有限公司南京太平南路证券营业部',           '赵老哥',         1, '南京赵强，短线一哥',                 CURRENT_TIMESTAMP),
('中泰证券股份有限公司常州延陵西路证券营业部',               '常州帮',         1, '赵老哥曾用',                         CURRENT_TIMESTAMP),
('中信证券（山东）有限责任公司青岛分公司',                   '青岛席位',       1, '陈小群代表席位',                     CURRENT_TIMESTAMP),
('国泰君安证券股份有限公司顺德大良证券营业部',               '乔帮主',         1, '顺德大良知名游资',                   CURRENT_TIMESTAMP),
('国泰君安证券股份有限公司上海福山路证券营业部',             '孙哥',           1, '福山路代表席位',                     CURRENT_TIMESTAMP);

-- ---------- 佛山无影脚 / 金田路 ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('华泰证券股份有限公司深圳益田路荣超商务中心证券营业部',     '金田路',         1, '佛山无影脚旧地、深市超短',           CURRENT_TIMESTAMP),
('华泰证券股份有限公司深圳益田路中投国际商务中心证券营业部', '深圳益田路中投', 1, '深市知名游资席位',                   CURRENT_TIMESTAMP);

-- ---------- 浙系 / 杭州派 ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('财通证券股份有限公司杭州上塘路证券营业部',                 '炒新一族',       1, '杭州上塘路、新股专户',               CURRENT_TIMESTAMP),
('国信证券股份有限公司杭州玉皇山南基金小镇证券营业部',       '玉皇山',         1, '梁帅、私募阵地',                     CURRENT_TIMESTAMP),
('财通证券股份有限公司绍兴营业部',                           '徐留胜',         1, '高频打板代表',                       CURRENT_TIMESTAMP),
('中国银河证券股份有限公司绍兴证券营业部',                   '银河绍兴',       1, '浙系打板席位',                       CURRENT_TIMESTAMP),
('国泰君安证券股份有限公司宁波桑田路证券营业部',             '宁波桑田路',     1, '宁波涨停板敢死队后裔',               CURRENT_TIMESTAMP),
('中信证券股份有限公司宁波解放南路证券营业部',               '宁波解放南路',   1, '徐翔旧部',                           CURRENT_TIMESTAMP),
('国信证券股份有限公司宁波解放南路证券营业部',               '国信宁波',       1, '宁波系',                             CURRENT_TIMESTAMP);

-- ---------- 沪/京 ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('中信证券股份有限公司上海溧阳路证券营业部',                 '溧阳路',         1, '赵建平、合力哥',                     CURRENT_TIMESTAMP),
('招商证券股份有限公司上海陆家嘴金融贸易区东方路证券营业部', '葛卫东',         1, '葛老大',                             CURRENT_TIMESTAMP),
('中信证券股份有限公司北京呼家楼证券营业部',                 '呼家楼帮',       1, '北京老牌大资金',                     CURRENT_TIMESTAMP),
('招商证券股份有限公司北京东三环中路证券营业部',             '北京东三环',     1, '北方游资',                           CURRENT_TIMESTAMP),
('申万宏源证券有限公司上海闵行老沪闵路证券营业部',           '老沪闵路',       1, '上海超短席位',                       CURRENT_TIMESTAMP);

-- ---------- 作手新一 / 粤系 ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('招商证券股份有限公司深圳后海大道荣超中心证券营业部',       '作手新一',       1, '深圳后海大道、超短代表',             CURRENT_TIMESTAMP),
('招商证券股份有限公司广州天河路证券营业部',                 '广州天河',       1, '粤系游资',                           CURRENT_TIMESTAMP),
('招商证券股份有限公司广州天河北路证券营业部',               '广州天河北',     1, '粤系游资',                           CURRENT_TIMESTAMP),
('华林证券股份有限公司南通新建路证券营业部',                 '南通新建路',     1, '苏系打板',                           CURRENT_TIMESTAMP);

-- ---------- 特殊席位（is_famous=0，做归类标识用） ----------
INSERT OR IGNORE INTO dim_trader_alias (exalter, alias, is_famous, notes, updated_at) VALUES
('机构专用',                                                 '机构专用',       0, '基金/QFII/券商自营/保险等机构席位汇总',
    CURRENT_TIMESTAMP),
('华泰证券股份有限公司总部',                                 '华泰总部',       0, '通常为程序化/QFII 通道',             CURRENT_TIMESTAMP);
