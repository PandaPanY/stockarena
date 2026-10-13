"""调用大模型获取决策：解析失败时带着错误信息重试一次（PRD 3.4）。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.parse import DecisionParseError, ParsedDecision, parse_decision
from app.llm import ChatRequest, LLMClient, LLMError, ToolSpec

MAX_ATTEMPTS = 2


@dataclass
class DecisionOutcome:
    status: str                       # ok / invalid / timeout / error
    parsed: Any = None                # 决策为 ParsedDecision，复盘为 ParsedReflection
    raw: str | None = None
    reasoning: str | None = None
    error: str | None = None
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    model: str | None = None
    trace: list[dict] | None = None   # 工具模式：每次工具调用的记录


async def run_llm_decision(
    client: LLMClient, *, model: str, system: str, user: str, params: dict, json_mode: bool,
    output_mode: str, timeout_s: float, market: str = "CN", restore: Callable[[str], str] | None = None,
) -> DecisionOutcome:
    def parse(text: str) -> ParsedDecision:
        return parse_decision(restore(text) if restore else text, output_mode, market)

    return await run_llm_json(client, model=model, system=system, user=user, params=params, json_mode=json_mode,
                              timeout_s=timeout_s, parse=parse)


async def run_llm_json(
    client: LLMClient, *, model: str, system: str, user: str, params: dict, json_mode: bool, timeout_s: float,
    parse: Callable[[str], Any],
) -> DecisionOutcome:
    """请求模型输出 JSON 并解析；parse 抛出 DecisionParseError 时带着错误信息重试一次。"""
    out = DecisionOutcome(status="error")
    messages: list[dict] = [{"role": "user", "content": user}]
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                out.status, out.error = "timeout", f"超过时限（{timeout_s:.0f} 秒）"
                break
            out.attempts = attempt
            try:
                resp = await asyncio.wait_for(
                    client.chat(ChatRequest(model=model, system=system, messages=messages, params=params,
                                            json_mode=json_mode, timeout_s=remaining)),
                    timeout=remaining,
                )
            except TimeoutError:
                out.status, out.error = "timeout", f"超过时限（{timeout_s:.0f} 秒）"
                break
            except LLMError as e:
                out.status, out.error = "error", str(e)
                break
            out.tokens_in += resp.tokens_in
            out.tokens_out += resp.tokens_out
            out.raw, out.reasoning, out.model = resp.content, resp.reasoning, resp.model or model
            try:
                out.parsed = parse(resp.content)
                out.status, out.error = "ok", None
                break
            except DecisionParseError as e:
                truncated = " 输出可能因长度上限被截断，请精简内容。" if resp.finish_reason == "length" else ""
                out.status, out.error = "invalid", f"第 {attempt} 次输出无法解析：{e}"
                messages = [*messages, {"role": "assistant", "content": resp.content or "(空)"},
                            {"role": "user",
                             "content": f"你的输出无法解析：{e}。{truncated}请只输出符合格式要求的 JSON 对象。"}]
    finally:
        out.latency_ms = int((time.monotonic() - started) * 1000)
    return out


MAX_SUBMIT_ATTEMPTS = 2      # submit_decision 参数无法解析时允许修正的次数
MAX_TURNS_SLACK = 4          # 除工具调用次数外允许的额外对话轮数（提醒提交、修正参数）
SUBMIT = "submit_decision"


async def run_llm_tools(
    client: LLMClient, *, model: str, system: str, user: str, params: dict, timeout_s: float,
    tools: list[ToolSpec], execute: Callable[[str, dict], Awaitable[str]], parse: Callable[[str], Any],
    max_calls: int,
) -> DecisionOutcome:
    """工具模式：模型可多次调用查询工具，最后调用 submit_decision（或直接输出决策 JSON）结束。

    - 查询工具调用次数达到 max_calls 后，只保留 submit_decision 并要求立即提交
    - submit_decision 参数无法解析时，把错误返回给模型修正，最多 MAX_SUBMIT_ATTEMPTS 次
    - 每次调用（参数、结果摘要、耗时）记录在 trace 中
    """
    out = DecisionOutcome(status="error", trace=[])
    messages: list[dict] = [{"role": "user", "content": user}]
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    calls = submits = nudges = 0
    submit_only = [t for t in tools if t.name == SUBMIT]
    try:
        for _turn in range(max_calls + MAX_TURNS_SLACK + MAX_SUBMIT_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                out.status, out.error = "timeout", f"超过时限（{timeout_s:.0f} 秒）"
                return out
            out.attempts += 1
            offered = tools if calls < max_calls else submit_only
            try:
                resp = await asyncio.wait_for(
                    client.chat(ChatRequest(model=model, system=system, messages=messages, params=params,
                                            timeout_s=remaining, tools=offered)), timeout=remaining)
            except TimeoutError:
                out.status, out.error = "timeout", f"超过时限（{timeout_s:.0f} 秒）"
                return out
            except LLMError as e:
                out.status, out.error = "error", str(e)
                return out
            out.tokens_in += resp.tokens_in
            out.tokens_out += resp.tokens_out
            out.model = resp.model or model
            if resp.reasoning:
                out.reasoning = "\n\n".join(x for x in (out.reasoning, resp.reasoning) if x)
            if not resp.tool_calls:
                # 没有调用工具：尝试把正文当作决策 JSON（兼容不按要求调用 submit_decision 的模型）
                out.raw = resp.content
                try:
                    out.parsed = parse(resp.content)
                    out.status, out.error = "ok", None
                    return out
                except DecisionParseError as e:
                    nudges += 1
                    out.status, out.error = "invalid", f"没有调用 submit_decision，正文也无法解析：{e}"
                    if nudges > 1:
                        return out
                    messages += [{"role": "assistant", "content": resp.content or "(空)"},
                                 {"role": "user", "content": "请调用 submit_decision 工具提交本轮决策。"}]
                    continue
            messages.append({"role": "assistant", "content": resp.content, "tool_calls": resp.tool_calls})
            limit_note = False
            for call in resp.tool_calls:
                t0 = time.monotonic()
                if call.name == SUBMIT:
                    submits += 1
                    raw = call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)
                    out.raw = raw
                    try:
                        out.parsed = parse(json.dumps(call.arguments, ensure_ascii=False) if call.arguments else raw)
                        out.status, out.error = "ok", None
                        out.trace.append({"tool": SUBMIT, "args": call.arguments, "result": "已提交", "ms": 0})
                        return out
                    except DecisionParseError as e:
                        out.status, out.error = "invalid", f"submit_decision 参数无法解析：{e}"
                        result = f"提交失败：{e}。请修正参数后重新调用 submit_decision。"
                        if submits >= MAX_SUBMIT_ATTEMPTS:
                            out.trace.append({"tool": SUBMIT, "args": call.arguments, "result": result, "ms": 0})
                            return out
                elif calls >= max_calls:
                    result = f"已达到本轮工具调用上限（{max_calls} 次），请立即调用 submit_decision 提交决策。"
                    limit_note = True
                else:
                    calls += 1
                    result = await execute(call.name, call.arguments)
                out.trace.append({"tool": call.name, "args": call.arguments, "result": result[:300],
                                  "ms": int((time.monotonic() - t0) * 1000)})
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
            if calls >= max_calls and not limit_note:
                messages.append({"role": "user", "content":
                                 f"已达到本轮工具调用上限（{max_calls} 次），请立即调用 submit_decision 提交决策。"})
        out.status, out.error = "invalid", "对话轮数过多仍未提交决策"
        return out
    finally:
        out.latency_ms = int((time.monotonic() - started) * 1000)
