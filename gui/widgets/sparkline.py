"""迷你折线图 widget（QPainter 自绘，无第三方依赖）。

业务定位
--------
给「题材预测页 → 板块走势 Tab」展示选中板块的逐日涨跌幅趋势用。
对几百个数据点的小型 sparkline 完全够用，不必引 PyQtChart / pyqtgraph。

特性
----
* 正值红、负值绿（A 股配色），可选阴影填充
* 0 基线灰虚线
* 报告日（``anchor_date``）位置画一条橙色竖线
* 报告日 +1 ~ +5 评估窗口浅黄色背景带
* hover 时显示最接近的数据点 tooltip（YYYYMMDD + pct%）

数据契约
--------
``set_data(points, anchor_date=None, eval_days=5)``:

* ``points``  : ``List[Tuple[str date_YYYYMMDD, Optional[float] pct]]``，
  必须按交易日**升序**传入；None 值的点会被跳过（不连线）。
* ``anchor_date``：报告日 ``YYYYMMDD``，可选，传了就画橙色竖线 + 评估窗口背景带。
* ``eval_days``：评估窗口长度（默认 5，即 D+1~D+5）。

约定
----
* 宽度建议 ≥ 400px、高度 80~140px（迷你 sparkline 经验值）
* 父布局自适应：widget 自身 ``sizeHint = (600, 100)``，可被 stretch。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
)
from PyQt5.QtWidgets import QToolTip, QWidget

#: 折线/区域颜色
_COLOR_UP = QColor("#cf1322")      # 涨红
_COLOR_DOWN = QColor("#389e0d")    # 跌绿
_COLOR_FILL_UP = QColor(207, 19, 34, 40)
_COLOR_FILL_DOWN = QColor(56, 158, 13, 40)
_COLOR_AXIS = QColor("#bfbfbf")
_COLOR_ANCHOR = QColor("#fa8c16")   # 报告日竖线（橙）
_COLOR_EVAL_BG = QColor(250, 219, 20, 35)  # 评估窗口浅黄背景
_COLOR_TEXT = QColor("#595959")
_COLOR_HOVER_DOT = QColor("#262626")


class SparklineWidget(QWidget):
    """轻量 sparkline 折线图。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._points: List[Tuple[str, Optional[float]]] = []
        self._anchor_date: Optional[str] = None
        self._eval_days: int = 5
        self._hover_idx: int = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(100)
        from PyQt5.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def set_data(
        self,
        points: List[Tuple[str, Optional[float]]],
        *,
        anchor_date: Optional[str] = None,
        eval_days: int = 5,
    ) -> None:
        """更新数据并触发重绘。

        Args:
            points: 按交易日升序的 ``[(YYYYMMDD, pct_chg or None), ...]``。
            anchor_date: 报告日 ``YYYYMMDD``。如果不在 points 范围内则不画线。
            eval_days: 评估窗口长度（用于在 anchor_date 右侧画浅黄背景带）。
        """
        self._points = list(points)
        self._anchor_date = anchor_date or None
        self._eval_days = max(0, int(eval_days))
        self._hover_idx = -1
        self.update()

    def clear(self) -> None:
        self.set_data([])

    # ------------------------------------------------------------------
    # 重绘
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 36, 12, 12, 22
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        # 背景
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if not self._points:
            painter.setPen(QPen(QColor("#bfbfbf")))
            painter.drawText(
                self.rect(), Qt.AlignCenter,
                "（无板块行情数据，可能 sector_ts_code 未匹配 / market.db 未同步）",
            )
            painter.end()
            return

        # 有效值计算 Y 轴范围
        valid_vals = [v for _, v in self._points if v is not None]
        if not valid_vals:
            painter.setPen(QPen(QColor("#bfbfbf")))
            painter.drawText(
                self.rect(), Qt.AlignCenter, "（数据全部为空）",
            )
            painter.end()
            return

        y_max = max(valid_vals)
        y_min = min(valid_vals)
        # 0 必须在范围内（A 股 sparkline 看正负更直观）
        y_max = max(y_max, 0.0)
        y_min = min(y_min, 0.0)
        y_span = y_max - y_min
        if y_span < 1e-6:
            y_span = 1.0
        # 上下各留 8% 余量
        y_pad = y_span * 0.08
        y_max += y_pad
        y_min -= y_pad
        y_span = y_max - y_min

        n = len(self._points)
        # 每个点的 X（含两侧半个间隔留白，方便 hover）
        if n == 1:
            xs = [pad_l + plot_w / 2.0]
        else:
            xs = [pad_l + i * plot_w / (n - 1) for i in range(n)]

        def _y_of(val: float) -> float:
            return pad_t + (y_max - val) / y_span * plot_h

        zero_y = _y_of(0.0)

        # ---- 评估窗口浅黄背景带 ----
        if self._anchor_date:
            anchor_idx = self._index_of(self._anchor_date)
            if anchor_idx is not None and self._eval_days > 0:
                end_idx = min(n - 1, anchor_idx + self._eval_days)
                if end_idx > anchor_idx:
                    x1 = xs[anchor_idx]
                    x2 = xs[end_idx]
                    painter.fillRect(
                        QRectF(x1, pad_t, x2 - x1, plot_h),
                        QBrush(_COLOR_EVAL_BG),
                    )

        # ---- 0 基线 ----
        if y_min < 0 < y_max:
            pen = QPen(_COLOR_AXIS)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(pad_l), int(zero_y),
                             int(pad_l + plot_w), int(zero_y))

        # ---- Y 轴左侧标签：max / 0 / min ----
        painter.setPen(QPen(_COLOR_TEXT))
        small_font = QFont(painter.font())
        small_font.setPointSize(max(7, painter.font().pointSize() - 2))
        painter.setFont(small_font)
        fm = QFontMetrics(small_font)
        painter.drawText(
            QPoint(2, int(pad_t + fm.ascent() / 2 + 4)),
            f"{y_max:+.1f}%",
        )
        painter.drawText(
            QPoint(2, int(pad_t + plot_h)),
            f"{y_min:+.1f}%",
        )
        if y_min < 0 < y_max:
            painter.drawText(
                QPoint(2, int(zero_y + fm.ascent() / 2)),
                "0%",
            )

        # ---- 区域填充（正红负绿，按段切分）----
        self._draw_filled_area(painter, xs, _y_of, zero_y)

        # ---- 折线 ----
        self._draw_polyline(painter, xs, _y_of)

        # ---- 报告日竖线 ----
        if self._anchor_date:
            anchor_idx = self._index_of(self._anchor_date)
            if anchor_idx is not None:
                x = xs[anchor_idx]
                pen = QPen(_COLOR_ANCHOR)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawLine(int(x), int(pad_t),
                                 int(x), int(pad_t + plot_h))
                # 标签 "D" 在顶部
                painter.setPen(QPen(_COLOR_ANCHOR))
                painter.drawText(
                    QPoint(int(x) + 3, int(pad_t) + 10),
                    f"D({self._format_date(self._anchor_date)})",
                )

        # ---- 起止日期标签 ----
        painter.setPen(QPen(_COLOR_TEXT))
        first_label = self._format_date(self._points[0][0])
        last_label = self._format_date(self._points[-1][0])
        painter.drawText(
            QPoint(int(pad_l), int(h - 4)),
            first_label,
        )
        last_w = fm.horizontalAdvance(last_label)
        painter.drawText(
            QPoint(int(pad_l + plot_w - last_w), int(h - 4)),
            last_label,
        )

        # ---- hover 点高亮 ----
        if 0 <= self._hover_idx < n:
            v = self._points[self._hover_idx][1]
            if v is not None:
                x = xs[self._hover_idx]
                y = _y_of(v)
                painter.setPen(QPen(_COLOR_HOVER_DOT))
                painter.setBrush(QBrush(_COLOR_HOVER_DOT))
                painter.drawEllipse(QPointF(x, y), 3.0, 3.0)

        painter.end()

    def _draw_polyline(self, painter, xs, y_of) -> None:
        """按 None 切段画折线，正/负段颜色不同。"""
        seg: List[Tuple[float, float, float]] = []  # (x, y, val)
        last_val: Optional[float] = None
        for i, (_, v) in enumerate(self._points):
            if v is None:
                # flush 当前段
                self._stroke_segment(painter, seg)
                seg = []
                last_val = None
                continue
            x = xs[i]
            y = y_of(v)
            seg.append((x, y, v))
            last_val = v
        if seg:
            self._stroke_segment(painter, seg)
        # 避免 unused
        _ = last_val

    @staticmethod
    def _stroke_segment(
        painter, seg: List[Tuple[float, float, float]],
    ) -> None:
        if len(seg) < 2:
            # 单点用小圆点表示
            if len(seg) == 1:
                x, y, v = seg[0]
                color = _COLOR_UP if v >= 0 else _COLOR_DOWN
                painter.setPen(QPen(color))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(QPointF(x, y), 2.0, 2.0)
            return
        # 取段内均值正负决定主色（更平滑）
        avg = sum(v for _, _, v in seg) / len(seg)
        color = _COLOR_UP if avg >= 0 else _COLOR_DOWN
        pen = QPen(color)
        pen.setWidthF(1.6)
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(seg[0][0], seg[0][1])
        for x, y, _ in seg[1:]:
            path.lineTo(x, y)
        painter.drawPath(path)

    def _draw_filled_area(self, painter, xs, y_of, zero_y) -> None:
        """逐段把折线与 0 基线之间填色，正红 / 负绿。"""
        n = len(self._points)
        if n < 2:
            return
        seg: List[Tuple[float, float, float]] = []
        for i, (_, v) in enumerate(self._points):
            if v is None:
                self._fill_segment(painter, seg, zero_y)
                seg = []
                continue
            seg.append((xs[i], y_of(v), v))
        if seg:
            self._fill_segment(painter, seg, zero_y)

    @staticmethod
    def _fill_segment(
        painter, seg: List[Tuple[float, float, float]], zero_y: float,
    ) -> None:
        if len(seg) < 2:
            return
        avg = sum(v for _, _, v in seg) / len(seg)
        brush = _COLOR_FILL_UP if avg >= 0 else _COLOR_FILL_DOWN
        path = QPainterPath()
        path.moveTo(seg[0][0], zero_y)
        for x, y, _ in seg:
            path.lineTo(x, y)
        path.lineTo(seg[-1][0], zero_y)
        path.closeSubpath()
        painter.fillPath(path, brush)

    # ------------------------------------------------------------------
    # hover tooltip
    # ------------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        if not self._points:
            return
        # 反推最近索引
        pad_l = 36
        plot_w = max(1, self.width() - pad_l - 12)
        n = len(self._points)
        if n == 1:
            idx = 0
        else:
            frac = (event.x() - pad_l) / plot_w
            frac = max(0.0, min(1.0, frac))
            idx = int(round(frac * (n - 1)))
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()
        date_str, val = self._points[idx]
        val_text = f"{val:+.2f}%" if val is not None else "—"
        QToolTip.showText(
            event.globalPos(),
            f"{self._format_date(date_str)}  {val_text}",
            self,
        )

    def leaveEvent(self, _event) -> None:
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _index_of(self, date_yyyymmdd: str) -> Optional[int]:
        for i, (d, _) in enumerate(self._points):
            if d == date_yyyymmdd:
                return i
        return None

    @staticmethod
    def _format_date(d: str) -> str:
        """``20260527`` → ``05-27``（紧凑显示）。"""
        if isinstance(d, str) and len(d) == 8 and d.isdigit():
            return f"{d[4:6]}-{d[6:]}"
        return d or ""
