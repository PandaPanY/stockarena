"""主数据源失败时自动改用备用数据源（A股：新浪 / 腾讯为主，Tushare 为备用，或反过来，见配置 cn_data_source）。

按方法逐次切换：主数据源的某次调用失败（重试之后）就用备用数据源再调一次；备用数据源没有这个方法或也失败时，
抛出主数据源的错误（附上备用数据源的错误）。只在一方有的方法（如行业分类、指数成分）直接用那一方。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class FallbackProvider:
    def __init__(self, primary, backup, name: str = ""):
        self.primary, self.backup = primary, backup
        self.market = primary.market
        self.delay_minutes = getattr(primary, "delay_minutes", 0)
        self.name = name or f"{type(primary).__name__}→{type(backup).__name__}"

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        first = getattr(self.primary, attr, None)
        second = getattr(self.backup, attr, None)
        if not callable(first) and not callable(second):
            raise AttributeError(attr)
        if not callable(first):
            return second
        if not callable(second):
            return first

        def call(*args, **kwargs):
            try:
                return first(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — 数据源异常类型繁杂，统一改用备用数据源
                logger.warning("%s.%s 失败，改用备用数据源 %s：%s", type(self.primary).__name__, attr,
                               type(self.backup).__name__, e)
                try:
                    return second(*args, **kwargs)
                except Exception as e2:  # noqa: BLE001
                    raise RuntimeError(f"{e}（备用数据源 {type(self.backup).__name__} 也失败：{e2}）") from e

        return call
