"""test_regenerate.py - /cache/regenerate 端点修复测试 (3 个用例)。"""
import os
import sys
import json
import asyncio
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs, make_test_plugin, make_test_plugin_with_caption_cache  # noqa: E402
install_stubs()
import main  # noqa: E402
import web_api  # noqa: E402


def test_regenerate_uses_url_not_image_id():
    """: /cache/regenerate 把 image_id 查成 url, 再调 mmx 拿新 desc。

    用户报告 bug: 重新生成按钮调 mmx 报 'File not found: <image_id>',
    原因是 _describe_one 拿到 image_id 字符串就当 URL 传了。
    修复: 先从 SQLite 查 entry.url, 再 _describe_one(url)。
    """
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    plugin = make_test_plugin_with_caption_cache(main, str(db_path))

    new_desc = "新生成的描述内容"
    captured = {"url": None}

    async def fake_describe(url):
        captured["url"] = url
        return new_desc

    plugin._caption_cache.put(
        image_id="abc123def45678901234567890abcde",
        url="file:///tmp/original.jpg",
        description="老描述",
    )

    async def fake_read_key(ctx):
        return "abc123def45678901234567890abcde"

    with patch.object(plugin, "_describe_one", fake_describe):
        with patch.object(web_api, "read_key_from_request", side_effect=fake_read_key):
            result = asyncio.run(web_api.api_regenerate(plugin))
    if isinstance(result, tuple):
        body_obj, status = result
    else:
        body_obj, status = result, 200
    assert status == 200, f"应返 200, 实际 {status}"
    # 关键: _describe_one 收到的应是 URL, 不是 image_id
    assert captured["url"] == "file:///tmp/original.jpg", \
        f"_describe_one 应收到 URL, 实际收到: {captured['url']!r}"
    payload = body_obj if isinstance(body_obj, dict) else json.loads(body_obj.get_data(as_text=True))
    assert payload["ok"] is True
    assert payload["data"]["key"] == "abc123def45678901234567890abcde"
    assert payload["data"]["description"] == new_desc
    print("✓ test_regenerate_uses_url_not_image_id")


def test_regenerate_404_when_image_id_not_found():
    """: 重新生成时 image_id 不存在, 返 404 而非走错路。"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    plugin = make_test_plugin_with_caption_cache(main, str(db_path))

    async def fake_read_key(ctx):
        return "nonexistent_image_id_1234567890abcdef"

    called = {"n": 0}

    async def fake_describe_should_not_be_called(url):
        called["n"] += 1
        return ""

    with patch.object(web_api, "read_key_from_request", side_effect=fake_read_key):
        with patch.object(plugin, "_describe_one", fake_describe_should_not_be_called):
            result = asyncio.run(web_api.api_regenerate(plugin))
    if isinstance(result, tuple):
        body_obj, status = result
    else:
        body_obj, status = result, 200
    assert status == 404, f"image_id 不存在应返 404, 实际 {status}"
    assert called["n"] == 0, f"_describe_one 不应被调 (image_id 找不到), 调了 {called['n']} 次"
    print("✓ test_regenerate_404_when_image_id_not_found")


def test_regenerate_removes_old_cache_before_regenerating():
    """: regenerate 路径必须先 delete 旧缓存, 再 describe (避免返回老 desc)。"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    plugin = main.VisionTextBridgePlugin.__new__(main.VisionTextBridgePlugin)
    plugin.config = {"mmx_path": "/usr/bin/true"}
    plugin.mmx_path = "/usr/bin/true"
    plugin.context = SimpleNamespace()
    plugin._caption_cache = main.CaptionCache(db_path)
    plugin._description_cache = {"abc123def45678901234567890abcde": "内存老描述"}
    plugin._vision_semaphore = asyncio.Semaphore(1)

    plugin._caption_cache.put(
        image_id="abc123def45678901234567890abcde",
        url="file:///tmp/orig.jpg",
        description="SQLite 老描述",
    )

    async def fake_read_key(ctx):
        return "abc123def45678901234567890abcde"

    async def fake_describe(url):
        return "新描述"
    with patch.object(web_api, "read_key_from_request", side_effect=fake_read_key):
        with patch.object(plugin, "_describe_one", fake_describe):
            result = asyncio.run(web_api.api_regenerate(plugin))
    entry = plugin._caption_cache.get("abc123def45678901234567890abcde")
    assert entry is None, f"regenerate 后旧 SQLite 应被 delete, 但仍存在: {entry}"
    assert "abc123def45678901234567890abcde" not in plugin._description_cache
    print("✓ test_regenerate_removes_old_cache_before_regenerating")


if __name__ == "__main__":
    test_regenerate_uses_url_not_image_id()
    test_regenerate_404_when_image_id_not_found()
    test_regenerate_removes_old_cache_before_regenerating()
    print("---")
    print("ALL REGENERATE TESTS PASSED")
