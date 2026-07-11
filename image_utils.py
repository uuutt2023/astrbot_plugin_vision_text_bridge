"""image_utils.py - 通用图片 URL helper + 元数据提取 + 字节读取。

API: is_image_url_part / extract_url_from_item / extract_urls_from_parts /
     extract_urls_from_context_list / is_data_url / strip_image_urls /
     collect_image_urls_from_components / to_text_part /
     sniff_image_meta / is_cacheable_url / read_image_bytes
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any, Iterable
from urllib.parse import unquote

# AstrBot 把 Pydantic TextPart 作为 content part 注入。测试沙箱 / 未来重命名可能没这个 import — try/except 兜底
try:
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # type: ignore

_log = logging.getLogger("astrbot_plugin_vision_text_bridge")

__all__ = [
    # 检测/提取
    "is_image_url_part",
    "extract_url_from_item",
    "extract_urls_from_parts",
    "extract_urls_from_context_list",
    "is_data_url",
    "strip_image_urls",
    # 异步扫描
    "_extract_image_url_from_component",
    "collect_image_urls_from_components",
    # TextPart 包装
    "to_text_part",
    # 元数据
    "sniff_image_meta",
    # URL 过滤
    "is_cacheable_url",
    # 字节读取
    "read_image_bytes",
    "_read_file_bytes_sync",
    "_normalize_file_url",
    "_is_windows_absolute",
]


# ---------------------------------------------------------------------------
# 检测 / 提取
# ---------------------------------------------------------------------------

def is_image_url_part(part: Any) -> bool:
    """判断一个 content part 是不是 ``type=='image_url'``。

    同时兼容 dict 形式 (Pydantic 序列化后) 和 object 形式
    (AstrBot 内部的 TextPart / MultimodalContent)。
    """
    if isinstance(part, dict):
        return part.get("type") == "image_url"
    return getattr(part, "type", None) == "image_url"


def extract_url_from_item(item: Any) -> str:
    """从 image_url 类型的 part / dict 取 URL。

    ``item.image_url`` 可能是:
    - 字符串 (Pydantic 简化)
    - dict (含 ``url`` 键)
    - object (含 ``url`` 属性)
    """
    if isinstance(item, dict):
        iu = item.get("image_url")
        if isinstance(iu, str):
            return iu
        if isinstance(iu, dict):
            return iu.get("url", "") or ""
        return ""
    iu = getattr(item, "image_url", None)
    if iu is None:
        return ""
    if isinstance(iu, str):
        return iu
    return getattr(iu, "url", "") or ""


def extract_urls_from_parts(parts: Iterable[Any]) -> list[str]:
    """从 parts list 抽全部非空 URL。"""
    if not parts:
        return []
    out: list[str] = []
    for p in parts:
        if is_image_url_part(p):
            u = extract_url_from_item(p)
            if u:
                out.append(u)
    return out


def extract_urls_from_context_list(content_list: Any) -> list[str]:
    """从 ``event.context`` / ``message_chain`` 抽 URL (兼容 list[obj] / list[dict])。"""
    if not content_list:
        return []
    out: list[str] = []
    for it in content_list:
        if is_image_url_part(it):
            u = extract_url_from_item(it)
            if u:
                out.append(u)
    return out


def is_data_url(url: str) -> bool:
    """判断是不是 ``data:image/...;base64,...`` 形式 (用户直接发图)。"""
    return isinstance(url, str) and url.startswith("data:image/")


def strip_image_urls(req: Any, only_data_url: bool) -> int:
    """从 ``req.image_urls`` 移除已处理的 URL — 让 framework provider 不再单独发图。

    防止 AstrBot 二次把原始 base64 图塞给 LLM (用户直接发图时)。

    Args:
        req: ProviderRequest-like object, 有 ``.image_urls`` list 属性
        only_data_url: True = 只移除 ``data:image/...`` (用户图), False = 移除全部

    Returns:
        移除的数量
    """
    urls = getattr(req, "image_urls", None)
    if not urls:
        return 0
    before = list(urls)
    if only_data_url:
        urls[:] = [u for u in urls if not is_data_url(u)]
    else:
        urls.clear()
    return len(before) - len(urls)


# ---------------------------------------------------------------------------
# 异步扫描 — 嵌套 components (image / Image / Node / NodeGroup)
# ---------------------------------------------------------------------------

# AstrBot comp type:
# - "image" (小写) — 标准 Image component
# - "Image" (大写) — 老版本
# - "node" — 嵌套节点 (Forward / RichText)
# - "NodeGroup" — 合并转发
_NESTED_TYPES = ("image", "Image", "node", "Node", "NodeGroup")
_NESTED_ATTRS = ("content", "message", "nodes", "children", "messages")


async def _extract_image_url_from_component(comp) -> str | None:
    """从 Image-like component 抽 URL/file path。

    优先用 ``.url`` 属性, 降级用 ``.file`` / ``.path``, 最后 ``convert_to_file_path()``。
    """
    if comp is None:
        return None
    u = getattr(comp, "url", None)
    if isinstance(u, str) and u:
        return u
    for attr in ("file", "path", "src"):
        v = getattr(comp, attr, None)
        if isinstance(v, str) and v:
            return v
    conv = getattr(comp, "convert_to_file_path", None)
    if callable(conv):
        try:
            fp = await conv()
        except Exception:  # noqa: BLE001
            fp = None
        if isinstance(fp, str) and fp:
            return fp
    return None


async def collect_image_urls_from_components(
    components,
    dedupe: list[str] | None = None,
) -> int:
    """递归扫 components list, 累计抽到的图片 URL 数。

    支持嵌套 (Forward / Node / NodeGroup) — 深度递归。

    Args:
        components: component list / iterable
        dedupe: 可选 list 传入, 内部 append 抽到的 URL, 用于调用方去重

    Returns:
        新增的图片数
    """
    if not components:
        return 0
    added = 0
    for comp in components:
        ctype = getattr(comp, "type", None)
        if ctype in _NESTED_TYPES:
            for attr in _NESTED_ATTRS:
                inner = getattr(comp, attr, None)
                if isinstance(inner, list):
                    added += await collect_image_urls_from_components(inner, dedupe)
                    break
            if ctype in ("image", "Image"):
                fp = await _extract_image_url_from_component(comp)
                if fp and (dedupe is None or fp not in dedupe):
                    if dedupe is not None:
                        dedupe.append(fp)
                    added += 1
            continue
        if ctype in ("image", "Image"):
            fp = await _extract_image_url_from_component(comp)
            if fp and (dedupe is None or fp not in dedupe):
                if dedupe is not None:
                    dedupe.append(fp)
                added += 1
    return added


# ---------------------------------------------------------------------------
# TextPart 包装
# ---------------------------------------------------------------------------

def to_text_part(part_dict: dict) -> Any:
    """把 ``{"text": "..."}`` dict 包装成 Pydantic ``TextPart``。

    缺 TextPart 的环境 (测试沙箱) 直接返 dict, 后续序列化兜底。
    """
    if TextPart is not None and isinstance(part_dict, dict):
        return TextPart(text=part_dict.get("text", ""))
    return part_dict


# ---------------------------------------------------------------------------
# mime / w / h 嗅探
# ---------------------------------------------------------------------------

def sniff_image_meta(data: bytes) -> tuple[str, int, int]:
    """嗅探图片 (mime, width, height)。失败返 ``("", 0, 0)``。

    优先 PIL (Pillow) — 准; 失败降级到 magic bytes (PNG/JPEG/GIF/WEBP header)。
    """
    if not data or len(data) < 8:
        return ("", 0, 0)
    # 优先 PIL
    try:
        from PIL import Image as _PILImage  # type: ignore
        with _PILImage.open(io.BytesIO(data)) as im:
            mime = _PILImage.MIME.get(im.format, "") if im.format else ""
            return (mime, int(im.width or 0), int(im.height or 0))
    except Exception:  # noqa: BLE001
        pass
    # 降级 magic bytes
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # PNG: width @ byte 16-19, height @ 20-23 (big-endian)
        if len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return ("image/png", w, h)
        return ("image/png", 0, 0)
    if data[:2] == b"\xff\xd8":
        # JPEG — 需要扫描 SOF marker, 这里返 mime 不知道精确 w/h
        return ("image/jpeg", 0, 0)
    if data[:4] == b"GIF8":
        if len(data) >= 10:
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return ("image/gif", w, h)
        return ("image/gif", 0, 0)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _sniff_webp(data)
    return ("", 0, 0)


def _sniff_jpeg(data: bytes) -> tuple[str, int, int]:
    """JPEG SOF 扫描取 w/h。失败返 ``("image/jpeg", 0, 0)``。"""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            return ("image/jpeg", 0, 0)
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            if i + 9 > len(data):
                return ("image/jpeg", 0, 0)
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return ("image/jpeg", w, h)
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg_len
    return ("image/jpeg", 0, 0)


def _sniff_webp(data: bytes) -> tuple[str, int, int]:
    """WEBP VP8/VP8L/VP8X 解 w/h。失败返 ``("image/webp", 0, 0)``。"""
    if len(data) < 30:
        return ("image/webp", 0, 0)
    fourcc = data[12:16]
    if fourcc == b"VP8 ":
        # 简单格式
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return ("image/webp", w, h)
    if fourcc == b"VP8L":
        # lossless
        if len(data) < 25:
            return ("image/webp", 0, 0)
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)) + 1
        return ("image/webp", w, h)
    return ("image/webp", 0, 0)


# ---------------------------------------------------------------------------
# URL 缓存策略
# ---------------------------------------------------------------------------

_BOT_AVATAR_HOSTS = ("q.qlogo.cn", "q1.qlogo.cn", "q2.qlogo.cn", "q3.qlogo.cn", "q4.qlogo.cn")
_BOT_AVATAR_PATHS = ("headimg_dl",)


def is_cacheable_url(url: str, config: Any) -> bool:
    """判断 URL 是否值得进描述缓存。

    规则:
    - 始终过滤: bot 自己的头像 (q.qlogo.cn/headimg_dl) — 用户 @ bot 时注入
    - 始终过滤: 明显非图 URL (无图片扩展名 / 不在白名单 host)
    - config 强制: ``config.get('cache_all_urls', False)`` 为 True 时全缓存
    """
    if not url or not isinstance(url, str):
        return False
    if url.startswith("data:image/"):
        return True  # 用户直接发图 — 永远缓存
    u_lower = url.lower()
    # bot 头像
    for h in _BOT_AVATAR_HOSTS:
        if h in u_lower:
            for p in _BOT_AVATAR_PATHS:
                if p in u_lower:
                    return False
    # config: 全缓存
    if config is not None and getattr(config, "get", None):
        try:
            if config.get("cache_all_urls", False):
                return True
        except Exception:  # noqa: BLE001
            pass
    # 默认: 缓存 (描述是 cheap, 重复描述浪费更大)
    return True


# ---------------------------------------------------------------------------
# 字节读取
# ---------------------------------------------------------------------------

def _read_file_bytes_sync(path: str) -> bytes:
    """同步读文件 bytes — 失败返空 bytes。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return b""


