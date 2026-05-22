"""
AI新闻分析器核心
支持OpenAI API和国产大模型API
"""

import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Callable
from core.ai_config import AIConfig
from core.data_loader import DataLoader


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
        max_sectors = 6,  # 可以是int或'auto'
        stocks_per_sector = 5,  # 可以是int或'auto'
        template_name: Optional[str] = None,
        market_summary: Optional[str] = None
    ) -> str:
        """构建用户提示词（支持自动模式）"""
        if template_name:
            template = self.config.get_prompt_template(template_name)
        else:
            current_template = self.config.get_current_prompt_template()
            template = self.config.get_prompt_template(current_template)
        
        user_prompt_template = template.get('user_prompt_template', '')
        
        # 处理自动模式：将'auto'替换为自然语言描述
        if max_sectors == 'auto':
            max_sectors_text = "根据新闻数据的实际情况自动确定（建议3-10个）"
        else:
            max_sectors_text = str(max_sectors)
        
        if stocks_per_sector == 'auto':
            stocks_per_sector_text = "根据每个板块的实际情况自动确定（建议3-10只）"
        else:
            stocks_per_sector_text = str(stocks_per_sector)
        
        # 处理盘后总结
        market_summary_text = ""
        if market_summary:
            market_summary_text = f"【盘后总结】\n{market_summary}\n"
        
        # 检查模板是否包含 {market_summary} 占位符
        has_placeholder = '{market_summary}' in user_prompt_template
        
        try:
            if has_placeholder:
                # 模板中有占位符，正常替换
                return user_prompt_template.format(
                    news_data=news_data,
                    max_sectors=max_sectors_text,
                    stocks_per_sector=stocks_per_sector_text,
                    market_summary=market_summary_text
                )
            else:
                # 模板中没有占位符，先替换其他变量，再将盘后总结追加到开头
                base_prompt = user_prompt_template.format(
                    news_data=news_data,
                    max_sectors=max_sectors_text,
                    stocks_per_sector=stocks_per_sector_text
                )
                
                # 如果有盘后总结，追加到提示词开头（在所有内容之前）
                if market_summary:
                    base_prompt = market_summary_text + "\n\n" + base_prompt
                
                return base_prompt
        except KeyError as e:
            # 如果模板缺少必要的占位符，返回错误提示
            raise ValueError(f"提示词模板缺少占位符: {e}")

    def _stream_chat(
        self,
        provider: str,
        news_data: str,
        max_sectors=6,
        stocks_per_sector=5,
        template_name: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        market_summary: Optional[str] = None,
    ) -> str:
        """统一流式调用（OpenAI 兼容接口或智谱）"""
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
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=provider_config.get("max_tokens", 4000),
            temperature=provider_config.get("temperature", 0.7),
            stream=True,
        )

        result = []
        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                result.append(content)
                if progress_callback:
                    progress_callback(content, is_streaming=True)
        return "".join(result)

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
        progress_callback: Optional[Callable] = None
    ) -> Dict:
        """
        分析新闻文件
        返回: {
            'success': True/False,
            'result': '分析结果文本',
            'report_file': '报告文件路径',
            'error': '错误信息'
        }
        """
        try:
            # 加载数据
            if progress_callback:
                progress_callback("正在加载数据...")

            data = self.data_loader.load(file_path)
            news_list = data['news_list']
            format_type = data['format']

            if progress_callback:
                progress_callback(
                    f"已加载 {data['count']} 条新闻 ({format_type}格式)")

            # 限制新闻数量
            if max_news and len(news_list) > max_news:
                news_list = news_list[:max_news]
                if progress_callback:
                    progress_callback(f"已限制为前 {max_news} 条新闻")

            # 格式化数据
            if progress_callback:
                progress_callback("正在格式化数据...")

            news_data = self.data_loader.format_for_ai(
                news_list, format_type)

            # 估算tokens
            estimated_tokens = self.data_loader.estimate_tokens(news_data)
            if progress_callback:
                progress_callback(f"预估输入tokens: {estimated_tokens}")

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
            )

            # 保存报告
            if progress_callback:
                progress_callback("正在保存报告...")

            report_file = self.save_report(
                result_text,
                file_path,
                data['time_range'],
                news_list
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
        news_list: Optional[list] = None
    ) -> str:
        """保存分析报告"""
        # 获取源文件名（用于报告头部）
        basename = os.path.basename(source_file)

        # 生成报告文件名 - 格式: 月日_时_盘后总结分析报告.md
        now = datetime.now()

        # 获取月日（去掉前导零）
        month_day = f"{now.month}月{now.day}日"

        # 获取时分（时不带前导零，分钟带前导零）
        hour = now.hour
        minute = now.strftime('%M')
        time_str = f"{hour}时{minute}分"

        # 生成文件名
        report_filename = f"{month_day}_{time_str}_盘后总结分析报告.md"

        # 生成保存路径: data/AI_analysis/月日/
        report_dir = os.path.join('data', 'AI_analysis', month_day)
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

