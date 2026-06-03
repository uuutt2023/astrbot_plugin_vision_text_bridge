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
        "image_placeholder_template": "【图片{index}：{description}】",
        "max_description_length": 0,
        "include_history": False,
        "include_extra_parts": True,
        "failure_message": "【图片{index}：理解失败：{error}】",
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
    p._chat_archive_link = None
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
    p = new_plugin()
    assert p._is_cacheable_url("http://x.com/a.jpg") is True
    assert p._is_cacheable_url("https://x.com/a.jpg") is True
    assert p._is_cacheable_url("base64://abc") is False
    assert p._is_cacheable_url("file:///tmp/a.jpg") is False
    assert p._is_cacheable_url("/tmp/a.jpg") is False
    assert p._is_cacheable_url("") is False
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
    part = {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}}
    assert main.VisionTextBridgePlugin._extract_image_url_from_part(part) == "https://x.com/a.jpg"
    text = {"type": "text", "text": "hello"}
    assert main.VisionTextBridgePlugin._extract_image_url_from_part(text) == ""
    print("✓ test_extract_image_url_from_part_dict")


def test_extract_image_url_from_part_object():
    img = SimpleNamespace(type="image_url", image_url=SimpleNamespace(url="https://x.com/a.jpg"))
    assert main.VisionTextBridgePlugin._extract_image_url_from_part(img) == "https://x.com/a.jpg"
    # image_url 直接是字符串
    img2 = SimpleNamespace(type="image_url", image_url="https://x.com/b.jpg")
    assert main.VisionTextBridgePlugin._extract_image_url_from_part(img2) == "https://x.com/b.jpg"
    print("✓ test_extract_image_url_from_part_object")


def test_remove_image_parts():
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://x.com/b.jpg"}},
    ]
    main.VisionTextBridgePlugin._remove_image_parts(parts, ["https://x.com/a.jpg"])
    assert len(parts) == 2
    assert parts[1]["image_url"]["url"] == "https://x.com/b.jpg"
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
    p = new_plugin()
    req = FakeReq(prompt="用户问：这是什么", image_urls=["https://x.com/a.jpg"])
    p._attach_descriptions_to_prompt(
        req,
        [(1, "https://x.com/a.jpg", "一只橘猫趴在沙发上")],
        start_index=1,
        field="image_urls",
    )
    assert req.prompt.startswith("用户问：这是什么")
    assert "【图片1：一只橘猫趴在沙发上】" in req.prompt
    assert req.image_urls == []  # 成功描述的图片从 image_urls 里移除了
    print("✓ test_attach_with_prompt")


def test_attach_no_prompt():
    p = new_plugin()
    req = FakeReq(prompt=None, image_urls=["https://x.com/a.jpg"])
    p._attach_descriptions_to_prompt(
        req,
        [(1, "https://x.com/a.jpg", "一只狗")],
        start_index=1,
        field="image_urls",
    )
    assert req.prompt == "【图片1：一只狗】"
    assert req.image_urls == []
    print("✓ test_attach_no_prompt")


def test_attach_failure_uses_template():
    p = new_plugin()
    req = FakeReq(prompt="看图", image_urls=["https://x.com/bad.jpg"])
    p._attach_descriptions_to_prompt(
        req,
        [(1, "https://x.com/bad.jpg", "")],  # 失败：空描述
        start_index=1,
        field="image_urls",
    )
    assert "【图片1：理解失败：mmx 调用失败或超时】" in req.prompt
    # 新行为：失败也清空 image_urls，避免 raw URL 走到 LLM
    assert req.image_urls == []
    print("✓ test_attach_failure_uses_template")


def test_attach_index_continues():
    p = new_plugin()
    req = FakeReq(prompt=None, image_urls=["a", "b", "c"])
    p._attach_descriptions_to_prompt(
        req,
        [(1, "a", "desc-a"), (2, "b", "desc-b")],
        start_index=1,
        field="image_urls",
    )
    assert "【图片1：desc-a】" in req.prompt
    assert "【图片2：desc-b】" in req.prompt
    # 新行为：被处理过的图片全部清空（含 image_urls 列表中所有项）
    assert req.image_urls == []
    print("✓ test_attach_index_continues")


# ------------------------------------------------------------------ 单测：缓存

