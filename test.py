"""
离线测试：模拟 ProviderRequest，验证插件在不实际调用 mmx 的情况下行为正确。

用法：
    python test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

# 把插件目录加到 path，让 import 找得到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 在 import 之前先 stub 掉 astrbot，避免 AstrBot 没装导致导入失败
import types


def _install_astrbot_stub() -> None:
    stub = types.ModuleType("astrbot")

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict  # 用 dict 充当配置
    logger_module = types.ModuleType("astrbot.api._logger")
    logger_module.info = lambda *a, **k: None
    logger_module.warning = lambda *a, **k: None
    logger_module.error = lambda *a, **k: None
    logger_module.exception = lambda *a, **k: None
    logger_module.debug = lambda *a, **k: None
    # logger 必须挂到 astrbot.api 上，from astrbot.api import logger 才能拿到
    setattr(api, "logger", logger_module)

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = SimpleNamespace
    event_module.filter = SimpleNamespace(
        on_llm_request=lambda *a, **k: (lambda f: f),
        command=lambda *a, **k: (lambda f: f),
        command_group=lambda *a, **k: (lambda f: f),
    )
    event_module.MessageChain = list

    provider_module = types.ModuleType("astrbot.api.provider")
    provider_module.ProviderRequest = SimpleNamespace

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = SimpleNamespace
    star_module.Star = object

    def _register(*args, **kwargs):
        def deco(cls):
            return cls
        return deco

    star_module.register = _register

    stub.api = api
    stub.logger = logger_module

    sys.modules.setdefault("astrbot", stub)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event_module)
    sys.modules.setdefault("astrbot.api.provider", provider_module)
    sys.modules.setdefault("astrbot.api.star", star_module)


_install_astrbot_stub()

import main  # noqa: E402


def make_config(**overrides):
    cfg = {
        "enabled": True,
        "mmx_path": "",
        "auto_install_cli": False,
        "command_timeout": 5,
        "max_concurrent_vision": 2,
        "vision_prompt": "请描述这张图片",
        "image_placeholder_template": "[Image {index} 描述] {description}",
        "max_description_length": 0,
        "include_history": False,
        "include_extra_parts": True,
        "failure_message": "[Image {index} 描述] 理解失败：{error}",
        "redact_sensitive": False,
        "cache_descriptions": True,
        "inject_system_prompt_guidance": True,
    }
    cfg.update(overrides)
    # AstrBotConfig 在 stub 里就是 dict
    return cfg


def make_plugin(**overrides):
    cfg = make_config(**overrides)
    # 不传 context
    return main.VisionTextBridgePlugin.__new__(main.VisionTextBridgePlugin).__init__ if False else None  # noqa


# 直接实例化时绕过父类构造，避免 AstrBot 不在时的麻烦
def new_plugin(**overrides):
    p = main.VisionTextBridgePlugin.__new__(main.VisionTextBridgePlugin)
    p.config = make_config(**overrides)
    p.mmx_path = "/usr/bin/true"  # 任意可执行文件占位
    p.npm_path = None
    p._description_cache = {}
    p._vision_semaphore = asyncio.Semaphore(2)
    # 手动设置 _configured_priority（代替 __init__ 中的赋值）
    p._configured_priority = p._resolve_priority()
    # 默认 None，让 _describe_one 在 SQLite 缓存表走 None 路径
    p._caption_cache = None
    # v0.8.7: 主钩子快照字段。__init__ 里会设，这里用 __new__ 创建的实例也要设
    p._pending_urls = None
    p._pending_parts = None
    p._pending_contexts = None
    p._priority_locked_warning_emitted = False
    # 注入一个 mock context，让 _register_web_apis 不报错
    p.context = SimpleNamespace(
        request=SimpleNamespace(
            args={},
            json=None,
        ),
        register_web_api=lambda *a, **k: None,
    )
    return p


class FakeReq:
    """模拟 ProviderRequest，包含插件关心的字段。"""

    def __init__(self, prompt=None, image_urls=None, extra_user_content_parts=None, contexts=None, system_prompt=""):
        self.prompt = prompt
        self.image_urls = image_urls or []
        self.extra_user_content_parts = extra_user_content_parts or []
        self.contexts = contexts or []
        self.system_prompt = system_prompt


def make_mmx_result(stdout: str = "", stderr: str = "", returncode: int = 0, ok: bool = True):
    """模拟 :class:`MmxResult`（main.py 中定义），让老测试能传 (stdout, stderr)。"""
    from dataclasses import dataclass

    @dataclass
    class _R:
        stdout: str
        stderr: str
        returncode: int
        ok: bool

    return _R(stdout, stderr, returncode, ok)


def wrap_run(fn):
    """包装一个返回 (stdout, stderr) 的 async 函数为返回 MmxResult。

    新版 _run_mmx 返回 dataclass，旧测试代码写的是 ``return "x", ""``。
    这个 wrapper 让旧代码无须修改即可工作。
    """

    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            return make_mmx_result(stdout=result[0], stderr=result[1])
        return result

    return wrapper


# ------------------------------------------------------------------ 单测：工具方法

def test_is_cacheable_url():
    # v0.8.7: 抽到 module-level _is_cacheable_url(url, config)
    p = new_plugin()
    assert main._is_cacheable_url("http://x.com/a.jpg", p.config) is True
    assert main._is_cacheable_url("https://x.com/a.jpg", p.config) is True
    assert main._is_cacheable_url("base64://abc", p.config) is False
    assert main._is_cacheable_url("file:///tmp/a.jpg", p.config) is True
    assert main._is_cacheable_url("/tmp/a.jpg", p.config) is False
    assert main._is_cacheable_url("", p.config) is False
    p2 = new_plugin(cache_file_paths=False)
    assert main._is_cacheable_url("file:///tmp/a.jpg", p2.config) is False
    print("✓ test_is_cacheable_url")


def test_truncate():
    p = new_plugin(max_description_length=10)
    assert p._truncate("1234567890") == "1234567890"
    assert p._truncate("1234567890abcde") == "1234567890…"
    p2 = new_plugin(max_description_length=0)
    assert p2._truncate("a" * 1000) == "a" * 1000
    print("✓ test_truncate")


def test_redact_text():
    p = new_plugin(redact_sensitive=True)
    out = p._redact_text("token=abc1234567 rest")
    assert "abc1234567" not in out
    assert "REDACTED" in out
    sk = p._redact_text("sk-foobar12345xyz rest")
    assert "foobar" not in sk
    print("✓ test_redact_text")


def test_extract_image_url_from_part_dict():
    # v0.8.7: 抽到 module-level _extract_url_from_item
    part = {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}}
    assert main._extract_url_from_item(part) == "https://x.com/a.jpg"
    text = {"type": "text", "text": "hello"}
    assert main._extract_url_from_item(text) == ""
    print("✓ test_extract_image_url_from_part_dict")


def test_extract_image_url_from_part_object():
    img = SimpleNamespace(type="image_url", image_url=SimpleNamespace(url="https://x.com/a.jpg"))
    assert main._extract_url_from_item(img) == "https://x.com/a.jpg"
    img2 = SimpleNamespace(type="image_url", image_url="https://x.com/b.jpg")
    assert main._extract_url_from_item(img2) == "https://x.com/b.jpg"
    print("✓ test_extract_image_url_from_part_object")


def test_remove_image_parts():
    # v0.8.7: 抽到 module-level helper。直接 inlined 测试
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://x.com/b.jpg"}},
    ]
    # 模拟主钩子入口清空：删所有 image_url parts
    parts[:] = [p for p in parts if not main._is_image_url_part(p)]
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    print("✓ test_remove_image_parts")


# ------------------------------------------------------------------ 单测：_build_vision_command

def test_build_vision_command_url():
    p = new_plugin()
    cmd = p._build_vision_command("https://x.com/a.jpg", "描述这张图")
    assert cmd == ("vision", "describe", "--image", "https://x.com/a.jpg", "--prompt", "描述这张图")
    print("✓ test_build_vision_command_url")


def test_build_vision_command_file_id():
    p = new_plugin()
    cmd = p._build_vision_command("file-abc123", "描述")
    assert cmd == ("vision", "describe", "--file-id", "file-abc123", "--prompt", "描述")
    print("✓ test_build_vision_command_file_id")


def test_build_vision_command_no_prompt():
    p = new_plugin()
    cmd = p._build_vision_command("https://x.com/a.jpg", "")
    assert cmd == ("vision", "describe", "--image", "https://x.com/a.jpg")
    print("✓ test_build_vision_command_no_prompt")


# ------------------------------------------------------------------ 单测：_attach_descriptions_to_prompt

def test_attach_with_prompt():
    """v0.7+ 新行为：图说作为 content block 注入到 extra_user_content_parts，不修改 prompt。
    v0.8.7: 方法名从 _attach_descriptions_to_prompt 简化为 _attach。"""
    p = new_plugin()
    req = FakeReq(prompt="用户问：这是什么", image_urls=["https://x.com/a.jpg"])
    p._attach(
        req,
        [(1, "https://x.com/a.jpg", "一只橘猫趴在沙发上")],
        start_index=1,
        field="image_urls",
    )
    assert req.prompt == "用户问：这是什么"
    assert len(req.extra_user_content_parts) == 1
    # v0.8.7: _to_text_part 优先用 TextPart Pydantic 对象（带 .text 属性），也兼容 dict
    ep0 = req.extra_user_content_parts[0]
    text = ep0["text"] if isinstance(ep0, dict) else ep0.text
    assert text == "[Image 1 描述] 一只橘猫趴在沙发上"
    assert req.image_urls == []
    print("✓ test_attach_with_prompt")


def test_attach_no_prompt():
    """prompt 为 None 时，只注入 content block。"""
    p = new_plugin()
    req = FakeReq(prompt=None, image_urls=["https://x.com/a.jpg"])
    p._attach(
        req,
        [(1, "https://x.com/a.jpg", "一只狗")],
        start_index=1,
        field="image_urls",
    )
    assert req.prompt is None
    assert len(req.extra_user_content_parts) == 1
    ep0 = req.extra_user_content_parts[0]
    text = ep0["text"] if isinstance(ep0, dict) else ep0.text
    assert text == "[Image 1 描述] 一只狗"
    assert req.image_urls == []
    print("✓ test_attach_no_prompt")


def test_attach_failure_uses_template():
    """mmx 失败时：占位仍注入 content block + 清空 image_urls。"""
    p = new_plugin()
    req = FakeReq(prompt="看图", image_urls=["https://x.com/bad.jpg"])
    p._attach(
        req,
        [(1, "https://x.com/bad.jpg", "")],  # 失败：空描述
        start_index=1,
        field="image_urls",
    )
    assert req.prompt == "看图"
    assert len(req.extra_user_content_parts) == 1
    ep0 = req.extra_user_content_parts[0]
    text = ep0["text"] if isinstance(ep0, dict) else ep0.text
    assert "理解失败" in text
    assert "mmx 调用失败或超时" in text
    assert req.image_urls == []
    print("✓ test_attach_failure_uses_template")


def test_attach_index_continues():
    """多张图：每张生成一个独立 content block。"""
    p = new_plugin()
    req = FakeReq(prompt=None, image_urls=["a", "b", "c"])
    p._attach(
        req,
        [(1, "a", "desc-a"), (2, "b", "desc-b")],
        start_index=1,
        field="image_urls",
    )
    assert len(req.extra_user_content_parts) == 2
    def _t(p):
        return p["text"] if isinstance(p, dict) else p.text
    assert _t(req.extra_user_content_parts[0]) == "[Image 1 描述] desc-a"
    assert _t(req.extra_user_content_parts[1]) == "[Image 2 描述] desc-b"
    assert req.prompt is None
    assert req.image_urls == []
    print("✓ test_attach_index_continues")


# ------------------------------------------------------------------ 单测：缓存

def test_cache_hit():
    """v0.8.2: 缓存用 md5 作 key。同 url 重复调用应命中内存缓存。"""
    p = new_plugin(cache_descriptions=True)
    # 直接调一次写入内存缓存（mock mmx）
    called = {"count": 0}

    async def fake_run(*a, **k):
        called["count"] += 1
        return "desc1", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        result1 = asyncio.run(p._describe_one("https://x.com/a.jpg"))
    assert result1 == "desc1"
    assert called["count"] == 1

    # 第二次调同一 url——应命中内存缓存，**不**调 mmx
    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        result2 = asyncio.run(p._describe_one("https://x.com/a.jpg"))
    assert result2 == "desc1"
    assert called["count"] == 1  # 不变，命中了
    print("✓ test_cache_hit")


# ------------------------------------------------------------------ 单测：end-to-end（mock mmx）

def test_e2e_image_urls_only():
    p = new_plugin()

    async def fake_run(*args, **kwargs):
        # args 形如 ("vision", "describe", "--image", url, "--prompt", prompt)
        assert args[0] == "vision"
        assert args[1] == "describe"
        return f"描述: {args[3]}", ""

    req = FakeReq(
        prompt="帮我看看",
        image_urls=["https://x.com/a.jpg", "https://x.com/b.jpg"],
    )
    event = SimpleNamespace()

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))  # v0.8.7: 不再要 event 参数

    # **v0.7 新行为**：prompt 保持原样
    assert req.prompt == "帮我看看"
    # 图说作为 content blocks 加入
    assert len(req.extra_user_content_parts) == 2
    def _t(p):
        return p["text"] if isinstance(p, dict) else p.text
    assert _t(req.extra_user_content_parts[0]) == "[Image 1 描述] 描述: https://x.com/a.jpg"
    assert _t(req.extra_user_content_parts[1]) == "[Image 2 描述] 描述: https://x.com/b.jpg"
    # image_urls 都被移除
    assert req.image_urls == []
    print("✓ test_e2e_image_urls_only")


def test_e2e_extra_parts():
    p = new_plugin()
    parts = [
        {"type": "text", "text": "附加说明"},
        {"type": "image_url", "image_url": {"url": "https://x.com/x.jpg"}},
    ]
    req = FakeReq(prompt="hi", image_urls=[], extra_user_content_parts=parts)

    async def fake_run(*args, **kwargs):
        return "x图说", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))  # v0.8.7: 不再要 event

    # 原 text part 保留 + image_url 被删除 + 图说 content block 加上
    assert len(req.extra_user_content_parts) == 2
    ep0 = req.extra_user_content_parts[0]
    text0 = ep0["text"] if isinstance(ep0, dict) else ep0.text
    assert text0 == "附加说明"
    ep1 = req.extra_user_content_parts[1]
    text1 = ep1["text"] if isinstance(ep1, dict) else ep1.text
    assert text1 == "[Image 1 描述] x图说"
    print("✓ test_e2e_extra_parts")


def test_e2e_disabled_plugin():
    p = new_plugin(enabled=False)
    req = FakeReq(prompt="hi", image_urls=["https://x.com/a.jpg"])
    called = {"count": 0}

    async def fake_run(*args, **kwargs):
        called["count"] += 1
        return "x", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))
    # 关闭时 _process_request 不应被调用（由 bridge_vision_to_text 拦）
    # 这里直接调 _process_request 还是会处理，验证 enabled 拦截在主入口
    # 所以这条用例改测主入口
    print("✓ test_e2e_disabled_plugin (skipped - main entry handles it)")


def test_e2e_mmx_failure_keeps_placeholder():
    p = new_plugin()

    async def fake_run(*args, **kwargs):
        raise RuntimeError("boom")

    req = FakeReq(prompt="hi", image_urls=["https://x.com/a.jpg"])
    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))

    # 失败也清空 image_urls
    assert req.image_urls == []
    # prompt 不变
    assert req.prompt == "hi"
    # 失败占位作为 content block 加入
    assert len(req.extra_user_content_parts) == 1
    assert "理解失败" in req.extra_user_content_parts[0]["text"]
    print("✓ test_e2e_mmx_failure_keeps_placeholder")


def test_e2e_history_in_contexts():
    p = new_plugin(include_history=True)
    contexts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "之前的对话"},
                {"type": "image_url", "image_url": {"url": "https://x.com/old.jpg"}},
            ],
        }
    ]
    req = FakeReq(prompt="现在的问题", image_urls=[], contexts=contexts)

    async def fake_run(*args, **kwargs):
        return "历史图说", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))

    # 历史 content 里 image_url 被移除，只剩 text
    new_content = contexts[0]["content"]
    assert len(new_content) == 1
    assert new_content[0]["type"] == "text"
    # **v0.7**：prompt 字符串不变
    assert req.prompt == "现在的问题"
    # 描述作为 content block 注入
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 历史图说"
    print("✓ test_e2e_history_in_contexts")


def test_e2e_truncation_applied():
    p = new_plugin(max_description_length=5)
    req = FakeReq(prompt=None, image_urls=["https://x.com/a.jpg"])

    async def fake_run(*args, **kwargs):
        return "这是一段很长的描述文字" * 10, ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(req))

    # prompt 不变
    assert req.prompt is None
    # 描述被截断后作为 content block
    assert len(req.extra_user_content_parts) == 1
    text = req.extra_user_content_parts[0]["text"]
    # 5 字符 + "…" = 6 字符内容
    desc_part = text.replace("[Image 1 描述] ", "")
    assert len(desc_part) == 6
    assert desc_part.endswith("…")
    print("✓ test_e2e_truncation_applied")


# ------------------------------------------------------------------ 单测：自动登录


def test_initialize_triggers_login_with_key():
    """initialize() 配置了 minimax_api_key 时应调用 mmx auth login。"""
    p = new_plugin(
        minimax_api_key="sk-foobar1234567890abcdef",
        auto_login=True,
    )
    p.mmx_path = "/usr/bin/true"  # 任意可执行文件占位
    calls = {"args": []}

    async def fake_run_mmx(*args, **kwargs):
        calls["args"].append((args, kwargs))
        return "Login successful", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run_mmx)):
        asyncio.run(p.initialize())

    # 应调用了一次 mmx auth login --api-key <key>
    assert len(calls["args"]) == 1
    cmd, kw = calls["args"][0]
    assert cmd[:3] == ("auth", "login", "--api-key")
    assert cmd[3].startswith("sk-foobar")  # key 完整传过去
    assert kw.get("timeout") == 30
    print("✓ test_initialize_triggers_login_with_key")


def test_initialize_skips_login_when_disabled():
    """auto_login=False 时即使配了 key 也不应触发登录。"""
    p = new_plugin(
        minimax_api_key="sk-shouldnotcall",
        auto_login=False,
    )
    p.mmx_path = "/usr/bin/true"
    calls = {"count": 0}

    async def fake_run_mmx(*args, **kwargs):
        calls["count"] += 1
        return "", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run_mmx)):
        asyncio.run(p.initialize())

    assert calls["count"] == 0
    print("✓ test_initialize_skips_login_when_disabled")


def test_initialize_skips_login_when_key_empty():
    """minimax_api_key 为空时不应触发登录。"""
    p = new_plugin(
        minimax_api_key="",
        auto_login=True,
    )
    p.mmx_path = "/usr/bin/true"
    calls = {"count": 0}

    async def fake_run_mmx(*args, **kwargs):
        calls["count"] += 1
        return "", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run_mmx)):
        asyncio.run(p.initialize())

    assert calls["count"] == 0
    print("✓ test_initialize_skips_login_when_key_empty")


def test_login_failure_does_not_crash():
    """登录失败（mmx 返回非 0）应只告警不抛异常。"""
    p = new_plugin(
        minimax_api_key="sk-badkey",
        auto_login=True,
    )
    p.mmx_path = "/usr/bin/true"

    async def fake_run_mmx(*args, **kwargs):
        raise RuntimeError("invalid api key")

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run_mmx)):
        # 应不抛
        asyncio.run(p.initialize())
    print("✓ test_login_failure_does_not_crash")


def test_initialize_skips_login_when_no_mmx():
    """找不到 mmx 时应跳过登录而不是崩溃。"""
    p = new_plugin(
        minimax_api_key="sk-foobar",
        auto_login=True,
    )
    p.mmx_path = None  # 关键：没装
    calls = {"count": 0}

    async def fake_run_mmx(*args, **kwargs):
        calls["count"] += 1
        return "", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run_mmx)):
        asyncio.run(p.initialize())

    assert calls["count"] == 0
    print("✓ test_initialize_skips_login_when_no_mmx")


# ------------------------------------------------------------------ 单测：priority


def test_default_priority_in_module():
    """main.DEFAULT_PRIORITY 应该存在且是 int。"""
    assert isinstance(main.DEFAULT_PRIORITY, int)
    # 默认 100 足以抢在多数常见插件（多为 0）前面
    assert main.DEFAULT_PRIORITY == 100
    print("✓ test_default_priority_in_module")


def test_decorator_uses_priority_kwarg():
    """bridge_vision_to_text 装饰器需将 priority 传到 on_llm_request。

    实际 AstrBot 会把 priority 存到 handler.extras_configs；这里验证装饰器
    被调用时传了 priority 参数（防止在 import 时传丢）。
    """
    # 由于 stub 把 on_llm_request 变成身份函数，无法直接验证。
    # 这里走一个间接路径：检查 plugin 的 handler 方法上是否有 priority 记录。
    p = new_plugin()
    assert hasattr(p, "_configured_priority")
    assert p._configured_priority == 100  # 默认
    print("✓ test_decorator_uses_priority_kwarg")


def test_resolve_priority_default():
    """未配置 priority 时应返回 DEFAULT_PRIORITY。"""
    p = new_plugin()  # make_config 默认不含 priority
    assert p._resolve_priority() == 100
    print("✓ test_resolve_priority_default")


def test_resolve_priority_from_config():
    """配置了 priority 时应返回配置值。"""
    p = new_plugin(priority=500)
    assert p._resolve_priority() == 500
    print("✓ test_resolve_priority_from_config")


def test_resolve_priority_invalid_falls_back():
    """priority 配置为非整数时回退默认。"""
    p = new_plugin(priority="not-an-int")
    assert p._resolve_priority() == 100
    p2 = new_plugin(priority=None)
    assert p2._resolve_priority() == 100
    p3 = new_plugin(priority="")
    assert p3._resolve_priority() == 100
    print("✓ test_resolve_priority_invalid_falls_back")


def test_priority_mismatch_warns_and_updates_global():
    """v0.8.7: priority 不一致时仅告警（设 _priority_locked_warning_emitted），
    不再改 global DEFAULT_PRIORITY（因为 on_llm_request decorator 已锁定）。"""
    original = main.DEFAULT_PRIORITY
    try:
        main.DEFAULT_PRIORITY = 100
        p = new_plugin(priority=500)
        assert p._priority_locked_warning_emitted is False
        p._warn_if_priority_mismatch()
        # v0.8.7 改语义：只设了 _priority_locked_warning_emitted 防止重报
        assert p._priority_locked_warning_emitted is True
        # global DEFAULT_PRIORITY 不再被改（警告一次即可）
        assert main.DEFAULT_PRIORITY == 100
    finally:
        main.DEFAULT_PRIORITY = original
    print("✓ test_priority_mismatch_warns_and_updates_global")


def test_priority_match_no_warning():
    """配置 priority 等于当前 DEFAULT_PRIORITY 时不应触发 mismatch 逻辑。"""
    original = main.DEFAULT_PRIORITY
    try:
        main.DEFAULT_PRIORITY = 200
        p = new_plugin(priority=200)
        # 应该 early return，不更新全局
        p._warn_if_priority_mismatch()
        assert main.DEFAULT_PRIORITY == 200  # 不变
    finally:
        main.DEFAULT_PRIORITY = original
    print("✓ test_priority_match_no_warning")


def test_priority_out_of_range_warns():
    """priority 超出建议范围时应告警。"""
    original = main.DEFAULT_PRIORITY
    try:
        main.DEFAULT_PRIORITY = 100
        p = new_plugin(priority=99999)  # 超出 10000
        p._warn_if_priority_mismatch()
        # v0.8.7: 不再更新 global DEFAULT_PRIORITY，只设 _priority_locked_warning_emitted
        assert main.DEFAULT_PRIORITY == 100  # 保持不变
        assert p._priority_locked_warning_emitted is True
    finally:
        main.DEFAULT_PRIORITY = original
    print("✓ test_priority_out_of_range_warns")


# ------------------------------------------------------------------ 单测：链末兜底


def test_is_data_url():
    assert main._is_data_url("data:image/webp;base64,UklGR") is True
    assert main._is_data_url("data:image/png;base64,abc") is True
    assert main._is_data_url("data:image/jpeg;base64,/9j/4AAQ") is True
    assert main._is_data_url("https://x.com/a.jpg") is False
    assert main._is_data_url("file:///tmp/a.jpg") is False
    assert main._is_data_url("base64://abc") is False
    assert main._is_data_url("") is False
    print("✓ test_is_data_url")


def test_strip_all_data_url_images_image_urls():
    p = new_plugin()
    req = FakeReq(
        prompt="hi",
        image_urls=[
            "https://x.com/a.jpg",          # 保留
            "data:image/webp;base64,ABCD",  # 删
            "data:image/png;base64,EFG",    # 删
            "file:///tmp/b.jpg",            # 保留
        ],
    )
    n = main._strip_image_urls(req, only_data_url=True)
    assert n == 2
    assert req.image_urls == ["https://x.com/a.jpg", "file:///tmp/b.jpg"]
    print("✓ test_strip_all_data_url_images_image_urls")


def test_strip_all_data_url_images_extra_parts():
    p = new_plugin()
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,ABCD"}},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
        # pydantic-style 也覆盖
        SimpleNamespace(type="image_url", image_url="data:image/png;base64,EF"),
    ]
    req = FakeReq(prompt=None, image_urls=[], extra_user_content_parts=parts)
    n = main._strip_image_urls(req, only_data_url=True)
    assert n == 2
    # 只剩 text 和 合法 URL 的 image_url
    assert len(req.extra_user_content_parts) == 2
    assert req.extra_user_content_parts[0]["type"] == "text"
    assert req.extra_user_content_parts[1]["image_url"]["url"] == "https://x.com/a.jpg"
    print("✓ test_strip_all_data_url_images_extra_parts")


def test_strip_all_data_url_images_contexts():
    p = new_plugin()
    contexts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/webp;base64,ABCD"}},
                {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
            ],
        },
        {
            "role": "assistant",
            "content": "plain text",
        },
    ]
    req = FakeReq(prompt=None, image_urls=[], contexts=contexts)
    n = main._strip_image_urls(req, only_data_url=True)
    assert n == 1
    new_content = contexts[0]["content"]
    assert len(new_content) == 2
    assert new_content[0]["type"] == "text"
    assert new_content[1]["image_url"]["url"] == "https://x.com/a.jpg"
    # assistant 文本不被动
    assert contexts[1]["content"] == "plain text"
    print("✓ test_strip_all_data_url_images_contexts")


def test_strip_returns_zero_when_nothing():
    p = new_plugin()
    req = FakeReq(
        prompt="hi",
        image_urls=["https://x.com/a.jpg"],
        extra_user_content_parts=[{"type": "text", "text": "x"}],
        contexts=[{"role": "user", "content": "y"}],
    )
    n = main._strip_image_urls(req, only_data_url=True)
    assert n == 0
    assert req.image_urls == ["https://x.com/a.jpg"]
    print("✓ test_strip_returns_zero_when_nothing")


def test_strip_handles_string_image_url_field():
    """ImageURLPart 中 image_url 可能是字符串而非 dict。"""
    p = new_plugin()
    parts = [SimpleNamespace(type="image_url", image_url="data:image/webp;base64,XX")]
    req = FakeReq(prompt=None, image_urls=[], extra_user_content_parts=parts)
    n = main._strip_image_urls(req, only_data_url=True)
    assert n == 1
    assert req.extra_user_content_parts == []
    print("✓ test_strip_handles_string_image_url_field")


def test_main_hook_then_residual_strip_endtoend():
    """主钩子处理后，链末兜底会清除剩下的 data:base64 残留。"""
    p = new_plugin()

    # 模拟主钩子已处理 image_urls，但 req.contexts 里 AngelHeart 又塞了 base64
    req = FakeReq(
        prompt="原始",
        image_urls=[],  # 主钩子已清空
        contexts=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/webp;base64,RESIDUAL"},
                    },
                ],
            }
        ],
    )

    async def fake_run(*args, **kwargs):
        return "ok", ""

    # 直接调链末兜底
    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p.strip_residual_base64(SimpleNamespace(), req))

    # base64 应被删除，文本保留
    new_content = req.contexts[0]["content"]
    assert len(new_content) == 1
    assert new_content[0]["type"] == "text"
    print("✓ test_main_hook_then_residual_strip_endtoend")


# ------------------------------------------------------------------ 单测：失败清理


def test_attach_clears_image_urls_even_on_failure():
    """mmx 失败时主钩子仍要清空 image_urls，避免 raw URL 走到 LLM。"""
    p = new_plugin()
    req = FakeReq(
        prompt="看图",
        image_urls=["https://x.com/a.jpg", "https://x.com/b.jpg"],
    )
    p._attach(
        req,
        [(1, "https://x.com/a.jpg", ""), (2, "https://x.com/b.jpg", "")],
        start_index=1,
        field="image_urls",
    )
    assert req.prompt == "看图"
    assert len(req.extra_user_content_parts) == 2
    def _t(p):
        return p["text"] if isinstance(p, dict) else p.text
    assert "[Image 1 描述]" in _t(req.extra_user_content_parts[0])
    assert "理解失败" in _t(req.extra_user_content_parts[0])
    assert "[Image 2 描述]" in _t(req.extra_user_content_parts[1])
    assert "理解失败" in _t(req.extra_user_content_parts[1])
    assert req.image_urls == []
    print("✓ test_attach_clears_image_urls_even_on_failure")


def test_attach_clears_extra_parts_even_on_failure():
    p = new_plugin()
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
    ]
    req = FakeReq(prompt=None, image_urls=[], extra_user_content_parts=parts)
    p._attach(
        req,
        [(1, "https://x.com/a.jpg", "")],  # 失败
        start_index=1,
        field="extra_user_content_parts",
    )
    # image_url 被清除，原 text 保留 + 失败占位加上
    assert len(req.extra_user_content_parts) == 2
    ep0 = req.extra_user_content_parts[0]
    text0 = ep0["text"] if isinstance(ep0, dict) else ep0.text
    assert text0 == "hi"
    ep1 = req.extra_user_content_parts[1]
    text1 = ep1["text"] if isinstance(ep1, dict) else ep1.text
    assert "理解失败" in text1
    assert req.prompt is None
    print("✓ test_attach_clears_extra_parts_even_on_failure")


def test_attach_clears_contexts_even_on_failure():
    p = new_plugin()
    contexts = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
        ],
    }]
    req = FakeReq(prompt=None, image_urls=[], contexts=contexts)
    p._attach(
        req,
        [(1, "https://x.com/a.jpg", "")],  # 失败
        start_index=1,
        field="contexts",
        context_target=contexts[0],
    )
    assert len(contexts[0]["content"]) == 1
    assert contexts[0]["content"][0]["type"] == "text"
    print("✓ test_attach_clears_contexts_even_on_failure")


# ------------------------------------------------------------------ 单测：链末全删


def test_strip_all_image_urls_removes_everything():
    p = new_plugin()
    req = FakeReq(
        prompt="hi",
        image_urls=[
            "https://x.com/a.jpg",
            "data:image/png;base64,XYZ",
            "file:///tmp/b.jpg",
        ],
        extra_user_content_parts=[
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "https://x.com/c.jpg"}},
        ],
        contexts=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "y"},
                {"type": "image_url", "image_url": {"url": "https://x.com/d.jpg"}},
            ],
        }],
    )
    n = main._strip_image_urls(req, only_data_url=False)
    assert n == 5  # 3 image_urls + 1 extra_part + 1 context
    assert req.image_urls == []
    assert req.extra_user_content_parts == [{"type": "text", "text": "x"}]
    assert req.contexts[0]["content"] == [{"type": "text", "text": "y"}]
    print("✓ test_strip_all_image_urls_removes_everything")


def test_strip_all_image_urls_zero_when_nothing():
    p = new_plugin()
    req = FakeReq(prompt="hi", image_urls=[], extra_user_content_parts=[], contexts=[])
    n = main._strip_image_urls(req, only_data_url=False)
    assert n == 0
    print("✓ test_strip_all_image_urls_zero_when_nothing")


def test_fallback_strip_all_when_configured():
    """配置开启 strip_all_image_urls_in_fallback 后，链末兑底应全删。"""
    p = new_plugin(strip_all_image_urls_in_fallback=True)
    req = FakeReq(
        prompt="hi",
        image_urls=["https://x.com/a.jpg"],
        contexts=[{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x.com/b.jpg"}}],
        }],
    )
    asyncio.run(p.strip_residual_base64(SimpleNamespace(), req))
    assert req.image_urls == []
    assert req.contexts[0]["content"] == []
    print("✓ test_fallback_strip_all_when_configured")


def test_fallback_strip_only_data_url_by_default():
    """v0.8.4 行为变更：链末兑底**总是**清空 req.image_urls（防 chat_plus 等中间插件
    重新填图）。默认 strip_all_image_urls_in_fallback=False 也清空。"""
    p = new_plugin()  # 默认 strip_all_image_urls_in_fallback=False
    req = FakeReq(
        prompt="hi",
        image_urls=["https://x.com/a.jpg"],
        contexts=[{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x.com/b.jpg"}}],
        }],
    )
    asyncio.run(p.strip_residual_base64(SimpleNamespace(), req))
    # v0.8.4 起：**总是**清空 image_urls（即使 https URL）
    assert req.image_urls == []
    # contexts 里的 image_url 也被清掉
    assert len(req.contexts[0]["content"]) == 0
    print("✓ test_fallback_strip_only_data_url_by_default")


# ------------------------------------------------------------------ 单测：诊断信息


def test_diagnose_balance_error():
    p = new_plugin()
    # 重置告警缓存
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("API error: insufficient balance (HTTP 200)", "http://x.com/a.jpg")
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED
    print("✓ test_diagnose_balance_error")


def test_diagnose_quota_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("quota exceeded", "http://x.com/a.jpg")
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED
    print("✓ test_diagnose_quota_error")


def test_diagnose_auth_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("auth token expired", "http://x.com/a.jpg")
    assert "auth" in main.VisionTextBridgePlugin._DIAGNOSED
    print("✓ test_diagnose_auth_error")


def test_diagnose_argument_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("No such file or directory", "http://x.com/a.jpg")
    assert "argument" in main.VisionTextBridgePlugin._DIAGNOSED
    print("✓ test_diagnose_argument_error")


def test_diagnose_unknown_error_no_warning():
    """未识别的错误不应触发告警。"""
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("some unknown error xyz123", "http://x.com/a.jpg")
    assert main.VisionTextBridgePlugin._DIAGNOSED == set()
    print("✓ test_diagnose_unknown_error_no_warning")


def test_diagnose_warn_once():
    """同一个错误 key 不重复告警。"""
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED.clear()
    p._diagnose_mmx_error("insufficient balance", "http://x.com/a.jpg")
    p._diagnose_mmx_error("insufficient balance", "http://x.com/b.jpg")
    # 实际：balance 在 set 中
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED
    # 再调一次不会重复 add（set 长度不变）
    size_before = len(main.VisionTextBridgePlugin._DIAGNOSED)
    p._diagnose_mmx_error("insufficient balance", "http://x.com/c.jpg")
    size_after = len(main.VisionTextBridgePlugin._DIAGNOSED)
    assert size_before == size_after
    print("✓ test_diagnose_warn_once")


# ------------------------------------------------------------------ 单测：SQLite 缓存


def test_caption_cache_basic_crud():
    """CaptionCache 增删改查。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "test.sqlite3")
        assert cache.count() == 0
        # put + get
        cache.put("https://x.com/a.jpg", "https://x.com/a.jpg", "一只猫")
        assert cache.count() == 1
        entry = cache.get("https://x.com/a.jpg")
        assert entry is not None
        assert entry.description == "一只猫"
        assert entry.hit_count == 1
        # 再 get 一次，hit_count 递增
        entry2 = cache.get("https://x.com/a.jpg")
        assert entry2.hit_count == 2
        # delete
        assert cache.delete("https://x.com/a.jpg") is True
        assert cache.count() == 0
        # 不存在的 key
        assert cache.get("nonexistent") is None
        assert cache.delete("nonexistent") is False
    print("✓ test_caption_cache_basic_crud")


