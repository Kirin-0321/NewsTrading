"""
使用滚动加载的爬虫
模拟用户滚动到页面底部，自动加载更多历史新闻
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Callable, Optional
import time
import re

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("⚠ Selenium未安装: pip install selenium")


class GuZhangNewsCrawlerScroll:
    """使用滚动加载的爬虫"""
    
    def __init__(self, save_dir='data', 
                 chrome_path=None,
                 chromedriver_path=None):
        """
        初始化爬虫
        
        Args:
            save_dir: 数据保存目录
            chrome_path: Chrome浏览器路径（None则自动检测）
            chromedriver_path: ChromeDriver路径（None则自动检测）
        """
        self.base_url = "https://724.guzhang.com/"
        self.save_dir = save_dir
        
        # 智能检测Chrome路径（支持打包后的exe）
        if chrome_path is None or chromedriver_path is None:
            chrome_path, chromedriver_path = self._get_chrome_paths()
        
        self.chrome_path = chrome_path
        self.chromedriver_path = chromedriver_path
        self.progress_callback: Optional[Callable] = None
        
        # 增量爬取参数
        self.auto_stop_enabled = False
        self.db_latest_time = None
        self.db_latest_title = None
        self.headless = True
        self._stop_requested = False
        self._driver = None
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        
        self._create_directories()
        
        # 检查文件是否存在
        if os.path.exists(self.chrome_path):
            print(f"✓ 找到Chrome浏览器")
        else:
            print(f"⚠ Chrome浏览器不存在: {self.chrome_path}")
            
        if os.path.exists(self.chromedriver_path):
            print(f"✓ 找到ChromeDriver")
        else:
            print(f"⚠ ChromeDriver不存在: {self.chromedriver_path}")
    
    def set_progress_callback(self, callback: Callable):
        """设置进度回调函数"""
        self.progress_callback = callback

    def request_stop(self):
        """请求协作式停止（当前滚动结束后退出并关闭浏览器）"""
        self._stop_requested = True
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
    
    def set_auto_stop(self, enabled: bool, latest_time=None, latest_title=None):
        """
        设置自动停止参数
        
        Args:
            enabled: 是否启用自动停止
            latest_time: 数据库最新新闻时间（datetime对象）
            latest_title: 数据库最新新闻标题
        """
        self.auto_stop_enabled = enabled
        self.db_latest_time = latest_time
        self.db_latest_title = latest_title
    
    def _get_chrome_paths(self):
        """智能获取Chrome和ChromeDriver路径（兼容打包后的exe）"""
        # 优先级1: 打包后的程序目录（与exe同级）
        if getattr(sys, 'frozen', False):
            # 打包后的程序
            exe_dir = os.path.dirname(sys.executable)
            chrome_dir = os.path.join(exe_dir, 'chrome-win64')
        else:
            # 开发环境
            chrome_dir = os.path.join(os.getcwd(), 'chrome-win64')
        
        chrome_path = os.path.join(chrome_dir, 'chrome.exe')
        chromedriver_path = os.path.join(chrome_dir, 'chromedriver.exe')
        
        # 检查文件是否存在
        if not os.path.exists(chrome_path):
            print(f"⚠ Chrome未找到: {chrome_path}")
        if not os.path.exists(chromedriver_path):
            print(f"⚠ ChromeDriver未找到: {chromedriver_path}")
        
        return chrome_path, chromedriver_path
    
    def _report_progress(self, current, total, news_count, message=''):
        """报告进度"""
        if self.progress_callback:
            self.progress_callback(current, total, news_count, message)
    
    def _create_directories(self):
        """创建保存目录"""
        dirs = [
            self.save_dir,
            os.path.join(self.save_dir, 'raw'),
            os.path.join(self.save_dir, 'summaries'),
        ]
        for dir_path in dirs:
            os.makedirs(dir_path, exist_ok=True)
    
    def fetch_page_with_scroll(
        self,
        scroll_times=36,
        wait_seconds=6,
        max_no_change=3,
        headless=True,
    ) -> str:
        """
        使用滚动加载页面
        
        Args:
            scroll_times: 滚动次数
            wait_seconds: 每次滚动后等待秒数
            max_no_change: 连续无新数据停止次数
            headless: 是否无头模式
            
        Returns:
            完整的网页HTML
        """
        self.headless = headless
        self._stop_requested = False
        print(f"\n{'='*60}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 准备启动浏览器...")
        print(f"{'='*60}")
        
        # 配置Chrome选项
        chrome_options = Options()
        chrome_options.binary_location = self.chrome_path
        if headless:
            chrome_options.add_argument('--headless')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument(f'user-agent={self.headers["User-Agent"]}')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        
        driver = None
        try:
            service = Service(executable_path=self.chromedriver_path)
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动Chrome浏览器...")
            driver = webdriver.Chrome(service=service, options=chrome_options)
            self._driver = driver
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 浏览器启动成功！")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在访问网站...")
            
            driver.get(self.base_url)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 网站加载完成")
            
            time.sleep(3)  # 等待初始内容加载
            
            # 获取初始新闻数量
            initial_html = driver.page_source
            initial_soup = BeautifulSoup(initial_html, 'html.parser')
            initial_count = len(initial_soup.find_all('li', class_='recent-news-item'))
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 初始新闻数量: {initial_count} 条")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始滚动加载...")
            print(f"目标滚动次数: {scroll_times} 次")
            print(f"每次等待时间: {wait_seconds} 秒\n")
            
            last_count = initial_count
            no_change_count = 0  # 连续未变化次数
            
            for i in range(scroll_times):
                if self._stop_requested:
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠ 收到停止请求，结束滚动")
                    break

                # 滚动到页面底部
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                
                # 等待新内容加载
                time.sleep(wait_seconds)
                
                # 检查新闻数量
                current_html = driver.page_source
                current_soup = BeautifulSoup(current_html, 'html.parser')
                news_items_list = current_soup.find_all('li', class_='recent-news-item')
                current_count = len(news_items_list)
                
                # 计算新增数量
                new_items = current_count - last_count
                
                # 获取最远（最旧）一条新闻的时间
                oldest_time_str = ""
                oldest_timestamp = None
                should_stop = False
                
                if news_items_list:
                    # 获取最后一条新闻（最旧的）
                    last_item = news_items_list[-1]
                    ptime_elem = last_item.find('span', class_='ptime')
                    if ptime_elem:
                        try:
                            timestamp = int(ptime_elem.text)
                            oldest_timestamp = timestamp
                            oldest_dt = datetime.fromtimestamp(timestamp)
                            oldest_time_str = oldest_dt.strftime('%Y-%m-%d %H:%M:%S')
                        except:
                            oldest_time_str = ""
                    
                    # 检查是否需要自动停止
                    if self.auto_stop_enabled and self.db_latest_time:
                        # 检查标题匹配
                        if self.db_latest_title:
                            for item in news_items_list:
                                title_elem = item.find('h2')
                                if title_elem:
                                    title = title_elem.text.strip()
                                    if title == self.db_latest_title:
                                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠ [自动停止] 遇到已有新闻（标题匹配）")
                                        print(f"  标题: {title}")
                                        should_stop = True
                                        break
                        
                        # 检查时间边界
                        if not should_stop and oldest_timestamp:
                            db_timestamp = int(self.db_latest_time.timestamp())
                            if oldest_timestamp <= db_timestamp:
                                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠ [自动停止] 时间到达边界")
                                print(f"  当前最远: {oldest_time_str}")
                                print(f"  数据库最新: {self.db_latest_time.strftime('%Y-%m-%d %H:%M:%S')}")
                                should_stop = True
                
                # 报告进度
                msg = f"第 {i+1}/{scroll_times} 次滚动 | 新增: +{new_items} | 总计: {current_count}"
                if oldest_time_str:
                    msg += f" | 最远: {oldest_time_str}"
                self._report_progress(i+1, scroll_times, current_count, msg)
                
                # 如果需要自动停止，立即退出
                if should_stop:
                    break
                
                if new_items > 0:
                    console_msg = f"[{datetime.now().strftime('%H:%M:%S')}] 第 {i+1}/{scroll_times} 次滚动 | 新增: +{new_items} 条 | 总计: {current_count} 条"
                    if oldest_time_str:
                        console_msg += f" | 最远: {oldest_time_str}"
                    console_msg += " ✓"
                    print(console_msg)
                    last_count = current_count
                    no_change_count = 0
                else:
                    no_change_count += 1
                    console_msg = f"[{datetime.now().strftime('%H:%M:%S')}] 第 {i+1}/{scroll_times} 次滚动 | 无新增 | 总计: {current_count} 条"
                    if oldest_time_str:
                        console_msg += f" | 最远: {oldest_time_str}"
                    console_msg += " ⚠"
                    print(console_msg)
                    
                    # 如果连续N次没有新内容，提前结束
                    if no_change_count >= max_no_change:
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠ 连续{no_change_count}次未加载新内容，停止滚动")
                        break
            
            # 获取最终页面
            html = driver.page_source
            
            # 统计结果
            soup = BeautifulSoup(html, 'html.parser')
            news_items = soup.find_all('li', class_='recent-news-item')
            
            print(f"\n{'='*60}")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 滚动加载完成！")
            print(f"  - 实际滚动次数: {i+1} 次")
            print(f"  - 初始新闻: {initial_count} 条")
            print(f"  - 最终新闻: {len(news_items)} 条")
            print(f"  - 新增新闻: {len(news_items) - initial_count} 条")
            print(f"{'='*60}\n")
            
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            self._driver = None
            return html
            
        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✗ 出错: {e}")
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            self._driver = None
            return None
    
    def parse_news(self, html: str) -> List[Dict]:
        """解析新闻数据"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始解析新闻数据...")
        
        soup = BeautifulSoup(html, 'html.parser')
        news_items = soup.find_all('li', class_='recent-news-item')
        
        news_list = []
        for item in news_items:
            try:
                news_data = self._parse_single_news(item)
                if news_data:
                    news_list.append(news_data)
            except:
                continue
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 原始数量: {len(news_list)} 条")
        
        # 去重（同时根据ID和标题）
        seen_ids = set()
        seen_titles = set()
        unique_news = []
        duplicates = 0
        
        for news in news_list:
            news_id = news.get('id', '')
            title = news.get('title', '').strip()
            
            # 跳过空标题
            if not title:
                duplicates += 1
                continue
            
            # 检查是否重复（ID或标题任一重复都算重复）
            is_duplicate = False
            
            if news_id and news_id in seen_ids:
                is_duplicate = True
            if title in seen_titles:
                is_duplicate = True
            
            if not is_duplicate:
                if news_id:
                    seen_ids.add(news_id)
                seen_titles.add(title)
                unique_news.append(news)
            else:
                duplicates += 1
        
        if duplicates > 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 去除重复: {duplicates} 条（{len(unique_news)} 条有效）")
        
        # 计算时间跨度
        if unique_news:
            sorted_news = sorted(unique_news, key=lambda x: x.get('timestamp', 0))
            start_time = sorted_news[0].get('datetime', '')
            end_time = sorted_news[-1].get('datetime', '')
            
            if start_time and end_time:
                start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
                hours = (end_dt - start_dt).total_seconds() / 3600
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 解析完成:")
                print(f"  - 有效数量: {len(unique_news)} 条")
                print(f"  - 时间跨度: {hours:.1f} 小时")
                print(f"  - 最早: {start_time}")
                print(f"  - 最新: {end_time}")
        
        return unique_news
    
    def _parse_single_news(self, item) -> Dict:
        """解析单条新闻"""
        news_id = item.get('data-aid', '')
        
        ptime_elem = item.find('span', class_='ptime')
        timestamp = int(ptime_elem.text) if ptime_elem else 0
        
        title_elem = item.find('h2')
        title = title_elem.text.strip() if title_elem else ''
        
        content_elem = item.find('div', class_='news-content')
        content = ''
        if content_elem:
            p_elem = content_elem.find('p')
            content = p_elem.text.strip() if p_elem else ''
        
        # 来源字段: 网站使用 span.from 而非 span.source-name
        source_elem = item.find('span', class_='from')
        source = source_elem.text.strip() if source_elem else ''
        
        datetime_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S') if timestamp else ''
        
        return {
            'id': news_id,
            'timestamp': timestamp,
            'datetime': datetime_str,
            'title': title,
            'content': content,
            'source': source,
        }
    
    def save_as_json(self, news_list: List[Dict]) -> str:
        """保存为JSON"""
        # 获取第一条和最后一条新闻的时间
        if news_list:
            sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
            first_time = sorted_news[0].get('datetime', '')
            last_time = sorted_news[-1].get('datetime', '')
            
            if first_time and last_time:
                # 解析时间：2025-12-20 09:30:00 -> 12-20-09
                first_dt = datetime.strptime(first_time, '%Y-%m-%d %H:%M:%S')
                last_dt = datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')
                
                first_str = first_dt.strftime('%m-%d-%H')
                last_str = last_dt.strftime('%m-%d-%H')
                
                filename_base = f'{first_str}——{last_str}'
            else:
                filename_base = datetime.now().strftime('%Y-%m-%d')
        else:
            filename_base = datetime.now().strftime('%Y-%m-%d')
        
        data = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'crawl_method': 'scroll',
            'total': len(news_list),
            'time_range': {
                'start': sorted_news[0].get('datetime', '') if news_list else '',
                'end': sorted_news[-1].get('datetime', '') if news_list else ''
            },
            'news': news_list
        }
        
        filename = os.path.join(self.save_dir, 'raw', f'{filename_base}.json')
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ JSON已保存: {filename}")
        return filename
    
    def save_as_markdown(self, news_list: List[Dict]) -> str:
        """保存为Markdown格式"""
        # 获取第一条和最后一条新闻的时间
        if news_list:
            sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
            first_time = sorted_news[0].get('datetime', '')
            last_time = sorted_news[-1].get('datetime', '')
            
            if first_time and last_time:
                first_dt = datetime.strptime(first_time, '%Y-%m-%d %H:%M:%S')
                last_dt = datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')
                
                first_str = first_dt.strftime('%m-%d-%H')
                last_str = last_dt.strftime('%m-%d-%H')
                
                filename_base = f'{first_str}——{last_str}'
                date_str = first_dt.strftime('%Y-%m-%d')
            else:
                filename_base = datetime.now().strftime('%Y-%m-%d')
                date_str = datetime.now().strftime('%Y-%m-%d')
        else:
            filename_base = datetime.now().strftime('%Y-%m-%d')
            date_str = datetime.now().strftime('%Y-%m-%d')
        
        # 构建Markdown内容
        lines = []
        lines.append(f"# {date_str} 财经新闻汇总\n")
        
        # 统计信息
        lines.append("## 📊 统计信息\n")
        lines.append(f"- **总计**: {len(news_list)} 条新闻")
        lines.append(f"- **抓取时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- **抓取方式**: 滚动加载\n")
        
        # 时间跨度
        if news_list:
            sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
            start_time = sorted_news[0].get('datetime', '')
            end_time = sorted_news[-1].get('datetime', '')
            if start_time and end_time:
                lines.append(f"- **时间范围**: {start_time} ~ {end_time}")
                
                start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
                hours = (end_dt - start_dt).total_seconds() / 3600
                lines.append(f"- **时间跨度**: {hours:.1f} 小时\n")
        
        # 来源统计
        source_count = {}
        for news in news_list:
            source = news.get('source', '未知')
            if source:
                source_count[source] = source_count.get(source, 0) + 1
        
        if source_count:
            lines.append("### 📰 新闻来源分布\n")
            sorted_sources = sorted(source_count.items(), key=lambda x: x[1], reverse=True)
            for source, count in sorted_sources[:10]:  # 只显示前10个来源
                percentage = (count / len(news_list) * 100) if len(news_list) > 0 else 0
                lines.append(f"- **{source}**: {count} 条 ({percentage:.1f}%)")
            lines.append("")
        
        lines.append("---\n")
        
        # 新闻详情
        lines.append("## 📰 新闻详情\n")
        
        # 按时间倒序
        sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0), reverse=True)
        
        for i, news in enumerate(sorted_news, 1):
            lines.append(f"### {i}. {news['title']}\n")
            lines.append(f"**时间**: {news['datetime']} | **来源**: {news['source']}\n")
            
            if news['content']:
                content = news['content']
                if len(content) > 500:
                    content = content[:500] + "..."
                lines.append(f"{content}\n")
            
            lines.append("---\n")
        
        # 页脚
        lines.append(f"\n> 数据来源: [鼓掌网]({self.base_url})")
        lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"> 抓取方式: 滚动加载")
        
        md_content = "\n".join(lines)
        
        # 保存文件
        filename = os.path.join(self.save_dir, 'summaries', f'{filename_base}_summary.md')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Markdown已保存: {filename}")
        return filename
    
    def save_as_txt(self, news_list: List[Dict]) -> str:
        """保存为TXT格式（只有时间和标题，极简格式）"""
        # 获取文件名
        if news_list:
            sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
            first_time = sorted_news[0].get('datetime', '')
            last_time = sorted_news[-1].get('datetime', '')
            
            if first_time and last_time:
                first_dt = datetime.strptime(first_time, '%Y-%m-%d %H:%M:%S')
                last_dt = datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')
                
                first_str = first_dt.strftime('%m-%d-%H')
                last_str = last_dt.strftime('%m-%d-%H')
                
                filename_base = f'{first_str}——{last_str}'
            else:
                filename_base = datetime.now().strftime('%Y-%m-%d')
        else:
            filename_base = datetime.now().strftime('%Y-%m-%d')
        
        # 构建TXT内容（极简格式）
        lines = []
        
        # 简短的统计信息
        lines.append(f"=== {filename_base} ===")
        lines.append(f"总计: {len(news_list)} 条")
        lines.append("")
        
        # 按时间倒序（最新在前）
        sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0), reverse=True)
        
        for news in sorted_news:
            # 只保存时间和标题，用 | 分隔
            time_str = news['datetime'].split()[1] if news.get('datetime') else ''  # 只要时分秒
            title = news.get('title', '')
            if time_str and title:
                lines.append(f"{time_str} | {title}")
        
        txt_content = "\n".join(lines)
        
        # 保存文件
        filename = os.path.join(self.save_dir, 'summaries', f'{filename_base}_titles.txt')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(txt_content)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ TXT已保存: {filename}")
        return filename
    
    def split_news_by_date(self, news_list: List[Dict]) -> Dict[str, List[Dict]]:
        """
        按日期分割新闻
        
        Args:
            news_list: 新闻列表
            
        Returns:
            按日期分组的字典 {日期: [新闻列表]}
        """
        news_by_date = {}
        
        for news in news_list:
            if news.get('datetime'):
                # 提取日期部分：2025-12-20 13:30:00 -> 2025-12-20
                date_str = news['datetime'].split()[0]
                
                if date_str not in news_by_date:
                    news_by_date[date_str] = []
                
                news_by_date[date_str].append(news)
        
        return news_by_date
    
    def save_daily_files(self, news_by_date: Dict[str, List[Dict]]) -> List[str]:
        """
        保存按天分割的文件
        
        Args:
            news_by_date: 按日期分组的新闻
            
        Returns:
            保存的文件列表
        """
        all_files = []
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 检测到跨天数据，按天分割保存...")
        print(f"共 {len(news_by_date)} 天的数据\n")
        
        for date_str, daily_news in sorted(news_by_date.items()):
            # 排序新闻
            sorted_news = sorted(daily_news, key=lambda x: x.get('timestamp', 0))
            
            # 获取该天的时间范围
            first_time = sorted_news[0].get('datetime', '')
            last_time = sorted_news[-1].get('datetime', '')
            
            if first_time and last_time:
                first_dt = datetime.strptime(first_time, '%Y-%m-%d %H:%M:%S')
                last_dt = datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')
                
                # 文件名：月-日_开始时-结束时
                date_part = first_dt.strftime('%m-%d')
                start_hour = first_dt.strftime('%H')
                end_hour = last_dt.strftime('%H')
                
                filename_base = f'{date_part}_{start_hour}-{end_hour}'
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 保存 {date_str} 的数据...")
                print(f"  - 时间范围: {first_time} ~ {last_time}")
                print(f"  - 新闻数量: {len(daily_news)} 条")
                print(f"  - 文件名: {filename_base}")
                
                # 保存三种格式
                json_file = self._save_single_json(daily_news, filename_base, date_str)
                md_file = self._save_single_markdown(daily_news, filename_base, date_str)
                txt_file = self._save_single_txt(daily_news, filename_base)
                
                all_files.extend([json_file, md_file, txt_file])
                print()
        
        return all_files
    
    def _save_single_json(self, news_list: List[Dict], filename_base: str, date_str: str) -> str:
        """保存单个JSON文件"""
        sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
        
        data = {
            'date': date_str,
            'crawl_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'crawl_method': 'scroll',
            'total': len(news_list),
            'time_range': {
                'start': sorted_news[0].get('datetime', '') if news_list else '',
                'end': sorted_news[-1].get('datetime', '') if news_list else ''
            },
            'news': news_list
        }
        
        filename = os.path.join(self.save_dir, 'raw', f'{filename_base}.json')
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return filename
    
    def _save_single_markdown(self, news_list: List[Dict], filename_base: str, date_str: str) -> str:
        """保存单个Markdown文件"""
        lines = []
        lines.append(f"# {date_str} 财经新闻汇总\n")
        
        # 统计信息
        lines.append("## 📊 统计信息\n")
        lines.append(f"- **总计**: {len(news_list)} 条新闻")
        lines.append(f"- **抓取时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- **抓取方式**: 滚动加载\n")
        
        # 时间跨度
        sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0))
        start_time = sorted_news[0].get('datetime', '')
        end_time = sorted_news[-1].get('datetime', '')
        if start_time and end_time:
            lines.append(f"- **时间范围**: {start_time} ~ {end_time}")
            
            start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
            end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
            hours = (end_dt - start_dt).total_seconds() / 3600
            lines.append(f"- **时间跨度**: {hours:.1f} 小时\n")
        
        # 来源统计
        source_count = {}
        for news in news_list:
            source = news.get('source', '未知')
            if source:
                source_count[source] = source_count.get(source, 0) + 1
        
        if source_count:
            lines.append("### 📰 新闻来源分布\n")
            sorted_sources = sorted(source_count.items(), key=lambda x: x[1], reverse=True)
            for source, count in sorted_sources[:10]:
                percentage = (count / len(news_list) * 100) if len(news_list) > 0 else 0
                lines.append(f"- **{source}**: {count} 条 ({percentage:.1f}%)")
            lines.append("")
        
        lines.append("---\n")
        lines.append("## 📰 新闻详情\n")
        
        # 按时间倒序
        sorted_news_desc = sorted(news_list, key=lambda x: x.get('timestamp', 0), reverse=True)
        
        for i, news in enumerate(sorted_news_desc, 1):
            lines.append(f"### {i}. {news['title']}\n")
            lines.append(f"**时间**: {news['datetime']} | **来源**: {news['source']}\n")
            
            if news['content']:
                content = news['content']
                if len(content) > 500:
                    content = content[:500] + "..."
                lines.append(f"{content}\n")
            
            lines.append("---\n")
        
        lines.append(f"\n> 数据来源: [鼓掌网]({self.base_url})")
        lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        md_content = "\n".join(lines)
        
        filename = os.path.join(self.save_dir, 'summaries', f'{filename_base}_summary.md')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        return filename
    
    def _save_single_txt(self, news_list: List[Dict], filename_base: str) -> str:
        """保存单个TXT文件"""
        lines = []
        lines.append(f"=== {filename_base} ===")
        lines.append(f"总计: {len(news_list)} 条")
        lines.append("")
        
        # 按时间倒序
        sorted_news = sorted(news_list, key=lambda x: x.get('timestamp', 0), reverse=True)
        
        for news in sorted_news:
            time_str = news['datetime'].split()[1] if news.get('datetime') else ''
            title = news.get('title', '')
            if time_str and title:
                lines.append(f"{time_str} | {title}")
        
        txt_content = "\n".join(lines)
        
        filename = os.path.join(self.save_dir, 'summaries', f'{filename_base}_titles.txt')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(txt_content)
        
        return filename
    
    def crawl(
        self,
        scroll_times=36,
        wait_seconds=6,
        max_no_change=3,
        headless=True,
    ) -> Dict:
        """
        执行爬取
        
        Args:
            scroll_times: 滚动次数
            wait_seconds: 每次滚动后等待秒数
            max_no_change: 连续无新数据停止次数
            headless: 是否无头模式
        """
        print("\n" + "=" * 60)
        print("鼓掌网财经新闻爬虫 - 滚动加载版")
        print(f"滚动次数: {scroll_times} 次")
        print(f"等待时间: {wait_seconds} 秒/次")
        print("=" * 60)
        
        start_time = time.time()
        
        # 获取页面
        html = self.fetch_page_with_scroll(
            scroll_times, wait_seconds, max_no_change, headless=headless
        )
        
        if not html:
            print("\n✗ 爬取失败")
            return {'success': False, 'error': '无法获取页面'}
        
        # 解析新闻
        news_list = self.parse_news(html)
        
        if not news_list:
            print("\n✗ 未解析到新闻")
            return {'success': False, 'error': '未解析到新闻'}
        
        # 统计跨天信息（仅用于日志显示）
        news_by_date = self.split_news_by_date(news_list)
        
        # 保存数据（所有新闻保存为单个JSON文件，不分割）
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 保存所有新闻到单个文件...\n")
        json_file = self.save_as_json(news_list)
        all_files = [json_file]
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 已保存: {len(news_list)} 条（跨 {len(news_by_date)} 天）")
        
        elapsed = time.time() - start_time
        
        print("\n" + "=" * 60)
        print(f"✓ 爬取完成！")
        print(f"  - 新闻总数: {len(news_list)} 条")
        print(f"  - 跨天数: {len(news_by_date)} 天")
        print(f"  - 总耗时: {elapsed:.2f} 秒")
        print(f"  - 平均速度: {len(news_list)/elapsed:.1f} 条/秒")
        print(f"  - 保存文件数: {len(all_files)} 个")
        print("=" * 60 + "\n")
        
        return {
            'success': True, 
            'total': len(news_list), 
            'days': len(news_by_date),
            'files': all_files,
            'elapsed': elapsed
        }


