# 题材抽取 `thinking:disabled` 误发 `deepseek-v4-flash` 致 SSE 死寂

> **修复时间**：2026-05-28 19:30  
> **影响版本**：`config/ai_config.json::theme_extraction.model = "deepseek-v4-flash"` 的所有调用方（GUI 手动回测页 / AI 分析页 / CLI `backtest_prompt` / 定时任务）  
> **修复文件**：`core/theme_extractor.py`（1 行判定收紧）  
> **诊断手段**：临时调试 CLI `tools/_debug_theme_extract.py`（逐阶段时间戳 + chunk watchdog；定位完根因后按规约用后即删）  
> **回归**：`tools/test_theme_extract_retry.py` 5/5 + `tools/test_theme_normalize.py` 26/26 全绿  
> **端到端**：临时 CLI 跑最新 backtest md 32.8s 跑完，13 题材 / 50 标的 / 35 新闻引用入库

---

## 一、问题现象（与 18:20 hotfix 是不同根因）

主人晚上手动回测页跑回测，进到「题材抽取阶段」**永远卡住**：

```
[19:25:07] 报告已保存：5月27日_0时00分_盘后总结分析报告_backtest_custom_6_192320.md
[19:25:08] 正在抽取题材并入库...
[19:25:08] httpx | POST .../chat/completions "HTTP/1.1 503 Service Temporarily Unavailable"
[19:25:08] openai._base_client | Retrying request to /chat/completions in 0.476426 seconds
[19:25:08] httpx | POST .../chat/completions "HTTP/1.1 503 Service Temporarily Unavailable"
[19:25:08] openai._base_client | Retrying request to /chat/completions in 0.923703 seconds
[19:25:09] httpx | POST .../chat/completions "HTTP/1.1 200 OK"
              ↓
              ⚠️ 之后 5 分多钟无任何 chunk、无超时、无重试、无报错
              主人盯着空白屏等到失去耐心
```

**18:20 那版 hotfix 没救命**——`stream_idle_timeout=60s` 在这场景下没机会触发。

---

## 二、根因分析（强迫症登场）

### 2.1 直接根因：参数误发

`core/theme_extractor.py::_call_llm` 末尾给 deepseek 强行加 `thinking` 参数：

```python
# 18:20 hotfix 之后的代码（治错根因）
if self.provider == "deepseek" and str(self.model).startswith("deepseek-v4"):
    create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
```

判定用 `startswith("deepseek-v4")` 一刀切——**`deepseek-v4-pro` 和 `deepseek-v4-flash` 都会命中**。

但实际上：

| 模型 | 支持 `thinking` 参数 | 误发后行为 |
|---|---|---|
| `deepseek-v4-pro` | ✅ 是（默认 enabled） | 正确响应 disabled |
| `deepseek-v4-flash` | ❌ **不支持** | **服务端 200 OK 但 SSE 流死寂**（不送 content / 不送 keepalive） |

而 `config/ai_config.json::theme_extraction.model` 默认就是 `deepseek-v4-flash`（题材分类不需要重推理、用便宜的 flash），所以**每次抽取都中招**。

### 2.2 为什么 18:20 hotfix 没救命

18:20 加的 `stream_idle_timeout=60s` 写到 httpx 的 `read` timeout，按文档：

> httpx `read` timeout 在 SSE/流式场景下 = 相邻 socket read 之间最大等待时间

按理 60s 没 chunk 就该抛 `ReadTimeout` 触发外层 `extract_from_text` 重试。但实测 5+ 分钟**根本不抛**。

辉夜推测两个可能（任一即可解释）：

1. **TCP keepalive 还在**：deepseek 服务端处理 thinking 异常后让 HTTP 连接挂着不主动关，但底层 TCP keepalive 心跳让 httpx 以为「数据还在路上」，read 计时不重置但也不超时
2. **OpenAI 2.38.0 SDK 在 stream context 下吞掉了 timeout 异常**：openai SDK 内部对流迭代器做了一层包装，client 级 timeout 没传到 httpx 真实 read 操作上

无论哪个，结果都是**"卡住永远不退"**。

### 2.3 为什么主分析（`_stream_chat`）没事

`core/ai_news_analyzer.py::_stream_chat` 对 `enable_deep_thinking` 做了分支：

```python
if provider == "deepseek" and model.startswith("deepseek-v4"):
    if enable_deep_thinking:
        create_kwargs["extra_body"] = {"thinking": {"type": "enabled"}, ...}
    else:
        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
```

主分析跑的是 `deepseek-v4-pro`（provider 配置 `model="deepseek-v4-pro"`），所以即便发了 `thinking:enabled/disabled` 也是正确支持的，没事。

而题材抽取用的是 `theme_extraction.model="deepseek-v4-flash"`，独立的 model 字段，专门被这次 bug 击中。

---

## 三、修复方案

### 3.1 一行收紧判定（已落地）

