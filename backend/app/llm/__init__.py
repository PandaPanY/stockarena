"""大模型适配层：按协议创建客户端。"""

from __future__ import annotations

from app.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMClient,
    LLMError,
    ProviderConfig,
    ToolCall,
    ToolSpec,
    calc_cost,
)

PROTOCOLS = ("openai_compatible", "anthropic", "gemini")


def create_client(cfg: ProviderConfig) -> LLMClient:
    client = _create(cfg)
    if cfg.provider_id is not None:
        # 记录每个供应商的调用成败，连续失败时推送提醒（app/llm/health.py）
        from app.llm.health import TrackedClient

        return TrackedClient(client, cfg.provider_id)
    return client


def _create(cfg: ProviderConfig) -> LLMClient:
    # 延迟导入：只加载实际用到的 SDK
    if cfg.protocol == "openai_compatible":
        from app.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(cfg)
    if cfg.protocol == "anthropic":
        from app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(cfg)
    if cfg.protocol == "gemini":
        from app.llm.gemini_client import GeminiClient

        return GeminiClient(cfg)
    raise ValueError(f"不支持的协议：{cfg.protocol}")


__all__ = ["PROTOCOLS", "ChatRequest", "ChatResponse", "LLMClient", "LLMError", "ProviderConfig", "ToolCall",
           "ToolSpec", "calc_cost", "create_client"]
