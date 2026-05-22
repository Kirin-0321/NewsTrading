"""
测试新闻来源字段抓取
诊断为什么 source 字段为空
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_with_requests():
    """使用 requests 直接获取页面测试"""
    print("=" * 60)
    print("测试1: 使用 requests 直接获取")
    print("=" * 60)
    
    url = "https://724.guzhang.com/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 查找新闻项
        news_items = soup.find_all('li', class_='recent-news-item')
        print(f"找到 {len(news_items)} 条新闻\n")
        
        if news_items:
            # 分析第一条新闻的HTML结构
            first_item = news_items[0]
            print("第一条新闻的完整HTML结构:")
            print("-" * 40)
            print(first_item.prettify()[:2000])
            print("-" * 40)
            
            # 尝试各种可能的来源选择器
            print("\n尝试不同的来源选择器:")
            selectors = [
                ('span', 'source-name'),
                ('span', 'source'),
                ('div', 'source'),
                ('span', 'news-source'),
                ('a', 'source'),
                ('span', 'from'),
            ]
            
            for tag, class_name in selectors:
                elem = first_item.find(tag, class_=class_name)
                if elem:
                    print(f"  ✓ {tag}.{class_name}: '{elem.text.strip()}'")
                else:
                    print(f"  ✗ {tag}.{class_name}: 未找到")
            
            # 查找所有span标签
            print("\n所有span标签:")
            for span in first_item.find_all('span'):
                classes = span.get('class', [])
                text = span.text.strip()[:50]
                print(f"  - class={classes}: '{text}'")
                
    except Exception as e:
        print(f"❌ 请求失败: {e}")


def test_with_selenium():
    """使用 Selenium 获取动态加载后的页面测试"""
    print("\n" + "=" * 60)
    print("测试2: 使用 Selenium 获取动态内容")
    print("=" * 60)
    
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        import time
    except ImportError:
        print("❌ Selenium 未安装")
        return
    
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    chrome_path = os.path.join(base_dir, 'chrome-win64', 'chrome.exe')
    chromedriver_path = os.path.join(base_dir, 'chrome-win64', 'chromedriver.exe')
    
    if not os.path.exists(chrome_path):
        print(f"❌ Chrome 不存在: {chrome_path}")
        return
    
    options = Options()
    options.binary_location = chrome_path
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1920,1080')
    
    service = Service(chromedriver_path)
    driver = None
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.get("https://724.guzhang.com/")
        time.sleep(5)  # 等待页面加载
        
        html = driver.page_source
        soup = BeautifulSoup(html, 'html.parser')
        
        news_items = soup.find_all('li', class_='recent-news-item')
        print(f"找到 {len(news_items)} 条新闻\n")
        
        if news_items:
            # 分析前3条新闻
            for idx, item in enumerate(news_items[:3]):
                print(f"\n--- 新闻 {idx + 1} ---")
                
                # 标题
                title_elem = item.find('h2')
                title = title_elem.text.strip() if title_elem else "无标题"
                print(f"标题: {title[:60]}...")
                
                # 尝试获取来源 - 当前方式
                source_elem = item.find('span', class_='source-name')
                source = source_elem.text.strip() if source_elem else ''
                print(f"source-name: '{source}'")
                
                # 查找所有可能包含来源的元素
                print("所有span标签内容:")
                for span in item.find_all('span'):
                    classes = span.get('class', [])
                    text = span.text.strip()
                    if text and len(text) < 50:
                        print(f"  class={classes}: '{text}'")
                
                # 查找data属性
                print("带有data-*属性的元素:")
                for elem in item.find_all(attrs={"data-source": True}):
                    print(f"  data-source: '{elem.get('data-source')}'")
                
                # 输出完整HTML以便分析
                print("\n完整HTML (前1500字符):")
                print(item.prettify()[:1500])
                
    except Exception as e:
        print(f"❌ Selenium 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()


def analyze_existing_data():
    """分析 SQLite 原始库中的来源字段"""
    print("\n" + "=" * 60)
    print("测试3: 分析 SQLite 原始库")
    print("=" * 60)

    try:
        from services.storage import get_raw_store

        store = get_raw_store()
        news_list = store.get_all_news()[:500]
        if not news_list:
            print("❌ 原始库为空")
            return

        print(f"抽样 {len(news_list)} 条新闻")
    except Exception as e:
        print(f"❌ 读取 SQLite 失败: {e}")
        return
    
    # 统计来源情况
    source_count = {'有来源': 0, '无来源': 0}
    source_examples = {}
    
    for news in news_list:
        source = news.get('source', '')
        if source:
            source_count['有来源'] += 1
            source_examples[source] = source_examples.get(source, 0) + 1
        else:
            source_count['无来源'] += 1
    
    print(f"\n来源统计:")
    print(f"  - 有来源: {source_count['有来源']} 条")
    print(f"  - 无来源: {source_count['无来源']} 条")
    
    if source_examples:
        print(f"\n来源分布 (前10):")
        sorted_sources = sorted(source_examples.items(), 
                                key=lambda x: x[1], reverse=True)
        for src, cnt in sorted_sources[:10]:
            print(f"  - {src}: {cnt} 条")
    else:
        print("\n⚠️ 所有新闻的来源字段都为空!")
        print("   可能原因:")
        print("   1. 网站HTML结构中没有 span.source-name 元素")
        print("   2. 来源信息使用了其他class名称")
        print("   3. 来源信息是通过JavaScript动态加载的")


def verify_fix():
    """验证修复是否生效"""
    print("\n" + "=" * 60)
    print("验证修复: 使用 span.from 选择器")
    print("=" * 60)
    
    url = "https://724.guzhang.com/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        news_items = soup.find_all('li', class_='recent-news-item')
        print(f"找到 {len(news_items)} 条新闻\n")
        
        success_count = 0
        for i, item in enumerate(news_items[:10]):
            title_elem = item.find('h2')
            title = title_elem.text.strip()[:40] if title_elem else "无标题"
            
            # 使用修复后的选择器
            source_elem = item.find('span', class_='from')
            source = source_elem.text.strip() if source_elem else ''
            
            status = "✓" if source else "✗"
            if source:
                success_count += 1
            print(f"  {status} [{source or '空'}] {title}...")
        
        print(f"\n成功率: {success_count}/{min(10, len(news_items))} " +
              f"({success_count/min(10, len(news_items))*100:.0f}%)")
        
        if success_count == min(10, len(news_items)):
            print("✅ 修复成功！新采集的数据将包含来源信息")
        else:
            print("⚠️ 部分新闻仍无来源，可能是网站数据本身缺失")
            
    except Exception as e:
        print(f"❌ 验证失败: {e}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--verify':
        # 只运行验证
        verify_fix()
    else:
        print("新闻来源字段诊断工具")
        print("=" * 60 + "\n")
        
        # 先分析现有数据
        analyze_existing_data()
        
        # 使用requests测试
        test_with_requests()
        
        # 使用selenium测试
        test_with_selenium()
        
        # 验证修复
        verify_fix()
        
        print("\n" + "=" * 60)
        print("诊断完成")
        print("=" * 60)

