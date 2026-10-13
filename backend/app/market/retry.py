"""数据源调用的重试与限流。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class DataSourceError(RuntimeError):
    """数据源在重试后仍然失败。"""


class RateLimiter:
    """简单的最小间隔限流：同一数据源两次请求之间至少间隔 min_interval 秒（线程安全）。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    name: str,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> T:
    """调用数据源，失败时指数退避重试（1s、2s、4s…）。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if limiter:
            limiter.wait()
        try:
            return fn()
        except Exception as e:  # 数据源异常类型繁杂（网络、解析、空数据），统一重试
            last_error = e
            logger.warning("数据源 %s 第 %d/%d 次失败：%s", name, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(base_delay * 2 ** (attempt - 1))
    raise DataSourceError(f"{name} 调用失败（已重试 {attempts} 次）：{last_error}") from last_error
