"""访问保护：首次设置密码、登录、限速、修改密码与凭证失效、命令行重置。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.auth import core
from app.auth.middleware import is_protected
from app.core.config import Settings, get_settings

PW = "test-pass-123"


@pytest.fixture
def auth_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SA_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    core.invalidate()
    core.limiter = core.LoginLimiter()
    core.register_limiter = core.LoginLimiter(max_failures=5, window=3600.0)
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    core.invalidate()


def setup(c: TestClient, pw: str = PW, open_registration: bool = True):
    """首次设置管理员密码（带首次设置码）；默认顺便打开注册，方便后面的用例注册普通用户。"""
    r = c.post("/api/auth/setup", json={"password": pw, "setup_code": core.ensure_setup_code()})
    if r.status_code == 200 and open_registration:
        assert c.put("/api/auth/registration", json={"open": True}).status_code == 200
    return r


def test_password_hash_roundtrip():
    h = core.hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert core.verify_password(PW, h)
    assert not core.verify_password("wrong-pass", h)
    assert not core.verify_password(PW, None)
    assert not core.verify_password(PW, "garbage")
    assert core.hash_password(PW) != h  # 每次加盐不同


def test_token_signature_and_expiry(settings: Settings):
    t = core.issue_token(1, 3, now=1000.0)
    assert core.parse_token(t, now=2000.0) == (1, 3)
    assert core.parse_token(t, now=1000.0 + core.SESSION_DAYS * 86400 + 1) is None
    uid, gen, exp, sig = t.split(".")
    assert core.parse_token(f"{uid}.{int(gen) + 1}.{exp}.{sig}", now=2000.0) is None  # 篡改代数
    assert core.parse_token("abc", now=2000.0) is None
    assert core.parse_token(None) is None


def test_protected_paths():
    assert is_protected("/api/quant/strategies")
    assert is_protected("/api/system/health")
    assert is_protected("/api/auth/password")
    assert is_protected("/docs") and is_protected("/openapi.json")
    assert not is_protected("/api/auth/status")
    assert not is_protected("/api/auth/login")
    assert not is_protected("/")
    assert not is_protected("/assets/index.js")


def test_disabled_by_settings_in_tests(client: TestClient):
    assert client.get("/api/system/health").status_code == 200
    assert client.get("/api/auth/status").json()["enabled"] is False


def test_setup_login_flow(auth_client: TestClient):
    c = auth_client
    st = c.get("/api/auth/status").json()
    assert st == {"enabled": True, "configured": False, "authenticated": False, "registration_open": False,
                  "features": {"skills": False, "mcp": True}, "source_url": get_settings().source_url, "user": None}
    r = c.get("/api/system/health")
    assert r.status_code == 401 and r.json()["code"] == "setup_required"

    code = core.setup_code_file().read_text(encoding="utf-8").strip()      # 启动时已生成
    assert len(code) == 14 and code == core.ensure_setup_code()
    assert c.post("/api/auth/setup", json={"password": PW}).status_code == 403                    # 缺设置码
    assert c.post("/api/auth/setup", json={"password": PW, "setup_code": "AAAA-BBBB-CCCC"}).status_code == 403
    assert c.post("/api/auth/setup", json={"password": "short", "setup_code": code}).status_code == 400
    # 设置码不区分大小写，可以不带减号
    r = c.post("/api/auth/setup", json={"password": PW, "setup_code": code.replace("-", "").lower()})
    assert r.status_code == 200
    assert not core.setup_code_file().exists()                              # 用过即删
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/system/health").status_code == 200
    st = c.get("/api/auth/status").json()
    assert st["authenticated"] is True and st["user"]["name"] == "admin" and st["user"]["role"] == "admin"
    # 已设置后不能再走首次设置
    assert setup(c, "another-pass").status_code == 409

    c.post("/api/auth/logout")
    r = c.get("/api/system/health")
    assert r.status_code == 401 and r.json()["code"] == "login_required"
    assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "ADMIN", "password": PW}).status_code == 200  # 不区分大小写
    assert c.get("/api/system/health").status_code == 200


def test_forged_cookie_rejected(auth_client: TestClient):
    c = auth_client
    setup(c)
    c.cookies.set(core.COOKIE_NAME, "1.1.9999999999.forged")
    assert c.get("/api/system/health").status_code == 401


def test_login_rate_limit(auth_client: TestClient):
    c = auth_client
    setup(c)
    c.post("/api/auth/logout")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 429 and "分钟后再试" in r.json()["detail"]


def test_limiter_window():
    lim = core.LoginLimiter(max_failures=2, window=100)
    lim.fail("ip", now=0)
    assert lim.wait_seconds("ip", now=1) == 0
    lim.fail("ip", now=10)
    assert lim.wait_seconds("ip", now=20) == 80
    assert lim.wait_seconds("ip", now=101) == 0  # 最早一次失败已过窗口
    assert lim.wait_seconds("other", now=20) == 0


def test_change_password_invalidates_other_devices(auth_client: TestClient):
    c = auth_client
    setup(c)
    old_token = c.cookies.get(core.COOKIE_NAME)

    bad = c.post("/api/auth/password", json={"old_password": "wrong-pass", "new_password": "new-pass-456"})
    assert bad.status_code == 400
    r = c.post("/api/auth/password", json={"old_password": PW, "new_password": "new-pass-456"})
    assert r.status_code == 200
    assert c.get("/api/system/health").status_code == 200  # 当前设备换发新凭证

    other = TestClient(c.app)
    other.cookies.set(core.COOKIE_NAME, old_token)
    assert other.get("/api/system/health").status_code == 401  # 旧凭证失效

    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "new-pass-456"}).status_code == 200


def test_logout_all(auth_client: TestClient):
    c = auth_client
    setup(c)
    token = c.cookies.get(core.COOKIE_NAME)
    assert c.post("/api/auth/logout-all").status_code == 200
    c.cookies.set(core.COOKIE_NAME, token)
    assert c.get("/api/system/health").status_code == 401


def test_cli_reset_password(auth_client: TestClient):
    from app.auth.__main__ import main

    c = auth_client
    setup(c)
    assert main(["x", "reset-password"]) == 0
    core.invalidate()
    assert c.get("/api/system/health").status_code == 401
    assert c.get("/api/auth/status").json()["configured"] is False
    assert core.setup_code_file().exists()                    # 重置时生成了新的设置码
    assert setup(c, "new-pass-456").status_code == 200
    assert main(["x"]) == 1


def test_register_login_and_roles(auth_client: TestClient):
    c = auth_client
    # 管理员完成首次设置之前不能注册
    assert c.post("/api/auth/register", json={"username": "alice", "password": PW}).status_code == 409
    setup(c)
    c.post("/api/auth/logout")

    assert c.post("/api/auth/register", json={"username": "a", "password": PW}).status_code == 400       # 用户名太短
    assert c.post("/api/auth/register", json={"username": "alice", "password": "short"}).status_code == 400
    assert c.post("/api/auth/register", json={"username": "Admin", "password": PW}).status_code == 409   # 已被使用
    r = c.post("/api/auth/register", json={"username": "张三", "password": PW})
    assert r.status_code == 200 and r.json()["user"]["role"] == "user"
    uid = r.json()["user"]["id"]
    assert c.get("/api/auth/me").json()["name"] == "张三"            # 注册后直接登录
    assert c.get("/api/system/health").status_code == 200
    assert c.get("/api/auth/users").status_code == 403                 # 普通用户不能管理用户

    # 管理员停用该用户：已登录的设备立即失效，也不能再登录
    admin = TestClient(c.app)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 200
    users = admin.get("/api/auth/users").json()
    assert [u["name"] for u in users] == ["admin", "张三"]
    assert admin.patch(f"/api/auth/users/{uid}", json={"disabled": True}).json()["disabled"] is True
    assert c.get("/api/system/health").status_code == 401
    r = c.post("/api/auth/login", json={"username": "张三", "password": PW})
    assert r.status_code == 403 and "停用" in r.json()["detail"]
    assert admin.patch("/api/auth/users/1", json={"disabled": True}).status_code == 400   # 不能停用管理员

    # 启用并由管理员重设密码
    admin.patch(f"/api/auth/users/{uid}", json={"disabled": False})
    assert admin.post(f"/api/auth/users/{uid}/password", json={"new_password": "reset-pass-789"}).status_code == 200
    assert c.post("/api/auth/login", json={"username": "张三", "password": PW}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "张三", "password": "reset-pass-789"}).status_code == 200


def test_register_rate_limit(auth_client: TestClient):
    c = auth_client
    setup(c)
    for k in range(5):
        assert c.post("/api/auth/register", json={"username": f"user{k}", "password": PW}).status_code == 200
    r = c.post("/api/auth/register", json={"username": "user9", "password": PW})
    assert r.status_code == 429


def test_current_user_from_token(auth_client: TestClient):
    """接口里的当前用户来自登录凭证（数据隔离的基础）。"""
    c = auth_client
    setup(c)
    assert c.get("/api/auth/me").json()["id"] == 1
    r = c.post("/api/auth/register", json={"username": "bob", "password": PW})
    assert c.get("/api/auth/me").json()["id"] == r.json()["user"]["id"] != 1


def test_setup_code_rate_limit(auth_client: TestClient):
    c = auth_client
    for _ in range(5):
        assert c.post("/api/auth/setup", json={"password": PW, "setup_code": "wrong"}).status_code == 403
    r = c.post("/api/auth/setup", json={"password": PW, "setup_code": core.ensure_setup_code()})
    assert r.status_code == 429


def test_registration_switch(auth_client: TestClient):
    """新安装默认不开放注册；管理员可以在「用户管理」里打开或关闭。"""
    c = auth_client
    setup(c, open_registration=False)
    assert c.get("/api/auth/registration").json() == {"open": False}
    other = TestClient(c.app)
    r = other.post("/api/auth/register", json={"username": "alice", "password": PW})
    assert r.status_code == 403 and "没有开放注册" in r.json()["detail"]

    assert c.put("/api/auth/registration", json={"open": True}).json() == {"open": True}
    assert other.get("/api/auth/status").json()["registration_open"] is True
    assert other.post("/api/auth/register", json={"username": "alice", "password": PW}).status_code == 200
    assert other.get("/api/auth/registration").status_code == 403          # 普通用户不能改
    assert other.put("/api/auth/registration", json={"open": False}).status_code == 403
