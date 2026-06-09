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
