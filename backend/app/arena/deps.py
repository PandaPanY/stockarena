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
def cn_provider() -> AkshareCNProvider:
    return AkshareCNProvider()


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
