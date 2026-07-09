"""test_vision_bridge_provider.py - 自定义 LLM provider 验证。"""
import os
import sys
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs  # noqa: E402

install_stubs()

import httpx  # noqa: E402

import vision_bridge_provider  # noqa: E402
import smart_imagechat_hub_integration  # noqa: E402


# ---------------------------------------------------------------------------
# 1) VisionBridgeProvider 类基础
# ---------------------------------------------------------------------------

def test_provider_class_exists_with_required_methods():
    """: VisionBridgeProvider 存在, 必备方法都有."""
    assert hasattr(vision_bridge_provider, "VisionBridgeProvider")
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x", "key": ["placeholder"], "model": "vision-bridge"},
    )
    assert callable(inst.text_chat)
    assert callable(inst.text_chat_stream)
    assert callable(inst.get_models)
    assert callable(inst.set_model)
    assert callable(inst.terminate)
    assert callable(inst.get_current_key)
    assert callable(inst.set_key)
    print("✓ test_provider_class_exists_with_required_methods")


def test_provider_init_reads_config():
    """: __init__ 读 api_base / api_key / model 正确."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={
            "api_base": "http://localhost:6185/.../v1/chat/completions/",
            "key": ["sk-test"],
            "model": "vision-bridge",
        },
    )
    # api_base 去掉末尾 /
    assert inst.api_base == "http://localhost:6185/.../v1/chat/completions"
    assert inst.api_key == "sk-test"
    assert inst.model == "vision-bridge"
    assert inst.current_model == "vision-bridge"
    print("✓ test_provider_init_reads_config")


def test_provider_init_placeholder_when_no_key():
    """: 没 key 时用 placeholder 占位."""
    inst = vision_bridge_provider.VisionBridgeProvider(provider_config={})
    assert inst.api_key == "placeholder"
    assert inst.api_base == ""
    assert inst.model == "vision-bridge"
    print("✓ test_provider_init_placeholder_when_no_key")


def test_provider_get_models_returns_vision_bridge():
    """: get_models 返 ['vision-bridge']."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x"},
    )
    models = asyncio.run(inst.get_models())
    assert models == ["vision-bridge"]
    print("✓ test_provider_get_models_returns_vision_bridge")


def test_provider_set_model_updates_current():
    """: set_model 更新 _current_model."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x", "model": "vision-bridge"},
    )
    inst.set_model("new-model")
    assert inst.current_model == "new-model"
    print("✓ test_provider_set_model_updates_current")


# ---------------------------------------------------------------------------
# 2) text_chat 调本插件 endpoint
# ---------------------------------------------------------------------------

def test_text_chat_posts_to_endpoint_with_image_urls():
    """: text_chat 构造 OpenAI ChatCompletion body 含 image_url + 调我方 endpoint."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={
            "api_base": "http://example.com/v1/chat/completions",
            "key": ["placeholder"],
            "model": "vision-bridge",
        },
    )

    # mock httpx.AsyncClient.post
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "choices": [{
            "message": {"role": "assistant", "content": "一只橘猫在窗台上"},
            "finish_reason": "stop",
        }],
    })
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = asyncio.run(inst.text_chat(
            prompt="请描述图片",
            image_urls=["http://x.com/cat.jpg"],
            system_prompt="你是助手",
        ))

    # 验证 post 被调, body 含 image_url
    assert mock_client.post.called
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://example.com/v1/chat/completions"
    body = call_args[1]["json"]
    assert body["model"] == "vision-bridge"
    # messages: system + user (with image + text)
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "你是助手"
    user_msg = body["messages"][1]
    assert user_msg["role"] == "user"
    # user content 是 list 含 image_url + text
    assert isinstance(user_msg["content"], list)
    assert any(c.get("type") == "image_url" for c in user_msg["content"])
    assert any(c.get("type") == "text" for c in user_msg["content"])

    # 验证 result completion_text
    assert result.completion_text == "一只橘猫在窗台上"
    assert result.role == "assistant"
    print("✓ test_text_chat_posts_to_endpoint_with_image_urls")


