"""smart_imagechat_hub_integration.py — 与 astrbot_plugin_smart_imagechat_hub 兼容对接。

设计:
  1. 检测 smart_imagechat_hub 是否安装 (查 data/plugins/astrbot_plugin_smart_imagechat_hub/metadata.yaml)
  2. 提供 web API /v1/chat/completions (OpenAI compatible) 接管它的 image caption 请求
  3. 用户把 smart_imagechat_hub 配置的 default_image_caption_provider_id 换成本插件的 OpenAI compatible provider (base_url = 本插件的 API)
  4. smart_imagechat_hub 发打标签请求 → 调到我方 API → 走 mmx 图像理解 → 返回 mmx 描述 (包装成 LLM 响应) → smart_imagechat_hub 拿到 tag 描述

参考:
  - https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub
  - smart_imagechat_hub 走 direct_provider_call=True 直接调 provider.text_chat(image_urls=...) → 绕过 on_llm_request 钩子
  - 所以本插件只能在 LLM provider 层 (OpenAI compatible API) 接管, 不能在钩子层

限制:
  - smart_imagechat_hub 打标签时 prompt 是 '请为这张图片生成 5-7 个简短中文特征标签...'
  - 我方拿到 prompt + image_urls → 调 mmx 拿到自然语言描述 → 包装成 OpenAI ChatCompletion 格式返回
  - smart_imagechat_hub 收到的是 mmx 描述而非纯 tag JSON —— 如果它的 _extract_tags 不能解析, 可能要适配
  - 默认走 mmx 路径; 配置 smart_imagechat_hub_compat_use_mmx_captions=False 时改走"提示 LLM 总结"
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


PLUGIN_NAME = "astrbot_plugin_smart_imagechat_hub"
_INSTALL_CHECK_CACHE: Optional[bool] = None  # module-level cache, 避免每次请求都查盘


def _get_plugin_root() -> Optional[Path]:
    """: 探测 AstrBot 插件目录 (data/plugins/) 的可能位置。"""
    try:
        from astrbot.api.star import StarTools
        d = StarTools.get_data_dir()
        return d.parent.parent
    except Exception:
        return None


def is_smart_imagechat_hub_installed() -> bool:
    """: 检测 smart_imagechat_hub 是否安装。

    用 module-level cache 缓存结果 (插件生命周期内不会变, 不用每次重新查盘)。
    测试时可调 reset_cache_for_testing() 清。
    """
    global _INSTALL_CHECK_CACHE
    if _INSTALL_CHECK_CACHE is not None:
        return _INSTALL_CHECK_CACHE
    root = _get_plugin_root()
    if root is None:
        _INSTALL_CHECK_CACHE = False  # 缓存以避免反复查盘
        return False
    candidates = [
        root / "data" / "plugins" / PLUGIN_NAME / "metadata.yaml",
        root / "plugins" / PLUGIN_NAME / "metadata.yaml",
    ]
    for c in candidates:
        if c.is_file():
            _INSTALL_CHECK_CACHE = True
            return True
    _INSTALL_CHECK_CACHE = False
    return False


def get_smart_imagechat_hub_dir() -> Optional[Path]:
    """: 返回 smart_imagechat_hub 安装路径 (如装了)。"""
    root = _get_plugin_root()
    if root is None:
        return None
    for sub in ("data/plugins", "plugins"):
        candidate = root / sub / PLUGIN_NAME
        if candidate.is_dir():
            return candidate
    return None


def reset_cache_for_testing() -> None:
    """: 清 module-level cache (测试用)。"""
    global _INSTALL_CHECK_CACHE
    _INSTALL_CHECK_CACHE = None
