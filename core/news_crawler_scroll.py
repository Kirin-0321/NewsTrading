"""
鼓掌网新闻爬虫（Selenium 滚动加载）

职责：
    浏览器启动 → 滚动加载 → 解析 → 返回内存中的新闻列表。

持久化（SQLite 入库、去重）由 services.crawl_sync_service 完成，
本模块不再生成 JSON/Markdown/TXT 文件。
"""

import os
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from bs4 import BeautifulSoup

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("⚠ Selenium未安装: pip install selenium")


class GuZhangNewsCrawlerScroll:
    """
    底层爬虫：负责浏览器滚动加载与新闻解析。

    输入：scroll_times / wait_seconds / max_no_change / headless
    输出：crawl() 返回 {success, total, news_list, elapsed}
    """

    def __init__(self, chrome_path=None, chromedriver_path=None):
        """
        Args:
            chrome_path: Chrome 可执行文件路径，None 时自动按打包/开发模式定位
            chromedriver_path: ChromeDriver 路径，同上
        """
        self.base_url = "https://724.guzhang.com/"

        if chrome_path is None or chromedriver_path is None:
            chrome_path, chromedriver_path = self._get_chrome_paths()
        self.chrome_path = chrome_path
        self.chromedriver_path = chromedriver_path

        self.progress_callback: Optional[Callable] = None

        # 增量爬取
        self.auto_stop_enabled = False
        self.db_latest_time: Optional[datetime] = None
        self.db_latest_title: Optional[str] = None

        # 运行时
        self.headless = True
        self._stop_requested = False
        self._driver = None

        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36'
            ),
        }

        if os.path.exists(self.chrome_path):
            print("✓ 找到Chrome浏览器")
        else:
            print(f"⚠ Chrome浏览器不存在: {self.chrome_path}")
        if os.path.exists(self.chromedriver_path):
            print("✓ 找到ChromeDriver")
        else:
            print(f"⚠ ChromeDriver不存在: {self.chromedriver_path}")

    # ----------------------- 外部接口 -----------------------

    def set_progress_callback(self, callback: Callable) -> None:
        """设置进度回调：callback(current, total, news_count, message)"""
        self.progress_callback = callback

    def request_stop(self) -> None:
        """协作式停止：当前滚动结束后退出并关闭浏览器。"""
        self._stop_requested = True
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def set_auto_stop(
        self,
        enabled: bool,
        latest_time: Optional[datetime] = None,
        latest_title: Optional[str] = None,
    ) -> None:
        """
        增量爬取：遇到已有最新新闻（标题匹配或时间到达）时自动停止。

        Args:
            enabled: 是否启用
            latest_time: 数据库最新新闻的发布时间
            latest_title: 数据库最新新闻的标题
        """
        self.auto_stop_enabled = enabled
        self.db_latest_time = latest_time
        self.db_latest_title = latest_title

    # ----------------------- 内部工具 -----------------------

    @staticmethod
    def _get_chrome_paths():
        """智能定位 Chrome / ChromeDriver（兼容 PyInstaller 打包）。"""
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            chrome_dir = os.path.join(exe_dir, 'chrome-win64')
        else:
            chrome_dir = os.path.join(os.getcwd(), 'chrome-win64')

        chrome_path = os.path.join(chrome_dir, 'chrome.exe')
        chromedriver_path = os.path.join(chrome_dir, 'chromedriver.exe')

        if not os.path.exists(chrome_path):
            print(f"⚠ Chrome未找到: {chrome_path}")
        if not os.path.exists(chromedriver_path):
            print(f"⚠ ChromeDriver未找到: {chromedriver_path}")
        return chrome_path, chromedriver_path

    def _report_progress(self, current, total, news_count, message=''):
        if self.progress_callback:
            self.progress_callback(current, total, news_count, message)

    # ----------------------- 滚动加载 -----------------------

    def fetch_page_with_scroll(
        self,
        scroll_times: int = 36,
        wait_seconds: int = 6,
        max_no_change: int = 3,
        headless: bool = True,
    ) -> Optional[str]:
        """
        滚动加载页面，返回完整 HTML（失败返回 None）。

        Args:
            scroll_times: 最大滚动次数
            wait_seconds: 每次滚动后等待秒数
            max_no_change: 连续 N 次无新内容则提前结束
            headless: 是否无头模式
        """
        self.headless = headless
        self._stop_requested = False

        print(f"\n{'='*60}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 准备启动浏览器...")
        print(f"{'='*60}")

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

            time.sleep(3)

            initial_soup = BeautifulSoup(driver.page_source, 'html.parser')
            initial_count = len(initial_soup.find_all('li', class_='recent-news-item'))
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                  f"初始新闻数量: {initial_count} 条")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始滚动加载...")
            print(f"目标滚动次数: {scroll_times} 次")
            print(f"每次等待时间: {wait_seconds} 秒\n")

            last_count = initial_count
            no_change_count = 0
            i = 0

            for i in range(scroll_times):
                if self._stop_requested:
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                          f"⚠ 收到停止请求，结束滚动")
                    break

                driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
                time.sleep(wait_seconds)

                current_soup = BeautifulSoup(driver.page_source, 'html.parser')
                news_items_list = current_soup.find_all(
                    'li', class_='recent-news-item'
                )
                current_count = len(news_items_list)
                new_items = current_count - last_count

                oldest_time_str = ""
                oldest_timestamp = None
                should_stop = False

                if news_items_list:
                    last_item = news_items_list[-1]
                    ptime_elem = last_item.find('span', class_='ptime')
                    if ptime_elem:
                        try:
                            oldest_timestamp = int(ptime_elem.text)
                            oldest_dt = datetime.fromtimestamp(oldest_timestamp)
                            oldest_time_str = oldest_dt.strftime(
                                '%Y-%m-%d %H:%M:%S'
                            )
                        except (TypeError, ValueError):
                            oldest_time_str = ""

                    if self.auto_stop_enabled and self.db_latest_time:
                        # 标题匹配
                        if self.db_latest_title:
                            for item in news_items_list:
                                title_elem = item.find('h2')
                                if title_elem and title_elem.text.strip() == self.db_latest_title:
                                    print(
                                        f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                                        f"⚠ [自动停止] 遇到已有新闻（标题匹配）"
                                    )
                                    print(f"  标题: {self.db_latest_title}")
                                    should_stop = True
                                    break

                        # 时间边界
                        if not should_stop and oldest_timestamp:
                            db_ts = int(self.db_latest_time.timestamp())
                            if oldest_timestamp <= db_ts:
                                print(
                                    f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                                    f"⚠ [自动停止] 时间到达边界"
                                )
                                print(f"  当前最远: {oldest_time_str}")
                                print(
                                    f"  数据库最新: "
                                    f"{self.db_latest_time.strftime('%Y-%m-%d %H:%M:%S')}"
                                )
                                should_stop = True

                msg = (
                    f"第 {i+1}/{scroll_times} 次滚动 | "
                    f"新增: +{new_items} | 总计: {current_count}"
                )
                if oldest_time_str:
                    msg += f" | 最远: {oldest_time_str}"
                self._report_progress(i + 1, scroll_times, current_count, msg)

                if should_stop:
                    break

                if new_items > 0:
                    console_msg = (
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"第 {i+1}/{scroll_times} 次滚动 | 新增: +{new_items} 条 | "
                        f"总计: {current_count} 条"
                    )
                    if oldest_time_str:
                        console_msg += f" | 最远: {oldest_time_str}"
                    console_msg += " ✓"
                    print(console_msg)
                    last_count = current_count
                    no_change_count = 0
                else:
                    no_change_count += 1
                    console_msg = (
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"第 {i+1}/{scroll_times} 次滚动 | 无新增 | "
                        f"总计: {current_count} 条"
                    )
                    if oldest_time_str:
                        console_msg += f" | 最远: {oldest_time_str}"
                    console_msg += " ⚠"
                    print(console_msg)
                    if no_change_count >= max_no_change:
                        print(
                            f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                            f"⚠ 连续{no_change_count}次未加载新内容，停止滚动"
                        )
                        break

            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')
            news_items = soup.find_all('li', class_='recent-news-item')

            print(f"\n{'='*60}")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 滚动加载完成！")
            print(f"  - 实际滚动次数: {i+1} 次")
            print(f"  - 初始新闻: {initial_count} 条")
            print(f"  - 最终新闻: {len(news_items)} 条")
            print(f"  - 新增新闻: {len(news_items) - initial_count} 条")
            print(f"{'='*60}\n")

            return html

        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✗ 出错: {e}")
            return None
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            self._driver = None

    # ----------------------- 解析 -----------------------

    def parse_news(self, html: str) -> List[Dict]:
        """
        解析 HTML 为新闻列表，并按 id/标题做基础去重。

        返回每条字段：id / timestamp / datetime / title / content / source
        """
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始解析新闻数据...")

        soup = BeautifulSoup(html, 'html.parser')
        news_items = soup.find_all('li', class_='recent-news-item')

        raw_list: List[Dict] = []
        for item in news_items:
            try:
                news = self._parse_single_news(item)
                if news:
                    raw_list.append(news)
            except Exception:
                continue

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 原始数量: {len(raw_list)} 条")

        seen_ids = set()
        seen_titles = set()
        unique_news: List[Dict] = []
        duplicates = 0

        for news in raw_list:
            nid = news.get('id', '')
            title = (news.get('title') or '').strip()
            if not title:
                duplicates += 1
                continue
            if (nid and nid in seen_ids) or title in seen_titles:
                duplicates += 1
                continue
            if nid:
                seen_ids.add(nid)
            seen_titles.add(title)
            unique_news.append(news)

        if duplicates > 0:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"去除重复: {duplicates} 条（{len(unique_news)} 条有效）"
            )

        if unique_news:
            sorted_news = sorted(unique_news, key=lambda x: x.get('timestamp', 0))
            start_time = sorted_news[0].get('datetime', '')
            end_time = sorted_news[-1].get('datetime', '')
            if start_time and end_time:
                try:
                    start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                    end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
                    hours = (end_dt - start_dt).total_seconds() / 3600
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] ✓ 解析完成: "
                        f"{len(unique_news)} 条 | 跨度 {hours:.1f} 小时 | "
                        f"{start_time} ~ {end_time}"
                    )
                except ValueError:
                    pass

        return unique_news

    @staticmethod
    def _parse_single_news(item) -> Dict:
        """解析单个新闻 li 节点。"""
        news_id = item.get('data-aid', '')

        ptime_elem = item.find('span', class_='ptime')
        timestamp = int(ptime_elem.text) if ptime_elem else 0

        title_elem = item.find('h2')
        title = title_elem.text.strip() if title_elem else ''

        content = ''
        content_elem = item.find('div', class_='news-content')
        if content_elem:
            p_elem = content_elem.find('p')
            content = p_elem.text.strip() if p_elem else ''

        # 网站使用 span.from 作为来源
        source_elem = item.find('span', class_='from')
        source = source_elem.text.strip() if source_elem else ''

        datetime_str = (
            datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
            if timestamp else ''
        )

        return {
            'id': news_id,
            'timestamp': timestamp,
            'datetime': datetime_str,
            'title': title,
            'content': content,
            'source': source,
        }

    # ----------------------- 入口 -----------------------

    def crawl(
        self,
        scroll_times: int = 36,
        wait_seconds: int = 6,
        max_no_change: int = 3,
        headless: bool = True,
    ) -> Dict:
        """
        执行一次爬取。

        Returns:
            成功: {success: True, total, news_list, elapsed}
            失败: {success: False, error: str}
        """
        print("\n" + "=" * 60)
        print("鼓掌网财经新闻爬虫 - 滚动加载版")
        print(f"滚动次数: {scroll_times} 次")
        print(f"等待时间: {wait_seconds} 秒/次")
        print("=" * 60)

        t0 = time.time()
        html = self.fetch_page_with_scroll(
            scroll_times=scroll_times,
            wait_seconds=wait_seconds,
            max_no_change=max_no_change,
            headless=headless,
        )
        if not html:
            print("\n✗ 爬取失败")
            return {'success': False, 'error': '无法获取页面'}

        news_list = self.parse_news(html)
        if not news_list:
            print("\n✗ 未解析到新闻")
            return {'success': False, 'error': '未解析到新闻'}

        elapsed = time.time() - t0

        print("\n" + "=" * 60)
        print("✓ 爬取完成！")
        print(f"  - 新闻总数: {len(news_list)} 条")
        print(f"  - 总耗时: {elapsed:.2f} 秒")
        print(f"  - 平均速度: {len(news_list) / elapsed:.1f} 条/秒")
        print("=" * 60 + "\n")

        return {
            'success': True,
            'total': len(news_list),
            'news_list': news_list,
            'elapsed': elapsed,
        }


