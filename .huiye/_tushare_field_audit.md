# Tushare 接口字段审计报告（10000+ 积分实测）

> 创建时间: 2026-05-26  
> 测试方式: Tushare MCP (`user-tushareMcp`) 直连实测  
> 测试交易日: **20260525**（周一收盘，市场强势：上证 +0.96%、创业板 +2.10%、半导体板块 +3.0%）  
> Tushare 积分: **10000+**  
> 关联文档: [盘后数据自动化-功能说明.md](../doc/features/05-26-2126-盘后数据自动化功能说明.md) · [盘后数据自动化-施工方案.md](../doc/design/05-26-2126-盘后数据自动化施工方案.md)（施工时严格按本审计的字段名/单位/口径实现）

## 修订记录

| 日期 | 修订内容 | 影响章节 |
|---|---|---|
| 2026-05-26 | 初版（10000+ 积分全量字段实测） | 全文 |
| **2026-05-28** | **纠正 `dc_index.level` 误判**：早期只测了概念板块就下结论"全 null"，实测**行业板块的 level 提供官方 3 级行业层级**（东财一级 31 / 二级 128 / 三级 337）。同时补充 `idx_type=行业板块/地域板块` 数据规模（496/32 行） | §0.1 第 11 条、§2.1、§3.3、§3.3·A（新）、§6 |

---

## 0. 摘要（看这一段就够）

### 0.1 12 个关键发现（直接影响代码设计）

| # | 发现 | 影响 |
|--:|------|------|
| 1 | `trade_cal.pretrade_date` 字段直接给出前一交易日 | **代码不需要自己算 T-1**，节省一段逻辑 |
| 2 | `kpl_list.status` 字段直接给"首板/2连板/3连板/4连板…" | **连板梯队不用算**，直接 group by status |
| 3 | `kpl_list.theme` 字段已给出每只涨停股的"题材标签"（如"色金属·铜电缆设备"） | 板块归属可直接读取 |
| 4 | `cls_stock_shock.reason` 是**财联社人工写好的"涨停催化原因"** | **板块催化字段不需 AI 编**，已有人写好 |
| 5 | `cls_stock_shock.plate` 是 JSON 字符串，含该股关联的所有 CLS 板块代码+涨幅 | 板块自动映射的金矿 |
| 6 | `moneyflow_hsgt` **T 日当天无数据**（实测 20260525 返回空），需 T+1 才有 | 北向接口必须做"取不到→兜底用 pretrade_date"逻辑 |
| 7 | `moneyflow_ind_dc` 10000+ 积分**全字段**返回（含超大单/大单/中单/小单分层 + 每个板块小单买的最多的股票） | L3 不再需要 AI 估算板块资金，全部硬数据 |
| 8 | `top_inst` 给出 1010 条机构席位明细，`exalter`(营业部全名) + `side="0"买/"1"卖` | 游资识别只需做"营业部→游资别名"映射表，数据本身齐全 |
| 9 | `top_list` 含可转债（如 `118057.SH 甬矽转债`），需按 `ts_code` 前缀过滤 | fetcher 实现时记得过滤非股票 |
| 10 | `limit_step` 跟 `kpl_list.status` 完全冗余，且字段更少 | **不要调用 limit_step**，节省积分 |
| 11 | `dc_index.level` 仅**行业板块**填值，提供 3 级行业层级（东财一级 31 / 二级 128 / 三级 337）；概念 / 地域板块为 null | **行业大类聚合可直接用 `level='东财一级行业'` 锚定 31 个一级行业**，省掉 AI 聚类；详见 §3.3·A |
| 12 | `dc_hot.hot` / `dc_hot.concept` 字段全 null | 当前 dc_hot 接口字段残缺，**V1 不用**它，等数据补齐 |

### 0.2 之前 5103 积分拿不到、现在 10000+ 已确认能拿的字段

| 接口 | 新解锁字段 |
|------|-----------|
| `limit_list_d` | `fd_amount`（封单金额）、`up_stat`（涨停周期"8/13"格式）、`limit_amount`（跌停剩余卖单）、`first_time`/`last_time`（首次/末次涨停时间 HHMMSS） |
| `kpl_list` | 完整 28 字段（`lu_time`、`open_time`、`theme`、`status`、`net_change`、`limit_order`、`free_float`） |
| `moneyflow_ind_dc` | 4 档资金分层 + 小单买入最多的股票 |
| `top_inst` | 完整 1000+ 条席位明细，包括游资营业部全名 |

---

## 1. MCP 工具调用方式

### 1.1 MCP 基本信息

