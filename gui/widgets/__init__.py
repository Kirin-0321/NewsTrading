"""GUI 自定义 widget 组件库。

为什么独立成包？
    避免每个页面各自重复实现一遍 sparkline / mini-chart 类小组件。
    QPainter 自绘的轻量 widget 都放在这里，不引第三方图表库依赖。

使用约定：直接从子模块 import，不在本 ``__init__`` 做 re-export，
和 ``gui/utils`` ``gui/workers`` 的风格保持一致。
"""
