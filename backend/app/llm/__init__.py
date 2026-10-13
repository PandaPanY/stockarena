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
