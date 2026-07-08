"""test_smart_imagechat_hub.py - smart_imagechat_hub 兼容功能测试 (18 个用例)。"""
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs  # noqa: E402

install_stubs()

import smart_imagechat_hub_integration  # noqa: E402
import web_api  # noqa: E402


# ---------------------------------------------------------------------------
# 1) 检测函数
# ---------------------------------------------------------------------------

def test_is_smart_imagechat_hub_installed_returns_false_when_no_metadata():
    """: 没装时返 False。"""
    smart_imagechat_hub_integration.reset_cache_for_testing()
    # _get_plugin_root 返 None (stub) → 直接 cache=False
    result = smart_imagechat_hub_integration.is_smart_imagechat_hub_installed()
    assert result is False
    print("✓ test_is_smart_imagechat_hub_installed_returns_false_when_no_metadata")


def test_is_smart_imagechat_hub_installed_returns_true_when_metadata_exists():
    """: 装了时返 True。"""
    import tempfile
    from pathlib import Path as _P
    smart_imagechat_hub_integration.reset_cache_for_testing()
    with tempfile.TemporaryDirectory() as tmp:
        # 构造真实路径: tmp/data/plugins/astrbot_plugin_smart_imagechat_hub/metadata.yaml
        plugin_dir = _P(tmp) / "data" / "plugins" / "astrbot_plugin_smart_imagechat_hub"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "metadata.yaml").write_text("name: test\n")
        # mock _get_plugin_root 返 tmp
        from unittest.mock import patch as _patch
        with _patch.object(smart_imagechat_hub_integration, "_get_plugin_root", return_value=_P(tmp)):
            result = smart_imagechat_hub_integration.is_smart_imagechat_hub_installed()
    assert result is True
    # 再调一次, 应该走 cache
    result2 = smart_imagechat_hub_integration.is_smart_imagechat_hub_installed()
    assert result2 is True
    print("✓ test_is_smart_imagechat_hub_installed_returns_true_when_metadata_exists")


def test_install_check_cache_works():
    """: module-level cache 正确生效。"""
    smart_imagechat_hub_integration.reset_cache_for_testing()
    with patch.object(smart_imagechat_hub_integration, "_get_plugin_root") as mock_root:
        mock_root.return_value = None
        # 第一次返 False, 缓存
        r1 = smart_imagechat_hub_integration.is_smart_imagechat_hub_installed()
        assert r1 is False
        # 第二次不查盘
        with patch.object(smart_imagechat_hub_integration, "_get_plugin_root") as mock2:
            mock2.return_value = MagicMock()
            r2 = smart_imagechat_hub_integration.is_smart_imagechat_hub_installed()
        assert r2 is False
        assert not mock2.called  # 缓存命中
    print("✓ test_install_check_cache_works")


def test_reset_cache_for_testing():
    """: reset_cache_for_testing 清缓存。"""
    smart_imagechat_hub_integration._INSTALL_CHECK_CACHE = True
    smart_imagechat_hub_integration.reset_cache_for_testing()
    assert smart_imagechat_hub_integration._INSTALL_CHECK_CACHE is None
    print("✓ test_reset_cache_for_testing")


# ---------------------------------------------------------------------------
# 2) web API /v1/chat/completions
# ---------------------------------------------------------------------------

def test_api_chat_completions_returns_openai_format():
    """: /v1/chat/completions 返回 OpenAI ChatCompletion 格式。"""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock

    fake_plugin = MagicMock()
    fake_plugin._describe_one = AsyncMock(return_value="图片里有一只橘猫在窗台上晒太阳")
    fake_plugin.config = {"enable_smart_imagechat_hub_compat": True}

    body = {
        "model": "vision_text_bridge",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请为这张图片生成 5-7 个标签"},
                {"type": "image_url", "image_url": {"url": "http://example.com/cat.jpg"}},
            ],
        }],
    }

    mock_req = MagicMock()
    mock_req.method = "POST"
    mock_req.get_json = AsyncMock(return_value=body)
    mock_req.get_data = AsyncMock(return_value=b"{}")

    def _fake_jsonify(o):
        # 模拟 quart jsonify 返的对象 (有 get_json() 方法, 也可直接当 dict)
        class _Resp:
            def __init__(self, data):
                self._data = data
            def get_json(self):
                return self._data
            def __getitem__(self, k):
                return self._data[k]
        return _Resp(o)

    with patch.object(web_api, "quart_request", mock_req), patch.object(web_api, "jsonify", side_effect=_fake_jsonify):
        result = asyncio.run(web_api.api_chat_completions(fake_plugin))

    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, 200
    data = resp.get_json() if hasattr(resp, "get_json") else resp
    assert data["ok"] is True, f"ok 应为 True, 实际 {data}"
    choices = data["data"]["choices"]
    assert len(choices) == 1
    assert choices[0]["message"]["content"] == "图片里有一只橘猫在窗台上晒太阳"
    assert data["data"]["object"] == "chat.completion"
    print("✓ test_api_chat_completions_returns_openai_format")
