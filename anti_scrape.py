#!/usr/bin/env python3
"""
反爬取中间件 — 防火墙
- IP 限流 (滑动窗口)
- 机器人 UA 拦截
- 可疑行为日志
"""
import time
import threading
from collections import defaultdict

# ============================================================
# 配置
# ============================================================
MAX_RPM = 8           # 每个 IP 每分钟最多请求数
BAN_SECONDS = 300     # 超限后封禁 5 分钟
TRUSTED_DOMAIN = "weibo-hot-dashboard-production.up.railway.app"

# 常见爬虫/扫描器 UA 特征
BOT_PATTERNS = [
    "python-requests", "python-urllib", "curl/", "wget/",
    "scrapy", "go-http-client", "Java/", "okhttp", "Apache-HttpClient",
    "nmap", "masscan", "zgrab", "sqlmap", "nikto", "nessus",
    "axios/", "node-fetch", "got (https", "libwww-perl",
    "l9tcpid", "l9explore", "grpc-go",
]

lock = threading.Lock()
hit_log = defaultdict(list)   # ip -> [timestamps]
ban_list = {}                  # ip -> unban_time


def is_bot_ua(user_agent):
    """检查 User-Agent 是否匹配已知爬虫特征"""
    if not user_agent:
        return True  # 空 UA 视为机器人
    ua_lower = user_agent.lower()
    for pat in BOT_PATTERNS:
        if pat.lower() in ua_lower:
            return True
    return False


def check_rate_limit(client_ip):
    """滑动窗口限流。返回 (allowed: bool, reason: str)"""
    now = time.time()

    with lock:
        # 检查是否在封禁期
        if client_ip in ban_list:
            if now < ban_list[client_ip]:
                return False, "banned"
            else:
                del ban_list[client_ip]

        # 清理过期记录
        window = now - 60
        hits = [t for t in hit_log.get(client_ip, []) if t > window]
        hit_log[client_ip] = hits

        # 检查是否超限
        if len(hits) >= MAX_RPM:
            ban_list[client_ip] = now + BAN_SECONDS
            return False, f"rate_limited({len(hits)}/min)"

        # 记录本次请求
        hits.append(now)
        return True, "ok"


def firewall(client_ip, user_agent, path, referer=""):
    """
    反爬防火墙入口。
    返回 (allowed: bool, http_code: int, reason: str)
    """
    # 静态资源 + HTML 页面放行（不检查 Referer）
    if path.endswith((".css", ".js", ".ico", ".png", ".svg", ".woff2", ".html")) or path == "/":
        return True, 200, "static"

    # 健康检查放行
    if path == "/health":
        return True, 200, "health"

    # API 请求 Referer 检查：只允许自己的域名访问
    if path.startswith("/api/"):
        ok_domains = [TRUSTED_DOMAIN, "localhost", "127.0.0.1"]
        ref_ok = False
        if referer:
            for d in ok_domains:
                if d in referer:
                    ref_ok = True
                    break
        # 也允许无 Referer 的请求（本地开发 / 直接访问）
        if not referer:
            ref_ok = True
        if not ref_ok:
            return False, 403, "bad_referer"

    # 机器人 UA 拦截
    if is_bot_ua(user_agent):
        return False, 403, "bot_ua"

    # IP 限流
    allowed, reason = check_rate_limit(client_ip)
    if not allowed:
        return False, 429 if reason.startswith("rate") else 403, reason

    return True, 200, "ok"
