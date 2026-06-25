"""
微博热搜 + 抖音热搜 统一面板
- 微博数据: weibotop.cn API
- 抖音数据: 抖音公开 API
- 后台双线程定时轮询
"""
import json
import os
import re
import sqlite3
import sys
import time
import threading
import urllib.request
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# 引入微博 API
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weibotop_api import get_latest, get_items, search_topic, get_topic_detail
from topic_scraper import scrape_topics_batch
from anti_scrape import firewall

# ---- 自建历史数据库（抖音 + 微博）----
# 线上 Railway 使用持久化卷 /data（需在 Railway 后台创建 Volume 挂载到此路径）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/data" if os.path.isdir("/data") else BASE_DIR
DY_SELF_DB = os.path.join(DATA_DIR, "douyin_self.db")
WB_SELF_DB = os.path.join(DATA_DIR, "weibo_self.db")
KS_SELF_DB = os.path.join(DATA_DIR, "kuaishou_self.db")

def _init_one_db(db_path):
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL,
        date TEXT NOT NULL, rank INTEGER NOT NULL, hot_value INTEGER DEFAULT 0)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_word ON topics(word)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_date ON topics(date)")
    db.commit()
    db.close()

def init_self_db():
    for db_path in [DY_SELF_DB, WB_SELF_DB, KS_SELF_DB]:
        _init_one_db(db_path)

