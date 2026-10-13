from __future__ import annotations

import pytest

from app.core.crypto import SecretBox, mask_key
from app.core.logsetup import mask_secrets


def test_secretbox_roundtrip_and_key_persisted(tmp_path):
    key_file = tmp_path / "secret.key"
    box = SecretBox(key_file)
    token = box.encrypt("sk-test-123456")
    assert token != "sk-test-123456"
    assert key_file.exists()
    # 重新加载同一个主密钥仍可解密
    assert SecretBox(key_file).decrypt(token) == "sk-test-123456"


def test_secretbox_wrong_key_fails(tmp_path):
    token = SecretBox(tmp_path / "a.key").encrypt("secret")
    with pytest.raises(ValueError):
        SecretBox(tmp_path / "b.key").decrypt(token)


def test_secretbox_try_decrypt_and_bad_key_file(tmp_path):
    from app.core.crypto import MasterKeyError, SecretError

    token = SecretBox(tmp_path / "a.key").encrypt("secret")
    other = SecretBox(tmp_path / "b.key")
    assert other.try_decrypt(token) is None and other.try_decrypt(None) is None
    with pytest.raises(SecretError, match="主密钥"):
        other.decrypt(token)
    bad = tmp_path / "bad.key"
    bad.write_text("not-a-key")
    with pytest.raises(MasterKeyError, match="格式不对"):
        SecretBox(bad)


def test_undecryptable_keys_do_not_break_api(client, settings, caplog):
    """主密钥被换掉后：模型列表照常返回并标出问题，测试接口给出明确提示，启动检查写日志。"""
    from app.arena.deps import secret_box
    from app.core.keycheck import check_encrypted_secrets
    from app.db.session import get_sessionmaker
    from app.models import LLMProviderRow

    r = client.post("/api/providers", json={"name": "云端", "protocol": "openai_compatible",
                                            "base_url": "http://10.0.0.1:9100/v1", "api_key": "sk-test-123456"})
    pid = r.json()["id"]
    assert check_encrypted_secrets() == 0
    with get_sessionmaker()() as s:            # 模拟主密钥被替换：密文换成另一把密钥加密的
        s.get(LLMProviderRow, pid).api_key_enc = SecretBox(settings.data_dir / "other.key").encrypt("x")
        s.commit()
    secret_box.cache_clear()
    p = next(x for x in client.get("/api/providers").json() if x["id"] == pid)
    assert p["has_api_key"] is True and "无法解密" in p["api_key_masked"] and p["api_key_error"]
    assert check_encrypted_secrets() == 1 and "无法用当前主密钥解密" in caplog.text


def test_mask_key():
    assert mask_key("sk-abcdefgh1234") == "****1234"
    assert mask_key("abc") == "***"
    assert mask_key(None) == ""


@pytest.mark.parametrize(
    "raw",
    [
        "calling with key sk-abcdEFGH12345678xyz",
        "Authorization: Bearer abcdefghijklmnop",
        '{"api_key": "abcdef123456"}',
    ],
)
def test_mask_secrets_in_logs(raw):
    masked = mask_secrets(raw)
    assert "****" in masked
    assert "12345678xyz" not in masked
    assert "ijklmnop" not in masked
    assert "abcdef123456" not in masked
