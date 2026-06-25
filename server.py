"""
微博话题文案面板 - 后端服务
- DeepSeek API 生成话题文案
- CDP 实时抓微博热搜
- CDP 搜索历史话题
"""
import json
import os
import re
import time
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

PORT = 18765
CDP_PROXY = "http://localhost:3456"
CACHE_FILE = os.path.expanduser("~/weibo-dashboard/data.json")
HOT_CACHE = {"data": None, "time": 0}

def get_api_key():
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.getenv("DEEPSEEK_API_KEY", "")

def cdp_request(method, path, body=None, timeout=15):
    """Call CDP proxy API"""
    try:
        url = f"{CDP_PROXY}{path}"
        data = body.encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def fetch_hotlist():
    """抓取微博实时热搜榜
    优先 CDP（Chrome已开启且proxy可用的场景）
    兜底 weibotop.py（可能慢但独立）"""
    import subprocess

    # 方法1: CDP
    result = fetch_via_cdp()
    if result and not (len(result) == 1 and "error" in result[0]):
        return result

    # 方法2: weibotop.py
    try:
        env = os.environ.copy()
        env["https_proxy"] = "http://127.0.0.1:7897"
        r = subprocess.run(
            ["python3", os.path.expanduser("~/scripts/weibotop.py"), "--json", "--top", "50"],
            capture_output=True, text=True, timeout=30, env=env
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            items = data if isinstance(data, list) else data.get("results", [])
            return [{
                "rank": item.get("rank", i+1),
                "title": item.get("name", item.get("title", "")),
                "hot": str(item.get("hotindex", item.get("hot", ""))),
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.parse.quote(item.get('name', item.get('title', '')))}%23"
            } for i, item in enumerate(items[:50]) if item.get("name") or item.get("title")]
    except:
        pass

    return None

def fetch_via_cdp():
    """通过 CDP 浏览器抓热搜"""
    try:
        targets = cdp_request("GET", "/targets", timeout=3)
        if "error" in targets:
            return None

        new_tab = cdp_request("GET", "/new?url=https://s.weibo.com/top/summary", timeout=15)
        if "error" in new_tab or not new_tab.get("targetId"):
            return None

        target_id = new_tab["targetId"]
        time.sleep(3)

        js = """
(() => {
  const rows = document.querySelectorAll("#pl_top_realtimehot table tbody tr");
  const results = [];
  rows.forEach((tr,i) => {
    const rankEl = tr.querySelector(".td-01");
    const td02 = tr.querySelector(".td-02");
    const link = td02?.querySelector("a");
    if (!link || link.href.includes("javascript")) return;
    const title = link.textContent?.trim();
    if (!title) return;
    const hot = td02?.textContent?.split(title)[1]?.trim() || "";
    results.push({rank: i+1, title, hot, url: link.href});
  });
  return JSON.stringify(results);
})()
"""
        result = cdp_request("POST", f"/eval?target={target_id}", body=js, timeout=10)
        cdp_request("GET", f"/close?target={target_id}", timeout=3)

        if "error" in result:
            return None

        return json.loads(result.get("value", "[]"))
    except:
        return None

