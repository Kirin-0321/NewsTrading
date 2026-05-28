# _施工_抽取保存GUI补全（合并 v4 一次升级）

> 创建：2026-05-27 10:03  
> 主文档：[../doc/design/05-27-1003-题材抽取保存与GUI补全设计.md](../doc/design/05-27-1003-题材抽取保存与GUI补全设计.md)  
> 替代：[_施工_题材强度带符号化.md](_施工_题材强度带符号化.md)（v3 内容合并到本文档，旧文件完工后删）  
> 完工后：本文件留作回溯，主人验收完可删

---

## 0. 前置约束（已决策）

### v3 决策（上次）
- 清空 theme_* 三表（走 schema 指纹机制）
- 删 `sentiment` 字段
- `strength_score` 改 -100 ~ +100
- `strength_level` 扩展 9 档枚举

### v4 决策（本次）
- **B1**：`theme_predictions` 加 `prompt_id` + `prompt_version` 列（冗余自 ai_reports）
- **B2**：`theme_stocks` 加 `normalized_code` 列（matcher 规范化后的代码）
- **B3**：`theme_predictions` 加 `sector_ts_code` + `sector_match_conf` 列（matcher 板块匹配结果）
- **D1**：GUI 形态 = 题材预测页 Tab + 独立模板评估页 双布局
- **C2 / C4**：题材预测页加打分明细 Tab + 模板筛选下拉
- **新建文件**：`services/scoring/matcher.py` 提前到本次（原 plan M2 提前到 M1.5）

**合并施工**：v3 + v4 = 一次 DROP 重建 + 一次抽取流程改造 + GUI 一次铺完。

---

## 1. 改动顺序

```
Step 1: services/scoring/__init__.py + services/scoring/matcher.py  新建（先有工具）
Step 2: prompts/theme_extraction/extract_themes.md     v2 模板（schema 改 + 删 sentiment）
Step 3: core/theme_extractor.py                        校验 + enrich_with_matcher
Step 4: services/storage/database.py                   v4 schema + 指纹 _THEME_V4_COLUMNS
Step 5: services/storage/theme_store.py                normalize 升级 + 反查 prompt_id + 写新列
Step 6: gui/pages/theme_prediction_page.py             删情绪列 + 加色标 + 加 4 Tab + 加模板筛选
Step 7: gui/pages/prompt_eval_page.py                  新建独立页（占位 + 桩数据展示）
Step 8: gui/main_window.py                             注册新页 + 加导航项
Step 9: tools/_debug_theme_extract.py                  跟随更新（打印 sector_ts_code 等）
Step 10: doc/design/05-26-2126-NewsTrading架构方案.md   第 580 / 601 行更新规范描述
Step 11: 备份 news.db + 实测 + GUI 验证
Step 12: 删除 _施工_题材强度带符号化.md + 更新 .huiye/README.md
```

依赖链：Step 1 是后续 enrich/保存依赖；Step 2-5 是数据流闭环；Step 6-8 是 GUI；Step 9-12 是收尾。

---

## 2. 逐文件改动清单

### 2.1 [services/scoring/__init__.py](../services/scoring/__init__.py) + [services/scoring/matcher.py](../services/scoring/matcher.py) 新建

**`__init__.py`：空文件或暴露 `__all__`**

```python
"""services.scoring —— 题材回测打分子模块。

模块组成（按依赖顺序，后续逐步引入）：
    matcher.py         代码规范化 + 板块名模糊匹配（本次落地）
    script_scorer.py   脚本规则打分（plan M3.2）
    ai_scorer.py       AI D+5 总评（plan M3.3）
    scoring_service.py 服务入口（plan M3.4）
"""
__all__ = ["matcher"]
```

**`matcher.py`：完整接口**

