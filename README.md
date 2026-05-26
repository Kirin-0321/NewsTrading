# A股新闻爬虫与 AI 分析系统

<div align="center">

![Version](https://img.shields.io/badge/version-2.0-blue)
![Python](https://img.shields.io/badge/python-3.9+-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

**PyQt5 桌面应用：新闻采集 → AI 清洗 → 投资分析 → 题材预测**

[快速开始](#快速开始) · [核心功能](#核心功能) · [配置说明](#配置说明) · [项目结构](#项目结构) · [故障排查](#故障排查)

</div>

---

## 项目简介

面向 A 股投资者的本地工具：自动爬取格隆汇等源站新闻，经 AI 筛选高价值条目，再生成 Markdown 分析报告，并可从报告中抽取结构化题材入库，支持定时无人值守。

### 核心特性

- **SQLite 主库** — 原始 / 精选 / 剔除 / 题材预测分表存储，按时间范围查询
- **三段流水线** — `crawl_sync` → `clean_sync` → `analyze`，GUI 与定时任务共用 `services/`
- **多模型支持** — DeepSeek、OpenAI、通义千问、智谱、火山引擎（OpenAI 兼容接口）
- **模型分流** — 分析默认 `deepseek-v4-pro`（思考模式）；清洗固定 `deepseek-v4-flash`
- **增量爬取** — 按库内最新 `published_ts` 截断，遇重复自动停止
- **题材预测** — 分析完成后可自动抽取题材、标的、新闻关联并入库
- **定时调度** — 每日定点或按小时间隔执行爬取 / 清洗 / 分析

---

## 快速开始

### 环境要求

- Windows 10+
- Python 3.9+（推荐 3.10/3.11）
- 项目自带 `chrome-win64/`（Chrome + ChromeDriver，爬虫必需）

### 1. 安装依赖

```batch
双击 install.bat
```

或：

```bash
pip install -r requirements.txt
```

### 2. 配置 API 密钥

```bash
# 复制环境变量模板（推荐）
copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 等
```

程序启动时会加载项目根目录的 `.env`（**优先于** `config/ai_config.json` 中的 key）。

可选：复制 AI 与提示词配置：

```bash
copy config\ai_config.example.json config\ai_config.json
```

`config/ai_config.json` 已在 `.gitignore` 中，请勿提交真实密钥。

### 3. 启动程序

```batch
双击 启动.bat
```

或：

```bash
python main.py
```

首次启动会自动初始化 `data/news.db`。

### 4. 打包为 EXE（可选）

```batch
双击 一键打包.bat
```

需已安装 PyInstaller（见 `requirements.txt`）。打包后请将 `chrome-win64/`、`图标.ico` 等与 exe 放在同级目录。

---

## 核心功能

### 界面导航

| 页面 | 说明 |
|------|------|
| 爬虫管理 | 滚动爬取 → 写入原始库，支持增量与无头模式 |
| 数据管理 | 浏览库内统计、合并去重（语义去重） |
| 新闻清洗 | 对未清洗增量做 AI keep/remove |
| 数据导出 | 按时间范围导出 JSON / Markdown / TXT 到 `data/exports/` |
| AI 分析 | 从原始库或精选库生成报告，可选盘后总结、自动抽题材 |
| 预测题材 | 查看 / 手动从报告抽取题材快照 |
| 定时任务 | 管理 `crawl_sync` / `clean_sync` / `analyze` 调度 |

### 典型日流程

```
定时 crawl_sync（可串联 auto_clean）
    → raw_news (clean_status='pending')
定时或手动 clean_sync（通常爬取任务已串联清洗）
    → UPDATE raw_news SET clean_status IN ('curated','rejected'), clean_reason=...
手动或定时 analyze（默认读 clean_status='curated'）
    → data/AI_analysis/…/*.md
    → theme_predictions（若开启自动抽取）
```

### 定时任务类型

配置保存在 `data/schedule_tasks.json`（程序运行期间需保持开启）。

| type | 含义 |
|------|------|
| `crawl_sync` | 爬取并入库；`params.auto_clean: true` 时完成后自动清洗 |
| `clean_sync` | 仅清洗未处理原始数据 |
| `analyze` | 按时间窗口分析精选库（或 `source: raw`） |

支持 **每日定点**（`time`: `"09:00"`）或 **按小时间隔**（`interval_hours`: `1`）。

示例：

```json
{
  "id": "uuid",
  "name": "早盘爬取+清洗",
  "type": "crawl_sync",
  "time": "09:00",
  "enabled": true,
  "params": {
    "scroll_times": 36,
    "wait_seconds": 6,
    "incremental": true,
    "headless": true,
    "auto_clean": true,
    "batch_size": 100,
    "provider": "deepseek"
  }
}
```

---

## 配置说明

### 环境变量（`.env`）

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek（推荐） |
| `OPENAI_API_KEY` | OpenAI 兼容接口 |
| `QWEN_API_KEY` | 通义千问 |
| `ZHIPU_API_KEY` | 智谱 |
| `VOLCENGINE_API_KEY` | 火山引擎 |

### AI 配置（`config/ai_config.json`）

- `current_provider` — 当前服务商
- `providers.*` — 各家的 `base_url`、`model`、`max_tokens` 等
- `prompt_templates` — 标准 / 激进 / 稳健 / 价值 / 短线 五套分析模板
- `theme_extraction` — 分析后自动抽题材：`enabled`、`auto_run`、`model`、`timeout`（秒，默认 1200，大报告 JSON 较慢时可调高）等

分析使用配置中的 `model`；**清洗**在 provider 为 deepseek 时固定使用 `deepseek-v4-flash`（与配置中的 model 无关）。

### 清洗规则（`config/cleaning_criteria.json`）

定义保留 / 剔除规则与 AI 系统提示词，由 `CleanSyncService` 加载。

---

## 项目结构

```
NewsTrading/
├── main.py                 # 入口：加载 .env → init_database() → GUI
├── 启动.bat / install.bat  # 启动与安装依赖
├── 一键打包.bat            # PyInstaller 打包
├── requirements.txt
├── .env.example
├── 图标.ico
│
├── config/
│   ├── ai_config.example.json
│   └── cleaning_criteria.json
│
├── core/                   # 底层能力（由 services 调用）
│   ├── news_crawler_scroll.py
│   ├── news_cleaner.py
│   ├── ai_news_analyzer.py
│   ├── theme_extractor.py
│   ├── semantic_dedup.py
│   ├── scheduler_service.py
│   └── ai_config.py
│
├── services/               # 业务服务层
│   ├── crawl_sync_service.py
│   ├── clean_sync_service.py
│   ├── analysis_service.py
│   ├── scheduled_runner.py
│   └── storage/            # SQLite：raw_news (含 clean_status) / theme_*
│
├── gui/
│   ├── main_window.py
│   ├── pages/
│   └── workers/
│
├── agent/                  # 函数级 API（供脚本/后续 MCP 使用）
│   └── __init__.py         # crawl_sync, clean_sync, analyze_news, get_db_stats
│
├── data/
│   ├── news.db             # 主库（gitignore，本地生成）
│   ├── schedule_tasks.json # 定时任务
│   ├── exports/            # 导出文件
│   └── AI_analysis/        # 分析报告（按日期分子目录）
│
├── chrome-win64/           # 浏览器与驱动（需自行准备或打包带入）
├── doc/                    # 历史功能文档（部分章节已过时，以本 README 为准）
└── .huiye/                 # 架构说明与 review 记录（开发用）
```

### 数据流

```
[源站] → crawl_sync → raw_news.clean_status = pending
              ↓
         clean_sync → raw_news.clean_status ∈ {curated, rejected}, clean_reason 填实
              ↓
         analyze（默认筛 clean_status=curated）
              → Markdown 报告 + theme_predictions / theme_stocks / theme_news
```

### 技术栈

| 类别 | 技术 |
|------|------|
| GUI | PyQt5 |
| 爬虫 | Selenium + BeautifulSoup4 |
| 数据库 | SQLite（标准库 `sqlite3`） |
| AI | OpenAI SDK、zhipuai |
| 调度 | schedule |
| 打包 | PyInstaller |

---

## 故障排查

### 程序无法启动

```bash
python --version    # 需 3.9+
pip install -r requirements.txt
python -c "from PyQt5.QtWidgets import QApplication"
```

### 爬虫失败 / ChromeDriver 错误

1. 确认 `chrome-win64/chrome.exe` 与 `chromedriver.exe` 存在且版本匹配  
2. 可从 [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 更新  
3. 打包运行时将 `chrome-win64` 放在 exe 同级目录

### AI 调用失败

1. 检查 `.env` 或 GUI 中 API Key 是否有效  
2. 查看控制台 / 日志输出  
3. 分析任务可尝试切换 `current_provider`  
4. DeepSeek 旧别名 `deepseek-chat` 将于 2026-07-24 停用，请使用 `deepseek-v4-pro` / `deepseek-v4-flash`

### database is locked

1. 关闭多余程序实例  
2. 确认无其他进程占用 `data/news.db`  
3. 重启应用（库连接使用 WAL + busy_timeout）

### 定时任务不执行

- 程序窗口需保持运行（调度在后台线程）  
- 检查 `data/schedule_tasks.json` 中 `enabled` 是否为 `true`  
- 修改任务后可在「定时任务」页刷新或重启应用

---

## 安全说明

- 勿将 `.env`、`config/ai_config.json` 提交到 Git  
- 分享项目前删除或脱敏 `data/schedule_tasks.json` 中的个人配置  
- 所有数据默认本地存储，不上传云端  

---

## 开发说明

- 架构与实施记录：`.huiye/架构方案.md`  
- 代码 review：`.huiye/review_report.md`  
- 历史 `doc/` 中关于 `workflows/` 的文档描述的是**已移除**的旧版任务流，请以本文与 `services/` 为准  
- `agent/` 当前为 Python 函数导出；MCP Server 尚未实现  

### 脚本调用示例

```python
from agent import crawl_sync, clean_sync, analyze_news, get_db_stats

print(get_db_stats())
crawl_sync(scroll_times=36, incremental=True)
clean_sync(batch_size=100)
result = analyze_news(source="curated", hours=24)
print(result.report_path)
```

---

## 致谢

- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/)
- [Selenium](https://www.selenium.dev/)
- [DeepSeek](https://www.deepseek.com/)
