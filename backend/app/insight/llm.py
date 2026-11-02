"""AI 洞察的设置与模型调用。

设置按用户保存（app_settings 键 insight）：{provider_id, model, weekly, deviation}。没有选择模型时沿用自选股监控的模型
（自选股监控又默认沿用 AI 复核的模型）。只能使用自己配置的模型。
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.insight.data import InsightError
from app.models import DEFAULT_USER_ID
from app.models.settings import AppSettingRow, user_key

SETTINGS_KEY = "insight"
TIMEOUT_S = 300


def settings(s: Session, uid: int = DEFAULT_USER_ID) -> dict:
    from app.models import LLMProviderRow
    from app.watch import service as watch

    row = s.get(AppSettingRow, user_key(SETTINGS_KEY, uid))
    cfg = dict(row.value or {}) if row else {}
    pid = cfg.get("provider_id") or watch.settings(s, uid).get("provider_id")
    prov = s.get(LLMProviderRow, pid) if pid else None
    if prov is None or prov.user_id != uid:
        pid = None
    return {"provider_id": pid, "model": cfg.get("model") if cfg.get("provider_id") else None,
            "weekly": cfg.get("weekly", True), "deviation": cfg.get("deviation", True),
            "own_model": bool(cfg.get("provider_id"))}


def set_settings(s: Session, uid: int = DEFAULT_USER_ID, **kw) -> dict:
    from app.models import LLMProviderRow

    key = user_key(SETTINGS_KEY, uid)
    row = s.get(AppSettingRow, key) or AppSettingRow(key=key)
    cur = dict(row.value or {})
    if "provider_id" in kw:
        pid = kw["provider_id"]
        if pid is not None:
            prov = s.get(LLMProviderRow, pid)
            if prov is None or prov.user_id != uid:
                raise InsightError("模型供应商不存在（只能使用自己配置的模型）")
        cur["provider_id"], cur["model"] = pid, kw.get("model") or None
    for k in ("weekly", "deviation"):
        if kw.get(k) is not None:
            cur[k] = bool(kw[k])
    row.value = cur
    s.add(row)
    s.flush()
    return settings(s, uid)


def client_for(s: Session, uid: int = DEFAULT_USER_ID):
    """(client, model, params)；没有可用的模型时抛出 InsightError。"""
    from app.arena.strategy import llm_config_for
    from app.llm import create_client
    from app.llm.base import is_local_endpoint
    from app.models import LLMProviderRow

    cfg = settings(s, uid)
    if not cfg["provider_id"]:
        raise InsightError("还没有可用的模型：先在「配置中心 → 模型供应商」添加，再在 AI 助手页选择")
    provider = s.get(LLMProviderRow, cfg["provider_id"])
    conf, model, _, _ = llm_config_for(s, provider.id, cfg["model"])
    params: dict = {"temperature": 0.3}
    if is_local_endpoint(provider.base_url):
        params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return create_client(conf), model, params


async def write(client, model: str, params: dict, system: str, user: str, timeout_s: float = TIMEOUT_S) -> dict:
    """让模型写一段文字（不要求 JSON）。返回 {status, content, error, model, tokens_in, tokens_out}。"""
    from app.llm.base import ChatRequest, LLMError

    try:
        resp = await asyncio.wait_for(client.chat(ChatRequest(model=model, system=system,
                                                              messages=[{"role": "user", "content": user}],
                                                              params=params, timeout_s=timeout_s)), timeout=timeout_s)
    except TimeoutError:
        return {"status": "error", "content": "", "error": f"超过时限（{timeout_s:.0f} 秒）", "model": model,
                "tokens_in": 0, "tokens_out": 0}
    except LLMError as e:
        return {"status": "error", "content": "", "error": str(e), "model": model, "tokens_in": 0, "tokens_out": 0}
    content = (resp.content or "").strip()
    return {"status": "ok" if content else "error", "content": content,
            "error": None if content else "模型没有返回内容", "model": resp.model or model,
            "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out}