- **server identifier**: `user-tushareMcp`
- **token 鉴权**: MCP server 内部已配置，**调用时无需传 token**
- **目录**: `C:\Users\Administrator\.cursor\projects\d\mcps\user-tushareMcp\tools\`（共 180+ 个工具 JSON descriptor）
- **大输出**: 当返回超 50KB 时自动转储到 `C:\Users\Administrator\.cursor\projects\d\agent-tools\<uuid>.txt`，文件内是单行 JSON 数组

### 1.2 用途定位

| 场景 | 能用 MCP 吗 |
|------|:----------:|
| **辉夜施工期间，验证字段、查样本数据** | ✅ **强烈推荐** |
| 写完代码后 Q&A、调试字段不一致 | ✅ |
| 写进 `services/market/tushare_fetcher.py` 生产代码 | ❌ **绝对不行**（生产运行时无 Cursor，无 MCP） |

**生产代码继续用直 HTTP `urllib`**，跟 `tools/export_tushare_theme_daily.py` 一致。MCP 只在调试期使用。

---

## 2. 接口分级与最终采用清单

### 2.1 L1 ✅ 采用（生产 fetcher 必调）

| 接口 | 用途 | 主键字段 | 数据规模 |
|------|------|---------|---------|
| `trade_cal` | 交易日 + 前一交易日 | `cal_date`/`pretrade_date` | 单日 1 行 |
| `index_daily` | 大盘指数（上证/深成/创业板/沪深300/中证500/科创50） | `close`/`pct_chg`/`amount` | 单股单日 1 行 |
| `dc_index` (idx_type=概念板块/行业板块/地域板块) | 板块涨跌幅 + 龙头 + 上涨家数；行业板块自带 3 级层级 | `pct_change`/`leading`/`up_num`/`level` | 单日 486/496/32 行（概念/行业/地域，需分别调用） |
| `dc_daily` | 板块历史日线（算 5 日涨幅用） | `pct_change`/`close` | 单板块单日 1 行 |
| `limit_list_d` (U/Z/D) | 涨跌停统计 + 炸板 + 封单金额 | `fd_amount`/`up_stat`/`limit_times` | 单日 U=90 / Z=34 / D=15 |
| `kpl_list` (tag=涨停) | 涨停个股完整属性 + 连板状态 + 题材 | `status`/`theme`/`lu_time` | 单日 128 行 |
| `moneyflow_hsgt` | 北向资金 | `north_money`/`hgt`/`sgt` | 单日 1 行 |
| `moneyflow_ind_dc` (content_type=概念) | 板块主力资金流（4 档分层） | `net_amount`/`buy_elg_amount` | 单日 486 行 |
| `top_list` | 龙虎榜个股 | `net_amount`/`reason` | 单日 80+ 行（含可转债） |
| `top_inst` | 龙虎榜机构席位 | `exalter`/`side`/`net_buy` | 单日 1010 行 |
| `daily` | 个股行情（龙头股拉一批用） | `close`/`pct_chg`/`amount` | 多 ts_code 单次返回 |
| `daily_basic` | 个股基本面（市值/换手/PE） | `total_mv`/`circ_mv`/`pe_ttm` | 单股单日 1 行 |

### 2.2 L3 🌟 采用（取代部分 AI 推理）

| 接口 | 用途 | 取代了原本要 AI 干的什么 |
|------|------|----------------------|
| `cls_stock_shock` | 财联社涨停个股催化原因 + 板块映射 | "板块新闻催化"识别、"涨停原因"推理 |
| `cls_market_shock` | 财联社板块异动（盘中精确时间） | "板块发动时机"判断 |

### 2.3 ⚠️ 可选

| 接口 | 用途 | 备注 |
|------|------|------|
| `dc_concept` | 题材摘要（z_t_num/main_change/lead_stock） | 跟 dc_index 重叠，仅当需要 hot/strength 字段时用 |
| `dc_member` | 板块成份股 | 仅在做"个股反查所属题材"时用，V1 不必 |

### 2.4 ❌ 不采用

| 接口 | 原因 |
|------|------|
| `limit_step` | 跟 `kpl_list.status` 冗余，字段更少 |
| `dc_hot` | `hot` / `concept` 字段全 null，残缺 |
| `ths_daily` | dc_index 已够用，作交叉验证可选，V1 不需 |
| `kpl_concept` | 跟 dc_concept 重叠 |

---

## 3. 各接口完整字段表（含实测样本值）

### 3.1 trade_cal — 交易日历

**参数**: `start_date`, `end_date`, `exchange="SSE"`  
**默认字段**: `exchange, cal_date, is_open, pretrade_date`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `exchange` | str | `"SSE"` | 交易所 |
| `cal_date` | str | `"20260525"` | 日期 YYYYMMDD |
| `is_open` | int | `1` / `0` | 是否交易日 |
| `pretrade_date` | str | `"20260522"` | **前一交易日**，直接取这个字段即可，不用自己算 |

**重要**: `pretrade_date` 在周末/节假日时也是正确的"上一个交易日"，**强烈复用**。

---

### 3.2 index_daily — 大盘指数日线

**参数**: `ts_code`（必填）, `trade_date`  
**默认字段**: `ts_code, trade_date, close, open, high, low, pre_close, change, pct_chg, vol, amount`

| 字段 | 类型 | 实测样本（上证 000001.SH） | 说明 / 单位 |
|------|------|---------------------------|-----------|
| `ts_code` | str | `"000001.SH"` | 指数代码 |
| `close` | float | `4152.5686` | 收盘点位 |
| `pre_close` | float | `4112.8996` | 昨收 |
| `change` | float | `39.669` | 涨跌点 |
| `pct_chg` | float | `0.9645` | 涨跌幅 **%（注意：单位是百分比，不是小数）** |
| `vol` | float | `615930861.0` | 成交量 **手** |
| `amount` | float | `1445655378.4` | 成交额 **千元**（注意：除以 10^5 得到"亿元"） |

**关键 ts_code 集合**:
- `000001.SH` 上证综指
- `399001.SZ` 深证成指
- `399006.SZ` 创业板指
- `000300.SH` 沪深300
- `000905.SH` 中证500
- `000852.SH` 中证1000
- `000688.SH` 科创50

---

### 3.3 dc_index — 东方财富板块（必填 `idx_type`，支持概念/行业/地域三类）

**参数**: `idx_type=概念板块`（必填，**中文**） / `行业板块` / `地域板块` , `trade_date`  
**默认字段**: `ts_code, trade_date, name, leading, leading_code, pct_change, leading_pct, total_mv, turnover_rate, up_num, down_num, idx_type, level`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"BK0917.DC"` | 板块代码（半导体设备） |
| `name` | str | `"半导体设备"` | 板块名 |
| `leading` | str | `"东芯股份"` | 当日龙头 |
| `leading_code` | str | `"688110.SH"` | 龙头股票代码 |
| `pct_change` | float | `3.00` | 板块涨跌幅 **%** |
| `leading_pct` | float | `20.0` | 龙头当日涨幅 % |
| `total_mv` | float | `75969529.6` | 板块总市值 **万元** |
| `turnover_rate` | float | `5.56` | 换手率 % |
| `up_num` | int | `2` | 板内上涨家数 |
| `down_num` | int | `9` | 板内下跌家数 |
| `idx_type` | str | `"概念板块"` | 板块类型（回显入参） |
| `level` | str / null | `"东财一级行业"` | ⚠️ **仅 `idx_type=行业板块` 时填值**（3 级取值，见 §3.3·A）；概念/地域板块为 null |

**数据规模**（20260527 实测）:

| idx_type | 单日行数 | level 分布 |
|---|---|---|
| 概念板块 | **486** | 全 null |
| 行业板块 | **496** | 东财一级 31 / 二级 128 / 三级 337（详见 §3.3·A） |
| 地域板块 | **32** | 全 null |
| **合计** | **1014** | 三次调用即可全量抓取 |

> 2026-05-25 实测概念板块 = 486 条；2026-05-27 同样 486 条 → 总数稳定。

---

