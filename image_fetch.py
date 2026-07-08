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
