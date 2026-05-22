"""
定时任务调度服务
使用schedule库实现定时任务
"""

import schedule
import time
import json
import os
from datetime import datetime
from threading import Thread
from typing import Callable, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SchedulerService:
    """定时任务调度服务"""
    
    def __init__(self, tasks_file='data/schedule_tasks.json'):
        self.tasks_file = tasks_file
        self.running = False
        self.thread = None
        self.on_task_run = None  # 任务执行回调
        
    def start(self):
        """启动调度服务"""
        if self.running:
            logger.warning("调度服务已经在运行")
            return
        
        self.running = True
        self.load_and_schedule_tasks()
        
        # 在新线程中运行调度器
        self.thread = Thread(target=self._run_scheduler, daemon=True)
        self.thread.start()
        logger.info("✅ 定时任务调度服务已启动")
    
    def stop(self):
        """停止调度服务"""
        self.running = False
        schedule.clear()
        if self.thread:
            self.thread.join(timeout=2)
        logger.info("⏹️ 定时任务调度服务已停止")
    
    def _run_scheduler(self):
        """运行调度器（在后台线程中）"""
        logger.info("📅 定时任务调度器开始运行...")
        while self.running:
            try:
                schedule.run_pending()
                time.sleep(1)  # 每秒检查一次
            except Exception as e:
                logger.error(f"调度器运行出错: {e}")
    
    def load_and_schedule_tasks(self):
        """加载并调度所有任务"""
        if not os.path.exists(self.tasks_file):
            logger.info("没有找到任务配置文件")
            return
        
        try:
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            
            # 清除现有任务
            schedule.clear()
            
            # 为每个启用的任务创建调度
            for task in tasks:
                if task.get('enabled', False):
                    self._schedule_task(task)
            
            logger.info(f"已加载 {len(tasks)} 个任务，其中 {len(schedule.jobs)} 个已启用")
            
        except Exception as e:
            logger.error(f"加载任务失败: {e}")
    
    def _schedule_task(self, task):
        """调度单个任务"""
        task_time = task.get('time', '09:00')
        task_id = task.get('id')
        
        def job():
            """任务执行函数"""
            try:
                logger.info(f"⏰ 开始执行定时任务: {task.get('name')}")
                
                # 更新上次执行时间
                self._update_task_last_run(task_id)
                
                # 执行任务（调用爬虫）
                if self.on_task_run:
                    self.on_task_run(task)
                else:
                    logger.warning("未设置任务执行回调")
                
                logger.info(f"✅ 任务执行完成: {task.get('name')}")
                
            except Exception as e:
                logger.error(f"❌ 任务执行失败: {task.get('name')}, 错误: {e}")
        
        # 使用schedule库调度任务
        schedule.every().day.at(task_time).do(job).tag(task_id)
        logger.info(f"📌 已调度任务: {task.get('name')} at {task_time}")
    
    def _update_task_last_run(self, task_id):
        """更新任务的上次执行时间"""
        try:
            if not os.path.exists(self.tasks_file):
                return
            
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            
            # 更新对应任务的last_run
            for task in tasks:
                if task.get('id') == task_id:
                    task['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    break
            
            # 保存回文件
            with open(self.tasks_file, 'w', encoding='utf-8') as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"更新任务执行时间失败: {e}")
    
    def reload_tasks(self):
        """重新加载任务（用于任务更新后）"""
        if self.running:
            self.load_and_schedule_tasks()
    
    def set_task_callback(self, callback: Callable):
        """设置任务执行回调函数"""
        self.on_task_run = callback
    
    def get_next_run_time(self, task_id):
        """获取任务的下次执行时间"""
        for job in schedule.jobs:
            if task_id in job.tags:
                return job.next_run
        return None
    
    def list_scheduled_jobs(self):
        """列出所有已调度的任务"""
        return [
            {
                'tags': list(job.tags),
                'next_run': job.next_run.strftime('%Y-%m-%d %H:%M:%S') if job.next_run else None
            }
            for job in schedule.jobs
        ]