def _init_topic_detail_tables(db_path):
    """创建话题详情 + 趋势表（仅微博）"""
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_detail (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_name TEXT NOT NULL,
        scrape_time TEXT NOT NULL,
        read_count INTEGER DEFAULT 0,
        mention_count INTEGER DEFAULT 0,
        interact_count INTEGER DEFAULT 0,
        ori_count INTEGER DEFAULT 0,
        sum_24h TEXT,
        sum_30d TEXT,
        current_rank INTEGER DEFAULT 0
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_td_name ON topic_detail(topic_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_td_time ON topic_detail(scrape_time)")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_trend (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_name TEXT NOT NULL,
        trend_type TEXT NOT NULL,
        scrape_hour TEXT NOT NULL,
        data_json TEXT NOT NULL
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tt_unique ON topic_trend(topic_name, trend_type, scrape_hour)")
    db.commit()
    db.close()

def _save_snapshot(db_path, items, word_key="title"):
    """通用：保存热搜快照，按小时记录（每次抓取独立存储）"""
    now = time.strftime("%Y-%m-%d %H:00")  # 精确到小时
    db = sqlite3.connect(db_path)
    for item in items:
        word = item.get(word_key, "")
        rank = item.get("rank", 0)
        hot = item.get("hot_value", 0)
        if not word:
            continue
        db.execute("INSERT INTO topics(word, date, rank, hot_value) VALUES(?,?,?,?)", (word, now, rank, hot))
    db.commit()
    db.close()

def save_douyin_snapshot(items):
    _save_snapshot(DY_SELF_DB, items)

def save_weibo_snapshot(items):
    _save_snapshot(WB_SELF_DB, items, word_key="title")

def save_kuaishou_snapshot(items):
    _save_snapshot(KS_SELF_DB, items, word_key="title")

# CJK 字符正则（FTS5 搜索时查询分词用）
CJK_RE = re.compile(r'([一-鿿㐀-䶿豈-﫿])')


def _space_cjk(text):
    """在 CJK 字符间插入空格，使 FTS5 能逐字索引中文"""
    return ' '.join(CJK_RE.sub(r' \1 ', text).split())


def _ensure_fts():
    """启动时检查并为历史库构建 FTS5 索引（如果不存在）"""
    for db_path, label in [
        (os.path.join(BASE_DIR, "weibo_history.db"), "微博历史库"),
        (os.path.join(BASE_DIR, "douyin_history.db"), "抖音历史库"),
    ]:
        if not os.path.exists(db_path):
            continue
        try:
            db = sqlite3.connect(db_path)
            has_fts = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='topics_fts'"
            ).fetchone() is not None
            db.close()
            if has_fts:
                print(f"  [{label}] FTS5 索引已存在，跳过")
                continue
        except:
            pass

        print(f"  [{label}] 构建 FTS5 索引...")
        t0 = time.time()
        try:
            db = sqlite3.connect(db_path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("DROP TABLE IF EXISTS topics_fts")
            db.execute("CREATE VIRTUAL TABLE topics_fts USING fts5(word)")
            db.commit()

            total = db.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            batch_size = 5000
            offset = 0
            inserted = 0
            while offset < total:
                rows = db.execute(
                    "SELECT id, word FROM topics ORDER BY id LIMIT ? OFFSET ?",
                    (batch_size, offset)
                ).fetchall()
                for row_id, word in rows:
                    db.execute(
                        "INSERT INTO topics_fts(rowid, word) VALUES(?, ?)",
                        (row_id, _space_cjk(word))
                    )
                    inserted += 1
                db.commit()
                offset += batch_size
            db.close()
            elapsed = time.time() - t0
            print(f"  [{label}] ✅ FTS5 索引完成（{inserted} 条，{elapsed:.1f}s）")
        except Exception as e:
            print(f"  [{label}] ❌ FTS5 构建失败: {e}")


def _search_db(db_path, query):
    """通用：搜索数据库，优先 FTS5（CJK 分词）→ LIKE 兜底，返回聚合结果"""
    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row

        # 检查 FTS5 索引是否存在
        has_fts = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topics_fts'"
        ).fetchone() is not None

        if has_fts:
            # FTS5 CJK 分词搜索：查询词也做空格分词
            fts_query = ' '.join(CJK_RE.sub(r' \1 ', query).split())
            rows = db.execute("""
                SELECT t.word, t.date, t.rank
                FROM topics_fts f
                JOIN topics t ON t.id = f.rowid
                WHERE topics_fts MATCH ?
                ORDER BY t.date DESC, t.rank ASC
                LIMIT 200
            """, (fts_query,)).fetchall()
        else:
            # LIKE 兜底（无 FTS 时）
            rows = db.execute(
                "SELECT word, date, rank FROM topics WHERE word LIKE ? ORDER BY date DESC, rank ASC LIMIT 200",
                (f"%{query}%",)
            ).fetchall()

        # 聚合：SQL GROUP BY 计算元信息
        if rows:
            word_set = list(set(r['word'] for r in rows))
            placeholders = ','.join('?' for _ in word_set)
            agg_rows = db.execute(
                f"SELECT word, MIN(date) as first_date, MAX(date) as last_date, "
                f"MIN(rank) as best_rank, COUNT(*) as cnt "
                f"FROM topics WHERE word IN ({placeholders}) GROUP BY word",
                word_set
            ).fetchall()
            agg_map = {r['word']: r for r in agg_rows}
        else:
            agg_map = {}

        by_word = {}
        for r in rows:
            w = r["word"]
            if w not in by_word:
                agg = agg_map.get(w)
                by_word[w] = {
                    "word": w,
                    "first_date": agg["first_date"] if agg else r["date"],
                    "last_date": agg["last_date"] if agg else r["date"],
                    "best_rank": agg["best_rank"] if agg else r["rank"],
                    "appearances": agg["cnt"] if agg else 1,
                    "history": []
                }
            info = by_word[w]
            if len(info["history"]) < 10:
                info["history"].append({"date": r["date"], "rank": r["rank"]})
        db.close()
        return sorted(by_word.values(), key=lambda x: x["appearances"], reverse=True)
    except Exception as e:
        print(f"[搜索] {db_path}: {e}")
        return []

def search_self_db(query):
    return _search_db(DY_SELF_DB, query)

def search_weibo_self_db(query):
    return _search_db(WB_SELF_DB, query)

def _merge_monitor_data(history_results, self_results):
    """合并历史库和自建库的监测数据"""
    merged = {}
    for r in history_results + self_results:
        w = r["word"]
        if w not in merged:
            merged[w] = {"word": w, "first_date": r["first_date"], "last_date": r["last_date"],
                         "best_rank": r["best_rank"], "appearances": 0, "history": []}
        info = merged[w]
        info["appearances"] += r.get("appearances", 0)
        info["first_date"] = min(info["first_date"], r["first_date"])
        info["last_date"] = max(info["last_date"], r["last_date"])
        info["best_rank"] = min(info["best_rank"], r["best_rank"])
        info["history"].extend(r.get("history", []))
    # 去重+排序 history
    for info in merged.values():
        seen = set()
        unique = []
        for h in sorted(info["history"], key=lambda x: x["date"]):
            k = f'{h["date"]}-{h["rank"]}'
            if k not in seen:
                seen.add(k)
                unique.append(h)
        info["history"] = sorted(unique, key=lambda x: x["date"])[-30:]
    return list(merged.values())

def _get_topic_records(db_path, word):
    """获取话题在所有日期的排名记录（用于走势图）"""
    try:
        db = sqlite3.connect(db_path)
        rows = db.execute(
            "SELECT date, rank, hot_value FROM topics WHERE word=? ORDER BY date ASC",
            (word,)
        ).fetchall()
        db.close()
        return [{"date": r[0], "rank": r[1], "hot": r[2]} for r in rows]
    except:
        return []

PORT = int(os.getenv("PORT", "18768"))

# ---- 抖音热搜抓取 ----
def fetch_douyin_hot():
    """抓取抖音实时热搜"""
    try:
        url = "https://www.douyin.com/aweme/v1/web/hot/search/list/?detail_list=1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.douyin.com/"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("data", {}).get("word_list", [])
        result = []
        for item in items:
            word = item.get("word", "")
            hot = item.get("hot_value", 0)
            if hot >= 10000:
                hot_str = f"{hot/10000:.0f}万"
            else:
                hot_str = str(hot)
            result.append({
                "rank": item.get("position", 0),
                "title": word,
                "hot": hot_str,
                "hot_value": hot,
                "url": f"https://www.douyin.com/search/{urllib.request.quote(word)}"
            })
        return result
    except Exception as e:
        print(f"[抖音] 抓取失败: {e}")
        return []


# ---- 微博热搜抓取（官方 API）----
def fetch_weibo_hot():
    """从微博官方 API 抓实时热搜"""
    try:
        url = "https://weibo.com/ajax/side/hotSearch"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://weibo.com/"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("data", {}).get("realtime", [])
        # 同时获取文娱榜标签（剧集/综艺/演出等）
        wenyu_labels = {}
        try:
            url2 = "https://weibo.com/ajax/statuses/hot_band?band_id=wen_yu"
            req2 = urllib.request.Request(url2, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://weibo.com/"
            })
            with urllib.request.urlopen(req2, timeout=8) as resp2:
                wy_data = json.loads(resp2.read())
            for wy_item in wy_data.get("data", {}).get("band_list", []):
                w = wy_item.get("word", "")
                sl = wy_item.get("subject_label", "")
                if w and sl:
                    wenyu_labels[w] = sl
        except:
            pass
        result = []
        for i, item in enumerate(items, 1):
            word = item.get("word", "")
            num = item.get("num", 0)
            if num >= 10000:
                hot_str = f"{num/10000:.0f}万"
            else:
                hot_str = str(num)
            badge = item.get("icon_desc", "") or item.get("label_name", "")  # 新/荐/热
            wy_label = wenyu_labels.get(word, "")  # 剧集/综艺/演出
            result.append({
                "rank": i,
                "title": word,
                "hot": hot_str,
                "hot_value": num,
                "badge": badge,
                "label": wy_label,
                "topic_flag": item.get("topic_flag", 0),
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.request.quote(word)}%23"
            })
        return result
    except Exception as e:
        print(f"[微博] 抓取失败: {e}")
        return []

