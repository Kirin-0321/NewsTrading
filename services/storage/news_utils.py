"""新闻字段归一化与时间解析。"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# raw_news.clean_status：未清洗 / 精选 / 剔除
CLEAN_PENDING = "pending"
CLEAN_CURATED = "curated"
CLEAN_REJECTED = "rejected"


def news_item_id(news: Dict[str, Any]) -> str:
    """生成稳定新闻 ID；优先使用源站 id。"""
    nid = str(news.get("id", "")).strip()
    if nid:
        return nid
    title = (news.get("title") or "").strip()
    dt = (news.get("datetime") or news.get("time") or "").strip()
    raw = f"{title}|{dt}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def parse_news_time(news: Dict[str, Any]) -> Optional[datetime]:
    """解析新闻发布时间。"""
    time_str = news.get("datetime") or news.get("time", "")
    if not time_str:
        ts = news.get("timestamp")
        if ts:
            try:
                return datetime.fromtimestamp(int(ts))
            except (TypeError, ValueError, OSError):
                return None
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def published_fields(news: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """返回 (published_at ISO 字符串, published_ts)。"""
    dt = parse_news_time(news)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M:%S"), int(dt.timestamp())
    ts = news.get("timestamp")
    if ts:
        try:
            d = datetime.fromtimestamp(int(ts))
            return d.strftime("%Y-%m-%d %H:%M:%S"), int(ts)
        except (TypeError, ValueError, OSError):
            pass
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S"), int(now.timestamp())


def normalize_news(news: Dict[str, Any]) -> Dict[str, Any]:
    """补全 id、datetime、timestamp 等字段。"""
    item = dict(news)
    item["id"] = news_item_id(item)
    pub_at, pub_ts = published_fields(item)
    item.setdefault("datetime", pub_at)
    item.setdefault("timestamp", pub_ts)
    return item


def news_to_row(news: Dict[str, Any], crawled_at: Optional[str] = None) -> Dict[str, Any]:
    """将新闻 dict 转为数据库行字段。"""
    item = normalize_news(news)
    pub_at, pub_ts = published_fields(item)
    crawled = crawled_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    known = {
        "id", "title", "content", "source", "datetime", "time", "timestamp",
        "clean_status", "clean_reason",
    }
    extra = {k: v for k, v in item.items() if k not in known}

    return {
        "id": item["id"],
        "title": item.get("title") or "",
        "content": item.get("content") or "",
        "source": item.get("source") or "",
        "published_at": pub_at,
        "published_ts": pub_ts,
        "crawled_at": crawled,
        "clean_status": item.get("clean_status") or CLEAN_PENDING,
        "clean_reason": item.get("clean_reason"),
        "extra_json": json.dumps(extra, ensure_ascii=False) if extra else None,
    }


def row_to_news(row) -> Dict[str, Any]:
    """将 sqlite Row 转为业务 dict。"""
    news = {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"] or "",
        "source": row["source"] or "",
        "datetime": row["published_at"],
        "timestamp": row["published_ts"],
    }
    if "clean_status" in row.keys():
        news["clean_status"] = row["clean_status"] or CLEAN_PENDING
        news["clean_reason"] = row["clean_reason"] or ""
        if news["clean_status"] == CLEAN_REJECTED:
            news["reason"] = news["clean_reason"]
    if row["extra_json"]:
        try:
            news.update(json.loads(row["extra_json"]))
        except json.JSONDecodeError:
            pass
    return news
