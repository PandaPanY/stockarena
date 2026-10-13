"""技能包的获取与审阅：从 GitHub 或上传的 zip 解压到暂存目录，解析 SKILL.md，扫描文件。只读，不执行任何文件。"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

MAX_TOTAL = 60 * 1024 * 1024        # 解压后总大小上限
MAX_FILES = 3000
MAX_DOWNLOAD = 80 * 1024 * 1024
SCRIPT_EXT = {".sh", ".bash", ".ps1", ".bat", ".cmd", ".py", ".js", ".cjs", ".mjs", ".ts", ".rb", ".pl"}
DOC_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".html"}
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 常见的文档 / 示例域名不计入「可能访问的域名」
IGNORE_DOMAINS = {"example.com", "localhost"}


class PackageError(ValueError):
    pass


@dataclass
class GithubSource:
    owner: str
    repo: str
    ref: str          # 分支或标签；为空表示默认分支
    path: str         # 仓库内的子目录（技能目录）；为空表示仓库根目录

    @property
    def url(self) -> str:
        tail = f"/tree/{self.ref}/{self.path}" if self.ref else ""
        return f"https://github.com/{self.owner}/{self.repo}{tail}".rstrip("/")


def parse_github(url: str) -> GithubSource:
    """支持 https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]。"""
    m = re.match(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/tree/([^/\s]+)(?:/(.+?))?)?/?$", url.strip())
    if not m:
        raise PackageError("请填写 GitHub 地址，如 https://github.com/owner/repo/tree/main/skills/xxx")
    # 路径里的中文等字符可能是 URL 编码（%E6…），压缩包内是原文
    owner, repo, ref = m.group(1), m.group(2), urllib.parse.unquote(m.group(3) or "")
    path = urllib.parse.unquote(m.group(4) or "").strip("/")
    return GithubSource(owner, repo, ref, path)


def default_branch(src: GithubSource, http_get) -> str:
    data = http_get(f"https://api.github.com/repos/{src.owner}/{src.repo}", json=True)
    return str(data.get("default_branch") or "main")


def download_github(src: GithubSource, http_get) -> tuple[bytes, str]:
    """下载仓库 zip（codeload），返回 (zip 内容, 实际分支)。

    http_get(url, json=False) 由调用方提供（便于测试）。
    """
    ref = src.ref or default_branch(src, http_get)
    data = http_get(f"https://codeload.github.com/{src.owner}/{src.repo}/zip/refs/heads/{ref}")
    if not data:
        raise PackageError("下载失败：返回为空")
    return data, ref


def _safe_member(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not str(p).strip() or name.endswith("/"):
        return None
    return p


def extract(zip_bytes: bytes, dest: Path, subdir: str = "", strip_top: bool = False) -> int:
    """安全解压（防路径穿越、限制大小与文件数、忽略符号链接）；subdir 只取其中一个子目录。返回文件数。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise PackageError(f"不是有效的 zip 文件：{e}") from None
    members = [m for m in zf.infolist() if not m.is_dir()]
    total, count = 0, 0
    prefix = PurePosixPath(subdir) if subdir else None
    for m in members:
        p = _safe_member(m.filename)
        if p is None:
            continue
        if (m.external_attr >> 16) & 0o170000 == 0o120000:     # 符号链接
            continue
        if strip_top:
            if len(p.parts) < 2:
                continue
            p = PurePosixPath(*p.parts[1:])
        if prefix is not None:
            try:
                p = p.relative_to(prefix)
            except ValueError:
                continue
        total += m.file_size
        count += 1
        if total > MAX_TOTAL or count > MAX_FILES:
            raise PackageError(f"技能包过大（上限 {MAX_TOTAL // 1024 // 1024} MB、{MAX_FILES} 个文件）")
        target = dest.joinpath(*p.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
    if count == 0:
        raise PackageError("压缩包中没有找到文件" + (f"（子目录 {subdir}）" if subdir else ""))
    return count


def find_skill_root(root: Path) -> Path:
    """技能根目录：含 SKILL.md 的最浅目录（上传的 zip 可能多一层文件夹）。"""
    hits = sorted(root.rglob("SKILL.md"), key=lambda p: len(p.parts))
    if not hits:
        raise PackageError("没有找到 SKILL.md，不是技能包")
    return hits[0].parent


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """SKILL.md 开头的 YAML 元数据与正文。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise PackageError(f"SKILL.md 元数据格式错误：{e}") from None
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def _kind(p: Path, head: bytes) -> str:
    ext = p.suffix.lower()
    if head.startswith(b"\x7fELF") or head.startswith(b"MZ") or head[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        return "binary"
    if ext in SCRIPT_EXT or head.startswith(b"#!"):
        return "script"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def scan(root: Path) -> dict:
    """审阅清单：元数据、文件（路径 / 大小 / 类型 / sha256）、脚本、程序、出现的域名、风险提示与包摘要。"""
    meta, body = parse_frontmatter((root / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    files, domains, digest = [], set(), hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        digest.update(rel.encode() + b"\0" + h.encode())
        kind = _kind(p, data[:8])
        files.append({"path": rel, "size": len(data), "kind": kind, "sha256": h})
        if kind in ("doc", "script", "other") and len(data) < 2_000_000:
            domains |= {d.lower() for d in URL_RE.findall(data.decode("utf-8", errors="ignore"))}
    domains -= IGNORE_DOMAINS
    scripts = [f["path"] for f in files if f["kind"] == "script"]
    binaries = [f["path"] for f in files if f["kind"] == "binary"]
    warnings = []
    if binaries:
        warnings.append(f"包含 {len(binaries)} 个可执行程序文件，请确认来源可信")
    if scripts:
        warnings.append(f"包含 {len(scripts)} 个脚本，安装命令与 AI 调用都只在隔离环境中运行")
    text = (root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?i)curl[^\n|]*\|\s*(ba)?sh|wget[^\n|]*\|\s*(ba)?sh", text):
        warnings.append("说明中有「下载后直接执行」的安装方式")
    if re.search(r"(?i)api[\s_-]?key|token|密钥|授权", text):
        warnings.append("需要鉴权（API Key / 授权）：请在技能终端中自己完成，不要把密钥交给 AI")
    name = str(meta.get("name") or root.name)
    return {
        "meta": {k: (v if isinstance(v, str | int | float | bool) else str(v)) for k, v in meta.items()},
        "name": re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:64] or "skill",
        "display_name": str(meta.get("display_name") or meta.get("display_name_zh") or name)[:128],
        "description": str(meta.get("description_zh") or meta.get("description") or "")[:2000],
        "version": str(meta.get("version") or "")[:32],
        "files": files, "scripts": scripts, "binaries": binaries, "domains": sorted(domains),
        "warnings": warnings, "body_chars": len(body), "sha256": digest.hexdigest(),
    }


_SHELL_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "terminal"}
_NOT_COMMANDS = {"bash", "sh", "node", "python", "python3", "powershell", "source", "export", "cd", "for", "if",
                 "pip", "npm", "curl", "wget", "echo", "cat", "from", "import", "do", "done", "then", "else", "fi",
                 "while", "case", "esac", "set", "sudo", "uv", "npx"}


def suggest_commands(root: Path, manifest: dict) -> tuple[list[str], list[str], str]:
    """从说明中推测：AI 可调用的命令入口、仅限用户的子命令、安装命令（只是建议，审批时由用户确认）。"""
    text = (root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    cmds: list[str] = []
    # 只看命令行代码块（```bash / sh / shell / console 或未标语言）；```python 等代码示例里的 from / import 不是命令
    shell = "\n".join(body for lang, body in re.findall(r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", text, re.S)
                      if lang.lower() in _SHELL_LANGS)
    for m in re.finditer(r"(?m)^\s*\$?\s*([a-z][a-z0-9_-]{1,30})\s+[a-z-]", shell):
        c = m.group(1)
        if c not in cmds and c not in _NOT_COMMANDS:
            cmds.append(c)
    user_only = ["auth"] if re.search(r"(?i)\bauth\b", text) else []
    setup = ""
    if "scripts/setup.sh" in {f["path"] for f in manifest.get("files", [])}:
        setup = "bash scripts/setup.sh"
    return cmds[:5], user_only, setup


def new_staging(base: Path) -> Path:
    d = base / "_staging" / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return d
