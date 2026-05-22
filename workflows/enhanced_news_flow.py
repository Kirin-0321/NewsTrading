"""
增强版每日新闻分析流程
爬取 → 清洗 → 合并去重 → 导出 → AI分析
"""

from typing import Dict, Any
from .daily_news_flow import DailyNewsFlow


class EnhancedNewsFlow(DailyNewsFlow):
    """增强版每日新闻分析流程（在标准流程上增加合并去重步骤）"""

    workflow_id = "enhanced_news_flow"
    name = "增强版新闻分析流程"
    description = "自动执行爬取、清洗、合并去重、导出、AI分析的完整流程"

    def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        results = {}

        self.log("Step 1: 开始爬取新闻...")
        crawler_file = self._run_crawler(params.get("crawler", {}))
        if not crawler_file:
            raise Exception("爬取新闻失败")
        results["crawler_file"] = crawler_file
        self.log(f"Step 1: ✓ 完成，文件: {crawler_file}")

        self.log("Step 2: 开始清洗数据...")
        cleaned_file = self._run_cleaner(crawler_file, params.get("cleaner", {}))
        if not cleaned_file:
            raise Exception("清洗数据失败")
        results["cleaned_file"] = cleaned_file
        self.log(f"Step 2: ✓ 完成，文件: {cleaned_file}")

        self.log("Step 3: 开始合并去重...")
        merge_result = self._run_merger(params.get("merger", {}))
        if not merge_result:
            raise Exception("合并去重失败")
        results["merge_result"] = merge_result
        self.log(f"Step 3: ✓ 完成，合并了 {merge_result.get('total_files', 0)} 个文件")
        self.log(f"  去重前: {merge_result.get('total_news_before', 0)} 条")
        self.log(f"  去重后: {merge_result.get('total_news_after', 0)} 条")
        self.log(f"  去除重复: {merge_result.get('duplicates_removed', 0)} 条")

        self.log("Step 4: 开始导出数据...")
        export_file = self._run_export(params.get("export", {}))
        if not export_file:
            raise Exception("导出数据失败")
        results["export_file"] = export_file
        self.log(f"Step 4: ✓ 完成，文件: {export_file}")

        self.log("Step 5: 开始AI分析...")
        summary = self._get_summary(params.get("analyzer", {}))
        if summary:
            self.log(f"Step 5: 读取盘后总结成功，长度: {len(summary)}字")
        else:
            self.log("Step 5: 未找到盘后总结，将不使用盘后总结进行分析", level="warning")

        analysis_file = self._run_analyzer(export_file, summary, params.get("analyzer", {}))
        if not analysis_file:
            raise Exception("AI分析失败")
        results["analysis_file"] = analysis_file
        self.log(f"Step 5: ✓ 完成，文件: {analysis_file}")

        return results

    def _get_summary(self, params: Dict[str, Any]) -> str:
        """获取盘后总结（增强版路径：分析数据/盘后总结/月.日/）"""
        import os
        import glob
        from datetime import datetime, timedelta

        summary_mode = params.get("summary_mode", "auto")
        summary_file = params.get("summary_file", "")

        if summary_mode == "auto":
            yesterday = datetime.now() - timedelta(days=1)
            summary_dir = f"分析数据/盘后总结/{yesterday.month}.{yesterday.day}"
            if os.path.exists(summary_dir):
                md_files = glob.glob(os.path.join(summary_dir, "*.md"))
                if md_files:
                    self.log(f"  读取盘后总结: {md_files[0]}")
                    return self.read_file(md_files[0])
            self.log(f"  未找到盘后总结目录: {summary_dir}", level="warning")
            return ""
        if summary_file:
            self.log(f"  读取指定盘后总结: {summary_file}")
            return self.read_file(summary_file)
        return ""

    def _run_merger(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行数据合并去重"""
        import os
        from core.data_merger import DataMerger
        from datetime import datetime, timedelta

        try:
            merge_mode = params.get("mode", "recent")
            recent_count = params.get("recent_count", 5)
            time_range_hours = params.get("time_range_hours", None)

            self.log(
                f"  参数: 模式={merge_mode}, 最近文件数={recent_count}, "
                f"时间范围={time_range_hours}小时"
            )

            all_files = DataMerger.get_mergeable_files("data/cleaned")
            if not all_files:
                self.log("  警告: 没有找到可合并的文件", level="warning")
                return None

            self.log(f"  找到 {len(all_files)} 个清洗文件")

            if merge_mode == "recent":
                files_to_merge = (
                    all_files[-recent_count:]
                    if len(all_files) > recent_count
                    else all_files
                )
            elif merge_mode == "all":
                files_to_merge = all_files
            elif merge_mode == "time_range" and time_range_hours:
                cutoff_time = datetime.now() - timedelta(hours=time_range_hours)
                files_to_merge = [
                    fp
                    for fp in all_files
                    if datetime.fromtimestamp(os.path.getmtime(fp)) >= cutoff_time
                ]
            else:
                files_to_merge = all_files

            self.log(f"  选择合并 {len(files_to_merge)} 个文件:")
            for fp in files_to_merge:
                self.log(f"    - {os.path.basename(fp)}")

            result = DataMerger.merge_and_split_by_date(
                file_paths=files_to_merge,
                output_dir="data/cleaned",
            )

            if result.get("success"):
                self.log(f"  合并成功，生成 {result['days_count']} 个按天分割的文件:")
                for daily_file in result["daily_files"]:
                    self.log(
                        f"    - {daily_file['filename']}: {daily_file['count']} 条"
                    )
                if result.get("deleted_files"):
                    self.log(f"  已删除 {len(result['deleted_files'])} 个旧文件")

            return result

        except Exception as e:
            self.log(f"  合并去重执行失败: {str(e)}", level="error")
            raise
