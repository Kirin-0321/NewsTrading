# 项目代码 Review 报告

> 评审者: 辉夜
> 评审时间: 2026-05-22
> 项目: A股新闻爬虫与AI分析系统(`d:\爬虫`)
> 远程仓库: `https://github.com/Kirin-0321/Stock-News-AI` (公开)
> 评审范围: 全套快速扫描(架构/代码/安全/性能/维护性)
> 代码规模: 37 个 Python 文件,约 11,000 行

---

## 🔥 摘要(TL;DR)

| 等级 | 数量 | 必须立即处理 |
|------|------|--------------|
| 🔴 严重(Critical) | 3 | 是,涉及账号资金安全 |
| 🟠 高危(High) | 7 | 强烈建议本周修复 |
| 🟡 中危(Medium) | 9 | 排期修复 |
| 🟢 建议(Low) | 12 | 顺手优化 |

**最关键结论**: API Key 已经被推到了公开 GitHub 仓库,**4 个第三方 AI 服务商(OpenAI/DeepSeek/Qwen/火山引擎)的密钥全部泄漏**,任何人都可以用主人的额度刷接口。这是必须**马上处理**的,其他都可以缓一缓。

---

## 🔴 Critical (严重) - 立即处理

### C1. ⚠️ API Key 已泄漏到公开 GitHub 仓库

**证据**:
- `git ls-files config/` 显示 `config/ai_config.json` 已被纳入版本控制
- `git log` 中存在 2 次 commit (`9060a81`、`1c0439d`) 均包含此文件
- 远程仓库 `Kirin-0321/Stock-News-AI` 是公开的(`gh repo view`)

**泄漏的密钥(已脱敏前缀)**:
```
openai     : sk-0630321a99c74890***   (与 deepseek 同一个 key)
deepseek   : sk-0630321a99c74890***
qwen       : sk-9278898865b74c1d***
volcengine : 029b7ee4-b274-43ad-***
```

**风险**:
- 任何爬虫扫到 GitHub 公开 key 的脚本都能立刻盗刷
- DeepSeek 余额 / 阿里云额度 / 字节火山引擎余额面临被刷的风险
- 一旦被恶意调用产生大额账单,平台会向开通账号的人追讨

**修复步骤(必须按顺序)**:
1. **立即**到 4 个平台后台**作废 / 旋转**当前 API Key
2. 把 `config/` 加入 `.gitignore`
3. `git rm --cached config/ai_config.json` 让 git 不再追踪
4. 用 `git filter-repo` 或 BFG 清除历史中的密钥
5. 强制 push 重写后的历史(`git push --force`)
6. 之后用环境变量(`os.environ`) 或单独的 `.env` 文件读取密钥

### C2. `.gitignore` 缺少 `config/` 排除规则

**证据**:
```10:55:.gitignore
# Python
__pycache__/
*.py[cod]
...
# 数据文件
Typora/
chrome-win64/
data/raw/*.json
...
# 环境变量
.env
```

`.gitignore` 完全没提 `config/`,导致 C1 发生。同时存在重复条目:`dist/` 在第 8 行和第 49 行各出现一次。

### C3. `core/ai_config.py` 字典字面量重复定义,后者覆盖前者

**证据**: `core/ai_config.py` 第 35-100 行的 `get_default_config()`:

```35:100:core/ai_config.py
return {
    "current_provider": "openai",
    "current_prompt_template": "standard",
    "prompt_templates": self.get_default_prompt_templates(),  # ← 第 38 行
    "providers": { ... },
    ...
    "prompt_templates": {                                     # ← 第 82 行,同名 key!
        "default": { ... },
        "conservative": { ... },
        "aggressive": { ... }
    },
    "current_template": "default"
}
```

**后果**: Python 字典字面量同名 key,后者覆盖前者 → `get_default_prompt_templates()` 返回的 5 个完整模板(standard/aggressive/conservative/value/short_term)**全部丢失**,只剩 3 个简陋模板。这是已经发生的 bug,不是潜在问题。

---

## 🟠 High (高危) - 一周内修

### H1. `news_crawler_scroll.py` 硬编码了 `F:\爬虫` 绝对路径

