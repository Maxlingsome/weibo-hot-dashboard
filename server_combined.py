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

# ---- 自建历史数据库（抖音 + 微博）----
DY_SELF_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "douyin_self.db")
WB_SELF_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weibo_self.db")

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
    _init_one_db(DY_SELF_DB)
    _init_one_db(WB_SELF_DB)

def _save_snapshot(db_path, items, word_key="title"):
    """通用：保存热搜快照，同一天同一话题保留最高排名"""
    today = date.today().isoformat()
    db = sqlite3.connect(db_path)
    for item in items:
        word = item.get(word_key, "")
        rank = item.get("rank", 0)
        hot = item.get("hot_value", 0)
        if not word:
            continue
        existing = db.execute("SELECT id, rank FROM topics WHERE word=? AND date=?", (word, today)).fetchone()
        if existing:
            if rank < existing[1]:
                db.execute("UPDATE topics SET rank=?, hot_value=? WHERE id=?", (rank, hot, existing[0]))
        else:
            db.execute("INSERT INTO topics(word, date, rank, hot_value) VALUES(?,?,?,?)", (word, today, rank, hot))
    db.commit()
    db.close()

def save_douyin_snapshot(items):
    _save_snapshot(DY_SELF_DB, items)

def save_weibo_snapshot(items):
    _save_snapshot(WB_SELF_DB, items, word_key="title")

def _search_db(db_path, query):
    """通用：搜索数据库，返回聚合结果"""
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT word, date, rank FROM topics WHERE word LIKE ? ORDER BY date DESC, rank ASC LIMIT 200", (f"%{query}%",)).fetchall()
        by_word = {}
        for r in rows:
            w = r["word"]
            if w not in by_word:
                by_word[w] = {"word": w, "first_date": r["date"], "last_date": r["date"], "best_rank": r["rank"], "appearances": 0, "history": []}
            info = by_word[w]
            info["appearances"] += 1
            info["first_date"] = min(info["first_date"], r["date"])
            info["last_date"] = max(info["last_date"], r["date"])
            info["best_rank"] = min(info["best_rank"], r["rank"])
            if len(info["history"]) < 10:
                info["history"].append({"date": r["date"], "rank": r["rank"]})
        db.close()
        return sorted(by_word.values(), key=lambda x: x["appearances"], reverse=True)
    except:
        return []

def search_self_db(query):
    return _search_db(DY_SELF_DB, query)

def search_weibo_self_db(query):
    return _search_db(WB_SELF_DB, query)

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


# ---- 微博热搜抓取 ----
def fetch_weibo_hot():
    try:
        timeid, timestamp = get_latest()
        items = get_items(timeid)
        result = []
        for i, item in enumerate(items, 1):
            name = item[0]
            hotindex = int(item[3])
            if hotindex >= 1_000_000:
                hot_str = f"{hotindex/1_000_000:.1f}M"
            elif hotindex >= 1_000:
                hot_str = f"{hotindex/1_000:.0f}K"
            else:
                hot_str = str(hotindex)
            result.append({
                "rank": i,
                "title": name,
                "hot": hot_str,
                "hot_value": hotindex,
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.request.quote(name)}%23",
                "updated_at": timestamp.replace(".0", "") if timestamp else ""
            })
        return result
    except Exception as e:
        print(f"[微博] 抓取失败: {e}")
        return []


# ---- 内存缓存 ----
CACHE = {"weibo": [], "douyin": [], "weibo_time": 0, "douyin_time": 0}
cache_lock = threading.Lock()


def poll_weibo():
    while True:
        try:
            items = fetch_weibo_hot()
            if items:
                with cache_lock:
                    CACHE["weibo"] = items
                    CACHE["weibo_time"] = time.time()
                save_weibo_snapshot(items)
                print(f"[微博] {len(items)} 条热搜（已保存）")
        except Exception as e:
            print(f"[微博] 轮询异常: {e}")
        time.sleep(180)  # 3分钟


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
        time.sleep(180)