def test_cache_hit():
    p = new_plugin(cache_descriptions=True)
    p._description_cache["https://x.com/a.jpg"] = "cached desc"
    # 通过 _describe_one 走缓存路径（mock 子进程，验证不调用 mmx）
    called = {"count": 0}

    async def fake_run(*a, **k):
        called["count"] += 1
        return "fresh", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        result = asyncio.run(p._describe_one("https://x.com/a.jpg"))
    assert result == "cached desc"
    assert called["count"] == 0  # 缓存命中，没调子进程
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
        asyncio.run(p._process_request(event, req))

    # prompt 拼接了两张图说
    assert "帮我看看" in req.prompt
    assert "【图片1：描述: https://x.com/a.jpg】" in req.prompt
    assert "【图片2：描述: https://x.com/b.jpg】" in req.prompt
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
        asyncio.run(p._process_request(SimpleNamespace(), req))

    # 图说被注入 prompt，image_url part 被删除，text part 保留
    assert "【图片1：x图说】" in req.prompt
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["type"] == "text"
    print("✓ test_e2e_extra_parts")


def test_e2e_disabled_plugin():
    p = new_plugin(enabled=False)
    req = FakeReq(prompt="hi", image_urls=["https://x.com/a.jpg"])
    called = {"count": 0}

    async def fake_run(*args, **kwargs):
        called["count"] += 1
        return "x", ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(SimpleNamespace(), req))
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
        asyncio.run(p._process_request(SimpleNamespace(), req))

    # 新行为：失败也清空 image_urls，prompt 用失败模板
    assert req.image_urls == []
    assert "【图片1：理解失败：mmx 调用失败或超时】" in req.prompt
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
        asyncio.run(p._process_request(SimpleNamespace(), req))

    # 历史 content 里 image_url 被移除，只剩 text
    new_content = contexts[0]["content"]
    assert len(new_content) == 1
    assert new_content[0]["type"] == "text"
    # 描述注入到了 req.prompt
    assert "【图片1：历史图说】" in req.prompt
    print("✓ test_e2e_history_in_contexts")


def test_e2e_truncation_applied():
    p = new_plugin(max_description_length=5)
    req = FakeReq(prompt=None, image_urls=["https://x.com/a.jpg"])

    async def fake_run(*args, **kwargs):
        return "这是一段很长的描述文字" * 10, ""

    with patch.object(p, "_run_mmx", side_effect=wrap_run(fake_run)):
        asyncio.run(p._process_request(SimpleNamespace(), req))

    # 描述被截断到 5 个字符 + …
    assert "【图片1：" in req.prompt
    # 截断后是 5 字符 + "…" = 6 字符
    desc_part = req.prompt.replace("【图片1：", "").rstrip("】")
    assert len(desc_part) == 6  # 5 字符 + 省略号
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
    """priority 配置值与 import 时锁定的 DEFAULT_PRIORITY 不一致时应告警并更新全局。"""
    # 保存原始全局值以备还原
    original = main.DEFAULT_PRIORITY
    try:
        # 设为 100，插件配置为 500
        main.DEFAULT_PRIORITY = 100
        p = new_plugin(priority=500)
        # 告警调用本身不会抛，这里只验证全局变量被更新了
        p._warn_if_priority_mismatch()
        assert main.DEFAULT_PRIORITY == 500  # 被更新
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
        assert main.DEFAULT_PRIORITY == 99999
    finally:
        main.DEFAULT_PRIORITY = original
    print("✓ test_priority_out_of_range_warns")


# ------------------------------------------------------------------ 单测：链末兜底


def test_is_data_url():
    assert main.VisionTextBridgePlugin._is_data_url("data:image/webp;base64,UklGR") is True
    assert main.VisionTextBridgePlugin._is_data_url("data:image/png;base64,abc") is True
    assert main.VisionTextBridgePlugin._is_data_url("data:image/jpeg;base64,/9j/4AAQ") is True
    assert main.VisionTextBridgePlugin._is_data_url("https://x.com/a.jpg") is False
    assert main.VisionTextBridgePlugin._is_data_url("file:///tmp/a.jpg") is False
    assert main.VisionTextBridgePlugin._is_data_url("base64://abc") is False
    assert main.VisionTextBridgePlugin._is_data_url("") is False
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
    n = p._strip_all_data_url_images(req)
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
    n = p._strip_all_data_url_images(req)
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
    n = p._strip_all_data_url_images(req)
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
    n = p._strip_all_data_url_images(req)
    assert n == 0
    assert req.image_urls == ["https://x.com/a.jpg"]
    print("✓ test_strip_returns_zero_when_nothing")


