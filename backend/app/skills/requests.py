"""AI 安装申请（第 4 步）：AI 可以搜索技能市场、提交安装申请；**只有用户批准后才会安装**。

- 申请时立即获取技能包并生成审阅清单（只下载、扫描，不执行），推送到微信（事件 skills），用户在技能页批准或拒绝；
- 防刷：同一技能（地址）的重复申请合并；待处理申请最多 MAX_PENDING 个；AI 每天搜索市场最多 MAX_SEARCH_PER_DAY 次；
- 开关：app_settings「skills.ai_requests」（默认开启），关闭后 AI 看不到市场搜索与申请工具。
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.models.settings import AppSettingRow
from app.models.skills import SkillRequestRow, SkillRow

logger = logging.getLogger(__name__)

KEY = "skills"
MAX_PENDING = 5
MAX_SEARCH_PER_DAY = 10
_search_count: dict[str, int] = {}


def settings(s: Session) -> dict:
    row = s.get(AppSettingRow, KEY)
    return {"ai_requests": True, **(row.value if row else {})}


def set_settings(s: Session, **kw) -> dict:
    row = s.get(AppSettingRow, KEY) or AppSettingRow(key=KEY, value={})
    row.value = {**settings(s), **kw}
    s.add(row)
    return row.value


def can_search() -> bool:
    today = date.today().isoformat()
    return _search_count.get(today, 0) < MAX_SEARCH_PER_DAY


def count_search() -> None:
    today = date.today().isoformat()
    if today not in _search_count:
        _search_count.clear()
    _search_count[today] = _search_count.get(today, 0) + 1


def existing(s: Session, url: str) -> str | None:
    """同一地址已有技能或待处理申请时返回说明。"""
    u = url.rstrip("/")
    for r in s.scalars(select(SkillRow).where(SkillRow.status.in_(("pending", "installing", "installed", "failed")))):
        if r.source_url.rstrip("/") == u:
            return {"installed": f"技能「{r.name}」已安装，可以直接使用",
                    "pending": f"技能「{r.name}」已在等待用户审批"}.get(r.status, f"技能「{r.name}」状态：{r.status}")
    if s.scalars(select(SkillRequestRow).where(SkillRequestRow.status == "pending",
                                               SkillRequestRow.source_url == u)).first():
        return "已经提交过这个申请，正在等待用户审批"
    return None


def create(s: Session, *, requester: str, url: str, reason: str, query: str = "", market_id: int | None = None,
           getter=None) -> tuple[SkillRequestRow | None, str]:
    """提交申请并暂存技能包。返回 (申请, 说明)；被拒绝时申请为 None。"""
    from app.skills import package, service

    if not settings(s).get("ai_requests", True):
        return None, "用户已关闭 AI 申请安装技能"
    try:
        package.parse_github(url)
    except package.PackageError as e:
        return None, str(e)
    dup = existing(s, url)
    if dup:
        return None, dup
    pending = s.scalars(select(SkillRequestRow).where(SkillRequestRow.status == "pending")).all()
    if len(pending) >= MAX_PENDING:
        return None, f"待用户审批的申请已有 {len(pending)} 个，暂不能再申请"
    req = SkillRequestRow(user_id=1, requester=requester[:64], reason=reason[:1000], query=query[:200],
                          source_url=url.rstrip("/"), market_id=market_id, status="pending")
    s.add(req)
    s.flush()
    try:
        row = service.stage_github(s, url, 1, getter or service.http_get, market_id, req.id)
    except Exception as e:  # noqa: BLE001 - 获取失败时申请仍保留，记录原因
        req.status = "failed"
        s.flush()
        return req, f"申请已记录，但获取技能包失败：{e}"
    req.skill_id = row.id
    s.flush()
    return req, f"已提交安装申请（技能「{row.name}」），等待用户审批；本次请照常根据现有资料完成任务"


def decide(s: Session, skill: SkillRow, approved: bool) -> None:
    """用户批准 / 拒绝技能时，同步更新对应的申请。"""
    if not skill.request_id:
        return
    req = s.get(SkillRequestRow, skill.request_id)
    if req is not None and req.status == "pending":
        req.status, req.decided_at = ("approved" if approved else "rejected"), utcnow()


def to_dict(r: SkillRequestRow, skill: SkillRow | None = None) -> dict:
    return {"id": r.id, "requester": r.requester, "reason": r.reason, "query": r.query, "source_url": r.source_url,
            "market_id": r.market_id, "skill_id": r.skill_id, "status": r.status,
            "skill_name": skill.display_name or skill.name if skill else None,
            "created_at": r.created_at.isoformat(), "decided_at": r.decided_at.isoformat() if r.decided_at else None}


def notify_message(req: SkillRequestRow, skill: SkillRow | None):
    from app.core.config import get_settings
    from app.notify.service import Message

    name = (skill.display_name or skill.name) if skill else req.source_url.rsplit("/", 1)[-1]
    lines = [f"**{req.requester}** 想安装技能「{name}」", "", f"理由：{req.reason}"]
    if req.query:
        lines.append(f"搜索词：{req.query}")
    lines.append(f"来源：{req.source_url}")
    if skill is not None:
        m = skill.manifest or {}
        lines.append(f"包含 {len(m.get('files', []))} 个文件、{len(m.get('scripts', []))} 个脚本、"
                     f"{len(m.get('binaries', []))} 个程序；可能访问：{'、'.join(m.get('domains', [])[:6]) or '无'}")
        lines += [f"- {w}" for w in m.get("warnings", [])]
    lines += ["", "AI 不能自行安装：请在「配置中心 → 技能」中审阅后批准或拒绝。"]
    base = get_settings().public_url
    if base:
        lines.append(f"{base.rstrip('/')}/settings?tab=skills")
    return Message("skills", f"skill-request:{req.id}", f"【技能申请】AI 想安装：{name}", "\n".join(lines))