class NewsCrawler:
    """
    服务层使用的爬虫包装：负责定位 Chrome 路径、转发进度与 auto_stop。
    被 services.crawl_sync_service 调用。
    """

    def __init__(self):
        self.progress_callback: Optional[Callable] = None
        self.crawler: Optional[GuZhangNewsCrawlerScroll] = None

        self.auto_stop_enabled = False
        self.db_latest_time: Optional[datetime] = None
        self.db_latest_title: Optional[str] = None

    def set_progress_callback(self, callback: Callable) -> None:
        self.progress_callback = callback

    def request_stop(self) -> None:
        if self.crawler is not None:
            self.crawler.request_stop()

    def set_auto_stop(
        self,
        enabled: bool,
        latest_time: Optional[datetime] = None,
        latest_title: Optional[str] = None,
    ) -> None:
        self.auto_stop_enabled = enabled
        self.db_latest_time = latest_time
        self.db_latest_title = latest_title

    def run(
        self,
        scroll_times: int = 36,
        wait_seconds: int = 6,
        headless: bool = True,
        max_no_change: int = 3,
    ) -> Dict:
        """
        执行爬取，返回 GuZhangNewsCrawlerScroll.crawl() 的字典。

        失败时抛出异常（兼容旧调用方式）。
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
            chromedriver_path = os.path.join(
                base_dir, 'chrome-win64', 'chromedriver.exe'
            )

        self.crawler = GuZhangNewsCrawlerScroll(
            chrome_path=chrome_path,
            chromedriver_path=chromedriver_path,
        )

        if self.progress_callback:
            self.crawler.set_progress_callback(self.progress_callback)
        if self.auto_stop_enabled:
            self.crawler.set_auto_stop(
                self.auto_stop_enabled,
                self.db_latest_time,
                self.db_latest_title,
            )

        result = self.crawler.crawl(
            scroll_times=scroll_times,
            wait_seconds=wait_seconds,
            max_no_change=max_no_change,
            headless=headless,
        )

        if result['success']:
            return result
        raise Exception(result.get('error', '爬取失败'))


def main(scroll_times: int = 36, wait_seconds: int = 6, headless: bool = True) -> Dict:
    """CLI / 外部脚本入口。"""
    return NewsCrawler().run(
        scroll_times=scroll_times,
        wait_seconds=wait_seconds,
        headless=headless,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='鼓掌网滚动加载爬虫')
    parser.add_argument('--times', type=int, default=36, help='滚动次数（默认 36）')
    parser.add_argument('--wait', type=int, default=6, help='每次滚动等待秒数（默认 6）')
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("配置信息:")
    print(f"  滚动次数: {args.times} 次")
    print(f"  等待时间: {args.wait} 秒")
    print("=" * 60)

    try:
        out = main(scroll_times=args.times, wait_seconds=args.wait)
        print(f"\n🎉 成功！爬取 {out.get('total', 0)} 条新闻")
    except Exception as exc:
        print(f"\n❌ 失败: {exc}")
