"""启动检查：已保存的 API Key 与推送密钥能否用当前主密钥解密。

主密钥（data/secret.key）被删除或换成别的文件后，程序会生成新密钥，旧密文全部无法解密。
这里在启动时统计一次并写进日志，提醒到页面上重新填写，而不是等到调用模型或推送时才失败。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

logger = logging.getLogger(__name__)


def check_encrypted_secrets() -> int:
    """返回无法解密的条数（0 表示正常）。"""
    from app.arena.deps import secret_box
    from app.db.session import get_sessionmaker
    from app.models import LLMProviderRow
    from app.models.notify import NotifyChannelRow

    box = secret_box()
    bad: list[str] = []
    with get_sessionmaker()() as s:
        for name, enc in s.execute(select(LLMProviderRow.name, LLMProviderRow.api_key_enc)):
            if enc and box.try_decrypt(enc) is None:
                bad.append(f"AI 模型「{name}」")
        for name, enc in s.execute(select(NotifyChannelRow.name, NotifyChannelRow.secret_enc)):
            if enc and box.try_decrypt(enc) is None:
                bad.append(f"推送通道「{name}」")
    if bad:
        logger.error("%d 个密钥无法用当前主密钥解密（主密钥可能被删除或替换过），请在「配置中心」重新填写：%s",
                     len(bad), "、".join(bad))
    return len(bad)