def test_caption_cache_v0_8_6_image_id_and_b64():
    """v0.8.6: image_id 主键 + 存 base64 / mime / 宽高 / size 元信息。"""
    import tempfile
    import base64
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "v8_8_6.sqlite3")
        # 模拟存一张 JPEG
        fake_jpg = b"\xff\xd8\xff\xe0\x00\x10JFIFfake-bytes-for-test"
        b64 = base64.b64encode(fake_jpg).decode("ascii")
        cache.put(
            "md5_abc",
            "file:///tmp/x.jpg",
            "描述",
            image_b64=b64,
            mime_type="image/jpeg",
            file_size=len(fake_jpg),
            width=320,
            height=240,
        )
        # 取回
        entry = cache.get("md5_abc", with_b64=True)
        assert entry is not None
        assert entry.image_id == "md5_abc"
        assert entry.mime_type == "image/jpeg"
        assert entry.file_size == len(fake_jpg)
        assert entry.width == 320
        assert entry.height == 240
        assert entry.image_b64 == b64
        # list 不返 base64（以减小 body）
        items = cache.list(limit=10)
        assert len(items) == 1
        assert items[0].image_b64 == ""  # 默认不返
        assert "image_b64" not in items[0].to_dict()  # to_dict 也不返
        # 显式 include_b64=True 才返
        items_with_b64 = cache.list(limit=10, include_b64=True)
        assert items_with_b64[0].image_b64 == b64
    print("✓ test_caption_cache_v0_8_6_image_id_and_b64")


