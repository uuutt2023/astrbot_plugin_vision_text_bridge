"""smart_imagechat_hub_integration.py - 与 astrbot_plugin_smart_imagechat_hub 兼容对接。

设计: 检测安装 / /v1/chat/completions 接管 image caption / 启动期自动注册 OpenAI provider
作者: uuutt
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from vision_bridge_provider import VisionBridgeProvider
try:
    from constants import (
        DEFAULT_DASHBOARD_PORT,
        PLUGIN_ROUTE_PREFIX,
        OPENAI_COMPAT_PATH,
    )
except ImportError:
    # 沙箱 fallback (constants.py 自身 import 失败 — 不应发生)
    DEFAULT_DASHBOARD_PORT = 6185
    PLUGIN_ROUTE_PREFIX = "/api/plug/astrbot_plugin_vision_text_bridge"
    OPENAI_COMPAT_PATH = "/v1/chat/completions"

logger = logging.getLogger(__name__)


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

# ---------------------------------------------------------------------------
# Provider 自动注册 — 让 smart_imagechat_hub 直接选 vision_text_bridge_compat 就能用
# ---------------------------------------------------------------------------
PROVIDER_ID = "vision_text_bridge_compat"
PROVIDER_TYPE = "vision_bridge_compat"  # : custom type - 避开 openai SDK 校验
PROVIDER_DEFAULT_MODEL = "vision-bridge"


def build_provider_config(
    api_base: str = "",
    api_key: str = "",
    model: str = PROVIDER_DEFAULT_MODEL,
) -> dict:
    """: 构造 AstrBot provider_config dict — type=vision_bridge_compat (custom).

    **不再需要用户传 api_base** — 内部从 dashboard_port 拼。
    **api_key 仍可省略** — 空时用占位符 'placeholder' (AstrBot OpenAI provider 校验必填, 实际本端点不校验)。
    """
    if not api_base:
        # : 默认从 dashboard_port 拼 - 减少魔法字符串
        api_base = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
    return {
        "type": PROVIDER_TYPE,
        "id": PROVIDER_ID,
        "enable": True,
        "api_base": api_base.rstrip("/"),
        # AstrBot 校验 api_key 必填, 用占位字符串 (我方不校验)
        "key": [api_key] if api_key else ["placeholder"],
        "model": model,
        "provider_type": "chat_completion",
    }


def is_provider_already_registered(plugin) -> bool:
    """: 检查 vision_text_bridge_compat provider 是否已注册 (避免重复)."""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            return False
        # : 检查类型, 不查 provider_config.id (因为 VisionBridgeProvider 不存 id 字段)
        for prov in getattr(pm, "provider_insts", []):
            if isinstance(prov, VisionBridgeProvider):
                return True
        # 兼容老 openai_chat_completion 注册路径 (有 provider_config.id 字段)
        for prov in getattr(pm, "provider_insts", []):
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                return True
        return False
    except Exception:
        return False


def _build_api_base(plugin) -> str:
    """: 算 api_base - schema dashboard_port > AstrBot dashboard.port > 默认."""
    try:
        ac = plugin.context.astr_context
        cfg = ac.config if hasattr(ac, "config") else {}
        dashboard = cfg.get("dashboard", {}) if isinstance(cfg, dict) else {}
        host = dashboard.get("host", "localhost")
        port = plugin.config.get("dashboard_port") or dashboard.get("port", DEFAULT_DASHBOARD_PORT)
        port = int(port)
    except Exception:
        host, port = "localhost", DEFAULT_DASHBOARD_PORT
    return f"http://{host}:{port}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"


def _inject_provider_class_map(pm) -> None:
    """: 注入 custom type class — 避开 openai SDK 校验."""
    for attr in ("provider_class_map", "provider_classes", "_provider_classes", "provider_cls_map"):
        cls_map = getattr(pm, attr, None)
        if isinstance(cls_map, dict):
            cls_map[PROVIDER_TYPE] = VisionBridgeProvider
            return
    try:
        pm.provider_class_map = {PROVIDER_TYPE: VisionBridgeProvider}
    except Exception:
        pass


def _cleanup_broken_instances(pm) -> tuple[dict, list]:
    """: 清理 framework 残留 broken instance — 单轮扫 provider_insts.

    返 (prov_dict, inst_list) — 都直接引用 pm 上字段, 后续 mutate 会影响原对象.
    """
    prov_dict = getattr(pm, "providers", None)
    inst_list = getattr(pm, "provider_insts", None)
    if not isinstance(inst_list, list):
        inst_list = []
    if isinstance(inst_list, list):
        for i, p_inst in enumerate(inst_list):
            if p_inst is None or isinstance(p_inst, VisionBridgeProvider):
                continue
            cfg = getattr(p_inst, "provider_config", None) or {}
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                inst_list[i] = None
    if isinstance(prov_dict, dict) and PROVIDER_ID in prov_dict:
        broken = prov_dict.get(PROVIDER_ID)
        if broken is None or not isinstance(broken, VisionBridgeProvider):
            prov_dict[PROVIDER_ID] = None
    return (prov_dict if isinstance(prov_dict, dict) else {}, inst_list)


def _add_or_replace_inst(inst_list: list, prov_dict: dict, inst) -> None:
    """: 加 inst — 优先替换 None 占位."""
    for i, p in enumerate(inst_list):
        if p is None:
            inst_list[i] = inst
            break
    else:
        inst_list.append(inst)
    prov_dict[PROVIDER_ID] = inst


async def auto_register_provider(plugin) -> bool:
    """: 调度 — 4 步走, 各 helper 失败互不影响.

    1. 算 api_base
    2. 注入 custom type class map (避开 openai SDK)
    3. 清理 framework 残留 broken instance
    4. instantiate 我方 provider + add to provider_manager
    """
    if is_provider_already_registered(plugin):
        return True
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            logger.debug("[vision_text_bridge] provider_manager 不可用, 跳过自动注册")
            return False
        api_base = _build_api_base(plugin)
        api_key = (
            plugin.config.get("api_key", "")
            or plugin.config.get("openai_compat_api_key", "")
            or plugin.config.get("smart_imagechat_hub_api_key", "")
        )
        model = (
            plugin.config.get("model_name")
            or plugin.config.get("openai_compat_model_name")
            or plugin.config.get("smart_imagechat_hub_model_name")
            or PROVIDER_DEFAULT_MODEL
        )
        _inject_provider_class_map(pm)
        prov_dict, inst_list = _cleanup_broken_instances(pm)
        inst = VisionBridgeProvider(
            provider_config={"api_base": api_base, "key": [api_key or "placeholder"], "model": model},
            provider_settings={},
        )
        _add_or_replace_inst(inst_list, prov_dict, inst)
        logger.info(
            "[vision_text_bridge] 已自动注册 OpenAI compatible provider: id=%s, api_base=%s, model=%s",
            PROVIDER_ID, api_base, model,
        )
        return True
    except Exception as e:
        logger.warning("[vision_text_bridge] auto_register_provider 失败: %s", e)
        return False


def remove_provider(plugin) -> bool:
    """: 卸载我方 provider (清理用)."""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            return False
        for prov in list(getattr(pm, "provider_insts", [])):
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                try:
                    if hasattr(prov, "terminate"):
                        prov.terminate()
                except Exception:
                    pass
                pm.provider_insts.remove(prov)
                logger.info("[vision_text_bridge] 已卸载 provider: %s", PROVIDER_ID)
                return True
        return False
    except Exception:
        return False

