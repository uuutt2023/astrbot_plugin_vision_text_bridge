"""test_chat_archive.py - chat_archive 集成测试 (10 个用例)。"""
import os
import sys
import hashlib
import asyncio
import tempfile
import sqlite3
import base64
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs, make_test_plugin, make_test_plugin_with_caption_cache  # noqa: E402
install_stubs()
import main  # noqa: E402
import caption_cache  # noqa: E402
import chat_archive_integration as cai  # noqa: E402


def test_url_hash_matches_chat_archive():
    """: sha256(url)[:32] 与 chat_archive.ArchiveMediaCache.url_hash 一致。"""
    url = "https://example.com/img.jpg"
    expected = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    assert cai._url_hash(url) == expected, "url_hash 必须与 chat_archive 规则一致"
    print("✓ test_url_hash_matches_chat_archive")


def test_find_chat_archive_image_returns_none_when_dir_missing():
    """: cache_dir 不存在时 find_chat_archive_image 返 None。"""
    with patch.object(cai, "get_chat_archive_cache_dir", return_value=None):
        assert cai.find_chat_archive_image("https://x.com/1.jpg") is None
    print("✓ test_find_chat_archive_image_returns_none_when_dir_missing")


def test_find_chat_archive_image_returns_bytes_when_file_exists():
    """: web_cache/<hash>.jpg 存在时返 (bytes, mime, w, h)。"""
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "web_cache"
    cache.mkdir(parents=True, exist_ok=True)
    # 写一个最小的 PNG (4x3 white pixel)
    import struct, zlib
    def _crc(data):
        return zlib.crc32(data) & 0xFFFFFFFF
    def _make_png(w=1, h=1):
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        ihdr_chunk = b"IHDR" + ihdr
        ihdr_block = struct.pack(">I", 13) + ihdr_chunk + struct.pack(">I", _crc(ihdr_chunk))
        raw = b""
        for _ in range(h):
            raw += b"\x00" + b"\xff\xff\xff" * w
        comp = zlib.compress(raw)
        idat_chunk = b"IDAT" + comp
        idat_block = struct.pack(">I", len(comp)) + idat_chunk + struct.pack(">I", _crc(idat_chunk))
        iend_chunk = b"IEND"
        iend_block = struct.pack(">I", 0) + iend_chunk + struct.pack(">I", _crc(iend_chunk))
        return sig + ihdr_block + idat_block + iend_block
    image_bytes = _make_png(4, 3)
    url = "https://example.com/test1.png"
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    (cache / f"{h}.png").write_bytes(image_bytes)
    with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache):
        result = cai.find_chat_archive_image(url)
    assert result is not None
    data, mime, w, h_px = result
    assert data == image_bytes
    assert mime == "image/png"
    print("✓ test_find_chat_archive_image_returns_bytes_when_file_exists")


def test_find_chat_archive_image_returns_none_for_missing_file():
    """: url 在 web_cache 找不到, 返 None。"""
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "web_cache"
    cache.mkdir(parents=True, exist_ok=True)
    with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache):
        assert cai.find_chat_archive_image("https://nonexistent.com/xxx.jpg") is None
    print("✓ test_find_chat_archive_image_returns_none_for_missing_file")


def test_persist_skips_b64_when_chat_archive_installed():
    """: chat_archive 装时 _persist 不存 image_b64 (省 DB)。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "persist.db"
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))
        # bypass _fetch_image_meta 走直接控制
        async def fake_fetch_meta(url, image_bytes=b""):
            return "ZmFrZWJhc2U2NA==", "image/jpeg", 100, 100, 11
        with patch.object(plugin, "_fetch_image_meta", fake_fetch_meta):
            with patch.object(cai, "is_chat_archive_installed", return_value=True):
                asyncio.run(plugin._persist(
                    image_id="abc123", url="https://x.com/a.jpg",
                    description="猫", image_bytes=b"\xff\xd8\xff\xe0",  # JPEG header
                ))
        # 验证: b64 字段在 SQLite 里是空字符串
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT image_b64 FROM image_captions WHERE image_id = ?", ("abc123",)).fetchone()
        assert row[0] == "", f"chat_archive 装时 image_b64 应为空, 实际: {row[0][:20]}"
    print("✓ test_persist_skips_b64_when_chat_archive_installed")


def test_persist_stores_b64_when_chat_archive_not_installed():
    """: chat_archive 未装时 _persist 仍存 image_b64 (兼容老路径)。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "persist2.db"
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))
        async def fake_fetch_meta(url, image_bytes=b""):
            return "ZmFrZWJhc2U2NA==", "image/jpeg", 100, 100, 11
        with patch.object(plugin, "_fetch_image_meta", fake_fetch_meta):
            with patch.object(cai, "is_chat_archive_installed", return_value=False):
                asyncio.run(plugin._persist(
                    image_id="abc456", url="https://x.com/b.jpg",
                    description="狗", image_bytes=b"\xff\xd8\xff\xe0",
                ))
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT image_b64 FROM image_captions WHERE image_id = ?", ("abc456",)).fetchone()
        assert row[0] == "ZmFrZWJhc2U2NA==", f"chat_archive 未装时应存 b64, 实际: {row[0][:20]}"
    print("✓ test_persist_stores_b64_when_chat_archive_not_installed")


