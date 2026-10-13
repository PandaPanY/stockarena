"""技能系统：包解析与安全解压、出网代理、命令策略、暂存 → 批准 → 安装 → 调用 → 审计（docker 用替身）。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile

import pytest

from app.skills import egress_proxy, gate, package, sandbox, service


@pytest.fixture(autouse=True)
def _skills_on(settings, monkeypatch):
    """这些用例测试技能本身：打开总开关（默认关闭的行为见 test_skills_gate.py）。"""
    from app.core.config import get_settings

    monkeypatch.setenv("SA_SKILLS_ENABLED", "true")
    get_settings.cache_clear()          # 隔离用的 settings fixture 已经读过一次配置
    gate.reset_cache()
    yield
    gate.reset_cache()

SKILL_MD = """---
name: westockdata
display_name: 腾讯自选股-金融数据查询
description: 金融数据查询
version: 1.0.12
---
# WeStock Data

```bash
bash scripts/setup.sh
```

```bash
westock search 宁德时代
westock quote sh600519
```

鉴权：使用前需先完成专属 APIKey 鉴权，`westock auth status`。
下载地址 https://stockbuddy.qq.com/release/cli
"""


def make_zip(files: dict[str, bytes], top: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(f"{top}{name}", data)
    return buf.getvalue()


PKG = {"SKILL.md": SKILL_MD.encode(), "scripts/setup.sh": b"#!/bin/bash\ncurl -fsSL https://stockbuddy.qq.com/x\n",
       "references/commands.md": b"# commands\n", "bin/tool": b"\x7fELF\x02\x01\x01" + b"\0" * 40}


def test_parse_github():
    s = package.parse_github("https://github.com/anthropics/skills/tree/main/skills/pdf")
    assert (s.owner, s.repo, s.ref, s.path) == ("anthropics", "skills", "main", "skills/pdf")
    s = package.parse_github("https://github.com/d/h/tree/main/skills/%E6%99%BA%E8%83%BD%E9%80%89%E8%82%A1/x")
    assert s.path == "skills/智能选股/x"
    s = package.parse_github("https://github.com/a/b.git")
    assert (s.repo, s.ref, s.path) == ("b", "", "")
    with pytest.raises(package.PackageError):
        package.parse_github("https://gitlab.com/a/b")


def test_extract_is_safe(tmp_path):
    data = make_zip({"../evil.txt": b"x", "/abs.txt": b"y", "ok/SKILL.md": b"---\nname: a\n---\n"})
    n = package.extract(data, tmp_path / "out")
    assert n == 1 and (tmp_path / "out/ok/SKILL.md").exists() and not (tmp_path / "evil.txt").exists()
    with pytest.raises(package.PackageError):
        package.extract(b"not a zip", tmp_path / "x")
    # GitHub 仓库 zip：去掉顶层目录，只取子目录
    data = make_zip({"skills/pdf/SKILL.md": b"---\nname: pdf\n---\n", "README.md": b"r"}, top="skills-main/")
    package.extract(data, tmp_path / "gh", subdir="skills/pdf", strip_top=True)
    assert [p.name for p in (tmp_path / "gh").iterdir()] == ["SKILL.md"]


def test_scan_and_suggestions(tmp_path):
    package.extract(make_zip(PKG, top="westockdata/"), tmp_path)
    root = package.find_skill_root(tmp_path)
    m = package.scan(root)
    assert m["name"] == "westockdata" and m["display_name"] == "腾讯自选股-金融数据查询"
    assert m["scripts"] == ["scripts/setup.sh"] and m["binaries"] == ["bin/tool"]
    assert "stockbuddy.qq.com" in m["domains"]
    assert any("可执行程序" in w for w in m["warnings"]) and any("鉴权" in w for w in m["warnings"])
    cmds, user_only, setup = package.suggest_commands(root, m)
    assert cmds == ["westock"] and user_only == ["auth"] and setup == "bash scripts/setup.sh"


def test_proxy_rules():
    assert egress_proxy.host_allowed("api.stockbuddy.qq.com", ["qq.com"])
    assert egress_proxy.host_allowed("qq.com", ["*.qq.com"])
    assert not egress_proxy.host_allowed("evilqq.com", ["qq.com"])
    assert not egress_proxy.host_allowed("github.com", [])
    auth = "Basic " + base64.b64encode(b"tok123:x").decode()
    assert egress_proxy.token_of({"proxy-authorization": auth}) == "tok123"


async def test_proxy_allows_and_blocks(tmp_path, monkeypatch):
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"tok": {"skill": "s", "domains": ["localhost"]}}))
    monkeypatch.setattr(egress_proxy, "ALLOW_FILE", str(allow))
    monkeypatch.setattr(egress_proxy, "LOG_FILE", str(tmp_path / "proxy.log"))
    egress_proxy._cache.update({"mtime": None, "data": {}})

    async def echo(r, w):
        w.write(await r.read(5))
        await w.drain()
        w.close()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    proxy = await asyncio.start_server(egress_proxy.handle, "127.0.0.1", 0)
    tport, pport = target.sockets[0].getsockname()[1], proxy.sockets[0].getsockname()[1]
    auth = base64.b64encode(b"tok:x").decode()

    async def connect(host: str, token_auth: str) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", pport)
        w.write(f"CONNECT {host}:{tport} HTTP/1.1\r\nProxy-Authorization: Basic {token_auth}\r\n\r\n".encode())
        await w.drain()
        line = await r.readline()
        if b"200" in line:
            await r.readline()                       # 状态行之后的空行
            w.write(b"hello")
            await w.drain()
            line += await r.read(5)
        w.close()
        return line

    async with target, proxy:
        assert (await connect("localhost", auth)).endswith(b"hello")                 # 允许的域名：隧道打通
        assert b"403" in await connect("example.org", auth)                          # 未声明的域名：拒绝
        assert b"403" in await connect("localhost", base64.b64encode(b"bad:x").decode())   # 错误令牌：拒绝
    logs = [json.loads(x) for x in (tmp_path / "proxy.log").read_text().splitlines()]
    assert [x["allowed"] for x in logs] == [True, False, False]


def test_docker_args_are_locked_down(settings):
    args = sandbox.docker_args(7, "tok", ["westock", "quote", "sh600519"], "n")
    joined = " ".join(args)
    for flag in ("--cap-drop ALL", "no-new-privileges", "--read-only", f"--network {sandbox.NETWORK}",
                 "--pids-limit", ":/skill:ro", f"HTTPS_PROXY=http://tok:x@{sandbox.GATEWAY}:3128"):
        assert flag in joined
    assert args[-3:] == ["westock", "quote", "sh600519"]
    assert "stockarena.db" not in joined and "secret.key" not in joined


def test_session_container_reused_until_stopped(settings, monkeypatch):
    """终端会话：首次命令启动常驻容器，之后的命令 docker exec 进同一个容器；结束会话即删除容器。"""
    execs, procs = [], []
    running = {"v": False}

    async def fake_exec(*args, timeout=120, stdin=None):
        execs.append(list(args))
        if args[:2] == ("docker", "inspect"):
            return (0, "true") if running["v"] else (1, "No such object")
        if args[:2] == ("docker", "run"):
            running["v"] = True
        if args[:2] == ("docker", "rm"):
            running["v"] = False
        return 0, ""

    async def fake_proc(cmd, kill_name, token, timeout):
        procs.append(cmd)
        return sandbox.RunResult(0, "ok", 0.1)

    monkeypatch.setattr(sandbox, "_exec", fake_exec)
    monkeypatch.setattr(sandbox, "_run_proc", fake_proc)

    async def scenario():
        await sandbox.run_session(3, "tok", ["westock", "auth", "status"])
        await sandbox.run_session(3, "tok", ["westock", "auth", "fetch"])
        assert await sandbox.stop_session(3)
        if sandbox._reaper:
            sandbox._reaper.cancel()

    asyncio.run(scenario())
    starts = [a for a in execs if a[:2] == ["docker", "run"]]
    assert len(starts) == 1                                                  # 只启动一次
    joined = " ".join(starts[0])
    for flag in ("--cap-drop ALL", "--read-only", f"--network {sandbox.NETWORK}", " -d ", "--init", "--rm"):
        assert flag in joined
    assert starts[0][-2:] == ["sleep", "infinity"] and "sa-skill-3-term" in joined
    assert procs == [["docker", "exec", "sa-skill-3-term", "westock", "auth", "status"],
                     ["docker", "exec", "sa-skill-3-term", "westock", "auth", "fetch"]]
    assert execs[-1] == ["docker", "rm", "-f", "sa-skill-3-term"] and 3 not in sandbox._sessions


@pytest.fixture
def fake_docker(monkeypatch):
    calls = []

    async def fake_run(skill_id, token, argv, timeout=90):
        calls.append(argv)
        return sandbox.RunResult(0, f"ran {' '.join(argv)}", 0.1, ["blocked.example.net"] if argv[0] == "bash" else [])

    async def ready():
        return {"ready": True, "docker": True, "message": "ok"}

    async def fake_session(skill_id, token, argv, timeout=300):
        calls.append(["(终端)", *argv])
        return sandbox.RunResult(0, f"ran {' '.join(argv)}", 0.1)

    async def fake_stop(skill_id):
        calls.append(["(结束终端)"])
        return True

    monkeypatch.setattr(sandbox, "run", fake_run)
    monkeypatch.setattr(sandbox, "run_session", fake_session)
    monkeypatch.setattr(sandbox, "stop_session", fake_stop)
    monkeypatch.setattr(sandbox, "status", ready)
    return calls


def test_stage_approve_install_and_call(client, fake_docker):
    data = base64.b64encode(make_zip(PKG, top="westockdata/")).decode()
    r = client.post("/api/skills/stage", json={"source": "upload", "filename": "w.zip", "content_base64": data})
    assert r.status_code == 200, r.text
    sk = r.json()
    assert sk["status"] == "pending" and not sk["enabled"] and sk["commands"] == ["westock"]
    assert sk["summary"]["binaries"] == 1 and sk["manifest"]["sha256"]
    sid = sk["id"]
    # 待审批时 AI 不能用
    assert client.post(f"/api/skills/{sid}/run", json={"argv": ["westock", "quote"]}).status_code == 400
    assert "curl" in client.get(f"/api/skills/{sid}/file", params={"path": "scripts/setup.sh"}).json()["content"]
    assert client.get(f"/api/skills/{sid}/file", params={"path": "../../stockarena.db"}).status_code == 404
    r = client.patch(f"/api/skills/{sid}", json={"allow_domains": ["qq.com", "bad domain!"]})
    assert r.status_code == 400
    client.patch(f"/api/skills/{sid}", json={"allow_domains": ["qq.com"]})
    assert client.post(f"/api/skills/{sid}/approve").status_code == 200
    for _ in range(50):
        sk = client.get(f"/api/skills/{sid}").json()
        if sk["status"] != "installing":
            break
    assert sk["status"] == "installed" and sk["enabled"]
    assert fake_docker[0] == ["bash", "-lc", "bash scripts/setup.sh"]
    assert "blocked.example.net" in sk["install_log"]
    # 用户终端：可以执行鉴权子命令
    r = client.post(f"/api/skills/{sid}/run", json={"argv": ["westock", "auth", "status"]})
    assert r.status_code == 200 and r.json()["output"] == "ran westock auth status"
    assert fake_docker[-1] == ["(终端)", "westock", "auth", "status"]          # 用户命令在终端会话容器中执行
    assert client.delete(f"/api/skills/{sid}/session").json() == {"ok": True}
    # 只能调用声明的命令
    assert client.post(f"/api/skills/{sid}/run", json={"argv": ["bash", "-c", "cat ~/.westock"]}).status_code == 400
    calls = client.get(f"/api/skills/{sid}/calls").json()
    assert [c["caller"] for c in calls] == ["user", "setup"]
    # 出网代理的允许清单只包含已安装技能
    allow = json.loads((sandbox.proxy_dir() / "allow.json").read_text())
    assert list(allow.values()) == [{"skill": "westockdata", "domains": ["qq.com"]}]
    assert client.delete(f"/api/skills/{sid}").status_code == 200
    assert json.loads((sandbox.proxy_dir() / "allow.json").read_text()) == {}


def test_ai_cannot_run_user_only_or_read_outside(client, fake_docker):
    from app.db.session import get_sessionmaker
    from app.models.skills import SkillRow

    data = base64.b64encode(make_zip(PKG)).decode()
    sid = client.post("/api/skills/stage", json={"source": "upload", "filename": "w.zip",
                                                 "content_base64": data}).json()["id"]
    client.post(f"/api/skills/{sid}/approve")
    with get_sessionmaker()() as s:
        row = s.get(SkillRow, sid)
        assert row.status == "installed"
        with pytest.raises(service.SkillError, match="只允许用户"):
            service.check_argv(row, ["westock", "auth", "status"], "ai:review")
        assert service.check_argv(row, ["westock", "quote", "sh600519"], "ai:review")[0] == "westock"
        assert "WeStock" in service.read_file(row, "SKILL.md")
        with pytest.raises(service.SkillError):
            service.read_file(row, "../home/.westock/key")
        with pytest.raises(service.SkillError, match="二进制"):
            service.read_file(row, "bin/tool")


def _fake_web(calls: list):
    def get(url, json=False):  # noqa: A002
        calls.append(url)
        if url == "https://api.github.com/repos/anthropics/skills":
            return {"default_branch": "main"}
        if "git/trees" in url:
            return {"tree": [{"type": "blob", "path": "skills/pdf/SKILL.md"},
                             {"type": "blob", "path": "skills/xlsx/SKILL.md"},
                             {"type": "blob", "path": "README.md"}]}
        if url.endswith("skills/pdf/SKILL.md"):
            return b"---\nname: pdf\ndescription: Read and fill PDF forms\n---\n"
        if url.endswith("skills/xlsx/SKILL.md"):
            return b"---\nname: xlsx\ndescription: Spreadsheet analysis and formulas\n---\n"
        if url.startswith("https://skillsmp.com/"):
            return {"success": True, "data": {"skills": [
                {"name": "a-share-data", "description": "A股行情与财报", "author": "x", "stars": 10,
                 "githubUrl": "https://github.com/x/fin/tree/main/skills/a-share-data", "contentLanguage": "zh"},
                {"name": "bad", "description": "no github", "githubUrl": "http://evil.example/x"}]}}
        if url.startswith("https://codeload.github.com/x/fin/"):
            return make_zip({"skills/a-share-data/SKILL.md": b"---\nname: a-share-data\n---\n# A\n"}, top="fin-main/")
        raise AssertionError(url)
    return get


def test_market_search_and_stage(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(service, "http_get", _fake_web(calls))
    markets = client.get("/api/skills/markets").json()
    assert {m["kind"] for m in markets} == {"github", "skillsmp"}            # 默认两个市场
    res = client.get("/api/skills/market-search", params={"q": "pdf"}).json()
    names = {(r["market"], r["name"]) for r in res}
    assert ("Anthropic 官方技能", "pdf") in names and ("SkillsMP 社区技能索引", "a-share-data") in names
    assert all(r["url"].startswith("https://github.com/") for r in res)       # 没有 GitHub 地址的结果被丢弃
    gh = next(r for r in res if r["name"] == "pdf")
    assert gh["url"] == "https://github.com/anthropics/skills/tree/main/skills/pdf"
    n = len(calls)
    client.get("/api/skills/market-search", params={"q": "pdf"})
    assert len([c for c in calls[n:] if "skillsmp" in c]) == 0                # SkillsMP 结果缓存一天
    # 从市场获取：与 GitHub 安装相同，只暂存待审
    sm = next(r for r in res if r["name"] == "a-share-data")
    r = client.post("/api/skills/markets/stage", json={"url": sm["url"], "market_id": sm["market_id"]})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["source_type"] == "market"
    assert client.get("/api/skills/market-search", params={"q": "A股"}).json()[0]["staged"] is True
    assert client.get("/api/skills/market-search", params={"q": "x"}).status_code == 400
    assert client.post("/api/skills/markets", json={"name": "坏", "kind": "github",
                                                    "url": "https://gitlab.com/a/b"}).status_code == 400


async def test_ai_skill_request_flow(client, fake_docker, monkeypatch):
    from app.db.session import get_sessionmaker
    from app.notify import service as notify
    from app.skills import requests, tools

    monkeypatch.setattr(service, "http_get", _fake_web([]))
    sent = []
    monkeypatch.setattr(notify, "deliver", lambda msg, sender=None: sent.append(msg))
    url = "https://github.com/x/fin/tree/main/skills/a-share-data"
    # AI 搜索市场、提交申请：技能包暂存待审，推送给用户
    client.get("/api/skills/markets")
    out = await tools.execute_market("search_skill_market", {"query": "A股"}, "AI 复核员")
    assert "a-share-data" in out and url in out
    msg = await tools.execute_market("request_skill_install", {"url": url, "reason": "需要业绩预告数据"}, "AI 复核员")
    assert "已提交安装申请" in msg
    assert len(sent) == 1 and sent[0].event == "skills" and "需要业绩预告数据" in sent[0].body
    assert "AI 不能自行安装" in sent[0].body
    # 重复申请合并；AI 不能安装
    again = await tools.execute_market("request_skill_install", {"url": url, "reason": "再来"}, "AI 复核员")
    assert "已在等待用户审批" in again and len(sent) == 1
    reqs = client.get("/api/skills/requests").json()
    req = reqs["requests"][0]
    assert req["status"] == "pending" and req["skill_name"] == "a-share-data" and reqs["settings"]["ai_requests"]
    skill = client.get(f"/api/skills/{req['skill_id']}").json()
    assert skill["status"] == "pending" and skill["request_id"] == req["id"]
    # 用户批准 → 申请标记为已批准
    client.post(f"/api/skills/{req['skill_id']}/approve")
    assert client.get("/api/skills/requests").json()["requests"][0]["status"] == "approved"
    installed = await tools.execute_market("request_skill_install", {"url": url, "reason": "x"}, "AI 复核员")
    assert "已安装" in installed
    # 关闭开关后 AI 不能申请；非 GitHub 地址被拒绝
    client.put("/api/skills/settings", json={"ai_requests": False})
    with get_sessionmaker()() as s:
        assert tools.market_enabled(s) is False
        r, why = requests.create(s, requester="ai", url="https://github.com/y/z", reason="r")
        assert r is None and "关闭" in why
        requests.set_settings(s, ai_requests=True)
        r, why = requests.create(s, requester="ai", url="http://evil.example/x", reason="r")
        assert r is None and "GitHub" in why
        s.commit()


def test_request_cap_and_reject(client, monkeypatch):
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.skills import requests

    monkeypatch.setattr(service, "http_get", _fake_web([]))

    def zip_for(url, json=False):  # noqa: A002
        if url.startswith("https://codeload.github.com/"):
            name = url.split("/")[4]
            return make_zip({"SKILL.md": f"---\nname: {name}\n---\n".encode()}, top=f"{name}-main/")
        return {"default_branch": "main"}

    with get_sessionmaker()() as s:
        for k in range(requests.MAX_PENDING):
            r, _ = requests.create(s, requester="ai", url=f"https://github.com/o/r{k}", reason="r", getter=zip_for)
            assert r is not None and r.status == "pending"
        r, why = requests.create(s, requester="ai", url="https://github.com/o/r9", reason="r", getter=zip_for)
        assert r is None and "暂不能再申请" in why
        s.commit()
        rid = s.scalars(select(requests.SkillRequestRow.id)).first()
    r = client.post(f"/api/skills/requests/{rid}/reject").json()
    assert r["status"] == "rejected"


def test_suggest_commands_ignores_code_examples(tmp_path):
    """```python 示例里的 from / import 不是命令（曾把 Vibe-Trading 技能的命令误识别为 from）。"""
    (tmp_path / "SKILL.md").write_text(
        "# 示例\n```python\nfrom src.tools.fund_flow_tool import FundFlowTool\nprint(FundFlowTool().execute())\n```\n"
        "```bash\n$ westock quote sh600519\nfor x in a b; do echo $x; done\n```\n```\ntool auth status\n```\n",
        encoding="utf-8")
    cmds, user_only, setup = package.suggest_commands(tmp_path, {"files": []})
    assert cmds == ["westock", "tool"] and user_only == ["auth"] and setup == ""


def test_secrets_redacted_from_output_and_audit():
    """技能报错时把带密钥的请求地址打印出来（westock 的 token=…），交给 AI 和写入记录前必须打码。"""
    key = "fakefakefake0123456789abcdef"
    out = sandbox.redact(
        f'Post "https://proxy.example.com/x?app=a&dev_id=dev_1&token={key}": timeout\n'
        f'{{"api_key": "{key}"}} Authorization: Bearer {key}{key} http://tok123:x@172.31.250.1:3128\n'
        "AUTH_REQUIRED:https://gu.qq.com/auth?code=abcdefghijklmnop&channel=wbgeneral")
    assert key not in out and "tok123" not in out
    assert "token=***" in out and "dev_id=dev_1" in out
    assert "code=abcdefghijklmnop" in out                 # 授权链接要给用户看，不能打码
    assert service.audit_argv(["westock", "auth", "set", key], ["auth"]) == ["westock", "auth", "set", "***"]
    assert service.audit_argv(["westock", "auth", "status"], ["auth"]) == ["westock", "auth", "status"]
    assert service.audit_argv(["westock", "quote", "sh600519"], ["auth"]) == ["westock", "quote", "sh600519"]