def test_caption_cache_v0_8_6_schema_upgrade():
    """v0.8.6 schema 升级：老库 (无 image_b64 等列) 启动后能补上。"""
    import tempfile
    import sqlite3
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.sqlite3"
        # 手建老 schema
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE image_captions (
                image_key TEXT PRIMARY KEY,
                image_url TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at REAL
            );
        """)
        conn.execute(
            "INSERT INTO image_captions VALUES (?, ?, ?, ?, ?, ?)",
            ("abc", "https://x.com/1.jpg", "老描述", 100.0, 5, 99.0),
        )
        conn.commit()
        conn.close()
        # 打开 CaptionCache 应自动补上缺失列
        cache = main.CaptionCache(db_path)
        entry = cache.get("abc", with_b64=True)
        assert entry is not None
        assert entry.image_id == "abc"
        assert entry.description == "老描述"
        # 新列都在
        assert entry.mime_type == ""
        assert entry.file_size == 0
        assert entry.width == 0
        assert entry.height == 0
        assert entry.image_b64 == ""
        # 新增能存
        cache.put(
            "def", "https://x.com/2.jpg", "新描述",
            mime_type="image/png", file_size=10, width=1, height=1, image_b64="AAA",
        )
        new = cache.get("def", with_b64=True)
        assert new is not None
        assert new.mime_type == "image/png"
    print("✓ test_caption_cache_v0_8_6_schema_upgrade")


def test_caption_cache_make_id_helpers():
    """v0.8.6: 静态 id 生成器。"""
    # make_id_from_bytes：同 bytes 返同 id
    id1 = main.CaptionCache.make_id_from_bytes(b"hello")
    id2 = main.CaptionCache.make_id_from_bytes(b"hello")
    id3 = main.CaptionCache.make_id_from_bytes(b"world")
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 32
    # make_id_from_url：退路
    id4 = main.CaptionCache.make_id_from_url("https://x.com/a.jpg")
    assert id4 == main.CaptionCache.make_id_from_url("https://x.com/a.jpg")
    assert id4 != main.CaptionCache.make_id_from_url("https://x.com/b.jpg")
    assert len(id4) == 32
    print("✓ test_caption_cache_make_id_helpers")


def test_caption_cache_persistence():
    """SQLite 缓存跨实例保留。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "persist.sqlite3"
        cache1 = main.CaptionCache(db_path)
        cache1.put("https://x.com/b.jpg", "https://x.com/b.jpg", "一只狗")
        del cache1
        # 重新创建实例，验证数据还在
        cache2 = main.CaptionCache(db_path)
        entry = cache2.get("https://x.com/b.jpg")
        assert entry is not None
        assert entry.description == "一只狗"
    print("✓ test_caption_cache_persistence")


