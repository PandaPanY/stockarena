"""A股数据源：基于 AKShare，以新浪为主、腾讯为备用。

实测（2026-10）：东方财富接口频繁断开连接，不作为数据源；新浪接口稳定。
所有方法都是同步阻塞调用，在异步代码中请用 asyncio.to_thread 调用。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from app.core.money import round_to_tick, to_decimal
from app.market.retry import RateLimiter, call_with_retry
from app.market.symbols import to_sina
from app.market.types import CorporateAction, DailyBar, Instrument

logger = logging.getLogger(__name__)

_sina_limiter = RateLimiter(0.3)
_tx_limiter = RateLimiter(0.5)
_misc_limiter = RateLimiter(0.3)

# 计算首日前收盘价需要更早的数据：多取一段，覆盖长假
_LOOKBACK_DAYS = 20
# 新浪 ETF 历史接口比实际晚一天，最近这么多天以内的缺口用腾讯日线补（覆盖长假后的第一个交易日）
_TX_PATCH_DAYS = 14


def _ak():
    # 延迟导入：akshare 导入较慢（约 2 秒），避免拖慢应用启动和与行情无关的测试
    import akshare

    return akshare


def _d(value) -> Decimal:
    return to_decimal(float(value))


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _to_date(value) -> date:
    return pd.Timestamp(value).date()


class AkshareCNProvider:
    market = "CN"
    delay_minutes = 0

    # ---------- 交易日历 / 基础信息 ----------

    def trading_days(self) -> list[date]:
        """新浪交易日历（覆盖到当年年底）。"""
        df = call_with_retry(lambda: _ak().tool_trade_date_hist_sina(), name="sina.trade_dates",
                             limiter=_sina_limiter)
        return sorted(_to_date(d) for d in df["trade_date"])

    def stock_names(self) -> dict[str, str]:
        """全部A股代码 → 简称。"""
        df = call_with_retry(lambda: _ak().stock_info_a_code_name(), name="stock_info_a_code_name",
                             limiter=_misc_limiter)
        return {str(r.code).zfill(6): str(r.name).replace(" ", "") for r in df.itertuples()}

    def index_constituents(self, index_code: str) -> list[tuple[str, str]]:
        """指数成分股（中证指数公司）。返回 [(6位代码, 简称)]。"""
        df = call_with_retry(lambda: _ak().index_stock_cons_csindex(symbol=index_code),
                             name=f"csindex.cons.{index_code}", limiter=_misc_limiter)
        return [(str(r[0]).zfill(6), str(r[1])) for r in df[["成分券代码", "成分券名称"]].itertuples(index=False)]

    def index_members(self, index_code: str) -> list[tuple[str, str, date, date | None]]:
        """指数成分的纳入日期（新浪「最新成份」）。返回 [(6 位代码, 名称, 纳入日期, 剔除日期)]。

        ⚠️ 新浪「历史成份」只更新到 2010 年，拿不到之后被剔除的股票；因此只能按当前成分的纳入日期
        做「半时点」股票池：纳入之前不可选。被剔除的股票仍然缺失（见 PRD 3.9 G1）。
        """
        df = call_with_retry(lambda: _ak().index_stock_cons(symbol=index_code), name=f"sina.index_cons.{index_code}",
                             limiter=_misc_limiter)
        out = []
        for r in df.itertuples(index=False):
            try:
                out.append((str(r[0]).zfill(6), str(r[1]), _to_date(r[2]), None))
            except (ValueError, TypeError):
                continue
        return out

    def industry_history(self, code: str) -> list[tuple[date, str, str]]:
        """申万行业分类（2021 版）的变更记录（巨潮资讯）。返回 [(生效日期, 一级行业, 二级行业)]，按日期升序。"""
        today = date.today().strftime("%Y%m%d")
        df = call_with_retry(lambda: _ak().stock_industry_change_cninfo(symbol=code, start_date="20000101",
                                                                          end_date=today),
                             name=f"cninfo.industry.{code}", limiter=_misc_limiter)
        out = []
        for r in df.to_dict("records"):
            if r.get("分类标准") != "申银万国行业分类标准" or not r.get("行业门类"):
                continue
            try:
                out.append((_to_date(r.get("变更日期")), str(r["行业门类"]).strip(),
                            str(r.get("行业次类") or "").strip()))
            except (ValueError, TypeError):
                continue
        return sorted(set(out))

    # ---------- 日线 ----------

    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        if inst.type == "index":
            return self._index_bars(inst, start, end)
        if inst.type in ("etf", "lof"):
            return self._fund_bars(inst, start, end)
        return self._stock_bars(inst, start, end)

    def _stock_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        sina_symbol = to_sina(inst.symbol)
        fetch_start = start - timedelta(days=_LOOKBACK_DAYS)
        try:
            raw = call_with_retry(
                lambda: _ak().stock_zh_a_daily(symbol=sina_symbol, start_date=_ymd(fetch_start),
                                               end_date=_ymd(end), adjust=""),
                name=f"sina.daily.{inst.symbol}", limiter=_sina_limiter,
            )
        except Exception:
            logger.warning("新浪日线失败，改用腾讯：%s", inst.symbol)
            raw = call_with_retry(
                lambda: _ak().stock_zh_a_hist_tx(symbol=sina_symbol, start_date=_ymd(fetch_start),
                                                 end_date=_ymd(end), adjust=""),
                name=f"tx.daily.{inst.symbol}", limiter=_tx_limiter,
            )
        factors = call_with_retry(
            lambda: _ak().stock_zh_a_daily(symbol=sina_symbol, adjust="hfq-factor"),
            name=f"sina.hfq_factor.{inst.symbol}", limiter=_sina_limiter,
        )
        factor_df = pd.DataFrame({
            "date": pd.to_datetime(factors["date"]),
            "factor": factors["hfq_factor"].astype(str).map(Decimal),
        })
        return self._build_bars(inst, raw, factor_df, start, end)

    def _fund_raw(self, inst: Instrument) -> pd.DataFrame:
        raw = call_with_retry(lambda: _ak().fund_etf_hist_sina(symbol=to_sina(inst.symbol)),
                              name=f"sina.etf_daily.{inst.symbol}", limiter=_sina_limiter)
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        return raw.sort_values("date").reset_index(drop=True)

    def _fund_recent_tx(self, inst: Instrument, raw: pd.DataFrame, end: date) -> pd.DataFrame:
        """新浪 ETF 历史接口当天收盘后不更新（实测要到下一个交易日下午才有前一天的数据），
        缺最近几个交易日时用腾讯日线补上：两者都是不复权价，数值一致（成交量取整到百股）。

        只补最近 _TX_PATCH_DAYS 天以内的缺口；补进来的行与新浪历史一起推算复权因子，口径不变。
        """
        last = raw["date"].max().date() if len(raw) else None
        if last is None or last >= end or (end - last).days > _TX_PATCH_DAYS:
            return raw
        try:
            tx = call_with_retry(
                lambda: _ak().stock_zh_a_hist_tx(symbol=to_sina(inst.symbol), start_date=_ymd(last + timedelta(days=1)),
                                                 end_date=_ymd(end), adjust=""),
                name=f"tx.etf_daily.{inst.symbol}", limiter=_tx_limiter)
        except Exception:  # noqa: BLE001 - 补不上时沿用新浪数据，下一轮再试
            logger.warning("腾讯 ETF 日线补充失败：%s", inst.symbol, exc_info=True)
            return raw
        if tx is None or tx.empty:
            return raw
        tx = tx.copy()
        tx["date"] = pd.to_datetime(tx["date"])
        tx = tx[(tx["date"] > pd.Timestamp(last)) & (tx["date"] <= pd.Timestamp(end))]
        if tx.empty:
            return raw
        cols = ["date", "open", "high", "low", "close", "volume", "amount"]
        return pd.concat([raw, tx[cols]], ignore_index=True).sort_values("date").reset_index(drop=True)

    def _fund_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        raw = self._fund_recent_tx(inst, self._fund_raw(inst), end)
        # 由分红与份额折算推算复权因子（需要完整历史，保证每次计算的因子基准一致）
        actions = fund_actions(inst, raw, self._fund_dividends(inst))
        factor_rows = [(pd.Timestamp("1900-01-01"), Decimal("1"))]
        factor = Decimal("1")
        closes = dict(zip(raw["date"], raw["close"], strict=True))
        dates = list(raw["date"])
        for act in actions:
            ex = pd.Timestamp(act.ex_date)
            prev_dates = [d for d in dates if d < ex]
            if not prev_dates:
                continue
            prev_close = _d(closes[prev_dates[-1]])
            ref = (prev_close - act.cash_per_share) / (1 + act.shares_per_share)
            if ref > 0:
                factor = factor * prev_close / ref
                factor_rows.append((ex, factor))
        factor_df = pd.DataFrame(factor_rows, columns=["date", "factor"])
        raw = raw[raw["date"] >= pd.Timestamp(start - timedelta(days=_LOOKBACK_DAYS))]
        return self._build_bars(inst, raw, factor_df, start, end)

    def _index_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        sina_symbol = f"{inst.exchange.lower()}{inst.code}"
        raw = call_with_retry(lambda: _ak().stock_zh_index_daily(symbol=sina_symbol),
                              name=f"sina.index_daily.{inst.symbol}", limiter=_sina_limiter)
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw[raw["date"] >= pd.Timestamp(start - timedelta(days=_LOOKBACK_DAYS))]
        if "amount" not in raw.columns:
            raw["amount"] = 0
        factor_df = pd.DataFrame({"date": [pd.Timestamp("1900-01-01")], "factor": [Decimal("1")]})
        return self._build_bars(inst, raw, factor_df, start, end)

    @staticmethod
    def _build_bars(inst: Instrument, raw: pd.DataFrame, factor_df: pd.DataFrame,
                    start: date, end: date) -> list[DailyBar]:
        if raw.empty:
            return []
        df = raw.copy()
        # pandas 3 的日期列精度可能不同（s / us），merge_asof 要求一致
        df["date"] = pd.to_datetime(df["date"]).astype("datetime64[us]")
        df = df.sort_values("date").reset_index(drop=True)
        factor_df = factor_df.copy()
        factor_df["date"] = pd.to_datetime(factor_df["date"]).astype("datetime64[us]")
        factor_df = factor_df.sort_values("date").reset_index(drop=True)
        df = pd.merge_asof(df, factor_df, on="date", direction="backward")
        df["factor"] = df["factor"].fillna(Decimal("1"))

        bars: list[DailyBar] = []
        prev_close: Decimal | None = None
        prev_factor: Decimal | None = None
        for row in df.itertuples(index=False):
            d = row.date.date()
            close = _d(row.close)
            factor = row.factor
            ref_prev: Decimal | None = None
            if prev_close is not None and prev_factor is not None:
                # 交易所前收盘价：除权除息日为除权参考价 = 昨收 × 昨日因子 / 今日因子
                ref_prev = round_to_tick(prev_close * prev_factor / factor, inst.tick, "nearest")
            if start <= d <= end:
                bars.append(DailyBar(
                    symbol=inst.symbol, date=d,
                    open=_d(row.open), high=_d(row.high), low=_d(row.low), close=close,
                    volume=int(row.volume or 0), amount=_d(getattr(row, "amount", 0) or 0),
                    prev_close=ref_prev, adj_factor=factor,
                ))
            prev_close, prev_factor = close, factor
        return bars

    # ---------- 除权除息 ----------

    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]:
        if inst.type in ("etf", "lof"):
            return fund_actions(inst, self._fund_raw(inst), self._fund_dividends(inst))
        if inst.type == "index":
            return []
        df = call_with_retry(
            lambda: _ak().stock_history_dividend_detail(symbol=inst.code, indicator="分红"),
            name=f"sina.dividend.{inst.symbol}", limiter=_sina_limiter,
        )
        actions = []
        for r in df.itertuples(index=False):
            if r.进度 != "实施" or pd.isna(r.除权除息日):
                continue
            # 新浪口径：每 10 股送股、转增、派息（元，税前）
            actions.append(CorporateAction(
                symbol=inst.symbol,
                ex_date=_to_date(r.除权除息日),
                cash_per_share=_d(r.派息 or 0) / 10,
                shares_per_share=(_d(r.送股 or 0) + _d(r.转增 or 0)) / 10,
                source="sina.dividend_detail",
            ))
        return sorted(actions, key=lambda a: a.ex_date)

    def _fund_dividends(self, inst: Instrument) -> list[CorporateAction]:
        """新浪 ETF 分红为累计分红（每份），相邻两次之差为单次分红。⚠️ 日期按除息日处理。"""
        df = call_with_retry(lambda: _ak().fund_etf_dividend_sina(symbol=to_sina(inst.symbol)),
                             name=f"sina.etf_dividend.{inst.symbol}", limiter=_sina_limiter)
        actions = []
        prev_total = Decimal("0")
        for r in df.sort_values("日期").itertuples(index=False):
            total = _d(r.累计分红)
            cash = total - prev_total
            prev_total = total
            if cash > 0:
                actions.append(CorporateAction(symbol=inst.symbol, ex_date=_to_date(r.日期),
                                               cash_per_share=cash, source="sina.etf_dividend"))
        return actions


# ---------------------------------------------------------------- 基金份额折算

# 新浪 ETF 历史行情不提供份额折算 / 拆分记录。单日价格变化超出该范围（ETF 涨跌幅限制最多 20%）视为份额折算，
# 例如 513100 在 2022-01-14 1 拆 5、512100 在 2022-09-05 约 2.76 份合并为 1 份。
_SPLIT_UP, _SPLIT_DOWN = Decimal("1.45"), Decimal("0.69")
# 推算的比例与整数比例相差不超过该比例时取整（当天真实涨跌会让推算值略有偏差）
_SNAP_TOLERANCE = Decimal("0.03")


def _split_shares(r: Decimal) -> Decimal:
    """价格比 r（折算当天收盘 / 折算前参考价）→ 每份新增份数（拆分为正，合并为负）。⚠️ 由价格推算。"""
    m = 1 / r                                   # 每 1 份折算成的份数
    if m >= 1:
        k = Decimal(round(m))
        if abs(m / k - 1) <= _SNAP_TOLERANCE:
            m = k
    else:
        k = Decimal(round(r))
        if k >= 2 and abs(r / k - 1) <= _SNAP_TOLERANCE:
            m = 1 / k
    return (m - 1).quantize(Decimal("0.000001"))


def fund_actions(inst: Instrument, raw: pd.DataFrame, dividends: list[CorporateAction]) -> list[CorporateAction]:
    """基金的除权事件：分红（新浪分红记录）+ 由价格跳变推算的份额折算；同一天的合并为一条。"""
    by_date = {a.ex_date: a for a in dividends}
    prev: Decimal | None = None
    for row in raw.sort_values("date").itertuples(index=False):
        d = _to_date(row.date)
        close = _d(row.close)
        if prev is not None and prev > 0 and close > 0:
            div = by_date.get(d)
            base = prev - (div.cash_per_share if div else Decimal("0"))
            r = close / base if base > 0 else Decimal("1")
            if r > _SPLIT_UP or r < _SPLIT_DOWN:
                shares = _split_shares(r)
                logger.info("%s %s 价格跳变 %.4f 倍，按份额折算处理：每份 %+s 份", inst.symbol, d, r, shares)
                by_date[d] = CorporateAction(symbol=inst.symbol, ex_date=d,
                                             cash_per_share=div.cash_per_share if div else Decimal("0"),
                                             shares_per_share=shares,
                                             source=(div.source + "+" if div else "") + "price_jump")
        prev = close
    return sorted(by_date.values(), key=lambda a: a.ex_date)
