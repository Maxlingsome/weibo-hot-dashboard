"""
微博话题文案面板 - 线上版后端
- weibotop.cn API 抓微博热搜（无需浏览器）
- DeepSeek API 生成话题文案
- 后台定时轮询 + 内存缓存
- 纯 Python 标准库 + pycryptodome

启动: python3 server_deploy.py
线上部署: Railway / Render / Fly.io / VPS
"""
import json
import os
import re
import sys
import time
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# 引入 weibotop_api.py（weibotop.cn API 封装，代理可选）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from weibotop_api import get_latest, get_items, search_topic
except ImportError:
    print("❌ 找不到 weibotop_api.py，请确认文件存在")
    sys.exit(1)

# ---- 配置（环境变量优先）----
PORT = int(os.getenv("PORT", "18765"))
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "3"))
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hot_cache.json")


# ---- 热搜数据抓取（复用 weibotop.py 函数）----
def fetch_hotlist() -> list:
    """从 weibotop.cn 获取最新热搜榜"""
    try:
        timeid, timestamp = get_latest()
        items = get_items(timeid)

        result = []
        for i, item in enumerate(items, 1):
            name = item[0]
            hotindex = int(item[3])

            # 格式化热度值
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
                "hotindex": hotindex,
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.request.quote(name)}%23",
                "updated_at": timestamp.replace(".0", "") if timestamp else ""
            })

        return result
    except Exception as e:
        print(f"[fetch_hotlist] 抓取失败: {e}")
        return []


def search_topics(query: str) -> list:
    """搜索微博话题历史"""
    if not query or len(query.strip()) == 0:
        return []

    try:
        results = search_topic(query.strip())

        result = []
        for i, item in enumerate(results[:20], 1):
            name = item[0]
            last_time = item[1].replace(".0", "") if len(item) > 1 else ""
            result.append({
                "rank": i,
                "name": name,
                "lastTime": last_time,
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.request.quote(name)}%23"
            })

        return result
    except Exception as e:
        print(f"[search_topics] 搜索失败: {e}")
        return [{"error": f"搜索失败: {e}"}]


# ---- 内存缓存 + 后台轮询 ----
HOT_CACHE = {
    "data": [],
    "time": 0,
}
cache_lock = threading.Lock()


def load_cache_from_disk():
    """从磁盘恢复缓存"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            with cache_lock:
                HOT_CACHE["data"] = saved.get("data", [])
                HOT_CACHE["time"] = saved.get("time", 0)
            print(f"从磁盘加载缓存: {len(HOT_CACHE['data'])} 条热搜")
    except Exception as e:
        print(f"加载缓存失败: {e}")


def save_cache_to_disk():
    """持久化缓存到磁盘"""
    try:
        with cache_lock:
            to_save = {"data": HOT_CACHE["data"], "time": HOT_CACHE["time"]}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False)
    except Exception as e:
        print(f"保存缓存失败: {e}")


def poll_loop():
    """后台线程：定时抓取热搜"""
    while True:
        try:
            now_str = time.strftime("%H:%M:%S")
            print(f"[{now_str}] 抓取微博热搜...")
            items = fetch_hotlist()
            if items:
                with cache_lock:
                    HOT_CACHE["data"] = items
                    HOT_CACHE["time"] = time.time()
                save_cache_to_disk()
                print(f"  获取 {len(items)} 条热搜")
            else:
                print("  抓取为空，保留缓存")
        except Exception as e:
            print(f"  轮询异常: {e}")
        time.sleep(POLL_MINUTES * 60)


# ---- DeepSeek API ----
SYSTEM_PROMPT = """你是微博热搜话题文案专家，专业为游戏行业营销产出高流量话题文案。

## 核心规则（铁律）
- 每条话题文案严格 **15字以内**（纯文案，不含#号）
- **零标点符号**：不用任何逗号、句号、感叹号、问号、破折号
- 不说形容词只呈现画面，标签优先于真名，留白比说满更有力

## 9大转化手法
1. 极致压缩：删弱动词，只留核心冲突
2. 反转引爆：先铺画面，最后动词翻转
3. 数字驱动：精确到个位
4. 人物标签化：外号代替真名
5. 画面呈现：只描述不评价
6. 口语化夸张：反讽/调侃代替正面描述
7. 留白激将：短到让人不舒服
8. 节点缝合：两个不相关节点缝一起
9. 回应体：用"回应"不用"辟谣"

## 游戏行业热搜触发点
- 新版本/新赛季 → 玩家期待+玩法变化+社交货币
- 新角色/新皮肤 → 颜值/强度/二创潜力
- 新代言人 → 人设反差+玩家梗+生活切片
- 电竞赛事 → 夺冠/翻车/争议，情绪先行
- 玩家争议 → 冲突对比+数字驱动+回应体
- 联动合作 → 跨界冲突感+线下打卡+UGC
- 数据/成绩 → 数字+里程碑+玩家共荣
- 玩家UGC出圈 → 标签化+留白+网感

## 游戏行业禁区
- 不写产品卖点（"XX游戏画面精美"=零传播）
- 不写品牌硬广（"XX游戏周年庆福利大放送"=废话题）
- 必须让不玩这游戏的人也想点

## 网感铁律
- 不说形容词只呈现画面
- 标签优先于真名："碳水哥">"外国美食博主"
- 关系词用最熟的："教"不写"指导"，"赢了"不写"夺冠"
- 自带立场不中立："就这""没毛病""我服了"
- 神比喻=传播捷径：A是B界的C