def fetch_weibo_wenyu():
    """抓取微博文娱榜"""
    try:
        url = "https://weibo.com/ajax/statuses/hot_band?band_id=wen_yu"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://weibo.com/"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("data", {}).get("band_list", [])
        result = []
        for i, item in enumerate(items, 1):
            word = item.get("word", "")
            num = item.get("num", 0)
            hot_str = f"{num/10000:.0f}万" if num >= 10000 else str(num)
            result.append({
                "rank": i, "title": word, "hot": hot_str, "hot_value": num,
                "label": item.get("subject_label", ""), "note": item.get("note", ""),
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.request.quote(word)}%23"
            })
        return result
    except Exception as e:
        print(f"[文娱榜] 抓取失败: {e}")
        return []

def fetch_kuaishou_hot():
    """抓取快手实时热搜"""
    try:
        import re
        url = "https://www.kuaishou.com/?isHome=1"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode()
        match = re.search(r'window\.__APOLLO_STATE__=(.*?);\(function\(\)', html, re.DOTALL)
        if not match:
            print(f"[快手] 无 __APOLLO_STATE__，HTML长度: {len(html)}，前100字: {html[:100]}")
            return []
        data = json.loads(match.group(1))
        client = data.get("defaultClient", {})
        root_key = [k for k in client.keys() if "visionHotRank" in k]
        if not root_key:
            return []
        items_ref = client[root_key[0]].get("items", [])
        result = []
        for i, item_ref in enumerate(items_ref, 1):
            hot_item = client.get(item_ref["id"], {})
            name = hot_item.get("name", "")
            hot_val = hot_item.get("hotValue", "")
            result.append({
                "rank": i, "title": name, "hot": hot_val or "—",
                "url": f"https://www.kuaishou.com/search/video?searchKey={urllib.request.quote(name)}"
            })
        return result
    except Exception as e:
        print(f"[快手] 抓取失败: {e}")
        return []


def fetch_bilibili_hot():
    """抓取B站热搜"""
    try:
        url = "https://api.bilibili.com/x/web-interface/wbi/search/square?limit=50"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("data", {}).get("trending", {}).get("list", [])
        result = []
        for i, item in enumerate(items, 1):
            word = item.get("keyword", "") or item.get("show_name", "")
            result.append({
                "rank": i, "title": word, "hot": "",
                "url": f"https://search.bilibili.com/all?keyword={urllib.request.quote(word)}"
            })
        return result
    except Exception as e:
        print(f"[B站] 抓取失败: {e}")
        return []


# ---- 内存缓存 ----
CACHE = {"weibo": [], "douyin": [], "wenyu": [], "kuaishou": [], "bilibili": [],
         "weibo_time": 0, "douyin_time": 0, "wenyu_time": 0, "kuaishou_time": 0, "bilibili_time": 0}
cache_lock = threading.Lock()

# 搜索结果缓存（TTL 5 分钟）
SEARCH_CACHE = {}  # { "key|query": (timestamp, result_list) }
SEARCH_CACHE_TTL = 300


def poll_weibo():
    while True:
        try:
            items = fetch_weibo_hot()
            if items:
                with cache_lock:
                    CACHE["weibo"] = items
                    CACHE["weibo_time"] = time.time()
                save_weibo_snapshot(items)

                # 话题详情 + 趋势抓取（后台子线程，不阻塞主轮询）
                topic_items = [
                    {"title": i["title"], "rank": i["rank"]}
                    for i in items if i.get("topic_flag") == 1
                ]
                if topic_items:
                    threading.Thread(
                        target=scrape_topics_batch,
                        args=(topic_items, WB_SELF_DB),
                        daemon=True
                    ).start()

                print(f"[微博] {len(items)} 条热搜（已保存）")
        except Exception as e:
            print(f"[微博] 轮询异常: {e}")
        time.sleep(60)


