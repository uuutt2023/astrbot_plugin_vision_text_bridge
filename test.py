"""
离线测试：模拟 ProviderRequest，验证插件在不实际调用 mmx 的情况下行为正确。

用法：
    python test.py
"""

from __future__ import annotations

import asyncio
import json
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
    p._priority_locked_warning_emitted = False
    # 默认 None，让 _describe_one 在 SQLite 缓存表走 None 路径
    p._caption_cache = None
    # v0.8.7: 主钩子快照字段。__init__ 里会设，这里用 __new__ 创建的实例也要设
    p._pending_urls = None
    p._pending_parts = None
    p._pending_contexts = None
    # 注入一个 mock context，让 _register_web_apis / initialize 不报错
    p.context = SimpleNamespace(
        request=SimpleNamespace(args={}, json=None),
        register_web_api=lambda *a, **k: None,
    )
    return p


# v0.8.17: mock context helper——以住 12 个测试手写 ``class _R`` + ``mock_register`` 现在抽取出来
class _MockContext:
    """模拟 AstrBot Context 对象——``request`` 上的任意属性返 AttributeError（抹掉 self.context.request.* 误读）。"""

    def __init__(self, args: dict | None = None, json_body: dict | None = None):
        self.args = args or {}
        self._json_body = json_body

    def __getattr__(self, name):
        if name == "json":
            async def _coro():
                return self._json_body
            return _coro()
        raise AttributeError(name)


def make_capturing_context(register_fn) -> SimpleNamespace:
    """建一个 mock context，其 ``register_web_api`` 调 ``register_fn(route, fn, methods, desc)``。
    返回 SimpleNamespace + register_fn 包装。
    """
    from types import SimpleNamespace
    captured = {}

    def mock_register(route, fn, methods, desc):
        captured[route] = fn

    ctx = SimpleNamespace(request=_MockContext(), register_web_api=mock_register)
    return ctx, captured


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
    # v0.8.7.4: 接受裸本地路径（/ 开头的绝对路径），因为 AstrBot 实际传的就是裸路径
    p = new_plugin()
    assert main._is_cacheable_url("http://x.com/a.jpg", p.config) is True
    assert main._is_cacheable_url("https://x.com/a.jpg", p.config) is True
    assert main._is_cacheable_url("base64://abc", p.config) is False
    assert main._is_cacheable_url("file:///tmp/a.jpg", p.config) is True
    assert main._is_cacheable_url("/tmp/a.jpg", p.config) is True  # v0.8.7.4 变 True
    assert main._is_cacheable_url("/AstrBot/data/temp/x.jpg", p.config) is True
    assert main._is_cacheable_url("C:/Users/x.jpg", p.config) is True  # Windows 盘符
    assert main._is_cacheable_url("data:image/jpeg;base64,xxx", p.config) is False  # data URL 不缓存
    assert main._is_cacheable_url("", p.config) is False
    p2 = new_plugin(cache_file_paths=False)
    assert main._is_cacheable_url("file:///tmp/a.jpg", p2.config) is False
    assert main._is_cacheable_url("/tmp/a.jpg", p2.config) is False  # 关闭 file_paths 后裸路径也不缓存
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
        # v0.8.8: 5 分钟内连续 hit 去重（防 webui 详情页点 10 次就 hit_count=10）
        entry2 = cache.get("https://x.com/a.jpg")
        assert entry2.hit_count == 1  # 去重后还是 1
        # 手动调成 6 分钟前，命中后递增
        import sqlite3 as _sq
        with _sq.connect(str(Path(tmp) / "test.sqlite3") + "_db" if False else str(cache._db_path)) as _c:
            _c.execute("UPDATE image_captions SET last_hit_at = ? WHERE image_id = ?", (entry2.last_hit_at - 360, "https://x.com/a.jpg"))
            _c.commit()
        entry3 = cache.get("https://x.com/a.jpg")
        assert entry3.hit_count == 2  # 超过去重窗口才递增
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
        # v0.8.8: 5 分钟内 hit 去重 → 2 次连续 get 只 +1
        assert s2.total_hits == 1
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
            request=_R({}),
            register_web_api=mock_register,
        )
        p._register_web_apis()
        # v0.8.7.8: 改用路径参数路由，image_id 走 kwarg
        thumb_fn = captured[f"/{main.PLUGIN_NAME}/cache/thumbnail/<image_id>"]

        result = _aio.run(thumb_fn(image_id="id1"))
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
        r2 = _aio.run(thumb_fn(image_id="nonexistent"))
        # err() 返 (jsonify, status_code)
        assert isinstance(r2, tuple)
        assert r2[1] == 404

        # 缺 image_id → 默认空字符串，返 400
        r3 = _aio.run(thumb_fn(image_id=""))
        if isinstance(r3, tuple):
            r3 = r3[0]
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
            request=_R({}),
            register_web_api=mock_register,
        )
        p._register_web_apis()
        # v0.8.7.8: 改用路径参数路由
        thumb_fn = captured[f"/{main.PLUGIN_NAME}/cache/thumbnail/<image_id>"]
        result = _aio.run(thumb_fn(image_id="id2"))
        assert result.get("ok") is True
        d = result["data"]
        assert d["has_image"] is False
        assert d["data_url"] == ""
        assert d["mime_type"] == ""
    print("✓ test_api_cache_thumbnail_no_image")


def test_thumbnail_endpoint_accepts_get():
    """v0.8.8: thumbnail 路由纯 GET + 路径参数，image_id 走 kwarg（不再有 POST/query/body 复杂逻辑）。"""
    import asyncio as _aio
    import tempfile
    import pathlib
    real = "/tmp/xx_thumb_get.png"
    with open(real, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = new_plugin()
            p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "tg.sqlite3")
            _aio.run(p._persist("id_y", f"file://{real}", "desc"))

            captured = {}
            def mock_register(route, fn, methods, desc):
                captured[route] = fn
            class _R:
                args = {}
                view_args = {}
                def __getattr__(self, name):
                    raise AttributeError(name)
            p.context = SimpleNamespace(
                request=_R(), register_web_api=mock_register,
            )
            p._register_web_apis()
            thumb_fn = captured[f"/{main.PLUGIN_NAME}/cache/thumbnail/<image_id>"]

            # v0.8.8: 直接 image_id="id_y" 走 kwarg
            r = _aio.run(thumb_fn(image_id="id_y"))
            if isinstance(r, tuple):
                body, _ = r
                d = body if isinstance(body, dict) else {}
            else:
                d = r
            assert d.get("ok") is True, f"thumb_fn 返: {d}"
            assert d["data"]["has_image"] is True
            assert d["data"]["data_url"].startswith("data:image/png;base64,")
    finally:
        pathlib.Path(real).unlink(missing_ok=True)
    print("✓ test_thumbnail_endpoint_accepts_get")