# ---- DeepSeek（复用）----
SYSTEM_PROMPT = """你是微博热搜话题文案专家，专业为游戏行业营销产出高流量话题文案。
## 核心规则
- 每条话题文案严格15字以内
- 零标点符号
- 不说形容词只呈现画面，标签优先于真名
## 输出格式
为每个事件产出5个话题方向，每个方向给1-2个候选文案。返回严格JSON：
{"event":"","directions":[{"name":"","candidates":[""],"technique":"","emotion":"","score":"⭐"}],"top_pick":"","reason":""}"""


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
    prompt = f"用户事件：{event}\n请为这个事件产出5个微博热搜话题方向，输出严格JSON。铁律：每条文案15字以内，零标点符号。"
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
    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/weibo":
            with cache_lock:
                data = list(CACHE["weibo"])
            self._json(data)

        elif path == "/api/douyin":
            with cache_lock:
                data = list(CACHE["douyin"])
            self._json(data)

        elif path == "/api/all":
            with cache_lock:
                data = {
                    "weibo": {"items": list(CACHE["weibo"]), "updated": CACHE["weibo_time"]},
                    "douyin": {"items": list(CACHE["douyin"]), "updated": CACHE["douyin_time"]},
                }
            self._json(data)

        elif path == "/api/search":
            query = params.get("q", [""])[0].strip()
            if not query:
                return self._json([])
            # 优先查本地历史库（快，全）
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
                return self._json(result)
            # 兜底：weibotop.cn API
            results = search_topic(query)[:50]
            # 从自建库获取峰值排名
            peak_map = {}
            self_results = search_weibo_self_db(query)
            for r in self_results:
                peak_map[r["word"]] = r["best_rank"]
            # 对自建库里没有的话题，用 weibotop.cn 补查（只查前5个，避免太慢）
            from concurrent.futures import ThreadPoolExecutor, as_completed
            need_fetch = [(i, item) for i, item in enumerate(results) if not peak_map.get(item[0])][:20]
            if need_fetch:
                def fetch_peak(item):
                    try:
                        detail = get_topic_detail(item[0])
                        # 返回 [timeId, timestamp, rank]
                        if isinstance(detail, list) and len(detail) >= 3:
                            return item[0], int(detail[2])
                        return item[0], None
                    except:
                        return item[0], None
                with ThreadPoolExecutor(max_workers=10) as ex:
                    futures = {ex.submit(fetch_peak, item): idx for idx, item in need_fetch}
                    for fut in as_completed(futures, timeout=15):
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
            self._json(result)

        elif path == "/api/search-douyin":
            query = params.get("q", [""])[0].strip().lower()
            if not query:
                return self._json([])
            results = []
            # 方法1: 查自建数据库（自己爬的）
            self_results = search_self_db(query)
            # 方法2: 查第三方历史库（lonnyzhang423）
            ext_results = []
            db_path = os.path.join(BASE_DIR, "douyin_history.db")
            if os.path.exists(db_path):
                try:
                    db = sqlite3.connect(db_path)
                    db.row_factory = sqlite3.Row
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
            self._json(results)

        elif path == "/" or path == "/index.html":
            html_path = os.path.join(BASE_DIR, "index_taste.html")
            if not os.path.exists(html_path):
                html_path = os.path.join(BASE_DIR, "index_combined.html")
            if os.path.exists(html_path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
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
        if self.path == "/api/generate":
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
    """导出双平台数据库 → backup/ 目录（含峰值汇总）"""
    _ensure_backup_dir()
    total = 0
    for db_path, filename in [(DY_SELF_DB, "douyin_data.json"), (WB_SELF_DB, "weibo_data.json")]:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT word, date, rank, hot_value FROM topics ORDER BY date DESC, rank ASC").fetchall()
        db.close()
        # 计算每个话题的最高排名
        peak = {}
        for r in rows:
            w = r["word"]
            if w not in peak or r["rank"] < peak[w]["best_rank"]:
                peak[w] = {"word": w, "best_rank": r["rank"], "appearances": 0, "first_date": r["date"], "last_date": r["date"]}
            info = peak[w]
            info["appearances"] += 1
            info["first_date"] = min(info["first_date"], r["date"])
            info["last_date"] = max(info["last_date"], r["date"])

        data = {
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_records": len(rows),
            "total_topics": len(peak),
            "records": [{"word": r["word"], "date": r["date"], "rank": r["rank"], "hot": r["hot_value"]} for r in rows],
            "peak_summary": sorted(peak.values(), key=lambda x: x["appearances"], reverse=True)
        }
        path = os.path.join(BACKUP_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        total += len(rows)
    return os.path.join(BACKUP_DIR, "douyin_data.json"), total

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
    """导出 + git push 到 GitHub"""
    try:
        path, count = export_backup()
        if count == 0:
            return "无数据，跳过"
        import subprocess
        subprocess.run(["git", "add", "backup/douyin_data.json", "backup/weibo_data.json"], cwd=BASE_DIR, capture_output=True, timeout=20)
        subprocess.run(["git", "commit", "-m", f"[自动备份] {count}条(微博+抖音) {time.strftime('%m-%d %H:%M')}"],
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
        except Exception as e:
            print(f"[备份] 异常: {e}")


def main():
    init_self_db()
    # 从备份恢复（如果本地库为空）
    restored = import_backup()
    if restored:
        print(f"📦 从 GitHub 备份恢复了 {restored} 条历史数据")

    print("=" * 50)
    print("  🔥 微博 + 🎵 抖音 热搜统一面板")
    print(f"  地址: http://localhost:{PORT}")
    print("=" * 50)

    # 首次抓取
    print("首次抓取中...")
    for name, fn in [("微博", fetch_weibo_hot), ("抖音", fetch_douyin_hot)]:
        try:
            items = fn()
            if items:
                with cache_lock:
                    if name == "微博":
                        CACHE["weibo"] = items
                        CACHE["weibo_time"] = time.time()
                    else:
                        CACHE["douyin"] = items
                        CACHE["douyin_time"] = time.time()
                print(f"  [{name}] {len(items)} 条")
        except Exception as e:
            print(f"  [{name}] 失败: {e}")

    # 后台轮询 + 备份
    threading.Thread(target=poll_weibo, daemon=True).start()
    threading.Thread(target=poll_douyin, daemon=True).start()
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

