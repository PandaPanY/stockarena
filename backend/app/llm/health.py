"""模型供应商的健康状态：记录每个供应商连续失败的次数，连续失败 FAIL_ALERT 次时推送「模型不可用」提醒，
恢复后再推送一次。

所有通过 create_client 创建、带 provider_id 的客户端都会被 TrackedClient 包一层，所以 AI 优化、AI 复核、自选股监控、
一句话创建策略、AI 对比选手的调用都会计入。只有模型服务返回的错误（LLMError：连不上、鉴权失败、超额等）算失败；
调用方自己的超时取消不算（不知道是模型坏了还是请求太大）。

状态存在 app_settings（键 llm.health.<供应商 id>），重启后保留；成功的调用只在状态变化或距上次记录超过 10 分钟时写库。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from app.core.timeutils import utcnow
from app.llm.base import ChatRequest, ChatResponse, LLMClient, LLMError

logger = logging.getLogger(__name__)

FAIL_ALERT = 3            # 连续失败几次推送提醒
OK_WRITE_EVERY = 600.0    # 一直正常时，最近成功时间最多每 10 分钟写一次库

_lock = threading.Lock()
_last_ok_write: dict[int, float] = {}


def _key(provider_id: int) -> str:
    return f"llm.health.{provider_id}"


def get(s, provider_id: int) -> dict:
    from app.models import AppSettingRow

    row = s.get(AppSettingRow, _key(provider_id))
    return dict(row.value or {}) if row else {}


def _save(s, provider_id: int, value: dict) -> None:
    from app.models import AppSettingRow

    row = s.get(AppSettingRow, _key(provider_id)) or AppSettingRow(key=_key(provider_id))
    row.value = value
    s.add(row)


def record(provider_id: int, ok: bool, error: str | None = None) -> None:
    """记录一次调用结果（同步，在线程里调用）。达到连续失败次数或失败后恢复时推送提醒。"""
    from app.db.session import get_sessionmaker
    from app.models import LLMProviderRow

    now = time.monotonic()
    if ok:
        with _lock:
            recent = now - _last_ok_write.get(provider_id, -OK_WRITE_EVERY) < OK_WRITE_EVERY
    msg = None
    with get_sessionmaker()() as s:
        cur = get(s, provider_id)
        if ok and recent and not cur.get("fails"):
            return
        stamp = utcnow().isoformat(timespec="seconds")
        if ok:
            was_down = bool(cur.get("alerted"))
            new = {**cur, "fails": 0, "alerted": False, "last_ok_at": stamp}
            with _lock:
                _last_ok_write[provider_id] = now
        else:
            fails = int(cur.get("fails") or 0) + 1
            new = {**cur, "fails": fails, "last_error": (error or "")[:500], "last_failure_at": stamp,
                   "down_since": cur.get("down_since") if cur.get("fails") else stamp}
            was_down = False
        prov = s.get(LLMProviderRow, provider_id)
        if prov is not None:
            if not ok and new["fails"] >= FAIL_ALERT and not cur.get("alerted"):
                new["alerted"] = True
                msg = _down_message(s, prov, new)
            elif ok and was_down:
                msg = _up_message(prov, cur)
        if ok:
            new.pop("down_since", None)
        _save(s, provider_id, new)
        s.commit()
    if msg is not None:
        try:
            from app.notify import service as notify

            notify.deliver(msg)
        except Exception:
            logger.exception("模型健康提醒发送失败")


def _down_message(s, prov, state: dict):
    from app.llm.usage import usage_lines, usages
    from app.notify.service import Message, settings_link

    lines = [f"**{prov.name}** 连续 {state['fails']} 次调用失败，最近一次错误：", "",
             f"> {state.get('last_error') or '未知'}", "", "用到这个模型的地方：", ""]
    lines += usage_lines(usages(s, prov.id)) or ["- （目前没有功能在用）"]
    lines += ["", "模型无法恢复时：在「配置中心 → 模型供应商」点「用到的地方」，可以把 AI 优化、自选股监控等"
              "一次换成其他模型；AI 交易复核需要在组合页单独「更换复核模型」（会开始新的一段评估）。"]
    link = settings_link("providers")
    if link:
        lines.append(link)
    return Message("errors", f"llm-down:{prov.id}:{state.get('down_since')}",
                   f"【模型不可用】{prov.name}：连续 {state['fails']} 次调用失败", "\n".join(lines),
                   user_id=prov.user_id)


def _up_message(prov, prev: dict):
    from app.notify.service import Message

    body = f"**{prov.name}** 自 {prev.get('down_since') or '-'}（UTC）起连续失败 {prev.get('fails')} 次，现已恢复。"
    return Message("errors", f"llm-up:{prov.id}:{prev.get('down_since')}", f"【模型恢复】{prov.name} 调用恢复正常",
                   body, user_id=prov.user_id)


class TrackedClient:
    """包装模型客户端：每次调用后记录该供应商的健康状态。"""

    def __init__(self, inner: LLMClient, provider_id: int):
        self._inner, self._provider_id = inner, provider_id

    async def chat(self, req: ChatRequest) -> ChatResponse:
        try:
            resp = await self._inner.chat(req)
        except LLMError as e:
            await asyncio.to_thread(_safe_record, self._provider_id, False, str(e))
            raise
        await asyncio.to_thread(_safe_record, self._provider_id, True)
        return resp

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _safe_record(provider_id: int, ok: bool, error: str | None = None) -> None:
    try:
        record(provider_id, ok, error)
    except Exception:
        logger.exception("记录模型健康状态失败")
