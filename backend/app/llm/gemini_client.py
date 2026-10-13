"""Google Gemini 协议（google-genai SDK）。"""

from __future__ import annotations

from google import genai
from google.genai import errors, types

from app.llm.base import ChatRequest, ChatResponse, LLMError, ProviderConfig, ToolCall


class GeminiClient:
    def __init__(self, cfg: ProviderConfig):
        http_options = types.HttpOptions(base_url=cfg.base_url) if cfg.base_url else None
        self._client = genai.Client(api_key=cfg.api_key, http_options=http_options)

    async def chat(self, req: ChatRequest) -> ChatResponse:
        p = req.params
        config = types.GenerateContentConfig(
            system_instruction=req.system,
            temperature=p.get("temperature"),
            max_output_tokens=p.get("max_tokens"),
            response_mime_type="application/json" if req.json_mode and not req.tools else None,
            http_options=types.HttpOptions(timeout=int(req.timeout_s * 1000)),
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in req.tools])] if req.tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True) if req.tools else None,
        )
        contents = [_to_gemini(m) for m in req.messages]
        try:
            resp = await self._client.aio.models.generate_content(model=req.model, contents=contents, config=config)
        except errors.APIError as e:
            raise LLMError(f"Gemini 接口错误（{e.code}）：{e.message}") from e
        except Exception as e:  # 网络层异常类型随底层 HTTP 库变化
            raise LLMError(f"无法连接 Gemini 服务：{e}") from e
        usage = resp.usage_metadata
        calls = [ToolCall(fc.id or f"call_{i}", fc.name, dict(fc.args or {}))
                 for i, fc in enumerate(resp.function_calls or [])]
        text = "".join(p.text for c in (resp.candidates or [])[:1] for p in (c.content.parts or []) if p.text) \
            if resp.candidates and resp.candidates[0].content else ""
        return ChatResponse(
            tool_calls=calls,
            content=text,
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
            finish_reason=str(resp.candidates[0].finish_reason) if resp.candidates else None,
            model=req.model,
        )


def _to_gemini(m: dict) -> types.Content:
    if m["role"] == "tool":
        return types.Content(role="user", parts=[types.Part.from_function_response(
            name=m["name"], response={"result": m["content"]})])
    if m["role"] == "assistant":
        parts = [types.Part(text=m["content"])] if m.get("content") else []
        parts += [types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.arguments))
                  for c in m.get("tool_calls") or []]
        return types.Content(role="model", parts=parts)
    return types.Content(role="user", parts=[types.Part(text=m["content"])])