def test_text_chat_handles_500_error():
    """: text_chat 接非 200 响应, 返错误文本不崩."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x", "key": ["placeholder"]},
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "internal error"
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = asyncio.run(inst.text_chat(prompt="hi", image_urls=["http://x.com/i.jpg"]))
    assert "error" in result.completion_text.lower()
    print("✓ test_text_chat_handles_500_error")


def test_text_chat_handles_http_exception():
    """: text_chat 接网络异常, 返错误文本不崩."""
    inst = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x", "key": ["placeholder"]},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("conn refused"))

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        result = asyncio.run(inst.text_chat(prompt="hi", image_urls=["http://x.com/i.jpg"]))
    assert "error" in result.completion_text.lower() or "vision_text_bridge" in result.completion_text
    print("✓ test_text_chat_handles_http_exception")


# ---------------------------------------------------------------------------
# 3) auto_register_provider 走新路径 (不调 load_provider)
# ---------------------------------------------------------------------------

def test_auto_register_does_not_call_load_provider():
    """: 反向验证: auto_register_provider 走自定义 class 路径, 不调 load_provider (避免 openai SDK)."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)

    # mock provider_manager
    fake_pm = MagicMock()
    fake_pm.provider_insts = []
    fake_pm.providers = {}
    plugin.context.provider_manager = fake_pm
    fake_ac = MagicMock()
    fake_ac.config = {"dashboard": {"host": "127.0.0.1", "port": 6185}}
    plugin.context.astr_context = fake_ac

    # 不需要 npm, 不需要真实 endpoint
    asyncio.run(smart_imagechat_hub_integration.auto_register_provider(plugin))

    # 验证 load_provider 没被调
    assert not fake_pm.load_provider.called, "auto_register_provider 不应调 load_provider"

    # 验证 provider_insts 加了 1 个
    assert len(fake_pm.provider_insts) == 1
    inst = fake_pm.provider_insts[0]
    assert isinstance(inst, vision_bridge_provider.VisionBridgeProvider)
    # 验证 providers dict 也加
    assert fake_pm.providers.get("vision_text_bridge_compat") is inst
    # 验证 api_base 正确 (去掉末尾 /)
    assert inst.api_base == "http://127.0.0.1:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions"
    assert inst.model == "vision-bridge"
    # 验证 api_key 用占位
    assert inst.api_key == "placeholder"
    print("✓ test_auto_register_does_not_call_load_provider")


def test_auto_register_skipped_when_already_registered():
    """: 已注册时跳过 (idempotent)."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)

    # 模拟已注册
    existing = vision_bridge_provider.VisionBridgeProvider(
        provider_config={"api_base": "http://x"},
    )
    fake_pm = MagicMock()
    fake_pm.provider_insts = [existing]
    fake_pm.providers = {"vision_text_bridge_compat": existing}
    plugin.context.provider_manager = fake_pm

    result = asyncio.run(smart_imagechat_hub_integration.auto_register_provider(plugin))
    assert result is True
    # 没新加
    assert len(fake_pm.provider_insts) == 1
    print("✓ test_auto_register_skipped_when_already_registered")

# ---------------------------------------------------------------------------
# dashboard_port 优先级 + api_base 默认 URL
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 新版 schema 默认值 + 共享常量
# ---------------------------------------------------------------------------

def test_dashboard_port_uses_schema_value():
    """: schema dashboard_port=8888 → 用 8888."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    fake_pm = MagicMock()
    fake_pm.provider_insts = []
    fake_pm.providers = {}
    fake_pm.provider_class_map = {}
    plugin.context.provider_manager = fake_pm
    fake_ac = MagicMock()
    fake_ac.config = {"dashboard": {"host": "1.2.3.4", "port": 1234}}
    plugin.context.astr_context = fake_ac
    plugin.config["dashboard_port"] = 8888

    asyncio.run(smart_imagechat_hub_integration.auto_register_provider(plugin))

    inst = fake_pm.provider_insts[-1]
    assert "8888" in inst.api_base
    print("✓ test_dashboard_port_uses_schema_value")


def test_schema_no_api_base_field():
    """: schema 移除自定义 api_base 字段 (只保留 dashboard_port + auto 推断 URL)."""
    import json
    schema = json.loads(open("_conf_schema.json").read())
    items = schema["OpenAI 兼容 provider"]["items"]
    assert "api_base" not in items, "api_base 字段应已移除"
    for required in ["dashboard_port", "enabled", "auto_register", "api_key", "model_name", "caption_format"]:
        assert required in items
    print("✓ test_schema_no_api_base_field")


