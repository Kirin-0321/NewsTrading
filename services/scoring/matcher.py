"""股票代码规范化 + 板块名模糊匹配。

提供三个纯函数（不依赖 PyQt，可在 services / tools / tests 复用）：

    normalize_stock_code(raw_code, raw_name=None) -> Optional[str]
        统一为 NNNNNN.{SH|SZ|BJ}。
        优先级：
            1) dim_stock 精确匹配（按 6 位代码或带后缀代码反查，最稳）
            2) 已带后缀且合法 -> 直接返回
            3) 6 位纯数字 -> 按首位规则推断后缀
            4) name 反查 dim_stock 兜底
            5) 否则 None

    match_sector_ts_code(theme_name, db=None) -> Tuple[Optional[str], float]
        题材名 -> dim_sector.ts_code, confidence ∈ [0, 1]
        匹配策略（按置信度从高到低）：
            完全相等 -> 1.0
            theme_name in sector.name -> 0.8
            sector.name in theme_name -> 0.7
            字符 2-gram Jaccard 相似度 -> 阈值 0.5，比例直接给
            < 0.5 -> (None, score)  保留 score 供 GUI 提示

    enrich_themes_with_matcher(themes) -> List[Dict]
        批量富化：给每个题材落 sector_ts_code/sector_match_conf；
                  给每个 stock 落 normalized_code。
        失败仍保留原数据（NULL 列），不抛异常。

规则参考表（normalize_stock_code 后缀推断）:
    600~603 / 605 / 688 / 689 -> SH
    000 / 001 / 002 / 003 / 300 / 301 -> SZ
    400 / 43x / 83x~88x / 92x -> BJ
    9 / 7 开头 -> 暂不支持（B 股 / 配股）-> None
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


_SH_PREFIXES = ("600", "601", "602", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
_BJ_PREFIXES = (
    "400", "430", "830", "831", "832", "833", "834",
    "835", "836", "837", "838", "839", "870", "871",
    "872", "873", "874", "920",
)
_VALID_SUFFIXES = ("SH", "SZ", "BJ")


def normalize_stock_code(
    raw_code: Optional[str],
    raw_name: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """详见模块 docstring。

    Args:
        raw_code: AI 抽取的原始代码（可能带后缀 / 纯数字 / 加 SH/SZ 前缀）
        raw_name: 标的名称，dim_stock 反查兜底用
        conn: 可选的 market.db 连接（批量调用时复用，避免反复开连接）

    Returns:
        如 "600172.SH" 或 None
    """
    code = (str(raw_code).strip().upper().replace(" ", "")) if raw_code else ""
    name = (str(raw_name).strip()) if raw_name else ""

    # === 1. dim_stock 精确反查（最稳，N5 修复优先级） ===
    if code:
        looked = _lookup_dim_stock(code=code, conn=conn)
        if looked:
            return looked

    # === 2. 已带 .SH/.SZ/.BJ 后缀 ===
    if "." in code:
        digits, _, suffix = code.partition(".")
        if suffix in _VALID_SUFFIXES and digits.isdigit() and len(digits) == 6:
            return f"{digits}.{suffix}"

    # === 3. SH600172 / SZ300750 形式 ===
    for prefix in _VALID_SUFFIXES:
        if code.startswith(prefix) and len(code) == 8 and code[2:].isdigit():
            return f"{code[2:]}.{prefix}"

    # === 4. 6 位纯数字 -> 按首位规则推断 ===
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
        # 9 / 7 开头 B 股 / 配股 不支持

    # === 5. name 兜底反查 ===
    if name:
        return _lookup_dim_stock(name=name, conn=conn)

    return None


def _lookup_dim_stock(
    *,
    code: Optional[str] = None,
    name: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """按 code 或 name 反查 dim_stock.ts_code。

    code 优先尝试完全匹配，再尝试 6 位数字模糊匹配（自动加后缀通配）。
    """
    if not code and not name:
        return None

    owned_conn = False
    if conn is None:
        from services.market.market_db import get_market_db
        db = get_market_db()
        ctx = db.connect(readonly=True)
        conn = ctx.__enter__()
        owned_conn = True
        _ctx_for_close = ctx
    else:
        _ctx_for_close = None

    try:
        if code:
            if "." in code:
                row = conn.execute(
                    "SELECT ts_code FROM dim_stock WHERE ts_code = ? LIMIT 1",
                    (code,),
                ).fetchone()
                if row:
                    return str(row[0])
            elif code.isdigit() and len(code) == 6:
                row = conn.execute(
                    "SELECT ts_code FROM dim_stock "
                    "WHERE ts_code LIKE ? LIMIT 1",
                    (f"{code}.%",),
                ).fetchone()
                if row:
                    return str(row[0])

        if name:
            row = conn.execute(
                "SELECT ts_code FROM dim_stock WHERE name = ? LIMIT 1",
                (name,),
            ).fetchone()
            if row:
                return str(row[0])
    except Exception as e:
        _log.warning("dim_stock 查询失败: %s", e)
    finally:
        if owned_conn and _ctx_for_close is not None:
            try:
                _ctx_for_close.__exit__(None, None, None)
            except Exception:
                pass

    return None


def match_sector_ts_code(
    theme_name: str,
    *,
    sectors: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[Optional[str], float]:
    """题材名 -> 板块代码。详见模块 docstring。

    Args:
        theme_name: 如「先进封装」
        sectors: 可选预加载的 [(ts_code, name), ...]，批量调用时复用

    Returns:
        (ts_code, confidence) 元组；置信度 < 0.5 时 ts_code = None
    """
    if not theme_name:
        return None, 0.0

    if sectors is None:
        from services.market.market_db import get_market_db
        db = get_market_db()
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT ts_code, name FROM dim_sector WHERE name IS NOT NULL"
            ).fetchall()
            sectors = [(r[0], r[1]) for r in rows]

    if not sectors:
        return None, 0.0

    theme = theme_name.strip()
    best_code: Optional[str] = None
    best_conf: float = 0.0

    for ts_code, sec_name in sectors:
        if not sec_name:
            continue
        conf = _score_match(theme, sec_name)
        if conf > best_conf:
            best_code, best_conf = ts_code, conf
            if best_conf >= 0.999:
                break

    if best_conf < 0.5:
        return None, round(best_conf, 3)
    return best_code, round(best_conf, 3)


def _score_match(theme: str, sector_name: str) -> float:
    """计算两个中文字符串的匹配置信度。"""
    if theme == sector_name:
        return 1.0
    if theme in sector_name:
        return 0.8
    if sector_name in theme:
        return 0.7
    t_grams = {theme[i:i + 2] for i in range(len(theme) - 1)}
    s_grams = {sector_name[i:i + 2] for i in range(len(sector_name) - 1)}
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

    失败保持原值（None），不抛异常。整段如果连 market.db 都连不上，则只写 None。
    """
    if not themes:
        return themes

    sectors: Optional[List[Tuple[str, str]]] = None
    conn: Optional[sqlite3.Connection] = None
    _ctx = None

    try:
        from services.market.market_db import get_market_db
        db = get_market_db()
        _ctx = db.connect(readonly=True)
        conn = _ctx.__enter__()
        rows = conn.execute(
            "SELECT ts_code, name FROM dim_sector WHERE name IS NOT NULL"
        ).fetchall()
        sectors = [(r[0], r[1]) for r in rows]
    except Exception as e:
        _log.warning("matcher 预加载 dim_sector 失败，将全部留空: %s", e)
        sectors = []

    try:
        for theme in themes:
            name = (theme.get("theme_name") or "").strip()
            best_code, best_conf = match_sector_ts_code(name, sectors=sectors)
            theme["sector_ts_code"] = best_code
            theme["sector_match_conf"] = best_conf

            for stock in theme.get("stocks") or []:
                stock["normalized_code"] = normalize_stock_code(
                    stock.get("code") or stock.get("stock_code"),
                    stock.get("name") or stock.get("stock_name"),
                    conn=conn,
                )
    finally:
        if _ctx is not None:
            try:
                _ctx.__exit__(None, None, None)
            except Exception:
                pass

    return themes