**位置**:
- 第 882-883 行:`CRAWLER_CONFIG.get('chrome_path', r"F:\爬虫\chrome-win64\chrome.exe")`
- 第 940-943 行:`argparse` 默认值同样是 `F:\爬虫\...`
- `tools/test_source_field.py:88-89` 同样硬编码

**风险**: 工作目录已经是 `D:\爬虫`,这些 fallback 永远找错路径;如果 `core.config` 导入失败,程序就会去 F 盘找浏览器,直接 crash。

### H2. `NewsCrawler.run(headless=...)` 参数完全是装饰品

**证据**:
```866:907:core/news_crawler_scroll.py
def run(self, scroll_times=36, wait_seconds=6, headless=True, max_no_change=3):
    ...
    self.crawler = GuZhangNewsCrawlerScroll(
        chrome_path=chrome_path,
        chromedriver_path=chromedriver_path
    )                                          # ← headless 没被传进去
    ...
    result = self.crawler.crawl(...)           # ← crawl 也不接 headless
```

而 `GuZhangNewsCrawlerScroll.fetch_page_with_scroll` 内部硬编码了 `chrome_options.add_argument('--headless')`(第 149 行),用户在 GUI 里取消勾选无头模式,行为不变。**这是直接的功能 bug**。

### H3. `CrawlerWorker.stop()` 用 `self.terminate()` 暴力杀线程

**位置**: `gui/workers/crawler_worker.py:254-258`

```254:258:gui/workers/crawler_worker.py
def stop(self):
    """停止爬虫"""
    self._is_running = False
    self.log_message.emit("正在停止爬虫...")
    self.terminate()
```

**风险**:
- QThread.terminate() 是异步暴力终止,Chrome 子进程**不会**自动 quit
- 多次启停后,Chrome 僵尸进程会堆积,占用大量内存
- 推荐使用协作式停止:在 worker 中检查 `self._is_running`,在 crawler 滚动循环中也检查 flag,自然退出 + driver.quit()

### H4. `core/db_helper.py` 命名严重误导

**实际行为**: 完全没用数据库,只是遍历 `data/raw/*.json` 文件做模拟。

**问题**:
- 文件名叫 `db_helper`、函数叫 `get_latest_news_from_db`、`load_all_db_news`、`deduplicate_with_db`
- README、requirements.txt 都声称"使用 SQLAlchemy + SQLite",`requirements.txt` 列了 `sqlalchemy>=2.0.0` 依赖
- 实际项目里没有任何 SQLAlchemy 的 import,也没有 `data/crawler.db` 文件

**后果**:
- 误导后续维护者
- 性能差:每次"查最新新闻"都要把所有 JSON 文件扫一遍
- requirements 装了用不上的库

建议:重命名为 `news_storage.py` / `local_news_index.py`,或者真的引入 SQLite(数据量大了之后查询快得多)。

### H5. `daily_news_flow.py` 与 `enhanced_news_flow.py` 严重代码重复

`_run_crawler`、`_run_cleaner`、`_run_export`、`_get_summary`、`_run_analyzer` 5 个方法**几乎逐字相同**,只有 `enhanced_news_flow` 多了一步 `_run_merger`。重复约 250 行。

**修复**: enhanced 应该 `继承 DailyNewsFlow` 并仅 override `execute()` 多调一步合并即可。或者把这 5 个方法抽到 `WorkflowBase`。

### H6. `AINewsAnalyzer` 5 个 `analyze_with_xxx` 方法 90% 重复

文件 611 行,其中 4 个 provider(openai/deepseek/qwen/volcengine)都是用 OpenAI SDK 走兼容接口,逻辑完全一样,只是 base_url 和 model 不同。zhipu 用 ZhipuAI SDK 也大同小异。

**建议**:
```python
def _call_openai_compat(self, provider: str, news_data, ...):
    cfg = self.config.get_provider_config(provider)
    if not cfg.get('api_key'):
        raise ValueError(f"未配置 {provider} API Key")
    client = OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'])
    ...
```
代码量可以从 ~500 行 降到 ~100 行。`NewsCleaner._call_xxx` 也是相同问题。

### H7. `WorkflowEngine.start_interval_schedule` 起了一个**永远停不下来**的后台线程

```267:275:core/workflow_engine.py
def run_scheduler():
    while True:                       # ← 永真循环
        schedule.run_pending()
        time.sleep(60)

if not hasattr(self, '_scheduler_thread') or not self._scheduler_thread.is_alive():
    self._scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    self._scheduler_thread.start()
```

