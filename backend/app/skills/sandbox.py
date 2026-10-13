"""技能沙箱：每条命令在一次性 Docker 容器中运行。

容器限制：
- 只挂载该技能的包（只读，/skill）与它自己的工作目录（可写，HOME=/home/skill）；看不到平台的数据库、密钥、配置；
- 不以 root 运行（与服务进程同一个普通用户），去掉全部 capabilities、禁止提权，根文件系统只读（/tmp 为内存盘），
  限 CPU、内存、进程数，超时强制结束；
- 网络：容器只连着一个没有外网出口的内部网络（--internal，固定网段），唯一能访问的是监听在该网络网关地址上的
  出网过滤代理（egress_proxy.py，以主机网络运行，只绑定网关地址，局域网访问不到）；代理按该技能的令牌只放行它声明的域名，
  其余一律拒绝并记录。这样不依赖宿主机的 IP 转发（服务器上 net.ipv4.ip_forward=0，容器本身无法直接出网）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

IMAGE = "stockarena-skill-runner:1"
NETWORK = "sa-skill-net"
SUBNET, GATEWAY = "172.31.250.0/24", "172.31.250.1"
PROXY = "sa-skill-proxy"
PROXY_PORT = 3128
OUTPUT_LIMIT = 200_000
DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
        bash ca-certificates curl coreutils procps python3 nodejs unzip xz-utils \\
    && rm -rf /var/lib/apt/lists/*
"""


@dataclass
class RunResult:
    exit_code: int | None
    output: str
    seconds: float
    blocked: list[str] = field(default_factory=list)
    timed_out: bool = False


def base_dir() -> Path:
    return get_settings().data_dir / "skills"


def proxy_dir() -> Path:
    d = base_dir() / "_proxy"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def skill_dirs(skill_id: int) -> tuple[Path, Path]:
    """(包目录, 工作目录)。"""
    root = base_dir() / str(skill_id)
    return root / "pkg", root / "home"