def poll_douyin():
    while True:
        try:
            items = fetch_douyin_hot()
            if items:
                with cache_lock:
                    CACHE["douyin"] = items
                    CACHE["douyin_time"] = time.time()
                save_douyin_snapshot(items)  # 自动保存到数据库
                print(f"[抖音] {len(items)} 条热搜（已保存）")
        except Exception as e:
            print(f"[抖音] 轮询异常: {e}")
        time.sleep(60)

def poll_wenyu():
    while True:
        try:
            items = fetch_weibo_wenyu()
            if items:
                with cache_lock:
                    CACHE["wenyu"] = items
                    CACHE["wenyu_time"] = time.time()
                print(f"[文娱榜] {len(items)} 条")
        except Exception as e:
            print(f"[文娱榜] 轮询异常: {e}")
        time.sleep(60)

def poll_kuaishou():
    while True:
        try:
            items = fetch_kuaishou_hot()
            if items:
                with cache_lock:
                    CACHE["kuaishou"] = items
                    CACHE["kuaishou_time"] = time.time()
                save_kuaishou_snapshot(items)
                print(f"[快手] {len(items)} 条（已保存）")
        except Exception as e:
            print(f"[快手] 轮询异常: {e}")
        time.sleep(60)

def poll_bilibili():
    while True:
        try:
            items = fetch_bilibili_hot()
            if items:
                with cache_lock:
                    CACHE["bilibili"] = items
                    CACHE["bilibili_time"] = time.time()
                print(f"[B站] {len(items)} 条")
        except Exception as e:
            print(f"[B站] 轮询异常: {e}")
        time.sleep(60)


# ---- DeepSeek（复用）----
SYSTEM_PROMPT = """你是微博热搜话题文案专家。底层知识来自20天微博+抖音双平台热搜数据学习，覆盖440+真实案例提炼。

## 商业话题三步法（最核心，一票否决）
1. **明确主体**：品牌/产品/人物名必须在标题里。去掉主体还能成立的标题=废标题。
2. **提取事件关键词**：从事件中提取事实信息词（地点/行为/数据/结果），不是"全球首个""纪录"这类包装词。先确认关键词骨架无误，再做第三步。
3. **网感包装**：信息骨架确认后再用9大手法包装。信息准确 > 网感炫技。

## 核心规则
- 每条话题文案严格15字以内，零标点符号
- 缺主体=一票否决：商业话题标题里没有品牌/产品名，直接淘汰
- 弱动词必杀：获得/进行/实现/开展 → 夺冠/露馅/创下/曝
- 精确数字 > 约数
- 争议用"回应" > "辟谣"
- 微博做减法往短压，抖音做完整叙事
- 不说形容词只呈现画面，标签优先于真名

## 9大转化手法（选用1-2个加工）
1. 极致压缩-删所有弱动词只留核心冲突
2. 反转引爆-先铺预期画面再翻转
3. 数字驱动-精确数字自带冲击力
4. 人物标签化-复杂身份压缩为可传播标签
5. 画面呈现-只描述不评价，让读者自己判断
6. 口语化夸张-反讽/调侃/极端口语替代正面描述
7. 留白激将-短到让人不舒服，不解释不说满
8. 节点缝合-两个不相关元素缝一起
9. 回应体-用"回应"不用"辟谣"，暗示有故事

## 网感案例库（真实热搜提炼，参考语感）
- 物理动词精神化：创飞了（撞飞→考试崩溃）、嚼烂（破坏→情绪）、打入冷宫（刑罚→不吃）
- 拟人化喊话：不锈钢餐具你有点不礼貌了、苹果实况你要毁了小猫
- 轻描淡写体：有种很诡异的感觉、是不一样的
- 半句钩子：看完西班牙之后我想说（标题不说完留白）
- 荒诞声明体：可以质疑我但不能质疑我的馒头
- 自省反转让：最应该去稻城的人是我自己（不说想要说应该）
- 清单体：人失去魅力的十种行为（开放式结构=天然UGC触发器）

## 输出格式
为每个事件产出5个话题方向，每个方向给1-2个候选文案。返回严格JSON：
{"event":"","directions":[{"name":"方向名","candidates":["话题文案"],"technique":"用的手法","emotion":"情绪类型","score":"⭐⭐⭐"}],"top_pick":"推荐首选话题","reason":"推荐理由"}"""


def get_api_key():
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.getenv("DEEPSEEK_API_KEY", "")


def generate_topics(event):
    api_key = get_api_key()
    if not api_key:
        return {"error": "未配置 DEEPSEEK_API_KEY"}
    prompt = f"""事件：{event}

请按三步法处理：
Step 1: 从事件中提取主体（品牌/产品/人物名，必须在标题里）
Step 2: 提取事件事实关键词（地点/行为/数据/结果，不是"全球首个""纪录"这类包装词）
Step 3: 确认关键词骨架无误后，用9大手法包装生成话题

要求：5个话题方向，每个方向1-2个候选文案，每条≤15字零标点。返回严格JSON。"""
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8, "max_tokens": 2000
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        json_match = re.search(r'\{[\s\S]*\}', content)
        return json.loads(json_match.group()) if json_match else {"raw": content, "event": event}
    except Exception as e:
        return {"error": str(e)}


