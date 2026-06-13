"""tests/stub_helpers.py — 4 个 test_*.py 共用的 astrbot/quart stub + plugin 构造。

沙箱无 astrbot / quart, 需要注入 mock 让 main / web_api 能 import。
这些 stub 集中在这里, 4 个 test 文件用 ``from stub_helpers import install_stubs, make_test_plugin``。
"""
import os
import sys
import types
import asyncio
import tempfile
from types import SimpleNamespace
from pathlib import Path


# ---------------------------------------------------------------------------
# stub 安装 (1 个函数, 4 个 test 共享)
# ---------------------------------------------------------------------------
def install_stubs() -> None:
    """: 注入 astrbot / quart mock 模块到 sys.modules. 可重复调用 (idempotent)."""
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "_stub_marker", False):
        return  # 已装

    stub = types.ModuleType("astrbot")
    stub._stub_marker = True

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

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
    star_module.register = lambda *a, **k: (lambda c: c)

    # StarTools (chat_archive_integration 用)
    _star_tools_data_dir = str(Path(tempfile.mkdtemp()) / "plugin_data" / "test_plugin")
    class _StarTools:
        @staticmethod
        def get_data_dir():
            return _star_tools_data_dir
    star_module.StarTools = _StarTools

    sys.modules.setdefault("astrbot", stub)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event_module)
    sys.modules.setdefault("astrbot.api.provider", provider_module)
    sys.modules.setdefault("astrbot.api.star", star_module)
    stub.api = api

    # quart stub (web_api 用)
    quart_mod = types.ModuleType("quart")
    quart_mod.request = SimpleNamespace(args={}, method="POST")

    async def _stub_get_json(silent=True):
        return None

    async def _stub_get_data(as_text=False):
        return ""

    async def _stub_form():
        return {}

    quart_mod.request.get_json = _stub_get_json
    quart_mod.request.get_data = _stub_get_data
    quart_mod.request.form = _stub_form
    sys.modules.setdefault("quart", quart_mod)


# ---------------------------------------------------------------------------
# plugin 构造 (3 个 test_*.py 重复同样的 plugin + caption_cache 构造)
# ---------------------------------------------------------------------------
def make_test_plugin(main_module, **config_overrides):
    """: 构造测试用 plugin + caption_cache (走 __new__ 绕过 AstrBot 父类 __init__)。

    Args:
        main_module:  已在测试中 import 的 main (VisionTextBridgePlugin)
        **config_overrides:  配置覆盖 (mmx_path, cache_descriptions, max_b64_size_kb, ...)

    Returns:
        plugin 实例 (已设 config, context, _caption_cache, _description_cache, _vision_semaphore)
    """
    p = main_module.VisionTextBridgePlugin.__new__(main_module.VisionTextBridgePlugin)
    p.config = {
        "mmx_path": "/usr/bin/true",
        "cache_descriptions": True,
        "max_b64_size_kb": 200,
        **config_overrides,
    }
    p.mmx_path = "/usr/bin/true"
    p.context = SimpleNamespace()
    # 默认空 caption_cache (测试需要时再覆盖 p._caption_cache = CaptionCache(...))
    p._caption_cache = None
    p._description_cache = {}
    p._vision_semaphore = asyncio.Semaphore(1)
    return p


def make_test_plugin_with_caption_cache(main_module, db_path: str, **config_overrides):
    """: make_test_plugin + 注入 caption_cache 实例。"""
    p = make_test_plugin(main_module, **config_overrides)
    p._caption_cache = main_module.CaptionCache(db_path)
    return p
