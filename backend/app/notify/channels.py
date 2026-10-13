"""推送通道：把一条 Markdown 消息发到微信。

- serverchan：Server 酱（方糖服务号）。SendKey 以 SCT 开头走 sctapi.ftqq.com；以 sctp 开头为 Server 酱³；
- pushplus：PushPlus 公众号，token 为 32 位；
- wecom_bot：企业微信群机器人，填完整的 Webhook 地址或其中的 key（微信里可通过企业微信接收）。

只访问以上固定域名（不接受任意 URL，避免被用来访问内网地址）；密钥不写入日志。
"""

from __future__ import annotations

import re

import httpx

KINDS = {"serverchan": "Server 酱", "pushplus": "PushPlus", "wecom_bot": "企业微信群机器人"}
WECOM_LIMIT = 4000          # 企业微信 markdown 内容上限 4096 字节，留出余量
TIMEOUT = 15


class NotifyError(Exception):
    pass


def validate(kind: str, secret: str) -> str:
    """校验并规范化密钥，返回保存用的值。"""
    s = (secret or "").strip()
    if kind == "serverchan":
        if not re.fullmatch(r"(SCT|sctp)\w{6,80}", s):
            raise NotifyError("Server 酱 SendKey 应以 SCT 或 sctp 开头")
    elif kind == "pushplus":
        if not re.fullmatch(r"[0-9a-fA-F]{32}", s):
            raise NotifyError("PushPlus token 应为 32 位字母数字")
    elif kind == "wecom_bot":
        m = re.fullmatch(r"(?:https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=)?([0-9a-fA-F-]{20,64})", s)
        if not m:
            raise NotifyError("企业微信机器人请填写 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=… "
                              "地址或其中的 key")
        s = m.group(1)
    else:
        raise NotifyError(f"未知推送方式 {kind}")
    return s


def _cut_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip() + "\n……（内容过长，其余请在网页查看）"


def send(kind: str, secret: str, title: str, markdown: str, client: httpx.Client | None = None) -> None:
    """发送一条消息；失败抛出 NotifyError（消息里不含密钥）。"""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        if kind == "serverchan":
            if secret.startswith("sctp"):
                num = re.match(r"sctp(\d+)t", secret)
                if not num:
                    raise NotifyError("Server 酱³ SendKey 格式不正确")
                url = f"https://{num.group(1)}.push.ft07.com/send/{secret}.send"
            else:
                url = f"https://sctapi.ftqq.com/{secret}.send"
            r = client.post(url, data={"title": title[:100], "desp": markdown})
            ok = r.status_code == 200 and r.json().get("code") == 0
        elif kind == "pushplus":
            r = client.post("https://www.pushplus.plus/send",
                            json={"token": secret, "title": title[:100], "content": markdown, "template": "markdown"})
            ok = r.status_code == 200 and r.json().get("code") == 200
        elif kind == "wecom_bot":
            content = _cut_utf8(f"**{title}**\n{markdown}", WECOM_LIMIT)
            r = client.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send", params={"key": secret},
                            json={"msgtype": "markdown", "markdown": {"content": content}})
            ok = r.status_code == 200 and r.json().get("errcode") == 0
        else:
            raise NotifyError(f"未知推送方式 {kind}")
    except httpx.HTTPError as e:
        raise NotifyError(f"网络错误：{type(e).__name__}") from None
    except ValueError:
        raise NotifyError("推送服务返回的内容无法解析") from None
    finally:
        if own:
            client.close()
    if not ok:
        try:
            msg = r.json().get("message") or r.json().get("msg") or r.json().get("errmsg") or ""
        except ValueError:
            msg = ""
        raise NotifyError(f"推送失败（HTTP {r.status_code}）{('：' + str(msg)[:200]) if msg else ''}")
