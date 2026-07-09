"""
设计: /v1/chat/completions 接管 image caption / 启动期自动注册 OpenAI provider
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

import json as _json
from pathlib import Path as _Path


def _find_cmd_config_file() -> "_Path | None":
    """: 找 AstrBot cmd_config.json 路径.

    官方文档: 配置在 ``data/cmd_config.json`` 的 provider 字段中.
    启发式: 当前进程 cwd 向上找 data/cmd_config.json (向上 5 层), 退到硬编码候选.
    """
    candidates = []
    # 官方: data/cmd_config.json
    candidates.append(_Path.cwd() / "data" / "cmd_config.json")
    cur = _Path.cwd()
    for _ in range(5):
        cand = cur / "data" / "cmd_config.json"
        if cand.exists():
            return cand
        cur = cur.parent
    # 退到硬编码候选
    candidates.extend([
        _Path("/AstrBot/data/cmd_config.json"),
        _Path.cwd() / "data" / "config" / "cmd_config.json",
        _Path("/AstrBot/data/config/cmd_config.json"),
        _Path("/AstrBot/cmd_config.json"),
    ])
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _patch_user_config_type(provider_id: str, new_type: str) -> bool:
    """: 改写 AstrBot cmd_config.json 里 provider_id 的 entry — 完整修复.

    根因 (2026-07-09 AstrBot 4.26.4):
      - openai_chat_completion type 用 ProviderOpenAIOfficial, 读 config["key"]
      - 老 config 默认 key=[] (空 list), chosen_api_key = api_keys[0] if api_keys else None
      - AsyncOpenAI(api_key=None) → 报 'Missing credentials' 错
      - 不管 type 改成啥, framework 加载 user config 时仍可能 read 到空 key

    完整修复 (针对老 openai_chat_completion entry):
      1. 保留 type='openai_chat_completion' (framework 已实现)
      2. key=[] → key=['placeholder'] (openai SDK 拿到 string 'placeholder' 不报)
      3. api_key='' → api_key='placeholder' (部分版本读这个字段)
      4. api_base='' → 设成我方 endpoint (OpenAI SDK 发 HTTP 过来)
      5. model='' → 设成 'vision-bridge' (我方可用任意)

    这样 framework 启动时 'Loading model openai_chat_completion(provider_id)' 走
    ProviderOpenAIOfficial(api_key='placeholder', base_url=我方 endpoint),
    openai SDK 不校验 placeholder, 发请求到我方, 我方 /v1/chat/completions 接到后调 mmx,
    返 OpenAI 格式 response. 全链路 OK, 不再 Missing credentials.

    Returns: True 改写成功, False 没找到 file / entry.
    """
    cfg_path = _find_cmd_config_file()
    if cfg_path is None:
        logger.debug("[vision_text_bridge] cmd_config.json 未找到, 跳过持久化改写")
        return False
    try:
        data = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("[vision_text_bridge] 读 cmd_config.json 失败: %s", e)
        return False
    # AstrBot cmd_config.json 格式: {"provider": [{"id": "...", "type": "...", "key": [], ...}, ...]}
    if isinstance(data.get("provider"), list):
        providers = data["provider"]
        provider_key = "provider"
    elif isinstance(data.get("providers"), list):
        providers = data["providers"]
        provider_key = "providers"
    else:
        logger.debug("[vision_text_bridge] cmd_config.json 无 provider 列表, 跳过")
        return False
    # 找目标 entry
    target_idx = None
    target_entry = None
    for i, entry in enumerate(providers):
        if isinstance(entry, dict) and entry.get("id") == provider_id:
            target_idx = i
            target_entry = entry
            break
    if target_entry is None:
        logger.debug("[vision_text_bridge] cmd_config.json 无 id=%s entry, 跳过", provider_id)
        return False

    changes = []
    # 1. type 改 openai_chat_completion (保留 framework 原生支持)
    old_type = target_entry.get("type", "")
    if old_type != "openai_chat_completion":
        changes.append(f"type {old_type!r} → 'openai_chat_completion'")
        target_entry["type"] = "openai_chat_completion"

    # 2. key=[] 或空 → key=["placeholder"]
    key = target_entry.get("key")
    if not isinstance(key, list) or not key or not all(isinstance(k, str) and k.strip() for k in key):
        old_key_repr = repr(key)[:30]
        target_entry["key"] = ["placeholder"]
        changes.append(f"key {old_key_repr} → ['placeholder']")

    # 3. api_key 空 → 'placeholder'
    api_key = target_entry.get("api_key", "")
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        target_entry["api_key"] = "placeholder"
        changes.append("api_key '' → 'placeholder'")

    # 4. api_base 空 → 设成我方 endpoint
    api_base = target_entry.get("api_base", "")
    if not api_base or not isinstance(api_base, str) or not api_base.strip():
        # 用默认值 (从 main.py 拿 dashboard_port + host)
        try:
            from constants import DEFAULT_DASHBOARD_PORT, PLUGIN_ROUTE_PREFIX, OPENAI_COMPAT_PATH
            default_base = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
        except ImportError:
            default_base = "http://localhost:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions"
        target_entry["api_base"] = default_base
        changes.append(f"api_base '' → {default_base!r}")

    # 5. model 空 → 'vision-bridge'
    model_cfg = target_entry.get("model_config") or {}
    if not isinstance(model_cfg, dict):
        model_cfg = {"model": "vision-bridge"}
        target_entry["model_config"] = model_cfg
        changes.append("model_config → {'model': 'vision-bridge'}")
    elif not model_cfg.get("model"):
        model_cfg["model"] = "vision-bridge"
        changes.append("model_config.model '' → 'vision-bridge'")

    if not changes:
        logger.debug("[vision_text_bridge] cmd_config.json id=%s 已正确, 跳过", provider_id)
        return False

    logger.info(
        "[vision_text_bridge] 改写 cmd_config.json id=%s: %s",
        provider_id, "; ".join(changes),
    )
    try:
        cfg_path.write_text(
            _json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning("[vision_text_bridge] 写 cmd_config.json 失败: %s", e)
        return False




PLUGIN_NAME = "astrbot_plugin_smart_imagechat_hub"
_INSTALL_CHECK_CACHE: Optional[bool] = None  # module-level cache, 避免每次请求都查盘


def _get_plugin_root() -> Optional[Path]:
    """探测 AstrBot 插件目录 (data/plugins/) 的可能位置。"""
    try:
        from astrbot.api.star import StarTools
        d = StarTools.get_data_dir()
        root = d.parent.parent
        logger.debug("_get_plugin_root: 通过 StarTools 获取根目录=%s", root)
        return root
    except Exception as e:
        logger.debug("_get_plugin_root: StarTools 失败: %s", e)
        return None


def is_smart_imagechat_hub_installed() -> bool:
    """检测 smart_imagechat_hub 是否安装。

    用 module-level cache 缓存结果 (插件生命周期内不会变, 不用每次重新查盘)。
    测试时可调 reset_cache_for_testing() 清。
    """
    global _INSTALL_CHECK_CACHE
    if _INSTALL_CHECK_CACHE is not None:
        logger.debug("is_smart_imagechat_hub_installed: 命中缓存 -> %s", _INSTALL_CHECK_CACHE)
        return _INSTALL_CHECK_CACHE
    logger.debug("is_smart_imagechat_hub_installed: 缓存未命中，开始磁盘检测")
    root = _get_plugin_root()
    if root is None:
        logger.debug("is_smart_imagechat_hub_installed: 无法确定插件根目录，返回 False")
        _INSTALL_CHECK_CACHE = False
        return False
    candidates = [
        root / "data" / "plugins" / PLUGIN_NAME / "metadata.yaml",
        root / "plugins" / PLUGIN_NAME / "metadata.yaml",
    ]
    logger.debug("is_smart_imagechat_hub_installed: 检查候选文件: %s", candidates)
    for c in candidates:
        if c.is_file():
            logger.debug("is_smart_imagechat_hub_installed: 找到文件 %s，判定已安装", c)
            _INSTALL_CHECK_CACHE = True
            return True
    logger.debug("is_smart_imagechat_hub_installed: 未找到 metadata.yaml，判定未安装")
    _INSTALL_CHECK_CACHE = False
    return False


def get_smart_imagechat_hub_dir() -> Optional[Path]:
    """返回 smart_imagechat_hub 安装路径 (如装了)。"""
    root = _get_plugin_root()
    if root is None:
        logger.debug("get_smart_imagechat_hub_dir: 无法获取根目录")
        return None
    for sub in ("data/plugins", "plugins"):
        candidate = root / sub / PLUGIN_NAME
        logger.debug("get_smart_imagechat_hub_dir: 尝试路径 %s", candidate)
        if candidate.is_dir():
            logger.debug("get_smart_imagechat_hub_dir: 找到目录 %s", candidate)
            return candidate
    logger.debug("get_smart_imagechat_hub_dir: 未找到 hub 目录")
    return None


def reset_cache_for_testing() -> None:
    """清 module-level cache (测试用)。"""
    global _INSTALL_CHECK_CACHE
    _INSTALL_CHECK_CACHE = None
    logger.debug("reset_cache_for_testing: 安装检测缓存已清空")


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
    config = {
        "type": PROVIDER_TYPE,
        "id": PROVIDER_ID,
        "enable": True,
        "api_base": api_base.rstrip("/"),
        "key": [api_key] if api_key else ["placeholder"],
        "api_key": api_key if api_key else "placeholder",
        "model": model,
        "provider_type": PROVIDER_TYPE,
    }
    logger.debug("build_provider_config: 生成配置=%s", config)
    return config


def is_provider_already_registered(plugin) -> bool:
    """检查 vision_text_bridge_compat provider 是否已注册 (避免重复)."""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            logger.debug("is_provider_already_registered: provider_manager 不存在")
            return False
        # 检查已实例化的 provider 列表中是否已有 VisionBridgeProvider
        for i, prov in enumerate(getattr(pm, "provider_insts", [])):
            logger.debug("is_provider_already_registered: 检查实例[%d] type=%s", i, type(prov).__name__)
            if isinstance(prov, VisionBridgeProvider):
                logger.debug("is_provider_already_registered: 发现 VisionBridgeProvider 实例，已注册")
                return True
            # 兼容性检查：也可能保留了旧的 openai_chat_completion 实例
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict):
                cfg_id = cfg.get("id")
                logger.debug("is_provider_already_registered: 实例[%d] config.id=%s", i, cfg_id)
                if cfg_id == PROVIDER_ID:
                    logger.debug("is_provider_already_registered: 根据 config.id 判定已注册")
                    return True
        logger.debug("is_provider_already_registered: 未找到已注册实例")
        return False
    except Exception as e:
        logger.debug("is_provider_already_registered: 检查过程中异常: %s", e)
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
        url = f"http://{host}:{port}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
        logger.debug("_build_api_base: host=%s, port=%s, url=%s", host, port, url)
        return url
    except Exception as e:
        logger.debug("_build_api_base: 计算异常，使用默认值: %s", e)
        return f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"


def _register_custom_provider_type(pm) -> None:
    """使用 provider_manager 的公开方法注册自定义适配器类。

    优先级：register_provider_type > provider_type_map > 主动设置。
    """
    logger.debug("_register_custom_provider_type: 开始注册类型 %s", PROVIDER_TYPE)

    # 1. 优先使用框架标准注册接口 (AstrBot >=4.26)
    if hasattr(pm, "register_provider_type"):
        logger.debug("_register_custom_provider_type: 发现 register_provider_type 方法")
        try:
            pm.register_provider_type(PROVIDER_TYPE, VisionBridgeProvider)
            logger.info("已通过 register_provider_type 注册自定义 provider: %s", PROVIDER_TYPE)
            logger.debug("_register_custom_provider_type: 注册成功")
            return
        except Exception as e:
            logger.warning("register_provider_type 调用失败: %s", e)

    # 2. 尝试设置已知的公开映射表
    for attr_name in ("provider_type_map", "custom_providers", "provider_class_map"):
        mapping = getattr(pm, attr_name, None)
        if isinstance(mapping, dict):
            logger.debug("_register_custom_provider_type: 向 %s 注入映射", attr_name)
            mapping[PROVIDER_TYPE] = VisionBridgeProvider
            logger.info("已注入 provider 类型映射到 %s", attr_name)
            logger.debug("_register_custom_provider_type: 注入完成")
            return
        else:
            logger.debug("_register_custom_provider_type: 属性 %s 不存在或不是字典", attr_name)

    # 3. 最后尝试直接设置属性（兼容性极低）
    try:
        pm.provider_class_map = {PROVIDER_TYPE: VisionBridgeProvider}
        logger.warning("已通过直接设置 provider_class_map 注册")
        logger.debug("_register_custom_provider_type: 直接设置 provider_class_map 成功")
    except Exception as e:
        logger.error("无法注册自定义 provider 类型，请检查 AstrBot 版本: %s", e)


def _cleanup_broken_instances(pm) -> tuple[dict, list]:
    """清理 framework 中可能残留的失败实例（非 VisionBridgeProvider）。

    返回 (providers_dict, provider_insts_list) 的引用。
    """
    prov_dict = getattr(pm, "providers", None)
    inst_list = getattr(pm, "provider_insts", None)
    if not isinstance(inst_list, list):
        inst_list = []
    logger.debug("_cleanup_broken_instances: 清理前 provider_insts 长度=%d, providers keys=%s",
                 len(inst_list), list(prov_dict.keys()) if isinstance(prov_dict, dict) else "N/A")

    # 清理 provider_insts 中的无效项
    for i, p_inst in enumerate(inst_list):
        if p_inst is None:
            continue
        if isinstance(p_inst, VisionBridgeProvider):
            continue
        cfg = getattr(p_inst, "provider_config", None)
        if isinstance(cfg, dict):
            cfg_id = cfg.get("id")
            cfg_type = cfg.get("type")
            logger.debug("_cleanup_broken_instances: 检查实例[%d] type=%s, config.id=%s, config.type=%s",
                         i, type(p_inst).__name__, cfg_id, cfg_type)
            if cfg_id == PROVIDER_ID or cfg_type == PROVIDER_TYPE:
                logger.debug("_cleanup_broken_instances: 标记实例[%d]为 None (旧实例)", i)
                inst_list[i] = None  # 标记为可替换位置
        else:
            logger.debug("_cleanup_broken_instances: 实例[%d] 无有效 provider_config", i)

    # 清理 providers 字典中的旧值
    if isinstance(prov_dict, dict) and PROVIDER_ID in prov_dict:
        existing = prov_dict.get(PROVIDER_ID)
        if not isinstance(existing, VisionBridgeProvider):
            logger.debug("_cleanup_broken_instances: providers[%s] 不是 VisionBridgeProvider，置为 None", PROVIDER_ID)
            prov_dict[PROVIDER_ID] = None

    logger.debug("_cleanup_broken_instances: 清理后 provider_insts 长度=%d", len(inst_list))
    return (prov_dict if isinstance(prov_dict, dict) else {}, inst_list)


def _add_or_replace_inst(inst_list: list, prov_dict: dict, inst) -> None:
    """将实例 inst 加入到列表和字典中，优先填补 None 空位。"""
    logger.debug("_add_or_replace_inst: 当前列表长度=%d", len(inst_list))
    # 替换列表中的第一个 None 或追加
    for i, p in enumerate(inst_list):
        if p is None:
            logger.debug("_add_or_replace_inst: 在索引 %d 处替换 None", i)
            inst_list[i] = inst
            break
    else:
        logger.debug("_add_or_replace_inst: 未找到 None 空位，追加到末尾")
        inst_list.append(inst)
    prov_dict[PROVIDER_ID] = inst
    logger.debug("_add_or_replace_inst: 字典已更新 key=%s", PROVIDER_ID)


async def auto_register_provider(plugin) -> bool:
    """调度注册流程：先持久化改写 user config → 计算参数 → 注册类型 → 清理 → 实例化并加入管理器。

    若已注册则直接返回 True。
    """
    logger.debug("auto_register_provider: 开始自动注册流程")
    # : 关键 — 先改写 user config, 否则 framework 启动时用老 type 加载 → 失败
    # 持久化改写 user config — 保留 type=openai_chat_completion (framework 原生支持), 但补全 key/api_key/api_base
    _patch_user_config_type(PROVIDER_ID, "openai_chat_completion")
    if is_provider_already_registered(plugin):
        logger.debug("auto_register_provider: 已注册，直接返回 True")
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
        logger.debug("auto_register_provider: api_base=%s, api_key_is_set=%s, model=%s",
                     api_base, bool(api_key), model)

        # 步骤1：确保自定义类型已注册
        logger.debug("auto_register_provider: 步骤1 - 注册自定义类型")
        _register_custom_provider_type(pm)

        # 步骤2：清理可能残留的错误实例
        logger.debug("auto_register_provider: 步骤2 - 清理残留实例")
        prov_dict, inst_list = _cleanup_broken_instances(pm)

        # 步骤3：构造配置（已包含 api_key 占位和正确的 provider_type）
        logger.debug("auto_register_provider: 步骤3 - 构造配置")
        config = {
            "type": PROVIDER_TYPE,
            "id": PROVIDER_ID,
            "enable": True,
            "api_base": api_base,
            "key": [api_key] if api_key else ["placeholder"],
            "api_key": api_key if api_key else "placeholder",
            "model": model,
            "provider_type": PROVIDER_TYPE,
        }
        logger.debug("auto_register_provider: 配置内容=%s", config)

        # 步骤4：实例化并插入管理器
        logger.debug("auto_register_provider: 步骤4 - 实例化 VisionBridgeProvider")
        inst = VisionBridgeProvider(
            provider_config=config,
            provider_settings={},
        )
        _add_or_replace_inst(inst_list, prov_dict, inst)

        logger.info(
            "[vision_text_bridge] 已自动注册 OpenAI compatible provider: id=%s, type=%s, api_base=%s, model=%s",
            PROVIDER_ID, PROVIDER_TYPE, api_base, model,
        )
        logger.debug("auto_register_provider: 注册成功，返回 True")
        return True
    except Exception as e:
        logger.warning("[vision_text_bridge] auto_register_provider 失败: %s", e)
        logger.debug("auto_register_provider: 异常详情", exc_info=True)
        return False


def remove_provider(plugin) -> bool:
    """卸载我方 provider (清理用)。"""
    logger.debug("remove_provider: 开始卸载 provider")
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            logger.debug("remove_provider: provider_manager 不存在")
            return False
        for prov in list(getattr(pm, "provider_insts", [])):
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                logger.debug("remove_provider: 找到目标 provider id=%s, 准备卸载", PROVIDER_ID)
                try:
                    if hasattr(prov, "terminate"):
                        prov.terminate()
                except Exception as e:
                    logger.debug("remove_provider: terminate 调用异常: %s", e)
                pm.provider_insts.remove(prov)
                logger.info("[vision_text_bridge] 已卸载 provider: %s", PROVIDER_ID)
                logger.debug("remove_provider: 卸载完成")
                return True
        logger.debug("remove_provider: 未找到需要卸载的 provider")
        return False
    except Exception as e:
        logger.debug("remove_provider: 卸载过程异常: %s", e)
        return False