#### 3.3·A 东财行业层级（`idx_type=行业板块` 专属）

**这是 2026-05-28 实测纠错发现：** 之前误判 "level 全 null"，实际**行业板块的 level 字段提供官方 3 级行业树**。

| level 取值 | 数量 | 用途 / 含义 | 样本 |
|---|---|---|---|
| `东财一级行业` | **31** | **大类**——基本覆盖全市场行业 | 银行、电子、医药生物、有色金属、电力设备… |
| `东财二级行业` | 128 | 中类 | 金属新材料、工业金属、消费电子… |
| `东财三级行业` | 337 | 细分子类 | 稀土、铜、综合Ⅲ… |

**31 个东财一级行业全名单**（按板块代码倒序）：

> 银行、综合、医药生物、通信、社会服务、商贸零售、轻工制造、汽车、交通运输、建筑装饰、建筑材料、计算机、基础化工、机械设备、国防军工、非银金融、房地产、电子、电力设备、美容护理、环保、传媒、钢铁、有色金属、石油石化、家用电器、食品饮料、煤炭、纺织服饰、农林牧渔、公用事业

**应用价值**：

1. **行业大类聚合不用 AI**：`sector_grouping.py` 的 AI 聚类只对"概念板块"必要；行业板块直接 `WHERE level='东财一级行业'` 取 31 行作为锚点
2. **逐级下钻**：从一级 → 二级 → 三级，可做行业资金分布的多粒度视图
3. **跟 申万行业 区别**：`limit_list_d.industry` 是申万行业（如"IT服务Ⅱ"），`dc_index.level + name` 是东财行业体系，两套独立，**不要混用**

---

### 3.4 dc_daily — 板块历史日线（算 5 日涨幅用）

**参数**: `ts_code`（板块代码）, `start_date` + `end_date`  
**默认字段**: `ts_code, trade_date, close, open, high, low, change, pct_change, vol, amount, swing, turnover_rate`（额外返回 `category`）

| 字段 | 类型 | 实测样本（BK0917 半导体设备） | 说明 |
|------|------|--------------------------|------|
| `close` | float | `3194.1` | 板块指数收盘 |
| `pct_change` | float | `3.0` | 当日涨跌幅 **%** |
| `vol` | float | `189176460.0` | 成交量 |
| `amount` | float | `933366140990.0` | 成交额 **元**（这里单位是元，不是千元，**坑！跟 index_daily 不同**） |
| `swing` | float | `3.22` | 振幅 % |
| `turnover_rate` | float | `5.56` | 换手率 % |
| `category` | str | `"概念板块"` | 板块类别 |

**5 日累计涨幅算法**: 拉 `start_date=pretrade_date_5` 到 `end_date=trade_date` 共 5~6 个交易日，按 `pct_change` 累加（注意：精确做法是 `(close_T / close_T-5 - 1) * 100`，简化做法是 5 日 pct_change 相加，误差 < 0.5%）。

---

### 3.5 limit_list_d — 涨跌停 + 炸板

**参数**: `trade_date`, `limit_type`（`U`涨停 / `D`跌停 / `Z`炸板）  
**默认字段**: `trade_date, ts_code, industry, name, close, pct_chg, amount, limit_amount, float_mv, total_mv, turnover_ratio, fd_amount, first_time, last_time, open_times, up_stat, limit_times, limit`

#### 涨停（U）字段说明

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"000409.SZ"` | |
| `industry` | str | `"IT服务Ⅱ"` | **申万行业，不是题材**（题材看 `kpl_list.theme`） |
| `name` | str | `"云鼎科技"` | |
| `pct_chg` | float | `10.05` | 涨跌幅 % |
| `amount` | float | `110704226.0` | 成交额 **元** |
| `limit_amount` | float / null | `null` | 涨停项 limit_amount 总为 null（跌停项才有） |
| `fd_amount` | float | `69386000.0` | **封单金额 元**（涨停核心字段） |
| `float_mv` | float | `6205980365.84` | 流通市值 元 |
| `total_mv` | float | `7123157881.12` | 总市值 元 |
| `turnover_ratio` | float | `1.78` | 换手率 % |
| `first_time` | str | `"92500"` | **首次涨停时间** HHMMSS（如 `"92500"` = 09:25:00） |
| `last_time` | str | `"92500"` | 末次涨停时间 |
| `open_times` | int | `0` | **炸板次数**（涨停期间被砸开几次） |
| `up_stat` | str | `"3/5"` | **涨停周期**（最近 N 个交易日内涨停 M 次） |
| `limit_times` | int | `1` | 当日涨停次数（开板再封会 ≥1） |
| `limit` | str | `"U"` | 涨停类型 |

**数据规模 20260525**: U=90 条（不含 ST）

#### 炸板（Z）字段差异

- `fd_amount` 通常为 null（炸板没有最终封单）
- `last_time` 为 null（没有最终涨停时刻）
- `first_time` 有值（首次涨停时刻）
- `limit_times` 为 null
- 数据规模 20260525: Z=34 条

#### 跌停（D）字段差异

- `limit_amount` 有值（**跌停剩余卖单金额**）
- `fd_amount` 有值（跌停封单金额）
- `first_time` 为 null，`last_time` 有值（首次跌停 → 锁死时刻）
- 数据规模 20260525: D=15 条

#### 计算指标

```
封板率 = U_count / (U_count + Z_count)                       # 实测 = 90 / (90+34) = 72.6%
炸板率 = Z_count / (U_count + Z_count)                       # 实测 = 34 / (90+34) = 27.4%
晋级率 = T 日连板数(status>=2连板) / T-1 日涨停总数            # 需要 T-1 日 kpl_list 配合
```

---

### 3.6 kpl_list — 开盘啦涨停榜 ⭐ 核心

**参数**: `trade_date`, `tag="涨停"`（默认）  
**默认字段** (28 个): `ts_code, name, trade_date, lu_time, ld_time, open_time, last_time, lu_desc, tag, theme, net_change, bid_amount, status, bid_change, bid_turnover, lu_bid_vol, pct_chg, bid_pct_chg, rt_pct_chg, limit_order, amount, turnover_rate, free_float, lu_limit_order`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"600396.SH"` | |
| `name` | str | `"华电辽能"` | |
| `lu_time` | str | `"14:46:22"` | **涨停时刻 HH:MM:SS**（注意：跟 limit_list_d 的 first_time HHMMSS 格式不同）|
| `last_time` | str | `"14:46:40"` | 末次涨停时刻 |
| `open_time` | str / null | `null` | 炸板时刻（开板时间） |
| `ld_time` | str / null | `null` | 跌停时间（涨停榜里此字段为 null） |
| `lu_desc` | str | `"通用"` | 涨停描述（"通用"/"首板"/"换手"/...） |
| `tag` | str | `"涨停"` | 榜单类型 |
| `theme` | str | `"色金属·铜电缆设备"` | **题材标签**（用 `·` 分隔多个题材） |
| `status` | str | `"首板"` / `"2连板"` / `"3连板"` / `"4连板"` | 🌟 **连板梯队直接读这个字段** |
| `net_change` | float | `245871741.0` | 主力净流入 元 |
| `limit_order` | float | `88923816.0` | 封单金额 元（跟 limit_list_d.fd_amount 一致） |
| `lu_limit_order` | float | `143159536.0` | 涨停时刻的封单金额 |
| `amount` | float | `4258079379.0` | 成交额 元 |
| `turnover_rate` | float | `40.64` | 换手率 % |
| `free_float` | float | `10831437588.0` | 流通市值 元 |
| `bid_amount` / `bid_change` / `bid_turnover` / `lu_bid_vol` / `bid_pct_chg` / `rt_pct_chg` | -- | **大部分 null** | 竞价数据，盘后接口此段缺失，忽略 |
| `pct_chg` | null | `null` | ⚠️ **此字段在 kpl_list 中常为 null**，涨跌幅请从 limit_list_d 或 daily 取 |

