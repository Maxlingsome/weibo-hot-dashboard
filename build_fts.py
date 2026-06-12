"""
一次性脚本：为 weibo_history.db 和 douyin_history.db 建立 FTS5 全文索引
CJK 字符间插入空格，实现字符级全文搜索
"""
import sqlite3
import os
import re
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# CJK 字符范围
CJK_RE = re.compile(r'([一-鿿㐀-䶿豈-﫿])')


def space_cjk(text):
    """在 CJK 字符间插入空格，使 FTS5 unicode61 能逐字索引"""
    text = CJK_RE.sub(r' \1 ', text)
    return ' '.join(text.split())  # 合并多余空格


DB_CONFIGS = [
    {"path": os.path.join(BASE_DIR, "weibo_history.db"), "name": "微博历史库"},
    {"path": os.path.join(BASE_DIR, "douyin_history.db"), "name": "抖音历史库"},
]


def build_fts(path, name):
    """为已有 SQLite 库建立独立 FTS5 索引（space-CJK）"""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")

    # 删除旧 FTS 表（content-sync 版本）
    db.execute("DROP TABLE IF EXISTS topics_fts")
    db.commit()

    print(f"  [{name}] 创建 FTS5 索引（CJK 逐字分词）...")
    t0 = time.time()

    # 独立 FTS 表：rowid = topics.id
    db.execute("CREATE VIRTUAL TABLE topics_fts USING fts5(word)")
    db.commit()

    # 分批读取 + 逐行插入（避免内存爆炸）
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
            spaced = space_cjk(word)
            db.execute(
                "INSERT INTO topics_fts(rowid, word) VALUES(?, ?)",
                (row_id, spaced)
            )
            inserted += 1

        db.commit()
        offset += batch_size
        pct = min(100, int(offset / total * 100))
        print(f"    {pct}% ({inserted}/{total})", end="\r")

    elapsed = time.time() - t0
    fts_count = db.execute("SELECT COUNT(*) FROM topics_fts").fetchone()[0]
    print(f"\n  [{name}] ✅ 完成，{fts_count} 条索引，耗时 {elapsed:.1f}s")
    db.close()


def main():
    print("=" * 50)
    print("  为热搜历史库建立 FTS5 全文索引（CJK 分词）")
    print("=" * 50)
    for cfg in DB_CONFIGS:
        if not os.path.exists(cfg["path"]):
            print(f"  [{cfg['name']}] 文件不存在，跳过")
            continue
        build_fts(cfg["path"], cfg["name"])
    print("\n✅ 全部完成，重启 server 即可生效")


if __name__ == "__main__":
    main()