## 输出格式
为每个事件产出 5 个话题方向，每个方向给 1-2 个候选文案，附带：
- 方向名：（角度概括）
- 文案：话题标题
- 手法：（用了哪个转化手法）
- 情绪：（愤怒/温暖/震惊/期待/自豪）
- 预估热度：⭐（1-5星）"""


def get_api_key():
    """获取 DeepSeek API Key"""
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.getenv("DEEPSEEK_API_KEY", "")


def generate_topics(event: str) -> dict:
    """调用 DeepSeek API 生成话题文案"""
    api_key = get_api_key()
    if not api_key:
        return {"error": "未配置 DEEPSEEK_API_KEY，请在环境变量中设置"}

    prompt = f"""用户事件：{event}

请为这个事件产出 5 个微博热搜话题方向，输出严格 JSON：

{{
  "event": "事件概括",
  "directions": [
    {{
      "name": "方向名",
      "candidates": ["文案1", "文案2"],
      "technique": "转化手法",
      "emotion": "情绪",
      "score": "⭐⭐⭐⭐⭐"
    }}
  ],
  "top_pick": "最推荐的话题文案",
  "reason": "为什么这个最可能爆"
}}

铁律：每条文案15字以内，零标点符号。网感优先，去官宣体。"""

    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8,
        "max_tokens": 2000
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            return json.loads(json_match.group())
        return {"raw": content, "event": event}
    except Exception as e:
        return {"error": str(e)}


# ---- HTTP 服务 ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    def _set_headers(self, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ---- API: 健康检查 ----
        if path == "/health":
            self._set_headers()
            with cache_lock:
                age = int((time.time() - HOT_CACHE["time"]) / 60) if HOT_CACHE["time"] else -1
            self.wfile.write(json.dumps({
                "status": "ok",
                "hot_count": len(HOT_CACHE["data"]),
                "cache_age_minutes": age
            }, ensure_ascii=False).encode())

        # ---- API: 热搜列表 ----
        elif path == "/hotlist":
            self._set_headers()
            with cache_lock:
                data = list(HOT_CACHE["data"])
                cache_time = HOT_CACHE["time"]

            if data:
                # 附加缓存信息
                age_min = int((time.time() - cache_time) / 60) if cache_time else 0
                header = {
                    "_cached": f"数据更新于 {age_min} 分钟前",
                    "_total": len(data)
                }
                self.wfile.write(json.dumps([header] + data, ensure_ascii=False).encode())
            else:
                self.wfile.write(json.dumps(
                    [{"error": "暂无数据，请等待首次抓取完成（约 1 分钟）"}],
                    ensure_ascii=False
                ).encode())

        # ---- API: 话题搜索 ----
        elif path == "/search":
            self._set_headers()
            query = params.get("q", [""])[0].strip()
            if not query:
                self.wfile.write(json.dumps([], ensure_ascii=False).encode())
                return
            results = search_topics(query)
            self.wfile.write(json.dumps(results, ensure_ascii=False).encode())

        # ---- 静态文件 ----
        elif path == "/" or path == "/index.html":
            html_path = os.path.join(BASE_DIR, "index.html")
            if os.path.exists(html_path):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(html_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers("text/html")
                self.wfile.write(b"<h1>index.html not found</h1>")

        elif path.startswith("/archive/"):
            # 安全校验：防止路径穿越
            safe_path = os.path.normpath(path.lstrip("/"))
            file_path = os.path.join(BASE_DIR, safe_path)
            if not file_path.startswith(BASE_DIR):
                self.send_error(403)
                return
            if os.path.exists(file_path) and os.path.isfile(file_path):
                self.send_response(200)
                if file_path.endswith(".json"):
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                else:
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        elif path == "/data.json":
            data_path = os.path.join(BASE_DIR, "data.json")
            if os.path.exists(data_path):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(data_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            event = data.get("event", "").strip()

            if not event:
                self._set_headers()
                self.wfile.write(json.dumps({"error": "请输入事件描述"}, ensure_ascii=False).encode())
                return

            result = generate_topics(event)
            self._set_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        else:
            self._set_headers()
            self.wfile.write(json.dumps({"error": "unknown path"}, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        """简洁日志"""
        if "/hotlist" in str(args) or "/health" in str(args):
            return  # 抑制高频请求日志
        print(f"[{self.address_string()}] {args}")


# ---- 启动 ----
def main():
    # 检查依赖
    api_key = get_api_key()
    if not api_key:
        print("⚠️  未配置 DEEPSEEK_API_KEY，/generate 功能不可用")
        print("   请设置环境变量: export DEEPSEEK_API_KEY=your_key")
    else:
        print(f"✅ DeepSeek API Key 已配置")

    # 从磁盘恢复缓存
    load_cache_from_disk()

    # 首次立即抓取
    print("=" * 50)
    print("  微博话题文案面板 - 线上版")
    print(f"  端口: {PORT}")
    print(f"  轮询间隔: {POLL_MINUTES} 分钟")
    print(f"  数据源: api.weibotop.cn")
    print("=" * 50)
    print("首次抓取中...")
    try:
        items = fetch_hotlist()
        if items:
            with cache_lock:
                HOT_CACHE["data"] = items
                HOT_CACHE["time"] = time.time()
            save_cache_to_disk()
            print(f"首次抓取成功: {len(items)} 条热搜")
        else:
            print("首次抓取为空，使用磁盘缓存或等待轮询")
    except Exception as e:
        print(f"首次抓取失败: {e}")

    # 启动后台轮询
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # 启动 HTTP 服务
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"🚀 服务已启动 → http://0.0.0.0:{PORT}")
    print(f"   📊 热搜: GET /hotlist")
    print(f"   🔍 搜索: GET /search?q=关键词")
    print(f"   ✍️  生成: POST /generate")
    print(f"   📖 学习: GET /data.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
