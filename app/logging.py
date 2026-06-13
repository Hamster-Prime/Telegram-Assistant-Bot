"""中文结构化日志 —— structlog 配置。

设计目标(用户要求):
- 全中文显示,方便排错
- 每项分明:时间 | 级别 | 事件 | 模块,固定列对齐
- 数据分明:key=value 逐项列出,中文字段名
- 日志全面:每个关键路径(请求、API 调用、key 切换、重试、降级、落库)都有日志

用法:
    from app.logging import get_logger, setup_logging
    setup_logging("INFO")
    log = get_logger("minimax.client")
    log.info("对话请求开始", 用户=123, 模型="MiniMax-M3", 消息数=5)
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

_LEVEL_ZH = {
    "debug": "调试",
    "info": "信息",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
}

_LEVEL_COLOR = {
    "调试": "\x1b[36m",   # 青
    "信息": "\x1b[32m",   # 绿
    "警告": "\x1b[33m",   # 黄
    "错误": "\x1b[31m",   # 红
    "严重": "\x1b[35m",   # 紫
}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"

_TZ = ZoneInfo("Asia/Shanghai")


def _add_timestamp(_, __, event_dict: dict) -> dict:
    event_dict["时间"] = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return event_dict


def _zh_level(_, method_name: str, event_dict: dict) -> dict:
    event_dict["级别"] = _LEVEL_ZH.get(method_name, method_name)
    return event_dict


class _ChineseConsoleRenderer:
    """渲染为:时间 | 级别 | [模块] 事件 | 键=值 | 键=值 ...

    每个数据项以「 | 」分隔,字段名与值用「=」连接,清晰分明。
    """

    def __init__(self, colors: bool = True) -> None:
        self._colors = colors and sys.stderr.isatty()

    def __call__(self, _, __, event_dict: dict) -> str:
        ts = event_dict.pop("时间", "")
        level = event_dict.pop("级别", "信息")
        logger_name = event_dict.pop("logger", "") or event_dict.pop("模块", "")
        event = event_dict.pop("event", "")
        # 异常栈(structlog format_exc_info 处理后为 exception 字段)
        exc = event_dict.pop("exception", None)

        parts: list[str] = []
        if self._colors:
            color = _LEVEL_COLOR.get(level, "")
            parts.append(f"{_DIM}{ts}{_RESET}")
            parts.append(f"{color}{level}{_RESET}")
            if logger_name:
                parts.append(f"{_DIM}[{logger_name}]{_RESET}")
            parts.append(f"{color}{event}{_RESET}")
        else:
            parts.append(ts)
            parts.append(level)
            if logger_name:
                parts.append(f"[{logger_name}]")
            parts.append(str(event))

        for key, val in event_dict.items():
            parts.append(f"{key}={val!r}" if isinstance(val, str) else f"{key}={val}")

        line = " | ".join(parts)
        if exc:
            line += "\n" + exc
        return line


def setup_logging(level: str = "INFO") -> None:
    """初始化全局日志。控制台输出中文结构化日志。"""
    # Windows 控制台默认 GBK,强制 UTF-8 防中文乱码
    for stream in (sys.stderr, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # 降低第三方库噪音
    for noisy in ("httpx", "httpcore", "aiogram.event", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            _zh_level,
            _add_timestamp,
            structlog.processors.format_exc_info,
            _ChineseConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
