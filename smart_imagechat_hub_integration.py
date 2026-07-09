"""
smart_imagechat_hub_integration.py - 与 astrbot_plugin_smart_imagechat_hub 兼容对接。

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
    """探测 AstrBot 插件目录 (data/plugins/) 的可能位置。"""
    try:
        from astrbot.api.star import StarTools
        d = StarTools.get_data_dir()
        return d.parent.parent
    except Exception:
        return None


def is_smart_imagechat_hub_installed() -> bool:
    """检测 smart_imagechat_hub 是否安装。

    用 module-level cache 缓存结果 (插件生命周期内不会变, 不用每次重新查盘)。
    测试时可调 reset_cache_for_testing() 清。
    """
    global _INSTALL_CHECK_CACHE
    if _INSTALL_CHECK_CACHE is not None:
        return _INSTALL_CHECK_CACHE
    root = _get_plugin_root()
    if root is None:
        _INSTALL_CHECK_CACHE = False
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
    """返回 smart_imagechat_hub 安装路径 (如装了)。"""
    root = _get_plugin_root()
    if root is None:
        return None
    for sub in ("data/plugins", "plugins"):
        candidate = root / sub / PLUGIN_NAME
        if candidate.is_dir():
            return candidate
    return None


def reset_cache_for_testing() -> None:
    """清 module-level cache (测试用)。"""
    global _INSTALL_CHECK_CACHE
    _INSTALL_CHECK_CACHE = None


# ---------------------------------------------------------------------------
# Provider 自动注册 — 让 smart_imagechat_hub 直接选 vision_text_bridge_compat 就能用
# ---------------------------------------------------------------------------
PROVIDER_ID = "vision_text_bridge_compat"
PROVIDER_TYPE = "vision_bridge_compat"  # 自定义类型，避免与 openai SDK 冲突
PROVIDER_DEFAULT_MODEL = "vision-bridge"


def build_provider_config(
    api_base: str = "",
    api_key: str = "",
    model: str = PROVIDER_DEFAULT_MODEL,
) -> dict:
    """构造 AstrBot provider_config dict — type=vision_bridge_compat (custom)。

    api_base 可留空，自动使用本插件暴露的端点。
    api_key 可留空，用占位符绕过框架必填校验（本端点实际不验证）。
    """
    if not api_base:
        api_base = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
    return {
        "type": PROVIDER_TYPE,
        "id": PROVIDER_ID,
        "enable": True,
        "api_base": api_base.rstrip("/"),
        # AstrBot 同时检查 key 和 api_key，都提供占位值
        "key": [api_key] if api_key else ["placeholder"],
        "api_key": api_key if api_key else "placeholder",
        "model": model,
        # 关键修复：将 provider_type 设为自定义类型，而不是 "chat_completion"
        "provider_type": PROVIDER_TYPE,
    }


def is_provider_already_registered(plugin) -> bool:
    """检查 vision_text_bridge_compat provider 是否已注册 (避免重复)."""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            return False
        # 检查已实例化的 provider 列表中是否已有 VisionBridgeProvider
        for prov in getattr(pm, "provider_insts", []):
            if isinstance(prov, VisionBridgeProvider):
                return True
            # 兼容性检查：也可能保留了旧的 openai_chat_completion 实例
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                return True
        return False
    except Exception:
        return False


def _build_api_base(plugin) -> str:
    """计算本插件提供的 OpenAI 兼容端点地址。"""
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


def _register_custom_provider_type(pm) -> None:
    """使用 provider_manager 的公开方法注册自定义适配器类。

    优先级：register_provider_type > provider_type_map > 主动设置。
    """
    # 1. 优先使用框架标准注册接口 (AstrBot >=4.26)
    if hasattr(pm, "register_provider_type"):
        try:
            pm.register_provider_type(PROVIDER_TYPE, VisionBridgeProvider)
            logger.info("已通过 register_provider_type 注册自定义 provider: %s", PROVIDER_TYPE)
            return
        except Exception as e:
            logger.warning("register_provider_type 调用失败: %s", e)

    # 2. 尝试设置已知的公开映射表
    for attr_name in ("provider_type_map", "custom_providers", "provider_class_map"):
        mapping = getattr(pm, attr_name, None)
        if isinstance(mapping, dict):
            mapping[PROVIDER_TYPE] = VisionBridgeProvider
            logger.info("已注入 provider 类型映射到 %s", attr_name)
            return

    # 3. 最后尝试直接设置属性（兼容性极低）
    try:
        pm.provider_class_map = {PROVIDER_TYPE: VisionBridgeProvider}
        logger.warning("已通过直接设置 provider_class_map 注册")
    except Exception:
        logger.error("无法注册自定义 provider 类型，请检查 AstrBot 版本")


def _cleanup_broken_instances(pm) -> tuple[dict, list]:
    """清理 framework 中可能残留的失败实例（非 VisionBridgeProvider）。

    返回 (providers_dict, provider_insts_list) 的引用。
    """
    prov_dict = getattr(pm, "providers", None)
    inst_list = getattr(pm, "provider_insts", None)
    if not isinstance(inst_list, list):
        inst_list = []

    # 清理 provider_insts 中的无效项
    for i, p_inst in enumerate(inst_list):
        if p_inst is None:
            continue
        if isinstance(p_inst, VisionBridgeProvider):
            continue
        cfg = getattr(p_inst, "provider_config", None)
        if isinstance(cfg, dict) and (cfg.get("id") == PROVIDER_ID or cfg.get("type") == PROVIDER_TYPE):
            inst_list[i] = None  # 标记为可替换位置

    # 清理 providers 字典中的旧值
    if isinstance(prov_dict, dict) and PROVIDER_ID in prov_dict:
        existing = prov_dict.get(PROVIDER_ID)
        if not isinstance(existing, VisionBridgeProvider):
            prov_dict[PROVIDER_ID] = None

    return (prov_dict if isinstance(prov_dict, dict) else {}, inst_list)


def _add_or_replace_inst(inst_list: list, prov_dict: dict, inst) -> None:
    """将实例 inst 加入到列表和字典中，优先填补 None 空位。"""
    # 替换列表中的第一个 None 或追加
    for i, p in enumerate(inst_list):
        if p is None:
            inst_list[i] = inst
            break
    else:
        inst_list.append(inst)
    prov_dict[PROVIDER_ID] = inst


async def auto_register_provider(plugin) -> bool:
    """调度注册流程：计算参数 → 注册类型 → 清理 → 实例化并加入管理器。

    若已注册则直接返回 True。
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

        # 步骤1：确保自定义类型已注册
        _register_custom_provider_type(pm)

        # 步骤2：清理可能残留的错误实例
        prov_dict, inst_list = _cleanup_broken_instances(pm)

        # 步骤3：构造配置（已包含 api_key 占位和正确的 provider_type）
        config = {
            "type": PROVIDER_TYPE,
            "id": PROVIDER_ID,
            "enable": True,
            "api_base": api_base,
            "key": [api_key] if api_key else ["placeholder"],
            "api_key": api_key if api_key else "placeholder",
            "model": model,
            "provider_type": PROVIDER_TYPE,  # 关键：不再是 "chat_completion"
        }

        # 步骤4：实例化并插入管理器
        inst = VisionBridgeProvider(
            provider_config=config,
            provider_settings={},
        )
        _add_or_replace_inst(inst_list, prov_dict, inst)

        logger.info(
            "[vision_text_bridge] 已自动注册 OpenAI compatible provider: id=%s, type=%s, api_base=%s, model=%s",
            PROVIDER_ID, PROVIDER_TYPE, api_base, model,
        )
        return True
    except Exception as e:
        logger.warning("[vision_text_bridge] auto_register_provider 失败: %s", e)
        return False


def remove_provider(plugin) -> bool:
    """卸载我方 provider (清理用)。"""
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