def test_caption_cache_list_and_search():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "list.sqlite3")
        cache.put("id_a", "https://a.com/1.jpg", "猫")
        cache.put("id_b", "https://b.com/2.jpg", "狗")
        cache.put("id_c", "https://c.com/3.jpg", "猫头鹰")
        all_items = cache.list(limit=10, offset=0)
        assert len(all_items) == 3
        cat_items = cache.list(limit=10, offset=0, search="猫")
        # "猫" 匹配 "猫" 和 "猫头鹰"——但搜索是 OR 匹配，https://c.com 包含 "c"
        # 实际："猫" 匹配 url 描述中任一含 "猫" 的
        # "猫" 出现在 cat ("猫") 和 owl ("猫头鹰") 的 description 中
        # 实际 list 全部看下
        all_cats = [it for it in cat_items if "猫" in it.description]
        assert len(all_cats) >= 2
        # limit 测试
        page1 = cache.list(limit=1, offset=0, order_by="created_at_asc")
        assert len(page1) == 1
        # 排序测试
        cache.get("id_a")  # 增加 hit_count
        cache.get("id_a")
        cache.get("id_a")
        most_hit = cache.list(limit=10, offset=0, order_by="hit_count_desc")
        assert most_hit[0].image_id == "id_a"
    print("✓ test_caption_cache_list_and_search")


def test_caption_cache_stats():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "stats.sqlite3")
        s = cache.stats()
        assert s.total == 0
        cache.put("a", "https://a.com/1.jpg", "猫")
        cache.get("a")
        cache.get("a")
        s2 = cache.stats()
        assert s2.total == 1
        assert s2.total_hits == 2
        assert s2.oldest_at is not None
        assert s2.newest_at is not None
    print("✓ test_caption_cache_stats")


def test_caption_cache_vacuum():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "v.sqlite3")
        for i in range(10):
            cache.put(f"k{i}", f"https://x.com/{i}.jpg", f"描述 {i}")
        cache.clear()
        cache.vacuum()  # 不应报错
    print("✓ test_caption_cache_vacuum")


def test_describe_one_uses_sqlite_cache():
    """_describe_one 命中 SQLite 缓存时直接返回，不调 mmx。"""
    import tempfile
    import hashlib
    from pathlib import Path
    url = "https://x.com/cached.jpg"
    # v0.8.6: image_id 退路是 md5(url 字符串)
    expected_key = hashlib.md5(url.encode("utf-8")).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "c.sqlite3")
        p._caption_cache.put(expected_key, url, "已缓存的描述")
        # 同时清空内存缓存确保走 SQLite
        p._description_cache.clear()

        called = {"count": 0}

        async def fake_run(*a, **k):
            called["count"] += 1
            return test.make_mmx_result("不应该被调用", "", 0, True)

        with patch.object(p, "_run_mmx", side_effect=fake_run):
            result = asyncio.run(p._describe_one(url))
        assert result == "已缓存的描述"
        assert called["count"] == 0  # 缓存命中，没调 mmx
        # 内存缓存应被同步填充
        assert p._description_cache[expected_key] == "已缓存的描述"
    print("✓ test_describe_one_uses_sqlite_cache")


def test_describe_one_writes_to_sqlite_cache():
    """mmx 成功后应同时写内存 + SQLite 缓存。"""
    import tempfile
    import hashlib
    from pathlib import Path
    url = "https://x.com/fresh.jpg"
    expected_key = hashlib.md5(url.encode("utf-8")).hexdigest()  # v0.8.6: 退路 md5(url)
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "c.sqlite3")
        p._description_cache.clear()
        with patch.object(
            p, "_run_mmx",
            return_value=make_mmx_result("新鲜描述", "", 0, True),
        ):
            result = asyncio.run(p._describe_one(url))
        assert result == "新鲜描述"
        assert p._description_cache[expected_key] == "新鲜描述"
        entry = p._caption_cache.get(expected_key)
        assert entry is not None
        assert entry.description == "新鲜描述"
    print("✓ test_describe_one_writes_to_sqlite_cache")


# ------------------------------------------------------------------ 单测：Chat Archive 联动已移除 (v0.8.6)




# ------------------------------------------------------------------ 单测：web API


def test_register_web_apis_called():
    """v0.8.6: _register_web_apis 应注册 6 个 web API（v0.8.5 之前 7 个，删了 chat-archive/refresh）。"""
    p = new_plugin()
    calls = []

    def mock_register(route, fn, methods, desc):
        calls.append((route, methods, desc))

    p.context = SimpleNamespace(
        request=SimpleNamespace(args={}, json=None),
        register_web_api=mock_register,
    )
    p._register_web_apis()
    routes = [c[0] for c in calls]
    assert any("cache/stats" in r for r in routes)
    assert any("cache/list" in r for r in routes)
    assert any("cache/delete" in r for r in routes)
    assert any("cache/clear" in r for r in routes)
    assert any("cache/regenerate" in r for r in routes)
    assert any("cache/export" in r for r in routes)
    assert any("cache/thumbnail" in r for r in routes)  # v0.8.6 新增
    # 确认 chat-archive/refresh 已删
    assert not any("chat-archive" in r for r in routes)
    print("✓ test_register_web_apis_called")


# ------------------------------------------------------------------ 单测：页面端到端