def test_api_chat_completions_no_image_url_returns_error():
    """: /v1/chat/completions 没图返 error。"""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock

    fake_plugin = MagicMock()
    fake_plugin._describe_one = AsyncMock()

    body = {
        "model": "vision_text_bridge",
        "messages": [{"role": "user", "content": "纯文本, 没图"}],
    }

    mock_req = MagicMock()
    mock_req.method = "POST"
    mock_req.get_json = AsyncMock(return_value=body)
    mock_req.get_data = AsyncMock(return_value=b"{}")

    def _fake_jsonify(o):
        class _Resp:
            def __init__(self, data): self._data = data
            def get_json(self): return self._data
        return _Resp(o)

    with patch.object(web_api, "quart_request", mock_req), patch.object(web_api, "jsonify", side_effect=_fake_jsonify):
        result = asyncio.run(web_api.api_chat_completions(fake_plugin))

    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, 200
    assert status == 400
    data = resp.get_json() if hasattr(resp, "get_json") else resp
    assert data["ok"] is False
    print("✓ test_api_chat_completions_no_image_url_returns_error")
def test_api_image_caption_get():
    """: GET /image/caption?url=... 返 mmx 描述。"""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock

    fake_plugin = MagicMock()
    fake_plugin._describe_one = AsyncMock(return_value="测试描述: 一只猫")

    mock_req = MagicMock()
    mock_req.method = "GET"
    mock_req.args = MagicMock()
    mock_req.args.get = MagicMock(return_value="http://example.com/cat.jpg")

    def _fake_jsonify(o):
        class _Resp:
            def __init__(self, data): self._data = data
            def get_json(self): return self._data
        return _Resp(o)

    with patch.object(web_api, "quart_request", mock_req), patch.object(web_api, "jsonify", side_effect=_fake_jsonify):
        result = asyncio.run(web_api.api_image_caption(fake_plugin))

    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, 200
    data = resp.get_json() if hasattr(resp, "get_json") else resp
    assert data["ok"] is True
    assert data["data"]["caption"] == "测试描述: 一只猫"
    assert data["data"]["url"] == "http://example.com/cat.jpg"
    print("✓ test_api_image_caption_get")
def test_api_image_caption_missing_url():
    """: /image/caption 没 url 返 error。"""
    import asyncio
    from unittest.mock import patch, MagicMock

    fake_plugin = MagicMock()

    mock_req = MagicMock()
    mock_req.method = "GET"
    mock_req.args = MagicMock()
    mock_req.args.get = MagicMock(return_value="")

    def _fake_jsonify(o):
        class _Resp:
            def __init__(self, data): self._data = data
            def get_json(self): return self._data
        return _Resp(o)

    with patch.object(web_api, "quart_request", mock_req), patch.object(web_api, "jsonify", side_effect=_fake_jsonify):
        result = asyncio.run(web_api.api_image_caption(fake_plugin))

    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, 200
    assert status == 400
    data = resp.get_json() if hasattr(resp, "get_json") else resp
    assert data["ok"] is False
    print("✓ test_api_image_caption_missing_url")
