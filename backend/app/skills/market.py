"""技能市场（第 3 步）：只负责检索与发现，安装一律走「下载 GitHub 包 → 用户审阅 → 批准」。

- github：一个包含多个技能目录的 GitHub 仓库（如 Anthropic 官方 anthropics/skills）。刷新时用 GitHub API 列出所有
  SKILL.md，读取名称与说明，缓存为索引，在本地按关键词检索。
- skillsmp：SkillsMP 社区索引（skillsmp.com，汇集 GitHub 上的开源技能）的公开搜索接口，匿名每天 50 次，
  结果缓存一天；每条结果带 GitHub 地址。社区技能质量参差，务必审阅后再批准。
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.models.skills import SkillMarketRow
from app.skills import package

logger = logging.getLogger(__name__)

DEFAULTS = [
    {"name": "Anthropic 官方技能", "kind": "github", "url": "https://github.com/anthropics/skills/tree/main/skills"},
    {"name": "SkillsMP 社区技能索引", "kind": "skillsmp", "url": "https://skillsmp.com/api/v1/skills/search"},
]
MAX_INDEX = 400
SEARCH_TTL = 86400
_search_cache: dict[tuple, tuple[float, list[dict]]] = {}


class MarketError(ValueError):
    pass


def ensure_defaults(s: Session, user_id: int = 1) -> None:
    if s.scalars(select(SkillMarketRow.id).limit(1)).first() is None:
        for m in DEFAULTS:
            s.add(SkillMarketRow(user_id=user_id, **m))
        s.flush()


def to_dict(m: SkillMarketRow) -> dict:
    return {"id": m.id, "name": m.name, "kind": m.kind, "url": m.url, "enabled": m.enabled,
            "skills": len(m.index or []), "indexed_at": m.indexed_at.isoformat() if m.indexed_at else None,
            "last_error": m.last_error}


def validate(kind: str, url: str) -> None:
    if kind == "github":
        package.parse_github(url)
    elif kind == "skillsmp":
        if not url.startswith("https://skillsmp.com/"):
            raise MarketError("SkillsMP 地址应为 https://skillsmp.com/api/v1/skills/search")
    else:
        raise MarketError("市场类型只能是 github 或 skillsmp")


def refresh_github(m: SkillMarketRow, http_get) -> int:
    """重建 GitHub 仓库市场的索引：列出仓库中所有 SKILL.md，读取名称与说明。"""
    src = package.parse_github(m.url)
    ref = src.ref or package.default_branch(src, http_get)
    tree = http_get(f"https://api.github.com/repos/{src.owner}/{src.repo}/git/trees/{ref}?recursive=1", json=True)
    prefix = f"{src.path}/" if src.path else ""
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob" and t["path"].endswith("SKILL.md")
             and t["path"].startswith(prefix)][:MAX_INDEX]
    index = []
    for p in paths:
        try:
            raw = http_get(f"https://raw.githubusercontent.com/{src.owner}/{src.repo}/{ref}/{p}")
            meta, _ = package.parse_frontmatter(raw.decode("utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001 - 单个技能读取失败不影响其他
            logger.info("读取 %s 失败：%s", p, e)
            continue
        folder = p.rsplit("/", 1)[0] if "/" in p else ""
        index.append({"name": str(meta.get("name") or folder.rsplit("/", 1)[-1] or src.repo)[:64],
                      "description": str(meta.get("description_zh") or meta.get("description") or "")[:500],
                      "url": f"https://github.com/{src.owner}/{src.repo}/tree/{ref}/{folder}".rstrip("/")})
    m.index, m.indexed_at, m.last_error = index, utcnow(), ""
    return len(index)


def _tokens(q: str) -> list[str]:
    return [t for t in re.split(r"[\s,，、]+", q.lower()) if t]


def _search_github(m: SkillMarketRow, q: str) -> list[dict]:
    toks = _tokens(q)
    out = []
    for it in m.index or []:
        text = f"{it['name']} {it['description']}".lower()
        score = sum(text.count(t) * (3 if t in it["name"].lower() else 1) for t in toks)
        if score:
            out.append({**it, "score": score})
    out.sort(key=lambda x: -x["score"])
    return out


def _search_skillsmp(m: SkillMarketRow, q: str, limit: int, http_get) -> list[dict]:
    key = (m.id, q.strip().lower(), limit)
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_TTL:
        return hit[1]
    url = f"{m.url}?{urllib.parse.urlencode({'q': q, 'limit': min(limit, 50)})}"
    data = http_get(url, json=True)
    if not data.get("success", False):
        err = (data.get("error") or {}).get("message") or "搜索失败"
        raise MarketError(f"SkillsMP：{err}")
    out = []
    for it in (data.get("data") or {}).get("skills", []):
        gh = str(it.get("githubUrl") or "")
        if not gh.startswith("https://github.com/"):
            continue
        out.append({"name": str(it.get("name") or "")[:64], "description": str(it.get("description") or "")[:500],
                    "url": gh, "author": it.get("author"), "stars": it.get("stars"),
                    "language": it.get("contentLanguage")})
    _search_cache[key] = (time.time(), out)
    return out


def search(s: Session, q: str, market_id: int | None = None, limit: int = 20, http_get=None) -> list[dict]:
    """在启用的市场中检索；返回 [{market_id, market, name, description, url, ...}]（url 为 GitHub 地址）。"""
    from app.skills.service import http_get as default_get

    http_get = http_get or default_get
    if len(q.strip()) < 2:
        raise MarketError("请输入至少 2 个字符的关键词")
    q_markets = select(SkillMarketRow).where(SkillMarketRow.enabled.is_(True))
    if market_id:
        q_markets = q_markets.where(SkillMarketRow.id == market_id)
    results: list[dict] = []
    errors = []
    for m in s.scalars(q_markets.order_by(SkillMarketRow.id)):
        try:
            if m.kind == "github":
                if not m.index:
                    refresh_github(m, http_get)
                found = _search_github(m, q)
            else:
                found = _search_skillsmp(m, q, limit, http_get)
        except Exception as e:  # noqa: BLE001 - 单个市场失败不影响其他
            m.last_error = str(e)[:500]
            errors.append(f"{m.name}：{e}")
            continue
        results += [{**it, "market_id": m.id, "market": m.name} for it in found[:limit]]
    if not results and errors:
        raise MarketError("；".join(errors))
    return results[: limit * 2]