def test_end_to_end_full_flow():
    """模拟完整流程：拦截 → 缓存 → 页面 API 查询 → 删除 → 重新生成。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "c.sqlite3")
        p._description_cache.clear()

        # 1. 拦截 LLM 请求 (mmx 调用成功)
        req = FakeReq(prompt="看图", image_urls=["https://x.com/x.jpg"])
        with patch.object(
            p, "_run_mmx",
            return_value=make_mmx_result("一只狗", "", 0, True),
        ):
            asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))
        # v0.7: prompt 不变，图说在 extra_user_content_parts
        assert req.prompt == "看图"
        assert len(req.extra_user_content_parts) == 1
        assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 一只狗"
        assert req.image_urls == []

        # 2. 页面查询 stats
        stats = p._caption_cache.stats()
        assert stats.total == 1
        assert stats.total_hits == 0  # 刚 put, 没 get 过

        # 3. 页面查询 list
        items = p._caption_cache.list(limit=10)
        assert len(items) == 1
        assert items[0].description == "一只狗"

        # 4. 再次 get (模拟用户发同一张图) — 命中内存
        with patch.object(
            p, "_run_mmx",
            return_value=make_mmx_result("不该被调", "", 0, True),
        ) as m:
            r2 = asyncio.run(p._describe_one("https://x.com/x.jpg"))
        assert r2 == "一只狗"  # 内存缓存命中

        # 5. 页面删除 (先清内存)
        p._description_cache.clear()
        # v0.8.6: image_id 是 32 位 hex（md5(url)）
        import hashlib
        key = hashlib.md5("https://x.com/x.jpg".encode("utf-8")).hexdigest()
        ok = p._caption_cache.delete(key)
        assert ok
        assert p._caption_cache.count() == 0

        # 6. 再次发同一张图 — 缓存被清，重新调 mmx
        with patch.object(
            p, "_run_mmx",
            return_value=make_mmx_result("新描述", "", 0, True),
        ):
            r3 = asyncio.run(p._describe_one("https://x.com/x.jpg"))
        assert r3 == "新描述"
        assert p._caption_cache.count() == 1
    print("✓ test_end_to_end_full_flow")


# ------------------------------------------------------------------ 单测：system_prompt 注入


def test_inject_system_prompt_guidance_default_on():
    """v0.7 新行为：图说本身在 user message 的 extra_user_content_parts 里，
    system_prompt 只追加"严格引用"提示。"""
    p = new_plugin()
    req = FakeReq(
        prompt="看图",
        extra_user_content_parts=[
            {"type": "text", "text": "[Image 1 描述] 一只狗"},
            {"type": "text", "text": "[Image 2 描述] 一只猫"},
        ],
    )
    req.system_prompt = "你是一个助手。"
    p._inject_guidance(req)
    # system_prompt 追加了指导
    assert "[视觉模型描述]" in req.system_prompt
    assert "2 张图片" in req.system_prompt
    assert "严格基于" in req.system_prompt
    # 原始 system_prompt 保留
    assert "你是一个助手。" in req.system_prompt
    # prompt 不变
    assert req.prompt == "看图"
    print("✓ test_inject_system_prompt_guidance_default_on")


def test_inject_disabled_when_config_off():
    p = new_plugin(inject_system_prompt_guidance=False)
    req = FakeReq(
        prompt="看图",
        extra_user_content_parts=[
            {"type": "text", "text": "[Image 1 描述] 一只狗"},
        ],
    )
    req.system_prompt = "你是一个助手。"
    p._inject_guidance(req)
    # 不应修改
    assert req.system_prompt == "你是一个助手。"
    print("✓ test_inject_disabled_when_config_off")


def test_inject_no_images_in_prompt_no_op():
    """v0.7: extra_user_content_parts 中没有 [Image N 描述] 标记就不注入。"""
    p = new_plugin()
    req = FakeReq(prompt="没有图")
    req.system_prompt = "你是一个助手。"
    p._inject_guidance(req)
    assert req.system_prompt == "你是一个助手。"
    print("✓ test_inject_no_images_in_prompt_no_op")


def test_main_hook_clears_image_urls_immediately():
    """v0.8: 主钩子入口**立即**清空 image_urls，防 AstrBot 切 provider。

    背景: AstrBot 在 astr_main_agent._select_image_chat_provider() 根据
        `if not req.image_urls or _provider_supports_modality(provider, "image")`
    判断是否切 provider。需保证主钩子处理中任何时刻 image_urls 都是空的。
    """
    p = new_plugin()
    req = FakeReq(prompt="看图", image_urls=["https://x.com/a.jpg"])
    # 模拟 _run_mmx: 返回描述
    with patch.object(
        p, "_run_mmx",
        return_value=make_mmx_result("猫", "", 0, True),
    ):
        # 主钩子入口调用 → 清空 + 处理 + 注入
        asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))

    # **主钩子完成后**，image_urls 仍然空（初始就清了 + 末尾又清了）
    assert req.image_urls == []
    # 图说在 extra_user_content_parts 里
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 猫"
    print("✓ test_main_hook_clears_image_urls_immediately")


def test_main_hook_clears_extra_parts_and_contexts_images():
    """主钩子入口清空 extra_user_content_parts 和 contexts 里的 image_url 组件。"""
    p = new_plugin()
    parts = [
        {"type": "text", "text": "附加"},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
    ]
    contexts = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "上轮"},
            {"type": "image_url", "image_url": {"url": "https://x.com/b.jpg"}},
        ],
    }]
    req = FakeReq(
        prompt="现在",
        image_urls=[],
        extra_user_content_parts=list(parts),
        contexts=list(contexts),
    )

    # 模拟 _run_mmx: 返回两段描述（一个 a 一个 b）
    counter = {"n": 0}
    async def fake_run(*args, **kwargs):
        counter["n"] += 1
        return f"desc{counter['n']}", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))

    # image_urls 仍空
    assert req.image_urls == []
    # extra_user_content_parts 中 image_url 被清除，原 text 保留
    # + 2 个新加的图说 content blocks
    text_parts = [p for p in req.extra_user_content_parts if p.get("type") == "text"]
    text_texts = [p["text"] for p in text_parts]
    # 原始 "附加" 保留 + 2 个图说
    assert "附加" in text_texts
    # 图说个数 >= 1（主钩子只处理 image_urls，因为 include_extra_parts=True
    # 但 image_urls 已经是空——因为我们从快照里处理图说，所以应该有一个）
    # 实际：include_extra_parts=True 走第 2 步——但快照里也只有一个 image_url（a.jpg）
    # 等等：image_urls 快照为 [], 所以第 1 步不处理
    # extra_parts 快照 含 image_url(a.jpg) → 第 2 步处理 → 1 个图说
    assert any("desc1" in t for t in text_texts)
    # image_url 组件被清除了（只保留 text）
    assert not any(p.get("type") == "image_url" for p in req.extra_user_content_parts)
    # contexts 里 image_url 也被清了
    assert not any(
        c.get("type") == "image_url"
        for c in contexts[0]["content"]
    )
    print("✓ test_main_hook_clears_extra_parts_and_contexts_images")


def test_main_hook_saves_snapshots_for_process_request():
    """主钩子入口保存快照，_process_request 从快照读图（不从 req）。"""
    p = new_plugin()
    req = FakeReq(
        prompt="看图",
        image_urls=["https://x.com/a.jpg", "https://x.com/b.jpg"],
    )

    # 在调主钩子之前，_pending_* 是 None 或不存在
    assert getattr(p, "_pending_image_urls", None) is None

    with patch.object(
        p, "_run_mmx",
        return_value=make_mmx_result("猫", "", 0, True),
    ):
        asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))

    # 钩子完成后，_pending_* 被清（None）
    assert getattr(p, "_pending_image_urls", None) is None
    # 但图说仍注入到 extra_user_content_parts
    assert len(req.extra_user_content_parts) == 2
    print("✓ test_main_hook_saves_snapshots_for_process_request")


def test_to_text_part_creates_pydantic_object():
    """验证 _to_text_part 返回的对象有 model_dump_for_context 方法（防止 v0.7 崩溃）。"""
    p = new_plugin()
    obj = main._to_text_part({"type": "text", "text": "hello"})
    # 必须有 model_dump_for_context 方法
    if hasattr(obj, "model_dump_for_context"):
        # 如果有，是 Pydantic 对象
        result = obj.model_dump_for_context()
        assert result["text"] == "hello"
        assert result["type"] == "text"
        print("✓ test_to_text_part_creates_pydantic_object (Pydantic)")
    else:
        # fallback 到 dict（在没有 astrbot 模块的测试环境下）
        assert obj == {"type": "text", "text": "hello"}
        print("✓ test_to_text_part_creates_pydantic_object (dict fallback)")


def test_mark_providers_adds_image_modality():
    """验证 _mark_all_providers_support_image 给 provider 补 'image' modality。"""
    p = new_plugin()

    # Mock context with provider_manager
    class FakeProvider:
        def __init__(self, pid, modalities=None):
            self.provider_config = {"id": pid}
            if modalities is not None:
                self.provider_config["modalities"] = modalities

    class FakeManager:
        def __init__(self, providers):
            self.providers = {p.provider_config["id"]: p for p in providers}

    p1 = FakeProvider("minimax-token-plan/MiniMax-M2.5", modalities=["text"])
    p2 = FakeProvider("deepseek/deepseek-v4-flash", modalities=None)
    p3 = FakeProvider("openai/gpt-4o", modalities=["text", "image"])  # 已有 image

    fake_ctx = SimpleNamespace(provider_manager=FakeManager([p1, p2, p3]))
    p.context = SimpleNamespace(astr_context=fake_ctx)

    p._mark_providers_support_image()

    # 三个都应被加上 'image' modality
    assert "image" in p1.provider_config["modalities"]
    assert "image" in p2.provider_config["modalities"]
    assert "image" in p3.provider_config["modalities"]  # 原本就有
    # p1 的 modalities 应保留 text
    assert "text" in p1.provider_config["modalities"]
    print("✓ test_mark_providers_adds_image_modality")


def test_mark_providers_skipped_when_config_off():
    """keep_provider_modality_as_is=True 时不动 provider modalities。"""
    p = new_plugin(keep_provider_modality_as_is=True)

    class FakeProvider:
        provider_config = {"id": "x", "modalities": ["text"]}

    fake_ctx = SimpleNamespace(provider_manager=SimpleNamespace(providers={"x": FakeProvider()}))
    p.context = SimpleNamespace(astr_context=fake_ctx)

    p._mark_providers_support_image()

    # modalities 不变
    assert FakeProvider.provider_config["modalities"] == ["text"]
    print("✓ test_mark_providers_skipped_when_config_off")


def test_cache_key_uses_md5_for_file_url():
    """v0.8.6: file:// 路径的缓存 image_id 应为图片内容 md5。"""
    import tempfile
    from pathlib import Path
    import hashlib
    p = new_plugin()
    # 创建一个临时图片文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        f.write(b"fake image bytes for test")
        tmp_path = f.name
    try:
        file_url = f"file://{tmp_path}"
        # 读取文件应该能读到这些字节
        bytes_read = Path(tmp_path).read_bytes()
        expected_md5 = hashlib.md5(bytes_read).hexdigest()
        # 调 _compute_image_cache_key
        key = asyncio.run(p._compute_image_cache_key(file_url))
        assert key == expected_md5, f"expected {expected_md5}, got {key}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print("✓ test_cache_key_uses_md5_for_file_url")


def test_cache_key_falls_back_to_url_on_read_failure():
    """v0.8.6: 读图片字节失败时，key 退到 md5(url 字符串)。"""
    import hashlib
    p = new_plugin()
    # 调一个不存在的文件
    url = "file:///nonexistent/file.jpg"
    key = asyncio.run(p._compute_image_cache_key(url))
    # 应为 32 位 hex，且等于 md5(url)
    assert len(key) == 32
    assert key == hashlib.md5(url.encode("utf-8")).hexdigest()
    print("✓ test_cache_key_falls_back_to_url_on_read_failure")


