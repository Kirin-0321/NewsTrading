"""
任务流基类
所有任务流都应继承此类
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional


class WorkflowBase:
    """任务流基类"""
    
    # 子类需要定义这些属性
    workflow_id = ""
    name = ""
    description = ""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化任务流
        
        Args:
            config_path: 配置文件路径，如果不提供则使用默认路径
        """
        self.config_path = config_path or self._get_default_config_path()
        self.config = self._load_config()
        self.logger = self._setup_logger()
        self.start_time = None
        self.results = {}
        
    def _get_default_config_path(self) -> str:
        """获取默认配置文件路径"""
        base_dir = Path(__file__).parent
        return str(base_dir / "configs" / f"{self.workflow_id}.json")
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            # 返回默认配置
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置（子类可覆盖）"""
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "enabled": True,
            "params": {}
        }
    
    def _setup_logger(self) -> logging.Logger:
        """设置日志记录器"""
        logger = logging.getLogger(f"workflow.{self.workflow_id}")
        logger.setLevel(logging.INFO)
        
        # 创建日志目录
        log_dir = Path("logs/workflows") / self.workflow_id
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 日志文件路径
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = log_dir / f"{timestamp}.log"
        latest_log = log_dir / "latest.log"
        
        # 文件处理器
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # 同时写入latest.log
        latest_handler = logging.FileHandler(latest_log, mode='w', encoding='utf-8')
        latest_handler.setLevel(logging.INFO)
        
        # 格式化
        formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(formatter)
        latest_handler.setFormatter(formatter)
        
        # 清除已有处理器
        logger.handlers.clear()
        logger.addHandler(file_handler)
        logger.addHandler(latest_handler)
        
        return logger
    
    def log(self, message: str, level: str = "info"):
        """记录日志"""
        if level == "info":
            self.logger.info(message)
        elif level == "warning":
            self.logger.warning(message)
        elif level == "error":
            self.logger.error(message)
    
    def run(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        运行任务流
        
        Args:
            params: 运行时参数，如果提供则覆盖配置文件中的参数
            
        Returns:
            执行结果字典
        """
        self.start_time = datetime.now()
        execution_mode = "手动执行" if params else "定时执行"
        
        try:
            self.log("=" * 60)
            self.log(f"任务流开始: {self.name}")
            self.log(f"执行模式: {execution_mode}")
            self.log("=" * 60)
            
            # 使用提供的参数或配置文件中的参数
            run_params = params if params is not None else self.config.get('params', {})
            
            # 执行任务流（由子类实现）
            self.results = self.execute(run_params)
            
            # 计算耗时
            duration = (datetime.now() - self.start_time).total_seconds()
            
            self.log("=" * 60)
            self.log(f"任务流完成: 成功")
            self.log(f"总耗时: {duration:.1f}秒 ({duration/60:.1f}分钟)")
            self.log("=" * 60)
            
            # 更新历史记录
            self._update_history(status="success", duration=duration)
            
            return {
                "status": "success",
                "duration": duration,
                "results": self.results
            }
            
        except Exception as e:
            duration = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
            
            self.log("=" * 60, level="error")
            self.log(f"任务流失败: {str(e)}", level="error")
            self.log(f"错误类型: {type(e).__name__}", level="error")
            
            # 记录堆栈信息
            import traceback
            self.log(f"堆栈信息:\n{traceback.format_exc()}", level="error")
            
            self.log("=" * 60, level="error")
            
            # 更新历史记录
            self._update_history(status="failure", duration=duration, error=str(e))
            
            return {
                "status": "failure",
                "duration": duration,
                "error": str(e)
            }
    
    def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行任务流的具体逻辑（子类必须实现）
        
        Args:
            params: 执行参数
            
        Returns:
            执行结果字典
        """
        raise NotImplementedError("子类必须实现execute方法")
    
    def _update_history(self, status: str, duration: float, error: str = None):
        """更新历史记录到配置文件"""
        try:
            # 读取当前配置
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            else:
                config = self._get_default_config()
            
            # 更新历史记录
            history = config.get('history', {})
            history['last_run'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history['last_status'] = status
            history['last_error'] = error
            history['last_duration'] = duration
            history['total_runs'] = history.get('total_runs', 0) + 1
            
            if status == "success":
                history['success_count'] = history.get('success_count', 0) + 1
            else:
                history['failure_count'] = history.get('failure_count', 0) + 1
            
            config['history'] = history
            
            # 写回配置文件
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            self.log(f"更新历史记录失败: {str(e)}", level="warning")
    
    # ========== 工具方法 ==========
    
    def read_file(self, file_path: str) -> str:
        """读取文件内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            self.log(f"读取文件失败 {file_path}: {str(e)}", level="warning")
            return ""
    
    def read_previous_day_summary(self) -> str:
        """读取前一天的盘后总结"""
        from datetime import timedelta
        
        # 计算前一天日期
        yesterday = datetime.now() - timedelta(days=1)
        date_str = yesterday.strftime("%Y_%m_%d")
        
        # 构建文件路径
        summary_file = f"盘后总结/{date_str}.md"
        
        if os.path.exists(summary_file):
            self.log(f"读取盘后总结: {summary_file}")
            return self.read_file(summary_file)
        else:
            self.log(f"未找到盘后总结文件: {summary_file}", level="warning")
            return ""

