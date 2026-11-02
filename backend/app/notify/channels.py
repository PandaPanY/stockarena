"""推送通道：把一条 Markdown 消息发到微信、飞书、钉钉或 Telegram。

- serverchan：Server 酱（方糖服务号）。SendKey 以 SCT 开头走 sctapi.ftqq.com；以 sctp 开头为 Server 酱³；
- pushplus：PushPlus 公众号，token 为 32 位；
- wecom_bot：企业微信群机器人，填完整的 Webhook 地址或其中的 key（微信里可通过企业微信接收）；
- feishu_bot：飞书 / Lark 群机器人，Webhook 地址或其中的 token；开启了签名校验时在后面加「#签名密钥」；
- dingtalk_bot：钉钉群机器人，Webhook 地址或其中的 access_token；开启了加签时在后面加「#SEC 开头的密钥」；
- telegram：Telegram 机器人，「bot token#chat id」（服务器需要能访问 api.telegram.org，必要时配置代理）。

只访问以上固定域名（不接受任意 URL，避免被用来访问内网地址）；密钥不写入日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import quote_plus

import httpx

KINDS = {"serverchan": "Server 酱", "pushplus": "PushPlus", "wecom_bot": "企业微信群机器人",
         "feishu_bot": "飞书群机器人", "dingtalk_bot": "钉钉群机器人", "telegram": "Telegram 机器人"}
WECOM_LIMIT = 4000          # 企业微信 markdown 内容上限 4096 字节，留出余量
FEISHU_LIMIT = 20000        # 飞书卡片约 30 KB
DINGTALK_LIMIT = 18000      # 钉钉 markdown 约 20000 字节
TELEGRAM_LIMIT = 4000       # Telegram 单条 4096 字符
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
    elif kind == "feishu_bot":
        body, _, sign = s.partition("#")
        m = re.fullmatch(r"(?:https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/)?([0-9a-zA-Z-]{20,64})",
                         body.strip())
        if not m:
            raise NotifyError("飞书机器人请填写 https://open.feishu.cn/open-apis/bot/v2/hook/… 地址或其中的 token")
        s = ("lark:" if m.group(1) == "larksuite.com" else "") + m.group(2)
        s += f"#{sign.strip()}" if sign.strip() else ""
    elif kind == "dingtalk_bot":
        body, _, sign = s.partition("#")
        m = re.fullmatch(r"(?:https://oapi\.dingtalk\.com/robot/send\?access_token=)?([0-9a-zA-Z]{20,80})",
                         body.strip())
        if not m:
            raise NotifyError("钉钉机器人请填写 https://oapi.dingtalk.com/robot/send?access_token=… 地址或其中的 token")
        if sign.strip() and not sign.strip().startswith("SEC"):
            raise NotifyError("钉钉加签密钥以 SEC 开头")
        s = m.group(1) + (f"#{sign.strip()}" if sign.strip() else "")
    elif kind == "telegram":
        if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}#(-?\d{1,20}|@[A-Za-z0-9_]{5,64})", s):
            raise NotifyError("Telegram 请填写「bot token#chat id」，如 123456789:AA…#-1001234567890")
    else:
        raise NotifyError(f"未知推送方式 {kind}")
    return s


def _feishu_sign(secret: str, ts: int) -> str:
    key = f"{ts}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def _dingtalk_sign(secret: str, ts_ms: int) -> str:
    digest = hmac.new(secret.encode(), f"{ts_ms}\n{secret}".encode(), hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


def _plain(markdown: str) -> str:
    """Telegram 用纯文本发送：去掉加粗、引用等 Markdown 记号（避免转义问题）。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", markdown)
    return re.sub(r"(?m)^> ?", "", text)


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
        elif kind == "feishu_bot":
            token, _, sign = secret.partition("#")
            domain = "open.larksuite.com" if token.startswith("lark:") else "open.feishu.cn"
            card = {"header": {"title": {"tag": "plain_text", "content": title[:100]}},
                    "elements": [{"tag": "markdown", "content": _cut_utf8(markdown, FEISHU_LIMIT)}]}
            payload: dict = {"msg_type": "interactive", "card": card}
            if sign:
                ts = int(time.time())
                payload |= {"timestamp": str(ts), "sign": _feishu_sign(sign, ts)}
            r = client.post(f"https://{domain}/open-apis/bot/v2/hook/{token.removeprefix('lark:')}", json=payload)
            body = r.json()
            ok = r.status_code == 200 and (body.get("code") == 0 or body.get("StatusCode") == 0)
        elif kind == "dingtalk_bot":
            token, _, sign = secret.partition("#")
            url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
            if sign:
                ts = int(time.time() * 1000)
                url += f"&timestamp={ts}&sign={_dingtalk_sign(sign, ts)}"
            text = _cut_utf8(f"### {title}\n\n{markdown}", DINGTALK_LIMIT)
            r = client.post(url, json={"msgtype": "markdown", "markdown": {"title": title[:100], "text": text}})
            ok = r.status_code == 200 and r.json().get("errcode") == 0
        elif kind == "telegram":
            token, _, chat = secret.partition("#")
            text = f"{title}\n\n{_plain(markdown)}"
            if len(text) > TELEGRAM_LIMIT:
                text = text[:TELEGRAM_LIMIT].rstrip() + "\n……（内容过长，其余请在网页查看）"
            r = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
            ok = r.status_code == 200 and r.json().get("ok") is True
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
            j = r.json()
            msg = j.get("message") or j.get("msg") or j.get("errmsg") or j.get("description") or ""
        except ValueError:
            msg = ""
        raise NotifyError(f"推送失败（HTTP {r.status_code}）{('：' + str(msg)[:200]) if msg else ''}")