加上 GUI 里 `SchedulerService` 又有一个独立的 daemon 线程在 1 秒/次轮询,**两个调度器并存职责重叠**。`SchedulerService` 提供了 `stop()`,而 `WorkflowEngine` 的没有。混用容易出现"明明禁用了任务还是跑了"的灵异现象。

---

## 🟡 Medium (中危)

### M1. `semantic_dedup.py` 默认参数与注释**全面不一致**

```146:163:core/semantic_dedup.py
default_deduplicator = SemanticDeduplicator(
    time_window_minutes=30,  # 30分钟时间窗口
    similarity_threshold=0.5  # 60%相似度阈值          ← 0.5 不是 60%,是 50%!
)
```

而 `data_merger.py:_deduplicate_news` 的注释写"10分钟窗口+80%相似度",`news_cleaner.py:_deduplicate` 注释也写"10分钟+80%",**实际全调用 default_deduplicator (30/0.5)**。注释撒谎。

### M2. 大量 `bare except` 吞异常

至少 26 处 `except:` 不写异常类型(Grep 统计),会吞掉 KeyboardInterrupt 等关键异常,且 debug 时找不到原因。最严重的位置:
- `news_crawler_scroll.py` 解析阶段(216、297、313 行)
- `data_merger.py`(多处时间解析)
- `semantic_dedup.py`

### M3. core/ 模块用 `print()` 当日志(161+ 处)

`workflows/base.py` 已经搭好了 logging 体系,但 `core/` 下所有模块仍在用 print。导致:
- 工作流执行时 core 模块的日志不进 logfile
- 打包成 EXE 后没有 console,这些 print 全部丢失

### M4. `WorkflowEngine._load_workflow` 只识别"直接父类"

```44:50:core/workflow_engine.py
for item_name in dir(module):
    item = getattr(module, item_name)
    if (isinstance(item, type) and 
        hasattr(item, '__bases__') and 
        'WorkflowBase' in [base.__name__ for base in item.__bases__]):  # ← 只看直接父类
        workflow_class = item
        break
```

如果以后做 `EnhancedNewsFlow(DailyNewsFlow)` 这种间接继承,会被忽略。建议用 `issubclass(item, WorkflowBase) and item is not WorkflowBase`。

### M5. AIConfig 在模块层就 `ai_config = AIConfig()` 创建单例

`core/ai_config.py:645`。会在 import 时立刻读 JSON 文件,如果配置文件里 JSON 损坏,整个程序无法启动(虽然有 try/except,但 fallback 会用默认配置覆盖回写)。建议改成 lazy 单例。

### M6. `MainWindow.__init__` 同步实例化 7 个 page,启动慢

```80:91:gui/main_window.py
self.pages = {
    'crawler': CrawlerPage(),
    'data': DataPage(),
    'analysis': AnalysisPage(),
    'export': ExportPage(),
    'ai_analysis': AIAnalysisPage(),
    'cleaning': NewsCleaningPage(),
    'schedule': SchedulePage(scheduler_service=self.scheduler_service)
}
```

这些 page 里有的会扫磁盘(`data_page` 列文件、`analysis_page` 加载 JSON)。建议改成懒加载:第一次切到该 page 时才创建。

### M7. `SchedulerService` 只实现了"每日定时"

```108:108:core/scheduler_service.py
schedule.every().day.at(task_time).do(job).tag(task_id)
```

但 README 和 GUI 里宣称支持"一次性 / 每小时 / 每日 / 间隔" 4 种模式。**实际只支持 daily**。其他模式 GUI 上能填,后端会忽略,变成静默 bug。

### M8. `config/ai_config.json` 中编码污染

```json
"system_prompt": "...你的核心任务是基于...新闻分析、市场情绪进行全方位复盘与策略推演\n\n【核心能力】：\n- 数据解读：从量能、涨跌停、连板梯队等数据中提取市场信号\n- 新闻分析：识别政策、技术突破、重大事件等核心催化�?\n..."
```

文件中出现大量 `催化�?`、`一般新�?` 这类乱码,说明保存配置时被某次非 UTF-8 写入污染。`ai_config.py:save_config` 用 `ensure_ascii=False + utf-8`,理论上不会出问题,可能是手动编辑时编辑器配置不当。

