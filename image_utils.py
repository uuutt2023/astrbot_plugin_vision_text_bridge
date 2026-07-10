"""image_utils.py - 通用图片 URL helper。

API: _is_cacheable_url / extract_image_url / collect_image_urls_from_components / is_bot_avatar_url
作者: uuutt
"""

from __future__ import annotations

from typing import Any, Iterable


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
    urls: list[str] = []
    for p in parts or []:
        u = extract_url_from_item(p)
        if u:
            urls.append(u)
    return urls


def extract_urls_from_context_list(content_list: Any) -> list[str]:
    """从 ``req.contexts[i].content`` list 抽 image_url 字段的 URL。"""
    urls: list[str] = []
    if not isinstance(content_list, list):
        return urls
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "image_url":
            u = extract_url_from_item(item)
            if u:
                urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# 类型判定
# ---------------------------------------------------------------------------

def is_data_url(url: str) -> bool:
    """判断 URL 是不是 ``data:image/...;base64,`` 形式 (内联图)。

    只看前 64 字符就够 — data URL header 都很短。
    """
    return bool(url) and url.startswith("data:image/") and ";base64," in url[:64]


# ---------------------------------------------------------------------------
# 链末 hook 用: 按条件删 image_url 字段
# ---------------------------------------------------------------------------

def strip_image_urls(req: Any, only_data_url: bool) -> int:
    """从 ``req`` 三处删 image_url 组件, 返回删除条数。

    ``only_data_url=True`` 只删 ``data:base64`` 内联图 (AstrBot 框架
    偶尔把整张图 base64 塞进 image_urls, 太大撑爆上下文, 链末清掉)。
    ``False`` 全删 (本插件主 hook 已经把真 URL 翻译成 caption,
    残留的 image_url 字段需要清空, 否则会二次发图)。
    """
    removed = 0

    # 1. req.image_urls: list[str]
    if getattr(req, "image_urls", None):
        if only_data_url:
            kept = [u for u in req.image_urls if not is_data_url(u)]
            removed += len(req.image_urls) - len(kept)
        else:
            removed += len(req.image_urls)
            kept = []
        req.image_urls = kept

    # 2. req.extra_user_content_parts
    ep = getattr(req, "extra_user_content_parts", None)
    if ep:
        kept = []
        for p in ep:
            if is_image_url_part(p):
                u = extract_url_from_item(p)
                if only_data_url and not is_data_url(u):
                    kept.append(p)
                    continue
                removed += 1
                continue
            kept.append(p)
        ep[:] = kept

    # 3. req.contexts[i].content[j]
    ctxs = getattr(req, "contexts", None) or []
    for c in ctxs:
        if not isinstance(c, dict):
            continue
        content = c.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "image_url":
                u = extract_url_from_item(x)
                if only_data_url and not is_data_url(u):
                    kept.append(x)
                    continue
                removed += 1
                continue
            kept.append(x)
        content[:] = kept

    return removed


# ---------------------------------------------------------------------------
# 从 message chain 里取图 (event.message_obj.message)
# ---------------------------------------------------------------------------

_NESTED_TYPES = frozenset((
    "reply", "Reply", "reference", "Reference",
    "forward", "Forward", "json", "Json", "node", "Node",
))
_NESTED_ATTRS = ("message", "messages", "content", "data", "nodes", "_message", "_data")


async def _extract_image_url_from_component(comp) -> str | None:
    """从一个 component 取图片本地路径 (调 convert_to_file_path)。失败返 None。"""
    if not callable(getattr(comp, "convert_to_file_path", None)):
        return None
    try:
        return await comp.convert_to_file_path()
    except Exception:
        return None


async def collect_image_urls_from_components(components, dedupe: list[str] | None = None) -> int:
    """递归从 message chain (顶层 + 嵌套 reply/Reference/forward/node) 拿图片。

    参数:
        components: message chain (list of components)
        dedupe: 已有的 URL 列表, 收集时会跳过 (用于 "event.message_obj
                里的图不要和 req.image_urls 重复")
    返回: 新增的图片数。
    """
    added = 0
    for comp in components:
        ctype = getattr(comp, "type", None)
        if ctype in _NESTED_TYPES:
            for attr in _NESTED_ATTRS:
                inner = getattr(comp, attr, None)
                if isinstance(inner, list):
                    added += await collect_image_urls_from_components(inner, dedupe)
                    break
            continue
        if ctype in ("image", "Image"):
            fp = await _extract_image_url_from_component(comp)
            if fp and (dedupe is None or fp not in dedupe):
                dedupe.append(fp)
                added += 1
    return added


