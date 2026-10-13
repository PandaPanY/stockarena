"""网络相关设置。

本机可能配置了系统代理（翻墙代理）。国内行情网站经代理访问会被断开或限流，因此行情域名需要直连。
AKShare 内部使用 requests，requests 遵循 NO_PROXY 环境变量，所以在进程启动时把这些域名追加到 NO_PROXY。
"""

from __future__ import annotations

import os

DEFAULT_DIRECT_HOSTS = [
    "sina.com.cn",
    "sinajs.cn",
    "eastmoney.com",
    "gtimg.cn",
    "qq.com",
    "csindex.com.cn",
    "sse.com.cn",
    "szse.cn",
    "cninfo.com.cn",      # 巨潮资讯公告（AI 复核、公告研究）
]


def apply_direct_hosts(hosts: list[str]) -> None:
    """把域名追加到 NO_PROXY / no_proxy（去重，保留原有配置）。"""
    if not hosts:
        return
    for var in ("NO_PROXY", "no_proxy"):
        existing = [h.strip() for h in os.environ.get(var, "").split(",") if h.strip()]
        merged = existing + [h for h in hosts if h not in existing]
        os.environ[var] = ",".join(merged)