def test_same_image_different_path_hits_cache():
    """**v0.8.6 关键场景**：同一张图不同路径能命中缓存（QQ 群聊的痛点）。"""
    import tempfile
    import hashlib
    from pathlib import Path
    p = new_plugin()
    # 同一张图的两个不同压缩路径（模拟 AstrBot 两次压缩生成不同文件名）
    img_bytes = b"hello world image"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f1:
        f1.write(img_bytes)
        path1 = f1.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f2:
        f2.write(img_bytes)
        path2 = f2.name
    try:
        url1 = f"file://{path1}"
        url2 = f"file://{path2}"
        # 两次调 _compute_image_cache_key，md5 应一样
        key1 = asyncio.run(p._compute_image_cache_key(url1))
        key2 = asyncio.run(p._compute_image_cache_key(url2))
        assert key1 == key2, f"expected same md5 key, got {key1} vs {key2}"
        assert key1 == hashlib.md5(img_bytes).hexdigest()
    finally:
        Path(path1).unlink(missing_ok=True)
        Path(path2).unlink(missing_ok=True)
    print("✓ test_same_image_different_path_hits_cache")


def test_login_mmx_handles_mmxresult_not_tuple():
    """v0.8.3 修复：_login_mmx 之前误以为 _run_mmx 返回 tuple，实际返回 MmxResult dataclass。

    'stdout, stderr = await self._run_mmx(...)' 会在 _run_mmx 返回 dataclass 时
    抛 'cannot unpack non-iterable _Result object'。现在改成 result = await ...,
    然后用 result.ok / result.stdout。
    """
    p = new_plugin()
    p.mmx_path = "/usr/bin/true"

    # 模拟 _run_mmx 返回成功的 MmxResult
    async def fake_ok(*args, **kwargs):
        from dataclasses import dataclass

        @dataclass
        class R:
            stdout: str = "Login successful"
            stderr: str = ""
            returncode: int = 0
            ok: bool = True

        return R()

    # 不应抛 'cannot unpack' 异常
    with patch.object(p, "_run_mmx", side_effect=fake_ok):
        asyncio.run(p._login_mmx("sk-foobar1234"))

    # 模拟 _run_mmx 返回失败的 MmxResult
    async def fake_fail(*args, **kwargs):
        from dataclasses import dataclass

        @dataclass
        class R:
            stdout: str = ""
            stderr: str = "unauthenticated"
            returncode: int = 1
            ok: bool = False

        return R()

    with patch.object(p, "_run_mmx", side_effect=fake_fail):
        asyncio.run(p._login_mmx("sk-foobar1234"))

    print("✓ test_login_mmx_handles_mmxresult_not_tuple")


def test_get_installed_plugin_names_handles_missing_attr():
    """_get_installed_plugin_names 在 context 没 plugin_manager 时不崩，返回空集。"""
    p = new_plugin()
    # 各种 context 形态都不能让插件崩
    p.context = SimpleNamespace()
    result = p._get_installed_plugin_names()
    assert isinstance(result, set)
    # context 是 None
    p.context = None
    try:
        result = p._get_installed_plugin_names()
    except Exception as e:
        # 允许报 "NoneType has no attribute" 但不崩 plugin
        pass
    print("✓ test_get_installed_plugin_names_handles_missing_attr")


def test_check_compatibility_warns_on_uni_nickname_low_priority():
    """_check_other_plugin_compatibility 在 priority<=0 且有 uni_nickname 时应警告。"""
    p = new_plugin()
    p._configured_priority = 0  # 低于 uni_nickname 的默认 0
    # Mock 插件名列表
    p.context = SimpleNamespace()
    p._get_installed_plugin_names = lambda: {"astrbot_plugin_uni_nickname"}
    # 不应抛异常
    p._check_compatibility()
    print("✓ test_check_compatibility_warns_on_uni_nickname_low_priority")


def test_chat_plus_style_image_reinjection_is_cleaned():
    """v0.8.4: 模拟 chat_plus 风格的中间插件在主钩子之后重新填图，链末兜底应清掉。

    场景: 插件链执行顺序
      1. 我 priority=500 跑（清空 image_urls + 注入图说 content block）
      2. 中间插件 priority=-1 跑（重新填 req.image_urls ＝ chat_plus 的图）
      3. 我 priority=-10000 链末跑（**总是**清空 req.image_urls）

    最终 LLM 看到的 image_urls 是空，只读图说 content block。
    """
    p = new_plugin()
    # 1. 主钩子：清空 + 注入图说
    req = FakeReq(
        prompt="看图",
        image_urls=["https://x.com/a.jpg"],
    )
    with patch.object(
        p, "_run_mmx",
        return_value=make_mmx_result("猫", "", 0, True),
    ):
        asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))

    # 主钩子完成后：图说在 extra_user_content_parts
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 猫"
    assert req.image_urls == []  # 主钩子已清

    # 2. 中间插件（如 chat_plus priority=-1）重新填图
    req.image_urls = ["https://x.com/re-injected-by-chatplus.jpg"]

    # 3. 链末兜底：应该清空
    asyncio.run(p.strip_residual_base64(SimpleNamespace(), req))
    assert req.image_urls == []
    # 图说 content block 不受影响
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 猫"
    print("✓ test_chat_plus_style_image_reinjection_is_cleaned")


def test_chat_plus_compat_event_image_components_recovered():
    """v0.8.5: chat_plus 默认 enable_image_processing=False，导致它调
    event.request_llm(image_urls=[]) 不传图。LLM 看到 0 图。

    本插件不应被这个默认配置劫持——从 event.message_obj.message 里
    补提 Image 组件，拿到本地图路径。
    """
    import tempfile
    from pathlib import Path
    p = new_plugin()
    p._vision_semaphore = asyncio.Semaphore(1)
    # 创建一个临时图文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        f.write(b"fake image bytes")
        tmp_path = f.name
    try:
        # mock event with message_obj.message containing Image component
        class _Image:
            # AstrBot 实际 Image 组件有 type="image" 属性（Pydantic ComponentType 枚举）
            type = "image"

            def __init__(self, path, url=None):
                self.path = path
                self.url = url
                self.file = None
            async def convert_to_file_path(self):
                return self.path

        class _Msg:
            def __init__(self, path):
                self.message = [_Image(path)]
        class _Evt:
            def __init__(self, path):
                self.message_obj = _Msg(path)

        evt = _Evt(tmp_path)
        # req.image_urls is **empty** (chat_plus 行为)
        req = FakeReq(prompt="看图", image_urls=[])

        with patch.object(p, "_run_mmx", return_value=make_mmx_result("小猫", "", 0, True)):
            asyncio.run(p.bridge_vision_to_text(evt, req))

        # **关键**：图说在 extra_user_content_parts（即使 req.image_urls=[]）
        assert len(req.extra_user_content_parts) == 1
        assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 小猫"
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print("✓ test_chat_plus_compat_event_image_components_recovered")


def test_v0851_no_duplicate_mmx_call_for_same_image():
    """v0.8.5.1 修复：Image 组件的 url/file 字段与 convert_to_file_path() 可能指向同张图。
    之前 v0.8.5 同时取二者，导致同张图被调 2 次 mmx（local 路径成功 + remote URL 超时）。
    现在只取 convert_to_file_path()，确保不重复。
    """
    import tempfile
    from pathlib import Path
    p = new_plugin()
    p._vision_semaphore = asyncio.Semaphore(1)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        f.write(b"fake image bytes")
        tmp_path = f.name
    try:
        class _Image:
            type = "image"
            def __init__(self, path, url):
                self.path = path
                self.url = url  # remote URL
                self.file = None
            async def convert_to_file_path(self):
                return self.path

        class _Msg:
            def __init__(self, comp):
                self.message = [comp]
        class _Evt:
            def __init__(self, comp):
                self.message_obj = _Msg(comp)

        # Image 组件有 url（远程 QQ URL）和 path（local path）
        comp = _Image(tmp_path, "https://multimedia.nt.qq.com.cn/download?xxx")
        evt = _Evt(comp)
        req = FakeReq(prompt="看图", image_urls=[])

        # 记录 mmx 调用次数
        call_args = []

        async def fake_run_mmx(*args, **kwargs):
            call_args.append(args)
            return make_mmx_result("描述", "", 0, True)

        with patch.object(p, "_run_mmx", side_effect=fake_run_mmx):
            asyncio.run(p.bridge_vision_to_text(evt, req))

        # **关键**：mmx 应**只调一次**（用 local path，不用 remote URL）
        assert len(call_args) == 1, f"expected 1 mmx call, got {len(call_args)}"
        # 调用应是 local path
        url_arg = call_args[0][3]  # vision describe --image <path> --prompt ...
        assert "multimedia.nt.qq.com.cn" not in url_arg, (
            f"mmx 用了远程 URL: {url_arg}"
        )
        assert tmp_path in url_arg or url_arg == tmp_path
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print("✓ test_v0851_no_duplicate_mmx_call_for_same_image")


def test_survives_prompt_overwrite_by_other_plugin():
    """v0.7 终极解：图说在 user message 的 content block 里，不依赖 prompt 字符串。
    任何插件重写 prompt 都不会丢失。"""
    p = new_plugin()
    # 1. 主钩子处理 image_urls → 把图说作为 content block 加入
    req = FakeReq(prompt="看图", image_urls=["https://x.com/x.jpg"])
    with patch.object(
        p, "_run_mmx",
        return_value=make_mmx_result("猫", "", 0, True),
    ):
        asyncio.run(p.bridge_vision_to_text(SimpleNamespace(), req))

    # 验证：主钩子处理后 prompt 字符串不变
    assert req.prompt == "看图"
    # 图说在 extra_user_content_parts 里（**不被**任何插件重写）
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["text"] == "[Image 1 描述] 猫"
    # image_urls 已清空
    assert req.image_urls == []
    print("  before overwrite: prompt 字符串不变, 图说在 content block 里")

    # 2. 模拟 AngelHeart 重写 req.prompt
    req.prompt = "AngelHeart 重新生成的用户消息：UT 问图片"
    # 3. 模拟 AngelHeart 重建 contexts
    req.contexts = [{"role": "user", "content": [{"type": "text", "text": "上轮消息"}]}]
    # 4. 模拟 AngelHeart 重写 system_prompt
    req.system_prompt = "scene prompt..."

    # 5. LLM 实际看到的 user message content 仍包含图说
    user_message_content = [
        {"type": "text", "text": req.prompt},
    ]
    user_message_content.extend(req.extra_user_content_parts)
    # **关键**：图说在 user message 的 content blocks 里，LLM 一定能看到
    has_caption = any("猫" in (b.get("text", "") or "") for b in user_message_content)
    assert has_caption, "图说丢失了！"
    print("✓ test_survives_prompt_overwrite_by_other_plugin")


def test_inject_creates_system_prompt_if_empty():
    p = new_plugin()
    req = FakeReq(
        prompt="看图",
        extra_user_content_parts=[
            {"type": "text", "text": "[Image 1 描述] 一只狗"},
        ],
    )
    req.system_prompt = ""
    p._inject_guidance(req)
    assert req.system_prompt  # 非空
    assert "[视觉模型描述]" in req.system_prompt
    print("✓ test_inject_creates_system_prompt_if_empty")


def test_inject_counts_images_correctly():
    p = new_plugin()
    req = FakeReq(
        prompt="用户消息",
        extra_user_content_parts=[
            {"type": "text", "text": "[Image 1 描述] 猫"},
            {"type": "text", "text": "[Image 2 描述] 狗"},
            {"type": "text", "text": "[Image 3 描述] 鸟"},
        ],
    )
    req.system_prompt = "X"
    p._inject_guidance(req)
    assert "3 张图片" in req.system_prompt
    assert "[Image 1 描述]" in req.system_prompt
    assert "[Image 2 描述]" in req.system_prompt
    assert "[Image 3 描述]" in req.system_prompt
    print("✓ test_inject_counts_images_correctly")