def test_schema_keys_simplified():
    """: schema key 精简 + hint 短."""
    import json
    schema = json.loads(open("_conf_schema.json").read())
    items = schema["OpenAI 兼容 provider"]["items"]
    for key in items.keys():
        assert not key.startswith("openai_compat_"), f"key {key!r} 仍有 openai_compat_ 前缀"
    # hint 短
    for key, val in items.items():
        if "hint" in val and len(val["hint"]) > 60:
            assert len(val["hint"]) <= 60, f"hint 仍长: {key!r}: {val['hint']!r}"
    print("✓ test_schema_keys_simplified")


def test_shared_constants_match_main():
    """: 共享常量与 main.py 一致."""
    import main as main_mod
    assert smart_imagechat_hub_integration.PLUGIN_ROUTE_PREFIX == main_mod.PLUGIN_ROUTE_PREFIX
    assert smart_imagechat_hub_integration.OPENAI_COMPAT_PATH == main_mod.OPENAI_COMPAT_PATH
    assert smart_imagechat_hub_integration.DEFAULT_DASHBOARD_PORT == main_mod.DEFAULT_DASHBOARD_PORT
    print("✓ test_shared_constants_match_main")


def test_inject_provider_class_map():
    """: auto_register 注入 custom type class 到 provider_manager.provider_class_map."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    fake_pm = MagicMock()
    fake_pm.provider_insts = []
    fake_pm.providers = {}
    fake_pm.provider_class_map = {}  # framework 给的空 map
    plugin.context.provider_manager = fake_pm
    fake_ac = MagicMock()
    fake_ac.config = {"dashboard": {"host": "127.0.0.1", "port": 6185}}
    plugin.context.astr_context = fake_ac

    asyncio.run(smart_imagechat_hub_integration.auto_register_provider(plugin))

    # : provider_class_map["vision_bridge_compat"] = VisionBridgeProvider
    assert "vision_bridge_compat" in fake_pm.provider_class_map
    assert fake_pm.provider_class_map["vision_bridge_compat"] is vision_bridge_provider.VisionBridgeProvider
    print("✓ test_inject_provider_class_map")


def test_override_broken_framework_instance():
    """: framework 残留 broken instance (None 或非 VisionBridgeProvider) → 我方覆盖."""
    from tests.stub_helpers import make_test_plugin
    import main
    plugin = make_test_plugin(main)
    # 模拟 AstrBot 启动时 framework instantiate 失败后留下的 None
    fake_pm = MagicMock()
    fake_pm.provider_insts = [None, None]  # 2 个 None 位置
    fake_pm.providers = {"vision_text_bridge_compat": None}  # None value
    fake_pm.provider_class_map = {}
    plugin.context.provider_manager = fake_pm
    fake_ac = MagicMock()
    fake_ac.config = {"dashboard": {"host": "127.0.0.1", "port": 6185}}
    plugin.context.astr_context = fake_ac

    asyncio.run(smart_imagechat_hub_integration.auto_register_provider(plugin))

    # : None 被替换为 VisionBridgeProvider instance
    inst = fake_pm.providers["vision_text_bridge_compat"]
    assert isinstance(inst, vision_bridge_provider.VisionBridgeProvider)
    # provider_insts 第一个 None 被替换
    assert isinstance(fake_pm.provider_insts[0], vision_bridge_provider.VisionBridgeProvider)
    print("✓ test_override_broken_framework_instance")


if __name__ == "__main__":
    test_provider_class_exists_with_required_methods()
    test_provider_init_reads_config()
    test_provider_init_placeholder_when_no_key()
    test_provider_get_models_returns_vision_bridge()
    test_provider_set_model_updates_current()
    test_text_chat_posts_to_endpoint_with_image_urls()
    test_text_chat_handles_500_error()
    test_text_chat_handles_http_exception()
    test_auto_register_does_not_call_load_provider()
    test_auto_register_skipped_when_already_registered()
    test_dashboard_port_uses_schema_value()
    test_schema_no_api_base_field()
    test_schema_keys_simplified()
    test_shared_constants_match_main()
    test_inject_provider_class_map()
    test_override_broken_framework_instance()
    print("---")
    print("ALL VISION_BRIDGE_PROVIDER TESTS PASSED")
