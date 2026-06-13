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

嵌套 group 支持 (v1.0.0 schema 改为分组):
  - config = {"基础": {"enabled": true, "priority": 100}, ...}
  - 读法: cfg_group_int(config, "基础", "enabled", True)
  - 兼容旧扁平: config = {"enabled": true, ...} 也能用, group 参数留空或传 None
"""

from __future__ import annotations

from typing import Any, Optional


def _lookup(config: Any, group: Optional[str], key: str):
    """: 嵌套 / 扁平兼容读 — 优先嵌套 dict, fallback 扁平。"""
    if group:
        sec = config.get(group) if hasattr(config, "get") else None
        if isinstance(sec, dict):
            return sec.get(key)
    # 兼容旧扁平 (v0.8.x 时代 schema 是扁平)
    return config.get(key) if hasattr(config, "get") else None


def cfg_int(config: Any, key: str, default: int) -> int:
    """读 int 配置项 — ``None`` / 空串 / 转换失败返 default。

    兼容新旧 schema: 优先按扁平 key 读, 找不到返 default。
    如要按 group 读, 用 :func:`cfg_group_int`。
    """
    v = config.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def cfg_str(config: Any, key: str, default: str) -> str:
    """读 str 配置项 — ``None`` 返 default, 其它强制转 str。

    兼容新旧 schema。group 读法见 :func:`cfg_group_str`。
    """
    v = config.get(key, default)
    if v is None:
        return default
    return str(v)


# ---------------------------------------------------------------------------
# 嵌套 group 读 (v1.0.0+ schema 用)
# ---------------------------------------------------------------------------

def cfg_group_int(config: Any, group: str, key: str, default: int) -> int:
    """: 读嵌套 group.key (int) — fallback 旧扁平 key。

    用法: ``cfg_group_int(config, "缓存", "memory_cache_max_size", 500)``
    """
    v = _lookup(config, group, key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def cfg_group_str(config: Any, group: str, key: str, default: str) -> str:
    """: 读嵌套 group.key (str)。"""
    v = _lookup(config, group, key)
    if v is None:
        return default
    return str(v)


def cfg_group_bool(config: Any, group: str, key: str, default: bool) -> bool:
    """: 读嵌套 group.key (bool) — 任何 falsy/truthy 都识别。"""
    v = _lookup(config, group, key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes", "on")
    return bool(v)
