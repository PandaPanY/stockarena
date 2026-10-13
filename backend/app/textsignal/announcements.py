"""上市公司公告：下载（巨潮资讯）、匿名化、按标题粗分「例行 / 可能有信息」。

匿名化的目的：模型训练时读过大量历史新闻，看到「招商银行 2019 年业绩预增」可能「记得」之后的股价，
历史检验就会失真。去掉公司名、代码、年份、人名和其他公司名后，模型只能就事论事地判断。
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

CNINFO_HOSTS = ["cninfo.com.cn", "www.cninfo.com.cn"]


@dataclass
class Announcement:
    symbol: str          # 600036.SH
    name: str            # 证券简称（下载时）
    date: str            # 公告日期 YYYY-MM-DD
    title: str
    url: str


def fetch(symbol: str, start: date, end: date) -> list[Announcement]:
    """一只股票在 [start, end] 的全部公告标题（巨潮资讯，一次请求可取多年）。"""
    import akshare as ak

    code = symbol.split(".")[0]
    df = ak.stock_zh_a_disclosure_report_cninfo(symbol=code, market="沪深京", category="",
                                                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    out = []
    for r in df.itertuples(index=False):
        title = str(r[2] or "").strip()
        if title:
            out.append(Announcement(symbol, str(r[1] or ""), str(r[3])[:10], title, str(r[4] or "")))
    return out


def download(symbols: list[str], start: date, end: date, path: Path, progress=print, pause: float = 0.3) -> dict:
    """逐只下载并追加写入 jsonl.gz（已下载的标的跳过，可断点续传）。"""
    done = {a["symbol"] for a in read(path)} if path.exists() else set()
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = rows = 0
    for k, sym in enumerate(symbols, 1):
        if sym in done:
            continue
        try:
            items = fetch(sym, start, end)
        except Exception as e:  # noqa: BLE001 - 单只失败不影响其他
            failed += 1
            progress(f"{k}/{len(symbols)} {sym} 失败：{e}")
            continue
        with gzip.open(path, "at", encoding="utf-8") as f:
            for a in items:
                f.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
        ok += 1
        rows += len(items)
        if k % 20 == 0:
            progress(f"{k}/{len(symbols)}：本次已下载 {ok} 只、{rows} 条")
        time.sleep(pause)
    return {"ok": ok, "failed": failed, "rows": rows, "skipped": len(done)}


def read(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


# ---------------------------------------------------------------- 例行 / 有信息


# 例行公告：议事规则、制度、会议通知与决议、中介机构意见、英文或 H 股版本等，标题本身几乎没有可交易的信息
_ROUTINE = re.compile(
    r"章程|制度|规则|细则|办法|规程|指引|议事|工作报告|述职|履职|声明与承诺|提名人|候选人|法律意见|核查意见|"
    r"保荐|督导|鉴证|审计报告|内部控制|内控|社会责任|ESG|可持续发展|独立董事.*意见|监事会决议|董事会决议|"
    r"股东大会|股东会|会议资料|会议通知|召开|决议公告|H股公告|英文|摘要|更正后|更新前|更新后|补充公告|"
    r"信息披露|投资者关系|说明会|路演|权益分派实施|募集资金.*存放|专项报告|财务决算|财务预算|评估报告")
# 可能有信息的事件（标题中出现即保留）
_MATERIAL = re.compile(
    r"预增|预减|预亏|扭亏|预盈|首亏|续亏|业绩预告|业绩快报|减持|增持|回购|中标|重大合同|签订|诉讼|仲裁|立案|"
    r"处罚|警示|问询|关注函|监管函|重组|收购|出售|并购|吸收合并|定增|非公开发行|向特定对象|配股|可转债|"
    r"股权激励|员工持股|质押|冻结|风险提示|退市|停牌|复牌|担保|计提|减值|会计差错|追溯|辞职|离任|"
    r"批准|核准|注册|获批|临床|药品|专利|经营数据|产销|销量|特别分红|高送转|分拆|破产|事故|召回")


# 这些关键词即使出现在「例行」格式的标题里也保留（如「关于回购股份的董事会决议公告」）
_STRONG = re.compile(r"预增|预减|预亏|扭亏|立案|处罚|退市|重组|回购|减持|增持")


def is_material(title: str) -> bool:
    """标题是否可能包含可交易的信息（粗筛，宁多勿漏）。"""
    if _STRONG.search(title):
        return True
    return bool(_MATERIAL.search(title)) and not _ROUTINE.search(title)


# ---------------------------------------------------------------- 匿名化


_COMPANY = re.compile(r"[一-龥A-Za-z（）()]{2,30}?(?:股份|集团|控股)?有限(?:责任)?公司")
_PREFIX = re.compile(r"^[^：:]{2,16}[：:]")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")      # 中文与数字之间没有 \b 边界
_PERSON = re.compile(r"[（(][一-龥·]{2,4}(?:[、，,][一-龥·]{2,4})*[）)]")
_ORDINAL_MEETING = re.compile(r"第[一二三四五六七八九十百零\d]+届")


# 公司名前面常见的连接词：只把它们之后的部分换成「某公司」，保留「关于控股股东」「全资子公司」等事件主体信息
_BEFORE_COMPANY = re.compile(r"关于|控股股东|股东|全资子公司|控股子公司|子公司|参股公司|与|向|对|和|及|为|收到|"
                             r"收购|出售|转让|受让|增资|投资|设立|参股|吸收合并|合并")
_COMPANY_SUFFIX = re.compile(r"(?:股份|集团|控股)?有限(?:责任)?公司$")


def _mask_company(m: re.Match) -> str:
    whole = m.group(0)
    name_part = _COMPANY_SUFFIX.sub("", whole)
    ends = [x.end() for x in _BEFORE_COMPANY.finditer(name_part)]
    head = whole[:ends[-1]] if ends else ""
    # 公告主体自己的全称：简称已先替换为「本公司」（如「本公司股份有限公司」），或出现在标题开头
    # （如简称「中国国航」、标题以「中国国际航空股份有限公司关于……」开头）；含简称的母公司等仍是「某公司」
    own = name_part[len(head):] == "本公司" or (m.start() == 0 and not head)
    return head + ("本公司" if own else "某公司")


# 括号里的 2～4 个汉字多为人名（如「独立董事提名人声明（张三）」），但要排除文件版本、机构类型等常见词
_NOT_PERSON = re.compile(r"稿|订|案|版|期|报|合伙|公司|更新|摘要|全文|修正|注册|英文|附件|草|代码|简称|补充|"
                         r"更正|说明|一|二|三|四|五")


def _mask_person(m: re.Match) -> str:
    return m.group(0) if _NOT_PERSON.search(m.group(0)) else "（某人）"


def anonymize(title: str, name: str = "", symbol: str = "") -> str:
    """去掉公司名（简称、全称、标题前缀）、代码、年份、括号里的人名、届次，其他公司名统一为「某公司」。"""
    t = title
    t = _PREFIX.sub("", t)
    # 长的先替换（「*ST康美」先于「康美」）；集合的遍历顺序每次运行可能不同，必须排序才能保证结果可复现
    aliases = {name, name.replace(" ", ""), name.replace("*", "").replace("ST", "")} - {""}
    for alias in sorted(aliases, key=len, reverse=True):
        if len(alias) >= 2:
            t = t.replace(alias, "本公司")
    t = _COMPANY.sub(_mask_company, t)
    if symbol:
        t = t.replace(symbol.split(".")[0], "******")
    t = _CODE.sub("******", t)
    t = _YEAR.sub(lambda m: "某" if t[m.end():m.end() + 1] == "年" else "某年", t)
    t = _PERSON.sub(_mask_person, t)
    t = _ORDINAL_MEETING.sub("第某届", t)
    return re.sub(r"\s+", " ", t).strip()
