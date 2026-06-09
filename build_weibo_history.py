#!/usr/bin/env python3
"""微博热搜历史数据导入（优化版：顺序下载+超时+即时入库）"""
import sys, os, time, sqlite3, json, ssl, hashlib, base64
from datetime import date, timedelta
from urllib.request import Request, urlopen, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weibo_history.db")
START_DATE = date(2022, 6, 1)

# AES 配置（直接从 weibotop_api 复制，避免 import 问题）
SHA1_INPUT = "tSdGmtwh49BcR1irt18mxG41dGsBuGKS"
_sha1_hex = hashlib.sha1(SHA1_INPUT.encode()).hexdigest()
AES_KEY = bytes.fromhex(_sha1_hex[:32])
BASE_URL = "https://api.weibotop.cn"

def encrypt(data):
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(data.encode(), 16))).decode()

def decrypt(b64):
    return json.loads(unpad(AES.new(AES_KEY, AES.MODE_ECB).decrypt(base64.b64decode(b64)), 16))

def api_get(path, params=None):
    url = f"{BASE_URL}/{path}"
    if params:
        url += "?" + "&".join(f"{k}={quote(str(v))}" for k,v in params.items())
    ctx = ssl.create_default_context()
    ctx.check_hostname = ctx.verify_mode = False
    req = Request(url)
    with urlopen(req, timeout=15, context=ctx) as r:
        return r.read().decode()

def fetch_day(d):
    try:
        ts = f"{d.isoformat()} 12:00:00"
        latest = json.loads(api_get("getclosesttime", {"timestamp": encrypt(ts)}))
        timeid = latest[0]
        raw = api_get("currentitems", {"timeid": encrypt(timeid)})
        items = decrypt(raw)
        return d.isoformat(), [(items[i][0], i+1, int(items[i][3])) for i in range(len(items))]
    except Exception as e:
        return d.isoformat(), []

def main():
    db = sqlite3.connect(DB_FILE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL,
        date TEXT NOT NULL, rank INTEGER NOT NULL, hot_value INTEGER DEFAULT 0)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_word ON topics(word)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_date ON topics(date)")
    db.commit()

    existing = set(r[0] for r in db.execute("SELECT DISTINCT date FROM topics").fetchall())
    days = [d for d in [START_DATE + timedelta(days=i) for i in range((date.today() - START_DATE).days + 1)]
            if d.isoformat() not in existing]

    if not days:
        print("已是最新")
        db.close()
        return

    print(f"下载 {len(days)} 天，线程数 8，预计 3-5 分钟")
    done = added = skipped = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_day, d): d for d in days}
        for fut in as_completed(futures):
            date_str, items = fut.result()
            if items:
                db.execute("DELETE FROM topics WHERE date=?", (date_str,))
                db.executemany("INSERT INTO topics(word, date, rank, hot_value) VALUES(?,?,?,?)",
                              [(w, date_str, r, h) for w, r, h in items])
                added += len(items)
            else:
                skipped += 1
            done += 1
            if done % 20 == 0:
                db.commit()
                elapsed = time.time() - t0
                eps = done / elapsed
                remaining = (len(days) - done) / eps if eps > 0 else 0
                print(f"  {done}/{len(days)} ({done*100//len(days)}%) | {added}条 | 预计剩余 {remaining:.0f}s", flush=True)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    distinct = db.execute("SELECT COUNT(DISTINCT word) FROM topics").fetchone()[0]
    print(f"\n✅ {total}条, {distinct}个话题, {len(days)+len(existing)}天 ({existing}天已有, 失败{skipped}天)")
    db.close()

if __name__ == "__main__":
    main()
