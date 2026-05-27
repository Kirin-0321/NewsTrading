"""services.scoring —— 题材回测打分子模块。

模块组成（按依赖顺序，后续逐步引入）：
    matcher.py         代码规范化 + 板块名模糊匹配（本次落地 v4）
    script_scorer.py   脚本规则打分（plan M3.2，未实施）
    ai_scorer.py       AI D+5 总评（plan M3.3，未实施）
    scoring_service.py 服务入口（plan M3.4，未实施）

设计原则：
    1. matcher 是纯函数 + readonly DB 查询，可在 services / tools / tests 自由复用
    2. 不依赖 PyQt
    3. 失败时返回 None / 空，不抛异常，避免污染上游抽取流程
"""

__all__ = ["matcher"]