```python
"""股票代码规范化 + 板块名模糊匹配。

提供两个纯函数（不依赖 PyQt，可在 services / tools / tests 复用）：

    normalize_stock_code(raw_code, raw_name=None) -> Optional[str]
        统一为 NNNNNN.{SH|SZ|BJ}。
        优先级：
            1) 已带后缀且合法 -> 直接返回
            2) 6 位纯数字 -> 按首位规则推断后缀
            3) 推断不出但有 name -> 走 dim_stock 反查兜底
            4) 否则 None
    
    match_sector_ts_code(theme_name, db=None) -> Tuple[Optional[str], float]
        题材名 -> dim_sector.ts_code, confidence ∈ [0, 1]
        匹配策略（按置信度从高到低）：
            完全相等 -> 1.0
            theme_name in sector.name -> 0.8
            sector.name in theme_name -> 0.7
            词袋 Jaccard 相似度 -> 0.5 阈值，比例直接给
            < 0.5 -> (None, score)

    enrich_themes_with_matcher(themes) -> List[Dict]
        批量富化：给每个题材落 sector_ts_code/sector_match_conf；
                  给每个 stock 落 normalized_code。
        失败仍保留原数据（NULL 列），不抛异常。

规则参考表（normalize_stock_code）:
    600~603 -> SH（上证主板）
    605     -> SH（上证主板 N 系列）
    688     -> SH（科创板）
    000     -> SZ（深圳主板）
    002 ~ 003 -> SZ（深圳中小板/主板）
    300 / 301 -> SZ（创业板）
    400/8/87 -> BJ（北交所）
    9       -> B 股（暂不支持，返回 None）
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple


_SH_PREFIXES = ("600", "601", "602", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
_BJ_PREFIXES = ("400", "430", "830", "831", "832", "833", "834",
                "835", "836", "837", "838", "839", "870", "871",
                "872", "873", "874", "920")  # 北交所代码段较多，按需扩展


def normalize_stock_code(
    raw_code: Optional[str],
    raw_name: Optional[str] = None,
) -> Optional[str]:
    """详见模块 docstring。"""
    if not raw_code:
        return _try_name_lookup(raw_name) if raw_name else None
    code = str(raw_code).strip().upper().replace(" ", "")
    
    # 已带后缀
    if "." in code:
        digits, _, suffix = code.partition(".")
        if suffix in ("SH", "SZ", "BJ") and digits.isdigit() and len(digits) == 6:
            return f"{digits}.{suffix}"
    
    # SH600172 / SZ300750 形式
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix) and len(code) == 8 and code[2:].isdigit():
            return f"{code[2:]}.{prefix}"
    
    # 6 位纯数字
    if code.isdigit() and len(code) == 6:
        for sp in _SH_PREFIXES:
            if code.startswith(sp):
                return f"{code}.SH"
        for sp in _SZ_PREFIXES:
            if code.startswith(sp):
                return f"{code}.SZ"
        for sp in _BJ_PREFIXES:
            if code.startswith(sp):
                return f"{code}.BJ"
        return None  # 9 开头 B 股等
    
    # 兜底用 name 反查
    return _try_name_lookup(raw_name) if raw_name else None


def _try_name_lookup(name: str) -> Optional[str]:
    """走 dim_stock 反查 ts_code（按完整名称）。"""
    from services.market.db_helper import get_market_db  # 复用现有 helper
    if not name:
        return None
    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT ts_code FROM dim_stock WHERE name = ? LIMIT 1",
                (name,),
            ).fetchone()
            return str(row[0]) if row else None
    except Exception:
        return None


def match_sector_ts_code(
    theme_name: str,
    db=None,
) -> Tuple[Optional[str], float]:
    """详见模块 docstring。"""
    if not theme_name:
        return None, 0.0
    if db is None:
        from services.market.db_helper import get_market_db
        db = get_market_db()
    
    with db.connect(readonly=True) as conn:
        sectors = conn.execute(
            "SELECT ts_code, name FROM dim_sector"
        ).fetchall()
    
    if not sectors:
        return None, 0.0
    
    theme = theme_name.strip()
    best_code, best_conf = None, 0.0
    
    for ts_code, name in sectors:
        if not name:
            continue
        conf = _score_match(theme, name)
        if conf > best_conf:
            best_code, best_conf = ts_code, conf
    
    if best_conf < 0.5:
        return None, best_conf
    return best_code, best_conf


def _score_match(theme: str, sector_name: str) -> float:
    """两个字符串匹配置信度。"""
    if theme == sector_name:
        return 1.0
    if theme in sector_name:
        return 0.8
    if sector_name in theme:
        return 0.7
    # 词袋 Jaccard（基于字符 2-gram）
    t_grams = {theme[i:i+2] for i in range(len(theme) - 1)}
    s_grams = {sector_name[i:i+2] for i in range(len(sector_name) - 1)}
    if not t_grams or not s_grams:
        return 0.0
    inter = len(t_grams & s_grams)
    union = len(t_grams | s_grams)
    return inter / union if union else 0.0


def enrich_themes_with_matcher(themes: List[Dict]) -> List[Dict]:
    """批量富化 themes（in-place 修改后返回同一 list）。
    
    对每个 theme：
        - 补 sector_ts_code / sector_match_conf
    对每个 stock：
        - 补 normalized_code
    
    失败保持原值（None），不抛异常。
    """
    from services.market.db_helper import get_market_db
    db = get_market_db()
    
    # 预加载 sectors 减少查询次数
    with db.connect(readonly=True) as conn:
        sectors = conn.execute(
            "SELECT ts_code, name FROM dim_sector"
        ).fetchall()
    sector_list = [(r[0], r[1]) for r in sectors if r[1]]
    
    for theme in themes:
        # 板块匹配
        name = theme.get("theme_name") or ""
        best_code, best_conf = None, 0.0
        for ts_code, sec_name in sector_list:
            conf = _score_match(name, sec_name)
            if conf > best_conf:
                best_code, best_conf = ts_code, conf
        if best_conf >= 0.5:
            theme["sector_ts_code"] = best_code
            theme["sector_match_conf"] = round(best_conf, 3)
        else:
            theme["sector_ts_code"] = None
            theme["sector_match_conf"] = round(best_conf, 3)
        
        # 标的代码规范化
        for stock in theme.get("stocks") or []:
            stock["normalized_code"] = normalize_stock_code(
                stock.get("code"),
                stock.get("name"),
            )
    
    return themes
```

> **注意**：`services.market.db_helper` 路径要核实，应该是 [gui/utils/market_db_helper.py](../gui/utils/market_db_helper.py) 还是 [services/storage/database.py](../services/storage/database.py)？  
> 实际：`market.db` 的 helper 在 `gui/utils/market_db_helper.py::get_market_db()`。GUI utils 被 services 调用违反分层 → **施工时应把 get_market_db 提到 services 层**，或者直接用 sqlite3.connect 走 `data/market.db` 路径。  
> 推荐：施工时把 `get_market_db` 移到 `services/market/db.py`，gui 层改成 re-export 兼容。

---

### 2.2 [prompts/theme_extraction/extract_themes.md](../prompts/theme_extraction/extract_themes.md)

**头部 version 改 v2.0**：

```yaml
---
id: extract_themes
name: '题材结构化抽取'
category: theme_extraction
version: '2.0'             # 1.0 -> 2.0
description: '从 AI 分析报告中抽取结构化题材列表（v2: strength_score 带符号 / 删 sentiment / 接入打分回测）'
provider_default: deepseek
model_default: deepseek-v4-pro
temperature_default: 0.2
---
```

