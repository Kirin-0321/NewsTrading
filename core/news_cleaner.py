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

        kept, removed, error_skipped = self._ai_clean_batches(
            all_news, batch_size, progress_callback
        )
        kept.sort(key=lambda x: x.get("datetime") or x.get("time", ""))
        removed.sort(key=lambda x: x.get("datetime") or x.get("time", ""))

        metadata = self._generate_metadata(
            ["<memory>"], len(all_news), kept, removed
        )
        metadata["error_skipped"] = error_skipped
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
        """AI 分批清洗；单批失败时拆半重试，仍失败则跳过问题条目。"""
        kept = []
        removed = []
        error_skipped = 0

        total = len(news_list)
        total_batches = (total + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            start_idx = batch_idx * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch = news_list[start_idx:end_idx]

            batch_kept, batch_removed, batch_skipped = self._process_batch_resilient(
                batch, progress_callback
            )
            kept.extend(batch_kept)
            removed.extend(batch_removed)
            error_skipped += batch_skipped

            if progress_callback:
                progress_callback(
                    f"批次 {batch_idx + 1}/{total_batches}",
                    end_idx,
                    total,
                    len(batch_kept),
                    len(batch_removed),
                )

        return kept, removed, error_skipped

    def _process_batch_resilient(
        self,
        batch: List[Dict],
        progress_callback: Optional[Callable],
    ) -> tuple:
        """
        处理单批新闻；API 拒绝（如敏感词）时二分拆批，隔离问题条目。

        Returns:
            (kept, removed, error_skipped_count)
        """
        if not batch:
            return [], [], 0

        user_prompt = self._build_batch_prompt(batch)
        try:
            decisions = self._call_ai_judge(user_prompt, len(batch))
            kept, removed = [], []
            for news, (decision, reason) in zip(batch, decisions):
                if decision == "keep":
                    item = news.copy()
                    item["keep_reason"] = reason or "符合保留标准"
                    kept.append(item)
                else:
                    item = news.copy()
                    item["removal_reason"] = reason or "不符合保留标准"
                    removed.append(item)
            return kept, removed, 0

        except Exception as e:
            if len(batch) == 1:
                item = batch[0].copy()
                item["removal_reason"] = self._format_skip_reason(e)
                title = (item.get("title") or "")[:40]
                if progress_callback:
                    progress_callback(
                        f"单条跳过（API拒绝）: {title}"
                    )
                print(f"清洗跳过单条: {title} | {e}")
                return [], [item], 1

            mid = len(batch) // 2
            if progress_callback:
                progress_callback(
                    f"批次 {len(batch)} 条失败，拆为 {mid}+{len(batch) - mid} 重试..."
                )
            print(f"批次失败({len(batch)}条)，拆分重试: {e}")

            k1, r1, s1 = self._process_batch_resilient(
                batch[:mid], progress_callback
            )
            k2, r2, s2 = self._process_batch_resilient(
                batch[mid:], progress_callback
            )
            return k1 + k2, r1 + r2, s1 + s2

    @staticmethod
    def _format_skip_reason(error: Exception) -> str:
        """将 API 异常转为剔除原因。"""
        msg = str(error).lower()
        keywords = (
            "敏感", "content_filter", "content filter", "moderation",
            "审核", "blocked", "policy", "违规", "risk",
        )
        if any(k in msg for k in keywords):
            return "清洗跳过(内容触发审核)"
        return "清洗跳过(API失败)"
    
    def _build_batch_prompt(self, batch: List[Dict]) -> str:
        """
        构建批次 prompt。

        输入: 一批新闻 dict（含 title/time/source/content）。
        输出: 字符串 prompt，要求 AI 对每条新闻给出 'keep|理由' 或 'remove|理由'。

        说明:
        - content 不再截断（库内 p99=493 字，最长 1705 字，远低于 context 上限）
        - 强制要求输出剔除/保留理由，便于事后追溯（落到 rejected_news.reason）
        """
        prompt = "请判断以下新闻是否应该保留：\n\n"

        for idx, news in enumerate(batch, 1):
            prompt += f"{idx}. 【标题】{news.get('title', '无标题')}\n"
            prompt += f"   【时间】{news.get('time', '未知')}\n"
            if news.get('source'):
                prompt += f"   【来源】{news['source']}\n"
            if news.get('content'):
                prompt += f"   【内容】{news['content']}\n"
            prompt += "\n"

        prompt += (
            "\n【输出格式 - 严格遵守】\n"
            "每条新闻一行，格式为：序号. keep|理由(≤20字)  或  序号. remove|理由(≤20字)\n"
            "理由必须使用清洗标准中的分类，如：\n"
            "  保留类：政策类-发改委 / 政策类-央行 / 行业数据 / 龙头业绩 / 美政府关注-半导体 / 马斯克相关 / 重大技术突破\n"
            "  剔除类：钝化-美伊口水 / 钝化-油价常规 / 钝化-中东冲突 / 小公司日常 / 子公司常规 / 非核心人事 / 个人观点 / 与A股无关\n"
            "示例：\n"
            "1. keep|政策类-发改委\n"
            "2. remove|小公司子公司\n"
            "3. remove|钝化-美伊口水\n"
            "禁止输出其他内容、解释或空行。\n"
        )

        return prompt
    
    def _call_ai_judge(
        self, user_prompt: str, expected_count: int
    ) -> List[tuple]:
        """
        调用 AI 做清洗判断。

        输入:
            user_prompt: 已构造好的批次提示词
            expected_count: 该批新闻数量
        输出:
            [(decision, reason), ...]，长度等于 expected_count
        """
        system_prompt = f"""你是A股投资新闻清洗专家，负责筛选有投资价值的新闻。

{self.criteria}

请严格按照上述标准判断。输出格式由用户消息指定（每行 'N. keep|理由' 或 'N. remove|理由'）。"""

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
    ) -> List[tuple]:
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
            temperature=0.1,
            max_tokens=2000,
        )
        result_text = response.choices[0].message.content
        return self._parse_decisions(result_text, expected_count)
    
    def _parse_decisions(
        self, result_text: str, expected_count: int
    ) -> List[tuple]:
        """
        解析 AI 返回的判断结果。

        输入:
            result_text: AI 原始文本（期望每行形如 "1. keep|政策类-发改委"）
            expected_count: 该批新闻数量
        输出:
            [(decision, reason), ...]，长度恒等于 expected_count
            - decision: 'keep' 或 'remove'
            - reason: 字符串，最长 40 字符；无法解析时为空串

        兜底策略:
            - 行内只有 keep/remove 没有 |reason：reason 留空
            - 解析数量不足：用 ('remove', 'AI解析丢失-保守剔除') 补齐
              （比旧版默认 keep 更安全，避免假阴性放过钝化题材）
            - 解析数量过多：截断到 expected_count
        """
        decisions: List[tuple] = []
        for raw_line in result_text.strip().split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            low = line.lower()
            if 'keep' in low:
                decision = 'keep'
            elif 'remove' in low:
                decision = 'remove'
            else:
                continue

            reason = ''
            if '|' in line:
                reason = line.split('|', 1)[1].strip()[:40]
            decisions.append((decision, reason))

        while len(decisions) < expected_count:
            decisions.append(('remove', 'AI解析丢失-保守剔除'))

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