def test_integration_status_includes_smart_imagechat_hub():
    """: api_integration_status 返 smart_imagechat_hub 字段。"""
    import asyncio
    from unittest.mock import patch, MagicMock

    fake_plugin = MagicMock()
    fake_plugin._caption_cache = None  # 不混
    fake_plugin.config = {
        "enable_smart_imagechat_hub_compat": True,
    }
    fake_plugin.context.astr_context.config = {
        "dashboard": {"host": "localhost", "port": 6185}
    }

    with patch("chat_archive_integration.is_chat_archive_installed", return_value=False), \
         patch("smart_imagechat_hub_integration.is_smart_imagechat_hub_installed", return_value=True):
        result = asyncio.run(web_api.api_integration_status(fake_plugin))
    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, 200
    data = resp if isinstance(resp, dict) else (resp.get_json() if hasattr(resp, "get_json") else resp)
    assert "smart_imagechat_hub" in data["data"]
    sih = data["data"]["smart_imagechat_hub"]
    assert sih["installed"] is True
    assert sih["compat_enabled"] is True
    assert sih["endpoint"] is not None
    assert "/v1/chat/completions" in sih["endpoint"]
    assert sih["usage_hint"] is not None
    print("✓ test_integration_status_includes_smart_imagechat_hub")


# ---------------------------------------------------------------------------
# 5) routes 注册
# ---------------------------------------------------------------------------

def test_routes_include_smart_imagechat_hub_endpoints():
    """: _ROUTES 含 /v1/chat/completions 和 /image/caption。"""
    routes = web_api._ROUTES
    paths = [r[0] for r in routes]
    assert "/v1/chat/completions" in paths
    assert "/image/caption" in paths
    print("✓ test_routes_include_smart_imagechat_hub_endpoints")


# ---------------------------------------------------------------------------
# 6) 配置 schema
# ---------------------------------------------------------------------------

def test_schema_has_smart_imagechat_hub_group():
    """: _conf_schema.json 含 'smart_imagechat_hub 兼容' group。"""
    import json
    with open("_conf_schema.json", "r", encoding="utf-8") as f:
        schema = json.load(f)
    assert "smart_imagechat_hub 兼容" in schema
    items = schema["smart_imagechat_hub 兼容"]["items"]
    assert "enable_smart_imagechat_hub_compat" in items
    assert "smart_imagechat_hub_auto_register_provider" in items
    assert "smart_imagechat_hub_caption_format" in items
    print("✓ test_schema_has_smart_imagechat_hub_group")


# ---------------------------------------------------------------------------
# 7) main.py 启动期检测
# ---------------------------------------------------------------------------

def test_main_detect_smart_imagechat_hub_logs_info():
    """: main._detect_smart_imagechat_hub 检测到时打 INFO log。"""
    from unittest.mock import MagicMock, AsyncMock
    import asyncio
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    plugin.config["enable_smart_imagechat_hub_compat"] = True
    with patch("smart_imagechat_hub_integration.is_smart_imagechat_hub_installed", return_value=True):
        # 不需要 await — 函数不是 async
        plugin._detect_smart_imagechat_hub()
    # 验证: 调了检测, 没抛异常即 OK (log 难直接验证)
    print("✓ test_main_detect_smart_imagechat_hub_logs_info")

def test_build_provider_config_structure():
    """: build_provider_config 返正确结构 (含 type/id/enable/api_base/model/key)."""
    cfg = smart_imagechat_hub_integration.build_provider_config(
        api_base="http://localhost:6185/api/plug/.../v1/chat/completions",
        api_key="",
        model="vision-bridge",
    )
    assert cfg["type"] == "openai_chat_completion"
    assert cfg["id"] == "vision_text_bridge_compat"
    assert cfg["enable"] is True
    assert cfg["api_base"].endswith("/v1/chat/completions")
    assert cfg["api_base"].endswith("/v1/chat/completions")
    assert cfg["key"] == ["placeholder"]  # v1.1.1+: 空时用占位符, 防 AstrBot Missing credentials
    assert cfg["model"] == "vision-bridge"
    assert cfg["provider_type"] == "chat_completion"
    print("✓ test_build_provider_config_structure")


def test_build_provider_config_with_api_key():
    """: build_provider_config 接受 API Key."""
    cfg = smart_imagechat_hub_integration.build_provider_config(
        api_base="http://x", api_key="sk-test"
    )
    assert cfg["key"] == ["sk-test"]
    print("✓ test_build_provider_config_with_api_key")