**数据规模 20260525**: 128 条（含连板和首板，跟 limit_list_d 的 U 数有差异因为统计口径不同：kpl_list 含 ST + 北交所，limit_list_d 不含 ST）

**连板梯队 group by 结果**（20260525 实测）:
- `"4连板"`: 1 条（四环生物，电网设备）
- `"3连板"`: 1 条（恒林股份）
- `"2连板"`: 约 29 条
- `"首板"`: 约 97 条

---

### 3.7 moneyflow_hsgt — 北向资金 ⚠️ 有延迟

**参数**: `trade_date` 或 `start_date`+`end_date`  
**默认字段**: `trade_date, ggt_ss, ggt_sz, hgt, sgt, north_money, south_money`

| 字段 | 类型 | 实测样本（20260522） | 说明 |
|------|------|---------------------|------|
| `hgt` | str | `"148079.68"` | 沪股通净流入 **万元** |
| `sgt` | str | `"184497.33"` | 深股通净流入 万元 |
| `north_money` | str | `"332577.01"` | 北向资金净流入 万元（= hgt + sgt，约 33.26 亿） |
| `ggt_ss` | str | `"30340.01"` | 港股通（上海）万元 |
| `ggt_sz` | str | `"23417.57"` | 港股通（深圳）万元 |
| `south_money` | str | `"53757.58"` | 南向资金净流入 万元 |

**⚠️ 重大注意**:
- **20260525 当天查询返回空 `[]`**
- 20260522（往前一个交易日）查询正常
- **结论**：北向 / 南向接口**有 1~2 个交易日的延迟**（具体时点未知）
- **fetcher 必须做兜底**：先查 trade_date，返回空则查 pretrade_date，并在 quality 报告里告知"北向数据为 T-1 日"

**单位**: **万元（不是元也不是亿元）**

---

### 3.8 moneyflow_ind_dc — 板块主力资金流 ⭐ 10000+ 全字段

**参数**: `trade_date`, `content_type="概念"` / `行业` / `地域`  
**默认字段** (18 个): `trade_date, content_type, ts_code, name, pct_change, close, net_amount, net_amount_rate, buy_elg_amount, buy_elg_amount_rate, buy_lg_amount, buy_lg_amount_rate, buy_md_amount, buy_md_amount_rate, buy_sm_amount, buy_sm_amount_rate, buy_sm_amount_stock, rank`

| 字段 | 类型 | 实测样本（半导体设备 第 1 名） | 说明 |
|------|------|--------------------------|------|
| `ts_code` | str | `"BK0917.DC"` | 板块代码（**跟 dc_index 同源**，可 join） |
| `name` | str | `"半导体设备"` | |
| `pct_change` | float | `3.0` | 板块涨跌幅 % |
| `close` | float | `3194.1` | 板块收盘点位 |
| `net_amount` | float | `24704892928.0` | **主力净流入 元**（约 247 亿） |
| `net_amount_rate` | float | `2.65` | 净流入率 % |
| `buy_elg_amount` | float | `22660812800.0` | **超大单净额 元** |
| `buy_lg_amount` | float | `2044080128.0` | **大单净额 元** |
| `buy_md_amount` | float | `-15837294592.0` | **中单净额 元**（负值常见） |
| `buy_sm_amount` | float | `-8871088128.0` | **小单净额 元** |
| `buy_*_amount_rate` | float | `2.43` | 各档资金率 % |
| `buy_sm_amount_stock` | str | `"中芯国际"` | **小单买入最多的股票** |
| `rank` | int | `1` | 排名（按 net_amount 降序） |

**数据规模 20260525**: 486 条（与 dc_index 一致，**可直接 join**）

**单位**: 全部 **元**（注意跟 hsgt 万元不同）

**前 5 名实测（20260525）**:
1. 半导体设备 +247.05 亿
2. (其他可查 rank=2~5)

---

### 3.9 top_list — 龙虎榜个股 ⚠️ 含可转债