**第 23-24 行（schema）改为**：

```
"strength_score": -100 到 +100 整数（必填）。
    正数（+1~+100）= 利好（绝对值越大越强）
    负数（-1~-100）= 利空（绝对值越大越强）
    接近 0（-19~+19）= 中性 / 钝化
"strength_level": 9 档枚举（必填，可由 strength_score 派生）：
    极强利好/强利好/中等利好/弱利好/中性/弱利空/中等利空/强利空/极强利空
```

**第 28 行**：完全删除 `"sentiment": ...`

**第 35 行（stock.code 说明保留可选，但加一行）**：

```
"code": "如 '601689.SH'（强烈建议提供，下游打分用；不确定可省略）",
```

**第 53-55 行（抽取规则 3-5）改为**：

```
3. 强度评分参考（按绝对值）：
   极强 80-100 / 强 60-79 / 中 40-59 / 弱 20-39
   中性 -19~+19（钝化题材或方向不明显）
   利空请填负数到对应区间（如强利空 = -79 ~ -60）
4. strength_score 与 strength_level 必须自洽：
   level 由 score 区间自动派生，AI 写错时系统会按 score 覆盖。
5. 风险提示、钝化题材也要抽：
   利空题材 strength_score 必须为负数；
   钝化但仍利好/利空的题材，is_cold=1 + 对应正/负 strength_score（如已发酵的黄金 = 弱利好 +30）。
```

---

### 2.3 [core/theme_extractor.py](../core/theme_extractor.py)

**Line 34**：

```python
# 旧：
_VALID_LEVELS = {"极强", "强", "中", "弱"}

# 新：
_VALID_LEVELS = {
    "极强利好", "强利好", "中等利好", "弱利好",
    "中性",
    "弱利空", "中等利空", "强利空", "极强利空",
}
```

**Line 35 附近**：删除 `_VALID_SENTIMENT = {...}` 整行常量。

**Line 492**：

```python
# 旧：
"strength_score": pick_int(item.get("strength_score"), 0, 100),

# 新：
"strength_score": pick_int(item.get("strength_score"), -100, 100),
```

**Line 497**：删除 `"sentiment": pick_enum(...)` 整行。

**`extract_from_file` 末尾新增富化步骤（约 Line 100-110 之间）**：

```python
def extract_from_file(self, report_path, progress_callback=None):
    # ... 原有逻辑 ...
    
    themes, news_id_map, err = ...  # 原有产物
    
    # v4 新增：matcher 富化
    if themes:
        try:
            from services.scoring.matcher import enrich_themes_with_matcher
            enrich_themes_with_matcher(themes)
        except Exception as exc:
            # 富化失败不影响入库，但记录警告
            warn = f"matcher 富化失败: {exc}"
            if err:
                err = err + "; " + warn
            else:
                err = warn
    
    return themes, news_id_map, err
```

---

### 2.4 [services/storage/database.py](../services/storage/database.py)

**Line 34-52（CREATE TABLE theme_predictions）改为 v4**：

```sql
CREATE TABLE IF NOT EXISTS theme_predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           TEXT NOT NULL,
    report_date         TEXT NOT NULL,
    report_time         TEXT,
    report_path         TEXT NOT NULL,
    theme_name          TEXT NOT NULL,
    theme_category      TEXT,
    strength_score      INTEGER NOT NULL,
    strength_level      TEXT NOT NULL,
    priority_rank       INTEGER,
    duration            TEXT,
    expectation_gap     TEXT,
    is_cold             INTEGER NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL,
    risk_note           TEXT,
    -- v4 新增：B1 + B3
    prompt_id           TEXT,
    prompt_version      TEXT,
    sector_ts_code      TEXT,
    sector_match_conf   REAL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theme_report_date  ON theme_predictions(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_theme_name         ON theme_predictions(theme_name);
CREATE INDEX IF NOT EXISTS idx_theme_strength     ON theme_predictions(report_date, strength_score DESC);
CREATE INDEX IF NOT EXISTS idx_theme_category     ON theme_predictions(theme_category);
CREATE INDEX IF NOT EXISTS idx_theme_report_id    ON theme_predictions(report_id);
-- v4 新增索引
CREATE INDEX IF NOT EXISTS idx_theme_prompt_date  ON theme_predictions(prompt_id, report_date);
CREATE INDEX IF NOT EXISTS idx_theme_sector_date  ON theme_predictions(sector_ts_code, report_date);
```

**Line 59-71（theme_stocks）改为 v4**：

```sql
CREATE TABLE IF NOT EXISTS theme_stocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    stock_name      TEXT NOT NULL,
    stock_code      TEXT,
    normalized_code TEXT,                  -- v4 新增
    role            TEXT,
    reason          TEXT,
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_theme    ON theme_stocks(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_name     ON theme_stocks(stock_name);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_code     ON theme_stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_normcode ON theme_stocks(normalized_code);  -- v4 新增
```

**Line 115-128（指纹 v2 → v4）**：

```python
# 旧（删）：
_THEME_V2_COLUMNS = { ... }

# 新：
_THEME_V4_COLUMNS = {
    "theme_predictions": {
        "id", "report_id", "report_date", "report_time", "report_path",
        "theme_name", "theme_category", "strength_score", "strength_level",
        "priority_rank", "duration", "expectation_gap",
        "is_cold", "reason", "risk_note",
        "prompt_id", "prompt_version", "sector_ts_code", "sector_match_conf",
        "created_at",
    },
    "theme_stocks": {
        "id", "theme_id", "stock_name", "stock_code", "normalized_code",
        "role", "reason",
    },
    "theme_news": {
        "id", "theme_id", "news_ref", "news_id", "relation_type",
    },
}
```

