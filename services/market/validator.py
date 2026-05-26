"""MarketSummary 校验器与完整度评分（Phase M2）。

输入 ``MarketSummary``（dict）+ schema 文件（``config/market_summary_schema.json``）
输出 ``ValidationReport``，包括::

    * completeness_score   0~1
    * 分层完整度 l1/l2/l3
    * numeric_field_rate   数值字段填充率
    * warnings / errors

完整度计算公式::

    score = sum(weight × is_filled) / sum(weight)

    sectors_top[].xxx 这类带 ``min_count`` 的字段，
    单独按 ``min(filled_count / min_count, 1)`` 折算成 0~1 再乘 weight。

字段路径语法::

    indices.sh.pct_chg              -> summary["indices"]["sh"]["pct_chg"]
    sectors_top[].pct_chg           -> 数组里每项的 pct_chg 字段
    limit_ladder.tiers              -> summary["limit_ladder"]["tiers"]（非空列表算 filled）
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldStat:
    """单个字段的统计。"""

    path: str
    layer: str
    weight: float
    required: bool
    min_count: int = 0
    filled_ratio: float = 0.0
    filled_count: int = 0
    total_count: int = 0
    contribution: float = 0.0  # weight × filled_ratio

    @property
    def is_filled(self) -> bool:
        return self.filled_ratio >= 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "layer": self.layer,
            "weight": self.weight,
            "required": self.required,
            "min_count": self.min_count,
            "filled_ratio": round(self.filled_ratio, 4),
            "filled_count": self.filled_count,
            "total_count": self.total_count,
        }


@dataclass
class ValidationReport:
    completeness_score: float = 0.0
    l1_complete: float = 0.0
    l2_complete: float = 0.0
    l3_complete: float = 0.0
    numeric_field_rate: float = 0.0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    field_stats: List[FieldStat] = field(default_factory=list)
    missing_required: List[str] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "completeness_score": round(self.completeness_score, 4),
            "l1_complete": round(self.l1_complete, 4),
            "l2_complete": round(self.l2_complete, 4),
            "l3_complete": round(self.l3_complete, 4),
            "numeric_field_rate": round(self.numeric_field_rate, 4),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "missing_required": list(self.missing_required),
            "gaps": list(self.gaps),
            "field_stats": [f.to_dict() for f in self.field_stats],
        }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class MarketSummaryValidator:
    """读 schema + 校验 MarketSummary。"""

    DEFAULT_SCHEMA_PATH = Path("config/market_summary_schema.json")

    def __init__(self, schema_path: Optional[Path] = None) -> None:
        self.schema_path = Path(
            schema_path or self.DEFAULT_SCHEMA_PATH
        )
        self.schema = self._load_schema(self.schema_path)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def validate(self, summary: Dict[str, Any]) -> ValidationReport:
        report = ValidationReport()

        fields_cfg = self.schema.get("fields", {})
        rules = self.schema.get("rules", {}) or {}

        # 1) 完整度
        layer_total: Dict[str, float] = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
        layer_score: Dict[str, float] = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
        total_weight = 0.0
        total_score = 0.0
        numeric_filled = 0
        numeric_total = 0

        for path, cfg in fields_cfg.items():
            layer = str(cfg.get("layer", "L2"))
            weight = float(cfg.get("weight", 1))
            required = bool(cfg.get("required", False))
            min_count = int(cfg.get("min_count") or 0)

            stat = self._evaluate_field(
                summary, path, layer, weight, required, min_count
            )
            stat.contribution = weight * stat.filled_ratio
            report.field_stats.append(stat)

            total_weight += weight
            total_score += stat.contribution
            layer_total[layer] = layer_total.get(layer, 0.0) + weight
            layer_score[layer] = (
                layer_score.get(layer, 0.0) + stat.contribution
            )

            # 必填缺失
            if required and stat.filled_ratio < 1.0:
                report.missing_required.append(path)
                report.errors.append(
                    f"必填字段未填满: {path}"
                    f"（filled_ratio={stat.filled_ratio:.2f}）"
                )

            # 缺口列表
            if stat.filled_ratio < 1.0:
                report.gaps.append(
                    {
                        "path": path,
                        "layer": layer,
                        "filled_ratio": round(stat.filled_ratio, 4),
                        "filled_count": stat.filled_count,
                        "total_count": stat.total_count,
                        "min_count": min_count,
                        "required": required,
                    }
                )

            # 数值字段填充率（非 L3）：以 filled_ratio 折算成 0~1 单位
            if layer != "L3":
                numeric_total += 1
                numeric_filled += min(stat.filled_ratio, 1.0)

        report.completeness_score = (
            (total_score / total_weight) if total_weight else 0.0
        )
        for layer_name in ("L1", "L2", "L3"):
            tw = layer_total.get(layer_name, 0.0)
            ts = layer_score.get(layer_name, 0.0)
            ratio = (ts / tw) if tw else 0.0
            setattr(
                report,
                f"{layer_name.lower()}_complete",
                round(ratio, 4),
            )

        report.numeric_field_rate = (
            (numeric_filled / numeric_total) if numeric_total else 0.0
        )

        # 2) 数值范围校验
        self._check_numeric_ranges(summary, rules, report)

        # 3) 阈值告警
        thresholds = (rules or {}).get("warn_thresholds") or {}
        warn = float(thresholds.get("completeness_warn", 0.6))
        block = float(thresholds.get("completeness_block", 0.3))
        if report.completeness_score < block:
            report.errors.append(
                f"完整度过低 ({report.completeness_score:.2%} < "
                f"{block:.0%})，建议重跑"
            )
        elif report.completeness_score < warn:
            report.warnings.append(
                f"完整度偏低 ({report.completeness_score:.2%} < "
                f"{warn:.0%})"
            )

        return report

    # ------------------------------------------------------------------
    # 字段评估
    # ------------------------------------------------------------------

    def _evaluate_field(
        self,
        summary: Dict[str, Any],
        path: str,
        layer: str,
        weight: float,
        required: bool,
        min_count: int,
    ) -> FieldStat:
        stat = FieldStat(
            path=path,
            layer=layer,
            weight=weight,
            required=required,
            min_count=min_count,
        )

        # sectors_top[].xxx 这类数组路径
        if "[]" in path:
            list_path, _, inner = path.partition("[].")
            arr = self._dig(summary, list_path)
            if not isinstance(arr, list):
                stat.filled_ratio = 0.0
                stat.total_count = min_count or 0
                return stat
            filled = 0
            for item in arr:
                if isinstance(item, dict):
                    val = self._dig(item, inner) if inner else item
                    if _is_filled(val):
                        filled += 1
            stat.filled_count = filled
            target = min_count if min_count > 0 else max(len(arr), 1)
            stat.total_count = target
            stat.filled_ratio = (
                min(filled / target, 1.0) if target else 0.0
            )
            return stat

        # 普通路径
        val = self._dig(summary, path)
        if _is_filled(val):
            stat.filled_ratio = 1.0
            stat.filled_count = 1
        else:
            stat.filled_ratio = 0.0
        stat.total_count = 1
        return stat

    @staticmethod
    def _dig(obj: Any, path: str) -> Any:
        if not path:
            return obj
        cur = obj
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
            if cur is None:
                return None
        return cur

    # ------------------------------------------------------------------
    # 数值范围
    # ------------------------------------------------------------------

    def _check_numeric_ranges(
        self,
        summary: Dict[str, Any],
        rules: Dict[str, Any],
        report: ValidationReport,
    ) -> None:
        pct_rule = rules.get("pct_chg_range") or {}
        pct_min = float(pct_rule.get("min", -22.0))
        pct_max = float(pct_rule.get("max", 22.0))

        rate_rule = rules.get("rate_range") or {}
        r_min = float(rate_rule.get("min", 0.0))
        r_max = float(rate_rule.get("max", 1.0))

        # pct_chg 类字段
        for path in (
            "indices.sh.pct_chg",
            "indices.sz.pct_chg",
            "indices.cyb.pct_chg",
        ):
            v = self._dig(summary, path)
            if _is_number(v) and not (pct_min <= float(v) <= pct_max):
                report.warnings.append(
                    f"{path} 数值超出合理区间 [{pct_min}, {pct_max}]: {v}"
                )

        # sectors_top[].pct_chg
        sectors = summary.get("sectors_top") or []
        if isinstance(sectors, list):
            for i, s in enumerate(sectors):
                pct = (s or {}).get("pct_chg")
                if _is_number(pct) and not (
                    pct_min <= float(pct) <= pct_max
                ):
                    report.warnings.append(
                        f"sectors_top[{i}].pct_chg 超出区间 "
                        f"[{pct_min}, {pct_max}]: {pct}"
                    )

        # rate 类
        for path in (
            "sentiment.seal_rate",
            "sentiment.fail_rate",
            "sentiment.promotion_rate",
        ):
            v = self._dig(summary, path)
            if _is_number(v) and not (r_min <= float(v) <= r_max):
                report.warnings.append(
                    f"{path} 不在 [{r_min}, {r_max}]: {v}"
                )

        # max_height
        mh = self._dig(summary, "sentiment.max_height")
        mh_max = int(rules.get("max_height_max") or 20)
        if _is_number(mh) and (int(mh) < 0 or int(mh) > mh_max):
            report.warnings.append(
                f"sentiment.max_height 异常: {mh}（上限 {mh_max}）"
            )

    # ------------------------------------------------------------------
    # schema 加载
    # ------------------------------------------------------------------

    @staticmethod
    def _load_schema(path: Path) -> Dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _log.warning("schema 文件不存在: %s，将用空 schema", path)
            return {"fields": {}, "rules": {}}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"schema {path} JSON 解析失败: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"schema {path} 顶层不是对象")
        if "fields" not in data:
            data["fields"] = {}
        if "rules" not in data:
            data["rules"] = {}
        return data


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_filled(val: Any) -> bool:
    """字段是否「视为已填充」。"""
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, tuple, dict, set)):
        return bool(val)
    if isinstance(val, bool):
        return True
    if isinstance(val, (int, float)):
        try:
            f = float(val)
        except (TypeError, ValueError):
            return True
        if math.isnan(f) or math.isinf(f):
            return False
        return True
    return True


def _is_number(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(f) or math.isinf(f))


def validate_summary(
    summary: Dict[str, Any],
    schema_path: Optional[Path] = None,
) -> ValidationReport:
    """便捷函数。"""
    return MarketSummaryValidator(schema_path).validate(summary)


__all__ = [
    "FieldStat",
    "ValidationReport",
    "MarketSummaryValidator",
    "validate_summary",
]
