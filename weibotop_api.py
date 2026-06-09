#!/usr/bin/env python3
"""
微博热搜历史数据抓取 - 数据来源: weibotop.cn
用法:
  weibotop.py                        # 最新热搜
  weibotop.py 2026-05-20             # 指定日期热搜
  weibotop.py --search 和平精英       # 搜索话题
  weibotop.py --search 特朗普 --top 10 # 搜索前10条
  weibotop.py --json                 # JSON 输出
  weibotop.py 2026-05-20 --json      # 指定日期 JSON 输出
"""

import hashlib
import base64
import json
import os
import sys
import argparse
from datetime import datetime
from urllib.request import Request, urlopen, quote
from urllib.error import URLError
import urllib.request

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad, pad
except ImportError:
    print("需要安装 pycryptodome: pip install pycryptodome", file=sys.stderr)
    sys.exit(1)

AES_BLOCK_SIZE = 16

# ---- 加密配置 ----
SHA1_INPUT = "tSdGtmwh49BcR1irt18mxG41dGsBuGKS"
sha1_hex = hashlib.sha1(SHA1_INPUT.encode()).hexdigest()
AES_KEY = bytes.fromhex(sha1_hex[:32])

BASE_URL = "https://api.weibotop.cn"


def encrypt(data: str) -> str:
    """AES-ECB PKCS7 加密 → Base64"""
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(data.encode(), AES_BLOCK_SIZE))
    return base64.b64encode(encrypted).decode()


def decrypt(b64_data: str):
    """Base64 → AES-ECB 解密 → JSON"""
    ciphertext = base64.b64decode(b64_data)
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    plaintext = unpad(cipher.decrypt(ciphertext), AES_BLOCK_SIZE)
    return json.loads(plaintext.decode("utf-8"))


def api_get(path: str, params=None) -> str:
    """GET 请求 API（优先代理，环境变量 HTTPS_PROXY 控制）"""
    url = f"{BASE_URL}/{path}"
    if params:
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"

    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    proxy_url = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:7897")
    try:
        proxy_handler = urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
        opener = urllib.request.build_opener(proxy_handler)
        req = Request(url)
        with opener.open(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        # 代理不可用时直连
        req = Request(url)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.read().decode("utf-8")


def get_latest() -> tuple[str, str]:
    """返回 (timeid, timestamp)"""
    data = json.loads(api_get("getlatest"))
    return data[0], data[1]


def get_closest_time(timestamp: str) -> tuple[str, str]:
    """返回最近的 (timeid, timestamp)"""
    data = json.loads(api_get("getclosesttime", {"timestamp": encrypt(timestamp)}))
    return data[0], data[1]


def get_items(timeid: str) -> list:
    """获取热搜列表，格式: [[name, downtime, uptime, hotindex], ...]"""
    raw = api_get("currentitems", {"timeid": encrypt(timeid)})
    return decrypt(raw)


def search_topic(keyword: str) -> list:
    """搜索话题，返回 [[name, lastTime], ...]"""
    raw = api_get("search", {"searchstr": encrypt(keyword)})
    return json.loads(raw)


def get_topic_detail(name: str) -> dict:
    """获取话题详情: {timeId, timeStamp, rank}"""
    data = api_get("gettimeidbyname", {"name": encrypt(name)})
    return json.loads(data)


def format_items(items: list) -> str:
    """格式化热搜列表输出"""
    lines = []
    for i, item in enumerate(items, 1):
        name, downtime, uptime, hotindex = item
        uptime = uptime.replace(".0", "")
        downtime = downtime.replace(".0", "")
        heat = int(hotindex)
        if heat >= 1_000_000:
            heat_str = f"{heat/1_000_000:.1f}M"
        elif heat >= 1_000:
            heat_str = f"{heat/1_000:.0f}K"
        else:
            heat_str = str(heat)
        lines.append(f"{i:>2}. {name}")
        lines.append(f"    热度: {heat_str}  |  {uptime} → {downtime}")
    return "\n".join(lines)


def format_search_results(results: list) -> str:
    """格式化搜索结果输出"""
    lines = []
    now = datetime.now()
    for i, item in enumerate(results, 1):
        name, last_time = item[0], item[1].replace(".0", "")
        last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
        days = (now - last_dt).days
        if days == 0:
            ago = "今天"
        elif days == 1:
            ago = "昨天"
        elif days < 30:
            ago = f"{days}天前"
        elif days < 365:
            ago = f"{days // 30}个月前"
        else:
            ago = f"{days // 365}年前"
        lines.append(f"{i:>2}. {name}")
        lines.append(f"    最后上榜: {last_time}  ({ago})")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="微博热搜历史数据抓取")
    parser.add_argument("date", nargs="?", help="日期 (YYYY-MM-DD), 默认最新")
    parser.add_argument("--search", "-s", type=str, help="搜索话题关键词")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--top", type=int, default=50, help="显示前 N 条 (默认 50)")
    args = parser.parse_args()

    try:
        # ---- 搜索模式 ----
        if args.search:
            print(f"# 搜索: {args.search}", file=sys.stderr)
            results = search_topic(args.search)[: args.top]
            if args.json:
                out = []
                for i, item in enumerate(results, 1):
                    out.append({
                        "rank": i,
                        "name": item[0],
                        "lastTime": item[1].replace(".0", ""),
                    })
                print(json.dumps(out, ensure_ascii=False, indent=2))
            else:
                print(f"共找到 {len(results)} 条:\n")
                print(format_search_results(results))
            return

        # ---- 榜单模式 ----
        if args.date:
            timestamp = f"{args.date} 12:00:00"
            timeid, actual_ts = get_closest_time(timestamp)
            print(f"# 微博热搜 - {actual_ts.replace('.0', '')}", file=sys.stderr)
        else:
            timeid, actual_ts = get_latest()
            print(f"# 微博热搜 - {actual_ts.replace('.0', '')}", file=sys.stderr)

        items = get_items(timeid)[: args.top]

        if args.json:
            result = []
            for i, item in enumerate(items, 1):
                result.append({
                    "rank": i,
                    "name": item[0],
                    "uptime": item[2].replace(".0", ""),
                    "downtime": item[1].replace(".0", ""),
                    "hotindex": int(item[3]),
                })
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_items(items))

    except URLError as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
