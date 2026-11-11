"""竞技模块的公共依赖：各市场数据源、行情服务、股票池解析、密钥。"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import SecretBox
from app.market.fundamentals import AkshareFundamentalsProvider, FundamentalsService
from app.market.markets import MARKETS
from app.market.news import EastmoneyNewsProvider, NewsService
from app.market.providers.akshare_cn import AkshareCNProvider
from app.market.providers.sina_intl import SinaHKProvider, SinaUSProvider
from app.market.service import MarketDataService
from app.market.universe import UniverseResolver, load_universe_specs

# 兼容旧代码：市场名称与货币
MARKET_INFO = {code: {"name": m.name, "currency": m.currency, "tz": m.tz} for code, m in MARKETS.items()}


@lru_cache
def cn_provider():
    """A股数据源（配置 cn_data_source、tushare_token、fuyao_key_file）：默认新浪 / 腾讯；填了 Tushare token 时
    失败改用 Tushare（cn_data_source=tushare 时反过来）；填了同花顺 Key 文件时，股票日线最后再试同花顺。"""
    import logging

    from app.market.providers.fallback import FallbackProvider
    from app.market.providers.fuyao_cn import FuyaoCNProvider, read_key
    from app.market.providers.tushare_cn import TushareCNProvider

    settings = get_settings()
    provider = AkshareCNProvider()
    if settings.cn_data_source != "akshare":
        if settings.tushare_token:
            tushare = TushareCNProvider(settings.tushare_token)
            provider = (FallbackProvider(tushare, provider) if settings.cn_data_source == "tushare"
                        else FallbackProvider(provider, tushare))
        elif settings.cn_data_source == "tushare":
            logging.getLogger(__name__).warning("cn_data_source=tushare 但没有设置 tushare_token，改用新浪 / 腾讯")
    key = read_key(settings.fuyao_key_file)
    if settings.fuyao_key_file and not key:
        logging.getLogger(__name__).warning("同花顺 Key 文件不存在或为空：%s", settings.fuyao_key_file)
    if key and settings.cn_data_source != "akshare":
        provider = FallbackProvider(provider, FuyaoCNProvider(key))
    return provider


def fuyao_provider():
    """同花顺数据源（用于交叉核对）；没有配置 Key 时为 None。"""
    from app.market.providers.fuyao_cn import FuyaoCNProvider, read_key

    key = read_key(get_settings().fuyao_key_file)
    return FuyaoCNProvider(key) if key else None


@lru_cache
def hk_provider() -> SinaHKProvider:
    return SinaHKProvider()


@lru_cache
def us_provider() -> SinaUSProvider:
    return SinaUSProvider()


def providers() -> dict[str, object]:
    # 每次调用时查找模块级函数，测试可以通过 monkeypatch 替换单个市场的数据源
    return {"CN": cn_provider(), "HK": hk_provider(), "US": us_provider()}


def market_service(session: Session, market: str = "CN") -> MarketDataService:
    provider = providers().get(market)
    if provider is None:
        raise ValueError(f"暂不支持市场：{market}")
    return MarketDataService(session, provider)


def universe_resolver() -> UniverseResolver:
    settings = get_settings()
    return UniverseResolver(load_universe_specs(settings.config_dir), providers(), settings.cache_dir)


@lru_cache
def secret_box() -> SecretBox:
    return SecretBox(get_settings().secret_key_file)


@lru_cache
def news_provider() -> EastmoneyNewsProvider:
    return EastmoneyNewsProvider()


@lru_cache
def fundamentals_provider() -> AkshareFundamentalsProvider:
    return AkshareFundamentalsProvider()


def news_service(session: Session) -> NewsService:
    return NewsService(session, news_provider())


def fundamentals_service(session: Session) -> FundamentalsService:
    return FundamentalsService(session, fundamentals_provider())
