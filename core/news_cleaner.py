"""
新闻清洗核心逻辑
使用AI智能筛选有价值的新闻
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Callable, Optional
from openai import OpenAI


class NewsCleaner:
    """AI新闻清洗器"""
    
    def __init__(self, criteria: str, ai_provider: str = 'deepseek'):
        """
        初始化清洗器
        
        参数:
            criteria: 清洗标准文本
            ai_provider: AI服务商
        """
        self.criteria = criteria
        self.ai_provider = ai_provider
        
        # 加载AI配置
        from core.ai_config import AIConfig
        self.config = AIConfig()

    def clean_news_list(
        self,
        news_list: List[Dict],
        batch_size: int = 100,
        auto_merge: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> Dict:
        """
        清洗内存中的新闻列表（供 SQLite 同步服务使用）。

        参数:
            news_list: 新闻 dict 列表
            batch_size: 每批处理数量
            auto_merge: 是否语义去重
            progress_callback: 进度回调

        返回:
            {'kept': [...], 'removed': [...], 'metadata': {...}}
        """
        all_news = list(news_list)
        if progress_callback:
            progress_callback(f"已加载 {len(all_news)} 条新闻")

        if auto_merge:
            before_count = len(all_news)
            all_news = self._deduplicate(all_news)
            if progress_callback:
                progress_callback(
                    f"去重完成：{before_count} → {len(all_news)} 条"
                )

        if progress_callback:
            progress_callback("开始AI清洗...")

        kept, removed = self._ai_clean_batches(
            all_news, batch_size, progress_callback
        )
        kept.sort(key=lambda x: x.get("datetime") or x.get("time", ""))
        removed.sort(key=lambda x: x.get("datetime") or x.get("time", ""))

        metadata = self._generate_metadata(
            ["<memory>"], len(all_news), kept, removed
        )
        return {"kept": kept, "removed": removed, "metadata": metadata}

    def _deduplicate(self, news_list: List[Dict]) -> List[Dict]:
        """去重新闻（30 分钟窗口 + 30% 标题/正文双路相似度）"""
        from core.semantic_dedup import semantic_deduplicate
        return semantic_deduplicate(news_list)
    
    def _ai_clean_batches(
        self,
        news_list: List[Dict],
        batch_size: int,
        progress_callback: Optional[Callable]
    ) -> tuple:
        """AI分批清洗"""
        kept = []
        removed = []
        
        total = len(news_list)
        total_batches = (total + batch_size - 1) // batch_size
        
        for batch_idx in range(total_batches):
            start_idx = batch_idx * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch = news_list[start_idx:end_idx]
            
            # 构建prompt
            user_prompt = self._build_batch_prompt(batch)
            
            # 调用AI判断
            try:
                decisions = self._call_ai_judge(user_prompt, len(batch))
                
                # 分类
                kept_count = 0
                removed_count = 0
                for news, decision in zip(batch, decisions):
                    if decision == 'keep':
                        kept.append(news)
                        kept_count += 1
                    else:
                        # 添加去除原因（可选）
                        news_with_reason = news.copy()
                        news_with_reason['removal_reason'] = '不符合保留标准'
                        removed.append(news_with_reason)
                        removed_count += 1
                
                # 进度回调
                if progress_callback:
                    progress_callback(
                        f"批次 {batch_idx + 1}/{total_batches}",
                        end_idx,
                        total,
                        kept_count,
                        removed_count
                    )
                    
            except Exception as e:
                print(f"批次 {batch_idx + 1} 处理失败: {e}")
                # 失败时全部保留
                kept.extend(batch)
                if progress_callback:
                    progress_callback(
                        f"批次 {batch_idx + 1}/{total_batches} 失败，已全部保留"
                    )
        
        return kept, removed
    
    def _build_batch_prompt(self, batch: List[Dict]) -> str:
        """构建批次prompt"""
        prompt = "请判断以下新闻是否应该保留：\n\n"
        
        for idx, news in enumerate(batch, 1):
            prompt += f"{idx}. 【标题】{news.get('title', '无标题')}\n"
            prompt += f"   【时间】{news.get('time', '未知')}\n"
            
            # 添加来源
            if news.get('source'):
                prompt += f"   【来源】{news['source']}\n"
            
            # 添加内容预览（前150字）
            if news.get('content'):
                content_preview = news['content'][:150]
                if len(news['content']) > 150:
                    content_preview += "..."
                prompt += f"   【内容】{content_preview}\n"
            
            prompt += "\n"
        
        prompt += "\n请对每条新闻输出判断（仅输出序号和结果，不要其他内容）：\n"
        prompt += "1. keep\n"
        prompt += "2. remove\n"
        prompt += "3. keep\n"
        prompt += "...\n"
        
        return prompt
    
    def _call_ai_judge(self, user_prompt: str, expected_count: int) -> List[str]:
        """调用AI判断"""
        # 构建system_prompt
        system_prompt = f"""你是A股投资新闻清洗专家，负责筛选有投资价值的新闻。

