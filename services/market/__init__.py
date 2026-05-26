"""盘后数据模块（Phase M1+ 引入）。

负责 Tushare 数据拉取、CLS 板块匹配、AI 兜底补全、合并校验与渲染，
落地为 ``data/market.db`` + 提供 ``MarketSummaryService`` 统一入口。

子模块（按调用顺序，逐步实现）::

    market_db.py        # 连接、迁移、备份管理
    tushare_client.py   # Tushare HTTP 客户端
    trade_date.py       # 交易日解析（含日历缓存）
    tushare_fetcher.py  # 各接口数据获取 + 入库
    metrics.py          # 派生指标（5 日累计、排名等）
    cls_enricher.py     # 财联社板块异动匹配
    ai_enricher.py      # AI 兜底补全
    trader_aliases.py   # 游资席位别名映射
    merger.py           # 合并、冲突仲裁
    validator.py        # JSON Schema 校验
    renderer.py         # compact Markdown 渲染
    service.py          # 统一入口 MarketSummaryService
"""

from services.market.market_db import MarketDB, get_market_db

__all__ = ["MarketDB", "get_market_db"]