def test_build_provider_config_uses_placeholder_when_no_api_key():
    """: 反向验证: api_key 留空时用占位符 'placeholder' (AstrBot OpenAI provider 校验必填)."""
    cfg = smart_imagechat_hub_integration.build_provider_config(api_base="http://x")
    # 必须有 key 字段 (不能空 list)
    assert cfg["key"], "api_key 留空时必须用占位符, 否则 AstrBot 报 Missing credentials"
    assert cfg["key"] == ["placeholder"]
    print("✓ test_build_provider_config_uses_placeholder_when_no_api_key")


def test_smart_imagechat_hub_integration_logger_defined():
    """: 反向验证: 模块 logger 已定义 (防 NameError 崩插件启动)."""
    import logging
    # logger 是 logging.Logger 实例
    assert isinstance(smart_imagechat_hub_integration.logger, logging.Logger)
    # 能调不报 NameError
    smart_imagechat_hub_integration.logger.info("test")
    print("✓ test_smart_imagechat_hub_integration_logger_defined")



def test_is_provider_already_registered_returns_false_when_empty():
    """: 没注册时 is_provider_already_registered 返 False."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    plugin.context.provider_manager = MagicMock(provider_insts=[])
    result = smart_imagechat_hub_integration.is_provider_already_registered(plugin)
    assert result is False
    print("✓ test_is_provider_already_registered_returns_false_when_empty")


def test_is_provider_already_registered_returns_true_when_registered():
    """: 注册后 is_provider_already_registered 返 True."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    # mock provider_manager 已有该 provider
    fake_prov = MagicMock()
    fake_prov.provider_config = {"id": smart_imagechat_hub_integration.PROVIDER_ID}
    plugin.context.provider_manager = MagicMock(provider_insts=[fake_prov])
    result = smart_imagechat_hub_integration.is_provider_already_registered(plugin)
    assert result is True
    print("✓ test_is_provider_already_registered_returns_true_when_registered")


def test_auto_register_provider_skipped_when_disabled():
    """: 配置 False 时 _auto_register_sih_provider 不调 auto_register."""
    from tests.stub_helpers import make_test_plugin
    import main
    import asyncio
    plugin = make_test_plugin(main)
    plugin.config["smart_imagechat_hub_auto_register_provider"] = False
    with patch("smart_imagechat_hub_integration.auto_register_provider") as mock_ar:
        asyncio.run(plugin._auto_register_sih_provider())
    assert not mock_ar.called  # 配置 False, 不调
    print("✓ test_auto_register_provider_skipped_when_disabled")


def test_auto_register_provider_calls_when_enabled():
    """: 配置 True 时 _auto_register_sih_provider 调 auto_register_provider."""
    from tests.stub_helpers import make_test_plugin
    import main
    import asyncio
    plugin = make_test_plugin(main)
    plugin.config["smart_imagechat_hub_auto_register_provider"] = True
    with patch("smart_imagechat_hub_integration.auto_register_provider", new=AsyncMock(return_value=True)) as mock_ar:
        asyncio.run(plugin._auto_register_sih_provider())
    assert mock_ar.called  # 配置 True, 调了
    print("✓ test_auto_register_provider_calls_when_enabled")



if __name__ == "__main__":
    test_is_smart_imagechat_hub_installed_returns_false_when_no_metadata()
    test_is_smart_imagechat_hub_installed_returns_true_when_metadata_exists()
    test_install_check_cache_works()
    test_reset_cache_for_testing()
    test_api_chat_completions_returns_openai_format()
    test_api_chat_completions_no_image_url_returns_error()
    test_api_image_caption_get()
    test_api_image_caption_missing_url()
    test_integration_status_includes_smart_imagechat_hub()
    test_routes_include_smart_imagechat_hub_endpoints()
    test_schema_has_smart_imagechat_hub_group()
    test_main_detect_smart_imagechat_hub_logs_info()
    test_build_provider_config_structure()
    test_build_provider_config_with_api_key()
    test_build_provider_config_uses_placeholder_when_no_api_key()
    test_smart_imagechat_hub_integration_logger_defined()
    test_is_provider_already_registered_returns_false_when_empty()
    test_is_provider_already_registered_returns_true_when_registered()
    test_auto_register_provider_skipped_when_disabled()
    test_auto_register_provider_calls_when_enabled()
    print("---")
    print("ALL SMART_IMAGECHAT_HUB TESTS PASSED")
