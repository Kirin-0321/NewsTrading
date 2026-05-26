# 05-26 23:25 — `export_market_summary.py` CLI 上线

## 起因

主人 23:22 反馈：希望"导出盘后数据 Markdown"这个功能 CLI 化，方便辉夜
在改 renderer 时反复验证渲染效果。

## 设计

**核心定位**：纯导出工具，**不调 Tushare、不调 AI、不写 DB**，只做
「读缓存 → 重渲染 → 输出」。秒级完成。

```
market_summaries.summary_json
        ↓ （读）
MarketSummaryRenderer.render_compact   ← 用最新代码，立刻看到渲染改动效果
        ↓ （渲染）
stdout / 文件
```

与现有 `market_fetch.py` 的区别：

| 工具 | 作用 | 是否调 API | 单日耗时 |
|---|---|---|---|
| `market_fetch.py` | 触发完整 build（fetch → enrich → render → 入库） | 是 | 30~60s |
| `export_market_summary.py` | 仅从缓存读 + 重渲染 | **否** | <1s |

## 改动

### 新增
- **`tools/export_market_summary.py`** —— 新 CLI 入口
  - 单日模式：默认导出最近一个有缓存的交易日；可指定 `trade_date`
  - 批量模式：`--recent N --save-dir DIR` 一次导出 N 天
  - 输出格式：默认 MD；`--json` / `--both` 切换
  - 渲染源：默认 live 重渲染；`--use-cached-md` 用 DB 缓存的 summary_md
  - `--quiet` 去掉 CLI 头部，stdout 纯净

### 文档
- **`doc/guides/05-26-2126-CLI使用文档.md`** §2b — 新增完整章节，含
  PowerShell 编码坑（`>` 重定向乱码）的说明 + 推荐用 `--save`
- **`.huiye/README.md`** 索引更新

## 验证

```text
> python tools/export_market_summary.py 20260526 --save _test.md
========================================================================
  导出 20260526 盘后总结
  数据来源    : market_summaries 缓存
  渲染模式    : live renderer（重渲染）
  完整度      : 95.45%
  build 模式  : hybrid
========================================================================
[保存] MD   → _test.md

# _test.md 内容（UTF-8 正确，与主人贴的导出一字不差）
# 20260526 盘后数据
- 交易日: **20260526**（上一交易日 20260525）
- 生成时间: 2026-05-26 23:20:25 / 模式: hybrid / Tushare 调用 20 次 / 耗时 22.4s
...
```

## 使用场景

### 辉夜：边改 renderer 边验证
```bash
# 1. 改 services/market/renderer.py（如：板块加 Bottom 10）
# 2. 立刻导出新版 MD
python tools/export_market_summary.py 20260526 --save data/exports/new.md
# 3. 与旧版对比
python tools/export_market_summary.py 20260526 --use-cached-md --save data/exports/old.md
diff data/exports/old.md data/exports/new.md
```

### 主人：每日喂 AI / 存档
```bash
# 当日盘后总结直接喂给 AI 分析
python tools/export_market_summary.py --quiet > today.md  # ⚠️ PowerShell 乱码
# 或
python tools/export_market_summary.py --save today.md  # ✅ UTF-8 正确
```

## 关联

- 下一步可能要改 renderer（A 包：板块 Bottom 10 / 龙虎榜其他席位等），
  详见辉夜 23:23 给主人的导出数据全面性评估
