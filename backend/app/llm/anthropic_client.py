"""Anthropic 协议（Claude）。

注意（2026）：新一代 Claude 模型（Opus 4.7 及以后）不接受 temperature / top_p，思考深度用 effort 控制，
因此这些参数只在选手配置中显式填写时才发送。
"""

from __future__ import annotations

import anthropic

from app.llm.base import ChatRequest, ChatResponse, LLMError, ProviderConfig, ToolCall, is_local_endpoint

DEFAULT_MAX_TOKENS = 16000


class AnthropicClient:
    def __init__(self, cfg: ProviderConfig):
        http_client = (anthropic.DefaultAsyncHttpxClient(trust_env=False)
                       if is_local_endpoint(cfg.base_url) else None)
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key, base_url=cfg.base_url or None, http_client=http_client, max_retries=1)

    async def chat(self, req: ChatRequest) -> ChatResponse:
        p = req.params
        kwargs: dict = {
            "model": req.model,
            "max_tokens": p.get("max_tokens", DEFAULT_MAX_TOKENS),
            "system": req.system,
            "messages": _to_anthropic(req.messages),
            "timeout": req.timeout_s,
        }
        if req.tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in req.tools]
        for key in ("temperature", "top_p", "thinking"):
            if key in p:
                kwargs[key] = p[key]
        if "effort" in p:
            kwargs["output_config"] = {"effort": p["effort"]}
        try:
            if p.get("fallbacks"):
                # 服务端拒答回退（beta）：被安全分类器拒答时由服务端自动换模型重试
                beta = p.get("fallbacks_beta", "server-side-fallback-2026-07-01")
                resp = await self._client.beta.messages.create(**kwargs, fallbacks=p["fallbacks"], betas=[beta])
            else:
                resp = await self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise LLMError(f"请求超时（{req.timeout_s:.0f} 秒）") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("鉴权失败，请检查 API Key") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"模型不存在：{e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"触发限流：{e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"接口返回错误（{e.status_code}）：{e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"无法连接 Anthropic 服务：{e}") from e

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise LLMError(f"模型拒绝回答（{getattr(detail, 'category', None) or '未说明原因'}）")
        texts = [b.text for b in resp.content if b.type == "text"]
        thoughts = [b.thinking for b in resp.content if b.type == "thinking" and b.thinking]
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        return ChatResponse(
            content="".join(texts),
            reasoning="\n".join(thoughts) or None,
            tokens_in=resp.usage.input_tokens,
            tokens_out=resp.usage.output_tokens,
            finish_reason=resp.stop_reason,
            model=resp.model,
            tool_calls=calls,
        )


def _to_anthropic(messages: list[dict]) -> list[dict]:
    """工具调用转换为 tool_use 块；连续的工具结果合并为一条 user 消息中的多个 tool_result 块。"""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out