class NewsCrawler:
    """
    爬虫包装类，支持进度回调
    """
    
    def __init__(self):
        self.progress_callback: Optional[Callable] = None
        self.crawler = None
        self.headless = True
        
        # 增量爬取参数
        self.auto_stop_enabled = False
        self.db_latest_time = None
        self.db_latest_title = None

    def request_stop(self):
        """请求停止爬虫"""
        if self.crawler is not None:
            self.crawler.request_stop()
    
    def set_progress_callback(self, callback: Callable):
        """设置进度回调函数"""
        self.progress_callback = callback
    
    def set_auto_stop(self, enabled: bool, latest_time=None, latest_title=None):
        """
        设置自动停止参数
        
        Args:
            enabled: 是否启用自动停止
            latest_time: 数据库最新新闻时间（datetime对象）
            latest_title: 数据库最新新闻标题
        """
        self.auto_stop_enabled = enabled
        self.db_latest_time = latest_time
        self.db_latest_title = latest_title
    
    def run(self, scroll_times=36, wait_seconds=6, headless=True, max_no_change=3):
        """
        运行爬虫
        
        Args:
            scroll_times: 滚动次数
            max_no_change: 连续无新数据停止次数
            wait_seconds: 等待时间
            headless: 无头模式
            
        Returns:
            生成的文件列表
        """
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        try:
            from core.config import CRAWLER_CONFIG
            chrome_path = CRAWLER_CONFIG.get(
                'chrome_path', os.path.join(base_dir, 'chrome-win64', 'chrome.exe')
            )
            chromedriver_path = CRAWLER_CONFIG.get(
                'chromedriver_path',
                os.path.join(base_dir, 'chrome-win64', 'chromedriver.exe'),
            )
        except Exception as e:
            print(f"警告: 读取配置失败 ({e})，使用默认路径")
            chrome_path = os.path.join(base_dir, 'chrome-win64', 'chrome.exe')
            chromedriver_path = os.path.join(base_dir, 'chrome-win64', 'chromedriver.exe')
        
        # 创建爬虫实例
        self.crawler = GuZhangNewsCrawlerScroll(
            chrome_path=chrome_path,
            chromedriver_path=chromedriver_path
        )
        
        # 设置进度回调
        if self.progress_callback:
            self.crawler.set_progress_callback(self.progress_callback)
        
        # 设置自动停止
        if self.auto_stop_enabled:
            self.crawler.set_auto_stop(self.auto_stop_enabled, self.db_latest_time, self.db_latest_title)
        
        self.crawler.headless = headless
        result = self.crawler.crawl(
            scroll_times=scroll_times,
            wait_seconds=wait_seconds,
            max_no_change=max_no_change,
            headless=headless,
        )
        
        if result['success']:
            return result.get('files', [])
        else:
            raise Exception(result.get('error', '爬取失败'))


