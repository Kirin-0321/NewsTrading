"""日期格式化 helper（GUI 显示层专用）。

业务背景
--------
``ai_reports.report_date`` / ``theme_predictions.report_date`` 等字段在
schema 协议下统一 ``YYYYMMDD``（8 位数字），便于和 ``trade_date`` 字段直接
等值匹配、参与 ``next_trade_date`` 等业务函数。

但展示给主人看时 ``2026-05-22`` 比 ``20260522`` 更友好——故 GUI 表格 / 详情
面板 / CSV 导出 / combo 选项均通过本 helper 把 raw 字符串转为显示串。

约定
----
- 只在 **显示边界** 调用（QTableWidget.setItem / QComboBox.addItem 文本 /
  QLabel.setText / csv writer 写入）；
- ``itemData`` / 业务函数入参 / SQL 查询参数仍传 raw YYYYMMDD；
- 入参不合法（非 8 位数字）时原样返回，确保旧数据不炸 UI。
"""

from __future__ import annotations


def format_yyyymmdd_to_dash(s: object) -> str:
    """``"20260522"`` → ``"2026-05-22"``；不合法值原样返回 str(s)。

    用于 GUI 表格 / 详情 / CSV 显示。
    """
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def dash_to_yyyymmdd(s: object) -> str:
    """``"2026-05-22"`` → ``"20260522"``；不合法值原样返回 str(s)。

    主要给迁移脚本 / 历史数据兼容路径用。
    """
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        compact = s.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return compact
    return s


__all__ = ["format_yyyymmdd_to_dash", "dash_to_yyyymmdd"]
