# 📈 A股新闻爬虫与AI分析系统

<div align="center">

![Version](https://img.shields.io/badge/version-2.0-blue)
![Python](https://img.shields.io/badge/python-3.7+-green)
![License](https://img.shields.io/badge/license-MIT-orange)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

**一个集新闻采集、智能清洗、AI分析、工作流自动化于一体的专业级A股投资辅助系统**

[快速开始](#快速开始) • [核心功能](#核心功能) • [使用指南](#使用指南) • [开发文档](#开发文档)

</div>

---

## 📋 项目简介

这是一个功能完整的PyQt5桌面应用程序，专为A股投资者和量化交易员设计。系统通过自动化采集新闻数据、AI智能筛选、多策略分析，帮助用户快速把握市场机会，提升投资决策效率。

### ✨ 核心特性

- 🤖 **AI深度集成** - 支持DeepSeek、智谱AI、通义千问等多个国产大模型
- 🔄 **全流程自动化** - 爬取→清洗→导出→分析，一键完成
- 📊 **多策略分析** - 提供标准、激进、稳健、价值、短线5种投资策略
- 🧹 **智能新闻清洗** - AI自动过滤无价值新闻，保留投资催化剂
- 📈 **增量数据采集** - 智能识别最新数据，避免重复爬取
- ⏰ **定时任务调度** - 支持每日自动执行，无人值守运行
- 💻 **可视化界面** - 全GUI操作，实时进度反馈，无需命令行
- 📦 **一键打包部署** - 可打包成exe独立运行，包含Chrome浏览器

---

## 🚀 快速开始

### 方式一：一键启动（推荐）

```batch
# 1. 安装依赖（首次运行）
双击 install_pyqt.bat

# 2. 启动程序
双击 start.bat
```

### 方式二：命令行启动

```bash
# 1. 安装依赖
pip install -r requirements_pyqt.txt

# 2. 运行程序
python main.py
```

### 方式三：使用打包后的EXE

```batch
# 直接运行（无需Python环境）
双击 dist\新闻爬虫系统\新闻爬虫系统.exe
```

---

## 🎯 核心功能

### 1️⃣ 智能爬虫模块

<details>
<summary><b>点击展开详情</b></summary>

#### 功能特性
- ✅ **滚动加载** - 模拟用户滚动操作，自动加载历史新闻
- ✅ **增量爬取** - 自动检测数据库最新记录，只爬取新增新闻
- ✅ **无头模式** - 后台运行，不影响其他工作
- ✅ **实时反馈** - 进度条、计数器、日志实时显示
- ✅ **智能停止** - 达到目标或检测到重复后自动停止

#### 使用方法
1. 进入"爬虫管理"页面
2. 设置滚动次数（默认36次）和等待时间（默认6秒）
3. 勾选"无头模式"和"自动停止"
4. 点击"开始爬取"，等待完成
5. 查看生成的JSON文件：`data/raw/日期_时间.json`

#### 技术实现
- **引擎**: Selenium + Chrome WebDriver
- **解析器**: BeautifulSoup4
- **存储**: JSON格式 + SQLite数据库
- **去重**: 基于标题和时间的智能去重

</details>

---

### 2️⃣ AI新闻清洗

<details>
<summary><b>点击展开详情</b></summary>

#### 功能特性
- 🧹 **AI驱动判断** - 使用大模型智能评估新闻价值
- 📋 **标准化规则** - 基于投资价值的清洗标准
- 📊 **批量处理** - 支持单文件或多文件批量清洗
- 📈 **统计报告** - 详细的保留/移除统计和分析

#### 清洗标准

**✅ 保留规则**：
- 🏛️ **政策类** - 国家级政策、行业规划、监管政策
- 🔬 **技术突破** - 重大创新、国产替代、技术升级
- 💰 **财报业绩** - 龙头公司业绩超预期、重大合同（>10亿）
- 📊 **市场数据** - 行业整体数据、市场格局变化
- 💵 **资金动向** - 大型融资（>10亿）、战略投资、股权变动
- 🌍 **国际事件** - 国际局势、贸易政策、汇率变化

**❌ 去除规则**：
- 日常运营、小公司动向（市值<100亿）
- 个股日常（检修、维护、小额订单）
- 展会活动、人事变动、常规产品发布
- 无实质内容、标题党、重复信息

#### 使用方法
1. 进入"新闻清洗"页面
2. 选择要清洗的数据文件
3. 选择AI服务商（DeepSeek推荐）
4. 设置批量大小（默认500条）
5. 点击"开始清洗"
6. 查看结果：`data/cleaned/日期_clear.json`

</details>

---

### 3️⃣ AI投资分析

<details>
<summary><b>点击展开详情</b></summary>

#### 五大分析策略

| 策略模板 | 适用场景 | 核心特点 | 推荐人群 |
|---------|---------|---------|---------|
| 📊 **标准分析** | 全市场分析 | 新闻重要性分级，平衡型策略 | 普通投资者 |
| 🚀 **激进策略** | 追逐热点 | 关注爆发性机会，高风险高回报 | 短线激进投资者 |
| 🛡️ **稳健策略** | 稳健投资 | 确定性优先，风险控制为主 | 稳健型投资者 |
| 💎 **价值投资** | 长线投资 | 寻找低估资产，价值重估 | 价值投资者 |
| ⚡ **短线交易** | 日内/短线 | 情绪分析、次日推演、操作预案 | 短线交易员 |

#### 智能数量决策

系统支持"自动模式"，根据新闻质量和市场分化程度，智能决定：
- **板块数量**: 3-10个（主线明确时集中，分化时分散）
- **股票数量**: 每个板块3-10只（根据板块容量调整）
- **原则**: 宁缺毋滥，只推荐有充分依据的标的

#### 盘后总结集成

- 📝 自动读取前日盘后总结
- 🔗 结合市场情绪和资金流向
- 🎯 提升次日策略准确性

#### 使用方法
1. 进入"AI分析"页面
2. 选择数据源（原始/清洗后）
3. 选择分析模板和AI服务商
4. 设置板块数量和股票数量（可选"自动"）
5. 可选：添加盘后总结文件
6. 点击"开始分析"
7. 查看报告：`data/AI_analysis/日期_时间.md`

#### 报告内容

- 📰 **新闻分级** - 重点新闻vs一般新闻
- 📊 **板块分析** - 核心催化剂、投资逻辑、龙头推荐
- 🎯 **介入时机** - 最佳买入时点和目标涨幅
- ⚠️ **风险提示** - 明确标注风险点
- 🔗 **新闻引用** - Markdown锚点链接，可直接跳转

</details>

---

### 4️⃣ 数据管理

<details>
<summary><b>点击展开详情</b></summary>

#### 功能特性
- 📁 **文件浏览** - 查看所有数据文件（JSON/Markdown/TXT）
- 👁️ **内容预览** - 直接在界面中预览文件内容
- 🗑️ **批量删除** - 清理不需要的文件
- 🔗 **合并去重** - 多文件合并 + 语义去重

#### 数据目录结构

```
data/
├── raw/              # 原始爬取数据
│   └── 12-27_09-00.json
├── cleaned/          # 清洗后数据
│   └── 12-27_clear.json
├── exports/          # 导出数据
│   └── 2025-12-27_export.md
├── AI_analysis/      # AI分析报告
│   └── 2025-12-27_analysis.md
└── summaries/        # 历史摘要
```

#### 语义去重

- 🔍 使用TF-IDF算法检测语义相似度
- 📊 支持相似度阈值调整（默认0.85）
- 🎯 保留最新或信息最完整的版本

</details>

---

### 5️⃣ 数据导出

<details>
<summary><b>点击展开详情</b></summary>

#### 支持格式
- 📄 **JSON** - 结构化数据，便于程序处理
- 📝 **Markdown** - 可读性强，支持标题、链接、列表
- 📋 **TXT** - 纯文本格式
- 📊 **Excel** - 表格格式（开发中）

#### 智能筛选
- ⏱️ **时间范围** - 按日期范围导出
- 📂 **数据源** - 原始数据/清洗后数据
- 🔍 **关键词** - 按关键词过滤（开发中）

#### 使用方法
1. 进入"数据导出"页面
2. 选择数据源和时间范围
3. 勾选导出格式
4. 点击"导出数据"
5. 选择保存位置
6. 自动打开导出目录

</details>

---

### 6️⃣ 定时任务

<details>
<summary><b>点击展开详情</b></summary>

#### 调度模式
- ⏰ **一次性** - 指定时间执行一次
- 🔄 **每小时** - 每小时执行
- 📅 **每日** - 指定时间每天执行
- ⏱️ **间隔** - 按固定间隔执行

#### 任务管理
- ➕ 创建新任务
- ✏️ 编辑任务参数
- ⏸️ 启用/禁用任务
- 📜 查看执行历史
- 🗑️ 删除任务

#### 典型任务
```
任务：每日新闻采集
时间：每天早上8:00
动作：爬取昨夜到今早的新闻
```

</details>

---

### 7️⃣ 工作流引擎

<details>
<summary><b>点击展开详情</b></summary>

#### 内置工作流

**每日新闻分析流程**（daily_news_flow）

```
Step 1: 爬取新闻
  ↓ 启用增量爬取，只获取新增新闻
  
Step 2: 清洗数据
  ↓ 使用AI过滤无价值新闻
  
Step 3: 导出数据
  ↓ 导出为Markdown格式
  
Step 4: AI分析
  ↓ 结合盘后总结，生成投资策略
  
📄 输出: 完整的分析报告
```

#### 工作流特性
- 🔧 **配置化** - JSON配置文件定义流程
- 📝 **日志记录** - 详细的执行日志
- 🔄 **错误处理** - 失败重试、状态跟踪
- ⏱️ **定时执行** - 支持定时自动运行
- 📊 **执行统计** - 成功率、耗时统计

#### 自定义工作流

可在`workflows/`目录创建自定义工作流：

```python
from workflows.base import WorkflowBase

class MyWorkflow(WorkflowBase):
    workflow_id = "my_workflow"
    name = "我的工作流"
    
    def execute(self, params):
        # 实现你的逻辑
        pass
```

</details>

---

## 📖 使用指南

### 典型工作流程

#### 场景1：每日自动分析（推荐）

```
1. 设置定时任务
   - 进入"定时任务"页面
   - 创建任务，选择"每日执行"
   - 设置时间：9:00
   - 保存

2. 系统自动执行
   - 每天9点自动运行
   - 爬取 → 清洗 → 导出 → 分析
   - 生成报告到 data/AI_analysis/

3. 查看报告
   - 使用Typora或其他Markdown编辑器打开
   - 报告包含板块推荐、个股分析、操作建议
```

#### 场景2：手动深度分析

```
1. 手动爬取
   - 进入"爬虫管理"
   - 设置滚动次数：72（爬取更多历史数据）
   - 开始爬取

2. 精细清洗
   - 进入"新闻清洗"
   - 根据需要调整清洗标准
   - 执行清洗

3. 多策略分析
   - 进入"AI分析"
   - 分别使用"激进"、"稳健"、"短线"模板
   - 生成多份报告对比

4. 数据导出
   - 导出为Excel进行进一步分析
```

#### 场景3：历史数据回测

```
1. 数据合并
   - 进入"数据管理"
   - 选择多个历史文件
   - 执行"合并去重"

2. 重新分析
   - 使用合并后的数据
   - 选择不同的分析策略
   - 对比历史准确性
```

---

## ⚙️ 配置说明

### AI模型配置

编辑 `config/ai_config.json`：

```json
{
  "current_provider": "deepseek",  // 当前使用的模型
  "providers": {
    "deepseek": {
      "api_key": "your-api-key",
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "max_tokens": 4000,
      "temperature": 0.7
    }
  }
}
```

**支持的模型**：
- DeepSeek (推荐，性价比高)
- 智谱AI (GLM-4)
- 通义千问 (Qwen-Max)
- 火山引擎 (豆包)
- OpenAI (GPT-4)

### 清洗规则配置

编辑 `config/cleaning_criteria.json`：

```json
{
  "default": {
    "name": "投资价值优先",
    "criteria": "保留规则和去除规则",
    "system_prompt": "AI清洗提示词"
  }
}
```

### 工作流配置

编辑 `workflows/configs/daily_news_flow.json`：

```json
{
  "enabled": true,
  "schedule": {
    "enabled": true,
    "time": "09:00"
  },
  "params": {
    "crawler": {
      "scroll_times": 36,
      "auto_stop": true
    },
    "analyzer": {
      "template": "short_term"
    }
  }
}
```

---

## 🛠️ 开发文档

### 技术栈

| 类别 | 技术 | 版本 |
|-----|------|------|
| **GUI框架** | PyQt5 | 5.15.9+ |
| **爬虫引擎** | Selenium | 4.15.0+ |
| **HTML解析** | BeautifulSoup4 | 4.12.0+ |
| **数据库** | SQLAlchemy | 2.0.0+ |
| **AI接口** | OpenAI SDK | 1.0.0+ |
| **任务调度** | Schedule | 1.2.0+ |
| **打包工具** | PyInstaller | 6.0.0+ |

### 项目结构

```
F:\爬虫/
├── main.py                    # 程序入口
├── start.bat                  # 启动脚本
├── build.spec                 # 打包配置
├── requirements_pyqt.txt      # 依赖列表
├── 图标.ico                   # 程序图标
│
├── config/                    # 配置文件
│   ├── ai_config.json        # AI模型配置
│   └── cleaning_criteria.json # 清洗规则
│
├── core/                      # 核心功能模块
│   ├── config.py             # 基础配置
│   ├── news_crawler_scroll.py # 爬虫核心
│   ├── news_cleaner.py       # 清洗核心
│   ├── ai_news_analyzer.py   # AI分析核心
│   ├── news_exporter.py      # 导出功能
│   ├── data_merger.py        # 数据合并
│   ├── semantic_dedup.py     # 语义去重
│   ├── db_helper.py          # 数据库操作
│   ├── workflow_engine.py    # 工作流引擎
│   └── scheduler_service.py  # 定时任务服务
│
├── gui/                       # 界面模块
│   ├── main_window.py        # 主窗口
│   ├── pages/                # 功能页面
│   │   ├── crawler_page.py
│   │   ├── data_page.py
│   │   ├── news_cleaning_page.py
│   │   ├── analysis_page.py
│   │   ├── export_page.py
│   │   ├── ai_analysis_page.py
│   │   └── schedule_page.py
│   ├── workers/              # 后台线程
│   │   ├── crawler_worker.py
│   │   └── analysis_worker.py
│   └── utils/                # 工具函数
│       └── styles.py
│
├── workflows/                 # 工作流模块
│   ├── base.py               # 工作流基类
│   ├── daily_news_flow.py    # 每日新闻流程
│   └── configs/              # 工作流配置
│
├── data/                      # 数据目录
│   ├── raw/                  # 原始数据
│   ├── cleaned/              # 清洗后数据
│   ├── exports/              # 导出数据
│   ├── AI_analysis/          # AI分析报告
│   └── crawler.db            # SQLite数据库
│
├── logs/                      # 日志目录
│   ├── crawler.log
│   ├── analysis.log
│   └── workflows/            # 工作流日志
│
├── chrome-win64/              # Chrome浏览器
│   ├── chrome.exe
│   └── chromedriver.exe
│
└── doc/                       # 文档目录
    ├── features/             # 功能说明
    ├── guides/               # 使用指南
    └── updates/              # 更新日志
```

### 核心类说明

#### 1. 爬虫类（GuZhangNewsCrawlerScroll）

```python
class GuZhangNewsCrawlerScroll:
    """新闻爬虫核心类"""
    
    def __init__(self, save_dir='data', 
                 chrome_path=None,
                 chromedriver_path=None):
        """初始化爬虫"""
        
    def set_auto_stop(self, enabled, latest_time, latest_title):
        """设置增量爬取参数"""
        
    def fetch_page_with_scroll(self, scroll_times, wait_seconds):
        """滚动加载页面"""
        
    def parse_news(self, html):
        """解析新闻数据"""
        
    def save_data(self, news_list):
        """保存数据"""
```

#### 2. AI分析类（AINewsAnalyzer）

```python
class AINewsAnalyzer:
    """AI新闻分析核心类"""
    
    def __init__(self, config):
        """初始化分析器"""
        
    def analyze_with_openai(self, news_data, 
                           max_sectors, 
                           stocks_per_sector,
                           template_name):
        """使用OpenAI API分析"""
        
    def build_user_prompt(self, news_data, 
                         max_sectors,
                         stocks_per_sector):
        """构建提示词"""
```

#### 3. 工作流基类（WorkflowBase）

```python
class WorkflowBase:
    """工作流基类"""
    
    def run(self, params):
        """运行工作流"""
        
    def execute(self, params):
        """执行具体逻辑（子类实现）"""
        
    def log(self, message, level="info"):
        """记录日志"""
```

---

## 🐛 故障排查

### 常见问题

#### 问题1：程序无法启动

**症状**：双击start.bat后闪退

**解决方案**：
```bash
# 1. 检查Python版本（需要3.7+）
python --version

# 2. 重新安装依赖
pip install -r requirements_pyqt.txt

# 3. 检查PyQt5是否安装成功
python -c "from PyQt5.QtWidgets import QApplication"
```

#### 问题2：ChromeDriver错误

**症状**：爬虫启动失败，提示ChromeDriver相关错误

**解决方案**：
```
1. 检查chrome-win64目录是否存在
2. 检查chrome.exe和chromedriver.exe是否存在
3. 下载最新版本Chrome for Testing：
   https://googlechromelabs.github.io/chrome-for-testing/
4. 确保Chrome和ChromeDriver版本匹配
```

#### 问题3：AI分析失败

**症状**：分析时报错，提示API调用失败

**解决方案**：
```
1. 检查config/ai_config.json中的API Key是否正确
2. 检查网络连接是否正常
3. 检查API余额是否充足
4. 尝试切换到其他AI服务商
5. 查看logs/analysis.log获取详细错误信息
```

#### 问题4：数据库锁定错误

**症状**：提示database is locked

**解决方案**：
```
1. 关闭所有打开的程序实例
2. 删除data/crawler.db-journal文件（如果存在）
3. 重新启动程序
```

#### 问题5：打包后的EXE无法运行

**症状**：双击exe文件后无反应

**解决方案**：
```
1. 检查build.spec配置是否正确
2. 确保所有依赖文件都包含在打包中
3. 检查图标文件、Chrome目录是否在exe同级目录
4. 以管理员身份运行
5. 查看日志文件获取错误信息
```

---

## 📊 性能优化

### 爬虫性能

- **并发爬取**: 使用多线程提升速度（开发中）
- **智能等待**: 根据网络状况动态调整等待时间
- **增量更新**: 只爬取新增数据，节省时间

### AI分析性能

- **批量处理**: 一次API调用处理多条新闻
- **提示词优化**: 精简提示词，减少token消耗
- **缓存机制**: 缓存重复分析结果（开发中）

### 数据库性能

- **索引优化**: 在标题、时间字段建立索引
- **定期清理**: 删除过期数据
- **批量插入**: 减少数据库写入次数

---

## 🔒 安全说明

### API密钥安全

- ⚠️ **不要将API Key提交到Git仓库**
- ✅ 使用环境变量或配置文件管理密钥
- ✅ 定期更换API Key
- ✅ 设置API调用限制

### 数据安全

- 🔐 本地存储，数据不上传云端
- 📁 定期备份data目录
- 🗑️ 及时清理敏感数据

### 网络安全

- 🛡️ 使用HTTPS协议
- 🔍 验证API响应合法性
- ⏱️ 设置请求超时时间

---

## 📈 未来规划

### v2.1 计划功能

- [ ] 多数据源支持（财联社、东方财富等）
- [ ] 实时行情集成
- [ ] 舆情分析功能
- [ ] 个股深度研报生成
- [ ] Web端远程访问

### v3.0 计划功能

- [ ] 量化回测框架
- [ ] 交易信号生成
- [ ] 风险管理模块
- [ ] 投资组合管理
- [ ] 机器学习模型集成

---

## 🤝 贡献指南

欢迎贡献代码、报告问题或提出建议！

### 如何贡献

1. Fork本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 提交Pull Request

### 代码规范

- 使用PEP 8编码规范
- 添加适当的注释和文档字符串
- 编写单元测试
- 更新相关文档

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 💬 联系方式

- 📧 邮箱：your-email@example.com
- 💻 GitHub：https://github.com/yourusername/your-repo
- 📝 问题反馈：[Issues](https://github.com/yourusername/your-repo/issues)

---

## 🙏 致谢

感谢以下开源项目：

- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) - 强大的GUI框架
- [Selenium](https://www.selenium.dev/) - 浏览器自动化工具
- [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/) - HTML解析库
- [OpenAI](https://openai.com/) - AI API服务
- [DeepSeek](https://www.deepseek.com/) - 国产大模型

---

## ⭐ Star History

如果这个项目对你有帮助，请给个Star ⭐️

---

<div align="center">

**用AI赋能投资决策，让数据创造价值**

Made with ❤️ by [Your Name]

</div>