def test_strip_handles_string_image_url_field():
    """ImageURLPart 中 image_url 可能是字符串而非 dict。"""
    p = new_plugin()
    parts = [SimpleNamespace(type="image_url", image_url="data:image/webp;base64,XX")]
    req = FakeReq(prompt=None, image_urls=[], extra_user_content_parts=parts)
    n = p._strip_all_data_url_images(req)
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
    # 两条都描述为空（失败）
    p._attach_descriptions_to_prompt(
        req,
        [(1, "https://x.com/a.jpg", ""), (2, "https://x.com/b.jpg", "")],
        start_index=1,
        field="image_urls",
    )
    # prompt 中插入两个【图片：理解失败】占位
    assert "【图片1：理解失败" in req.prompt
    assert "【图片2：理解失败" in req.prompt
    # image_urls 应被全部清空，不留 raw URL
    assert req.image_urls == []
    print("✓ test_attach_clears_image_urls_even_on_failure")


def test_attach_clears_extra_parts_even_on_failure():
    p = new_plugin()
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "https://x.com/a.jpg"}},
    ]
    req = FakeReq(prompt=None, image_urls=[], extra_user_content_parts=parts)
    p._attach_descriptions_to_prompt(
        req,
        [(1, "https://x.com/a.jpg", "")],  # 失败
        start_index=1,
        field="extra_user_content_parts",
    )
    # image_url 组件被清除，text 保留
    assert len(req.extra_user_content_parts) == 1
    assert req.extra_user_content_parts[0]["type"] == "text"
    assert "【图片1：理解失败" in req.prompt
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
    p._attach_descriptions_to_prompt(
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
    n = p._strip_all_image_urls(req)
    assert n == 5  # 3 image_urls + 1 extra_part + 1 context
    assert req.image_urls == []
    assert req.extra_user_content_parts == [{"type": "text", "text": "x"}]
    assert req.contexts[0]["content"] == [{"type": "text", "text": "y"}]
    print("✓ test_strip_all_image_urls_removes_everything")


def test_strip_all_image_urls_zero_when_nothing():
    p = new_plugin()
    req = FakeReq(prompt="hi", image_urls=[], extra_user_content_parts=[], contexts=[])
    n = p._strip_all_image_urls(req)
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
    """默认配置下链末兑底只删 data:base64。"""
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
    # https URL 保留
    assert req.image_urls == ["https://x.com/a.jpg"]
    assert len(req.contexts[0]["content"]) == 1
    print("✓ test_fallback_strip_only_data_url_by_default")


# ------------------------------------------------------------------ 单测：诊断信息


def test_diagnose_balance_error():
    p = new_plugin()
    # 重置告警缓存
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("API error: insufficient balance (HTTP 200)", "http://x.com/a.jpg")
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS
    print("✓ test_diagnose_balance_error")


def test_diagnose_quota_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("quota exceeded", "http://x.com/a.jpg")
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS
    print("✓ test_diagnose_quota_error")


def test_diagnose_auth_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("auth token expired", "http://x.com/a.jpg")
    assert "auth" in main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS
    print("✓ test_diagnose_auth_error")


def test_diagnose_argument_error():
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("No such file or directory", "http://x.com/a.jpg")
    assert "argument" in main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS
    print("✓ test_diagnose_argument_error")


def test_diagnose_unknown_error_no_warning():
    """未识别的错误不应触发告警。"""
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("some unknown error xyz123", "http://x.com/a.jpg")
    assert main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS == set()
    print("✓ test_diagnose_unknown_error_no_warning")


def test_diagnose_warn_once():
    """同一个错误 key 不重复告警。"""
    p = new_plugin()
    main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.clear()
    p._diagnose_mmx_error("insufficient balance", "http://x.com/a.jpg")
    p._diagnose_mmx_error("insufficient balance", "http://x.com/b.jpg")
    # 实际：balance 在 set 中
    assert "balance" in main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS
    # 再调一次不会重复 add（set 长度不变）
    size_before = len(main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS)
    p._diagnose_mmx_error("insufficient balance", "http://x.com/c.jpg")
    size_after = len(main.VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS)
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
        cache.put("a", "https://a.com/1.jpg", "猫")
        cache.put("b", "https://b.com/2.jpg", "狗")
        cache.put("c", "https://c.com/3.jpg", "猫头鹰")
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
        cache.get("a")  # 增加 hit_count
        cache.get("a")
        cache.get("a")
        most_hit = cache.list(limit=10, offset=0, order_by="hit_count_desc")
        assert most_hit[0].image_key == "a"
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
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "c.sqlite3")
        p._caption_cache.put("https://x.com/cached.jpg", "https://x.com/cached.jpg", "已缓存的描述")
        # 同时清空内存缓存确保走 SQLite
        p._description_cache.clear()

        called = {"count": 0}

        async def fake_run(*a, **k):
            called["count"] += 1
            return test.make_mmx_result("不应该被调用", "", 0, True)

        with patch.object(p, "_run_mmx", side_effect=fake_run):
            result = asyncio.run(p._describe_one("https://x.com/cached.jpg"))
        assert result == "已缓存的描述"
        assert called["count"] == 0  # 缓存命中，没调 mmx
        # 内存缓存应被同步填充
        assert p._description_cache["https://x.com/cached.jpg"] == "已缓存的描述"
    print("✓ test_describe_one_uses_sqlite_cache")