async def _exec(*args: str, timeout: float = 120, stdin: bytes | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return -1, "超时"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def docker_available() -> bool:
    return shutil.which("docker") is not None


NO_DOCKER = ("找不到 docker 命令，无法运行技能。技能需要在装有 Docker 的机器上直接运行 StockArena"
             "（Docker 部署方式暂不支持），见 docs/SKILLS.md")


async def status() -> dict:
    """沙箱是否就绪：docker、镜像、内部网络、代理容器。"""
    if not docker_available():
        return {"ready": False, "docker": False, "message": NO_DOCKER}
    code, _ = await _exec("docker", "image", "inspect", IMAGE, timeout=30)
    image = code == 0
    code, _ = await _exec("docker", "network", "inspect", NETWORK, timeout=30)
    network = code == 0
    code, out = await _exec("docker", "inspect", "-f", "{{.State.Running}}", PROXY, timeout=30)
    proxy = code == 0 and out.strip() == "true"
    ready = image and network and proxy
    return {"ready": ready, "docker": True, "image": image, "network": network, "proxy": proxy,
            "message": "沙箱已就绪" if ready else "沙箱未就绪，请点「初始化沙箱」"}


async def setup() -> str:
    """构建运行镜像、创建内部网络、启动出网代理（幂等）。"""
    log: list[str] = []
    st = await status()
    if not st.get("docker"):
        raise RuntimeError(st["message"])
    if not st["image"]:
        build_dir = base_dir() / "_image"
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        # 以主机网络构建：服务器关闭了 IP 转发，默认桥接网络的容器无法出网
        code, out = await _exec("docker", "build", "--network", "host", "-t", IMAGE, str(build_dir), timeout=1200)
        log.append(f"构建镜像：{'成功' if code == 0 else '失败'}\n{out[-2000:]}")
        if code != 0:
            raise RuntimeError("\n".join(log))
    if not st["network"]:
        code, out = await _exec("docker", "network", "create", "--internal", "--subnet", SUBNET, "--gateway", GATEWAY,
                                NETWORK, timeout=60)
        log.append(f"创建内部网络：{out.strip()[:200]}")
        if code != 0:
            raise RuntimeError("\n".join(log))
    if not st["proxy"]:
        pdir = proxy_dir()
        shutil.copyfile(Path(__file__).with_name("egress_proxy.py"), pdir / "egress_proxy.py")
        if not (pdir / "allow.json").exists():
            (pdir / "allow.json").write_text("{}", encoding="utf-8")
        await _exec("docker", "rm", "-f", PROXY, timeout=60)
        uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
        # 代理以主机网络运行（可以出网），只绑定内部网络的网关地址（局域网访问不到）
        code, out = await _exec(
            "docker", "run", "-d", "--name", PROXY, "--restart", "unless-stopped", "--user", uid, "--network", "host",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--memory", "256m",
            "-e", f"BIND={GATEWAY}", "-e", f"PORT={PROXY_PORT}",
            "-v", f"{pdir}:/proxy:ro", "-v", f"{pdir / 'logs'}:/logs", IMAGE, "python3", "/proxy/egress_proxy.py",
            timeout=120)
        log.append(f"启动代理：{out.strip()[:200]}")
        if code != 0:
            raise RuntimeError("\n".join(log))
    return "\n".join(log) or "沙箱已就绪"


def write_allow(entries: dict[str, dict]) -> None:
    """令牌 → {skill, domains}。原子写入，代理按修改时间重新读取。"""
    pdir = proxy_dir()
    tmp = pdir / f"allow.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, pdir / "allow.json")


def new_token() -> str:
    return secrets.token_hex(16)


def _blocked_since(token: str, since: float) -> list[str]:
    path = proxy_dir() / "logs" / "proxy.log"
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f.readlines()[-5000:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) >= since and r.get("token") == token[:8] and not r.get("allowed") and \
                    r.get("host") not in out:
                out.append(r["host"])
    return out


# 技能输出里的密钥打码：有的技能在报错时把带密钥的请求地址原样打印（如 westock 的 token=…），
# 输出会交给 AI、写进调用记录，必须先去掉。只打码参数名明确是密钥的值，不动授权链接里的 code=。
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b((?:access_|refresh_)?token|api[_-]?key|apikey|secret|password|passwd|signature)"
                r"=([^&\s\"'<>]{8,})"), r"\1=***"),
    (re.compile(r"(?i)(\"(?:(?:access_|refresh_)?token|api[_-]?key|apikey|secret|password)\"\s*:\s*\")([^\"]{8,})\""),
     r'\1***"'),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(https?://)[^\s:/@]+:[^\s/@]*@"), r"\1***@"),        # 代理地址里的令牌
]