**Line 176（引用处）**：`_THEME_V2_COLUMNS.items()` → `_THEME_V4_COLUMNS.items()`

---

### 2.5 [services/storage/theme_store.py](../services/storage/theme_store.py)

**Line 21-26（`_LEVEL_RANGE` 重建）**：

```python
_LEVEL_RANGE: List[Tuple[str, int, int]] = [
    ("极强利好", 80, 100),
    ("强利好", 60, 79),
    ("中等利好", 40, 59),
    ("弱利好", 20, 39),
    ("中性", -19, 19),
    ("弱利空", -39, -20),
    ("中等利空", -59, -40),
    ("强利空", -79, -60),
    ("极强利空", -100, -80),
]
```

**Line 28**：删 `_VALID_SENTIMENT = {...}`

**Line 33-38（`_score_to_level`）**：

```python
def _score_to_level(score: int) -> str:
    """-100~+100 按区间映射 9 档。"""
    for level, lo, hi in _LEVEL_RANGE:
        if lo <= score <= hi:
            return level
    return "中性"
```

**Line 41-84（`_normalize_theme`）**：

修改 3 处：
- Line 60：`score = max(-100, min(100, score))`
- Line 67-68：`if score is None: score = 0`（默认中性）
- Line 73-75：完全删除 sentiment 3 行
- **新增**：兼容 matcher 富化后的字段，原样 pass through `sector_ts_code` / `sector_match_conf` / `stocks[].normalized_code`

**Line 93-130（`save_themes` 入口处）新增 prompt_id 反查**：

```python
def save_themes(self, report_meta, themes, news_id_map=None):
    # ... 入参校验保持 ...
    
    # v4 新增：反查 prompt_id / prompt_version
    prompt_id = None
    prompt_version = None
    try:
        from services.storage import get_ai_reports_store
        rec = get_ai_reports_store().get_by_path(report_path)
        if rec is not None:
            prompt_id = rec.prompt_id
            prompt_version = rec.prompt_version
    except Exception:
        pass  # 反查失败不影响入库
    
    # ... 后续保持 ...
```

**Line 144-147（INSERT SQL）改为 v4**：

```python
INSERT INTO theme_predictions
(report_id, report_date, report_time, report_path,
 theme_name, theme_category,
 strength_score, strength_level,
 priority_rank, duration, expectation_gap,
 is_cold,
 reason, risk_note,
 prompt_id, prompt_version,
 sector_ts_code, sector_match_conf,
 created_at)
VALUES (?, ?, ?, ?,  ?, ?,  ?, ?,  ?, ?, ?,  ?,  ?, ?,  ?, ?,  ?, ?,  ?)
```

**Line 158-163（VALUES 元组）**：

```python
(
    report_id, report_date, report_time, report_path,
    theme_name, t.get("theme_category"),
    t["strength_score"], t["strength_level"],
    t.get("priority_rank"), t.get("duration"), t.get("expectation_gap"),
    t["is_cold"],
    reason, t.get("risk_note"),
    prompt_id, prompt_version,
    t.get("sector_ts_code"), t.get("sector_match_conf"),
    created_at,
)
```

**Line 177-190（INSERT theme_stocks）改为 v4**：

```python
INSERT INTO theme_stocks
(theme_id, stock_name, stock_code, normalized_code, role, reason)
VALUES (?, ?, ?, ?, ?, ?)

# values:
(
    theme_id,
    name,
    stock.get("code") or stock.get("stock_code"),
    stock.get("normalized_code"),  # v4 新增
    stock.get("role"),
    stock.get("reason"),
)
```

**Line 252 SELECT**：去 `sentiment,`；其他 SELECT 全字段 `SELECT *` 不受影响。

---

### 2.6 [gui/pages/theme_prediction_page.py](../gui/pages/theme_prediction_page.py)

#### 2.6.1 列定义 v4（Line 36-47）

```python
_THEME_TABLE_COLS = [
    ("时间", 56),
    ("题材", 200),
    ("等级", 80),                 # 9 档字符串变长，加宽
    ("分数", 60),
    # ("情绪", 60),                 # 删
    ("冷处理", 60),
    ("持续性", 70),
    ("预期差", 70),
    ("排名", 50),
    ("综合脚本分", 80),           # 新增（C1）
    ("模板", 90),                 # 新增（看模板）
    ("核心逻辑（含催化）", 260),
]
```

#### 2.6.2 `_LEVEL_ORDER`（Line 58）

```python
_LEVEL_ORDER = {
    "极强利好": 4, "强利好": 3, "中等利好": 2, "弱利好": 1,
    "中性": 0,
    "弱利空": -1, "中等利空": -2, "强利空": -3, "极强利空": -4,
}
```

#### 2.6.3 `_render_theme_table`（Line 439-453）

```python
def _render_theme_table(self, themes: list):
    self.theme_table.setRowCount(len(themes))
    for row, t in enumerate(themes):
        score = t.get("strength_score") or 0
        cells = [
            t.get("report_time") or "",
            t.get("theme_name") or "",
            t.get("strength_level") or "",
            str(score),
            # "情绪" 列已删
            "❄️" if t.get("is_cold") else "",
            t.get("duration") or "",
            t.get("expectation_gap") or "",
            str(t.get("priority_rank") or ""),
            self._format_score_script(t),    # 新增（C1）
            (t.get("prompt_id") or "—")[:18],  # 新增
            t.get("reason") or "",
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            self.theme_table.setItem(row, col, item)
            # 分数列正负色标（C1）
            if col == 3:  # 分数列
                if score > 0:
                    item.setForeground(QColor("#d32f2f"))  # 红正
                elif score < 0:
                    item.setForeground(QColor("#1976d2"))  # 蓝负
```

