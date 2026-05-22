"""
任务流引擎
负责加载、管理和执行任务流
"""

import os
import json
import importlib
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


class WorkflowEngine:
    """任务流引擎"""
    
    def __init__(self):
        """初始化引擎"""
        self.workflows_dir = Path("workflows")
        self.configs_dir = self.workflows_dir / "configs"
        self.workflows = {}
        self._load_workflows()
    
    def _load_workflows(self):
        """加载所有任务流"""
        if not self.configs_dir.exists():
            return
        
        # 遍历所有配置文件
        for config_file in self.configs_dir.glob("*.json"):
            try:
                workflow_id = config_file.stem
                self._load_workflow(workflow_id)
            except Exception as e:
                print(f"加载任务流失败 {config_file}: {str(e)}")
    
    def _load_workflow(self, workflow_id: str):
        """加载单个任务流"""
        # 导入模块
        module = importlib.import_module(f"workflows.{workflow_id}")
        
        from workflows.base import WorkflowBase

        workflow_class = None
        for item_name in dir(module):
            item = getattr(module, item_name)
            if (
                isinstance(item, type)
                and issubclass(item, WorkflowBase)
                and item is not WorkflowBase
            ):
                workflow_class = item
                break
        
        if workflow_class:
            self.workflows[workflow_id] = workflow_class
    
    def get_workflow_list(self) -> List[Dict[str, Any]]:
        """
        获取所有任务流列表
        
        Returns:
            任务流信息列表
        """
        workflow_list = []
        
        for workflow_id, workflow_class in self.workflows.items():
            # 读取配置文件
            config_path = self.configs_dir / f"{workflow_id}.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            else:
                config = {}
            
            workflow_list.append({
                'workflow_id': workflow_id,
                'name': config.get('name', workflow_class.name),
                'description': config.get('description', workflow_class.description),
                'enabled': config.get('enabled', True),
                'schedule': config.get('schedule', {}),
                'history': config.get('history', {}),
                'config': config
            })
        
        return workflow_list
    
    def get_workflow_info(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个任务流信息
        
        Args:
            workflow_id: 任务流ID
            
        Returns:
            任务流信息字典
        """
        if workflow_id not in self.workflows:
            return None
        
        workflow_class = self.workflows[workflow_id]
        config_path = self.configs_dir / f"{workflow_id}.json"
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            config = {}
        
        return {
            'workflow_id': workflow_id,
            'name': config.get('name', workflow_class.name),
            'description': config.get('description', workflow_class.description),
            'enabled': config.get('enabled', True),
            'schedule': config.get('schedule', {}),
            'params': config.get('params', {}),
            'history': config.get('history', {}),
            'config': config
        }
    
    def execute_workflow(self, workflow_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        执行任务流
        
        Args:
            workflow_id: 任务流ID
            params: 运行时参数，如果不提供则使用配置文件中的参数
            
        Returns:
            执行结果
        """
        if workflow_id not in self.workflows:
            return {
                "status": "failure",
                "error": f"任务流不存在: {workflow_id}"
            }
        
        try:
            # 创建任务流实例
            workflow_class = self.workflows[workflow_id]
            workflow = workflow_class()
            
            # 执行任务流
            result = workflow.run(params)
            
            return result
            
        except Exception as e:
            return {
                "status": "failure",
                "error": str(e)
            }
    
    def update_workflow_config(self, workflow_id: str, config: Dict[str, Any]):
        """
        更新任务流配置
        
        Args:
            workflow_id: 任务流ID
            config: 新配置
        """
        config_path = self.configs_dir / f"{workflow_id}.json"
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    
    def toggle_workflow(self, workflow_id: str, enabled: bool):
        """
        启用/禁用任务流
        
        Args:
            workflow_id: 任务流ID
            enabled: 是否启用
        """
        config_path = self.configs_dir / f"{workflow_id}.json"
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            config = {}
        
        config['enabled'] = enabled
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    
    def get_workflow_logs(self, workflow_id: str, limit: int = 10) -> List[str]:
        """
        获取任务流的执行日志文件列表
        
        Args:
            workflow_id: 任务流ID
            limit: 返回数量限制
            
        Returns:
            日志文件路径列表（按时间倒序）
        """
        log_dir = Path("logs/workflows") / workflow_id
        
        if not log_dir.exists():
            return []
        
        # 获取所有日志文件（排除latest.log）
        log_files = [f for f in log_dir.glob("*.log") if f.name != "latest.log"]
        
        # 按修改时间倒序排序
        log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        
        # 返回指定数量
        return [str(f) for f in log_files[:limit]]
    
    def read_log_file(self, log_path: str) -> str:
        """
        读取日志文件内容
        
        Args:
            log_path: 日志文件路径
            
        Returns:
            日志内容
        """
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"读取日志失败: {str(e)}"
    
    def start_interval_schedule(self, workflow_id: str, interval_hours: int = 1):
        """
        启动间隔调度（每隔N小时执行一次）
        
        Args:
            workflow_id: 任务流ID
            interval_hours: 间隔小时数
        """
        import schedule
        import threading
        import time
        
        if workflow_id not in self.workflows:
            raise ValueError(f"任务流不存在: {workflow_id}")
        
        workflow_class = self.workflows[workflow_id]
        workflow_name = workflow_class.name
        
        def job():
            """定时执行的任务"""
            print(f"\n⏰ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行任务流: {workflow_name}")
            result = self.execute_workflow(workflow_id)
            if result['status'] == 'success':
                print(f"✅ 任务流执行成功，耗时: {result['duration']:.1f}秒")
            else:
                print(f"❌ 任务流执行失败: {result.get('error', '未知错误')}")
        
        # 清除该任务流的旧调度
        schedule.clear(workflow_id)
        
        # 创建新的间隔调度
        schedule.every(interval_hours).hours.do(job).tag(workflow_id)
        
        print(f"✅ 已启动间隔调度: {workflow_name}")
        print(f"   间隔时间: 每 {interval_hours} 小时")
        print(f"   首次执行: 立即执行")
        print(f"   下次执行: {interval_hours} 小时后")
        
        # 立即执行一次
        job()
        
        # 在后台线程中运行调度器
        def run_scheduler():
            while True:
                schedule.run_pending()
                time.sleep(60)  # 每分钟检查一次
        
        if not hasattr(self, '_scheduler_thread') or not self._scheduler_thread.is_alive():
            self._scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            self._scheduler_thread.start()
        
        return True


# 全局引擎实例
_engine_instance = None

def get_workflow_engine() -> WorkflowEngine:
    """获取任务流引擎单例"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = WorkflowEngine()
    return _engine_instance