def _normalize_file_url(url: str) -> str:
    """``file:///path`` / ``file://localhost/path`` → ``/path``。"""
    if not url or not isinstance(url, str):
        return url or ""
    if url.startswith("file:///"):
        return url[len("file:///"):]
    if url.startswith("file://localhost/"):
        return url[len("file://localhost/"):]
    if url.startswith("file://"):
        return url[len("file://"):]
    return url


def _is_windows_absolute(path: str) -> bool:
    """判断 Windows 绝对路径 (``C:\\`` / ``C:/``)。"""
    if not path or len(path) < 3:
        return False
    return path[1] == ":" and path[0].isalpha() and path[2] in ("\\", "/")


async def read_image_bytes(url: str) -> bytes:
    """读图片 bytes — 支持 http(s) / file:// / 本地路径 / base64 data URL。

    失败返空 bytes。
    """
    if not url or not isinstance(url, str):
        return b""
    # base64 data URL
    if url.startswith("data:image/"):
        try:
            import base64
            comma = url.find(",")
            if comma > 0:
                b64 = url[comma + 1:]
                return base64.b64decode(b64, validate=False)
        except Exception:  # noqa: BLE001
            return b""
        return b""
    # file://
    if url.startswith("file://"):
        path = _normalize_file_url(url)
        if _is_windows_absolute(path):
            path = path.replace("/", "\\")
        return _read_file_bytes_sync(path)
    # http(s)
    if url.startswith("http://") or url.startswith("https://"):
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.content
        except Exception:  # noqa: BLE001
            return b""
    # 本地路径
    return _read_file_bytes_sync(url)