def search_topics(query):
    """搜索微博话题（优先CDP，兜底weibotop.py）"""
    if not query or len(query) < 1:
        return []

    # CDP优先
    result = search_via_cdp(query)
    if result and not (len(result) == 1 and "error" in result[0]):
        return result[:20]

    # weibotop.py 兜底
    import subprocess
    try:
        env = os.environ.copy()
        env["https_proxy"] = "http://127.0.0.1:7897"
        r = subprocess.run(
            ["python3", os.path.expanduser("~/scripts/weibotop.py"), "--search", query, "--json", "--top", "20"],
            capture_output=True, text=True, timeout=30, env=env
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            results = data if isinstance(data, list) else data.get("results", [])
            return [{
                "name": item.get("name", ""),
                "lastTime": item.get("lastTime", item.get("last_time", "")),
                "url": f"https://s.weibo.com/weibo?q=%23{urllib.parse.quote(item.get('name', ''))}%23"
            } for item in results[:20] if item.get("name")]
    except:
        pass

    return [{"error": "搜索需要Chrome开启远程调试，请打开 chrome://inspect/#remote-debugging 并勾选Allow"}]

def search_via_cdp(query):
    """通过CDP搜索微博话题"""
    try:
        encoded = urllib.parse.quote(query)
        new_tab = cdp_request("GET", f"/new?url=https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D{encoded}", timeout=15)
        if "error" in new_tab:
            return None
        target_id = new_tab.get("targetId", "")
        time.sleep(2)

        js = """
(() => {
  const cards = document.querySelectorAll(".card");
  const results = [];
  cards.forEach(card => {
    const name = card.querySelector(".m-text-cut, .card-main .weibo-text, .name")?.textContent?.trim();
    const text = card.querySelector(".weibo-text, .card-main .weibo-main")?.textContent?.trim();
    if (name && name.length > 1 && name.length < 100) {
      results.push({
        name: name.substring(0, 50),
        text: text?.substring(0, 200) || "",
        author: card.querySelector(".m-text-box .m-text-cut, .head_name")?.textContent?.trim() || "",
        time: card.querySelector(".from, .time")?.textContent?.trim() || ""
      });
    }
  });
  return JSON.stringify(results.slice(0, 20));
})()
"""
        result = cdp_request("POST", f"/eval?target={target_id}", body=js, timeout=10)
        cdp_request("GET", f"/close?target={target_id}", timeout=3)
        if "error" in result:
            return None
        return json.loads(result.get("value", "[]"))
    except:
        return None

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
- 不说形容词只呈现画面，标签优先于真名，留白比说满更有力

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
- 自省反转让：最应该去稻城的人是我自己
- 清单体：人失去魅力的十种行为（开放式结构=天然UGC触发器）

## 输出格式
为每个事件产出5个话题方向，每个方向给1-2个候选文案。返回严格JSON：
{"event":"","directions":[{"name":"方向名","candidates":["文案"],"technique":"手法","emotion":"情绪","score":"⭐⭐⭐"}],"top_pick":"首选话题","reason":"理由"}"""

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

        if parsed.path == "/health":
            self._set_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())

        elif parsed.path == "/hotlist":
            self._set_headers()
            now = time.time()

            # 尝试获取新数据
            fresh = fetch_hotlist()
            if fresh:
                HOT_CACHE["data"] = fresh
                HOT_CACHE["time"] = now
                self.wfile.write(json.dumps(fresh, ensure_ascii=False).encode())
                return

            # 有缓存就用缓存
            if HOT_CACHE["data"]:
                data = [{"_cached": f"数据来自 {int((now - HOT_CACHE['time'])/60)} 分钟前"}] + HOT_CACHE["data"]
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
                return

            # 最后尝试读 cron job 写的 data.json
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    cached = json.load(f)
                items = cached.get("hot_list", [])
                if items:
                    self.wfile.write(json.dumps(items, ensure_ascii=False).encode())
                    return

            self.wfile.write(json.dumps(
                [{"error": "无法获取热搜。请确保Chrome已开启并启用远程调试，或等待cron job更新"}],
                ensure_ascii=False).encode())

        elif parsed.path == "/search":
            # 搜索话题
            self._set_headers()
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0].strip()
            if not query:
                self.wfile.write(json.dumps([], ensure_ascii=False).encode())
                return
            results = search_topics(query)
            self.wfile.write(json.dumps(results, ensure_ascii=False).encode())

        elif parsed.path == "/":
            self._set_headers("text/html")
            self.wfile.write(b"<script>location.href='index.html'</script>")

        else:
            # 静态文件服务
            path = parsed.path.lstrip("/") or "index.html"
            # 安全：阻止路径穿越
            if ".." in path or path.startswith("/"):
                self._set_headers()
                self.wfile.write(json.dumps({"error": "forbidden"}, ensure_ascii=False).encode())
                return
            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            if not os.path.isfile(filepath):
                self._set_headers()
                self.wfile.write(json.dumps({"error": "not found"}, ensure_ascii=False).encode())
                return
            ext = os.path.splitext(path)[1].lower()
            mime = {
                ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
                ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
                ".ico": "image/x-icon", ".woff2": "font/woff2"
            }.get(ext, "application/octet-stream")
            self._set_headers(mime)
            with open(filepath, "rb") as f:
                self.wfile.write(f.read())

    def do_POST(self):
        if self.path in ("/generate", "/api/v1/g"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)
            event = data.get("event", "").strip()

            if not event:
                self._set_headers()
                self.wfile.write(json.dumps({"error": "请输入事件描述"}, ensure_ascii=False).encode())
                return

            result = self._generate(event)
            self._set_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        else:
            self._set_headers()
            self.wfile.write(json.dumps({"error": "unknown path"}, ensure_ascii=False).encode())

    def _generate(self, event):
        api_key = get_api_key()
        if not api_key:
            return {"error": "未找到 DEEPSEEK_API_KEY"}

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
            "temperature": 0.8,
            "max_tokens": 2000
        }, ensure_ascii=False).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        )

        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                return json.loads(json_match.group())
            return {"raw": content, "event": event}
        except Exception as e:
            return {"error": str(e)}

    def log_message(self, format, *args):
        pass

def main():
    api_key = get_api_key()
    if not api_key:
        print("⚠️  未找到 DEEPSEEK_API_KEY")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"🚀 微博面板已启动 → http://localhost:{PORT}/index.html")
    print(f"   📊 实时热搜: GET /hotlist")
    print(f"   🔍 话题搜索: GET /search?q=关键词")
    print(f"   ✍️  生成文案: POST /generate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务已停止")

if __name__ == "__main__":
    main()