**参数**: `trade_date`（必填）  
**默认字段** (15 个): `trade_date, ts_code, name, close, pct_change, turnover_rate, amount, l_sell, l_buy, l_amount, net_amount, net_rate, amount_rate, float_values, reason`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"000510.SZ"` | |
| `name` | str | `"新金路"` | |
| `pct_change` | float | `9.9732` | 当日涨跌幅 % |
| `amount` | float | `3135876147.0` | 当日成交额 元 |
| `l_buy` | float | `410432400.0` | **龙虎榜买入额** 元 |
| `l_sell` | float | `351136826.0` | **龙虎榜卖出额** 元 |
| `l_amount` | float | `761569226.0` | 龙虎榜总成交 元 |
| `net_amount` | float | `59295574.0` | **龙虎榜净买入** 元 |
| `net_rate` | float | `1.89` | 净买入率 % |
| `amount_rate` | float | `24.29` | 龙虎榜占当日成交比 % |
| `float_values` | float | `9966608740.56` | 流通市值 元 |
| `reason` | str | `"连续三个交易日内，涨幅偏离值累计达到20%的证券"` | **上榜原因** |

**⚠️ 注意**:
- **同一只股票可能多次出现**（一个原因一行，比如同时满足"涨幅偏离 + 换手率"会出 2 行）
- **含可转债**（ts_code 前缀 `11/12`，如 `118057.SH`、`123118.SZ`）
- 含 B 股（`900xxx.SH/SZ`）

**过滤建议**:
```python
def is_main_board_stock(ts_code: str) -> bool:
    """只保留 A 股主板/中小板/创业板/科创板/北交所"""
    prefix = ts_code.split(".")[0]
    if prefix.startswith(("0", "3", "6", "8", "9")) and not prefix.startswith("11") and not prefix.startswith("12"):
        # 排除可转债 11x/12x
        if prefix.startswith("900"):  # B 股
            return False
        return True
    return False
```

---

### 3.10 top_inst — 龙虎榜机构席位 ⭐ 游资识别核心

**参数**: `trade_date`（必填）  
**默认字段**: `trade_date, ts_code, exalter, side, buy, buy_rate, sell, sell_rate, net_buy, reason`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"000026.SZ"` | 股票代码 |
| `exalter` | str | `"机构专用"` / `"东方财富证券股份有限公司拉萨金珠西路第一证券营业部"` | **席位全名/营业部全名** |
| `side` | str | `"0"` / `"1"` | **`"0"`=买方席位 / `"1"`=卖方席位** |
| `buy` | float | `79494057.0` | 该席位买入额 元 |
| `sell` | float | `64765159.0` | 该席位卖出额 元 |
| `net_buy` | float | `14728898.0` | 该席位净买入 元 |
| `buy_rate` | float | `2.95` | 买入占当日成交比 % |
| `sell_rate` | float | `2.40` | 卖出占当日成交比 % |
| `reason` | str | `"连续三个交易日内，涨幅偏离值累计达到20%的证券"` | 上榜原因 |

**数据规模 20260525**: **1010 条**（80 只票上榜 × 平均 5 买 5 卖 + "机构专用"合并）

**游资识别策略**（V2 已对齐）:
1. 维护 `dim_trader_alias` 表（DB，取代旧 `config/trader_seat_aliases.json`），GUI 可编辑
2. fetcher 输出原始 exalter；merger 渲染时映射别名
3. AI enricher 兜底：未匹配的 exalter 让 AI 判断是不是知名游资

---

### 3.11 daily — 个股行情（支持多 ts_code）

**参数**: `ts_code`（**支持逗号分隔多股**） + `trade_date`，或 `ts_code` + `start_date`+`end_date`  
**默认字段**: `ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount`

| 字段 | 类型 | 实测样本（中芯国际） | 说明 |
|------|------|---------------------|------|
| `close` | float | `156.0` | 收盘价 元 |
| `pct_chg` | float | `18.7847` | 涨跌幅 **%** |
| `vol` | float | `2521802.26` | 成交量 **手** |
| `amount` | float | `37253708.024` | 成交额 **千元**（与 index_daily 一致） |

**关键**: `ts_code` 字段支持逗号分隔多股（如 `"688981.SH,002156.SZ,000988.SZ"`），单次调用拉一批龙头股，节省积分。

---

### 3.12 daily_basic — 个股基本面

**参数**: `ts_code` + `trade_date`  
**默认字段** (17 个): `ts_code, trade_date, close, turnover_rate, turnover_rate_f, volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, free_share, total_mv, circ_mv`

| 字段 | 实测样本（中芯国际） | 说明 |
|------|---------------------|------|
| `turnover_rate` | `12.6118` | 换手率 % |
| `turnover_rate_f` | `14.0129` | 自由流通换手率 % |
| `volume_ratio` | `1.92` | **量比** |
| `pe_ttm` | `247.7529` | PE-TTM |
| `pb` | `8.3445` | PB |
| `total_mv` | `125005451.7036` | 总市值 **万元** |
| `circ_mv` | `31193175.7644` | 流通市值 **万元** |

**⚠️ 单位坑**: 这里 `total_mv` 单位是**万元**，跟 `dc_index.total_mv` 也是万元一致，但跟 `kpl_list.free_float` 和 `top_list.float_values` 的"元"不同。

---

### 3.13 cls_stock_shock — 财联社涨停个股 🌟 金矿

**参数**: `trade_date`  
**默认字段**: `ts_code, name, trade_date, type, type_name, change, lu_num, reason, plate, is_st, time`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"000409.SZ"` | |
| `name` | str | `"云鼎科技"` | |
| `type` | str | `"up_pool"` / `"continuous_up_pool"` / `"open_up_pool"`(炸板) / ... | 池子类型 |
| `type_name` | str | `"涨停池"` / `"连板"` | 中文池名 |
| `change` | str | `"0.1005"` | 涨跌幅（小数，**不是 %**，跟其他接口不一样） |
| `lu_num` | str | `"1"` / `"2"` | 连板数 |
| `reason` | str | `"智能矿山+机器人\|1. 公司智能矿山系统..."` | **🌟 财联社人工写的涨停原因 + 公司主营** |
| `plate` | str | `"[{\"secu_code\":\"cls80069\",\"secu_name\":\"工业互联网\",\"change\":0.0095},{...}]"` | **🌟 JSON 字符串：该股关联的所有 CLS 板块** |
| `is_st` | str | `"0"` / `"1"` | 是否 ST |
| `time` | str | `"2026-05-25 09:25:00"` | 涨停事件时间 |

**数据规模 20260525**: 220 条（约等于全部涨停 + 部分炸板/跌停）

**用途**:
1. 取代 AI 推理"涨停原因"——`reason` 字段已有人工总结
2. 板块映射——`plate` JSON 含该股关联的 CLS 板块代码 + 板块涨幅
3. 跨板块共振分析——同一只票出现在多个 plate 中

**代码示例（解析 plate）**:
```python
import json
plate_list = json.loads(record["plate"])
for p in plate_list:
    secu_code = p["secu_code"]      # "cls80069"
    secu_name = p["secu_name"]      # "工业互联网"
    plate_change = p["change"]       # 0.0095 (小数)