{self.criteria}

请严格按照上述标准判断，只输出序号和结果（keep或remove），每行一个。"""
        
        provider = self.ai_provider if self.ai_provider in (
            "deepseek", "openai", "zhipu", "qwen", "volcengine"
        ) else "deepseek"
        decisions = self._call_provider(
            provider, system_prompt, user_prompt, expected_count
        )
        return decisions

    def _call_provider(
        self,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        expected_count: int,
    ) -> List[str]:
        """调用指定 AI 服务商进行清洗判断"""
        provider_config = self.config.get_provider_config(provider)
        api_key = provider_config.get("api_key")
        if not api_key:
            raise ValueError(f"未配置 {provider} API Key")

        if provider == "zhipu":
            try:
                from zhipuai import ZhipuAI
            except ImportError:
                raise ImportError("请安装zhipuai库: pip install zhipuai")
            client = ZhipuAI(api_key=api_key)
        else:
            base_url = provider_config.get("base_url")
            if provider == "qwen" and not base_url:
                base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            client = OpenAI(api_key=api_key, base_url=base_url)

        model = provider_config.get("model") or {
            "deepseek": "deepseek-chat",
            "openai": "gpt-4",
            "zhipu": "glm-4",
            "qwen": "qwen-plus",
            "volcengine": "doubao-seed-1-6-251015",
        }.get(provider, "deepseek-chat")

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        result_text = response.choices[0].message.content
        return self._parse_decisions(result_text, expected_count)
    
    def _parse_decisions(self, result_text: str, expected_count: int) -> List[str]:
        """解析AI返回的判断结果"""
        decisions = []
        lines = result_text.strip().split('\n')
        
        for line in lines:
            line = line.strip().lower()
            
            # 匹配 "1. keep" 或 "keep" 或 "1. remove"
            if 'keep' in line:
                decisions.append('keep')
            elif 'remove' in line:
                decisions.append('remove')
        
        # 如果解析的数量不够，剩余的默认保留
        while len(decisions) < expected_count:
            decisions.append('keep')
        
        # 如果解析的太多，截断
        return decisions[:expected_count]
    
    def _generate_metadata(
        self,
        source_files: List[str],
        source_count: int,
        kept: List[Dict],
        removed: List[Dict]
    ) -> Dict:
        """生成元数据"""
        # 计算时间范围
        all_times = [news['time'] for news in kept + removed if news.get('time')]
        start_time = min(all_times) if all_times else None
        end_time = max(all_times) if all_times else None
        
        return {
            'type': 'cleaned_single',
            'source_files': [os.path.basename(f) for f in source_files],
            'source_count': source_count,
            'kept_count': len(kept),
            'removed_count': len(removed),
            'cleaning_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'time_range': {
                'start': start_time,
                'end': end_time
            },
            'criteria': self.criteria[:200] + '...',  # 只保存前200字符
            'ai_provider': self.ai_provider
        }
