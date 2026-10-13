"""密码哈希、登录凭证签发与校验、登录失败限速。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import SecretBox
from app.models import DEFAULT_USER_ID, User
from app.models.user import ADMIN, USER

logger = logging.getLogger(__name__)

COOKIE_NAME = "sa_session"
SESSION_DAYS = 30
PASSWORD_MIN, PASSWORD_MAX = 8, 128

# scrypt 参数：N=2^14、r=8、p=1，单次约 50ms、16MB 内存
_N, _R, _P = 2**14, 8, 1


class AuthError(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------- 密码 ----------------

def check_password_rule(password: str) -> None:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise AuthError(f"密码长度需要 {PASSWORD_MIN}～{PASSWORD_MAX} 位")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(_unb64(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(digest))


# ---------------- 登录凭证 ----------------

@lru_cache
def _signing_key(key_file: Path) -> bytes:
    # 由主密钥派生，和 API Key 加密用的密钥分开
    return hashlib.sha256(b"stockarena-session:" + SecretBox._load_or_create_key(key_file)).digest()


def _sign(payload: str) -> str:
    key = _signing_key(get_settings().secret_key_file)
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def issue_token(user_id: int, gen: int, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    payload = f"{user_id}.{gen}.{exp}"
    return f"{payload}.{_sign(payload)}"


def parse_token(token: str | None, now: float | None = None) -> tuple[int, int] | None:
    """校验签名与有效期，返回 (user_id, gen)；无效时返回 None。代数是否仍有效由调用方对照数据库。"""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    payload, sig = ".".join(parts[:3]), parts[3]
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        uid, gen, exp = (int(x) for x in parts[:3])
    except ValueError:
        return None
    if exp < (now if now is not None else time.time()):
        return None
    return uid, gen


# ---------------- 用户名 ----------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-一-鿿]{2,32}$")      # 字母、数字、汉字、下划线、减号


def check_username_rule(name: str) -> None:
    if not USERNAME_RE.match(name):
        raise AuthError("用户名需要 2～32 位，只能包含字母、数字、汉字、下划线和减号")


def find_user(s: Session, name: str) -> User | None:
    """按用户名查找（不区分大小写）。"""
    return s.scalars(select(User).where(func.lower(User.name) == name.strip().lower())).first()


# ---------------- 用户状态（带短缓存，中间件每个请求都要查） ----------------

@dataclass(frozen=True)
class UserState:
    exists: bool
    configured: bool        # 已设置密码
    gen: int
    role: str
    disabled: bool


_MISSING = UserState(False, False, 0, USER, True)
_state_cache: dict[int, tuple[float, UserState]] = {}
_state_lock = threading.Lock()
STATE_TTL = 5.0


def get_admin(s: Session) -> User:
    """管理员账户（id 1，首次打开页面时设置密码）。"""
    user = s.get(User, DEFAULT_USER_ID)
    if user is None:
        user = User(id=DEFAULT_USER_ID, name="admin", role=ADMIN)
        s.add(user)
        s.flush()
    return user


def user_state(uid: int) -> UserState:
    """用户是否存在、已设置密码、凭证代数、角色、是否停用。缓存 5 秒；本进程内修改后立即刷新（见 invalidate）。"""
    now = time.monotonic()
    with _state_lock:
        hit = _state_cache.get(uid)
        if hit and now - hit[0] < STATE_TTL:
            return hit[1]
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        u = s.get(User, uid)
        st = (UserState(True, bool(u.password_hash), u.session_gen, u.role, bool(u.disabled)) if u else _MISSING)
    with _state_lock:
        _state_cache[uid] = (now, st)
    return st


def admin_configured() -> bool:
    """管理员是否已设置密码（没有时页面进入首次设置）。"""
    return user_state(DEFAULT_USER_ID).configured


def is_admin(uid: int) -> bool:
    st = user_state(uid)
    return st.role == ADMIN and not st.disabled


# ---------------- 首次设置码（防止部署后被别人抢先设置管理员密码） ----------------

SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # 去掉容易看错的 0/O、1/I/L


def setup_code_file() -> Path:
    return get_settings().data_dir / "setup-code.txt"


def ensure_setup_code(announce: bool = False, log: bool = True) -> str:
    """管理员还没设置密码时使用的一次性设置码：保存在 data/setup-code.txt，生成时写进日志。

    设置管理员密码时必须填写，证明操作者能看到服务器上的文件或日志。设置成功后删除；
    命令行 reset-password 清除管理员密码时重新生成。announce=True 时（启动时）即使已有也再写一次日志；
    log=False 时不写日志（命令行自己打印）。文件只有本用户可读写。
    """
    f = setup_code_file()
    try:
        code = f.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        code = ""
    fresh = not code
    if fresh:
        code = "-".join("".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(4)) for _ in range(3))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            f.chmod(0o600)
    if log and (fresh or announce):
        logger.warning("管理员还没有设置密码。首次设置码：%s（也保存在 %s）。打开页面设置密码时需要填写。", code, f)
    return code


def _normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def check_setup_code(given: str) -> bool:
    return hmac.compare_digest(_normalize_code(given).encode(), _normalize_code(ensure_setup_code()).encode())


def clear_setup_code() -> None:
    setup_code_file().unlink(missing_ok=True)


# ---------------- 开放注册开关（管理员在「用户管理」里设置；新安装默认关闭） ----------------

REGISTRATION_KEY = "auth.registration"


def registration_open(s: Session) -> bool:
    from app.models.settings import AppSettingRow

    row = s.get(AppSettingRow, REGISTRATION_KEY)
    return bool(row and (row.value or {}).get("open"))


def set_registration_open(s: Session, value: bool) -> None:
    from app.models.settings import AppSettingRow

    row = s.get(AppSettingRow, REGISTRATION_KEY)
    if row is None:
        s.add(AppSettingRow(key=REGISTRATION_KEY, value={"open": value}))
    else:
        row.value = {"open": value}


def invalidate(uid: int | None = None) -> None:
    with _state_lock:
        if uid is None:
            _state_cache.clear()
        else:
            _state_cache.pop(uid, None)


def token_valid(token: str | None) -> int | None:
    """凭证有效时返回用户 ID：签名与有效期正确、用户存在且已设置密码、未停用、代数一致。"""
    parsed = parse_token(token)
    if parsed is None:
        return None
    uid, gen = parsed
    st = user_state(uid)
    if not st.configured or st.disabled or gen != st.gen:
        return None
    return uid


# ---------------- 登录失败限速（按来源 IP，内存中） ----------------

class LoginLimiter:
    """15 分钟内连续失败 5 次后锁定，直到最早一次失败满 15 分钟。成功登录清零。"""

    def __init__(self, max_failures: int = 5, window: float = 900.0):
        self.max_failures, self.window = max_failures, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        fails = [t for t in self._fails.get(ip, []) if now - t < self.window]
        if fails:
            self._fails[ip] = fails
        else:
            self._fails.pop(ip, None)
        return fails

    def wait_seconds(self, ip: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            fails = self._recent(ip, now)
            if len(fails) < self.max_failures:
                return 0
            return max(1, int(fails[0] + self.window - now))

    def fail(self, ip: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._fails[ip] = [*self._recent(ip, now), now]

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


limiter = LoginLimiter()
# 注册限速：同一来源 IP 每小时最多注册 5 个账户（成功的注册也计数）
register_limiter = LoginLimiter(max_failures=5, window=3600.0)