```

**⚠️ CLS 板块代码体系跟 dc_index 不同**:
- CLS: `cls80069` / `80457.CLS`（财联社）
- dc_index: `BK0917.DC`（东财）
- 没有官方映射表，**靠板块名匹配**（不严格）

---

### 3.14 cls_market_shock — 财联社板块异动

**参数**: `trade_date`  
**默认字段**: `ts_code, c_time, trade_date, name, status`

| 字段 | 类型 | 实测样本 | 说明 |
|------|------|---------|------|
| `ts_code` | str | `"80457.CLS"` | CLS 板块代码 |
| `name` | str | `"芯片产业链"` | 板块名 |
| `c_time` | str | `"2026-05-25 14:51:05"` | **异动时间**（精确到秒） |
| `status` | str | `"up"` / `"down"` | 上涨异动 / 下跌异动 |

**数据规模 20260525**: 22 条

**用途**: "盘中哪些板块发动了"——可用于盘后报告的「板块异动时间线」补充。

**注意**: 同一板块可能多次异动（如"芯片产业链"出现 3 次：09:45 / 13:04 / 14:51），渲染时合并或保留最早一次。

---

## 4. 单位/口径速查表（重要！避免代码出 bug）

| 接口字段 | 单位 | 备注 |
|---------|------|------|
| `index_daily.amount` | **千元** | 除以 10^5 = 亿元 |
| `index_daily.pct_chg` | **%** | |
| `dc_index.total_mv` | **万元** | |
| `dc_index.pct_change` | **%** | |
| `dc_daily.amount` | **元** | ⚠️ 跟 index_daily 不同 |
| `limit_list_d.fd_amount` | **元** | |
| `limit_list_d.amount` | **元** | |
| `limit_list_d.total_mv` | **元** | ⚠️ 跟 daily_basic 的 total_mv 万元不同 |
| `kpl_list.limit_order` | **元** | |
| `kpl_list.amount` | **元** | |
| `kpl_list.free_float` | **元** | |
| `moneyflow_hsgt.north_money` | **万元** | 字符串类型，需 float() |
| `moneyflow_ind_dc.net_amount` | **元** | |
| `top_list.l_buy` / `l_sell` / `net_amount` | **元** | |
| `top_inst.buy` / `sell` / `net_buy` | **元** | |
| `daily.amount` | **千元** | 跟 index_daily 一致 |
| `daily_basic.total_mv` / `circ_mv` | **万元** | |
| `cls_stock_shock.change` | **小数** | `0.1005` 表示 10.05% |
| `cls_stock_shock.plate[].change` | **小数** | 同上 |

**建议**: 所有数值在 fetcher 内部**统一归一化到"亿元"和"%"**，避免下游计算出错。

---

## 5. 时间格式差异表

| 字段 | 格式 | 示例 |
|------|------|------|
| `limit_list_d.first_time` / `last_time` | **HHMMSS**（无分隔） | `"92500"` = 09:25:00 |
| `kpl_list.lu_time` / `last_time` / `open_time` | **HH:MM:SS**（含分隔） | `"14:46:22"` |
| `cls_stock_shock.time` | **YYYY-MM-DD HH:MM:SS** | `"2026-05-25 09:25:00"` |
| `cls_market_shock.c_time` | **YYYY-MM-DD HH:MM:SS** | `"2026-05-25 14:51:05"` |
| `trade_cal.cal_date` | **YYYYMMDD** | `"20260525"` |
| `daily.trade_date` 等所有 trade_date | **YYYYMMDD** | `"20260525"` |

**建议**: fetcher 内部统一转为 `datetime` 对象，渲染时再格式化。

---

## 6. 各接口缺陷/坑列表

| 接口 | 坑 | 应对 |
|------|----|------|
| `moneyflow_hsgt` | T 日数据延迟 | 兜底用 `pretrade_date`，在 quality 标注"北向 = T-1 日数据" |
| `dc_index` | `level` 字段**仅行业板块有值**（早期审计误判全 null，2026-05-28 纠正） | 概念/地域板块直接忽略 level；行业板块用 `level='东财一级行业'` 取 31 行做行业大类锚点 |
| `dc_hot` | `hot` / `concept` 字段总 null | V1 不用此接口 |
| `kpl_list` | `pct_chg` / 竞价相关字段大量 null | 涨跌幅从 limit_list_d 或 daily 取 |
| `top_list` | 包含可转债、B 股 | 按 ts_code 前缀过滤 |
| `top_list` | 同股可能多行（多个上榜原因） | 按 ts_code group 合并，reason 拼接 |
| `cls_stock_shock.plate` | CLS 板块代码与 dc_index 不互通 | 靠板块名匹配（不严格） |
| `cls_market_shock` | 同板块可能多次异动 | 合并或保留最早一次 |
| `limit_list_d.industry` | 是申万行业，不是题材 | 题材取 `kpl_list.theme` 或 `cls_stock_shock.plate` |

---

## 7. 给 TushareMarketFetcher 的实现建议（关键决策）

### 7.1 接口调用顺序（最少 9 次 API call 完成全部 L1+L3）

```
1.  trade_cal(start=trade_date, end=trade_date)            # 拿 pretrade_date
2.  index_daily(ts_code="000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000852.SH,000688.SH")
                                                            # 7 个指数单次调用
                                                            # ⚠️ 实测 index_daily 单 ts_code 调用，多 ts_code 需循环或并发
3.  dc_index(trade_date, idx_type="概念板块")              # 486 条
4.  moneyflow_ind_dc(trade_date, content_type="概念")      # 486 条
5.  limit_list_d(trade_date, limit_type="U")               # 90 条
6.  limit_list_d(trade_date, limit_type="Z")               # 34 条
7.  limit_list_d(trade_date, limit_type="D")               # 15 条
8.  kpl_list(trade_date, tag="涨停")                       # 128 条
9.  moneyflow_hsgt(trade_date)                             # T 日空 → 兜底 pretrade_date
10. top_list(trade_date)                                   # 80+ 条
11. top_inst(trade_date)                                   # 1010 条
12. cls_stock_shock(trade_date)                            # 220 条
13. cls_market_shock(trade_date)                           # 22 条
14. dc_daily(ts_code=top_5_板块代码, start=pretrade_5, end=trade_date)
                                                            # 仅 top 5 板块算 5 日涨幅
