"""
配置文件
"""

import os

# 项目根目录
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# 爬虫配置
CRAWLER_CONFIG = {
    # 目标网站
    'base_url': 'https://724.guzhang.com/',
    
    # 请求超时时间（秒）
    'timeout': 15,
    
    # 请求间隔（秒）- 避免对服务器造成压力
    'request_interval': 2,
    
    # 重试次数
    'max_retries': 3,
    
    # 用户代理
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    
    # Chrome 路径配置
    'chrome_path': os.path.join(BASE_DIR, 'chrome-win64', 'chrome.exe'),
    'chromedriver_path': os.path.join(BASE_DIR, 'chrome-win64', 'chromedriver.exe'),
}

# 存储配置
STORAGE_CONFIG = {
    # 数据保存目录
    'save_dir': 'data',
    
    # 是否保存JSON格式
    'save_json': True,
    
    # 是否保存Markdown格式
    'save_markdown': True,
    
    # JSON文件编码
    'json_encoding': 'utf-8',
    
    # JSON缩进
    'json_indent': 2,
}

# 定时任务配置
SCHEDULE_CONFIG = {
    # 默认执行模式: once, hourly, daily, interval
    'default_mode': 'hourly',
    
    # 每日执行时间（用于daily模式）
    'daily_time': '09:00',
    
    # 间隔分钟数（用于interval模式）
    'interval_minutes': 60,
}

# Markdown生成配置
MARKDOWN_CONFIG = {
    # 内容最大长度（字符）
    'max_content_length': 500,
    
    # 相似文章最多显示数量
    'max_similar_articles': 3,
    
    # 是否包含统计图表
    'include_charts': False,
}

# 日志配置
LOG_CONFIG = {
    # 日志文件路径
    'log_file': 'crawler.log',
    
    # 日志级别: DEBUG, INFO, WARNING, ERROR
    'log_level': 'INFO',
    
    # 是否在控制台输出日志
    'console_output': True,
}

