"""
AI分析配置管理
支持多种AI服务商的配置和管理

【Phase M0 改造】：prompt 模板不再存储于 config/ai_config.json，
改为读取 prompts/{category}/{id}.md 文件。本类对外的 get_prompt_template /
save_template / delete_template 接口保持不变（依然返回老的 dict 格式），
内部通过 core.prompt_loader.PromptLoader 提供持久化。

迁移工具: ``tools/migrate_prompts_to_files.py``
"""

import os
import json
from typing import Dict, Optional

from core.env_loader import load_dotenv
from core.prompt_loader import (
    PromptError,
    PromptLoader,
    PromptNotFoundError,
)

load_dotenv()

# prompt 模板默认放在该 category 下；Phase M0 仅 analysis 一类
_PROMPT_CATEGORY_ANALYSIS = "analysis"

# 内置（迁移自老 ai_config.json）的 analysis prompt 模板 ID，
# GUI 编辑/删除时按此清单判断"是否内置"。
BUILTIN_PROMPT_TEMPLATES = frozenset({
    "standard", "aggressive", "conservative", "value",
    "short_term", "comprehensive", "default",
})

# DeepSeek V4 等大上下文模型：各阶段输出 token 下限（可通过 providers.*.max_tokens 上调）
DEFAULT_MAX_OUTPUT_TOKENS = 65536
# 清洗每条约 keep|理由 行估算 token（含序号/标点），用于 batch 动态下限
CLEANING_TOKENS_PER_ITEM = 80

# 环境变量优先于配置文件中的 api_key
_PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "qwen": "QWEN_API_KEY",
    "volcengine": "VOLCENGINE_API_KEY",
}


