"""大模型调用的统一接口。各协议实现只负责一次请求，重试与解析由 agent 层负责。"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlparse


@dataclass
class ToolSpec:
    """提供给模型的工具（函数）定义，parameters 为 JSON Schema。"""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict                      # 解析后的参数；模型给出的参数不是合法 JSON 时为空，原文见 raw_arguments
    raw_arguments: str = ""


@dataclass
class ChatRequest:
    """messages 使用统一格式，由各协议实现转换：
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}   # tool_calls 可省略
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
    """

    model: str
    system: str
    messages: list[dict]
    params: dict = field(default_factory=dict)
    json_mode: bool = False
    timeout_s: float = 600
    tools: list[ToolSpec] | None = None


@dataclass
class ChatResponse:
    content: str
    reasoning: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str | None = None
    model: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_arguments(raw) -> dict:
    """工具参数：字符串按 JSON 解析，失败返回空 dict。"""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、拒答、超时等），message 为可展示的中文说明。"""


class LLMClient(Protocol):
    async def chat(self, req: ChatRequest) -> ChatResponse: ...


@dataclass(frozen=True)
class ProviderConfig:
    protocol: str                        # openai_compatible / anthropic / gemini
    base_url: str | None
    api_key: str | None
    price_in_per_m: Decimal = Decimal("0")
    price_out_per_m: Decimal = Decimal("0")


def is_local_endpoint(base_url: str | None) -> bool:
    """本机或局域网地址：调用时不走系统代理。"""
    if not base_url:
        return False
    host = urlparse(base_url).hostname or ""
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def calc_cost(cfg: ProviderConfig, tokens_in: int, tokens_out: int) -> Decimal:
    return (cfg.price_in_per_m * tokens_in + cfg.price_out_per_m * tokens_out) / Decimal(1_000_000)