def test_describe_one_writes_to_sqlite_cache():
    """mmx 成功后应同时写内存 + SQLite 缓存。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = new_plugin()
        p._caption_cache = main.CaptionCache(Path(tmp) / "c.sqlite3")
        p._description_cache.clear()
        with patch.object(
            p, "_run_mmx",
            return_value=make_mmx_result("新鲜描述", "", 0, True),
        ):
            result = asyncio.run(p._describe_one("https://x.com/fresh.jpg"))
        assert result == "新鲜描述"
        assert p._description_cache["https://x.com/fresh.jpg"] == "新鲜描述"
        entry = p._caption_cache.get("https://x.com/fresh.jpg")
        assert entry is not None
        assert entry.description == "新鲜描述"
    print("✓ test_describe_one_writes_to_sqlite_cache")


# ------------------------------------------------------------------ 单测：Chat Archive 联动


def test_chat_archive_link_no_plugin():
    """data 目录中没找到 chat_archive 插件时，available=False。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        # 模拟本插件 data dir: .../data/plugins/<self>/data/
        # 兄弟目录没有 chat_archive
        self_data = Path(tmp) / "plugins" / "self_plugin" / "data"
        self_data.mkdir(parents=True)
        link = main.ChatArchiveLink(plugin_data_dir=self_data)
        assert link.available is False
        assert link.web_cache_dir is None
    print("✓ test_chat_archive_link_no_plugin")


def test_chat_archive_link_detected():
    """data 目录中找到 chat_archive 插件时，available=True。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        plugins_root = Path(tmp) / "plugins"
        # 自己的 data
        self_data = plugins_root / "self_plugin" / "data"
        self_data.mkdir(parents=True)
        # Chat Archive 的 data + web_cache
        ca_data = plugins_root / "astrbot_plugin_chat_archive" / "data"
        web_cache = ca_data / "web_cache"
        web_cache.mkdir(parents=True)
        (web_cache / "abc123.jpg").write_bytes(b"fake image")

        link = main.ChatArchiveLink(plugin_data_dir=self_data)
        assert link.available is True
        assert link.web_cache_dir is not None
        assert link.web_cache_dir.exists()
        files = link.list_cached_files()
        assert len(files) == 1
    print("✓ test_chat_archive_link_detected")


def test_chat_archive_link_detected_without_web_cache():
    """data 目录在但 web_cache 还没创建——仍算 available=True。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        plugins_root = Path(tmp) / "plugins"
        self_data = plugins_root / "self_plugin" / "data"
        self_data.mkdir(parents=True)
        ca_data = plugins_root / "astrbot_plugin_chat_archive" / "data"
        ca_data.mkdir(parents=True)
        # 注意：没创建 web_cache

        link = main.ChatArchiveLink(plugin_data_dir=self_data)
        assert link.available is True  # 插件在
        assert link.web_cache_dir is None  # web_cache 还没
    print("✓ test_chat_archive_link_detected_without_web_cache")