class AIConfig:
    """AI配置管理类"""

    def __init__(self):
        self.config_file = 'config/ai_config.json'
        self.config = self.load_config()
        self._prompt_loader: Optional[PromptLoader] = None
        # 首次运行时若 prompts/ 目录尚未生成（旧用户首次升级），
        # 把 config 里残留的 prompt_templates 兜底落盘
        self._bootstrap_prompts_if_needed()

    def load_config(self) -> Dict:
        """加载配置文件；若不存在则从 example 复制或生成默认配置"""
        example_file = "config/ai_config.example.json"
        if not os.path.exists(self.config_file):
            if os.path.exists(example_file):
                os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
                with open(example_file, "r", encoding="utf-8") as src:
                    with open(self.config_file, "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            else:
                config = self.get_default_config()
                self.save_config(config)
                return config

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"加载配置失败: {e}")
            return self.get_default_config()

    def get_default_config(self) -> Dict:
        """获取默认配置"""
        return {
            "current_provider": "deepseek",
            "current_prompt_template": "standard",
            "current_template": "standard",
            "prompt_templates": self.get_default_prompt_templates(),
            "providers": {
                "openai": {
                    "api_key": "",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4",
                    "max_tokens": 16384,
                    "temperature": 0.7
                },
                "deepseek": {
                    "api_key": "",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-pro",
                    "max_tokens": 65536,
                    "temperature": 0.7
                },
                "zhipu": {
                    "api_key": "",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-4",
                    "max_tokens": 16384,
                    "temperature": 0.7
                },
                "qwen": {
                    "api_key": "",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "qwen-max",
                    "max_tokens": 16384,
                    "temperature": 0.7
                },
                "volcengine": {
                    "api_key": "",
                    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "model": "doubao-seed-1-6-251015",
                    "max_tokens": 32768,
                    "temperature": 0.7
                }
            },
            "analysis_params": {
                "max_sectors": 6,
                "stocks_per_sector": 5,
                "detail_level": "standard",
                "max_input_tokens": 900000,
                "deep_thinking_enabled": True,
            },
        }

    def save_config(self, config: Optional[Dict] = None):
        """保存配置到文件"""
        if config is None:
            config = self.config

        # 确保目录存在
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)

        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败: {e}")
            return False

    def get_current_provider(self) -> str:
        """获取当前使用的AI服务商"""
        return self.config.get('current_provider', 'openai')

    def set_current_provider(self, provider: str):
        """设置当前使用的AI服务商"""
        if provider in self.config['providers']:
            self.config['current_provider'] = provider
            self.save_config()
            return True
        return False

    def get_provider_config(self, provider: Optional[str] = None) -> Dict:
        """获取指定服务商的配置（api_key 已合并环境变量）"""
        if provider is None:
            provider = self.get_current_provider()
        cfg = dict(self.config.get("providers", {}).get(provider, {}))
        api_key = self.get_api_key(provider)
        if api_key:
            cfg["api_key"] = api_key
        return cfg

    def set_api_key(self, provider: str, api_key: str):
        """设置API Key"""
        if provider in self.config['providers']:
            self.config['providers'][provider]['api_key'] = api_key
            self.save_config()
            return True
        return False

    def get_api_key(self, provider: Optional[str] = None) -> str:
        """获取 API Key：优先环境变量，其次配置文件"""
        if provider is None:
            provider = self.get_current_provider()
        env_name = _PROVIDER_ENV_KEYS.get(provider or "")
        if env_name:
            env_key = os.environ.get(env_name, "").strip()
            if env_key:
                return env_key
        cfg = self.config.get("providers", {}).get(provider, {})
        return (cfg.get("api_key") or "").strip()

    def get_model(self, provider: Optional[str] = None) -> str:
        """获取模型名称"""
        config = self.get_provider_config(provider)
        return config.get('model', 'gpt-4')

    def set_model(self, provider: str, model: str):
        """设置模型名称"""
        if provider in self.config['providers']:
            self.config['providers'][provider]['model'] = model
            self.save_config()
            return True
        return False

    def get_analysis_params(self) -> Dict:
        """获取分析参数"""
        return self.config.get('analysis_params', {})

    def get_max_output_tokens(self, provider: Optional[str] = None) -> int:
        """读取 AI 输出 max_tokens；未配置或非法时用 DEFAULT_MAX_OUTPUT_TOKENS。"""
        cfg = self.get_provider_config(provider)
        raw = cfg.get("max_tokens")
        if raw is None:
            return DEFAULT_MAX_OUTPUT_TOKENS
        try:
            return max(int(raw), 1)
        except (TypeError, ValueError):
            return DEFAULT_MAX_OUTPUT_TOKENS

    def get_cleaning_max_tokens(
        self, expected_count: int, provider: Optional[str] = None
    ) -> int:
        """清洗批次输出上限：配置值与「条数 × CLEANING_TOKENS_PER_ITEM」取较大值。"""
        base = self.get_max_output_tokens(provider)
        dynamic = max(int(expected_count) * CLEANING_TOKENS_PER_ITEM, 4096)
        # 清洗输出必须按批大小动态上浮，避免 providers.max_tokens 过低导致截断
        return min(max(base, dynamic), DEFAULT_MAX_OUTPUT_TOKENS)

    def get_theme_extraction_config(self) -> Dict:
        """获取题材抽取配置。

        默认值与 ai_config.example.json 保持一致，避免老配置文件缺失字段时崩溃。
        """
        defaults = {
            "enabled": True,
            "auto_run": True,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "temperature": 0.2,
            "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            # 流式读超时（秒）：大报告 + 高 max_tokens 时 JSON 生成可能超过 10 分钟
            "timeout": 1200,
        }
        cfg = dict(defaults)
        cfg.update(self.config.get("theme_extraction", {}) or {})
        provider = cfg.get("provider") or "deepseek"
        configured = cfg.get("max_tokens")
        try:
            configured_int = int(configured) if configured is not None else DEFAULT_MAX_OUTPUT_TOKENS
        except (TypeError, ValueError):
            configured_int = DEFAULT_MAX_OUTPUT_TOKENS
        cfg["max_tokens"] = max(configured_int, self.get_max_output_tokens(provider))
        return cfg

    def set_analysis_param(self, key: str, value):
        """设置分析参数"""
        if 'analysis_params' not in self.config:
            self.config['analysis_params'] = {}
        self.config['analysis_params'][key] = value
        self.save_config()

    def is_configured(self, provider: Optional[str] = None) -> bool:
        """检查是否已配置（有API Key）"""
        api_key = self.get_api_key(provider)
        return bool(api_key and api_key.strip())

    def get_available_providers(self) -> list:
        """获取所有可用的服务商列表"""
        return list(self.config['providers'].keys())

    def get_available_models(self, provider: str) -> list:
        """获取指定服务商支持的模型列表"""
        models = {
            'openai': ['gpt-4', 'gpt-4-turbo', 'gpt-3.5-turbo'],
            'deepseek': [
                # DeepSeek V4（2026-04-24 发布，1M 上下文，支持思考模式）
                'deepseek-v4-pro',         # 旗舰版，1.6T 参数，前沿推理/编码/长 Agent
                'deepseek-v4-flash',       # 经济版，284B 参数，高吞吐场景
                # 旧别名（将于 2026-07-24 停用，仅做兼容保留）
                'deepseek-chat',           # → 实际指向 v4-flash 非思考模式
                'deepseek-reasoner',       # → 实际指向 v4-flash 思考模式
            ],
            'zhipu': ['glm-4', 'glm-3-turbo'],
            'qwen': ['qwen-max', 'qwen-plus', 'qwen-turbo'],
            'volcengine': [
                'doubao-seed-1-6-251015',  # 豆包1.6 (推荐，长上下文)
                'doubao-pro-32k',          # 豆包Pro 32K
                'doubao-pro-128k',         # 豆包Pro 128K
                'doubao-lite-32k',         # 豆包Lite 32K
                'doubao-lite-128k'         # 豆包Lite 128K
            ]
        }
        return models.get(provider, [])

    # ========================================================
    # 提示词模板管理（Phase M0 起改为 PromptLoader 文件存储）
    # ========================================================

    def _get_prompt_loader(self) -> PromptLoader:
        """惰性持有 PromptLoader，所有 prompt 操作经此入口。"""
        if self._prompt_loader is None:
            self._prompt_loader = PromptLoader()
        return self._prompt_loader

    def _bootstrap_prompts_if_needed(self) -> None:
        """旧用户首次升级时，prompts/analysis 可能尚未生成。

        此时把 config 中残留的 prompt_templates（或内置默认）落到磁盘，
        保证后续 get_prompt_template 一定能读到。
        """
        loader = self._get_prompt_loader()
        try:
            existing = loader.list_category(_PROMPT_CATEGORY_ANALYSIS)
        except PromptError:
            existing = []
        if existing:
            return

        # 优先用残留的 prompt_templates（理论上瘦身后应为空，但保险起见）
        legacy = self.config.get("prompt_templates") or {}
        if not legacy:
            legacy = self.get_default_prompt_templates()
        if not legacy:
            return

        for prompt_id, tmpl in legacy.items():
            try:
                self._save_template_to_loader(
                    prompt_id,
                    tmpl.get("name") or prompt_id,
                    tmpl.get("system_prompt") or "",
                    tmpl.get("user_prompt_template") or "",
                )
            except Exception as e:  # noqa: BLE001
                print(f"[AIConfig] bootstrap {prompt_id} 失败: {e}")
        # 落盘后清理掉 config 内的残留，避免反复 bootstrap
        if "prompt_templates" in self.config:
            self.config.pop("prompt_templates", None)
            self.save_config()

    @staticmethod
    def _quote_yaml(value: str) -> str:
        s = "" if value is None else str(value)
        if "\n" in s or "\r" in s or "\t" in s:
            escaped = (
                s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            return f'"{escaped}"'
        return "'" + s.replace("'", "''") + "'"

    def _render_prompt_file(
        self,
        prompt_id: str,
        name: str,
        system_prompt: str,
        user_prompt_template: str,
    ) -> str:
        """构造 prompts/analysis/{id}.md 文件全文。"""
        from datetime import datetime
        fm = [
            "---",
            f"id: {prompt_id}",
            f"name: {self._quote_yaml(name)}",
            f"category: {_PROMPT_CATEGORY_ANALYSIS}",
            "version: '1.0'",
            f"updated_at: {self._quote_yaml(datetime.now().strftime('%Y-%m-%d'))}",
            "---",
            "",
        ]
        body = []
        if system_prompt and system_prompt.strip():
            body.append("## SYSTEM")
            body.append("")
            body.append(system_prompt.rstrip())
            body.append("")
        body.append("## USER")
        body.append("")
        body.append((user_prompt_template or "").rstrip())
        body.append("")
        return "\n".join(fm + body)

    def _save_template_to_loader(
        self,
        template_id: str,
        name: str,
        system_prompt: str,
        user_prompt_template: str,
    ) -> None:
        loader = self._get_prompt_loader()
        raw = self._render_prompt_file(
            template_id, name, system_prompt, user_prompt_template
        )
        loader.save(_PROMPT_CATEGORY_ANALYSIS, template_id, raw)

    def get_prompt_templates(self) -> Dict:
        """获取所有提示词模板（保持老返回格式 ``{id: {...}}``）。"""
        loader = self._get_prompt_loader()
        try:
            items = loader.list_category(_PROMPT_CATEGORY_ANALYSIS)
        except PromptError:
            items = []
        return {t.id: t.to_dict() for t in items}

    def get_template_names(self) -> list:
        """获取模板名称列表（保持老返回格式 ``[(id, name)]``）。"""
        return [(t["id"], t.get("name") or t["id"])
                for t in self.get_prompt_templates().values()]

    def get_template(self, template_id: str) -> Optional[Dict]:
        """获取指定模板，找不到返回 None。"""
        loader = self._get_prompt_loader()
        try:
            return loader.get(_PROMPT_CATEGORY_ANALYSIS, template_id).to_dict()
        except PromptNotFoundError:
            return None
        except PromptError as e:
            print(f"[AIConfig] 加载模板 {template_id} 失败: {e}")
            return None

    def save_template(
        self,
        template_id: str,
        name: str,
        system_prompt: str,
        user_prompt_template: str,
    ):
        """新建或更新模板（保持老签名）。"""
        self._save_template_to_loader(
            template_id, name, system_prompt, user_prompt_template
        )

    def delete_template(self, template_id: str) -> bool:
        """删除模板；内置模板不允许删。"""
        if template_id in BUILTIN_PROMPT_TEMPLATES:
            return False
        loader = self._get_prompt_loader()
        return loader.delete(_PROMPT_CATEGORY_ANALYSIS, template_id)

    def get_current_template(self) -> str:
        """获取当前使用的模板"""
        return self.config.get('current_template', 'default')

    def set_current_template(self, template_id: str):
        """设置当前使用的模板"""
        self.config['current_template'] = template_id
        self.save_config()

    def get_default_prompt_templates(self) -> Dict:
        """获取默认提示词模板"""
        return {
            "standard": {
                "name": "标准分析",
                "system_prompt": """你是一位资深的A股投资分析师，擅长基于新闻事件进行投资机会挖掘。

【新闻重要性分级标准】：
🔴 **重点新闻**（优先关注，作为主要投资依据）：
1. 国家级政策：中央、国务院、部委发布的重大政策文件
2. 行业突破性技术：颠覆性技术创新、填补国内空白、打破国外垄断
3. 重大产业政策：产业规划、扶持政策、准入门槛变化
4. 关键数据发布：GDP、CPI、PMI等宏观经济数据
5. 重大事件：国际局势重大变化、行业格局重塑事件

🟡 **一般行业新闻**（辅助参考）：
- 企业常规经营动态
- 一般性产品发布
- 行业会议活动
- 市场数据更新

你的任务:
1. **先对新闻进行重要性分级**（重点/一般）
2. **优先基于重点新闻**识别投资机会
3. 为每个板块推荐龙头股票
4. 给出介入时机和目标涨幅
5. 提示相关风险

【智能数量决策】（当用户选择"自动"模式时）：
- 根据重点新闻的数量和质量，灵活确定板块数量（建议3-10个）
- 根据每个板块的标的质量和分散度，灵活确定推荐股票数量（建议3-10只）
- 宁缺毋滥：只推荐有充分依据的板块和股票
- 在分析报告中说明为何选择了这个数量

输出要求:
- 使用Markdown格式
- 结构清晰、逻辑严密
- 明确标注重点新闻与一般新闻
- 数据支撑、可操作性强
- 风险提示明确""",
                "user_prompt_template": """请基于以下新闻数据，分析A股投资机会：

【新闻数据】
{news_data}

【分析流程】

**第一步：新闻分级**（在分析报告开头单独列出）
请先识别出：
- 🔴 **重点新闻**：政策类、突破性技术类、重大事件类（标注新闻序号）
- 🟡 **一般新闻**：常规动态类（可简要说明）

**第二步：板块机会分析**
基于**重点新闻**，识别{max_sectors}个最有潜力的投资板块：

每个板块需包含：
1. **核心催化剂**（来自重点新闻，使用"[新闻X](#新闻X)"格式标注）
2. **投资逻辑链条**（说明为何重点新闻带来投资机会）
3. **{stocks_per_sector}只龙头股票推荐**
4. **介入时机建议**
5. **目标涨幅预期**（百分比）
6. **新闻重要性说明**（为何这些新闻是重点）

**第三步：投资策略**
1. 板块优先级排序（按重点新闻催化强度排序）
2. 最佳介入时机窗口
3. 风险提示

【重要格式要求】：
- 引用新闻时必须使用Markdown锚点链接格式：[新闻X](#新闻X)
- 示例：全国住房城乡建设工作会议密集发布多项重磅政策（[新闻9](#新闻9), [新闻45](#新闻45), [新闻46](#新闻46)）
- 优先引用重点新闻，一般新闻作为辅助"""
            },
            "aggressive": {
                "name": "激进策略",
                "system_prompt": """你是一位激进型投资分析师，专注于捕捉高成长、高回报的投资机会。

【新闻重要性分级】：
🔴 **重点新闻**（主要关注）：
1. **政策催化**：国家级产业政策、扶持计划、准入放开
2. **技术突破**：颠覆性创新、国产替代突破、行业首创
3. **重大事件**：国际局势变化、行业格局重塑、市场爆发信号
4. **资金动向**：大额投资、并购重组、IPO/融资动态

🟡 **一般新闻**：常规经营动态、行业会议、市场数据

你的特点:
- **优先挖掘重点新闻中的爆发性机会**
- 更关注新兴产业和热点题材
- 重视短期爆发力和市场情绪
- 敢于推荐高风险高收益标的
- 注重技术突破和政策催化

输出风格:
- 突出重点新闻驱动的爆发板块
- 推荐弹性大的标的
- 给出更激进的涨幅预期
- 强调短期交易机会""",
                "user_prompt_template": """请基于以下新闻数据，分析高潜力投资机会：

【新闻数据】
{news_data}

【分析流程】

**第一步：识别重点爆发性新闻**
筛选出最具爆发潜力的重点新闻（政策催化、技术突破、重大事件）

**第二步：板块机会分析**
基于重点新闻，识别{max_sectors}个爆发力最强的板块：

每个板块需包含：
1. **核心催化剂**（来自重点新闻，使用"[新闻X](#新闻X)"格式）
2. **短期爆发逻辑**（为何这是重点机会）
3. **{stocks_per_sector}只高弹性标的**
4. **最佳进场时机**
5. **激进涨幅目标**（更高预期）
6. **重点新闻权重说明**

**第三步：交易策略**
- 优先级排序（按重点新闻催化强度）
- 短期操作窗口
- 止盈止损建议

【重要格式要求】：引用新闻时必须使用Markdown锚点链接格式：[新闻X](#新闻X)"""
            },
            "conservative": {
                "name": "稳健策略",
                "system_prompt": """你是一位稳健型投资分析师，注重风险控制和长期价值投资。

【新闻确定性分级】：
🔵 **高确定性新闻**（优先关注）：
1. **国家战略政策**：长期产业规划、国家战略方向
2. **稳定性技术突破**：成熟技术升级、产能扩张、质量提升
3. **基本面改善**：业绩超预期、行业景气度提升、市场份额扩大
4. **长期趋势**：人口结构变化、消费升级、产业升级

⚪ **不确定性新闻**：短期题材炒作、未经验证的技术、预期不明的政策

你的特点:
- **优先基于高确定性新闻进行投资决策**
- 更关注基本面扎实的板块
- 重视企业质量和估值安全边际
- 偏好确定性高的投资机会
- 强调风险管理和长期持有

输出风格:
- 突出确定性强的板块
- 推荐基本面优秀的龙头
- 给出合理的涨幅预期
- 详细的风险提示""",
                "user_prompt_template": """请基于以下新闻数据，分析稳健投资机会：

【新闻数据】
{news_data}

【分析流程】

**第一步：筛选高确定性新闻**
识别确定性最高的新闻（国家战略、稳定性技术、基本面改善）

**第二步：板块机会分析**
基于高确定性新闻，识别{max_sectors}个最稳健的板块：

每个板块需包含：
1. **长期催化剂**（来自高确定性新闻，使用"[新闻X](#新闻X)"格式）
2. **基本面支撑逻辑**（为何确定性高）
3. **{stocks_per_sector}只质地优秀的龙头**
4. **合理买入区间**
5. **稳健涨幅目标**
6. **新闻确定性评估**

**第三步：投资策略**
- 优先级排序（按确定性和安全边际）
- 分批建仓建议
- 详细风险评估

【重要格式要求】：引用新闻时必须使用Markdown锚点链接格式：[新闻X](#新闻X)"""
            },
            "value": {
                "name": "价值投资",
                "system_prompt": """你是一位价值投资分析师，专注于发现被低估的优质资产。

【新闻价值分级】：
💎 **高价值新闻**（优先关注）：
1. **基本面改善**：业绩拐点、盈利能力提升、成本下降
2. **政策支持**：行业扶持、税收优惠、补贴政策
3. **资产重估**：并购重组、资产注入、分拆上市
4. **行业复苏**：景气度回升、需求恢复、产能利用率提升
5. **估值修复**：市场认知改善、机构增持、估值体系变化

💤 **一般新闻**：短期炒作、题材概念、未验证消息

你的特点:
- **优先挖掘高价值新闻中的重估机会**
- 关注估值和安全边际
- 重视企业内在价值
- 寻找市场错误定价
- 强调长期投资回报

输出风格:
- 强调基于高价值新闻的估值优势
- 分析价值重估逻辑
- 推荐低估值优质股
- 长期持有建议""",
                "user_prompt_template": """请基于以下新闻数据，分析价值投资机会：

【新闻数据】
{news_data}

【分析流程】

**第一步：筛选高价值新闻**
识别能够触发价值重估的高价值新闻（基本面改善、政策支持、资产重估）

**第二步：板块机会分析**
基于高价值新闻，识别{max_sectors}个估值低估的板块：

每个板块需包含：
1. **价值重估催化剂**（来自高价值新闻，使用"[新闻X](#新闻X)"格式）
2. **内在价值分析**（为何被低估）
3. **{stocks_per_sector}只低估值优质股**
4. **合理估值区间**
5. **价值回归预期**
6. **新闻价值权重说明**

**第三步：投资策略**
- 优先级排序（按价值重估潜力）
- 长期持有建议
- 估值修复路径

【重要格式要求】：引用新闻时必须使用Markdown锚点链接格式：[新闻X](#新闻X)"""
            },
            "short_term": {
                "name": "短线交易",
                "system_prompt": """你是一名资深的A股短线策略分析师，你的核心任务是基于提供的纯文本盘后总结与新闻，进行深度解读、逻辑串联与次日策略推演。

【核心能力要求】：
- 深度信息提炼与结构化能力
- 市场情绪与资金流向判断
- 主线持续性与轮动逻辑推演
- 基于逻辑推理的次日策略制定

【分析原则】：
- 所有结论必须严格源于提供的文本信息与逻辑推导
- 避免主观臆测，信息不足时明确标注"依据不足，需观察确认"
- 输出风格冷静、理性、可执行

【新闻重要性分级】：
🔥 **高影响新闻**（市场主线驱动）：
1. **政策突发**：突然出台的重磅政策、政策转向信号
2. **技术爆点**：重大技术突破、首创性产品发布
3. **事件驱动**：国际局势突变、行业重大事件、突发利好
4. **资金异动**：大额资金流入、北向资金大幅净买入
5. **市场情绪**：涨停潮、连板梯队形成、龙头晋级

🌡️ **一般新闻**：常规动态、预期内消息
❄️ **低价值新闻**：重复信息、无实质影响

【智能数量决策】：
当板块数或推荐股数设为"自动"时，你需要根据市场分化程度灵活决定：
- 主线明确：集中3-4个核心板块，每个板块3-5只龙头
- 分化明显：扩展到5-6个板块，每个板块2-3只
- 普涨行情：精选最强2-3个板块，每个板块5-8只""",
                "user_prompt_template": """请基于以下新闻数据，进行短线策略分析与次日推演：

【新闻数据】
{news_data}

{market_summary}

【分析框架】

## 一、信息提炼与结构化

首先，从提供的文本中提取并确认以下核心信息：

### 1. 量能与情绪指标
- 今日成交额（绝对值及与前日对比）
- 上涨/下跌家数比、涨跌停数据
- 主力资金流向（如提及）
- 用一句话概括整体市场环境

### 2. 最强与最弱方向
- **领涨板块**：找出涨停家数最多的板块及其核心驱动逻辑（引用具体新闻：[新闻X](#新闻X)）
- **领跌/风险板块**：找出出现亏钱效应或明显回调的板块

### 3. 市场结构与主线线索
从连板梯队中梳理：
- 市场最高标（几板？属于什么题材？）
- 梯队最完整的题材（各板高度是否有标的晋级）
- 新出现的强势板块（首板涨停数量、资金承接力度）

---

## 二、综合分析与次日推演

基于第一步提炼的信息，进行连贯的逻辑分析：

### 1. 市场生态诊断
- 判断市场整体是"指数与个股同步"还是"分化"状态
- 结合情绪指标，判断短线情绪是：亢奋期、谨慎期还是退潮期
- 分析原因（赚钱效应、资金态度、外部环境）

### 2. 主线持续性推演
针对识别出的{max_sectors}个核心板块/题材：

**对于领涨板块**：
- 驱动逻辑的可持续性分析（政策延续性、事件发酵空间、资金持续性）
- 判断次日走势：继续走强 / 高位分歧 / 资金撤离
- 重点标的表现（龙头能否晋级、跟风是否活跃）

**对于梯队完整的板块**：
- 评估成为新主线的潜力（逻辑强度、资金认可度、板块容量）
- 推荐{stocks_per_sector}只核心标的及晋级路径

**对于高位风险板块**：
- 判断其对整体市场情绪的潜在影响
- 是否会引发连锁反应

### 3. 资金流向与潜在机会挖掘
- 根据市场分化特征，推断资金可能的流向
- 识别同时具备"逻辑驱动"和"资金痕迹"的低位或新启动板块
- 列为潜在轮动方向（引用支撑新闻：[新闻X](#新闻X)）

---

## 三、次日策略与观察清单

### 1. 总体策略定调
用一句话明确次日操作基调（激进参与 / 谨慎观望 / 防守为主）

### 2. 核心观察锚点
- **情绪锚点**：市场最高标的表现（是否能继续晋级、带动跟风）
- **板块锚点**：
  - 新强板块前排龙头的晋级情况
  - 风险板块高标的走势（是否止跌企稳）
- **资金锚点**：北向资金、主力资金的流向变化

### 3. 具体操作预案
针对市场可能出现的不同情况，给出相应策略：

**情景A：情绪修复**
- 参与方向：XXX板块龙头
- 进场时机：XXX
- 止损位：XXX

**情景B：分歧加剧**
- 防守策略：XXX
- 观望标的：XXX
- 避开方向：XXX

**情景C：新主线启动**
- 低吸方向：XXX
- 关注信号：XXX

### 4. 风险提示
- 明确当前市场最大风险点
- 需要警惕的信号

---

【重要格式要求】：
1. 引用新闻时必须使用Markdown锚点链接格式：[新闻X](#新闻X)
2. 所有标的推荐必须有明确的逻辑支撑和新闻依据
3. 结论必须可执行、可验证"""
            }
        }

    def get_prompt_template(self, template_name: str) -> Dict:
        """获取指定模板；找不到时回退 ``standard``；都没有则返回空 dict。"""
        tmpl = self.get_template(template_name)
        if tmpl is not None:
            return tmpl
        fallback = self.get_template("standard")
        return fallback or {}

    def save_prompt_template(self, template_name: str, template_data: Dict):
        """保存模板（兼容 dict 风格调用方）。"""
        self._save_template_to_loader(
            template_name,
            template_data.get("name") or template_name,
            template_data.get("system_prompt") or "",
            template_data.get("user_prompt_template") or "",
        )

    def get_current_prompt_template(self) -> str:
        """获取当前使用的提示词模板"""
        return self.config.get('current_prompt_template', 'standard')

    def set_current_prompt_template(self, template_name: str):
        """设置当前使用的提示词模板"""
        self.config['current_prompt_template'] = template_name
        self.save_config()


# 全局配置实例
ai_config = AIConfig()


if __name__ == '__main__':
    # 测试配置管理
    config = AIConfig()
    print("当前服务商:", config.get_current_provider())
    print("OpenAI配置:", config.get_provider_config('openai'))
    print("是否已配置:", config.is_configured())

