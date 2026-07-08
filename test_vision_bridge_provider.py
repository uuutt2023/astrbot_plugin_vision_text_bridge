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
    print("---")
    print("ALL VISION_BRIDGE_PROVIDER TESTS PASSED")