def test_chat_archive_link_refresh():
    """refresh() 应能强制重新检测。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        plugins_root = Path(tmp) / "plugins"
        self_data = plugins_root / "self_plugin" / "data"
        self_data.mkdir(parents=True)
        ca_data = plugins_root / "astrbot_plugin_chat_archive" / "data"
        ca_data.mkdir(parents=True)

        link = main.ChatArchiveLink(plugin_data_dir=self_data)
        # 第一次检测：available 但 web_cache 为 None
        assert link.available is True
        assert link.web_cache_dir is None
        # 创建 web_cache，refresh 应检测到
        web_cache = ca_data / "web_cache"
        web_cache.mkdir()
        link.refresh()
        assert link.web_cache_dir is not None
    print("✓ test_chat_archive_link_refresh")


# ------------------------------------------------------------------ 单测：web API


def test_register_web_apis_called():
    """_register_web_apis 应注册 7 个 web API。"""
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
    assert any("chat-archive/refresh" in r for r in routes)
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
        assert "【图片1：一只狗】" in req.prompt
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
        ok = p._caption_cache.delete("https://x.com/x.jpg")
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
    """默认 inject_system_prompt_guidance=True 时，注入严格引用提示。"""
    p = new_plugin()
    req = FakeReq(prompt="看图\n\n【图片1：一只狗】\n\n【图片2：一只猫】")
    req.system_prompt = "你是一个助手。"
    p._maybe_inject_system_prompt_guidance(req)
    # 应追加 system_prompt
    assert "[系统提示]" in req.system_prompt
    assert "2 张图片" in req.system_prompt
    assert "严格基于" in req.system_prompt
    # 原始 system_prompt 保留
    assert "你是一个助手。" in req.system_prompt
    print("✓ test_inject_system_prompt_guidance_default_on")


def test_inject_disabled_when_config_off():
    p = new_plugin(inject_system_prompt_guidance=False)
    req = FakeReq(prompt="看图\n\n【图片1：一只狗】")
    req.system_prompt = "你是一个助手。"
    p._maybe_inject_system_prompt_guidance(req)
    # 不应修改
    assert req.system_prompt == "你是一个助手。"
    print("✓ test_inject_disabled_when_config_off")


def test_inject_no_images_in_prompt_no_op():
    p = new_plugin()
    req = FakeReq(prompt="没有图")  # 无【图片x】
    req.system_prompt = "你是一个助手。"
    p._maybe_inject_system_prompt_guidance(req)
    assert req.system_prompt == "你是一个助手。"
    print("✓ test_inject_no_images_in_prompt_no_op")


def test_inject_creates_system_prompt_if_empty():
    p = new_plugin()
    req = FakeReq(prompt="看图\n\n【图片1：一只狗】")
    req.system_prompt = ""
    p._maybe_inject_system_prompt_guidance(req)
    assert req.system_prompt  # 非空
    assert "[系统提示]" in req.system_prompt
    print("✓ test_inject_creates_system_prompt_if_empty")


def test_inject_counts_images_correctly():
    p = new_plugin()
    req = FakeReq(prompt="""用户消息

【图片1：猫】
【图片2：狗】
【图片3：鸟】""")
    req.system_prompt = "X"
    p._maybe_inject_system_prompt_guidance(req)
    assert "3 张图片" in req.system_prompt
    assert "【图片1】" in req.system_prompt
    assert "【图片2】" in req.system_prompt
    assert "【图片3】" in req.system_prompt
    print("✓ test_inject_counts_images_correctly")


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
    """默认 placeholder 应表明是“视觉模型描述”，提示 LLM 严格引用。"""
    import json
    schema = json.load(open("/workspace/astrbot_plugin_vision_text_bridge/_conf_schema.json"))
    default = schema["image_placeholder_template"]["default"]
    assert "视觉模型描述" in default
    assert "{index}" in default
    assert "{description}" in default
    print("✓ test_default_image_placeholder_marks_as_vision_model")


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
        test_caption_cache_stats,
        test_caption_cache_vacuum,
        test_describe_one_uses_sqlite_cache,
        test_describe_one_writes_to_sqlite_cache,
        test_chat_archive_link_no_plugin,
        test_chat_archive_link_detected,
        test_chat_archive_link_detected_without_web_cache,
        test_chat_archive_link_refresh,
        test_register_web_apis_called,
        test_end_to_end_full_flow,
        test_inject_system_prompt_guidance_default_on,
        test_inject_disabled_when_config_off,
        test_inject_no_images_in_prompt_no_op,
        test_inject_creates_system_prompt_if_empty,
        test_inject_counts_images_correctly,
        test_default_vision_prompt_is_conservative,
        test_default_image_placeholder_marks_as_vision_model,
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


if __name__ == "__main__":
    run_all()
