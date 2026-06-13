"""chat_archive_integration.py — 与 astrbot_plugin_chat_archive 协同。

设计:
  1. 检测 chat_archive 是否安装（看 data/plugins/astrbot_plugin_chat_archive/metadata.yaml）
  2. 如果装了, 本插件 webui 缩略图从 chat_archive 的 web_cache 拿 (sha256(url)[:32] + ext)
  3. 本插件的 SQLite 不再存 image_b64 (省 DB 空间, 单一缓存来源)
  4. 过期清理交给 chat_archive (它每天扫 web_cache 删除 mtime > N 天的文件)

参考:
  - https://github.com/YukiNo420/astrbot_plugin_chat_archive
  - chat_archive.archive.media_cache.ArchiveMediaCache.download_media_to_cache(url) 返
    /static/cache/<sha256(url)[:32]><ext>
  - chat_archive.config.get_static_cache_dir() 返 <data>/plugin_data/astrbot_plugin_chat_archive/web_cache/
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Tuple

# 图像扩展名 (与 chat_archive._CONTENT_TYPE_EXTENSIONS 反向对应)
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

# 1) chat_archive 安装目录
#    AstrBot 插件目录通常: <AstrBot data>/data/plugins/<plugin_name>/
#    本插件无法直接拿 AstrBot data 路径, 用 plugin.parent.parent.plugins / plugin.parent.plugins 探测
def _get_plugin_root() -> Optional[Path]:
    """: 探测 AstrBot 插件目录 (data/plugins/) 的可能位置。"""
    try:
        from astrbot.api.star import StarTools
        d = StarTools.get_data_dir()
        # d = <data>/plugin_data/<plugin_name>
        # 父目录 = <data>/plugin_data/
        # 祖父目录 = <data>/
        return d.parent.parent
    except Exception:
        return None


def is_chat_archive_installed() -> bool:
    """: 探测 astrbot_plugin_chat_archive 是否安装。"""
    root = _get_plugin_root()
    if root is None:
        return False
    # 1. 探测 plugins 子目录
    plugins_dir = root / "plugins" / "astrbot_plugin_chat_archive"
    if (plugins_dir / "metadata.yaml").exists() or (plugins_dir / "main.py").exists():
        return True
    # 2. 探测 data/plugins (部署变体)
    alt = root / "data" / "plugins" / "astrbot_plugin_chat_archive"
    if (alt / "metadata.yaml").exists() or (alt / "main.py").exists():
        return True
    return False


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def get_chat_archive_cache_dir() -> Optional[Path]:
    """: chat_archive 的 web_cache 目录。

    chat_archive 的 web_cache 路径:
      <AstrBot data>/plugin_data/astrbot_plugin_chat_archive/web_cache/
    """
    root = _get_plugin_root()
    if root is None:
        return None
    cache = root / "plugin_data" / "astrbot_plugin_chat_archive" / "web_cache"
    if cache.is_dir():
        return cache
    return None


# ---------------------------------------------------------------------------
# 查图
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    """与 chat_archive.ArchiveMediaCache._guess_extension 同一规则:
    sha256(url.encode("utf-8")) 取前 32 个 hex 字符。
    """
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:32]


def find_chat_archive_image(url: str) -> Optional[Tuple[bytes, str, int, int]]:
    """: 在 chat_archive web_cache 找 url 对应的图片。

    Returns:
        (bytes, mime, width, height) 或 None
    """
    cache_dir = get_chat_archive_cache_dir()
    if cache_dir is None or not url:
        return None
    h = _url_hash(url)
    for ext in _IMAGE_EXTS:
        p = cache_dir / f"{h}{ext}"
        if p.is_file():
            data = p.read_bytes()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(ext, "image/jpeg")
            # 宽高从字节解析
            try:
                w, h_px = _read_image_dimensions(data)
            except Exception:
                w, h_px = 0, 0
            return data, mime, w, h_px
    return None


def _read_image_dimensions(data: bytes) -> Tuple[int, int]:
    """: 从图片字节解析宽高 (PNG/JPEG/WebP/GIF 头部读取)。"""
    if not data or len(data) < 24:
        return 0, 0
    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # IHDR: width (4B big-endian) + height (4B) at offset 16
        import struct
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    # JPEG
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            # SOFn markers (start of frame)
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                import struct
                h_, w_ = struct.unpack(">HH", data[i + 3:i + 7])
                return int(w_), int(h_)
            # 跳过段
            seg_len = struct.unpack(">H", data[i:i + 2])[0]
            i += seg_len
        return 0, 0
    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a"):
        import struct
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)
    # WebP (VP8X chunk)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # VP8L (lossless) or VP8X (extended) or VP8 (lossy)
        chunk = data[12:16]
        if chunk == b"VP8X":
            # width/height encoded in 24-bit values
            b = data[20:26]
            w = 1 + (b[0] | (b[1] << 8) | (b[2] << 16))
            h = 1 + (b[3] | (b[4] << 8) | (b[5] << 16))
            return int(w), int(h)
        if chunk == b"VP8 ":
            import struct
            w, h = struct.unpack("<HH", data[26:30])
            return int(w), int(h)
        if chunk == b"VP8L":
            b = data[21:25]
            w = 1 + (b[0] | (b[1] << 8) | (b[2] << 16))
            h = 1 + ((b[3] & 0x0F) << 10 | (b[1] & 0xC0) << 2 | (b[2] & 0xC0) << 4)
            return int(w), int(h)
        return 0, 0
    return 0, 0