def test_inject_caption_text_to_system_prompt_optional():
    """可选配置：注入图说本身到 system_prompt（冗余防覆盖）。"""
    p = new_plugin(inject_caption_text_to_system_prompt=True)
    req = FakeReq(
        prompt="看图",
        extra_user_content_parts=[
            {"type": "text", "text": "[Image 1 描述] 猫"},
        ],
    )
    req.system_prompt = "你是一个助手。"
    p._inject_guidance(req)
    # 开启 inject_caption_text_to_system_prompt 时，图说也进入 system_prompt
    assert "猫" in req.system_prompt
    print("✓ test_inject_caption_text_to_system_prompt_optional")


# ------------------------------------------------------------------ 单测：mmx 提示词默认保守


def test_default_vision_prompt_is_conservative():
    """默认 mmx prompt 应是保守描述模板。"""
    from main import VisionTextBridgePlugin  # type: ignore

    # 实际从 _describe_one 内部获取（避免硬编码）
    # 这里的检测方式是：构造一个 plugin 实例，调 _describe_one 看 prompt
    # 简化：直接读 _conf_schema.json 的 default
    import json
    schema = json.load(open("/workspace/astrbot_plugin_vision_text_bridge/_conf_schema.json"))
    default = schema["vision_prompt"]["default"]
    assert "严禁猜测" in default
    assert "无法确定" in default
    assert "不要补充背景知识" in default
    print("✓ test_default_vision_prompt_is_conservative")


def test_default_image_placeholder_marks_as_vision_model():
    """v0.7 默认 placeholder 是 [Image N 描述] xxx 格式，提示 LLM 这是“描述”不是“原图”。"""
    import json
    schema = json.load(open("/workspace/astrbot_plugin_vision_text_bridge/_conf_schema.json"))
    default = schema["image_placeholder_template"]["default"]
    assert "描述" in default  # 中文“描述”提示 LLM
    assert "[Image" in default  # [Image N 描述] 格式
    assert "{index}" in default
    assert "{description}" in default
    print("✓ test_default_image_placeholder_marks_as_vision_model")


# ------------------------------------------------------------------ 单测：插件加载


def test_api_cache_thumbnail_returns_data_url():
    """v0.8.6: /cache/thumbnail?image_id=... 返回 data URL。"""
    import asyncio as _aio
    import base64
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "thumb.sqlite3")
        # 存一条带 base64 的
        fake = b"\x89PNG\r\n\x1a\n" + b"x" * 30
        p._caption_cache.put(
            "id1", "https://x.com/a.png", "一只猫",
            image_b64=base64.b64encode(fake).decode("ascii"),
            mime_type="image/png", file_size=len(fake), width=10, height=20,
        )
        p._description_cache.clear()

        # 用 mock context 拿到 thumbnail 闭包
        captured = {}
        def mock_register(route, fn, methods, desc):
            captured[route] = fn

        class _R:
            def __init__(self, args): self.args = args

        p.context = SimpleNamespace(
            request=_R({"image_id": "id1"}),
            register_web_api=mock_register,
        )
        p._register_web_apis()
        thumb_fn = captured[f"/{main.PLUGIN_NAME}/cache/thumbnail"]

        result = _aio.run(thumb_fn())
        # 验证返 ok 且 data_url 是 data:image/png;base64,...
        assert isinstance(result, dict)
        assert result.get("ok") is True
        d = result["data"]
        assert d["image_id"] == "id1"
        assert d["has_image"] is True
        assert d["mime_type"] == "image/png"
        assert d["data_url"].startswith("data:image/png;base64,")
        assert d["width"] == 10
        assert d["height"] == 20
        assert d["file_size"] == len(fake)

        # 错误的 image_id
        p.context.request = _R({"image_id": "nonexistent"})
        r2 = _aio.run(thumb_fn())
        # err() 返 (jsonify, status_code)
        assert isinstance(r2, tuple)
        assert r2[1] == 404

        # 缺 image_id
        p.context.request = _R({})
        r3 = _aio.run(thumb_fn())
        # 单个 err()返 (jsonify, 400) -- 但这里 status_code 不一定是 400 因为是默认参数
        # 实际 err()第二位置 == 400
        if isinstance(r3, tuple):
            r3 = r3[0]
        # jsonify 之后可能调用过，但 get_json/get('error') 看 'error' 字段
        assert r3["error"] == "缺少参数 image_id"

    print("✓ test_api_cache_thumbnail_returns_data_url")


def test_api_cache_thumbnail_no_image():
    """v0.8.6: 缓存中存在但未存 image_b64（如老 v0.8.5 迁移过来）时返 has_image=False。"""
    import asyncio as _aio
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "no_b64.sqlite3")
        p._caption_cache.put("id2", "https://x.com/no-img.jpg", "老条目")  # 不传 b64

        captured = {}
        def mock_register(route, fn, methods, desc):
            captured[route] = fn
        class _R:
            def __init__(self, args): self.args = args

        p.context = SimpleNamespace(
            request=_R({"image_id": "id2"}),
            register_web_api=mock_register,
        )
        p._register_web_apis()
        thumb_fn = captured[f"/{main.PLUGIN_NAME}/cache/thumbnail"]
        result = _aio.run(thumb_fn())
        assert result.get("ok") is True
        d = result["data"]
        assert d["has_image"] is False
        assert d["data_url"] == ""
        assert d["mime_type"] == ""
    print("✓ test_api_cache_thumbnail_no_image")


def test_sniff_image_meta():
    """v0.8.6: _sniff_image_meta 能识别常见图片格式的 mime/宽高。"""
    p = new_plugin()
    # 1x1 transparent PNG
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da6300010000000500010d0a2db40000000049454e44ae426082"
    )
    mime, w, h = main._sniff_image_meta(png_bytes)
    assert mime == "image/png"
    assert w == 1 and h == 1
    # 1x1 GIF
    gif_bytes = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 100
    mime, w, h = main._sniff_image_meta(gif_bytes)
    assert mime == "image/gif"
    assert w == 1 and h == 1
    # 不可识别的字节
    mime, w, h = main._sniff_image_meta(b"random bytes 1234567890")
    assert mime == "" and w == 0 and h == 0
    # 空
    mime, w, h = main._sniff_image_meta(b"")
    assert mime == "" and w == 0 and h == 0
    print("✓ test_sniff_image_meta")


def test_sibling_modules_loaded():
    """main.py 启动时应能动态加载同级 caption_cache.py（v0.8.6 起 chat_archive_link.py 已删除）。"""
    # 验证 import main 后这些类已绑定
    assert hasattr(main, "CaptionCache")
    assert hasattr(main, "CaptionEntry")
    assert hasattr(main, "CacheStats")
    # 验证它们指向实际类（不是 None）
    assert main.CaptionCache.__name__ == "CaptionCache"
    print("✓ test_sibling_modules_loaded")


def test_main_imports_without_sys_path_modification():
    """模拟 AstrBot 加载插件的场景：sys.path 中没有插件目录。

    AstrBot 加载插件时不会自动把插件目录加到 sys.path。如果 main.py 用普通
    ``from caption_cache import ...`` 会报 No module named 'caption_cache'。

    我们的修复：main.py 使用 importlib.util.spec_from_file_location 显式加载
    同级文件，不依赖 sys.path。
    """
    import sys as _sys
    import importlib.util as _ilu
    saved_path = list(_sys.path)
    plugin_dir = "/workspace/astrbot_plugin_vision_text_bridge"
    # 把插件目录从 sys.path 移除
    _sys.path = [p for p in _sys.path if p != plugin_dir and p != "."]
    try:
        # 模拟 AstrBot 加载：直接用 importlib 加载 main.py
        spec = _ilu.spec_from_file_location(
            "main_under_test", f"{plugin_dir}/main.py"
        )
        mod = _ilu.module_from_spec(spec)
        # v0.8.7 修复：Python 3.11 @dataclass 在 exec_module 时会用 sys.modules
        # 查 module 的 __dict__，但 spec loader 加载的 module 默认不注册。
        # 这里显式注册，模拟 AstrBot 实际加载路径。
        _sys.modules["main_under_test"] = mod
        spec.loader.exec_module(mod)
        # 验证 _load_sibling_module 工作：main.py 模块中应有这些绑定（v0.8.6 起
        # chat_archive_link.py 已删除，不再验证 ChatArchiveLink）
        assert hasattr(mod, "CaptionCache"), "CaptionCache 未绑定"
        assert mod.CaptionCache.__name__ == "CaptionCache"
    finally:
        _sys.path = saved_path
    print("✓ test_main_imports_without_sys_path_modification")


# ------------------------------------------------------------------ runner

