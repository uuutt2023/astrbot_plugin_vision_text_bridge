"""test_integration_status.py — /cache/integration_status 端点反向验证。

覆盖:
  - chat_archive 未装 → storage_mode="local", policy.expiry_cleanup="local"
  - chat_archive 装了 + 无老 b64 → storage_mode="chat_archive"
  - chat_archive 装了 + 有老 b64 → storage_mode="mixed"
  - 缩略图 source 字段: local / chat_archive / none 三种情况
"""
import os
import sys
import asyncio
import tempfile
import hashlib
import struct
import zlib
import base64
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs, make_test_plugin, make_test_plugin_with_caption_cache  # noqa: E402
install_stubs()
import main  # noqa: E402
import web_api  # noqa: E402
import chat_archive_integration as cai  # noqa: E402


def _make_png(w=4, h=3):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    ihdr_chunk = b"IHDR" + ihdr
    crc = zlib.crc32(ihdr_chunk) & 0xFFFFFFFF
    ihdr_block = struct.pack(">I", 13) + ihdr_chunk + struct.pack(">I", crc)
    raw = b""
    for _ in range(h):
        raw += b"\x00" + b"\xff\xff\xff" * w
    comp = zlib.compress(raw)
    idat_chunk = b"IDAT" + comp
    idat_block = struct.pack(">I", len(comp)) + idat_chunk + struct.pack(">I", zlib.crc32(idat_chunk) & 0xFFFFFFFF)
    iend_chunk = b"IEND"
    iend_block = struct.pack(">I", 0) + iend_chunk + struct.pack(">I", zlib.crc32(iend_chunk) & 0xFFFFFFFF)
    return sig + ihdr_block + idat_block + iend_block


def _run(coro):
    return asyncio.run(coro)


def test_integration_status_chat_archive_not_installed():
    """: chat_archive 未装 → storage_mode=local, policy.expiry_cleanup=local."""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=False):
            result = _run(web_api.api_integration_status(plugin))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        assert status == 200
        d = body if isinstance(body, dict) else body.get_json()
        assert d["ok"] is True
        data = d["data"]
        assert data["chat_archive_installed"] is False
        assert data["chat_archive_cache_dir"] is None
        assert data["storage_mode"] == "local"
        assert data["policy"]["thumbnail_source"] == "local"
        assert data["policy"]["image_b64_stored"] is True
        assert data["policy"]["expiry_cleanup"] == "local"
        assert data["policy"]["description_cleanup"] == "local"
    print("✓ test_integration_status_chat_archive_not_installed")


def test_integration_status_chat_archive_installed_no_local_b64():
    """: chat_archive 装了 + 无老 b64 → storage_mode=chat_archive."""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        # 装时新 put 不会存 b64 (见 _persist chat_archive 路径)
        # 但 put 存的是空 b64, 不算 has_local_b64
        plugin._caption_cache.put("img1", "https://x.com/a.jpg", "猫")  # b64=""
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=Path("/fake/web_cache")):
                result = _run(web_api.api_integration_status(plugin))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        data = d["data"]
        assert data["chat_archive_installed"] is True
        assert data["chat_archive_cache_dir"] == "/fake/web_cache"
        # b64 是空 (新条目装时存空) → storage_mode=chat_archive (非 mixed)
        assert data["storage_mode"] == "chat_archive"
        assert data["policy"]["thumbnail_source"] == "chat_archive"
        assert data["policy"]["image_b64_stored"] is False
        assert data["policy"]["expiry_cleanup"] == "chat_archive"
    print("✓ test_integration_status_chat_archive_installed_no_local_b64")