# ---- HTTP ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    def _check_firewall(self):
        """反爬检查，不过则直接返回 403/429"""
        ip = self.client_address[0]
        ua = self.headers.get("User-Agent", "")
        ref = self.headers.get("Referer", "")
        ok, code, reason = firewall(ip, ua, self.path, ref)
        if not ok:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Blocked: {reason}".encode())
        return ok

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "https://weibo-hot-dashboard-production.up.railway.app")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "https://weibo-hot-dashboard-production.up.railway.app")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if not self._check_firewall():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/v1/h":
            with cache_lock:
                data = list(CACHE["weibo"])
            self._json(data)

        elif path == "/api/v1/d":
            with cache_lock:
                data = list(CACHE["douyin"])
            self._json(data)

        elif path == "/api/v1/w":
            with cache_lock:
                data = list(CACHE["wenyu"])
            self._json(data)

        elif path == "/api/v1/k":
            with cache_lock:
                data = list(CACHE["kuaishou"])
            self._json(data)

        elif path == "/api/v1/b":
            with cache_lock:
                data = list(CACHE["bilibili"])
            self._json(data)

        elif path == "/api/v1/t":
            topic = params.get("q", [""])[0].strip()
            if not topic:
                return self._json({"topic": "", "has_data": False})

            db = sqlite3.connect(WB_SELF_DB)
            db.row_factory = sqlite3.Row

            # 最新一条详情
            detail_row = db.execute(
                """SELECT * FROM topic_detail WHERE topic_name=?
                   ORDER BY scrape_time DESC LIMIT 1""",
                (topic,)
            ).fetchone()

            # 今天的趋势数据
            today = time.strftime("%Y-%m-%d")
            trend_rows = db.execute(
                """SELECT trend_type, scrape_hour, data_json FROM topic_trend
                   WHERE topic_name=? AND scrape_hour >= ?
                   ORDER BY scrape_hour DESC, trend_type""",
                (topic, today + " 00:00")
            ).fetchall()
            db.close()

            result = {"topic": topic, "has_data": False}

            if detail_row:
                result["has_data"] = True
                result["latest"] = {
                    "time": detail_row["scrape_time"],
                    "read_count": detail_row["read_count"],
                    "mention_count": detail_row["mention_count"],
                    "interact_count": detail_row["interact_count"],
                    "ori_count": detail_row["ori_count"],
                    "current_rank": detail_row["current_rank"],
                    "sum_24h": json.loads(detail_row["sum_24h"]) if detail_row["sum_24h"] else {},
                    "sum_30d": json.loads(detail_row["sum_30d"]) if detail_row["sum_30d"] else {},
                }

            if trend_rows:
                result["trend"] = {"read": [], "discussion": [], "original": [], "interaction": []}
                seen_types = set()
                for row in trend_rows:
                    ttype = row["trend_type"]
                    if ttype in seen_types:
                        continue
                    seen_types.add(ttype)
                    points = json.loads(row["data_json"])
                    type_map = {"read": "read", "me": "discussion", "ori": "original", "partake": "interaction"}
                    mapped = type_map.get(ttype, ttype)
                    result["trend"][mapped] = points

            self._json(result)

        elif path == "/api/v1/m":
            query = params.get("q", [""])[0].strip()
            if not query:
                return self._json({"error": "请输入话题词"})
            result = {"query": query, "platforms": {}}
            # 精确匹配单个话题
            for platform, sources in [
                ("weibo", [os.path.join(BASE_DIR, "weibo_history.db"), WB_SELF_DB]),
                ("douyin", [os.path.join(BASE_DIR, "douyin_history.db"), DY_SELF_DB]),
                ("kuaishou", [KS_SELF_DB]),
            ]:
                records = []
                for db_path in sources:
                    records.extend(_get_topic_records(db_path, query) if os.path.exists(db_path) else [])
                if records:
                    # 按天聚合：每天取最优排名
                    day_best = {}  # date_only -> {rank, hot, date}
                    for r in records:
                        day = r["date"][:10]  # "2026-06-16 14:00" -> "2026-06-16"
                        if day not in day_best or r["rank"] < day_best[day]["rank"]:
                            day_best[day] = r
                    unique = sorted(day_best.values(), key=lambda x: x["date"])
                    ranks = [r["rank"] for r in unique if r["rank"] > 0]
                    result["platforms"][platform] = {
                        "found": True,
                        "records": unique,
                        "best_rank": min(ranks) if ranks else None,
                        "total_days": len(unique),
                        "first_date": unique[0]["date"],
                        "last_date": unique[-1]["date"],
                    }
                else:
                    # 查实时缓存兜底
                    with cache_lock:
                        live = CACHE.get(platform, [])
                    live_match = [i for i in live if i.get("title", "") == query]
                    result["platforms"][platform] = {
                        "found": len(live_match) > 0,
                        "live": live_match,
                        "records": [],
                    }
            self._json(result)

        elif path == "/api/v1/md":
            word = params.get("word", [""])[0].strip()
            platform = params.get("platform", ["weibo"])[0].strip()
            if not word:
                return self._json([])
            db_map = {
                "weibo": [os.path.join(BASE_DIR, "weibo_history.db"), WB_SELF_DB],
                "douyin": [os.path.join(BASE_DIR, "douyin_history.db"), DY_SELF_DB],
                "kuaishou": [KS_SELF_DB],
            }
            records = []
            for db_path in db_map.get(platform, []):
                records.extend(_get_topic_records(db_path, word))
            # 去重+排序
            seen = set()
            unique = []
            for r in sorted(records, key=lambda x: x["date"]):
                k = f'{r["date"]}-{r["rank"]}'
                if k not in seen:
                    seen.add(k)
                    unique.append(r)
            self._json(unique)

        elif path == "/api/v1/a":
            with cache_lock:
                data = {
                    "weibo": {"items": list(CACHE["weibo"]), "updated": CACHE["weibo_time"]},
                    "douyin": {"items": list(CACHE["douyin"]), "updated": CACHE["douyin_time"]},
                }
            self._json(data)

        elif path == "/api/v1/s":
            query = params.get("q", [""])[0].strip()
            if not query:
                return self._json([])

            # 检查缓存
            cache_key = f"wb|{query}"
            now_ts = time.time()
            if cache_key in SEARCH_CACHE:
                ts, val = SEARCH_CACHE[cache_key]
                if now_ts - ts < SEARCH_CACHE_TTL:
                    return self._json(val)

            # 优先查本地历史库（FTS5 → LIKE）
            local_results = _search_db(os.path.join(BASE_DIR, "weibo_history.db"), query)
            if local_results:
                result = []
                for i, r in enumerate(local_results, 1):
                    result.append({
                        "rank": i, "name": r["word"], "lastTime": r["last_date"],
                        "peakRank": r["best_rank"], "appearances": r["appearances"],
                        "firstDate": r["first_date"],
                        "url": "https://s.weibo.com/weibo?q=%23" + urllib.request.quote(r["word"]) + "%23"
                    })
                SEARCH_CACHE[cache_key] = (now_ts, result)
                return self._json(result)

            # 兜底：weibotop.cn API
            results = search_topic(query)[:50]
            # 从自建库获取峰值排名
            peak_map = {}
            self_results = search_weibo_self_db(query)
            for r in self_results:
                peak_map[r["word"]] = r["best_rank"]
            # 对自建库里没有的话题，用 weibotop.cn 补查（只查前 10 个，避免太慢）
            from concurrent.futures import ThreadPoolExecutor, as_completed
            need_fetch = [(i, item) for i, item in enumerate(results) if not peak_map.get(item[0])][:10]
            if need_fetch:
                def fetch_peak(item):
                    try:
                        detail = get_topic_detail(item[0])
                        if isinstance(detail, list) and len(detail) >= 3:
                            return item[0], int(detail[2])
                        return item[0], None
                    except:
                        return item[0], None
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futures = {ex.submit(fetch_peak, item): idx for idx, item in need_fetch}
                    for fut in as_completed(futures, timeout=5):
                        try:
                            name, rank = fut.result()
                            if rank:
                                peak_map[name] = rank
                        except:
                            pass
            result = []
            for i, item in enumerate(results, 1):
                name = item[0]
                last_time = item[1].replace(".0", "") if len(item) > 1 else ""
                peak = peak_map.get(name)
                result.append({
                    "rank": i, "name": name, "lastTime": last_time,
                    "peakRank": peak,
                    "url": "https://s.weibo.com/weibo?q=%23" + urllib.request.quote(name) + "%23"
                })
            SEARCH_CACHE[cache_key] = (now_ts, result)
            self._json(result)

        elif path == "/api/v1/sd":
            query = params.get("q", [""])[0].strip().lower()
            if not query:
                return self._json([])

            # 检查缓存
            cache_key = f"dy|{query}"
            now_ts = time.time()
            if cache_key in SEARCH_CACHE:
                ts, val = SEARCH_CACHE[cache_key]
                if now_ts - ts < SEARCH_CACHE_TTL:
                    return self._json(val)

            results = []
            # 方法1: 查自建数据库（自己爬的）
            self_results = search_self_db(query)
            # 方法2: 查第三方历史库（lonnyzhang423，FTS5 优先）
            ext_results = []
            db_path = os.path.join(BASE_DIR, "douyin_history.db")
            if os.path.exists(db_path):
                try:
                    db = sqlite3.connect(db_path)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.row_factory = sqlite3.Row
                    has_fts = db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='topics_fts'"
                    ).fetchone() is not None

                    if has_fts:
                        # CJK 分词查询
                        fts_query = ' '.join(CJK_RE.sub(r' \1 ', query).split())
                        rows = db.execute(
                            "SELECT t.word, t.date, t.rank FROM topics_fts f "
                            "JOIN topics t ON t.id = f.rowid "
                            "WHERE topics_fts MATCH ? "
                            "ORDER BY t.date DESC, t.rank ASC LIMIT 100",
                            (fts_query,)
                        ).fetchall()
                    else:
                        rows = db.execute(
                            "SELECT word, date, rank FROM topics WHERE word LIKE ? "
                            "ORDER BY date DESC, rank ASC LIMIT 100",
                            (f"%{query}%",)
                        ).fetchall()

                    ext_by_word = {}
                    for r in rows:
                        w = r["word"]
                        if w not in ext_by_word:
                            ext_by_word[w] = {"word": w, "first_date": r["date"], "last_date": r["date"],
                                              "best_rank": r["rank"], "appearances": 0, "history": []}
                        info = ext_by_word[w]
                        info["appearances"] += 1
                        info["first_date"] = min(info["first_date"], r["date"])
                        info["last_date"] = max(info["last_date"], r["date"])
                        info["best_rank"] = min(info["best_rank"], r["rank"])
                        if len(info["history"]) < 5:
                            info["history"].append({"date": r["date"], "rank": r["rank"]})
                    ext_results = sorted(ext_by_word.values(), key=lambda x: x["appearances"], reverse=True)
                    db.close()
                except Exception as e:
                    print(f"[抖音搜索] 第三方库查询失败: {e}")
            # 合并：自建优先，第三方补充
            seen = {r["word"] for r in self_results}
            for r in ext_results:
                if r["word"] not in seen:
                    self_results.append(r)
                    seen.add(r["word"])
            results = self_results  # 全量返回

            # 兜底：实时热搜中匹配
            if not results:
                with cache_lock:
                    items = list(CACHE["douyin"])
                matched = [{"word": item["title"], "best_rank": item["rank"],
                           "appearances": 1, "history": []} for item in items
                          if query in item.get("title", "").lower()]
                results = matched[:20]

            SEARCH_CACHE[cache_key] = (now_ts, results)
            self._json(results)

        elif path == "/" or path == "/index.html":
            html_path = os.path.join(BASE_DIR, "index_clean.html")
            if not os.path.exists(html_path):
                html_path = os.path.join(BASE_DIR, "index_taste.html")
            if not os.path.exists(html_path):
                html_path = os.path.join(BASE_DIR, "index_combined.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    html = f.read()
                # 服务端注入初始数据，前端首屏无需调 API
                with cache_lock:
                    seed = {
                        "weibo": CACHE["weibo"],
                        "douyin": CACHE["douyin"],
                        "kuaishou": CACHE["kuaishou"],
                        "bilibili": CACHE["bilibili"],
                        "ts": int(time.time()),
                    }
                inject = f"\n<script>window.__SEED__={json.dumps(seed,ensure_ascii=False)};</script>\n"
                html = html.replace("</head>", inject + "</head>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                with open(html_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        elif path.endswith(".html") and os.path.exists(os.path.join(BASE_DIR, path.lstrip("/"))):
            fp = os.path.join(BASE_DIR, path.lstrip("/"))
            if fp.startswith(BASE_DIR):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(403)

        elif path.startswith("/archive/"):
            safe = os.path.normpath(path.lstrip("/"))
            fp = os.path.join(BASE_DIR, safe)
            if fp.startswith(BASE_DIR) and os.path.exists(fp) and os.path.isfile(fp):
                self.send_response(200)
                ct = "application/json" if fp.endswith(".json") else "text/plain"
                self.send_header("Content-Type", f"{ct}; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        elif path == "/data.json":
            dp = os.path.join(BASE_DIR, "data.json")
            if os.path.exists(dp):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(dp, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_firewall():
            return
        if self.path == "/api/v1/g":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._json(generate_topics(body.get("event", "").strip()))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


# ---- 数据备份与恢复 ----
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
BACKUP_FILE = "douyin_data.json"

def _ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)

def export_backup():
    """导出全量数据（含话题详情+趋势）→ backup/ 目录，gzip 压缩"""
    import gzip
    _ensure_backup_dir()
    total = 0
    # 1. 热搜排名数据
    for db_path, filename in [(DY_SELF_DB, "douyin_data.json.gz"), (WB_SELF_DB, "weibo_data.json.gz"), (KS_SELF_DB, "kuaishou_data.json.gz")]:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT word, date, rank, hot_value FROM topics ORDER BY date DESC, rank ASC").fetchall()
        peak = {}
        for r in rows:
            w = r["word"]
            if w not in peak or r["rank"] < peak[w]["best_rank"]:
                peak[w] = {"word": w, "best_rank": r["rank"], "appearances": 0, "first_date": r["date"], "last_date": r["date"]}
            info = peak[w]
            info["appearances"] += 1
            info["first_date"] = min(info["first_date"], r["date"])
            info["last_date"] = max(info["last_date"], r["date"])
        # 导出 topic_detail + topic_trend
        detail_rows = []
        try:
            detail_rows = db.execute("SELECT * FROM topic_detail ORDER BY scrape_time DESC").fetchall()
        except:
            pass
        trend_rows = []
        try:
            trend_rows = db.execute("SELECT * FROM topic_trend ORDER BY scrape_hour DESC").fetchall()
        except:
            pass
        data = {
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_records": len(rows),
            "total_topics": len(peak),
            "records": [{"word": r["word"], "date": r["date"], "rank": r["rank"], "hot": r["hot_value"]} for r in rows],
            "peak_summary": sorted(peak.values(), key=lambda x: x["appearances"], reverse=True),
            "topic_details": [dict(r) for r in detail_rows],
            "topic_trends": [dict(r) for r in trend_rows],
        }
        path = os.path.join(BACKUP_DIR, filename)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        total += len(rows)
        db.close()
    return BACKUP_DIR, total

def auto_trim_db(db_path, max_mb=400):
    """数据库超过 max_mb 时，删除最旧 30% 的 topics 和 topic_detail 记录"""
    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    if size_mb < max_mb:
        return
    db = sqlite3.connect(db_path)
    # 保留最近记录，删最旧的 30%
    for table in ["topics", "topic_detail"]:
        try:
            total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            delete_n = int(total * 0.3)
            if delete_n > 0:
                db.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY id ASC LIMIT ?)", (delete_n,))
        except:
            pass
    db.commit()
    db.execute("VACUUM")
    db.close()
    new_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"[DB瘦身] {os.path.basename(db_path)}: {size_mb:.0f}MB → {new_mb:.0f}MB")

def import_backup():
    """从备份恢复（仅当本地数据库为空时）"""
    path = os.path.join(BACKUP_DIR, BACKUP_FILE)
    if not os.path.exists(path):
        return 0
    try:
        db = sqlite3.connect(SELF_DB)
        exist = db.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        if exist > 0:
            db.close()
            return 0
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        for t in data.get("topics", []):
            db.execute("INSERT OR IGNORE INTO topics(word, date, rank, hot_value) VALUES(?,?,?,?)",
                       (t["word"], t["date"], t["rank"], t.get("hot", 0)))
            count += 1
        db.commit()
        db.close()
        return count
    except Exception as e:
        print(f"[恢复] 失败: {e}")
        return 0

def push_backup():
    """导出 + git push 到 GitHub（压缩格式节省空间）"""
    try:
        path, count = export_backup()
        if count == 0:
            return "无数据，跳过"
        import subprocess
        subprocess.run(["git", "add", "backup/"], cwd=BASE_DIR, capture_output=True, timeout=20)
        subprocess.run(["git", "commit", "-m", f"[自动备份] {count}条 {time.strftime('%m-%d %H:%M')}"],
                       cwd=BASE_DIR, capture_output=True, timeout=20)
        subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True, timeout=60)
        return f"✅ {count} 条已备份到 GitHub"
    except Exception as e:
        return f"❌ {e}"