#### 2.6.4 详情区第 4 个 Tab（Line 315-318）

```python
self.detail_tab.addTab(self.reason_browser, "📝 逻辑/原因")
self.detail_tab.addTab(self.stock_table, "💼 关联标的")
self.detail_tab.addTab(self.news_splitter, "📰 关联新闻")
self.detail_tab.addTab(self._build_score_detail(), "📈 打分明细")  # 新增（C2）
```

新增方法 `_build_score_detail()`：

```python
def _build_score_detail(self) -> QWidget:
    """打分明细 Tab 内容（一期占位 + 桩数据）。"""
    w = QWidget()
    layout = QVBoxLayout(w)
    
    # 顶部：5 天涨跌曲线（一期用简单 QLabel/Table 占位，二期接 QChart）
    self.score_chart_placeholder = QLabel("（5 天涨跌曲线占位，打分系统上线后渲染）")
    self.score_chart_placeholder.setMinimumHeight(200)
    self.score_chart_placeholder.setStyleSheet(
        "border: 1px dashed #d9d9d9; background: #fafafa; color: #8c8c8c;"
    )
    self.score_chart_placeholder.setAlignment(Qt.AlignCenter)
    layout.addWidget(self.score_chart_placeholder)
    
    # 中部：标的逐只打分表
    self.score_stock_table = QTableWidget(0, 7)
    self.score_stock_table.setHorizontalHeaderLabels([
        "标的", "代码", "D+1", "D+2", "D+3", "D+4", "D+5",
    ])
    self.score_stock_table.setStyleSheet(TABLE_STYLE)
    self.score_stock_table.setEditTriggers(QTableWidget.NoEditTriggers)
    self.score_stock_table.verticalHeader().setVisible(False)
    layout.addWidget(self.score_stock_table)
    
    # 底部：AI 点评（折叠）
    self.score_ai_comment = QTextBrowser()
    self.score_ai_comment.setStyleSheet(TEXTBROWSER_STYLE)
    self.score_ai_comment.setPlaceholderText(
        "AI D+5 综合点评（打分系统未上线时显示此提示）"
    )
    self.score_ai_comment.setMaximumHeight(140)
    layout.addWidget(self.score_ai_comment)
    
    return w
```

新增方法 `_format_score_script(theme)`：

```python
def _format_score_script(self, theme: dict) -> str:
    """读 theme_prediction_scores 取综合分；未打分返回 '—'。"""
    try:
        from services.storage.database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                """SELECT score_script FROM theme_prediction_scores
                   WHERE theme_id = ? AND day_offset = 5""",
                (theme.get("id"),),
            ).fetchone()
        return f"{row[0]:.1f}" if row and row[0] is not None else "—"
    except Exception:
        return "—"
```

#### 2.6.5 模板筛选下拉（C4，Line 234-247）

在控制行加：

```python
ctrl.addWidget(QLabel("模板:"))
self.template_filter_combo = QComboBox()
self.template_filter_combo.setStyleSheet(COMBOBOX_STYLE)
self.template_filter_combo.setMinimumWidth(140)
self.template_filter_combo.addItem("全部模板", None)
# 后续在 _reload_dates 中 distinct prompt_id 填充
self.template_filter_combo.currentIndexChanged.connect(self._on_template_filter_changed)
ctrl.addWidget(self.template_filter_combo)
```

新增方法 `_on_template_filter_changed`：

```python
def _on_template_filter_changed(self):
    self._reload_themes_for_date()
```

`_reload_themes_for_date` 内 SQL 加 prompt_id 过滤（按 self.template_filter_combo.currentData() 判断）。

---

### 2.7 [gui/pages/prompt_eval_page.py](../gui/pages/prompt_eval_page.py) 新建

```python
"""模板评估页 —— 按 prompt_id 横向对比预测准确度。

上半区: 控制条（时间范围 + 模板多选 + 指标）
下半区: 左大表格 + 右折线图
底部:   重打分按钮 + 导出 CSV

数据来源: theme_prediction_scores JOIN theme_predictions 按 prompt_id 聚合
一期: 桩数据展示（打分系统未上线时表格全空，按钮带禁用 tooltip）
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QTableWidget, QTableWidgetItem, QSplitter, QGroupBox, QMessageBox,
)
from gui.utils.styles import (
    BUTTON_PRIMARY, COMBOBOX_STYLE, TABLE_STYLE,
)


_RANGE_OPTIONS = [
    ("最近 7 天", 7),
    ("最近 30 天", 30),
    ("最近 90 天", 90),
    ("自定义", None),
]

_METRIC_OPTIONS = [
    ("综合脚本分", "score_script"),
    ("T+1 涨幅", "t1_pct"),
    ("T+5 涨幅", "t5_pct"),
    ("Alpha", "alpha"),
    ("命中率", "hit_rate"),
    ("胜率", "win_rate"),
]


class PromptEvalPage(QWidget):
    """模板评估页（C 双布局之独立页）。"""
    
    def __init__(self):
        super().__init__()
        self.init_ui()
        self.refresh()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        title = QLabel("📈 模板评估")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #262626;")
        layout.addWidget(title)
        
        layout.addWidget(self._build_control_bar())
        
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_table_area())
        splitter.addWidget(self._build_chart_area())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        
        layout.addWidget(self._build_bottom_bar())
    
    def _build_control_bar(self) -> QWidget:
        # 时间范围 + 指标切换
        pass
    
    def _build_table_area(self) -> QWidget:
        # 大表格：模板 × {样本数 / T+1 / T+5 / Alpha / 命中率 / 胜率 / 稳定性}
        pass
    
    def _build_chart_area(self) -> QWidget:
        # 折线图（一期占位）
        pass
    
    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.addStretch()
        self.rescore_btn = QPushButton("🔄 重打分")
        self.rescore_btn.setStyleSheet(BUTTON_PRIMARY)
        self.rescore_btn.setEnabled(False)
        self.rescore_btn.setToolTip("打分系统未上线，请见 plan M3")
        h.addWidget(self.rescore_btn)
        self.export_btn = QPushButton("📤 导出 CSV")
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip("打分系统未上线")
        h.addWidget(self.export_btn)
        return bar
    
    def refresh(self):
        # 一期：查 theme_prediction_scores 表是否存在，存在则查询，否则占位
        pass
```

