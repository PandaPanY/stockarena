"""技能功能总开关。

技能会在 Docker 容器里执行第三方代码，部署门槛和风险都比较高，开源版默认关闭。由配置项 skills_enabled 决定：
- true / false：以配置为准；
- 不写（默认）：已经安装过技能的旧安装保持开启（升级不改变原有行为），新安装关闭。

关闭时：设置页不显示「技能」，技能接口返回提示，AI 复核与自选股监控不提供技能工具，启动时不检查 Docker。
"""

from __future__ import annotations

import threading

from sqlalchemy import select

from app.core.config import get_settings

DISABLED_MESSAGE = "技能功能未开启：需要在配置文件中设置 skills_enabled: true 并重启（见 docs/SKILLS.md）"

_auto: bool | None = None
_lock = threading.Lock()


def skills_enabled() -> bool:
    configured = get_settings().skills_enabled
    if configured is not None:
        return configured
    global _auto
    with _lock:
        if _auto is None:
            _auto = _has_installed()
        return _auto


def _has_installed() -> bool:
    from app.db.session import get_sessionmaker
    from app.models.skills import SkillRow

    with get_sessionmaker()() as s:
        return s.scalars(select(SkillRow.id).where(SkillRow.status == "installed").limit(1)).first() is not None


def reset_cache() -> None:
    """测试用：重新判断自动开关。"""
    global _auto
    with _lock:
        _auto = None