def test_thumbnail_uses_chat_archive_when_no_b64():
    """: SQLite 没 b64, 但 chat_archive 有文件 → 走 chat_archive。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "thumb.db"
        cache_dir = Path(tmp) / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))

        url = "https://x.com/c.jpg"
        # put 时 b64 空 (chat_archive 装的模式)
        plugin._caption_cache.put("img_789", url, "猫")
        # 写一个文件到 web_cache
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        (cache_dir / f"{h}.jpg").write_bytes(jpeg_bytes)
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache_dir):
                import web_api
                result = asyncio.run(web_api._do_thumbnail(plugin, "img_789"))
        if isinstance(result, tuple):
            body_obj, status = result
        else:
            body_obj, status = result, 200
        assert status == 200, f"应返 200, 实际 {status}"
        payload = body_obj if isinstance(body_obj, dict) else body_obj.get_json()
        assert payload["ok"] is True
        assert payload["data"]["has_image"] is True
        # 验证 data_url 是 base64 of jpeg
        assert payload["data"]["data_url"].startswith("data:image/jpeg;base64,")
        # 验证 base64 解码后是 jpeg 字节
        b64_part = payload["data"]["data_url"].split(",", 1)[1]
        assert base64.b64decode(b64_part) == jpeg_bytes
    print("✓ test_thumbnail_uses_chat_archive_when_no_b64")


def test_thumbnail_uses_b64_when_chat_archive_not_installed():
    """: SQLite 有 b64, chat_archive 没装 → 走 SQLite (老路径)。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "thumb2.db"
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))

        # put 时 b64 有 (chat_archive 未装模式)
        plugin._caption_cache.put(
            "img_111", "https://x.com/d.jpg", "狗",
            image_b64="YWJjZGVm", mime_type="image/jpeg", file_size=6, width=10, height=10,
        )
        with patch.object(cai, "is_chat_archive_installed", return_value=False):
            import web_api
            result = asyncio.run(web_api._do_thumbnail(plugin, "img_111"))
        if isinstance(result, tuple):
            body_obj, status = result
        else:
            body_obj, status = result, 200
        assert status == 200
        payload = body_obj if isinstance(body_obj, dict) else body_obj.get_json()
        assert payload["data"]["has_image"] is True
        assert payload["data"]["data_url"] == "data:image/jpeg;base64,YWJjZGVm"
    print("✓ test_thumbnail_uses_b64_when_chat_archive_not_installed")


def test_thumbnail_returns_has_image_false_when_both_missing():
    """: SQLite 没 b64 + chat_archive 没文件 → has_image=False。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "thumb3.db"
        cache_dir = Path(tmp) / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin = make_test_plugin_with_caption_cache(main, str(db_path))

        plugin._caption_cache.put("img_222", "https://x.com/e.jpg", "鸟")
        # 故意不放文件
        with patch.object(cai, "is_chat_archive_installed", return_value=True):
            with patch.object(cai, "get_chat_archive_cache_dir", return_value=cache_dir):
                import web_api
                result = asyncio.run(web_api._do_thumbnail(plugin, "img_222"))
        if isinstance(result, tuple):
            body_obj, status = result
        else:
            body_obj, status = result, 200
        assert status == 200
        payload = body_obj if isinstance(body_obj, dict) else body_obj.get_json()
        assert payload["data"]["has_image"] is False
        assert payload["data"]["data_url"] == ""
    print("✓ test_thumbnail_returns_has_image_false_when_both_missing")


def test_clean_expired_still_runs_for_description_only():
    """: 本插件的 clean_expired 仍跑 (清 description 行, 跟 chat_archive 清理 web_cache 不冲突)。"""
    import time
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(Path(tmp) / "clean.db")
        cap.put("k1", "https://a.com/1", "猫")
        with sqlite3.connect(cap._db_path) as c:
            c.execute(
                "UPDATE image_captions SET created_at = ? WHERE image_id = 'k1'",
                (time.time() - 100 * 86400,),
            )
            c.commit()
        # 清理 7 天前 → k1 删
        deleted = cap.clean_expired(max_age_days=7)
        assert deleted == 1
        # k1 entry 删除
        assert cap.get("k1") is None
    print("✓ test_clean_expired_still_runs_for_description_only")


if __name__ == "__main__":
    test_url_hash_matches_chat_archive()
    test_find_chat_archive_image_returns_none_when_dir_missing()
    test_find_chat_archive_image_returns_bytes_when_file_exists()
    test_find_chat_archive_image_returns_none_for_missing_file()
    test_persist_skips_b64_when_chat_archive_installed()
    test_persist_stores_b64_when_chat_archive_not_installed()
    test_thumbnail_uses_chat_archive_when_no_b64()
    test_thumbnail_uses_b64_when_chat_archive_not_installed()
    test_thumbnail_returns_has_image_false_when_both_missing()
    test_clean_expired_still_runs_for_description_only()
    print("---")
    print("ALL CHAT_ARCHIVE INTEGRATION TESTS PASSED")