> 完整实现交给施工时补全；本桩文件保证导入不报错 + 主导航能切过去。

---

### 2.8 [gui/main_window.py](../gui/main_window.py)

**Line 22 附近，import 加**：

```python
from gui.pages.prompt_eval_page import PromptEvalPage
```

**Line 89 附近，pages dict 加**：

```python
'theme_prediction': ThemePredictionPage(),
'prompt_eval': PromptEvalPage(),      # 新增
'cleaning': NewsCleaningPage(),
```

**Line 133 附近，nav_items 加**：

```python
('theme_prediction', '🎯 预测题材'),
('prompt_eval', '📈 模板评估'),       # 新增
('schedule', '⏰ 定时任务'),
```

---

### 2.9 [tools/_debug_theme_extract.py](../tools/_debug_theme_extract.py)

跟随：
- 打印输出中 sentiment 字段移除
- 新增打印 sector_ts_code / sector_match_conf / stocks[].normalized_code
- prompt schema 提示更新

---

### 2.10 [doc/design/05-26-2126-NewsTrading架构方案.md](../doc/design/05-26-2126-NewsTrading架构方案.md)

**Line 580 / 601**（按上次 v3 文档同步规范，并增补 v4 新增字段说明）

---

## 2.X Review 阶段补丁（10:18 主人要求补全后追加）

> 主文档 §10 列了 11 个遗漏点，这里给行号级修复方案。**施工时按 §1 主流程顺序走，但在每个 Step 末尾应用对应补丁**。

### P1 路径规范化（严重，必修）

**问题**：ai_reports.file_path = 相对 posix，theme_predictions.report_path = 绝对 Windows 路径，反查 0 命中。

**改 [core/theme_extractor.py:673-707](../core/theme_extractor.py) `parse_report_meta`**：

```python
# 旧（Line 684, 707）：
abspath = os.path.abspath(report_path)
...
"report_path": abspath,

# 新：
from services.storage.ai_reports_store import _to_relative_posix
rel_path = _to_relative_posix(report_path)
...
"report_path": rel_path,
```

**双保险**：在 [services/storage/theme_store.py:save_themes](../services/storage/theme_store.py) 反查 prompt_id 时也调一次规范化：

```python
from services.storage.ai_reports_store import _to_relative_posix, get_ai_reports_store
rec = get_ai_reports_store().get_by_path(_to_relative_posix(report_path))
```

### P2 重复入库防护（严重，必修）

**问题**：同一 md 抽两次，theme_predictions 重复 2 份，打分双倍计入。

**改 [services/storage/theme_store.py:save_themes](../services/storage/theme_store.py)**：

主循环开始前（约 Line 131 `with get_connection() as conn:` 内、`for raw in themes:` 之前）：

```python
with get_connection() as conn:
    # P2: 同 report_id 已存在则先清旧版本，保证幂等
    if report_id:
        conn.execute(
            "DELETE FROM theme_predictions WHERE report_id = ?",
            (report_id,),
        )
        # theme_stocks / theme_news 走 ON DELETE CASCADE 自动清
    
    for raw in themes:
        ...
```

### P3 theme_prediction_scores FK（中等，影响打分系统 plan M3.1）

**问题**：打分表与题材表无外键，删题材时孤儿打分残留。

**plan M3.1 的 DDL 应改为**（本次施工不涉及，但要在 plan 文件里标注）：

```sql
CREATE TABLE IF NOT EXISTS theme_prediction_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    ...
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS theme_stock_scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id     INTEGER NOT NULL,
    ...
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
```

**当前文档动作**：在本施工的 Step 12 "完工后清理"加一项：更新 plan M3.1 DDL 加 FK。

### P4 版本聚合断层（中等）

**问题**：scalper@1.0 跑 5 天 + scalper@2.0 跑 3 天，简单聚合掩盖 v2 改进。

**影响 [gui/pages/prompt_eval_page.py](../gui/pages/prompt_eval_page.py)**：

模板评估页查询 SQL 加 `GROUP BY (prompt_id, prompt_version)`，表格行显示如 `A股短线投机·实战派 @1.0`。控制条加 "忽略版本" checkbox（默认勾选时合并版本，方便长期趋势观察）：

```python
# 一期占位时也加上 checkbox，便于后续接入
self.ignore_version_check = QCheckBox("忽略版本号（合并 v1/v2/v3）")
self.ignore_version_check.setChecked(True)
self.ignore_version_check.stateChanged.connect(self.refresh)
ctrl_layout.addWidget(self.ignore_version_check)
```