15. daily(ts_code=top_5_板块_龙头, trade_date)              # 龙头股行情，单次多 ts_code
16. daily_basic(ts_code=top_5_板块_龙头, trade_date)        # 龙头基本面
```

**索引调用方式**: `index_daily` 实测**不支持多 ts_code 逗号分隔**（descriptor 单数形式 `ts_code`），需循环 7 次或并发。

**总计**: 约 15~20 次 API call / 每个交易日。10000+ 积分日限额绰绰有余。

### 7.2 缓存设计（V2 已修正：纯数据入库）

> ⚠️ V1 设计的"文件缓存"已被取代。**V2 决策**：全部入 `data/market.db` 的 `fact_*` 表，每个 fact 表都有 `raw_json` 列兜底；不再写 `data/tushare_cache/` 文件。
>
> 入库逻辑：
> - 默认：检查 `fact_index_daily` 是否已有该 `trade_date` → 命中则跳过
> - `force_refresh=True`：删除该 `trade_date` 的所有 `fact_*` 行后重拉
>
> 以下示意（按 API 一一对应）仅供参考，**实际不会生成这些文件**：

| API → 入库表对照 | 表 |
|---|---|
| trade_cal | dim_trade_calendar |
| index_daily × 7 | fact_index_daily |
| dc_index | dim_sector (upsert) |
| moneyflow_ind_dc + dc_daily 派生 | fact_sector_daily |
| limit_list_d (U/Z/D) + kpl_list | fact_limit_stock |
| moneyflow_hsgt | fact_hsgt_daily |
| top_list | fact_top_list |
| top_inst | fact_top_inst |
| cls_stock_shock | fact_cls_stock_shock |
| cls_market_shock | fact_cls_market_shock |
| daily / daily_basic（leaders 详情） | 内存计算，仅用于 sectors_top[].leaders 渲染 |

幂等：同一天 trade_date 重复 build → 命中 `market_summaries` 主键，不重复扣积分。

### 7.3 派生指标算法（基于实测数据验证）

```python
# 封板率
def seal_rate(u_count, z_count):
    total = u_count + z_count
    return u_count / total if total > 0 else 0.0
# 实测 20260525: 90 / (90+34) = 0.726

# 晋级率（需 T-1 日 kpl_list）
def promotion_rate(kpl_today, kpl_prev):
    # T-1 日涨停股
    prev_limit_codes = {k["ts_code"] for k in kpl_prev if k["status"] != "首板"
                        and int(k["status"].replace("连板","").replace("首板","0") or 0) >= 1}
    # 简化：T-1 任意涨停的股
    prev_codes = {k["ts_code"] for k in kpl_prev}
    # T 日成功连板（status >= 2连板，且 ts_code 在 T-1 涨停名单中）
    today_promoted = {k["ts_code"] for k in kpl_today
                      if "连板" in (k["status"] or "") and k["ts_code"] in prev_codes}
    return len(today_promoted) / len(prev_codes) if prev_codes else 0.0

# 连板梯队
def limit_ladder(kpl_list):
    ladder = {"4连板及以上": [], "3连板": [], "2连板": [], "首板": []}
    for k in kpl_list:
        status = k["status"] or "首板"
        if status == "首板":
            ladder["首板"].append(k)
        elif status == "2连板":
            ladder["2连板"].append(k)
        elif status == "3连板":
            ladder["3连板"].append(k)
        else:
            ladder["4连板及以上"].append(k)
    return ladder