"""image_meta.py - 图片元数据提取 (尺寸/格式)。"""

from __future__ import annotations

import io
from typing import Any

#  AstrBot 把 Pydantic TextPart 作为 content part 注入。
# 测试沙箱 / 未来重命名可能没这个 import — try/except 兜底。
try:
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # type: ignore


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
    if not data or len(data) < 16:
        return "", 0, 0

    # 1) PIL
    try:
        from PIL import Image as _PIL
        with _PIL.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            mime = {
                "PNG": "image/png", "JPEG": "image/jpeg", "JPG": "image/jpeg",
                "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp",
                "ICO": "image/x-icon",
            }.get(fmt, "image/jpeg")
            return mime, int(im.width or 0), int(im.height or 0)
    except Exception:
        pass

    # 2) magic bytes
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        return _sniff_jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _sniff_webp(data)
    return "", 0, 0


def _sniff_jpeg(data: bytes) -> tuple[str, int, int]:
    """JPEG 复杂点 — SOF marker 嵌 w/h。返 (mime, w, h) 或 (mime, 0, 0) 失败。"""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            break
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return "image/jpeg", w, h
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return "image/jpeg", 0, 0


def _sniff_webp(data: bytes) -> tuple[str, int, int]:
    """WEBP 容器, 内部 VP8 / VP8L / VP8X。返 (mime, w, h) 或 (mime, 0, 0) 失败。"""
    if data[12:16] == b"VP8 " and len(data) >= 30:
        return "image/webp", (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if data[12:16] == b"VP8L" and len(data) >= 25:
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)) + 1
        return "image/webp", w, h
    return "image/webp", 0, 0


# ---------------------------------------------------------------------------
# URL 缓存策略
# ---------------------------------------------------------------------------

def is_cacheable_url(url: str, config: Any) -> bool:
    """判断 url 适不适合入缓存 (SQLite + 内存)。

    支持的 scheme:
        - ``http://`` / ``https://`` — 网络图, 永久 OK
        - ``file://`` — 本地文件, 受 ``cache_file_paths`` 开关
        - ``/`` 开头 (Unix 绝对路径) — 裸本地路径, 受 ``cache_file_paths`` 开关
        - Windows 盘符 (``C:\\`` / ``C:/``) — 同上
        - ``data:image/...`` — 内联 base64, **不**入缓存 (重复太多, 撑爆 DB)

    .4 之前只认 http(s)/file, 漏掉 AstrBot 实际传的裸路径 →
    ``_describe_one`` 里 ``if cacheable and cache_key`` 跳过整段缓存逻辑
    → webui 永远空。这个 fix 修的就是这个。
    """
    if not url:
        return False
    lo = url.lower()
    if lo.startswith("http://") or lo.startswith("https://"):
        return True
    if lo.startswith("file://"):
        return bool(config.get("cache_file_paths", True))
    if lo.startswith("data:image/"):
        return False
    if lo.startswith("/") or (len(lo) >= 2 and lo[1] == ":"):
        return bool(config.get("cache_file_paths", True))
    return False


"""image_fetch.py - 图片下载 + 缓存 (用于 mmx 子进程读图)。"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import unquote


def _read_file_bytes_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _normalize_file_url(url: str) -> str:
    """``file:///C:/path`` → ``C:/path`` (Windows) / ``file:///foo`` → ``/foo`` (Unix)。"""
    path = url[len("file://"):]
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path


def _is_windows_absolute(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":"


async def read_image_bytes(url: str) -> bytes:
    """读 url 指向的图片字节。

    Raises:
        ValueError: 不支持的 scheme (e.g. ``data:``, ``ftp://``)
        aiohttp.ClientResponseError: HTTP 4xx / 5xx
        FileNotFoundError: 本地文件不存在
    """
    lo = url.lower()
    if lo.startswith("file://"):
        return await asyncio.to_thread(_read_file_bytes_sync, _normalize_file_url(url))
    if lo.startswith("http://") or lo.startswith("https://"):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                return await r.read()
    if lo.startswith("/") or _is_windows_absolute(lo):
        return await asyncio.to_thread(_read_file_bytes_sync, url)
    raise ValueError(f"unsupported scheme: {url[:50]}")


# 保留模块级兼容 shim — 老测试可能 ``from main import _read_file_bytes_sync``
__all__ = ["read_image_bytes", "_read_file_bytes_sync"]