def backup_loop():
    while True:
        time.sleep(6 * 3600)
        try:
            print(f"[备份] {push_backup()}")
            # 备份后检查 DB 大小，超过 400MB 自动瘦身
            for db_path in [WB_SELF_DB, DY_SELF_DB, KS_SELF_DB]:
                if os.path.exists(db_path):
                    auto_trim_db(db_path, max_mb=400)
        except Exception as e:
            print(f"[备份] 异常: {e}")


def main():
    init_self_db()
    _init_topic_detail_tables(WB_SELF_DB)
    # 从备份恢复（如果本地库为空）
    restored = import_backup()
    if restored:
        print(f"📦 从 GitHub 备份恢复了 {restored} 条历史数据")

    # 自动构建 FTS5 索引（如果不存在）
    _ensure_fts()

    print("=" * 50)
    print("  🔥 微博 + 🎵 抖音 热搜统一面板")
    print(f"  地址: http://localhost:{PORT}")
    print("=" * 50)

    # 首次抓取
    print("首次抓取中...")
    for name, fn in [("微博", fetch_weibo_hot), ("抖音", fetch_douyin_hot), ("文娱", fetch_weibo_wenyu), ("快手", fetch_kuaishou_hot), ("B站", fetch_bilibili_hot)]:
        try:
            items = fn()
            if items:
                with cache_lock:
                    key_map = {"微博": "weibo", "抖音": "douyin", "文娱": "wenyu", "快手": "kuaishou", "B站": "bilibili"}
                    k = key_map.get(name, name)
                    CACHE[k] = items
                    CACHE[k + "_time"] = time.time()
                print(f"  [{name}] {len(items)} 条")
        except Exception as e:
            print(f"  [{name}] 失败: {e}")

    # 后台轮询 + 备份
    threading.Thread(target=poll_weibo, daemon=True).start()
    threading.Thread(target=poll_douyin, daemon=True).start()
    threading.Thread(target=poll_wenyu, daemon=True).start()
    threading.Thread(target=poll_kuaishou, daemon=True).start()
    threading.Thread(target=poll_bilibili, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()

    # 首次备份（1分钟后，等首批数据入库）
    def delayed_backup():
        time.sleep(60)
        print(f"[备份] {push_backup()}")
    threading.Thread(target=delayed_backup, daemon=True).start()

    # HTTP 服务
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n✅ 已启动 → http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()