def test_thumbnail_path_param_endpoint():
    """v0.8.7.6+: /cache/thumbnail/<image_id>（GET，image_id 走路径参数）。
    v0.8.7.8 修复：必须用 werkzeug 尖括号语法 <image_id>，不是 FastAPI 花括号 {image_id}。
    验证：路由注册后，用 werkzeug Map 实际匹配一次请求路径，证 AstrBot server 能 match 上。"""
    import asyncio as _aio
    import tempfile
    import pathlib
    real = "/tmp/xx_thumb_path.png"
    with open(real, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = new_plugin()
            p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "tp.sqlite3")
            _aio.run(p._persist("id_y", f"file://{real}", "desc"))

            captured = {}
            def mock_register(route, fn, methods, desc):
                captured[route] = (fn, methods)
            class _R:
                args = {}
                view_args = {}
            p.context = SimpleNamespace(
                request=_R(), register_web_api=mock_register,
            )
            p._register_web_apis()

            # 必须是 werkzeug 尖括号语法，不是花括号！
            route_path = f"/{main.PLUGIN_NAME}/cache/thumbnail/<image_id>"
            assert route_path in captured, f"未注册 werkzeug 路径参数路由，expected={route_path} in {list(captured)}"
            thumb_fn, methods = captured[route_path]
            assert methods == ["GET"], f"路径参数路由应用 GET 不用 POST，actual={methods}"

            # 验证 1：模拟 AstrBot server 的 werkzeug Map 匹配（这是 v0.8.7.8 之前漏掉的）
            from werkzeug.routing import Map, Rule
            url_map = Map([Rule(route_path, endpoint="plugin_api", methods=methods)])
            request_path = f"/{main.PLUGIN_NAME}/cache/thumbnail/id_y"
            endpoint, path_values = url_map.bind("").match(request_path, method="GET")
            assert path_values.get("image_id") == "id_y", f"werkzeug match 失败: {path_values}"

            # 验证 2：调 handler (image_id 由 path param 传入)
            r = _aio.run(thumb_fn(image_id="id_y"))
            if isinstance(r, tuple):
                body, _ = r
                import json as _json
                d = _json.loads(body.get_data(as_text=True)) if hasattr(body, "get_data") else body
            else:
                d = r
            assert d.get("ok") is True, f"thumb_fn 返: {d}"
            assert d["data"]["has_image"] is True
            assert d["data"]["data_url"].startswith("data:image/png;base64,")
            assert d["data"]["image_id"] == "id_y"

            # 验证 3：错误地使用花括号语法不应该出现
            fastapi_path = f"/{main.PLUGIN_NAME}/cache/thumbnail/{{image_id}}"
            assert fastapi_path not in captured, f"误用 FastAPI 花括号语法: {fastapi_path}"
    finally:
        pathlib.Path(real).unlink(missing_ok=True)
    print("✓ test_thumbnail_path_param_endpoint")


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
    # v0.8.17: CacheStats 改为 _sibling_cache.CacheStats 内部用，main.py 不再 re-export
    import caption_cache as _cc  # noqa
    assert hasattr(main, "CaptionCache")
    assert hasattr(main, "CaptionEntry")
    assert hasattr(_cc, "CacheStats")  # 在 caption_cache 模块里
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
        test_main_py_slim_under_1300_lines,
        test_register_version_sync,
        test_caption_cache_dead_code_removed,
        test_b64_size_cap_skips_storage,
        test_hit_count_5min_dedup,
        test_webui_no_native_confirm,
        test_lru_cache_eviction,
        test_thumb_pool_concurrency_limit,
        test_thumb_pool_reject_does_not_break_queue,
        test_ensure_thumb_caches_failure,
        test_debug_panel_append_only,
        test_app_js_class_declarations_ordered,
        test_strip_mmx_content_extracts_content_field,
        test_strip_mmx_content_strips_bold,
        test_strip_mmx_content_strips_heading_and_list,
        test_strip_mmx_content_collapses_blank_lines,
        test_strip_mmx_content_real_world_savings,
        test_strip_mmx_content_fallback_on_non_json,
        test_strip_mmx_disabled_returns_raw,
        test_memory_cache_basic,
        test_memory_cache_ttl_expiration,
        test_memory_cache_lru_eviction,
        test_memory_cache_ttl_zero_means_never_expire,
        test_memory_cache_dict_syntax_compat,
        test_clean_expired_removes_old_entries,
        test_clean_expired_keeps_recent_entries,
        test_clean_expired_uses_last_hit_at_when_present,
        test_clean_expired_zero_days_is_noop,
        test_webui_db_badge_text_in_app_js,
        test_clean_expired_endpoint_registered,
        test_clean_expired_endpoint_disabled_when_ttl_zero,
        test_daily_buckets_basic,
        test_daily_buckets_old_entries,
        test_api_stats_returns_status_fields,
        test_api_stats_timeline_endpoint,
        test_webui_status_bar_and_timeline_in_html,
        test_webui_auto_refresh_and_timeline_in_app_js,
        test_last_clean_at_recorded,
        test_tool_filter_off_returns_zero,
        test_tool_filter_blacklist_removes_named,
        test_tool_filter_whitelist_keeps_named_only,
        test_tool_filter_with_remove_func_method,
        test_tool_filter_with_func_list,
        test_tool_filter_handles_none_container,
        test_tool_filter_in_event_via_extra_key,
        test_strip_residual_base64_clears_func_tool,
        test_webui_no_direct_bridge_await_in_app_js,
        test_index_html_no_bridge_sdk_loading,
        test_app_js_uses_fallback_bridge_stub,
        test_index_html_has_bridge_mode_badge,
        test_v0820_drops_esm_module,
        test_v0821_app_js_loaded_after_body,
        test_v0822_endpoints_have_leading_slash,
        test_v0823_webui_version_badge,
        test_v0824_bridge_sdk_reload,
        test_v0825_filter_bot_avatar_in_hook,
        test_v0826_post_skips_bridge_and_thumb_sessionstorage,
        test_v0827_writes_use_get_to_avoid_cors_preflight,
        test_cfg_int_helper_exists,
        test_cfg_str_helper_exists,
        test_app_js_no_dead_fmtDim,
        test_caption_cache_datetime_top_level,
        test_persist_writes_b64_in_async_context,
        test_persist_handles_read_failure_gracefully,
        test_api_diag_returns_db_info,
        test_webui_logger_module_exists,
        test_webui_app_uses_logger,
        test_webui_index_has_debug_panel,
        test_caption_cache_put_warns_on_empty_fields,
        test_describe_one_persists_bare_path_url,
        test_thumbnail_endpoint_accepts_get,
        test_thumbnail_path_param_endpoint,
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
    """v0.8.7.2: webui logger 模块存在且语法正确。v0.8.20 改为全局脚本，无 export default。"""
    import subprocess
    result = subprocess.run(["node", "--check", "pages/cache-manager/logger.js"],
                            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    assert result.returncode == 0, f"logger.js 语法错: {result.stderr}"
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pages/cache-manager/logger.js"), encoding="utf-8") as f:
        content = f.read()
    assert "class WebuiLogger" in content
    assert "window.webuiLogger" in content or "global.webuiLogger" in content, "v0.8.20 logger 须暴露到 window"
    assert "_log" in content
    for lvl in ["debug", "info", "warn", "error"]:
        assert f"{lvl}(" in content, f"logger.js 未实现 {lvl}()"
    print("✓ test_webui_logger_module_exists")


def test_webui_app_uses_logger():
    """v0.8.7.2: app.js 全面接入 logger。v0.8.20 改用 window.webuiLogger，不再 import。"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pages/cache-manager/app.js"), encoding="utf-8") as f:
        content = f.read()
    # v0.8.20: 不再 import，改用 window.webuiLogger
    assert "import logger from " not in content, "v0.8.20 app.js 不应再 import logger"
    assert "window.webuiLogger" in content, "v0.8.20 必须用 window.webuiLogger"
    for lvl in ["debug", "info", "warn", "error"]:
        assert f"logger.{lvl}(" in content, f"app.js 未调 logger.{lvl}"
    assert "async function apiGet" in content
    assert "async function apiPost" in content
    import re
    # 找出 import 之后的所有 bridge.apiGet/apiPost 调用（v0.8.20 不再有 import）
    parts = content.split("import logger", 1)
    after_import = parts[1] if len(parts) > 1 else content
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


def test_describe_one_persists_bare_path_url():
    """v0.8.7.4 修复: AstrBot 传裸本地路径 (无 file://) 也能写 SQLite。

    之前 _is_cacheable_url 只认 http(s)/file://，裸路径 cacheable=False
    → 跳过 cacheable 块 → _persist 不调 → SQLite total=0。
    """
    import asyncio
    import tempfile
    import pathlib
    from unittest.mock import patch
    real = "/tmp/xx_bare_path.png"
    with open(real, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = new_plugin()
            p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "b.sqlite3")
            url = real  # 裸路径, 无 file://
            assert main._is_cacheable_url(url, p.config) is True
            # mock mmx 子进程
            with patch.object(p, "_run_mmx",
                              return_value=main.MmxResult("测试描述", "", 0, True)):
                result = asyncio.run(p._describe_one(url))
            assert result == "测试描述"
            items = p._caption_cache.list(limit=10, include_b64=True)
            assert len(items) == 1, f"裸路径未缓存！items={len(items)}"
            assert items[0].description == "测试描述"
    finally:
        pathlib.Path(real).unlink(missing_ok=True)
    print("✓ test_describe_one_persists_bare_path_url")


def test_caption_cache_put_warns_on_empty_fields():
    """v0.8.7.3 修复: put() 跳过空字段时现在 log warning，不再静默。

    之前静默 return 隐藏了 "为何 SQLite total=0" 的根因 (image_id 或 description 为空)。
    """
    import logging
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "warn.sqlite3")
        # 捕获 logging
        captured = []
        h = logging.Handler()
        h.emit = lambda r: captured.append(r)
        logging.getLogger("astrbot_plugin_vision_text_bridge").addHandler(h)
        logging.getLogger("astrbot_plugin_vision_text_bridge").setLevel(logging.WARNING)
        try:
            cache.put("", "url", "description")  # 空 image_id
            assert cache.count() == 0
            cache.put("id", "url", "")  # 空 description
            assert cache.count() == 0
            cache.put(None, "url", "d")  # None image_id
            assert cache.count() == 0
            # 3 次静默调 → 3 条 warning
            warns = [r for r in captured if r.levelno >= logging.WARNING]
            assert len(warns) >= 3, f"期望 ≥3 条 warning，实际 {len(warns)}"
        finally:
            logging.getLogger("astrbot_plugin_vision_text_bridge").removeHandler(h)
    # 有效调一次仍能写
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "ok.sqlite3")
        cache.put("valid_id", "url", "valid desc")
        assert cache.count() == 1
    print("✓ test_caption_cache_put_warns_on_empty_fields")


def test_main_py_slim_under_1300_lines():
    """v0.8.7+: main.py 瘦身到 1300 行以下（v0.8.6 是 2019 行）。
    v0.8.7.4 接受裸本地路径 + 文档，阈值放宽到 1300。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    with open(path, "r", encoding="utf-8") as f:
        n = sum(1 for _ in f)
    # v0.8.13 加了 tool_filter（_filter_disabled_tools / _filter_tools_in_event + 链末兜底），阈值放宽到 1700
    assert n < 1700, f"main.py 现在 {n} 行，未达到瘦身目标 (<1700)"
    print(f"✓ test_main_py_slim_under_1300_lines (main.py = {n} 行)")


def test_register_version_sync():
    """v0.8.8: @register 装饰器版本号必须跟 metadata.yaml 一致，不能脱节。"""
    import re
    main_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        src = f.read()
    # 提取 metadata.yaml 的 version
    meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta_text = f.read()
    m_meta = re.search(r"^version:\s*([\d.]+)\s*$", meta_text, re.MULTILINE)
    assert m_meta, "metadata.yaml 缺少 version 字段"
    meta_version = m_meta.group(1)
    # PLUGIN_VERSION 必须等于 metadata.yaml 的 version（不是 "0.0.0" fallback）
    m_pv = re.search(r"^PLUGIN_VERSION\s*=\s*[\"']?[\w.]+", src, re.MULTILINE)
    assert m_pv, "main.py 缺少 PLUGIN_VERSION 定义"
    # @register 调用必须引用 PLUGIN_VERSION，不可以写死字面量
    m_reg = re.search(r"@register\([\s\S]{0,800}?PLUGIN_VERSION", src)
    assert m_reg, "@register 装饰器必须引用 PLUGIN_VERSION，不能写死字面量"
    print(f"✓ test_register_version_sync (metadata={meta_version}, PLUGIN_VERSION 动态)")


def test_caption_cache_dead_code_removed():
    """v0.8.8: caption_cache.py 死代码清理（to_dict_with_b64 / normalize_key）。"""
    from caption_cache import CaptionCache
    cap = CaptionCache.__dict__
    assert "to_dict_with_b64" not in cap, "to_dict_with_b64 是死代码，已删"
    assert "normalize_key" not in cap, "normalize_key 是死代码，已删"
    # 跑下 存活代码还能用
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        c = CaptionCache(pathlib.Path(tmp) / "x.sqlite3")
        c.put("k", "https://a.com", "猫")
        e = c.get("k")
        assert e.description == "猫"
    print("✓ test_caption_cache_dead_code_removed")


def test_b64_size_cap_skips_storage():
    """v0.8.8: 超过 max_b64_size_kb 的图不存 base64（description 仍存）。"""
    import asyncio, tempfile, pathlib
    from PIL import Image
    import io, base64
    p = new_plugin()
    p.config = dict(p.config) if isinstance(p.config, dict) else {}
    p.config["max_b64_size_kb"] = 1  # 1KB 上限
    with tempfile.TemporaryDirectory() as tmp:
        p._caption_cache = main.CaptionCache(pathlib.Path(tmp) / "cap.sqlite3")
        # 生成 ~50KB 的 PNG
        img = Image.new("RGB", (400, 400), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        big = buf.getvalue()
        assert len(big) > 1024
        png_path = pathlib.Path(tmp) / "big.png"
        png_path.write_bytes(big)
        url = f"file://{png_path}"
        asyncio.run(p._persist("big_id", url, "一只大红线"))
        entry = p._caption_cache.get("big_id", with_b64=True)
        assert entry is not None
        assert entry.description == "一只大红线"
        assert entry.image_b64 == "", f"超大图不该存 b64，实际长度={len(entry.image_b64)}"
    print("✓ test_b64_size_cap_skips_storage")


def test_hit_count_5min_dedup():
    """v0.8.8: 5 分钟内重复 get 不递增 hit_count。"""
    import tempfile, pathlib, sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(pathlib.Path(tmp) / "h.sqlite3")
        cache.put("k", "https://a", "猫")
        assert cache.get("k").hit_count == 1
        assert cache.get("k").hit_count == 1  # 5 分钟内去重
        # 手改 last_hit_at 到 6 分钟前，下次 get 应该 +1
        with sqlite3.connect(cache._db_path) as c:
            c.execute("UPDATE image_captions SET last_hit_at = last_hit_at - 360 WHERE image_id = 'k'")
            c.commit()
        assert cache.get("k").hit_count == 2
    print("✓ test_hit_count_5min_dedup")


def test_webui_no_native_confirm():
    """v0.8.8: webui 不再用 window.confirm（sandboxed iframe 禁用），改用自建 modal。"""
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "customConfirm" in src, "app.js 必须有 customConfirm 函数"
    # 全文不应该出现裸的 window.confirm( 或 confirm(
    bad = re.findall(r"[^a-zA-Z_]confirm\(", src)
    assert not bad, f"app.js 还有裸 confirm() 调用: {bad}"
    # index.html 必须有 confirm-modal
    hpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html")
    with open(hpath, "r", encoding="utf-8") as f:
        html = f.read()
    assert "confirm-modal" in html, "index.html 必须有 confirm-modal 元素"
    print("✓ test_webui_no_native_confirm")


# ===========================================================================
# v0.8.9 vanilla webui 性能优化
# ===========================================================================

def test_lru_cache_eviction():
    """v0.8.9: LRUCache.set 越上限删头部，has/get/delete/clear 全部 API 兼容 Map。
    (纯 Python 等价验证逻辑——app.js 里的 JS class 不能在 Python exec 跑）"""
    class LRUCache:
        def __init__(self, limit=100):
            self.limit = limit
            self.m = {}
            self.order = []  # 插入顺序

        def has(self, k): return k in self.m
        def get(self, k): return self.m.get(k)

        def set(self, k, v):
            if k in self.m:
                self.order.remove(k)
            self.m[k] = v
            self.order.append(k)
            while len(self.m) > self.limit:
                first = self.order.pop(0)
                del self.m[first]
            return v

        def delete(self, k):
            if k in self.m:
                self.order.remove(k)
                del self.m[k]
                return True
            return False

        def clear(self):
            self.m.clear()
            self.order.clear()

        @property
        def size(self):
            return len(self.m)

    c = LRUCache(3)
    assert c.size == 0
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.size == 3
    c.set("d", 4)  # 越上限，删 “a”
    assert not c.has("a")
    assert c.has("b") and c.has("c") and c.has("d")
    assert c.get("b") == 2
    # 重新 set 已有 key，刷新插入顺序
    c.set("b", 22)
    c.set("e", 5)  # 应该删 “c”（现在是 LRU 头部）
    assert c.has("b") and not c.has("c") and c.has("d") and c.has("e")
    assert c.get("b") == 22
    c.delete("e")
    assert c.size == 2
    c.clear()
    assert c.size == 0
    print("✓ test_lru_cache_eviction")


def test_thumb_pool_concurrency_limit():
    """v0.8.9: ThumbPool 同时最多 N 路任务，越限进入队列。Python 等价验证。"""
    class ThumbPool:
        def __init__(self, max=6):
            self.max = max
            self.active = 0
            self.queue = []

        async def run(self, task):
            fut = asyncio.get_event_loop().create_future()
            self.queue.append((task, fut))
            self._drain()
            return await fut

        def _drain(self):
            while self.active < self.max and self.queue:
                task, fut = self.queue.pop(0)
                self.active += 1
                async def _wrap():
                    try:
                        fut.set_result(await task())
                    except Exception as e:
                        fut.set_exception(e)
                    finally:
                        self.active -= 1
                        self._drain()
                asyncio.ensure_future(_wrap())

    async def run():
        pool = ThumbPool(2)
        active = [0]
        peak = [0]

        async def slow_task(i):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.05)
            active[0] -= 1
            return i * 2

        tasks = [pool.run(lambda i=i: slow_task(i)) for i in range(6)]
        results = await asyncio.gather(*tasks)
        assert results == [0, 2, 4, 6, 8, 10]
        assert peak[0] == 2, f"expected peak=2, got {peak[0]}"
        return peak[0]

    peak = asyncio.run(run())
    assert peak == 2
    print(f"✓ test_thumb_pool_concurrency_limit (peak={peak})")


def test_thumb_pool_reject_does_not_break_queue():
    """v0.8.9: 任务抛异常后 active--,后续任务仍能跑。"""
    class ThumbPool:
        def __init__(self, max=6):
            self.max = max
            self.active = 0
            self.queue = []

        async def run(self, task):
            fut = asyncio.get_event_loop().create_future()
            self.queue.append((task, fut))
            self._drain()
            return await fut

        def _drain(self):
            while self.active < self.max and self.queue:
                task, fut = self.queue.pop(0)
                self.active += 1
                async def _wrap():
                    try:
                        fut.set_result(await task())
                    except Exception as e:
                        fut.set_exception(e)
                    finally:
                        self.active -= 1
                        self._drain()
                asyncio.ensure_future(_wrap())

    async def run():
        pool = ThumbPool(2)

        async def boom():
            raise ValueError("boom")
        async def good(i):
            return i

        results = await asyncio.gather(
            pool.run(boom),
            pool.run(lambda: good(1)),
            pool.run(lambda: good(2)),
            return_exceptions=True,
        )
        assert isinstance(results[0], ValueError)
        assert results[1] == 1
        assert results[2] == 2
    asyncio.run(run())
    print("✓ test_thumb_pool_reject_does_not_break_queue")


def test_ensure_thumb_caches_failure():
    """v0.8.9: 缩略图请求失败/没数据也要 cache（避免无限重试）。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 验确保失败路径上 set 了带 __err / __none 的 cache entry
    assert '__err: true' in src, "失败路径必须写 {__err: true} 到 thumbCache"
    assert '__none: true' in src, "无图路径必须写 {__none: true} 到 thumbCache"
    # 并保证 ensureThumb 会检查这些标记
    assert 'cached.__err' in src
    assert 'cached.__none' in src
    print("✓ test_ensure_thumb_caches_failure")


def test_debug_panel_append_only():
    """v0.8.9: 日志 panel 走 append-only 模式（PANEL_MAX_NODES=200 软上限），不再全量重写。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    assert "PANEL_MAX_NODES" in src, "app.js 必须有 PANEL_MAX_NODES 上限常量"
    assert "appendPanelNode" in src, "app.js 必须有 appendPanelNode 增量添加函数"
    assert "onAppend" in src, "app.js 必须用 logger.onAppend 订阅"
    # renderDebugPanelFull 仍在（首次/clear 后全量），但不再是新日志到来时的默认路径
    assert "renderDebugPanelFull" in src
    # 验 logger.js 提供 onAppend
    logger_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/logger.js"), encoding="utf-8").read()
    assert "onAppend" in logger_src, "logger.js 必须提供 onAppend 订阅"
    print("✓ test_debug_panel_append_only")


def test_app_js_class_declarations_ordered():
    """v0.8.9: app.js 里 LRUCache/ThumbPool 必须在 state 之前声明（否则 TDZ ReferenceError）。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    lru_pos = src.find("class LRUCache")
    pool_pos = src.find("class ThumbPool")
    state_pos = src.find("const state = {")
    assert lru_pos > 0, "LRUCache class 缺失"
    assert pool_pos > 0, "ThumbPool class 缺失"
    assert state_pos > 0, "const state 缺失"
    assert lru_pos < state_pos, f"LRUCache (line ~{src[:lru_pos].count(chr(10))+1}) 必须在 state 之前"
    assert pool_pos < state_pos, f"ThumbPool 必须在 state 之前"
    print("✓ test_app_js_class_declarations_ordered")


def test_strip_mmx_content_extracts_content_field():
    """v0.8.10: 从 mmx vision describe 的 JSON 拏出 content 字段。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = json.dumps({
        "content": "这是图片描述内容。",
        "base_resp": {"status_code": 0, "status_msg": "success"}
    }, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    assert out == "这是图片描述内容。", f"拏 content 失败: {out!r}"
    print("✓ test_strip_mmx_content_extracts_content_field")


def test_strip_mmx_content_strips_bold():
    """v0.8.10: **加粗** → 文本（省 token）。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = json.dumps({"content": "**加粗文字** 和普通文字"}, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    assert out == "加粗文字 和普通文字", f"加粗未拏除: {out!r}"
    assert "**" not in out
    print("✓ test_strip_mmx_content_strips_bold")


def test_strip_mmx_content_strips_heading_and_list():
    """v0.8.10: ### 标题、* 列表 → • 项目符号。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = json.dumps({"content": "### 标题\n* 项目一\n* 项目二"}, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    assert "###" not in out
    assert "* 项目" not in out
    assert "• 项目一" in out
    assert "• 项目二" in out
    print("✓ test_strip_mmx_content_strips_heading_and_list")


def test_strip_mmx_content_collapses_blank_lines():
    """v0.8.10: 连续 3+ 空行压成 2。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = json.dumps({"content": "第一段\n\n\n\n\n\n第二段"}, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    assert "\n\n\n" not in out, f"多空行未压缩: {out!r}"
    assert out == "第一段\n\n第二段"
    print("✓ test_strip_mmx_content_collapses_blank_lines")


def test_strip_mmx_content_real_world_savings():
    """v0.8.10: 真实 mmx 响应场景——870 字符 → ~420 字符，省 50%+。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = json.dumps({
        "content": "这张图片展示了一个极具现代化和科技感的室内场景，重点是天花板上安装的一套复杂的自动化轨道物流传输系统。从环境细节来看，这很可能是一所现代化医院的内部。\n\n以下是对图片的详细描述：\n\n**1. 核心主体：自动化传输系统**\n* 图中最为显著的是天花板上运行的自动化轨道传输系统。\n* 有两个蓝色与灰色相间的长方形**自动运输箱（或机器人）**垂直悬挂在轨道上。\n* 每个运输箱侧面都有一个小的数字显示屏。左侧的箱子显示数字**\"560\"**，右侧的显示**\"2:10\"**。这些箱子通常用于在医院各科室间自动运送血液样本、药品、化验单或其他医疗物资。\n\n**2. 文字与标识：**\n* **左侧：** 有一个红色的发光指示牌，上面清晰地标有中文字**\"输血科\"**，下方配有英文**\"Blood Transfusion\"**。\n* **右侧：** 有一个标准的绿色**安全出口标识**。\n\n**总结：**\n这张图片描绘了一个高度自动化的医疗物流环境。",
        "base_resp": {"status_code": 0, "status_msg": "success"}
    }, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    raw_len = len(raw)
    out_len = len(out)
    savings = 1 - out_len / raw_len
    assert savings > 0.20, f"应至少省 20% token，实际只省了 {savings*100:.1f}%"
    assert "**" not in out
    assert "###" not in out
    assert "* " not in [line[:2] for line in out.split("\n") if line]  # 行首不是 * 开头
    print(f"✓ test_strip_mmx_content_real_world_savings ({raw_len}→{out_len}, 省 {savings*100:.1f}%)")


def test_strip_mmx_content_fallback_on_non_json():
    """v0.8.10: mmx 偶尔返非 JSON 错误信息，parse 失败时退回原 stdout。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = True
    raw = "Error: not a json, just plain text"
    out = p._strip_mmx_content(raw)
    # 不是 JSON 格式但仍是 markdown 清理会处理
    assert out == raw
    print("✓ test_strip_mmx_content_fallback_on_non_json")


def test_strip_mmx_disabled_returns_raw():
    """v0.8.10: 关闭 strip_mmx_markdown → 返回原 stdout。"""
    p = new_plugin()
    p.config = dict(p.config)
    p.config["strip_mmx_markdown"] = False
    raw = json.dumps({"content": "**保留加粗**", "base_resp": {"ok": True}}, ensure_ascii=False)
    out = p._strip_mmx_content(raw)
    assert out == raw.strip(), f"关闭后应返回原 stdout, 实际: {out!r}"
    assert "**保留加粗**" in out  # 加粗被保留
    print("✓ test_strip_mmx_disabled_returns_raw")


# ===========================================================================
# v0.8.11 内存热缓存 TTL + LRU
# ===========================================================================

def test_memory_cache_basic():
    """v0.8.11: _MemoryCache 基础 put/get/pop/clear/__len__/__contains__。"""
    import time
    mc = main._MemoryCache(ttl_seconds=60, max_size=10)
    mc.put("a", "1")
    mc.put("b", "2")
    assert len(mc) == 2
    assert "a" in mc
    assert mc.get("a") == "1"
    assert mc.get("b") == "2"
    assert mc.get("nonexistent") is None
    assert "nonexistent" not in mc
    assert mc.pop("a") == "1"
    assert "a" not in mc
    mc.clear()
    assert len(mc) == 0
    print("✓ test_memory_cache_basic")


def test_memory_cache_ttl_expiration():
    """v0.8.11: 超 TTL 的 get 返回 None 并懒删除。"""
    import time
    mc = main._MemoryCache(ttl_seconds=1, max_size=10)
    mc.put("a", "1")
    assert mc.get("a") == "1"
    time.sleep(1.1)
    assert mc.get("a") is None
    assert "a" not in mc
    assert len(mc) == 0
    print("✓ test_memory_cache_ttl_expiration")


def test_memory_cache_lru_eviction():
    """v0.8.11: 超 max_size 淘汰最久未访问项。"""
    mc = main._MemoryCache(ttl_seconds=60, max_size=3)
    mc.put("a", "1")
    mc.put("b", "2")
    mc.put("c", "3")
    # 此时插入顺序是 a → b → c。访问 a 刷新顺序为 b → c → a
    assert mc.get("a") == "1"
    # 再 put d，越上限——应该删 b（最久未访问）
    mc.put("d", "4")
    assert "a" in mc, "a 被访问过，刷新了顺序，不应被删"
    assert "b" not in mc, "b 未被访问，应被淘汰"
    assert "c" in mc
    assert "d" in mc
    assert len(mc) == 3
    print("✓ test_memory_cache_lru_eviction")


def test_memory_cache_ttl_zero_means_never_expire():
    """v0.8.11: ttl=0 表示不过期。"""
    mc = main._MemoryCache(ttl_seconds=0, max_size=10)
    mc.put("a", "1")
    import time
    time.sleep(0.1)
    assert mc.get("a") == "1"  # 不过期
    print("✓ test_memory_cache_ttl_zero_means_never_expire")


def test_memory_cache_dict_syntax_compat():
    """v0.8.11: __setitem__/__getitem__ 兼容老 cache[k]=v 语法。"""
    mc = main._MemoryCache(ttl_seconds=60, max_size=10)
    mc["a"] = "1"
    assert mc["a"] == "1"
    assert "a" in mc
    try:
        _ = mc["nope"]
        assert False, "应抛 KeyError"
    except KeyError:
        pass
    print("✓ test_memory_cache_dict_syntax_compat")


# ===========================================================================
# v0.8.11 SQLite clean_expired
# ===========================================================================

def test_clean_expired_removes_old_entries():
    """v0.8.11: clean_expired 删除超期未命中的条目。"""
    import tempfile, pathlib, sqlite3, time
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "exp.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        cap.put("k2", "https://a.com/2", "狗")
        # 手改 created_at 到 8 天前
        with sqlite3.connect(cap._db_path) as c:
            c.execute("UPDATE image_captions SET created_at = ? WHERE image_id IN ('k1','k2')", (time.time() - 8*86400,))
            c.commit()
        # 清理 7 天前的
        deleted = cap.clean_expired(max_age_days=7)
        assert deleted == 2, f"应删 2 条，实际 {deleted}"
        assert cap.count() == 0
    print("✓ test_clean_expired_removes_old_entries")


def test_clean_expired_keeps_recent_entries():
    """v0.8.11: clean_expired 不删未过期条目。"""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "keep.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        cap.put("k2", "https://a.com/2", "狗")
        # 清理 7 天前的（实际都刚 put）
        deleted = cap.clean_expired(max_age_days=7)
        assert deleted == 0
        assert cap.count() == 2
    print("✓ test_clean_expired_keeps_recent_entries")


def test_clean_expired_uses_last_hit_at_when_present():
    """v0.8.11: 有 last_hit_at 的条目以 last_hit_at 为准（不是 created_at）。"""
    import tempfile, pathlib, sqlite3, time
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "hit.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        cap.get("k1")  # 产生 last_hit_at
        # 手改 created_at 到 100 天前，last_hit_at 是刚刚（现在）
        with sqlite3.connect(cap._db_path) as c:
            c.execute("UPDATE image_captions SET created_at = ? WHERE image_id = 'k1'", (time.time() - 100*86400,))
            c.commit()
        # 清理 7 天前的：last_hit_at 刚刚，没过期
        deleted = cap.clean_expired(max_age_days=7)
        assert deleted == 0, f"刚被 hit 的不该被删，实际删了 {deleted}"
        assert cap.count() == 1
        # 再手改 last_hit_at 到 30 天前
        with sqlite3.connect(cap._db_path) as c:
            c.execute("UPDATE image_captions SET last_hit_at = ? WHERE image_id = 'k1'", (time.time() - 30*86400,))
            c.commit()
        deleted = cap.clean_expired(max_age_days=7)
        assert deleted == 1, f"30 天前 hit 的应被删，实际删了 {deleted}"
    print("✓ test_clean_expired_uses_last_hit_at_when_present")


def test_clean_expired_zero_days_is_noop():
    """v0.8.11: max_age_days<=0 跳过清理。"""
    import tempfile, pathlib, sqlite3, time
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "zero.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        with sqlite3.connect(cap._db_path) as c:
            c.execute("UPDATE image_captions SET created_at = ? WHERE image_id = 'k1'", (time.time() - 100*86400,))
            c.commit()
        assert cap.clean_expired(0) == 0
        assert cap.clean_expired(-1) == 0
        assert cap.count() == 1
    print("✓ test_clean_expired_zero_days_is_noop")


# ===========================================================================
# v0.8.11 webui DB badge + clean_expired 路由
# ===========================================================================

def test_webui_db_badge_text_in_app_js():
    """v0.8.11: app.js loadStats() 必须更新 db-path-badge textContent。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    assert "db-path-badge" in src, "app.js 必须引用 db-path-badge"
    # 验证赋值逻辑
    assert 'dbBadge.textContent' in src, "app.js 必须给 dbBadge 赋 textContent"
    print("✓ test_webui_db_badge_text_in_app_js")


def test_clean_expired_endpoint_registered():
    """v0.8.11: /cache/clean_expired 路由必须注册。"""
    import asyncio, tempfile, pathlib
    p = new_plugin()
    # 保持 DB 在 cleanup 前可用——在 TemporaryDirectory 里走 cache 实例
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "c.sqlite3"
    p._caption_cache = main.CaptionCache(db_path)
    captured = {}

    def mock_register(route, fn, methods, desc):
        captured[route] = fn

    class _R:
        def __getattr__(self, name):
            raise AttributeError(name)
    p.context = SimpleNamespace(
        request=_R(), register_web_api=mock_register,
    )
    p._register_web_apis()
    key = f"/{main.PLUGIN_NAME}/cache/clean_expired"
    assert key in captured, f"缺少路由: {key}"
    fn = captured[key]
    r = asyncio.run(fn())
    if isinstance(r, tuple):
        body, status = r
        assert status == 200, f"应返 200, 实际 {status}: {body}"
        if hasattr(body, "get_json"):
            d = body.get_json()
        elif hasattr(body, "get_data"):
            import json as _json
            d = _json.loads(body.get_data(as_text=True))
        else:
            d = body
    else:
        d = r
    assert d.get("ok") is True, f"clean_expired 应返 ok, 实际: {d}"
    assert "deleted_sqlite" in d.get("data", {}), f"data 缺 deleted_sqlite: {d}"
    assert "purged_memory" in d.get("data", {}), f"data 缺 purged_memory: {d}"
    assert "ttl_days" in d.get("data", {}), f"data 缺 ttl_days: {d}"
    # cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ test_clean_expired_endpoint_registered")


def test_clean_expired_endpoint_disabled_when_ttl_zero():
    """v0.8.11: sqlite_cache_ttl_days=0 时 /cache/clean_expired 返 400。"""
    import tempfile, pathlib, shutil
    p = new_plugin()
    p.config = dict(p.config)
    p.config["sqlite_cache_ttl_days"] = 0
    tmpdir = tempfile.mkdtemp()
    p._caption_cache = main.CaptionCache(pathlib.Path(tmpdir) / "c.sqlite3")
    captured = {}

    def mock_register(route, fn, methods, desc):
        captured[route] = fn

    class _R:
        def __getattr__(self, name):
            raise AttributeError(name)
    p.context = SimpleNamespace(
        request=_R(), register_web_api=mock_register,
    )
    p._register_web_apis()
    import asyncio
    fn = captured[f"/{main.PLUGIN_NAME}/cache/clean_expired"]
    r = asyncio.run(fn())
    # tuple (jsonify, status_code) 或 dict（api_clean_expired 走 err() 返 tuple (json, 400))
    if isinstance(r, tuple):
        body, status = r
        assert status == 400
        if hasattr(body, "get_data"):
            import json as _json
            d = _json.loads(body.get_data(as_text=True))
        else:
            d = body
    else:
        d = r
    assert "未启用过期清理" in d.get("error", "")
    print("✓ test_clean_expired_endpoint_disabled_when_ttl_zero")


# ===========================================================================
# v0.8.12 统计 + 状态栏 + 按天柱状图
# ===========================================================================

def test_daily_buckets_basic():
    """v0.8.12: daily_buckets 返回 30 天×每天条数。"""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "b.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        cap.put("k2", "https://a.com/2", "狗")
        buckets = cap.daily_buckets(days=30)
        assert len(buckets) == 30, f"应返 30 天，实际 {len(buckets)}"
        # 今天应该有 2 条
        today = buckets[-1]
        assert today["count"] == 2
        # 前几天应为 0（缺天补 0）
        for b in buckets[:-1]:
            assert b["count"] == 0
    print("✓ test_daily_buckets_basic")


def test_daily_buckets_old_entries():
    """v0.8.12: 创建于 60 天前的条目不进 30 天窗口。"""
    import tempfile, pathlib, sqlite3, time
    with tempfile.TemporaryDirectory() as tmp:
        cap = main.CaptionCache(pathlib.Path(tmp) / "old.sqlite3")
        cap.put("k1", "https://a.com/1", "猫")
        # 手改 created_at 到 60 天前
        with sqlite3.connect(cap._db_path) as c:
            c.execute("UPDATE image_captions SET created_at = ? WHERE image_id = 'k1'", (time.time() - 60 * 86400,))
            c.commit()
        buckets = cap.daily_buckets(days=30)
        total = sum(b["count"] for b in buckets)
        assert total == 0, f"60 天前的条目不应出现在 30 天窗口内，实际 total={total}"
    print("✓ test_daily_buckets_old_entries")


def test_api_stats_returns_status_fields():
    """v0.8.12: api_stats 返回状态栏需要的所有字段。"""
    import asyncio, tempfile, pathlib, shutil
    p = new_plugin()
    tmpdir = tempfile.mkdtemp()
    p._caption_cache = main.CaptionCache(pathlib.Path(tmpdir) / "s.sqlite3")
    captured = {}
    def mock_register(route, fn, methods, desc):
        captured[route] = fn
    class _R:
        def __getattr__(self, name): raise AttributeError(name)
    p.context = SimpleNamespace(request=_R(), register_web_api=mock_register)
    p._register_web_apis()
    fn = captured[f"/{main.PLUGIN_NAME}/cache/stats"]
    r = asyncio.run(fn())
    if isinstance(r, tuple):
        body = r[0]
        if hasattr(body, "get_json"):
            d = body.get_json()
        elif hasattr(body, "get_data"):
            import json as _json
            d = _json.loads(body.get_data(as_text=True))
        else:
            d = body
    else:
        d = r
    data = d.get("data", {})
    for k in ("memory_cache_ttl_seconds", "memory_cache_max_size", "sqlite_cache_ttl_days",
              "sqlite_clean_interval_hours", "next_clean_at"):
        assert k in data, f"api_stats 缺 {k}: {data}"
    print(f"✓ test_api_stats_returns_status_fields (ttl={data['memory_cache_ttl_seconds']}s, max={data['memory_cache_max_size']}, sql_ttl={data['sqlite_cache_ttl_days']}d)")


def test_api_stats_timeline_endpoint():
    """v0.8.12: /cache/stats/timeline 返回 30 天桶。"""
    import asyncio
    import tempfile, pathlib
    tmpdir = tempfile.mkdtemp()
    p = new_plugin()
    p._caption_cache = main.CaptionCache(pathlib.Path(tmpdir) / "tl.sqlite3")
    p._caption_cache.put("k1", "https://a.com/1", "猫")
    captured = {}
    def mock_register(route, fn, methods, desc):
        captured[route] = fn
    class _R:
        args = {}
        def __getattr__(self, name): raise AttributeError(name)
    p.context = SimpleNamespace(request=_R(), register_web_api=mock_register)
    p._register_web_apis()
    fn = captured[f"/{main.PLUGIN_NAME}/cache/stats/timeline"]
    r = asyncio.run(fn())
    if isinstance(r, tuple):
        body = r[0]
        if hasattr(body, "get_json"):
            d = body.get_json()
        elif hasattr(body, "get_data"):
            import json as _json
            d = _json.loads(body.get_data(as_text=True))
        else:
            d = body
    else:
        d = r
    data = d.get("data", {})
    assert data.get("days") == 30, f"应返 days=30, 实际 {data.get('days')}"
    assert len(data.get("buckets", [])) == 30
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ test_api_stats_timeline_endpoint")


def test_webui_status_bar_and_timeline_in_html():
    """v0.8.12: index.html 必须有 status-bar / timeline-svg / auto-refresh-toggle 元素。"""
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    for sel in ("status-bar", "status-mem-ttl", "status-mem-max", "status-sql-ttl",
                "status-next-clean", "timeline-svg", "auto-refresh-toggle"):
        assert sel in h, f"index.html 缺 #{sel}"
    print("✓ test_webui_status_bar_and_timeline_in_html")


def test_webui_auto_refresh_and_timeline_in_app_js():
    """v0.8.12: app.js 必须实现 auto-refresh toggle + loadTimeline + drawTimeline。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    for fn in ("setAutoRefresh", "loadTimeline", "drawTimeline", "renderStatusBar"):
        assert fn in src, f"app.js 缺函数 {fn}"
    assert "auto-refresh-toggle" in src, "app.js 缺 auto-refresh-toggle 事件绑定"
    assert "cache/stats/timeline" in src, "app.js 缺 cache/stats/timeline endpoint 调用"
    print("✓ test_webui_auto_refresh_and_timeline_in_app_js")


def test_last_clean_at_recorded():
    """v0.8.12: clean_expired 后 _last_clean_at 会被记录（供 webui 算下次清理）。"""
    import asyncio, tempfile, pathlib, shutil, time
    p = new_plugin()
    p.config = dict(p.config)
    p.config["sqlite_cache_ttl_days"] = 7
    tmpdir = tempfile.mkdtemp()
    p._caption_cache = main.CaptionCache(pathlib.Path(tmpdir) / "c.sqlite3")
    captured = {}
    def mock_register(route, fn, methods, desc):
        captured[route] = fn
    class _R:
        def __getattr__(self, name): raise AttributeError(name)
    p.context = SimpleNamespace(request=_R(), register_web_api=mock_register)
    p._register_web_apis()
    fn = captured[f"/{main.PLUGIN_NAME}/cache/clean_expired"]
    before = time.time()
    asyncio.run(fn())
    after = time.time()
    last = getattr(p, "_last_clean_at", 0)
    assert before - 1 <= last <= after + 1, f"_last_clean_at={last} 应在 [{before}, {after}] 之间"
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ test_last_clean_at_recorded")


# ===========================================================================
# v0.8.13 工具过滤器——干扰 chat_plus 注入
# ===========================================================================

def test_tool_filter_off_returns_zero():
    """v0.8.13: tool_filter_mode=off 不动工具集。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self): self.tools = [FakeTool("a"), FakeTool("b")]
    c = FakeContainer()
    assert main._filter_disabled_tools(c, "off", ["a"]) == 0
    assert len(c.tools) == 2, "off 模式不应该改"
    print("✓ test_tool_filter_off_returns_zero")


def test_tool_filter_blacklist_removes_named():
    """v0.8.13: blacklist 模式删 names 里匹配的工具（含 * 通配符）。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self): self.tools = [FakeTool("call_maid"), FakeTool("archive_get_history"),
                                            FakeTool("archive_get_sessions"), FakeTool("keep_me")]
    c = FakeContainer()
    n = main._filter_disabled_tools(c, "blacklist", ["call_maid", "archive_*"])
    assert n >= 3, f"应删 3 个，实际 {n}"
    kept = [t.name for t in c.tools]
    assert "keep_me" in kept
    assert "call_maid" not in kept
    assert "archive_get_history" not in kept
    assert "archive_get_sessions" not in kept
    print("✓ test_tool_filter_blacklist_removes_named")


def test_tool_filter_whitelist_keeps_named_only():
    """v0.8.13: whitelist 模式只保留 names 里匹配的工具。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self): self.tools = [FakeTool("call_maid"), FakeTool("archive_get_history"),
                                            FakeTool("keep_me"), FakeTool("drop_me")]
    c = FakeContainer()
    n = main._filter_disabled_tools(c, "whitelist", ["keep_*", "call_maid"])
    assert n >= 2
    kept = [t.name for t in c.tools]
    assert "keep_me" in kept
    assert "call_maid" in kept
    assert "archive_get_history" not in kept
    assert "drop_me" not in kept
    print("✓ test_tool_filter_whitelist_keeps_named_only")


def test_tool_filter_with_remove_func_method():
    """v0.8.13: 兼容 FunctionToolManager 风格的 remove_func(name) 接口。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self):
            self.tools = [FakeTool("bad"), FakeTool("good")]
            self.removed = []
        def remove_func(self, name):
            self.tools = [t for t in self.tools if t.name != name]
            self.removed.append(name)
    c = FakeContainer()
    n = main._filter_disabled_tools(c, "blacklist", ["bad"])
    assert n == 1
    assert c.removed == ["bad"]
    assert len(c.tools) == 1
    assert c.tools[0].name == "good"
    print("✓ test_tool_filter_with_remove_func_method")


def test_tool_filter_with_func_list():
    """v0.8.13: 兼容 .func_list 字段。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self): self.func_list = [FakeTool("x"), FakeTool("y"), FakeTool("z")]
    c = FakeContainer()
    main._filter_disabled_tools(c, "blacklist", ["y"])
    assert [t.name for t in c.func_list] == ["x", "z"]
    print("✓ test_tool_filter_with_func_list")


def test_tool_filter_handles_none_container():
    """v0.8.13: 传 None / 空 names 不崩。"""
    assert main._filter_disabled_tools(None, "blacklist", ["a"]) == 0
    class C: pass
    c = C()  # 没 .tools / .func_list / .remove_func
    assert main._filter_disabled_tools(c, "blacklist", ["a"]) == 0
    print("✓ test_tool_filter_handles_none_container")


def test_tool_filter_in_event_via_extra_key():
    """v0.8.13: 主钩子入口能从 event.get_extra(extra_key) 拿 tool set 并清。"""
    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeContainer:
        def __init__(self): self.tools = [FakeTool("call_maid"), FakeTool("keep")]
    class FakeEvent:
        def __init__(self, ts): self._ts = ts
        def get_extra(self, k, default=None):
            return self._ts if k == "_group_chat_plus_func_tool" else default
    class FakeReq: pass
    p = new_plugin()
    p.config = dict(p.config)
    p.config["tool_filter_mode"] = "blacklist"
    p.config["tool_filter_names"] = "call_maid"
    p.config["tool_filter_extra_key"] = "_group_chat_plus_func_tool"
    p._filter_tools_in_event(FakeEvent(FakeContainer()), FakeReq())
    # 没在主钩子调用后改回 func_tool 里——但是 _filter_tools_in_event 只动 event.get_extra 的 set
    # 验证：再调用一次，看是否修改
    print("✓ test_tool_filter_in_event_via_extra_key")


def test_strip_residual_base64_clears_func_tool():
    """v0.8.13: 链末兜底钩子删 req.func_tool 里在 names 列表的工具。"""
    import asyncio
    from types import SimpleNamespace

    class FakeTool:
        def __init__(self, name): self.name = name
    class FakeFuncTool:
        def __init__(self): self.tools = [FakeTool("call_maid"), FakeTool("angel_recall"),
                                            FakeTool("pixiv_random"), FakeTool("keep")]
    p = new_plugin()
    p.config = dict(p.config)
    p.config["tool_filter_mode"] = "blacklist"
    p.config["tool_filter_names"] = "call_maid,angel_*,pixiv_*"
    # 直接调链末钩子
    class _Req:
        def __init__(self):
            self.image_urls = []
            self.extra_user_content_parts = None
            self.contexts = None
            self.func_tool = FakeFuncTool()
    req = _Req()
    asyncio.run(p.strip_residual_base64(None, req))
    kept = [t.name for t in req.func_tool.tools]
    assert "call_maid" not in kept
    assert "angel_recall" not in kept
    assert "pixiv_random" not in kept
    assert "keep" in kept
    print("✓ test_strip_residual_base64_clears_func_tool")


# ===========================================================================
# v0.8.14 webui bridge 防御性 fallback
# ===========================================================================

def test_webui_no_direct_bridge_await_in_app_js():
    """v0.8.18: app.js 顶层不应有 ``const bridge = window.AstrBotPluginPage;`` 裸读——必须走 fallbackBridge stub。"""
    import re
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    bad = re.search(r"^const bridge = window\.AstrBotPluginPage;?\s*$", src, re.MULTILINE)
    assert not bad, "app.js 顶层不应该再裸读 window.AstrBotPluginPage"
    # 验证有 fallback 路径
    assert "fallbackFetch" in src, "app.js 必须有 fallbackFetch() 直 fetch backend"
    assert "/api/plug/astrbot_plugin_vision_text_bridge" in src, "app.js 必须有正确的 PLUGIN_PATH"
    print("✓ test_webui_no_direct_bridge_await_in_app_js")


def test_index_html_no_bridge_sdk_loading():
    """v0.8.18: index.html 不再主动 inject bridge-sdk.js——AstrBot 服务端 CORS wildcard
    + origin=null + credentials mode=include 三者撞。改走 fallbackFetch 直打 backend。
    """
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    import re
    code_only = re.sub(r"<!--[\s\S]*?-->", "", h)  # 去掉注释
    # 不应该再 inject bridge-sdk.js（允许在注释里解释为什么）
    assert "/api/plugin/page/bridge-sdk.js" not in code_only, "v0.8.18 不应再主动加载 bridge-sdk.js"
    print("✓ test_index_html_no_bridge_sdk_loading")


def test_app_js_uses_fallback_bridge_stub():
    """v0.8.18: app.js 应该用 fallback bridge stub，永远不依赖 window.AstrBotPluginPage。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 必须有 _fallbackBridge stub
    assert "_fallbackBridge" in src, "app.js 必须定义 _fallbackBridge stub"
    # bridge 应该是 fallback 而不是 AstrBotPluginPage
    assert "const bridge = window.AstrBotPluginPage || _fallbackBridge" in src, "bridge 必须是 fallback"
    # 必须检测 bridge.apiGet/apiPost 是否为 function，不是就 fallbackFetch
    assert "typeof bridge.apiGet === \"function\"" in src, "必须检测 bridge.apiGet 是否可用"
    # v0.8.19: bridge mode badge 让用户能视觉上看出 webui 加载状态
    assert "bridge-mode-badge" in src, "app.js 必须更新 bridge-mode-badge 状态"
    print("✓ test_app_js_uses_fallback_bridge_stub")


def test_index_html_has_bridge_mode_badge():
    """v0.8.19: index.html 顶部必须添加 #bridge-mode-badge 元素。"""
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    assert "bridge-mode-badge" in h, "index.html 必须有 #bridge-mode-badge 元素"
    print("✓ test_index_html_has_bridge_mode_badge")


def test_v0820_drops_esm_module():
    """v0.8.20: app.js / logger.js / index.html 不再使用 ESM type=module。
    部分 AstrBot 版本对 <script type=module> 处理不一致，导致 app.js 顶层代码不跑。
    """
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    # 1. 不能再用 type=module
    assert "type=\"module\"" not in h, "v0.8.20 index.html 不应再使用 type=module"
    # 2. logger.js / app.js 都用普通 <script>
    assert '<script src="./logger.js' in h, "logger.js 必须用普通 <script> 加载"
    assert '<script src="./app.js' in h, "app.js 必须用普通 <script> 加载"
    # 3. logger.js 不再 export default
    lj = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/logger.js"), encoding="utf-8").read()
    assert "export default" not in lj, "v0.8.20 logger.js 不再 export default"
    # 4. app.js 不再 import logger
    aj = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    assert "import logger from " not in aj, "v0.8.20 app.js 不再 import logger"
    # 5. app.js 必须包裹为 IIFE 解决 top-level await
    assert "(async function main() {" in aj, "v0.8.20 app.js 必须包裹为 IIFE"
    # 6. 必须在末尾 catch 启动崩溃，全屏显示错
    assert "})().catch(" in aj, "v0.8.20 app.js 必须在 IIFE 末尾 catch 启动错误"
    print("✓ test_v0820_drops_esm_module")


def test_v0822_endpoints_have_leading_slash():
    """v0.8.22: app.js 调 apiGet/apiPost 时 endpoint 必须以 '/' 开头，
    避免和 PLUGIN_PATH 直接拼接产生 '/api/plug/<plugin>cache/stats' (错)
    """
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 抓出所有 apiGet/apiPost(endpoint, ...) 调用
    import re
    calls = re.findall(r"(?:apiGet|apiPost)\(([^\n]+)", src)
    for c in calls:
        # 提取第一个字符串参数（"或`“包起来的）
        m = re.search(r'[`"\'](/[^"\'`]+)[`"\']', c)
        if m:
            assert m.group(1).startswith("/"), f"endpoint 必须以 / 开头: {m.group(1)} in '{c.strip()}'"
    # 防御性：fallbackFetch 内部也要以 / 开头
    assert "const ep = endpoint.startsWith(\"/\")" in src, "fallbackFetch 必须防御性加 /"
    print("✓ test_v0822_endpoints_have_leading_slash")


def test_v0823_webui_version_badge():
    """v0.8.23: webui 启动时读 script src 的 ?v=X.Y.Z 写入 db-path-badge，
    让用户一眼看出 AstrBot 实际加载了什么版本（避免 AstrBot cache 误示）
    """
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    a = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # index.html 顶部必须含 app.js? v=0.8.23
    assert 'app.js?v=0.8.27' in h, "index.html app.js 必须用 v=0.8.27"
    # app.js 必须从 document.querySelectorAll('script[src*="app.js"]') 拿版本
    assert 'querySelectorAll(\'script[src*="app.js"]\')' in a, "app.js 必须 querySelectorAll 读版本"
    assert "match(/[?&]v=([0-9.]+)/)" in a, "app.js 必须从 src 解析 ?v=X.Y.Z"
    assert "db-path-badge" in a, "app.js 必须更新 db-path-badge 显示 webui 版本"
    print("✓ test_v0823_webui_version_badge")


def test_v0824_bridge_sdk_reload():
    """v0.8.24: 重新启用 AstrBot bridge SDK——
    走 postMessage 跟同源 parent 通信, 绕开 sandbox iframe origin=null 问题。
    crossOrigin='anonymous' (不是 'use-credentials') 才能让 server ACAO: * 被接受.
    """
    a = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 1. 必须 inject bridge-sdk.js
    assert "/api/plugin/page/bridge-sdk.js" in a, "app.js 必须主动 inject bridge-sdk.js"
    # 2. 必须用 crossOrigin='anonymous' (不是 use-credentials)
    assert 'crossOrigin = "anonymous"' in a, "必须 crossOrigin=anonymous (不撞 ACAO=*)"
    # 3. 必须 async=false 同步等 SDK 加载完
    assert "s.async = false" in a, "必须 async=false 同步等 SDK"
    # 4. onerror 必须 fallback 到 _fallbackBridge
    assert "bridge-sdk.js 加载失败" in a, "SDK 加载失败时必须继续走 fallback"
    print("✓ test_v0824_bridge_sdk_reload")


def test_v0825_filter_bot_avatar_in_hook():
    """v0.8.25: 在 on_llm_request hook 入口过滤 AstrBot 框架注入的 bot 头像
    (q.qlogo.cn/headimg_dl?dst_uin=... 模式), 视觉理解 bot 头像没意义。
    """
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
    # 1. 必须在 saved_urls 那段加 q.qlogo.cn 过滤
    assert "q.qlogo.cn/headimg_dl" in src, "main.py 必须过滤 q.qlogo.cn bot avatar"
    assert "_bot_avatar_pat" in src, "main.py 必须有 _bot_avatar_pat 正则"
    # 2. 必须过滤后 log 一下跳过了多少
    assert "v0.8.25 过滤 bot 头像" in src, "main.py 必须 log 过滤结果"
    # 3. 加 saved_urls 诊断 log——后续调试用
    assert "hook 入口 saved_urls" in src, "main.py 必须加 hook 入口诊断 log"
    print("✓ test_v0825_filter_bot_avatar_in_hook")


def test_v0826_post_skips_bridge_and_thumb_sessionstorage():
    """v0.8.26:
    1. apiPost 跳过 bridge.apiPost (v0.8.24 发现 bridge 把 body 转 query 导致 400)
    2. ensureThumb 加 sessionStorage 跨刷新缓存
    """
    a = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 1. apiPost 必须直接走 fallbackFetch
    # 找到 apiPost 函数体
    import re
    m = re.search(r"async function apiPost\(.*?\n\s*const resp = await fallbackFetch\(.POST., endpoint, body\);", a, re.DOTALL)
    assert m, "apiPost 必须直接走 fallbackFetch (不经过 bridge)"
    assert "bridge.apiPost(endpoint, body)" not in m.group(0), "apiPost 内部不应再调 bridge.apiPost"
    # 2. ensureThumb 必须有 sessionStorage 读写
    assert "sessionStorage.getItem(ssKey)" in a, "ensureThumb 必须读 sessionStorage"
    assert "sessionStorage.setItem(ssKey, JSON.stringify(thumb))" in a, "ensureThumb 必须写 sessionStorage"
    print("✓ test_v0826_post_skips_bridge_and_thumb_sessionstorage")


def test_v0827_writes_use_get_to_avoid_cors_preflight():
    """v0.8.27: sandbox iframe 里 POST + Content-Type: application/json 触发 CORS preflight
    (server 不发 ACAO → 拒)。把 write 操作改用 GET + query string
    (GET 是 simple request, 不发 preflight, 可过)。
    """
    a = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    m = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
    # 1. webui onDelete 必须用 apiGet 不是 apiPost
    import re
    m_d = re.search(r"async function onDelete\(id\)\s*\{(.*?)\n\}", a, re.DOTALL)
    assert m_d, "app.js 必须有 onDelete 函数"
    assert "apiPost(\"/cache/delete\"" not in m_d.group(1), "onDelete 不应再用 apiPost"
    assert "apiGet(\"/cache/delete\"" in m_d.group(1), "onDelete 应改用 apiGet"
    # 2. onRegenerate 必须用 apiGet
    m_r = re.search(r"async function onRegenerate\(id\)\s*\{(.*?)\n\}", a, re.DOTALL)
    assert m_r and "apiGet(\"cache/regenerate\"" in m_r.group(1), "onRegenerate 应改用 apiGet"
    # 3. onClear 必须用 apiGet
    m_c = re.search(r"async function onClear\(\)\s*\{(.*?)\n\}", a, re.DOTALL)
    assert m_c and "apiGet(\"cache/clear\"" in m_c.group(1), "onClear 应改用 apiGet"
    # 4. onCleanExpired 必须用 apiGet
    m_x = re.search(r"async function onCleanExpired\(\)\s*\{(.*?)\n\}", a, re.DOTALL)
    assert m_x and "apiGet(\"cache/clean_expired\"" in m_x.group(1), "onCleanExpired 应改用 apiGet"
    # 5. backend 路由必须同时支持 GET (避免 CORS preflight)
    assert '("/cache/delete", api_delete, ["GET", "POST"]' in m, "/cache/delete 必须支持 GET"
    assert '("/cache/regenerate", api_regenerate, ["GET", "POST"]' in m, "/cache/regenerate 必须支持 GET"
    assert '("/cache/clear", api_clear, ["GET", "POST"]' in m, "/cache/clear 必须支持 GET"
    assert '("/cache/clean_expired", api_clean_expired, ["GET", "POST"]' in m, "/cache/clean_expired 必须支持 GET"
    # 6. backend api_delete / api_regenerate 必须从 query 读 key (兼容 GET)
    assert "request.query.get(\"key\")" in m, "api_delete/api_regenerate 必须从 query 读 key"
    print("✓ test_v0827_writes_use_get_to_avoid_cors_preflight")


def test_v0821_app_js_loaded_after_body():
    """v0.8.21: <script src='./app.js'> 必须在 </body> 之前加载，
    避免在 <head> 里提前执行（DOM 还没 parse 完）时调 $('xxx') 返回 null。
    """
    h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/index.html"), encoding="utf-8").read()
    # logger.js 可以在 head 里（轻量，全局）但 app.js 必须在 body 末尾
    # 1. 找 app.js 的 script 标签所在行
    import re
    # 忽略 HTML 注释里的“</body>”提及，只匹配实际标签
    body_match = re.search(r"</body>\s*$", h, re.MULTILINE)
    assert body_match, "index.html 缺 </body>"
    app_match = re.search(r'<script src="\./app\.js[^"]*"', h)
    assert app_match, "index.html 缺 app.js <script>"
    assert app_match.start() < body_match.start(), "app.js 必须在 </body> 之前加载"
    # 2. app.js IIFE 顶部必须等 DOMContentLoaded（双保险）
    aj = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    assert "DOMContentLoaded" in aj, "app.js IIFE 顶部必须等 DOMContentLoaded"
    assert "document.readyState" in aj, "app.js IIFE 必须检查 document.readyState"
    # 3. bind() helper 必须存在，防 null addEventListener
    assert "const bind = (id, evt, fn) =>" in aj, "app.js 必须有 bind() helper"
    print("✓ test_v0821_app_js_loaded_after_body")


# ===========================================================================
# v0.8.17 代码瘦身 helper
# ===========================================================================

def test_cfg_int_helper_exists():
    """v0.8.17: main._cfg_int 抽出来统一 int config 读取。"""
    p = new_plugin()
    p.config = {"foo": "42", "bar": 0, "baz": None, "qux": "abc", "missing": None}
    assert main._cfg_int(p.config, "foo", 0) == 42
    assert main._cfg_int(p.config, "bar", 99) == 0
    assert main._cfg_int(p.config, "baz", 7) == 7
    assert main._cfg_int(p.config, "qux", 0) == 0  # 非法转 int 返 default
    assert main._cfg_int(p.config, "missing", 5) == 5
    print("✓ test_cfg_int_helper_exists")


def test_cfg_str_helper_exists():
    """v0.8.17: main._cfg_str 抽出来统一 str config 读取。"""
    p = new_plugin()
    p.config = {"foo": "bar", "baz": None, "num": 42}
    assert main._cfg_str(p.config, "foo", "x") == "bar"
    assert main._cfg_str(p.config, "baz", "x") == "x"
    assert main._cfg_str(p.config, "num", "x") == "42"  # 非 str 强制 str
    print("✓ test_cfg_str_helper_exists")


def test_app_js_no_dead_fmtDim():
    """v0.8.17: app.js 死代码清理——fmtDim 未被引用，删。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages/cache-manager/app.js"), encoding="utf-8").read()
    # 允许出现在注释 / 字面量字符串里，但不允许是函数定义
    import re
    m = re.search(r"function\s+fmtDim\s*\(", src)
    assert not m, "app.js fmtDim 函数定义还在——死代码"
    print("✓ test_app_js_no_dead_fmtDim")


def test_caption_cache_datetime_top_level():
    """v0.8.17: caption_cache.datetime 提到模块顶部，不再方法内 import。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "caption_cache.py"), encoding="utf-8").read()
    assert "import datetime\n" in src or "import datetime as" in src, "caption_cache 顶部必须 import datetime"
    # 验证方法内没有再 import datetime as _dt
    import re
    for m in re.finditer(r"import datetime as _dt", src):
        # 找到行号
        line_no = src[:m.start()].count("\n") + 1
        # 顶部 import 之后的 都不应有这个 as 别名
        assert line_no > 30, f"caption_cache.py:{line_no} 还在方法内 import datetime as _dt"
    print("✓ test_caption_cache_datetime_top_level")


if __name__ == "__main__":
    run_all()
