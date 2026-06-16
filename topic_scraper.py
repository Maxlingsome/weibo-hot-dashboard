#!/usr/bin/env python3
"""
微博话题详情 + 趋势数据抓取模块
数据来源: m.s.weibo.com/ajax_topic/detail + /ajax_topic/trend（无需登录）
"""
import json
import time
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://m.s.weibo.com/",
}


def fetch_topic_detail(topic_name):
    """
    GET m.s.weibo.com/ajax_topic/detail?q=%23话题%23&show_rank_info=1
    返回 parsed data dict，失败返回 None
    """
    encoded = urllib.request.quote(f"#{topic_name}#")
    url = f"https://m.s.weibo.com/ajax_topic/detail?q={encoded}&show_rank_info=1"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if str(data.get("code")) != "100000":
            return None
        return data.get("data", {})
    except Exception:
        return None


def fetch_topic_trend(topic_name):
    """
    GET m.s.weibo.com/ajax_topic/trend?q=%23话题%23
    返回 parsed data dict（含 read/me/ori/partake 四条时序），失败返回 None
    """
    encoded = urllib.request.quote(f"#{topic_name}#")
    url = f"https://m.s.weibo.com/ajax_topic/trend?q={encoded}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if str(data.get("code")) != "100000":
            return None
        return data.get("data", {})
    except Exception:
        return None


def store_topic_detail(db_path, topic_name, rank, detail_data):
    """解析 detail API 返回数据，写入 topic_detail 表"""
    db = sqlite3.connect(db_path)
    try:
        base_info = detail_data.get("baseInfo", {})
        count = base_info.get("count", {})
        base_data = detail_data.get("baseData", {})

        read_count = count.get("read", 0) or 0
        mention_count = count.get("mention", 0) or 0
        interact_count = count.get("interact", 0) or 0
        ori_count = count.get("ori_m", 0) or 0
        sum_24h = json.dumps(base_data.get("sum_24h", {}), ensure_ascii=False)
        sum_30d = json.dumps(base_data.get("sum_30d", {}), ensure_ascii=False)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        db.execute(
            """INSERT INTO topic_detail(topic_name, scrape_time, read_count,
               mention_count, interact_count, ori_count, sum_24h, sum_30d, current_rank)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (topic_name, now, read_count, mention_count, interact_count,
             ori_count, sum_24h, sum_30d, rank)
        )
        db.commit()
    except Exception as e:
        print(f"  [详情入库] {topic_name}: {e}")
    finally:
        db.close()


def store_topic_trend(db_path, topic_name, trend_data):
    """解析 trend API 返回数据，按小时去重写入 topic_trend 表"""
    current_hour = time.strftime("%Y-%m-%d %H:00")
    db = sqlite3.connect(db_path)
    try:
        for trend_type in ["read", "me", "ori", "partake"]:
            points = trend_data.get(trend_type, [])
            if not points:
                continue
            data_json = json.dumps(points, ensure_ascii=False)
            db.execute(
                """INSERT OR REPLACE INTO topic_trend(topic_name, trend_type, scrape_hour, data_json)
                   VALUES(?,?,?,?)""",
                (topic_name, trend_type, current_hour, data_json)
            )
        db.commit()
    except Exception as e:
        print(f"  [趋势入库] {topic_name}: {e}")
    finally:
        db.close()


def scrape_single_topic(topic_name, rank, db_path):
    """抓取单个话题的 detail + trend 并存库。返回 (topic_name, success)"""
    success = False
    try:
        # 1. 详情
        detail = fetch_topic_detail(topic_name)
        if detail:
            store_topic_detail(db_path, topic_name, rank, detail)
            success = True

        # 2. 趋势（当前小时已有则跳过）
        current_hour = time.strftime("%Y-%m-%d %H:00")
        db = sqlite3.connect(db_path)
        already_has = db.execute(
            "SELECT 1 FROM topic_trend WHERE topic_name=? AND scrape_hour=? LIMIT 1",
            (topic_name, current_hour)
        ).fetchone() is not None
        db.close()

        if not already_has:
            trend = fetch_topic_trend(topic_name)
            if trend:
                store_topic_trend(db_path, topic_name, trend)
    except Exception as e:
        print(f"  [抓取] {topic_name}: {e}")
    return (topic_name, success)


def scrape_topics_batch(topic_items, db_path, max_workers=6):
    """
    并发抓取多个话题的 detail + trend。

    Args:
        topic_items: [{"title": str, "rank": int}, ...]  只包含 topic_flag==1 的话题
        db_path: weibo_self.db 路径
        max_workers: 并发数
    """
    if not topic_items:
        return

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                scrape_single_topic,
                item["title"],
                item["rank"],
                db_path
            ): item["title"]
            for item in topic_items
        }
        for future in as_completed(futures, timeout=120):
            try:
                name, ok = future.result()
                done += 1
            except Exception:
                pass

    print(f"[话题抓取] {done}/{len(topic_items)} 个话题详情已入库")


def cleanup_old_detail(db_path, keep_days=7):
    """清理超过 keep_days 天的 topic_detail 记录"""
    try:
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - keep_days * 86400)
        )
        db = sqlite3.connect(db_path)
        deleted = db.execute(
            "DELETE FROM topic_detail WHERE scrape_time < ?", (cutoff,)
        ).rowcount
        # 趋势表也清理过期数据
        deleted_trend = db.execute(
            "DELETE FROM topic_trend WHERE scrape_hour < ?", (cutoff,)
        ).rowcount
        db.commit()
        db.close()
        if deleted or deleted_trend:
            print(f"[清理] 删除了 {deleted} 条详情、{deleted_trend} 条趋势（{keep_days}天前）")
    except Exception as e:
        print(f"[清理] 失败: {e}")