def test_integration_status_chat_archive_installed_with_local_b64():
    """: chat_archive 装了 + 有老 b64 → storage_mode=mixed。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        # 老条目 b64 非空 (chat_archive 未装时存的)
        plugin._caption_cache.put(
            "old_img", "https://x.com/old.jpg", "老描述",
            image_b64="YWJjZGVm", mime_type="image/jpeg",
        )
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=Path("/fake/web_cache")):
                result = _run(web_api.api_integration_status(plugin))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        data = d["data"]
        assert data["chat_archive_installed"] is True
        assert data["storage_mode"] == "mixed", f"有老 b64 应是 mixed, 实际 {data['storage_mode']}"
    print("✓ test_integration_status_chat_archive_installed_with_local_b64")


def test_integration_status_chat_archive_dir_none_when_uninstalled():
    """: chat_archive 未装时 cache_dir 必为 None。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=False):
            result = _run(web_api.api_integration_status(plugin))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        assert d["data"]["chat_archive_cache_dir"] is None
    print("✓ test_integration_status_chat_archive_dir_none_when_uninstalled")


def test_integration_status_handles_chat_archive_check_error():
    """: chat_archive 检测抛异常时降级为未装 (不让 API 挂)。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", side_effect=OSError("fs read error")):
            result = _run(web_api.api_integration_status(plugin))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        assert status == 200, f"应降级返 200, 实际 {status}"
        d = body if isinstance(body, dict) else body.get_json()
        assert d["data"]["chat_archive_installed"] is False
    print("✓ test_integration_status_handles_chat_archive_check_error")


def test_thumbnail_source_field_local():
    """: SQLite 有 b64 → 缩略图 source=local."""
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_test_plugin_with_caption_cache(main, str(Path(tmp) / "i.db"))
        plugin._caption_cache.put(
            "img1", "https://x.com/a.jpg", "猫",
            image_b64="YWJjZGVm", mime_type="image/jpeg", file_size=6, width=10, height=10,
        )
        result = _run(web_api._do_thumbnail(plugin, "img1"))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        assert d["data"]["source"] == "local"
        assert d["data"]["has_image"] is True
    print("✓ test_thumbnail_source_field_local")


def test_thumbnail_source_field_chat_archive():
    """: SQLite 无 b64 + chat_archive 有图 → 缩略图 source=chat_archive."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "th.db"
        cache_dir = Path(tmp) / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))
        url = "https://x.com/c.jpg"
        plugin._caption_cache.put("img1", url, "猫")  # b64 空
        png_bytes = _make_png(4, 3)
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        (cache_dir / f"{h}.png").write_bytes(png_bytes)
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache_dir):
                result = _run(web_api._do_thumbnail(plugin, "img1"))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        assert d["data"]["source"] == "chat_archive", f"应 source=chat_archive, 实际 {d['data'].get('source')}"
        assert d["data"]["has_image"] is True
    print("✓ test_thumbnail_source_field_chat_archive")


def test_thumbnail_source_field_none():
    """: SQLite 无 b64 + chat_archive 也无 → source=none, has_image=False."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "th.db"
        cache_dir = Path(tmp) / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))
        url = "https://x.com/missing.jpg"
        plugin._caption_cache.put("img1", url, "鸟")  # b64 空
        cai.reset_cache_for_testing()
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache_dir):
                result = _run(web_api._do_thumbnail(plugin, "img1"))
        if isinstance(result, tuple):
            body, status = result
        else:
            body, status = result, 200
        d = body if isinstance(body, dict) else body.get_json()
        assert d["data"]["source"] == "none"
        assert d["data"]["has_image"] is False
    print("✓ test_thumbnail_source_field_none")


if __name__ == "__main__":
    test_integration_status_chat_archive_not_installed()
    test_integration_status_chat_archive_installed_no_local_b64()
    test_integration_status_chat_archive_installed_with_local_b64()
    test_integration_status_chat_archive_dir_none_when_uninstalled()
    test_integration_status_handles_chat_archive_check_error()
    test_thumbnail_source_field_local()
    test_thumbnail_source_field_chat_archive()
    test_thumbnail_source_field_none()
    print("---")
    print("ALL INTEGRATION STATUS TESTS PASSED")
