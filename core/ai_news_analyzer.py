"""
AI新闻分析器核心
支持OpenAI API和国产大模型API
"""

import os
import re
import json
from datetime import datetime
from typing import Dict, List, Optional, Callable
from core.ai_config import AIConfig
from core.data_loader import DataLoader


class AnalysisCancelledError(Exception):
    """用户主动终止分析。"""


def _raise_if_cancelled(cancel_check: Optional[Callable[[], bool]]) -> None:
    if cancel_check and cancel_check():
        raise AnalysisCancelledError("用户已终止分析")


class AINewsAnalyzer:
    """AI新闻分析器"""

    def __init__(self, config: Optional[AIConfig] = None):
        self.config = config or AIConfig()
        self.data_loader = DataLoader()

    def get_system_prompt(self, template_name: Optional[str] = None) -> str:
        """获取系统提示词"""
        if template_name:
            template = self.config.get_prompt_template(template_name)
            return template.get('system_prompt', '')
        
        # 使用当前选中的模板
        current_template = self.config.get_current_prompt_template()
        template = self.config.get_prompt_template(current_template)
        return template.get('system_prompt', '')

    def build_user_prompt(
        self,
        news_data: str,
        max_sectors=6,  # int 或 'auto'
        stocks_per_sector=5,  # int 或 'auto'
        template_name: Optional[str] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        """
        构建 user prompt。

        Prompt cache 友好策略（关键）:
            DeepSeek / Qwen / OpenAI 等 LLM 服务商按 prompt **前缀** 自动命中缓存，
            命中部分计费降至 1/10。本方法将 prompt 拆为两段：

                [前段 - 静态]   模板正文 + max_sectors/stocks_per_sector 渲染结果
                [后段 - 动态]   market_summary（可选）+ news_data

            模板里若内嵌 ``{news_data}`` / ``{market_summary}`` 占位符，
            一律置空丢弃，统一追加到末尾。这样无需用户改模板即可获得 cache 收益。

        占位符渲染规则:
            ``{max_sectors}`` / ``{stocks_per_sector}`` -> 按值替换（数值或 'auto' 描述）
            ``{news_data}``                            -> 置空，数据追加到末尾
            ``{market_summary}``                       -> 置空，数据追加到末尾

        Args:
            news_data: 已格式化的新闻文本（带 1-based 编号）
            max_sectors: 板块数，int 或 'auto'
            stocks_per_sector: 每板块股票数，int 或 'auto'
            template_name: 模板 ID，默认取 AIConfig.current_prompt_template
            market_summary: 盘后总结文本（可选，会拼接到末尾）

        Returns:
            完整 user prompt 字符串，前段静态可缓存、后段为本次变化数据
        """
        if template_name:
            template = self.config.get_prompt_template(template_name)
        else:
            template = self.config.get_prompt_template(
                self.config.get_current_prompt_template()
            )

        user_prompt_template = template.get('user_prompt_template', '')

        max_sectors_text = (
            "根据新闻数据的实际情况自动确定（建议3-10个）"
            if max_sectors == 'auto' else str(max_sectors)
        )
        stocks_per_sector_text = (
            "根据每个板块的实际情况自动确定（建议3-10只）"
            if stocks_per_sector == 'auto' else str(stocks_per_sector)
        )

        try:
            body = user_prompt_template.format(
                news_data="",
                market_summary="",
                max_sectors=max_sectors_text,
                stocks_per_sector=stocks_per_sector_text,
            )
        except KeyError as e:
            raise ValueError(f"提示词模板缺少占位符: {e}")

        body = re.sub(r'\n{3,}', '\n\n', body).strip()

        sections = [body]
        if market_summary and market_summary.strip():
            sections.append("---\n\n【盘后总结】\n" + market_summary.strip())
        sections.append("---\n\n【新闻数据】\n" + news_data)

        return "\n\n".join(sections)

    def _stream_chat(
        self,
        provider: str,
        news_data: str,
        max_sectors=6,
        stocks_per_sector=5,
        template_name: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
        enable_deep_thinking: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        """统一流式调用（OpenAI 兼容接口或智谱）"""
        _raise_if_cancelled(cancel_check)
        provider_config = self.config.get_provider_config(provider)
        api_key = provider_config.get("api_key")
        if not api_key:
            raise ValueError(f"未配置 {provider} API Key")

        system_prompt = self.get_system_prompt(template_name)
        user_prompt = self.build_user_prompt(
            news_data,
            max_sectors,
            stocks_per_sector,
            template_name=template_name,
            market_summary=market_summary,
        )

        if progress_callback:
            progress_callback(f"正在调用 {provider} API...")

        if provider == "zhipu":
            try:
                from zhipuai import ZhipuAI
            except ImportError:
                raise ImportError("请安装zhipuai库: pip install zhipuai")
            client = ZhipuAI(api_key=api_key)
        else:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("请安装openai库: pip install openai")
            base_url = provider_config.get("base_url")
            if provider == "qwen" and not base_url:
                base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            client = OpenAI(api_key=api_key, base_url=base_url)

        model = provider_config.get("model", "gpt-4")

        # 场景分流（参见 doc/design/05-26-2126-NewsTrading架构方案.md §13）:
        # DeepSeek V4 默认 thinking=enabled，须显式 disabled 才能关闭。
        create_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.config.get_max_output_tokens(provider),
            "temperature": provider_config.get("temperature", 0.7),
            "stream": True,
        }
        # DeepSeek V4 默认开启 thinking；必须显式 enabled/disabled，
        # 否则仅不传 extra_body 时模型仍会返回 reasoning_content。
        if provider == "deepseek" and model.startswith("deepseek-v4"):
            if enable_deep_thinking:
                create_kwargs["extra_body"] = {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "max",
                }
            else:
                create_kwargs["extra_body"] = {
                    "thinking": {"type": "disabled"},
                }

        response = client.chat.completions.create(**create_kwargs)

        # 思考模式下，DeepSeek V4 通过 chunk.delta.reasoning_content 推送
        # 思考链；普通模式只会有 chunk.delta.content。两者先后到达，
        # 直接拼接 -> 「思考过程」在前、「正式分析」在后，落到 md 报告。
        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        sent_reasoning_header = False
        sent_content_header = False
        collect_reasoning = enable_deep_thinking

        try:
            for chunk in response:
                _raise_if_cancelled(cancel_check)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) if collect_reasoning else None
                content = delta.content

                if reasoning:
                    if not sent_reasoning_header:
                        header = "## 🧠 模型思考过程\n\n"
                        if progress_callback:
                            progress_callback(header, is_streaming=True)
                        sent_reasoning_header = True
                    reasoning_parts.append(reasoning)
                    if progress_callback:
                        progress_callback(reasoning, is_streaming=True)

                if content:
                    if not sent_content_header:
                        if reasoning_parts:
                            sep = "\n\n---\n\n## 📋 主要分析\n\n"
                            if progress_callback:
                                progress_callback(sep, is_streaming=True)
                        sent_content_header = True
                    content_parts.append(content)
                    if progress_callback:
                        progress_callback(content, is_streaming=True)
        except AnalysisCancelledError:
            if hasattr(response, "close"):
                try:
                    response.close()
                except Exception:
                    pass
            raise

        reasoning_text = "".join(reasoning_parts).strip()
        content_text = "".join(content_parts)

        if reasoning_text and collect_reasoning:
            return (
                "## 🧠 模型思考过程\n\n"
                f"{reasoning_text}\n\n"
                "---\n\n"
                "## 📋 主要分析\n\n"
                f"{content_text}"
            )
        return content_text

    def analyze_with_openai(
        self,
        news_data: str,
        max_sectors = 6,  # int或'auto'
        stocks_per_sector = 5,  # int或'auto'
        progress_callback: Optional[Callable] = None,
        template_name: Optional[str] = None,
        market_summary: Optional[str] = None
    ) -> str:
        return self._stream_chat(
            "openai", news_data, max_sectors, stocks_per_sector,
            template_name, progress_callback, market_summary,
        )

    def analyze_with_deepseek(
        self,
        news_data: str,
        max_sectors = 6,
        stocks_per_sector = 5,
        template_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        return self._stream_chat(
            "deepseek", news_data, max_sectors, stocks_per_sector,
            template_id, progress_callback, market_summary,
        )

    def analyze_with_qwen(
        self,
        news_data: str,
        max_sectors = 6,
        stocks_per_sector = 5,
        template_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        return self._stream_chat(
            "qwen", news_data, max_sectors, stocks_per_sector,
            template_id, progress_callback, market_summary,
        )

    def analyze_with_zhipu(
        self,
        news_data: str,
        max_sectors = 6,
        stocks_per_sector = 5,
        template_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        return self._stream_chat(
            "zhipu", news_data, max_sectors, stocks_per_sector,
            template_id, progress_callback, market_summary,
        )

    def analyze_with_volcengine(
        self,
        news_data: str,
        max_sectors = 6,
        stocks_per_sector = 5,
        template_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        return self._stream_chat(
            "volcengine", news_data, max_sectors, stocks_per_sector,
            template_id, progress_callback, market_summary,
        )

    def analyze(
        self,
        file_path: str,
        provider: Optional[str] = None,
        max_sectors = 6,  # int或'auto'
        stocks_per_sector = 5,  # int或'auto'
        max_news: Optional[int] = None,
        template_id: Optional[str] = None,
        market_summary: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        enable_deep_thinking: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
        naming_dt: Optional[datetime] = None,
        backtest_suffix: bool = False,
    ) -> Dict:
        """
        分析新闻文件
        返回: {
            'success': True/False,
            'result': '分析结果文本',
            'report_file': '报告文件路径',
            'error': '错误信息'
        }

        Args:
            naming_dt: 报告文件命名用的时间戳。
                - None（默认）：真实生成，用 ``datetime.now()`` 命名
                - 给定 datetime：用该值命名（典型场景：回测产物用模拟交易日 00:00）
            backtest_suffix: True 时文件名追加 ``_backtest`` 后缀，标识虚拟回测产物。
        """
        try:
            _raise_if_cancelled(cancel_check)
            # 加载数据
            if progress_callback:
                progress_callback("正在加载数据...")

            data = self.data_loader.load(file_path)
            news_list = data['news_list']
            format_type = data['format']

            _raise_if_cancelled(cancel_check)
            if progress_callback:
                progress_callback(
                    f"已加载 {data['count']} 条新闻 ({format_type}格式)")

            # 限制新闻数量
            if max_news and len(news_list) > max_news:
                news_list = news_list[:max_news]
                if progress_callback:
                    progress_callback(f"已限制为前 {max_news} 条新闻")

            _raise_if_cancelled(cancel_check)
            # 格式化数据
            if progress_callback:
                progress_callback("正在格式化数据...")

            news_data = self.data_loader.format_for_ai(
                news_list, format_type)

            # 估算tokens
            estimated_tokens = self.data_loader.estimate_tokens(news_data)
            if progress_callback:
                progress_callback(f"预估输入tokens: {estimated_tokens}")

            _raise_if_cancelled(cancel_check)
            # 选择服务商
            if provider is None:
                provider = self.config.get_current_provider()

            supported = ("openai", "deepseek", "zhipu", "volcengine", "qwen")
            if provider not in supported:
                raise ValueError(f"不支持的AI服务商: {provider}")
            result_text = self._stream_chat(
                provider,
                news_data,
                max_sectors,
                stocks_per_sector,
                template_id,
                progress_callback,
                market_summary,
                enable_deep_thinking=enable_deep_thinking,
                cancel_check=cancel_check,
            )

            _raise_if_cancelled(cancel_check)
            # 保存报告
            if progress_callback:
                progress_callback("正在保存报告...")

            report_file = self.save_report(
                result_text,
                file_path,
                data['time_range'],
                news_list,
                naming_dt=naming_dt,
                backtest_suffix=backtest_suffix,
            )

            if progress_callback:
                progress_callback(f"报告已保存: {report_file}")

            return {
                'success': True,
                'result': result_text,
                'report_file': report_file,
                'news_count': len(news_list),
                'time_range': data['time_range']
            }

        except AnalysisCancelledError as e:
            error_msg = str(e)
            if progress_callback:
                progress_callback(error_msg)
            return {
                'success': False,
                'cancelled': True,
                'error': error_msg,
            }
        except Exception as e:
            error_msg = f"分析失败: {str(e)}"
            if progress_callback:
                progress_callback(error_msg)

            return {
                'success': False,
                'error': error_msg
            }

    def save_report(
        self,
        content: str,
        source_file: str,
        time_range: Dict,
        news_list: Optional[list] = None,
        naming_dt: Optional[datetime] = None,
        backtest_suffix: bool = False,
    ) -> str:
        """保存分析报告。

        Args:
            naming_dt: 文件命名用的时间戳；None=用 ``datetime.now()``（真实生成），
                给定值=用该时间命名（典型：回测产物用模拟交易日 00:00）
            backtest_suffix: True 时文件名追加 ``_backtest`` 后缀
        """
        basename = os.path.basename(source_file)

        # 命名时间：真实生成 = now；回测 = 模拟交易日 00:00
        naming = naming_dt or datetime.now()

        month_day = f"{naming.month}月{naming.day}日"
        time_str = f"{naming.hour}时{naming.strftime('%M')}分"

        # 文件名（回测追加 _backtest 后缀，便于人/程序一眼识别）
        suffix = "_backtest" if backtest_suffix else ""
        report_filename = (
            f"{month_day}_{time_str}_盘后总结分析报告{suffix}.md"
        )

        # 生成保存路径: data/AI_analysis/月日/
        from services.storage.database import get_project_root
        project_root = get_project_root()
        report_dir = os.path.join(project_root, 'data', 'AI_analysis', month_day)
        report_path = os.path.join(report_dir, report_filename)

        # 确保目录存在
        os.makedirs(report_dir, exist_ok=True)

        # 添加报告头部
        header = f"""# 📊 A股投资机会分析报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**数据来源**: {basename}
**时间范围**: {time_range.get('start', '未知')} 至 {time_range.get('end', '未知')}

---

"""

        # 提取引用的新闻序号
        referenced_news = self._extract_referenced_news(content, news_list)

        # 添加引用新闻详情
        if referenced_news:
            footer = self._generate_news_references(referenced_news)
            content = content + "\n\n" + footer

        # 保存报告
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(header + content)

        return report_path

    def _extract_referenced_news(
        self, 
        content: str, 
        news_list: Optional[list]
    ) -> Dict:
        """提取报告中引用的新闻序号"""
        if not news_list:
            return {}

        import re
        # 匹配格式：新闻1, 新闻2, 新闻34等
        pattern = r'新闻(\d+)'
        matches = re.findall(pattern, content)

        referenced = {}
        for num_str in set(matches):
            num = int(num_str)
            if 0 < num <= len(news_list):
                # 新闻序号从1开始，列表索引从0开始
                referenced[num] = news_list[num - 1]

        return referenced

    def _generate_news_references(self, referenced_news: Dict) -> str:
        """生成引用新闻详情部分"""
        lines = []
        lines.append("---")
        lines.append("")
        lines.append("## 📰 引用新闻详情")
        lines.append("")
        lines.append("以下是报告中引用的新闻原文（点击标题可跳转到原文）：")
        lines.append("")

        # 按序号排序
        for num in sorted(referenced_news.keys()):
            news = referenced_news[num]
            # 添加锚点ID，用于页内跳转
            lines.append(f'<a id="新闻{num}"></a>')
            lines.append("")
            lines.append(f"### 新闻{num}")
            lines.append("")

            # 数据库 ID（题材抽取脚本反查 raw_news 用，永远放标题前）
            news_db_id = news.get("id") or ""
            if news_db_id:
                lines.append(f"**数据库ID**: `{news_db_id}`")

            # 时间
            time_str = news.get('datetime') or news.get('time', '未知')
            lines.append(f"**时间**: {time_str}")

            # 标题 - 添加链接支持
            title = news.get('title', '无标题')
            url = news.get('url') or news.get('link', '')
            if url:
                # 如果有URL，将标题设置为可点击的链接
                lines.append(f"**标题**: [{title}]({url})")
            else:
                lines.append(f"**标题**: {title}")

            # 来源
            source = news.get('source', '')
            if source:
                lines.append(f"**来源**: {source}")

            # 原文链接（如果有且未在标题中显示）
            if url:
                lines.append(f"**原文链接**: {url}")

            # 内容
            content = news.get('content', '')
            if content:
                lines.append("")
                lines.append(f"**内容**:")
                lines.append(content)
            else:
                lines.append("")
                lines.append("*（无详细内容）*")

            lines.append("")
            lines.append("---")
            lines.append("")

        return '\n'.join(lines)

    def _format_connection_error(self, provider: str, err: Exception) -> str:
        """将 API 错误转为可操作的提示"""
        msg = str(err)
        env_hint = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "qwen": "QWEN_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "volcengine": "VOLCENGINE_API_KEY",
        }.get(provider, "对应服务商的 *_API_KEY")

        if "401" in msg or "authentication" in msg.lower() or "invalid" in msg.lower():
            return (
                f"认证失败（{provider}）：当前 API Key 无效或已作废。\n"
                f"请到该平台控制台重新创建密钥，并在界面填写或写入 .env 的 {env_hint}。\n"
                f"注意：DeepSeek 与 OpenAI 的 Key 不能混用。\n"
                f"原始信息: {msg}"
            )
        return f"连接失败: {msg}"

    def test_connection(self, provider: Optional[str] = None) -> Dict:
        """测试 API 连接"""
        if provider is None:
            provider = self.config.get_current_provider()

        supported = ("openai", "deepseek", "zhipu", "volcengine", "qwen")
        if provider not in supported:
            return {"success": False, "message": f"不支持的服务商: {provider}"}

        api_key = self.config.get_api_key(provider)
        if not api_key:
            env_name = {
                "deepseek": "DEEPSEEK_API_KEY",
                "openai": "OPENAI_API_KEY",
                "qwen": "QWEN_API_KEY",
                "zhipu": "ZHIPU_API_KEY",
                "volcengine": "VOLCENGINE_API_KEY",
            }.get(provider, "")
            return {
                "success": False,
                "message": f"未配置 {provider} 的 API Key，请在界面填写或设置环境变量 {env_name}",
            }

        try:
            self._stream_chat(
                provider,
                "ping",
                max_sectors=1,
                stocks_per_sector=1,
                template_name="standard",
                progress_callback=None,
                market_summary=None,
                enable_deep_thinking=False,
            )
            return {"success": True, "message": f"{provider} 连接成功"}
        except Exception as e:
            return {"success": False, "message": self._format_connection_error(provider, e)}


if __name__ == '__main__':
    # 测试分析器
    analyzer = AINewsAnalyzer()

    # 测试连接
    result = analyzer.test_connection('openai')
    print(f"连接测试: {result}")

