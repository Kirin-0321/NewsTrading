"""CLS 数据集成（Phase M2）—— catalysts 字段主路径。

从 ``fact_cls_stock_shock`` 表读取财联社的「涨停股催化原因 + 关联板块」，
把它们映射到 ``sectors_top`` 里的板块，作为 ``sectors_top[].catalysts``
字段填进 MarketSummary。

这是「砍掉 70% AI 推理」的核心，详见 ``doc/reports/05-26-2126-Tushare接口字段审计.md`` §13：

* ``cls_stock_shock.reason`` 是财联社人工写的涨停原因（不用 AI 推理）。
* ``cls_stock_shock.plate`` 是 JSON 字符串，含该股关联的所有 CLS 板块。
* 80~90% 的热门板块都能匹配到 catalysts；剩下的交给 ``AIEnricher`` 兜底。

板块名三级匹配::

    1. 精确：sector.name == cls_plate.secu_name
    2. 包含：双向 substring
    3. 关键词：同义词分组（``THEME_KEYWORDS``）
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from services.market.market_db import MarketDB, get_market_db

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 板块同义词关键词字典（第三级匹配用）
# ---------------------------------------------------------------------------
# 维护原则：
# * 每行一个题材簇，左边是规范名，右边是「常见板块名片段」列表
# * 关键词应该是【最短且有辨识度的子串】，避免误命中
# * 大小写不敏感
THEME_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("半导体", ["半导体", "芯片", "晶圆", "光刻", "封测", "存储", "IC"]),
    ("机器人", ["机器人", "人形机器人", "具身智能", "灵巧手", "减速器"]),
    ("算力", [
        "算力", "数据中心", "IDC", "服务器", "光模块", "CPO", "液冷",
    ]),
    ("AI应用", [
        "AI应用", "人工智能", "大模型", "AIGC", "Sora",
        "AI智能体", "多模态",
    ]),
    ("电网", ["电网", "智能电网", "特高压", "电力设备", "配电"]),
    ("新能源汽车", [
        "新能源汽车", "锂电", "动力电池", "固态电池", "钠离子",
        "电池回收", "充电桩",
    ]),
    ("光伏", ["光伏", "TOPCon", "HJT", "钙钛矿", "BC电池"]),
    ("风电", ["风电", "海风", "海上风电"]),
    ("军工", ["军工", "国防", "航空发动机", "导弹", "卫星导航", "无人机"]),
    ("低空经济", ["低空", "eVTOL", "通用航空", "飞行汽车"]),
    ("固态电池", ["固态电池", "半固态", "硫化物电解质"]),
    ("创新药", ["创新药", "ADC", "CXO", "GLP", "减肥药"]),
    ("猪肉", ["猪肉", "生猪", "养殖"]),
    ("白酒", ["白酒", "酿酒"]),
    ("券商", ["券商", "证券"]),
    ("银行", ["银行"]),
    ("地产", ["地产", "房地产"]),
    ("游戏", ["游戏", "网游", "手游"]),
    ("传媒", ["传媒", "影视", "出版"]),
    ("化工", ["化工", "化学制品", "纯碱"]),
    ("稀土", ["稀土", "永磁"]),
    ("黄金", ["黄金", "贵金属"]),
    ("华为", ["华为", "鸿蒙", "昇腾", "鲲鹏", "麒麟"]),
    ("苹果", ["苹果", "AppleVision", "MR"]),
    ("可控核聚变", ["核聚变", "可控核聚变", "聚变"]),
    ("商业航天", ["商业航天", "火箭", "卫星互联网", "星网"]),
    ("减肥药", ["减肥药", "司美格鲁肽", "GLP"]),
    ("脑机接口", ["脑机", "脑机接口", "BCI"]),
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class CLSPlateAggregate:
    """单个 CLS 板块名的聚合结果（中间产物）。"""

    plate_name: str
    secu_codes: Set[str] = field(default_factory=set)
    reasons: List[str] = field(default_factory=list)
    ts_codes: Set[str] = field(default_factory=set)

    def add(self, secu_code: str, reason: str, ts_code: str) -> None:
        self.secu_codes.add(secu_code)
        if ts_code:
            self.ts_codes.add(ts_code)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class CLSEnrichResult:
    """``CLSEnricher.enrich`` 的返回。

    Attributes:
        catalysts_by_sector: dc 板块名 → 该板块的 catalysts（去重后的 reason 列表）
        unmatched_sectors:   未命中的 dc 板块名列表（交给 AI 兜底）
        match_sources:       dc 板块名 → 命中方式（exact / contains / keyword）
        plate_aggregates:    CLS 板块名 → 聚合中间结果（调试用）
        cls_market_shock:    板块异动时间线（dc 板块名 → [(time, status, count), ...]）
    """

    catalysts_by_sector: Dict[str, List[str]] = field(default_factory=dict)
    unmatched_sectors: List[str] = field(default_factory=list)
    match_sources: Dict[str, str] = field(default_factory=dict)
    plate_aggregates: Dict[str, CLSPlateAggregate] = field(
        default_factory=dict
    )
    cls_market_shock: Dict[str, List[Tuple[str, str, int]]] = field(
        default_factory=dict
    )

    @property
    def matched_count(self) -> int:
        return len(self.catalysts_by_sector)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched_sectors)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


# 截断每条 catalyst 的展示长度（reason 字段可能很长，含「公司主营」段落）
_CATALYST_MAX_LEN = 80
# 每个板块最多展示几条 catalyst
_CATALYSTS_PER_SECTOR = 3
# 板块名清洗：去掉 「板块」「概念」「指数」 等后缀，提升匹配率
_NAME_NOISE_RE = re.compile(r"(板块|概念|指数|题材)$")
# 噪声 reason 标签——这些不是真正的「催化原因」，是 CLS 的市场统计标签
_NOISE_REASONS: Set[str] = {
    "ST股", "*ST", "退市", "退市风险",
    "昨日涨停", "昨日大额成交", "昨日高换手", "百日新高",
    "首板", "连板", "二连板", "三连板", "高位股",
    "央企", "国企改革", "次新股", "举牌", "高送转",
}


class CLSEnricher:
    """从 ``fact_cls_stock_shock`` 提取板块 catalysts 注入到 sectors_top。"""

    def __init__(self, db: Optional[MarketDB] = None) -> None:
        self.db = db or get_market_db()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def enrich(
        self,
        trade_date: str,
        sectors_top: Sequence[dict],
    ) -> CLSEnrichResult:
        """对 ``sectors_top`` 每个板块做 catalysts 匹配。

        Args:
            trade_date: 形如 ``20260525``
            sectors_top: ``MarketSummary.sectors_top``，每项须含 ``name`` 字段

        Returns:
            ``CLSEnrichResult``
        """
        result = CLSEnrichResult()
        if not sectors_top:
            return result

        plate_index = self._load_plate_index(trade_date)
        result.plate_aggregates = plate_index
        result.cls_market_shock = self._load_cls_market_shock(trade_date)

        for sector in sectors_top:
            name = (sector or {}).get("name") or ""
            name = str(name).strip()
            if not name:
                continue

            hit_plate, source = self._match_plate_name(name, plate_index)
            if hit_plate is None:
                result.unmatched_sectors.append(name)
                continue

            agg = plate_index[hit_plate]
            catalysts = self._format_catalysts(agg.reasons)
            if not catalysts:
                # 板块名匹配到了，但 reason 全空 → 仍标记为 unmatched
                result.unmatched_sectors.append(name)
                continue

            result.catalysts_by_sector[name] = catalysts
            result.match_sources[name] = source

        _log.info(
            "CLSEnricher: %d sectors, %d matched, %d unmatched (date=%s)",
            len(sectors_top),
            result.matched_count,
            result.unmatched_count,
            trade_date,
        )
        return result

    # ------------------------------------------------------------------
    # 加载 CLS plate 索引（板块名 → 聚合）
    # ------------------------------------------------------------------

    def _load_plate_index(
        self, trade_date: str
    ) -> Dict[str, CLSPlateAggregate]:
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT ts_code, reason, plate_json "
                "FROM fact_cls_stock_shock "
                "WHERE trade_date = ?",
                (trade_date,),
            ).fetchall()

        index: Dict[str, CLSPlateAggregate] = {}
        for r in rows:
            ts_code = str(r["ts_code"] or "")
            reason = (r["reason"] or "").strip() if r["reason"] else ""
            plate_json = r["plate_json"]
            for plate in self._iter_plates(plate_json):
                pname = plate.get("secu_name") or ""
                pcode = plate.get("secu_code") or ""
                pname = str(pname).strip()
                if not pname:
                    continue
                agg = index.get(pname)
                if agg is None:
                    agg = CLSPlateAggregate(plate_name=pname)
                    index[pname] = agg
                agg.add(str(pcode), reason, ts_code)
        return index

    @staticmethod
    def _iter_plates(plate_json: object) -> Iterable[dict]:
        if not plate_json:
            return ()
        try:
            data = json.loads(plate_json) if isinstance(
                plate_json, str
            ) else plate_json
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(data, list):
            return ()
        return [p for p in data if isinstance(p, dict)]

    def _load_cls_market_shock(
        self, trade_date: str
    ) -> Dict[str, List[Tuple[str, str, int]]]:
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT sector_name, first_shock_time, status, shock_count "
                "FROM fact_cls_market_shock "
                "WHERE trade_date = ? "
                "ORDER BY first_shock_time ASC",
                (trade_date,),
            ).fetchall()
        out: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
        for r in rows:
            name = str(r["sector_name"] or "").strip()
            if not name:
                continue
            out[name].append(
                (
                    str(r["first_shock_time"] or ""),
                    str(r["status"] or ""),
                    int(r["shock_count"] or 0),
                )
            )
        return dict(out)

    # ------------------------------------------------------------------
    # 板块名三级匹配
    # ------------------------------------------------------------------

    def _match_plate_name(
        self,
        sector_name: str,
        plate_index: Dict[str, CLSPlateAggregate],
    ) -> Tuple[Optional[str], str]:
        """三级匹配，返回 (命中的 plate_name, source)。"""
        if not plate_index:
            return None, "none"

        clean_sector = self._normalize_name(sector_name)

        # 1) 精确（含规范化）
        if sector_name in plate_index:
            return sector_name, "exact"
        for pname in plate_index:
            if self._normalize_name(pname) == clean_sector:
                return pname, "exact"

        # 2) 双向包含
        best_match: Optional[str] = None
        best_score = -1
        for pname in plate_index:
            clean_p = self._normalize_name(pname)
            if not clean_p:
                continue
            if clean_p in clean_sector or clean_sector in clean_p:
                # 越接近原文越好（用长度作 tiebreaker：长的关键词更专一）
                score = len(clean_p) + len(clean_sector)
                if score > best_score:
                    best_score = score
                    best_match = pname
        if best_match is not None:
            return best_match, "contains"

        # 3) 关键词分组
        sector_kw_group = self._find_keyword_group(sector_name)
        if sector_kw_group is None:
            return None, "none"

        # 找 plate 名命中同一组的最高匹配
        best_match = None
        best_score = -1
        for pname in plate_index:
            plate_kw_group = self._find_keyword_group(pname)
            if plate_kw_group == sector_kw_group:
                agg = plate_index[pname]
                # 用涨停股数量作 score：池子大、reason 充分
                score = len(agg.ts_codes)
                if score > best_score:
                    best_score = score
                    best_match = pname
        if best_match is not None:
            return best_match, "keyword"
        return None, "none"

    @staticmethod
    def _normalize_name(name: str) -> str:
        if not name:
            return ""
        s = str(name).strip()
        s = _NAME_NOISE_RE.sub("", s)
        return s

    @staticmethod
    def _find_keyword_group(name: str) -> Optional[str]:
        """返回 ``THEME_KEYWORDS`` 中匹配的组名，否则 None。"""
        if not name:
            return None
        text = str(name).lower()
        for group, kws in THEME_KEYWORDS:
            for kw in kws:
                if kw.lower() in text:
                    return group
        return None

    # ------------------------------------------------------------------
    # Catalyst 格式化
    # ------------------------------------------------------------------

    @staticmethod
    def _format_catalysts(reasons: Sequence[str]) -> List[str]:
        """``reason`` 形如 ``"题材关键词|公司主营段落"``，取关键词部分截断。"""
        seen: Set[str] = set()
        out: List[str] = []
        for r in reasons:
            if not r:
                continue
            # 取分隔符前的部分（题材关键词部分），fallback 用整段
            head = r.split("|", 1)[0].strip() if "|" in r else r.strip()
            head = head.replace("\n", " ").strip()
            if not head:
                continue
            if head in _NOISE_REASONS:
                continue
            if len(head) > _CATALYST_MAX_LEN:
                head = head[: _CATALYST_MAX_LEN - 1] + "…"
            if head in seen:
                continue
            seen.add(head)
            out.append(head)
            if len(out) >= _CATALYSTS_PER_SECTOR:
                break
        return out


__all__ = [
    "CLSEnricher",
    "CLSEnrichResult",
    "CLSPlateAggregate",
    "THEME_KEYWORDS",
]
