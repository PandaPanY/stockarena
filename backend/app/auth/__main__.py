"""命令行工具：管理员忘记密码时清除管理员的登录密码（普通用户的密码由管理员在页面上重设）。

用法（在服务器的 backend/ 目录下）：
    uv run python -m app.auth reset-password

清除后所有设备退出登录，并生成新的首次设置码（打印在屏幕上，也保存在数据目录下的 setup-code.txt）；
打开页面重新设置密码时需要填写（服务在运行时最多 5 秒内生效，不用重启）。
"""

from __future__ import annotations

import sys

from app.auth import core
from app.db.session import get_sessionmaker


def reset_password() -> None:
    with get_sessionmaker()() as s:
        user = core.get_admin(s)
        user.password_hash = None
        user.session_gen += 1
        s.commit()
    core.clear_setup_code()
    code = core.ensure_setup_code(log=False)
    print("已清除管理员（admin）的登录密码，管理员所有设备已退出登录。")
    print(f"首次设置码：{code}（也保存在 {core.setup_code_file()}）。请尽快打开页面，填写设置码并重新设置密码。")


def main(argv: list[str]) -> int:
    if argv[1:] == ["reset-password"]:
        reset_password()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
