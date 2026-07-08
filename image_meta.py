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
