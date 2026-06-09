"""
vision_text_bridge.config_helpers
====================================

配置项类型安全的 getter — 把 ``config.get(key, default)`` 包一层,
对 ``None`` / 空串 / 类型转换失败做兜底。

为什么单独抽: ``VisionTextBridgePlugin`` 类里到处调 ``_cfg_int`` /
``_cfg_str`` (40+ 处), 嵌在 plugin 里没必要 — 抽成模块级 helper,
方便 :mod:`web_api` / :mod:`main` 共享。

AstrBotConfig 实例实际是个 dict-like (有 ``.get(key, default)``),
某些版本可能是对象 — 两种都支持。
"""

from __future__ import annotations

from typing import Any


def cfg_int(config: Any, key: str, default: int) -> int:
    """读 int 配置项 — ``None`` / 空串 / 转换失败返 default。"""
    v = config.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def cfg_str(config: Any, key: str, default: str) -> str:
    """读 str 配置项 — ``None`` 返 default, 其它强制转 str。"""
    v = config.get(key, default)
    if v is None:
        return default
    return str(v)