### P5 友好 name 显示（中等）

**问题**：主人看 "speculator_scalper" 不如看 "A股短线投机·实战派" 直观。

**新增 helper [gui/utils/prompt_name_helper.py](../gui/utils/prompt_name_helper.py) 或直接 inline**：

```python
from core.prompt_loader import PromptLoader, PromptError

_loader = PromptLoader()

def friendly_prompt_name(
    prompt_id: str | None,
    category: str = "analysis",
    fallback: str = "未知模板",
) -> str:
    """prompt_id -> 友好显示名。
    
    - speculator_scalper -> 'A股短线投机·实战派'
    - None -> fallback
    - 找不到 prompt 文件 -> 原 prompt_id（向后兼容）
    """
    if not prompt_id:
        return fallback
    try:
        return _loader.get(category, prompt_id).name or prompt_id
    except PromptError:
        return prompt_id
```

**应用到**：
- [theme_prediction_page.py](../gui/pages/theme_prediction_page.py) 题材表"模板"列显示 friendly name
- [theme_prediction_page.py](../gui/pages/theme_prediction_page.py) 模板筛选下拉，addItem 时 `(friendly_name, prompt_id)` 二元组（data 仍存 prompt_id 供查询）
- [prompt_eval_page.py](../gui/pages/prompt_eval_page.py) 模板评估页同上

### P6 ThemeStore 组合筛选接口（中等）

**改 [services/storage/theme_store.py:233-244](../services/storage/theme_store.py) `get_by_date`**（追加可选参数）：

```python
def get_by_date(
    self,
    report_date: str,
    prompt_id: Optional[str] = None,   # P6 新增
) -> List[Dict]:
    """按日期取所有题材，可选按模板过滤。"""
    where = "WHERE report_date = ?"
    params: list = [report_date]
    if prompt_id:
        where += " AND prompt_id = ?"
        params.append(prompt_id)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM theme_predictions
            {where}
            ORDER BY report_time DESC, strength_score DESC
            """,
            params,
        ).fetchall()
        return [self._expand(conn, r) for r in rows]
```

**新增 [services/storage/theme_store.py](../services/storage/theme_store.py) `list_distinct_prompts`**：

```python
def list_distinct_prompts(self) -> List[str]:
    """返回所有出现过的 prompt_id（去重），给模板筛选下拉用。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT prompt_id FROM theme_predictions "
            "WHERE prompt_id IS NOT NULL ORDER BY prompt_id"
        ).fetchall()
        return [r[0] for r in rows]
```

### P7 折线图实现（轻微）

**选 PyQtChart**（PyQt5 配套，零新依赖）：

```python
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QValueAxis
from PyQt5.QtGui import QPainter
```

**降级**：如果运行时 `import PyQt5.QtChart` 失败（部分 PyQt5 包没带），fallback 到 QLabel 占位 + 提示 "请安装 PyQtChart 启用折线图"。

### P8 样本量警告（轻微）

**[prompt_eval_page.py](../gui/pages/prompt_eval_page.py) 表格渲染**：

```python
# 样本数列渲染时
sample_count = row_data.get("sample_count", 0)
item = QTableWidgetItem(str(sample_count))
if sample_count < 5:
    item.setBackground(QColor("#fff7e6"))  # 浅橙
    item.setToolTip(f"样本数仅 {sample_count}，建议 ≥ 5 才有统计意义")
```

### P9 AI 评分去重（轻微，影响 plan M3.3）

**影响 ai_scorer.score_theme_d5**（本次不涉及，标记到 plan M3.3）：

同 `(theme_name, report_date)` 已评过则跳过：

```python
def score_theme_d5(self, theme_id):
    # 取 (theme_name, report_date) 唯一键
    theme = ...
    if self._already_ai_scored(theme.theme_name, theme.report_date):
        return  # 跳过 LLM 调用
    ...
```

**当前文档动作**：在本施工的 Step 12 加一项：plan M3.3 ai_scorer 设计加去重检查。

### P10 重打分按钮范围（轻微）

**[prompt_eval_page.py](../gui/pages/prompt_eval_page.py) 重打分按钮 click handler**（一期桩）：

```python
def _on_rescore(self):
    reply = QMessageBox.question(
        self, "确认重打分",
        f"将对【{self._current_filter_desc()}】范围内的题材重跑脚本打分，约耗时 N 分钟，是否继续？",
        QMessageBox.Yes | QMessageBox.No,
    )
    if reply != QMessageBox.Yes:
        return
    # 一期：只显示 "打分系统未上线" 的提示
    QMessageBox.information(self, "提示", "打分系统未上线，请见 plan M3")
```

### P11 services.market.db_helper 路径分层（轻微）

施工文档 §2.1 已明确处理方式：**把 `get_market_db` 上提到 `services/market/db.py`，gui 改 re-export**。本节仅作交叉引用。

---

## 3. 测试用例

### 3.1 matcher 单元测试（新建 [tools/_scratch_matcher_test.py](../tools/_scratch_matcher_test.py)）

