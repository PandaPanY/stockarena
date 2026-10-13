"""API Key 等敏感信息的加密存储。

主密钥保存在 data/secret.key（首次使用时自动生成，不进版本库）。
丢失主密钥后，已加密的 API Key 无法解密，需要在界面中重新填写。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

UNDECRYPTABLE = "无法解密：主密钥（数据目录下的 secret.key）与保存时不一致，可能被删除或替换过；请重新填写"


class MasterKeyError(RuntimeError):
    """主密钥文件存在但格式不对：启动时直接报错，避免之后每个用到密钥的接口都失败。"""


class SecretError(ValueError):
    """密文无法用当前主密钥解密。"""


class SecretBox:
    def __init__(self, key_file: Path):
        key = self._load_or_create_key(key_file)
        try:
            self._fernet = Fernet(key)
        except ValueError as e:
            raise MasterKeyError(
                f"主密钥文件 {key_file} 格式不对（应为 44 个字符的 Fernet 密钥）。"
                "如果有备份请恢复；没有备份时删除该文件，重启后会生成新密钥，已保存的 API Key 和推送密钥需要重新填写。"
            ) from e

    @staticmethod
    def _load_or_create_key(key_file: Path) -> bytes:
        if key_file.exists():
            return key_file.read_bytes().strip()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        with contextlib.suppress(OSError):         # 只有本用户可读写（Windows 上不起作用，忽略）
            key_file.chmod(0o600)
        logger.info("已生成新的主密钥 %s（用于加密 API Key，请和数据库一起备份）", key_file)
        return key

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise SecretError(UNDECRYPTABLE) from e

    def try_decrypt(self, token: str | None) -> str | None:
        """解密失败时返回 None（列表展示等场景用，不让一条坏数据拖垮整个接口）。"""
        if not token:
            return None
        try:
            return self.decrypt(token)
        except SecretError:
            return None


def mask_key(secret: str | None, visible: int = 4) -> str:
    """界面展示用：只保留末尾几位。"""
    if not secret:
        return ""
    if len(secret) <= visible:
        return "*" * len(secret)
    return "****" + secret[-visible:]
