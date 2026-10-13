"""技能服务：暂存待审、批准安装、启停、卸载、调用（命令策略 + 审计）、读取说明文件。"""

from __future__ import annotations

import json as _json
import logging
import re
import shutil
import urllib.request
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.models.skills import SkillCallRow, SkillRow
from app.skills import package, sandbox

logger = logging.getLogger(__name__)

AI_TIMEOUT = 90
USER_TIMEOUT = 300
SETUP_TIMEOUT = 900
READ_LIMIT = 30_000
ARG_LIMIT = 40


class SkillError(ValueError):
    pass


# ---------------------------------------------------------------- 获取与暂存（不执行任何文件）


def http_get(url: str, json: bool = False):  # noqa: A002 - 与 package.download_github 的调用约定一致
    """下载（GitHub API / codeload）；json=True 时解析为 JSON。"""
    want_json = json
    req = urllib.request.Request(url, headers={"User-Agent": "StockArena-skills",
                                               "Accept": "application/vnd.github+json" if want_json else "*/*"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - 只访问 GitHub 固定域名
        data = resp.read(package.MAX_DOWNLOAD + 1)
    if len(data) > package.MAX_DOWNLOAD:
        raise package.PackageError("下载内容过大")
    if want_json:
        return _json.loads(data.decode("utf-8"))
    return data


def _finish_stage(s: Session, staging: Path, source_type: str, source_url: str, user_id: int,
                  market_id: int | None = None, request_id: int | None = None) -> SkillRow:
    root = package.find_skill_root(staging)
    manifest = package.scan(root)
    cmds, user_only, setup = package.suggest_commands(root, manifest)
    row = SkillRow(user_id=user_id, name=manifest["name"], display_name=manifest["display_name"],
                   description=manifest["description"], version=manifest["version"], source_type=source_type,
                   source_url=source_url, market_id=market_id, status="pending", enabled=False, manifest=manifest,
                   allow_domains=manifest["domains"][:20], commands=cmds, user_only=user_only, setup_command=setup,
                   package_sha256=manifest["sha256"], request_id=request_id)
    s.add(row)
    s.flush()
    pkg, home = sandbox.skill_dirs(row.id)
    if pkg.parent.exists():
        shutil.rmtree(pkg.parent)
    pkg.parent.mkdir(parents=True)
    shutil.move(str(root), str(pkg))
    home.mkdir()
    shutil.rmtree(staging, ignore_errors=True)
    return row


def stage_github(s: Session, url: str, user_id: int, getter=http_get, market_id: int | None = None,
                 request_id: int | None = None) -> SkillRow:
    src = package.parse_github(url)
    data, ref = package.download_github(src, getter)
    staging = package.new_staging(sandbox.base_dir())
    try:
        package.extract(data, staging, subdir=src.path, strip_top=True)
        src.ref = ref
        return _finish_stage(s, staging, "market" if market_id else "github", src.url, user_id, market_id,
                             request_id)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def stage_upload(s: Session, filename: str, data: bytes, user_id: int) -> SkillRow:
    staging = package.new_staging(sandbox.base_dir())
    try:
        package.extract(data, staging)
        return _finish_stage(s, staging, "upload", filename, user_id)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------------------------------------------------------------- 审批、安装、启停


DOMAIN_RE = re.compile(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
CMD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def set_policy(row: SkillRow, *, allow_domains: list[str] | None = None, commands: list[str] | None = None,
               user_only: list[str] | None = None, setup_command: str | None = None) -> None:
    if allow_domains is not None:
        bad = [d for d in allow_domains if not DOMAIN_RE.match(d.strip())]
        if bad:
            raise SkillError(f"域名格式不对：{'、'.join(bad)}")
        row.allow_domains = sorted({d.strip().lower().removeprefix("*.") for d in allow_domains if d.strip()})
    if commands is not None:
        bad = [c for c in commands if not CMD_RE.match(c)]
        if bad:
            raise SkillError(f"命令名只能是字母数字：{'、'.join(bad)}")
        row.commands = list(dict.fromkeys(commands))
    if user_only is not None:
        row.user_only = [c.strip() for c in user_only if c.strip()]
    if setup_command is not None:
        row.setup_command = setup_command.strip()[:500]


def _token(row: SkillRow) -> str:
    path = sandbox.base_dir() / str(row.id) / "proxy_token"
    if not path.exists():
        path.write_text(sandbox.new_token(), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def sync_proxy(s: Session) -> None:
    """把所有已安装（或安装中）技能的令牌与允许域名写给出网代理。"""
    entries = {}
    for row in s.scalars(select(SkillRow).where(SkillRow.status.in_(("installing", "installed")))):
        entries[_token(row)] = {"skill": row.name, "domains": row.allow_domains or []}
    sandbox.write_allow(entries)


def approve(s: Session, row: SkillRow) -> None:
    """用户批准：记录时间，状态改为安装中（由调用方随后在后台执行 install）。"""
    if row.status not in ("pending", "failed"):
        raise SkillError(f"当前状态（{row.status}）不能批准")
    row.status, row.approved_at = "installing", utcnow()
    s.flush()
    sync_proxy(s)


async def install(skill_id: int) -> str:
    """在沙箱中运行安装命令（若有），完成后标记为已安装。"""
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        row = s.get(SkillRow, skill_id)
        setup, token = row.setup_command, _token(row)
    log = ""
    ok = True
    if setup:
        res = await sandbox.run(skill_id, token, ["bash", "-lc", setup], timeout=SETUP_TIMEOUT)
        ok = res.exit_code == 0
        log = f"$ {setup}\n{res.output[-20000:]}\n退出码：{res.exit_code}" + (
            f"\n被拦截的域名：{'、'.join(res.blocked)}（如确需访问，请加入允许域名后重试安装）" if res.blocked else "")
        with get_sessionmaker()() as s:
            _audit(s, skill_id, "setup", "install", ["bash", "-lc", setup], res)
            s.commit()
    with get_sessionmaker()() as s:
        row = s.get(SkillRow, skill_id)
        row.status = "installed" if ok else "failed"
        row.enabled = ok
        row.install_log = log or "没有安装命令，直接可用"
        s.commit()
        sync_proxy(s)
    return row.status


def reject(s: Session, row: SkillRow) -> None:
    if row.status not in ("pending", "failed"):
        raise SkillError("只能拒绝待审批或安装失败的技能")
    row.status, row.enabled = "rejected", False
    shutil.rmtree(sandbox.base_dir() / str(row.id), ignore_errors=True)


def uninstall(s: Session, row: SkillRow) -> None:
    row.status, row.enabled = "removed", False
    shutil.rmtree(sandbox.base_dir() / str(row.id), ignore_errors=True)
    s.flush()
    sync_proxy(s)


# ---------------------------------------------------------------- 调用


def check_argv(row: SkillRow, argv: list[str], caller: str) -> list[str]:
    """命令策略：只能调用声明的命令入口（不经过 shell）；仅限用户的子命令不允许 AI 调用。"""
    if row.status != "installed" or not row.enabled:
        raise SkillError(f"技能「{row.name}」未安装或已停用")
    if not argv or not isinstance(argv, list) or len(argv) > ARG_LIMIT:
        raise SkillError("命令格式不对：需要一个参数列表")
    argv = [str(a) for a in argv]
    if any("\x00" in a or len(a) > 500 for a in argv):
        raise SkillError("参数中有非法字符或过长")
    if argv[0] not in (row.commands or []):
        raise SkillError(f"只能调用技能声明的命令：{'、'.join(row.commands or []) or '（无）'}")
    if caller != "user" and any(a in (row.user_only or []) for a in argv[1:3]):
        raise SkillError(f"「{' '.join(argv[:2])}」只允许用户本人在技能终端中执行（涉及鉴权或密钥）")
    return argv


def audit_argv(argv: list[str], user_only: list[str]) -> list[str]:
    """记录用的参数：仅限用户的子命令（如 auth set <密钥>）只保留命令与子命令，其余参数打码。"""
    hit = next((k for k in (1, 2) if k < len(argv) and argv[k] in (user_only or [])), None)
    if hit is None:
        return [sandbox.redact(a) for a in argv]
    keep = min(hit + 2, len(argv))
    return argv[:keep] + ["***"] * (len(argv) - keep)


def _audit(s: Session, skill_id: int, caller: str, context: str, argv: list[str], res: sandbox.RunResult) -> None:
    row = s.get(SkillRow, skill_id)
    argv = audit_argv(argv, row.user_only if row else [])
    out = res.output if caller != "user" else res.output[:20000]
    s.add(SkillCallRow(skill_id=skill_id, caller=caller, context=context[:128], argv=argv, exit_code=res.exit_code,
                       seconds=res.seconds, output=out[:20000], blocked=res.blocked))


async def call(skill_id: int, argv: list[str], caller: str, context: str = "") -> sandbox.RunResult:
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        row = s.get(SkillRow, skill_id)
        if row is None:
            raise SkillError("技能不存在")
        argv = check_argv(row, argv, caller)
        token = _token(row)
    if caller == "user":   # 技能终端：同一终端里的命令在同一个会话容器中执行
        res = await sandbox.run_session(skill_id, token, argv, timeout=USER_TIMEOUT)
    else:
        res = await sandbox.run(skill_id, token, argv, timeout=AI_TIMEOUT)
    with get_sessionmaker()() as s:
        _audit(s, skill_id, caller, context, argv, res)
        s.commit()
    return res


def read_file(row: SkillRow, path: str = "SKILL.md") -> str:
    """读取技能包里的说明文件（只读文本，供 AI 按需查看）。"""
    if row.status != "installed" or not row.enabled:
        raise SkillError(f"技能「{row.name}」未安装或已停用")
    pkg, _ = sandbox.skill_dirs(row.id)
    target = (pkg / path).resolve()
    if pkg.resolve() not in target.parents and target != pkg.resolve():
        raise SkillError("只能读取技能包内的文件")
    if not target.is_file():
        files = [f["path"] for f in (row.manifest or {}).get("files", []) if f["kind"] == "doc"][:50]
        raise SkillError(f"文件不存在：{path}。可读取的文件：{'、'.join(files)}")
    data = target.read_bytes()
    if b"\x00" in data[:1024]:
        raise SkillError("这是二进制文件，不能读取")
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= READ_LIMIT else text[:READ_LIMIT] + f"\n……（共 {len(text)} 字符，已截断）"


def installed(s: Session) -> list[SkillRow]:
    return list(s.scalars(select(SkillRow).where(SkillRow.status == "installed", SkillRow.enabled.is_(True))
                          .order_by(SkillRow.id)))


def to_dict(row: SkillRow, detail: bool = False) -> dict:
    d = {"id": row.id, "name": row.name, "display_name": row.display_name, "description": row.description,
         "version": row.version, "source_type": row.source_type, "source_url": row.source_url,
         "status": row.status, "enabled": row.enabled, "allow_domains": row.allow_domains or [],
         "commands": row.commands or [], "user_only": row.user_only or [], "setup_command": row.setup_command,
         "package_sha256": row.package_sha256, "request_id": row.request_id,
         "approved_at": row.approved_at.isoformat() if row.approved_at else None,
         "created_at": row.created_at.isoformat(), "warnings": (row.manifest or {}).get("warnings", []),
         "summary": {k: len((row.manifest or {}).get(k, [])) for k in ("files", "scripts", "binaries", "domains")}}
    if detail:
        d["manifest"] = row.manifest
        d["install_log"] = row.install_log
    return d


def preview(row: SkillRow, path: str) -> str:
    """审阅用：查看包内任意文本文件（含脚本），不限状态。"""
    pkg, _ = sandbox.skill_dirs(row.id)
    target = (pkg / path).resolve()
    if pkg.resolve() not in target.parents or not target.is_file():
        raise SkillError("文件不存在")
    data = target.read_bytes()
    if b"\x00" in data[:1024]:
        return f"（二进制文件，{len(data)} 字节，sha256 见清单）"
    text = data.decode("utf-8", errors="replace")
    return text[:200_000]


def dumps_calls(rows: list[SkillCallRow]) -> list[dict]:
    return [{"id": c.id, "caller": c.caller, "context": c.context, "argv": c.argv, "exit_code": c.exit_code,
             "seconds": c.seconds, "output": c.output[:4000], "blocked": c.blocked,
             "created_at": c.created_at.isoformat()} for c in rows]