def main(scroll_times=36, wait_seconds=6, headless=True):
    """
    主函数，供外部调用
    
    Args:
        scroll_times: 滚动次数
        wait_seconds: 等待时间
        headless: 无头模式
        
    Returns:
        生成的文件列表
    """
    crawler = NewsCrawler()
    return crawler.run(scroll_times=scroll_times, wait_seconds=wait_seconds, headless=headless)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='使用滚动加载的爬虫')
    parser.add_argument('--times', type=int, default=36, 
                       help='滚动次数（默认36次）')
    parser.add_argument('--wait', type=int, default=6,
                       help='每次滚动后等待秒数（默认6秒）')
    _base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    _default_chrome = os.path.join(_base, 'chrome-win64', 'chrome.exe')
    _default_driver = os.path.join(_base, 'chrome-win64', 'chromedriver.exe')
    parser.add_argument('--chrome', type=str,
                       default=_default_chrome,
                       help='Chrome浏览器路径')
    parser.add_argument('--driver', type=str,
                       default=_default_driver,
                       help='ChromeDriver路径')
    
    args = parser.parse_args()
    
    print("\n" + "=" * 60)
    print("配置信息:")
    print(f"  Chrome路径: {args.chrome}")
    print(f"  ChromeDriver路径: {args.driver}")
    print(f"  滚动次数: {args.times} 次")
    print(f"  等待时间: {args.wait} 秒")
    print("=" * 60)
    
    # 执行爬取
    try:
        files = main(scroll_times=args.times, wait_seconds=args.wait)
        print("\n🎉 成功！")
        print(f"生成文件: {files}")
    except Exception as e:
        print(f"\n❌ 失败: {str(e)}")

