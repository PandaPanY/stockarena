"""模型供应商接口。"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id
from app.arena.deps import secret_box
from app.core.crypto import UNDECRYPTABLE, mask_key
from app.db.session import get_db
from app.llm import PROTOCOLS, ChatRequest, LLMError, ProviderConfig, create_client
from app.models import AgentRow, LLMProviderRow

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 常用供应商模板（价格为占位 0，请按官网实际价格填写）
PRESETS = [
    {"name": "OpenAI", "protocol": "openai_compatible", "base_url": "https://api.openai.com/v1", "default_model": ""},
    {"name": "DeepSeek", "protocol": "openai_compatible", "base_url": "https://api.deepseek.com/v1",
     "default_model": "deepseek-chat"},
    {"name": "通义千问（DashScope）", "protocol": "openai_compatible",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-plus"},
    {"name": "Kimi（Moonshot）", "protocol": "openai_compatible", "base_url": "https://api.moonshot.cn/v1",
     "default_model": ""},
    {"name": "智谱 GLM", "protocol": "openai_compatible", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "default_model": ""},
    {"name": "硅基流动", "protocol": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1",
     "default_model": ""},
    {"name": "OpenRouter", "protocol": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
     "default_model": ""},
    {"name": "Anthropic Claude", "protocol": "anthropic", "base_url": "", "default_model": "claude-opus-5-5",
     "price_in_per_m": "4", "price_out_per_m": "20"},
    {"name": "Google Gemini", "protocol": "gemini", "base_url": "", "default_model": ""},
    {"name": "Ollama（本机）", "protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
     "default_model": ""},
    {"name": "LM Studio（本机）", "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1",
     "default_model": ""},
    {"name": "llama.cpp / vLLM（局域网）", "protocol": "openai_compatible", "base_url": "http://192.168.x.x:port/v1",
     "default_model": ""},
]


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    protocol: str
    base_url: str | None = None
    api_key: str | None = Field(None, description="为 null 时保持不变；空字符串表示清除")
    default_model: str = ""
    timeout_s: int = Field(600, ge=10, le=3600)
    price_in_per_m: Decimal = Decimal("0")
    price_out_per_m: Decimal = Decimal("0")
    supports_json_mode: bool = True
    knowledge_cutoff: date | None = None


def to_dict(p: LLMProviderRow) -> dict:
    key = secret_box().try_decrypt(p.api_key_enc)
    broken = bool(p.api_key_enc) and key is None
    return {
        "id": p.id, "name": p.name, "protocol": p.protocol, "base_url": p.base_url, "default_model": p.default_model,
        "timeout_s": p.timeout_s, "price_in_per_m": format(p.price_in_per_m, "f"),
        "price_out_per_m": format(p.price_out_per_m, "f"), "supports_json_mode": p.supports_json_mode,
        "knowledge_cutoff": str(p.knowledge_cutoff) if p.knowledge_cutoff else None,
        "has_api_key": bool(p.api_key_enc), "api_key_masked": "（无法解密，请重新填写）" if broken else mask_key(key),
        "api_key_error": UNDECRYPTABLE if broken else None,
    }


def _get(db: Session, provider_id: int, user_id: int) -> LLMProviderRow:
    p = db.get(LLMProviderRow, provider_id)
    if p is None or p.user_id != user_id:
        raise HTTPException(404, "供应商不存在")
    return p


def _apply(p: LLMProviderRow, body: ProviderIn) -> None:
    if body.protocol not in PROTOCOLS:
        raise HTTPException(400, f"协议只能是 {', '.join(PROTOCOLS)}")
    p.name, p.protocol, p.base_url = body.name, body.protocol, (body.base_url or None)
    p.default_model, p.timeout_s = body.default_model, body.timeout_s
    p.price_in_per_m, p.price_out_per_m = body.price_in_per_m, body.price_out_per_m
    p.supports_json_mode, p.knowledge_cutoff = body.supports_json_mode, body.knowledge_cutoff
    if body.api_key is not None:
        p.api_key_enc = secret_box().encrypt(body.api_key) if body.api_key else None


def _config(p: LLMProviderRow) -> ProviderConfig:
    key = secret_box().decrypt(p.api_key_enc) if p.api_key_enc else None
    return ProviderConfig(p.protocol, p.base_url, key, p.price_in_per_m, p.price_out_per_m)


@router.get("/presets")
def presets() -> list[dict]:
    return PRESETS


@router.get("")
def list_providers(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    return [to_dict(p) for p in db.scalars(select(LLMProviderRow).where(LLMProviderRow.user_id == user_id)
                                           .order_by(LLMProviderRow.id))]


@router.post("")
def create_provider(body: ProviderIn, db: Session = Depends(get_db),
                    user_id: int = Depends(current_user_id)) -> dict:
    p = LLMProviderRow(user_id=user_id)
    _apply(p, body)
    db.add(p)
    db.commit()
    return to_dict(p)


@router.put("/{provider_id}")
def update_provider(provider_id: int, body: ProviderIn, db: Session = Depends(get_db),
                    user_id: int = Depends(current_user_id)) -> dict:
    p = _get(db, provider_id, user_id)
    _apply(p, body)
    db.commit()
    return to_dict(p)


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    p = _get(db, provider_id, user_id)
    if db.scalars(select(AgentRow).where(AgentRow.provider_id == p.id)).first():
        raise HTTPException(400, "有选手正在使用该供应商，无法删除")
    db.delete(p)
    db.commit()
    return {"ok": True}


class TestIn(BaseModel):
    model: str | None = None


@router.post("/{provider_id}/test")
async def test_provider(provider_id: int, body: TestIn | None = None, db: Session = Depends(get_db),
                        user_id: int = Depends(current_user_id)) -> dict:
    p = _get(db, provider_id, user_id)
    model = (body.model if body else None) or p.default_model
    if not model:
        raise HTTPException(400, "请先填写默认模型")
    started = time.monotonic()
    try:
        client = create_client(_config(p))
        resp = await client.chat(ChatRequest(model=model, system="你是连通性测试助手。",
                                             messages=[{"role": "user", "content": "请只回复：连接成功"}],
                                             params={"max_tokens": 2000}, timeout_s=120))
    except LLMError as e:
        return {"ok": False, "error": str(e), "latency_ms": int((time.monotonic() - started) * 1000)}
    return {"ok": True, "reply": resp.content[:200], "model": resp.model, "tokens_in": resp.tokens_in,
            "tokens_out": resp.tokens_out, "has_reasoning": bool(resp.reasoning),
            "latency_ms": int((time.monotonic() - started) * 1000)}


@router.get("/{provider_id}/models")
async def list_models(provider_id: int, db: Session = Depends(get_db),
                      user_id: int = Depends(current_user_id)) -> dict:
    p = _get(db, provider_id, user_id)
    if p.protocol != "openai_compatible":
        return {"models": [], "error": "该协议暂不支持自动获取模型列表，请手动填写"}
    from app.llm.openai_compat import OpenAICompatClient

    try:
        return {"models": await OpenAICompatClient(_config(p)).list_models()}
    except LLMError as e:
        return {"models": [], "error": str(e)}
