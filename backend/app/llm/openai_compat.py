"""OpenAI 兼容协议。

适用于 OpenAI、DeepSeek、通义千问、Kimi、智谱、硅基流动、OpenRouter、Ollama、LM Studio、vLLM、llama.cpp 等。
"""

from __future__ import annotations

import json

import openai

from app.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMError,
    ProviderConfig,
    ToolCall,
    is_local_endpoint,
    parse_arguments,
)

# 原样透传给接口的可选参数（只在选手参数中显式配置时才发送）
_PASSTHROUGH = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "reasoning_effort",
                "frequency_penalty", "presence_penalty")


class OpenAICompatClient:
    def __init__(self, cfg: ProviderConfig):
        http_client = openai.DefaultAsyncHttpxClient(trust_env=False) if is_local_endpoint(cfg.base_url) else None
        self._client = openai.AsyncOpenAI(
            api_key=cfg.api_key or "not-needed",   # 本地模型通常不校验密钥
            base_url=cfg.base_url,
            http_client=http_client,
            max_retries=1,
        )

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list(timeout=20)
        except openai.APIError as e:
            raise LLMError(f"获取模型列表失败：{e}") from e
        return [m.id for m in page.data]

    async def chat(self, req: ChatRequest) -> ChatResponse:
        kwargs: dict = {
            "model": req.model,
            "messages": [{"role": "system", "content": req.system}, *[_to_openai(m) for m in req.messages]],
            "timeout": req.timeout_s,
        }
        for key in _PASSTHROUGH:
            if key in req.params:
                kwargs[key] = req.params[key]
        if req.tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                 "parameters": t.parameters}} for t in req.tools]
        elif req.json_mode:     # JSON 模式与工具调用同时开启时部分服务会报错，工具模式下不启用
            kwargs["response_format"] = {"type": "json_object"}
        if req.params.get("extra_body"):
            kwargs["extra_body"] = req.params["extra_body"]
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.APITimeoutError as e:
            raise LLMError(f"请求超时（{req.timeout_s:.0f} 秒）") from e
        except openai.AuthenticationError as e:
            raise LLMError(f"鉴权失败，请检查 API Key：{e.message}") from e
        except openai.NotFoundError as e:
            raise LLMError(f"模型或接口地址不存在：{e.message}") from e
        except openai.RateLimitError as e:
            raise LLMError(f"触发限流：{e.message}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"接口返回错误（{e.status_code}）：{e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"无法连接模型服务：{e}") from e

        if not resp.choices:
            raise LLMError("模型没有返回任何结果")
        choice = resp.choices[0]
        message = choice.message
        # DeepSeek、llama.cpp、vLLM 等把推理过程放在非标准字段 reasoning_content 中
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")
        usage = resp.usage
        calls = [ToolCall(c.id or f"call_{i}", c.function.name, parse_arguments(c.function.arguments),
                          c.function.arguments or "")
                 for i, c in enumerate(message.tool_calls or []) if getattr(c, "function", None)]
        return ChatResponse(
            content=message.content or "",
            reasoning=reasoning if isinstance(reasoning, str) else None,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            finish_reason=choice.finish_reason,
            model=resp.model,
            tool_calls=calls,
        )


def _to_openai(m: dict) -> dict:
    if m["role"] == "tool":
        return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
    if m["role"] == "assistant" and m.get("tool_calls"):
        return {"role": "assistant", "content": m.get("content") or None, "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments, ensure_ascii=False)}}
            for c in m["tool_calls"]]}
    return {"role": m["role"], "content": m["content"]}