```

### 7.4 字段映射建议（MarketSummary JSON 字段 → Tushare 来源）

| MarketSummary 字段 | Tushare 来源 | 处理 |
|--------------------|--------------|------|
| `indices.sh.close` | `index_daily(000001.SH).close` | 直接 |
| `indices.sh.pct_chg` | `index_daily(000001.SH).pct_chg` | 直接 |
| `indices.total_turnover_yi` | `index_daily.amount` 4 大指数累加 | 千元 → 亿元 (/10^5) |
| `breadth.limit_up` | `limit_list_d(U).count()` | |
| `breadth.failed_limit` | `limit_list_d(Z).count()` | |
| `sentiment.seal_rate` | 派生（见 7.3） | |
| `sentiment.max_height` | `max(kpl_list.status 中的数字)` | |
| `sentiment.max_stock` | `kpl_list` 中 status 最高的 name | |
| `capital_flow.north_net_yi` | `moneyflow_hsgt.north_money` | 万元 → 亿元 (/10^4) |
| `sectors_top[].name` | `dc_index.name`（按 pct_change 排序） | |
| `sectors_top[].pct_chg` | `dc_index.pct_change` | |
| `sectors_top[].pct_chg_5d` | `dc_daily` 5 日累加（仅 top 板块） | |
| `sectors_top[].main_net_yi` | `moneyflow_ind_dc.net_amount` (join ts_code) | 元 → 亿元 (/10^8) |
| `sectors_top[].leaders[]` | `dc_index.leading` + `dc_index.leading_code` | |
| `sectors_top[].limit_up_count` | `kpl_list` 按 theme 包含 sector name 计数 | 关键词匹配 |
| `sectors_top[].catalysts` | `cls_stock_shock.reason`（按 plate 匹配板块） | 🌟 |
| `limit_ladder.*` | `kpl_list` group by status | |
| `dragon_tiger.stocks[]` | `top_list`（过滤可转债） | |
| `dragon_tiger.famous_traders[]` | `top_inst` + 别名表 | AI 兜底未匹配 |
| `regulation[]` | `cls_market_shock` 中 status 异常的板块 | |

---

## 8. 还要测试的（V1 落地前的最后兜底）

- [ ] **跨天连续**：拉 20260518~20260525 一周数据，验证缓存命中、增量逻辑、5 日涨幅计算
- [ ] **空数据日**：找一个数据稀疏的日子（休市次日清晨）测试，看接口返回 `[]` 时 fetcher 是否正确兜底
- [ ] **超大涨停日**：找一个涨停 > 150 只的日子（如某次政策刺激），测试 kpl_list 是否限频/截断
- [ ] **moneyflow_hsgt 延迟边界**：测试到底延迟 1 天还是 2 天（在 T 日 17:00、22:00、次日 9:00 分别查 trade_date=T）
- [ ] **CLS 板块名 vs dc_index 名 的匹配率**：跑一次全量匹配，看模糊匹配能命中多少

**这些 V1 实施第一周做即可**，本审计已经把"主路径"打通。

---

## 9. 一句话总结

**10000+ 积分下，盘后数据 L1+L2 几乎全部硬数据，L3 由财联社的 `cls_stock_shock.reason` 兜底了大半；之前文档假设的"AI 大量推理"路径可以**砍掉 70%**。`TushareMarketFetcher` 按本审计第 7 节的字段映射写一次过——无需返工。**

---

## 10. limit_list_ths 字段块（2026-05-27 18:10 增补）

> 三源融合 v1 实测，参考 [doc/updates/05-27-1810-涨停三源融合上线.md](../doc/updates/05-27-1810-涨停三源融合上线.md)

### 10.1 接口定位

- **同花顺涨跌停榜单接口**，文档：<https://tushare.pro/document/2?doc_id=355>
- **历史范围**：从 20231101 起
- **更新时机**：每天 16 点左右（**比 limit_list_d / kpl_list 早 ~12 小时**，是 T 日盘后唯一可见涨停数据的来源）
- **积分要求**：8000+
- **限速**：每分钟 500 次

### 10.2 5 个泳池

| limit_type 中文 | 5/26 行数 | 业务对应 |
|----------------|-----------|---------|
| 涨停池 | 64 | `fact_limit_stock.limit_type='U'`（主源）|
| 跌停池 | 34 | `fact_limit_stock.limit_type='D'`（主源）|
| 炸板池 | 30 | `fact_limit_stock.limit_type='Z'`（主源）|
| 连扳池 | 28 | 涨停池子集，仅取 tag 字段更新（"7天5板"间断梯队）|
| 冲刺涨停 | 17 | `fact_limit_sprint`（独立表，不属涨停股）|

### 10.3 涨停池字段实测（5/26，64 行）

| 字段 | 类型 | 命中率 | 业务含义 | 三源对账 |
|------|------|-------|---------|---------|
| `ts_code` | str | 100% | 股票代码 | 三源一致 |
| `name` | str | 100% | 股票名称 | 三源一致 |
| `price` | float | 100% | 收盘价 | = d.close ✅ |
| `pct_chg` | float | 100% | 涨跌幅 % | = d.pct_chg ✅ |
| `limit_amount` | float | 100% | 封单额（元）| = d.fd_amount × 1e8 ✅ |
| `limit_order` | float | 100% | 封单量（股）| **同花顺独家**（kpl/d 无）|
| `turnover` | float | 100% | 成交额（元）| = d.amount ✅ |
| `turnover_rate` | float | 100% | 换手率 % | = d.turnover_ratio ✅ |
| `free_float` | float | 100% | 实际流通市值（元）| = d.float_mv ✅ |
| `status` | str | 100% | 涨停形态（"换手板"/"T字板"/"一字板"）| **vs kpl.status="3连板" 不同维度** |
| `tag` | str | 100% | 高度 tag（"3天3板"/"7天5板"/"首板"）| **同花顺独家** ⭐ |
| `lu_desc` | str | 100% | 涨停原因（"摘帽+智能安防+智慧停车+低空经济"）| **同花顺独家** ⭐ 文本质量碾压 kpl.theme |
| `limit_up_suc_rate` | float | 97% | 一年封板率 0~1 | **同花顺独家** ⭐ |
| `market_type` | str | 100% | HS/GEM/STAR 板块分类 | **同花顺独家** |
| `open_num` | int | 56% | 炸板次数 | vs d.limit_times（100%）后者更全 |

**默认不返回的字段**（需在 fields 参数指定）：`first_lu_time` / `last_lu_time` / `first_ld_time` / `last_ld_time` / `rise_rate` / `sum_float` / `lu_limit_order`。

### 10.4 关键反一致性发现（重要！）

实测 5/26：

```
limit_list_d  涨停 = 46 只  ← 长期漏算 18 只（28%）
kpl_list      涨停 = 64 只
limit_list_ths 涨停 = 64 只  ← 与 kpl 一致

三方交集 = 46（即 limit_list_d 的 46 是 kpl/ths 的真子集）
```

→ **东财 `limit_list_d` 系统性漏 18 只**（多为 ST 股、ST 摘帽股、低价股），口径偏窄。三源融合后取 ths∪kpl 并集（实际 = ths 64 只）。

### 10.5 冲刺涨停字段实测（5/26，17 行）

| 字段 | 命中率 | 备注 |
|------|-------|------|
| `ts_code` / `name` | 100% | |
| `price` / `pct_chg` | 100% | 涨幅通常 9.x%~19.x%（含科创/创业 20% 极限）|
| `turnover_rate` / `turnover` / `free_float` | 100% | |
| `market_type` | 100% | HS / GEM / STAR |
| `rise_rate` | **0%** | Tushare 不返回此字段（接口 quirk）|
| `lu_desc` | **0%** | 接口不返回（因为还没真涨停）|
| `tag` / `status` | 100% | 但语义为"冲刺涨停"非高度 |

### 10.6 三源融合字段优先级（COALESCE 顺序）

| 字段 | 优先级 | 已落实 |
|------|--------|--------|
| `close` | ths > d | `tushare_fetcher._merge_ths_into_limit_stock` |
| `pct_chg` | ths > d | 同上 |
| `theme` | kpl 独家 | `_merge_kpl_into_limit_stock` |
| `lu_desc` | ths 独家 | `_merge_ths_into_limit_stock` |
| `tag` | ths 独家（连扳池版本最新）| `_merge_ths_lianban_tag` |
| `cons_nums` | parse(kpl.status) > parse(ths.tag) > 历史递归 | `metrics.parse_cons_nums` |
| `limit_up_time` | kpl.lu_time（带冒号）> d.first_time | 不变 |
| `industry` | d 独家 | `_ingest_limit_stock` |
| `total_mv` | d 独家 | 同上 |

---

*本审计是 [盘后数据自动化-施工方案.md](../doc/design/05-26-2126-盘后数据自动化施工方案.md) 的字段层依据。*  
*施工时遇到任何字段不一致 → 以本审计为准；本审计未列的字段 → 不要瞎用。*