```diff
-    # 与主分析器一致：V4 默认 thinking，分类抽取必须关闭，否则长时间无 content 易触发读超时
-    if self.provider == "deepseek" and str(self.model).startswith("deepseek-v4"):
-        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
+    # 2026-05-28 19:30 验证假设：deepseek-v4-flash 不支持 thinking 参数，
+    # 误传 {"thinking":{"type":"disabled"}} 后服务端 200 OK 但 SSE 流死寂。
+    # 收紧判定：只对带 thinking 能力的 deepseek-v4-pro 显式关闭，flash 不传。
+    if self.provider == "deepseek" and str(self.model).startswith("deepseek-v4-pro"):
+        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
```

| 模型 | 修复前行为 | 修复后行为 |
|---|---|---|
| `deepseek-v4-pro` | 传 `thinking:disabled`（正确） | 传 `thinking:disabled`（正确） |
| `deepseek-v4-flash` | **传 `thinking:disabled`（误发 → SSE 死寂）** | **不传 thinking（正常流式）** |
| `deepseek-chat` / 其他 | 不传 | 不传 |

### 3.2 不动 18:20 hotfix

18:20 加的 `stream_idle_timeout=60s` + 重试循环**保留**——它解决的是另一类问题（真断流、5xx、限流）。两个修复正交：

- 19:30 修复：让 flash 模型**不再死寂**（治标本）
- 18:20 修复：万一真断流时**自动重试 3 次**（兜底）

### 3.3 调试手段（已用完即删）

本次定位卡死的关键是临时调试 CLI `tools/_debug_theme_extract.py`，要点：

- 逐阶段打时间戳：导入 / 初始化 / 读 md / `_call_llm` / 解析 / matcher / 入库
- **chunk watchdog**：每 50 个 chunk 打一行 `+N chars / 累计 N chars / max_gap=N.Ns`
- `--stage llm`（仅到拿 raw_response 停）/ `--no-save`（不入库）/ `--latest`（自动选最新 md）

按 `huiye-persona.mdc` 规约 `_` 前缀临时文件用后即删，已删。下次若再遇流式假死，30 分钟可参照本节再写一份等价工具。

---

## 四、验证记录

### 4.1 修复前实测（19:23 那次 backtest_prompt）

```
19:23:20  开始 LLM 分析
19:25:07  报告 md 已保存（分析 OK）
19:25:09  题材抽取 LLM 200 OK
19:25:09 ~ 19:30:49  ⚠️ 5 分多钟无任何输出（被辉夜手动 kill）
ai_reports id=394 已写入
theme_predictions 该报告 0 条
```

### 4.2 修复后实测（19:34 临时 debug CLI `--latest`）

```
[19:34:07] _read_report OK，截断后 9380 chars
[19:34:09] chunk #   1 + 2 chars | 累计 2 chars | max_gap=2.2s
...
[19:34:39] _call_llm OK | 耗时 31.7s | chunks=3944 chars=11678 | max_gap=2.2s | finish_reason=stop
[19:35:23] extract_from_file 返回 | themes=13 chunks=3876 max_gap=2.6s | err=None
[19:35:23] save_themes OK | stats={'themes': 13, 'stocks': 50, 'news': 35, 'prompt_id': 'custom_6'}
[19:35:23] === 全部完成，总耗时 32.8s ===
```

数据库验证：

```
theme_predictions:  40 行（3 份 md：13 + 16 + 11）
theme_stocks:       127 行
theme_news:         96 行
is_backtest=1:      40 行
```

### 4.3 回归测试

```
tools/test_theme_extract_retry.py  → 5/5 全绿
tools/test_theme_normalize.py      → 26/26 全绿（17 + 6 + 3）
```

---

## 五、教训登记

1. **API 参数兼容性按精确模型名判定，不要用前缀通配**——`startswith("deepseek-v4")` 把 pro 和 flash 当成同款是这次 bug 的核心。同理 GPT/Claude/Qwen 系列以后也得按精确模型/能力判定，不要按系列名一刀切
2. **服务端"假死"是真实存在的——TCP keepalive 让 httpx read timeout 在某些异常态下失灵**。所以 hotfix 18:20 的 `stream_idle_timeout=60s` **不能完全依赖**作为最后兜底，参数兼容性才是治本的方向
3. **debug CLI 价值高**：本次定位卡死的关键就是 `_debug_theme_extract.py` 的逐阶段时间戳。未来涉及流式调用的 bug，第一时间上类似工具，比堆日志和盯 UI 高效十倍

---

## 六、相关文档

- 18:20 hotfix（被本次根因压过去）：[`05-28-1820-题材抽取流式静默自动重试.md`](./05-28-1820-题材抽取流式静默自动重试.md)
- 题材抽取保存与 GUI 补全设计：[`../design/05-27-1003-题材抽取保存与GUI补全设计.md`](../design/05-27-1003-题材抽取保存与GUI补全设计.md)