### M9. `requirements.txt` 列了完全未使用的 sqlalchemy

如 H4 所述,代码里搜不到任何 `import sqlalchemy`。

---

## 🟢 Low (建议)

| # | 问题 | 位置 |
|---|------|------|
| L1 | 根目录脏文件 `1`(adb 输出,与项目无关) | `d:\爬虫\1` |
| L2 | 根目录有 `Typora.zip`(已被 `git rm`,但 staged 状态未提交) | `git status` |
| L3 | 根目录中文乱码文件夹"分析数据"(`?????`),git 子模块/编码处理有问题 | git warning: `could not open directory '分析数据/...'` |
| L4 | `analyze_news_impact.py`(971 行)未被任何代码 import,疑似死代码 | `core/analyze_news_impact.py` |
| L5 | `tools/` 下两个测试脚本写死 F:\爬虫,无法在当前 D 盘运行 | `tools/test_source_field.py:88` |
| L6 | README 的 EXE 启动路径 `dist\新闻爬虫系统\新闻爬虫系统.exe` 与实际打包配置 `build.spec` 可能不一致 | 需验证 |
| L7 | README 提到 `requirements_pyqt.txt` / `install_pyqt.bat` / `start.bat`,实际文件名是 `requirements.txt`、`install.bat`、`一键启动.bat` | README、根目录 |
| L8 | README 中"使用 SQLAlchemy + SQLite",实际未使用 | README:467-468 |
| L9 | `ai_config.json` 中 `current_prompt_template: comprehensive`,而 `get_default_prompt_templates()` 不存在 comprehensive,配置丢失会无法回退 | `core/ai_config.py` |
| L10 | `set_api_key` 把 key 明文落盘,无任何保护(至少应支持环境变量优先) | `core/ai_config.py:136-142` |
| L11 | 多处 console emoji 输出,Windows cmd / 旧版 PowerShell 编码差时会报 UnicodeEncodeError | 全项目多处 |
| L12 | 缺少 `core/__init__.py`(Glob 查不到),靠 sys.path 工作,打包时易出问题 | `core/` |

---

## 📐 架构层补充观察

### 优点
1. **GUI/core/workflows 三层分离清晰**,职责边界基本合理
2. **PyQt5 用 QThread + signal 做后台任务**,主线程不会卡(架构上对的)
3. **WorkflowBase 抽象不错**,有 logger / history / params 三件套
4. **支持多 AI 服务商抽象**,易扩展(虽然实现重复)

### 缺点
1. **Storage 层缺位**: 文件系统直接当数据库用,数据量超过几十 MB 后查询会很慢
2. **配置层职责混乱**: `core/config.py`(常量)+ `core/ai_config.py`(JSON 持久化)+ `config/*.json`(JSON 文件)+ workflow 单独的 `configs/*.json`,4 套配置体系没有统一接口
3. **测试缺失**: `tools/` 下只有 2 个一次性测试脚本,没有 `tests/` 目录,没有 pytest,没有 CI
4. **打包路径混乱**: `getattr(sys, 'frozen', False)` 处理打包路径的逻辑分散在 `main.py`、`gui/main_window.py`、`core/news_crawler_scroll.py` 三处,应该集中到一个 `paths.py`

---

## 🎯 建议处理顺序

**今天必须做**:
1. 修复 C1 + C2 + C3(API Key 旋转 + .gitignore + 字典 bug)

**这周做**:
2. H1 / H2 / H3 / H4(去硬编码、修 headless、修 stop()、改名 db_helper)
3. M1(语义去重参数与注释一致)
4. L1 / L2(清理根目录脏文件)

**有空再做**:
5. H5 / H6(消除重复代码)
6. M3(统一 logging)
7. M6(GUI 懒加载)
8. M7(实现真正的多种调度模式 或 删掉 GUI 的假选项)
9. 补 tests/

---

## 修复优先级总结

```
C1 → C2 → C3 → H1 → H2 → H3 → H4 → H5 → H6 → H7 → M系列 → L系列
```

主人确认后,辉夜会按这个顺序往下修。第一步只动 C1+C2+C3+L1(api key 安全 + 删根目录脏文件),不破坏功能。