def redact(text: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def docker_args(skill_id: int, token: str, argv: list[str], name: str, cpus: str = "2",
                memory: str = "1g") -> list[str]:
    """组装 docker run 参数（单独成函数便于测试）。"""
    pkg, home = skill_dirs(skill_id)
    uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    proxy = f"http://{token}:x@{GATEWAY}:{PROXY_PORT}"
    env = {"HOME": "/home/skill", "PATH": "/home/skill/.local/bin:/usr/local/bin:/usr/bin:/bin",
           "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
           "NO_PROXY": "", "LANG": "C.UTF-8", "TERM": "dumb"}
    args = ["docker", "run", "--rm", "--name", name, "--network", NETWORK, "--user", uid,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
            "--memory", memory, "--cpus", cpus, "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{pkg}:/skill:ro", "-v", f"{home}:/home/skill", "-w", "/skill"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return [*args, IMAGE, *argv]


async def run(skill_id: int, token: str, argv: list[str], timeout: float = 90) -> RunResult:
    """在沙箱中运行一条命令（argv 直接执行，不经过 shell）。"""
    pkg, home = skill_dirs(skill_id)
    home.mkdir(parents=True, exist_ok=True)
    name = f"sa-skill-{skill_id}-{uuid.uuid4().hex[:8]}"
    return await _run_proc(docker_args(skill_id, token, argv, name), name, token, timeout)


async def _run_proc(cmd: list[str], kill_name: str, token: str, timeout: float) -> RunResult:
    """执行 docker run / docker exec，超时强制结束容器 kill_name。"""
    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await _exec("docker", "kill", kill_name, timeout=30)
        out, _ = await proc.communicate()
    text = redact(out.decode("utf-8", errors="replace"))
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + f"\n……（输出过长，已截断，共 {len(text)} 字符）"
    return RunResult(None if timed_out else proc.returncode, text, round(time.time() - t0, 2),
                     _blocked_since(token, t0 - 1), timed_out)


# ---------------------------------------------------------------- 终端会话


# 有的技能把「设备」标识绑定在容器的这一次运行上（如 westock 的授权：auth status 生成授权码、auth fetch 领取，
# 两次必须是同一台设备），一次性容器做不到。技能终端因此使用会话容器：同一个终端里的命令都在同一个容器中执行
# （docker exec），限制与一次性容器完全相同；关闭终端或闲置超时后销毁。AI 调用仍然每次用一次性容器。
SESSION_IDLE = 15 * 60
SESSION_SUFFIX = "-term"
_sessions: dict[int, float] = {}          # skill_id -> 最近使用时间
_reaper: asyncio.Task | None = None


def session_name(skill_id: int) -> str:
    return f"sa-skill-{skill_id}{SESSION_SUFFIX}"


def session_args(skill_id: int, token: str) -> list[str]:
    """会话容器：常驻（sleep），--init 回收孤儿进程，其余参数与一次性容器相同。"""
    name = session_name(skill_id)
    args = docker_args(skill_id, token, ["sleep", "infinity"], name)
    i = args.index("--rm")
    return [*args[:i + 1], "-d", "--init", *args[i + 1:]]


async def _ensure_session(skill_id: int, token: str) -> str:
    name = session_name(skill_id)
    code, out = await _exec("docker", "inspect", "-f", "{{.State.Running}}", name, timeout=30)
    if code == 0 and out.strip() == "true":
        return name
    await _exec("docker", "rm", "-f", name, timeout=30)
    _, home = skill_dirs(skill_id)
    home.mkdir(parents=True, exist_ok=True)
    code, out = await _exec(*session_args(skill_id, token), timeout=60)
    if code != 0:
        raise RuntimeError(f"启动终端会话失败：{out.strip()[-300:]}")
    return name


async def run_session(skill_id: int, token: str, argv: list[str], timeout: float = 300) -> RunResult:
    """在该技能的终端会话容器中运行一条命令（不存在则先启动）。超时会结束整个会话。"""
    global _reaper
    name = await _ensure_session(skill_id, token)
    _sessions[skill_id] = time.time()
    if _reaper is None or _reaper.done():
        _reaper = asyncio.get_running_loop().create_task(_reap_loop())
    res = await _run_proc(["docker", "exec", name, *argv], name, token, timeout)
    _sessions[skill_id] = time.time()
    if res.timed_out:
        _sessions.pop(skill_id, None)
    return res


async def stop_session(skill_id: int) -> bool:
    """结束终端会话（关闭终端、卸载技能时调用）。"""
    existed = _sessions.pop(skill_id, None) is not None
    code, _ = await _exec("docker", "rm", "-f", session_name(skill_id), timeout=30)
    return existed or code == 0


async def _reap_loop() -> None:
    while _sessions:
        await asyncio.sleep(60)
        now = time.time()
        for sid, last in list(_sessions.items()):
            if now - last > SESSION_IDLE:
                logger.info("技能 %s 的终端会话闲置超时，已结束", sid)
                await stop_session(sid)


async def cleanup_sessions() -> None:
    """服务启动 / 停止时清理遗留的会话容器（进程重启后无法得知它们的闲置时间）。"""
    _sessions.clear()
    if not docker_available():
        return
    try:
        code, out = await _exec("docker", "ps", "-aq", "--filter", f"name=^sa-skill-[0-9]+{SESSION_SUFFIX}$",
                                timeout=30)
        ids = out.split() if code == 0 else []
        if ids:
            await _exec("docker", "rm", "-f", *ids, timeout=60)
    except OSError:  # docker 不可用时不影响服务启停
        logger.warning("清理技能终端会话失败", exc_info=True)