```python
from services.scoring.matcher import normalize_stock_code, _score_match

# normalize_stock_code 覆盖
assert normalize_stock_code("600172") == "600172.SH"
assert normalize_stock_code("000001") == "000001.SZ"
assert normalize_stock_code("300750") == "300750.SZ"
assert normalize_stock_code("688981") == "688981.SH"
assert normalize_stock_code("600172.SH") == "600172.SH"
assert normalize_stock_code("SH600172") == "600172.SH"
assert normalize_stock_code("000001.sz") == "000001.SZ"
assert normalize_stock_code(" 600172 ") == "600172.SH"
assert normalize_stock_code("XYZ") is None
assert normalize_stock_code("") is None
assert normalize_stock_code(None) is None

# _score_match 覆盖
assert _score_match("先进封装", "先进封装") == 1.0
assert 0.7 < _score_match("先进封装", "先进封装概念") < 0.85
assert 0.7 < _score_match("先进封装概念", "先进封装") < 0.85
assert _score_match("人形机器人", "工业机器人") > 0
print("[OK] matcher 全部用例通过")
```

### 3.2 schema 升级自动验证

```bash
# 升级前快照
python -c "import sqlite3; c=sqlite3.connect('data/news.db'); print('theme_predictions cols:', [r[1] for r in c.execute('PRAGMA table_info(theme_predictions)')])"

# 触发 init
python -c "from services.storage.database import init_database; init_database()"

# 升级后校验
python -c "import sqlite3; c=sqlite3.connect('data/news.db'); cols=[r[1] for r in c.execute('PRAGMA table_info(theme_predictions)')]; assert 'prompt_id' in cols and 'sector_ts_code' in cols and 'sentiment' not in cols, cols; print('[OK] v4 schema 已应用')"
```

### 3.3 端到端（带 LLM + matcher）

```bash
python tools/_debug_theme_extract.py "data/AI_analysis/5月27日/5月27日_0时39分_盘后总结分析报告.md"
```

检查：
- [ ] 输出 themes 中每个题材有 sector_ts_code（如 "BK0917.DC"）+ sector_match_conf
- [ ] 输出 themes 中每个 stock 有 normalized_code（"600172.SH" 格式）
- [ ] 利空题材 strength_score 为负
- [ ] 入库后 theme_predictions.prompt_id == "speculator_scalper"

### 3.4 GUI 验证

启动主程序：
- [ ] 主导航有"📈 模板评估"
- [ ] 题材预测页表格无"情绪"列、有"综合脚本分"、有"模板"
- [ ] 分数列正数红字 / 负数蓝字
- [ ] 详情区第 4 个 Tab "打分明细" 可切换，显示占位（打分系统未上线）
- [ ] 上半区抽取面板有"模板筛选"下拉

---

## 4. 回滚策略

```bash
# 备份数据库（一行）
copy data\news.db data\news.db.bak.20260527_1003

# 改动失败时
git checkout HEAD~1 -- \
  services/scoring/__init__.py \
  services/scoring/matcher.py \
  prompts/theme_extraction/extract_themes.md \
  core/theme_extractor.py \
  services/storage/database.py \
  services/storage/theme_store.py \
  gui/pages/theme_prediction_page.py \
  gui/pages/prompt_eval_page.py \
  gui/main_window.py \
  tools/_debug_theme_extract.py

# 恢复数据库
copy data\news.db.bak.20260527_1003 data\news.db
```

---

## 5. 风险点

| 风险 | 处理 |
|------|------|
| matcher 引入 dim_sector 查询，启动时 dim_sector 可能为空（首次运行） | 失败保持 None，不影响主流程；下次拉过盘后数据后自动补 |
| `services.market.db_helper` 路径冲突（gui/utils 反向被 services 引用） | 抽取时把 helper 上提到 `services/market/db.py`，gui 改 re-export |
| AI 仍输出 `sentiment` 字段 | 校验阶段直接 drop，不入库 |
| 题材 prompt_id 反查失败 | NULL 不报错，下游 prompt 评估时归入 "未知" 分组 |
| GUI 第 4 个 Tab 加载慢（每次切题材查 theme_prediction_scores） | 一期同步查询；二期改成异步 + cache |
| 模板评估页 nav 加多了，侧栏挤 | 当前 9 项 + 新增 1 = 10 项，仍在合理范围；后续可分组 |

---

## 6. 时间预估

| 步骤 | 预估 |
|------|------|
| Step 1 matcher 模块 | 25 分钟（含单测） |
| Step 2 prompt v2 | 8 分钟 |
| Step 3 theme_extractor + enrich 调用 | 8 分钟 |
| Step 4 database v4 schema | 8 分钟 |
| Step 5 theme_store 反查 + 写新列 | 15 分钟 |
| Step 6 theme_prediction_page 改造 | 25 分钟 |
| Step 7 prompt_eval_page 新建（占位+桩） | 20 分钟 |
| Step 8 main_window 注册 | 3 分钟 |
| Step 9 _debug 工具 | 3 分钟 |
| Step 10 架构文档同步 | 5 分钟 |
| Step 11 实测 + GUI 验证 | 15 分钟 |
| Step 12 文档收尾 | 5 分钟 |
| **总计** | **约 140 分钟（2.3 小时）** |

---

## 7. 完工后清理

- [ ] 删除本文件
- [ ] 删除 [_施工_题材强度带符号化.md](_施工_题材强度带符号化.md)（v3 已合并到 v4）
- [ ] 更新 [.huiye/README.md](README.md) 索引
- [ ] 更新 plan 文件 M2 状态（matcher 已落地 → 划掉 M2.1 todo）
- [ ] **更新打分系统 plan M3.1 DDL**：加 `FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE`（补丁 P3）
- [ ] **更新打分系统 plan M3.3 ai_scorer 设计**：加 `(theme_name, report_date)` 去重检查（补丁 P9）
- [ ] **跑回归脚本** `python tools/test_ai_sector_code_accuracy.py` 确认 AI 板块代码准确率仍 < 50%（若已超过，开 ticket 重新评估 A2 决策）
