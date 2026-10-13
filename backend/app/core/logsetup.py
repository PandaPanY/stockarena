"""日志配置：控制台 + 按大小滚动的文件日志，并对敏感信息自动打码。"""

from __future__ import annotations

import logging
import logging.config
import re
import sys
from pathlib import Path

# 常见密钥形态：sk-xxx / Bearer xxx / api_key=xxx / "api_key": "xxx"
_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(
        r"""((?:api[_-]?key|x-api-key|authorization)["']?\s*[:=]\s*["']?)[^\s"',}]{6,}""",
        re.IGNORECASE,
    ),
]


def mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}****", text)
    return text


class SecretMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


def _ensure_utf8_stderr() -> None:
    """Windows 下输出被重定向（IDE、管道、进程管理器）时 stderr 默认用 GBK 编码，中文日志会乱码。
    真实控制台由 Python 直接写入 Unicode，不受影响，因此只处理非终端的情况。"""
    stream = sys.stderr
    if stream is None or stream.isatty() or not hasattr(stream, "reconfigure"):
        return
    if (stream.encoding or "").lower().replace("-", "") != "utf8":
        stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(level: str, log_dir: Path) -> None:
    _ensure_utf8_stderr()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"mask": {"()": SecretMaskingFilter}},
            "formatters": {
                "default": {"format": "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["mask"],
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "default",
                    "filters": ["mask"],
                    "filename": str(log_dir / "app.log"),
                    "maxBytes": 10 * 1024 * 1024,
                    "backupCount": 5,
                    "encoding": "utf-8",
                },
            },
            "root": {"level": level.upper(), "handlers": ["console", "file"]},
        }
    )