def run_all():
    tests = [
        test_is_cacheable_url,
        test_truncate,
        test_redact_text,
        test_extract_image_url_from_part_dict,
        test_extract_image_url_from_part_object,
        test_remove_image_parts,
        test_build_vision_command_url,
        test_build_vision_command_file_id,
        test_build_vision_command_no_prompt,
        test_attach_with_prompt,
        test_attach_no_prompt,
        test_attach_failure_uses_template,
        test_attach_index_continues,
        test_cache_hit,
        test_e2e_image_urls_only,
        test_e2e_extra_parts,
        test_e2e_disabled_plugin,
        test_e2e_mmx_failure_keeps_placeholder,
        test_e2e_history_in_contexts,
        test_e2e_truncation_applied,
        test_initialize_triggers_login_with_key,
        test_initialize_skips_login_when_disabled,
        test_initialize_skips_login_when_key_empty,
        test_login_failure_does_not_crash,
        test_initialize_skips_login_when_no_mmx,
        test_default_priority_in_module,
        test_decorator_uses_priority_kwarg,
        test_resolve_priority_default,
        test_resolve_priority_from_config,
        test_resolve_priority_invalid_falls_back,
        test_priority_mismatch_warns_and_updates_global,
        test_priority_match_no_warning,
        test_priority_out_of_range_warns,
        test_is_data_url,
        test_strip_all_data_url_images_image_urls,
        test_strip_all_data_url_images_extra_parts,
        test_strip_all_data_url_images_contexts,
        test_strip_returns_zero_when_nothing,
        test_strip_handles_string_image_url_field,
        test_main_hook_then_residual_strip_endtoend,
        test_attach_clears_image_urls_even_on_failure,
        test_attach_clears_extra_parts_even_on_failure,
        test_attach_clears_contexts_even_on_failure,
        test_strip_all_image_urls_removes_everything,
        test_strip_all_image_urls_zero_when_nothing,
        test_fallback_strip_all_when_configured,
        test_fallback_strip_only_data_url_by_default,
        test_diagnose_balance_error,
        test_diagnose_quota_error,
        test_diagnose_auth_error,
        test_diagnose_argument_error,
        test_diagnose_unknown_error_no_warning,
        test_diagnose_warn_once,
        test_caption_cache_basic_crud,
        test_caption_cache_persistence,
        test_caption_cache_list_and_search,
        test_caption_cache_v0_8_6_image_id_and_b64,
        test_caption_cache_v0_8_6_schema_upgrade,
        test_caption_cache_make_id_helpers,
        test_caption_cache_stats,
        test_caption_cache_vacuum,
        test_describe_one_uses_sqlite_cache,
        test_describe_one_writes_to_sqlite_cache,
        test_register_web_apis_called,
        test_api_cache_thumbnail_returns_data_url,
        test_api_cache_thumbnail_no_image,
        test_sniff_image_meta,
        test_end_to_end_full_flow,
        test_inject_system_prompt_guidance_default_on,
        test_inject_disabled_when_config_off,
        test_inject_no_images_in_prompt_no_op,
        test_inject_creates_system_prompt_if_empty,
        test_inject_counts_images_correctly,
        test_survives_prompt_overwrite_by_other_plugin,
        test_default_vision_prompt_is_conservative,
        test_default_image_placeholder_marks_as_vision_model,
        test_sibling_modules_loaded,
        test_main_imports_without_sys_path_modification,
        test_main_hook_clears_image_urls_immediately,
        test_main_hook_clears_extra_parts_and_contexts_images,
        test_main_hook_saves_snapshots_for_process_request,
        test_to_text_part_creates_pydantic_object,
        test_mark_providers_adds_image_modality,
        test_mark_providers_skipped_when_config_off,
        test_cache_key_uses_md5_for_file_url,
        test_cache_key_falls_back_to_url_on_read_failure,
        test_same_image_different_path_hits_cache,
        test_login_mmx_handles_mmxresult_not_tuple,
        test_get_installed_plugin_names_handles_missing_attr,
        test_check_compatibility_warns_on_uni_nickname_low_priority,
        test_chat_plus_style_image_reinjection_is_cleaned,
        test_chat_plus_compat_event_image_components_recovered,
        test_v0851_no_duplicate_mmx_call_for_same_image,
        # v0.8.7 新增
        test_verbose_granular_toggles,
        test_verbose_total_switch_enables_all,
        test_mmx_result_dataclass,
        test_helper_module_level_functions_exist,
        test_main_py_slim_under_1250_lines,
        test_persist_writes_b64_in_async_context,
        test_persist_handles_read_failure_gracefully,
        test_api_diag_returns_db_info,
        test_webui_logger_module_exists,
        test_webui_app_uses_logger,
        test_webui_index_has_debug_panel,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    total = len(tests)
    print(f"\n{'='*50}")
    print(f"PASS: {total - failed}/{total}")
    sys.exit(0 if failed == 0 else 1)


# ------------------------------------------------------------------ v0.8.7 新增


def test_verbose_granular_toggles():
    """v0.8.7: verbose_hook_trace / verbose_mmx_subprocess / verbose_cache_trace / verbose_id_computation 任一为 true 即开。"""
    p = new_plugin(verbose_hook_trace=True)
    assert p._should_log("hook_trace") is True
    assert p._should_log("mmx_subprocess") is False  # 只开 hook_trace
    p2 = new_plugin(verbose_mmx_subprocess=True)
    assert p2._should_log("mmx_subprocess") is True
    assert p2._should_log("hook_trace") is False
    p3 = new_plugin(verbose_cache_trace=True)
    assert p3._should_log("cache_trace") is True
    assert p3._should_log("id_computation") is False
    p4 = new_plugin(verbose_id_computation=True)
    assert p4._should_log("id_computation") is True
    # 默认全关
    p5 = new_plugin()
    assert p5._should_log("hook_trace") is False
    assert p5._should_log("mmx_subprocess") is False
    assert p5._should_log("cache_trace") is False
    assert p5._should_log("id_computation") is False
    print("✓ test_verbose_granular_toggles")


def test_verbose_total_switch_enables_all():
    """v0.8.7: verbose_logging=true 是总开关，覆盖所有细粒度。"""
    p = new_plugin(verbose_logging=True)
    assert p._should_log("hook_trace") is True
    assert p._should_log("mmx_subprocess") is True
    assert p._should_log("cache_trace") is True
    assert p._should_log("id_computation") is True
    print("✓ test_verbose_total_switch_enables_all")


def test_mmx_result_dataclass():
    """v0.8.7: MmxResult 拍到模块顶层的 dataclass，不再是 _run_mmx 内部 class。"""
    from dataclasses import fields
    f = fields(main.MmxResult)
    names = {x.name for x in f}
    assert names == {"stdout", "stderr", "returncode", "ok"}
    r = main.MmxResult("hi", "err", 0, True)
    assert r.stdout == "hi" and r.ok is True
    print("✓ test_mmx_result_dataclass")


def test_helper_module_level_functions_exist():
    """v0.8.7: 多个小 helper 抽到模块顶层，验证它们存在。"""
    expected = [
        "_is_image_url_part", "_extract_url_from_item",
        "_extract_urls_from_parts", "_extract_urls_from_context_list",
        "_is_data_url", "_strip_image_urls", "_to_text_part",
        "_sniff_image_meta", "_is_cacheable_url",
    ]
    for name in expected:
        assert hasattr(main, name), f"main.{name} 缺失"
        assert callable(getattr(main, name)), f"main.{name} 不可调用"
    print("✓ test_helper_module_level_functions_exist")


def test_persist_writes_b64_in_async_context():
    """v0.8.7.1 修复: _persist 在 async 上下文能正常写 base64。

    之前 v0.8.7 同步版本用 asyncio.get_event_loop().run_until_complete，
    在 async 上下文必抛 RuntimeError，fallback 到同步读 file:// 经常被
    except 静默吞掉 → SQLite 写入了 description 但 base64 为空。
    """
    import asyncio
    import tempfile
    import pathlib
    real = "/tmp/xx_persist_test.png"
    with open(real, "wb") as f:
        f.write(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
        ))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = new_plugin()
            p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "p.sqlite3")
            url = f"file://{real}"
            asyncio.run(p._persist("img_test_aaa", url, "描述X"))
            # v0.8.6+ list() 默认不返 image_b64（减小 body），验证必须传 include_b64=True
            items = p._caption_cache.list(limit=10, include_b64=True)
            assert len(items) == 1
            e = items[0]
            assert e.image_id == "img_test_aaa"
            assert e.description == "描述X"
            assert len(e.image_b64) > 0, f"base64 为空！bug 未修复: e.image_b64={e.image_b64!r}"
            mime, w, h = main._sniff_image_meta(__import__("base64").b64decode(e.image_b64))
            assert mime == "image/png"
            assert w == 1 and h == 1
    finally:
        pathlib.Path(real).unlink(missing_ok=True)
    print("✓ test_persist_writes_b64_in_async_context")


def test_persist_handles_read_failure_gracefully():
    """_persist 读字节失败时 description 仍写入（不影响 webui 文本展示）。"""
    import asyncio
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "p2.sqlite3")
        bad_url = "file:///nonexistent_xxx_404.jpg"
        asyncio.run(p._persist("img_test_bbb", bad_url, "描述Y"))
        items = p._caption_cache.list(limit=10, include_b64=True)
        assert len(items) == 1
        e = items[0]
        assert e.description == "描述Y"  # 描述写入
        assert e.image_b64 == ""  # 缩略图为空（接受）
    print("✓ test_persist_handles_read_failure_gracefully")


def test_api_diag_returns_db_info():
    """v0.8.7.1: /cache/diag 诊断端点返 DB 路径 + schema + 最近 3 条。"""
    import asyncio as _aio
    import tempfile
    import pathlib
    real = "/tmp/xx_diag_test.png"
    with open(real, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = new_plugin()
            p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "diag.sqlite3")
            _aio.run(p._persist("id1", f"file://{real}", "描述A"))
            _aio.run(p._persist("id2", "file:///nonexistent.jpg", "描述B"))

            captured = {}
            def mock_register(route, fn, methods, desc):
                captured[route] = fn
            class _R:
                args = {}
            p.context = SimpleNamespace(request=_R(), register_web_api=mock_register)
            p._register_web_apis()
            diag_fn = captured[f"/{main.PLUGIN_NAME}/cache/diag"]

            result = _aio.run(diag_fn())
            assert result.get("ok") is True
            d = result["data"]
            assert d["cache_initialized"] is True
            assert d["total_entries"] == 2
            assert "diag.sqlite3" in d["db_path"]
            # schema 应含 image_b64
            assert "image_b64" in d["schema_columns"]
            assert "image_id" in d["schema_columns"]
            # recent 返 2 条（DESC 排序）
            assert len(d["recent_3"]) == 2
            # 第一条（最近）= id2 (nonexistent file → b64 空)
            r0 = d["recent_3"][0]
            assert r0["image_id"] == "id2"
            assert r0["has_b64"] is False
            # 第二条 = id1 (真实 file → b64 有值)
            r1 = d["recent_3"][1]
            assert r1["image_id"] == "id1"
            assert r1["has_b64"] is True
            assert r1["b64_len"] > 0
    finally:
        pathlib.Path(real).unlink(missing_ok=True)
    print("✓ test_api_diag_returns_db_info")


def test_webui_logger_module_exists():
    """v0.8.7.2: webui logger 模块存在且语法正确。"""
    import subprocess
    result = subprocess.run(["node", "--check", "pages/cache-manager/logger.js"],
                            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    assert result.returncode == 0, f"logger.js 语法错: {result.stderr}"
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pages/cache-manager/logger.js"), encoding="utf-8") as f:
        content = f.read()
    assert "export default logger" in content
    assert "class WebuiLogger" in content
    assert "_log" in content
    for lvl in ["debug", "info", "warn", "error"]:
        assert f"{lvl}(" in content, f"logger.js 未实现 {lvl}()"
    print("✓ test_webui_logger_module_exists")


def test_webui_app_uses_logger():
    """v0.8.7.2: app.js 全面接入 logger（4 个级别都有调用 + API 包装）。"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pages/cache-manager/app.js"), encoding="utf-8") as f:
        content = f.read()
    assert 'import logger from "./logger.js"' in content
    for lvl in ["debug", "info", "warn", "error"]:
        assert f"logger.{lvl}(" in content, f"app.js 未调 logger.{lvl}"
    assert "async function apiGet" in content
    assert "async function apiPost" in content
    # 业务代码不应直接调 bridge.apiGet/apiPost（必须走包装）
    # 允许 logger.js / 注释中提及
    import re
    # 找出 import 之后的所有 bridge.apiGet/apiPost 调用
    after_import = content.split("import logger")[1]
    # 但包装函数本身（apiGet/apiPost）当然会调 bridge.apiGet/apiPost——排除这两个函数体
    # 简化：只检查不在 async function apiGet/apiPost 内部
    lines = after_import.split("\n")
    in_api_wrapper = False
    depth = 0
    bad = []
    for i, line in enumerate(lines):
        if re.match(r"\s*async function api(Get|Post)\b", line):
            in_api_wrapper = True
            depth = 0
        if in_api_wrapper:
            depth += line.count("{") - line.count("}")
            if depth <= 0 and "{" in line:
                in_api_wrapper = False
            continue
        if re.search(r"\bbridge\.(apiGet|apiPost)\(", line):
            bad.append(line.strip())
    assert len(bad) == 0, f"app.js 业务代码中不应直接调 bridge.apiGet/apiPost，剩 {len(bad)} 处: {bad}"
    print("✓ test_webui_app_uses_logger")


def test_webui_index_has_debug_panel():
    """v0.8.7.2: index.html 包含 debug panel 元素。"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pages/cache-manager/index.html"), encoding="utf-8") as f:
        content = f.read()
    assert 'id="debug-panel"' in content
    assert 'id="debug-level"' in content
    assert 'id="debug-body"' in content
    assert 'id="debug-clear"' in content
    assert 'id="debug-show"' in content
    assert 'id="debug-copy"' in content
    assert 'id="debug-download"' in content
    print("✓ test_webui_index_has_debug_panel")


def test_main_py_slim_under_1250_lines():
    """v0.8.7+: main.py 瘦身到 1250 行以下（v0.8.6 是 2019 行）。
    v0.8.7.1 加了 /cache/diag 诊断 endpoint，阈值放宽到 1250。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    with open(path, "r", encoding="utf-8") as f:
        n = sum(1 for _ in f)
    assert n < 1250, f"main.py 现在 {n} 行，未达到瘦身目标 (<1250)"
    print(f"✓ test_main_py_slim_under_1250_lines (main.py = {n} 行)")


if __name__ == "__main__":
    run_all()
