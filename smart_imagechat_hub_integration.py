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
        DEFAULT_OPENAI_COMPAT_PORT,
        PROVIDER_ID,
    )
except ImportError:
    # 沙箱 fallback (constants.py 自身 import 失败 — 不应发生)
    DEFAULT_DASHBOARD_PORT = 6185
    PLUGIN_ROUTE_PREFIX = "/api/plug/astrbot_plugin_vision_text_bridge"
    OPENAI_COMPAT_PATH = "/v1/chat/completions"
    DEFAULT_OPENAI_COMPAT_PORT = 6188
    PROVIDER_ID = "vision_text_bridge_compat"

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


def _sanitize_cmd_config_file() -> bool:
    """: 清理 cmd_config.json provider list 中所有非 dict 项 — 防 'str' object has no attribute 'get' 错误.

    用户 log 00:53:50 framework 启动 AttributeError 报错说明 cmd_config.json 里 provider
    list 含某种 string entry (可能是老 _patch_user_config_type 写入损坏数据).
    framework load_provider() 调用 get_merged_provider_config(provider_config) → pc.get(...)
    → 当 pc 是 str 肘抛 AttributeError.

    修: 读 cmd_config.json, 过滤 provider list, 删除非 dict entry, 写回.

    Returns: True 清理成功, False 跳过。
    """
    cfg_path = _find_cmd_config_file()
    if cfg_path is None:
        logger.debug("_sanitize_cmd_config_file: 未找到 cmd_config.json, 跳过")
        return False
    try:
        data = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("_sanitize_cmd_config_file: 读 cmd_config.json 失败: %s", e)
        return False
    # 找 provider list
    providers = None
    if isinstance(data.get("provider"), list):
        providers = data["provider"]
    elif isinstance(data.get("providers"), list):
        providers = data["providers"]
    if providers is None:
        logger.debug("_sanitize_cmd_config_file: cmd_config.json 无 provider list, 跳过")
        return False
    # 过滤非 dict entry
    original_count = len(providers)
    clean_providers = [
        entry for entry in providers if isinstance(entry, dict) and entry.get("id")
    ]
    removed_count = original_count - len(clean_providers)
    if removed_count <= 0:
        logger.debug("_sanitize_cmd_config_file: cmd_config.json provider list 无非 dict entry, 跳过")
        return False
    logger.warning(
        "_sanitize_cmd_config_file: cmd_config.json provider list 清理 %d 个非 dict entry "
        "(原=%d, 现在=%d)",
        removed_count, original_count, len(clean_providers),
    )
    # 写回
    if isinstance(data.get("provider"), list):
        data["provider"] = clean_providers
    else:
        data["providers"] = clean_providers
    try:
        cfg_path.write_text(
            _json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("_sanitize_cmd_config_file: cmd_config.json 清理成功")
        return True
    except Exception as e:
        logger.warning("_sanitize_cmd_config_file: 写 cmd_config.json 失败: %s", e)
        return False


def _patch_user_config_type(provider_id: str, new_type: str) -> bool:
    """: 改写 AstrBot cmd_config.json 里 provider_id 的 entry — 完整修复.

    策略: **保留** entry, 让 framework 用 openai_chat_completion type 加载
    ProviderOpenAIOfficial instance (含 model_config.model=vision-bridge) — 这样
    smart_imagechat_hub 列表 filter 'openai_chat_completion' 能看到我方.

    framework 加载时 'Loading model openai_chat_completion(provider_id)' 走
    ProviderOpenAIOfficial(api_key='placeholder', base_url=我方 endpoint),
    openai SDK 不校验 placeholder, 发 HTTP 请求到我方, 我方 /v1/chat/completions
    接到后调 mmx, 返 OpenAI 格式 response. 全链路 OK, smart_imagechat_hub 列表能看到.

    完整修复 (针对老 openai_chat_completion entry):
      1. 保留 type='openai_chat_completion' (framework 已实现)
      2. key=[] → key=['placeholder'] (openai SDK 拿 string 'placeholder' 不报)
      3. api_key='' → api_key='placeholder'
      4. api_base='' → 设成我方 endpoint
      5. model_config 缺/空 → 补 {'model': 'vision-bridge'} (关键 — smart_imagechat_hub 查模型时读)

    Returns: True 改写成功 / entry 不存在时 append, False 没找到 file.
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
    if isinstance(data.get("provider"), list):
        providers = data["provider"]
    elif isinstance(data.get("providers"), list):
        providers = data["providers"]
    else:
        logger.debug("[vision_text_bridge] cmd_config.json 无 provider 列表, 跳过")
        return False
    # 找 target_entry (不删, 改它)
    target_entry = None
    for entry in providers:
        if isinstance(entry, dict) and entry.get("id") == provider_id:
            target_entry = entry
            break
    # entry 不存在 → append 新 entry
    if target_entry is None:
        try:
            from constants import DEFAULT_DASHBOARD_PORT, PLUGIN_ROUTE_PREFIX, OPENAI_COMPAT_PATH
            default_base = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
        except ImportError:
            default_base = "http://localhost:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions"
        target_entry = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "enable": True,
            "key": ["placeholder"],
            "api_key": "placeholder",
            "api_base": default_base,
            "model_config": {"model": PROVIDER_DEFAULT_MODEL},
        }
        providers.append(target_entry)
        logger.info(
            "[vision_text_bridge] cmd_config.json append 新 provider entry: id=%s, type=openai_chat_completion, model=%s",
            provider_id, PROVIDER_DEFAULT_MODEL,
        )

    changes = []
    # 1. type 改 openai_chat_completion
    if target_entry.get("type") != "openai_chat_completion":
        old_type = target_entry.get("type", "")
        changes.append(f"type {old_type!r} → 'openai_chat_completion'")
        target_entry["type"] = "openai_chat_completion"

    # 2. key 补 placeholder
    key = target_entry.get("key")
    if not isinstance(key, list) or not key or not all(isinstance(k, str) and k.strip() for k in key):
        old_key_repr = repr(key)[:30]
        target_entry["key"] = ["placeholder"]
        changes.append(f"key {old_key_repr} → ['placeholder']")

    # 3. api_key 补 placeholder
    api_key = target_entry.get("api_key", "")
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        target_entry["api_key"] = "placeholder"
        changes.append("api_key '' → 'placeholder'")

    # 4. api_base 补 我方 endpoint
    api_base = target_entry.get("api_base", "")
    if not api_base or not isinstance(api_base, str) or not api_base.strip():
        try:
            from constants import DEFAULT_DASHBOARD_PORT, PLUGIN_ROUTE_PREFIX, OPENAI_COMPAT_PATH
            default_base = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"
        except ImportError:
            default_base = "http://localhost:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions"
        target_entry["api_base"] = default_base
        changes.append(f"api_base '' → {default_base!r}")

    # 5. model_config 补 {'model': 'vision-bridge'} — 关键: smart_imagechat_hub 查模型时读
    model_cfg = target_entry.get("model_config")
    if not isinstance(model_cfg, dict) or not model_cfg.get("model"):
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            target_entry["model_config"] = model_cfg
        model_cfg["model"] = PROVIDER_DEFAULT_MODEL
        changes.append(f"model_config.model = {PROVIDER_DEFAULT_MODEL!r}")

    if not changes:
        logger.debug("[vision_text_bridge] cmd_config.json id=%s 已正确, 跳过", provider_id)
        return True

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
        logger.debug("is_external_image_caption_plugin_installed: 命中缓存 -> %s", _INSTALL_CHECK_CACHE)
        return _INSTALL_CHECK_CACHE
    logger.debug("is_external_image_caption_plugin_installed: 缓存未命中，开始磁盘检测")
    root = _get_plugin_root()
    if root is None:
        logger.debug("is_external_image_caption_plugin_installed: 无法确定插件根目录，返回 False")
        _INSTALL_CHECK_CACHE = False
        return False
    candidates = [
        root / "data" / "plugins" / PLUGIN_NAME / "metadata.yaml",
        root / "plugins" / PLUGIN_NAME / "metadata.yaml",
    ]
    logger.debug("is_external_image_caption_plugin_installed: 检查候选文件: %s", candidates)
    for c in candidates:
        if c.is_file():
            logger.debug("is_external_image_caption_plugin_installed: 找到文件 %s，判定已安装", c)
            _INSTALL_CHECK_CACHE = True
            return True
    logger.debug("is_external_image_caption_plugin_installed: 未找到 metadata.yaml，判定未安装")
    _INSTALL_CHECK_CACHE = False
    return False


def get_smart_imagechat_hub_dir() -> Optional[Path]:
    """返回 smart_imagechat_hub 安装路径 (如装了)。"""
    root = _get_plugin_root()
    if root is None:
        logger.debug("get_external_image_caption_plugin_dir: 无法获取根目录")
        return None
    for sub in ("data/plugins", "plugins"):
        candidate = root / sub / PLUGIN_NAME
        logger.debug("get_external_image_caption_plugin_dir: 尝试路径 %s", candidate)
        if candidate.is_dir():
            logger.debug("get_external_image_caption_plugin_dir: 找到目录 %s", candidate)
            return candidate
    logger.debug("get_external_image_caption_plugin_dir: 未找到插件目录")
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
        "model": model_name,
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
        # : P1 fix — 加查 pm.providers 字典 (framework 启动时 instantiate 进入)
        prov_dict = getattr(pm, "providers", None)
        if isinstance(prov_dict, dict) and prov_dict.get(PROVIDER_ID) is not None:
            logger.debug("is_provider_already_registered: pm.providers[%s] 已有 instance", PROVIDER_ID)
            return True
        # 检查已实例化的 provider 列表中是否已有 VisionBridgeProvider
        for i, prov in enumerate(getattr(pm, "provider_insts", [])):
            if isinstance(prov, VisionBridgeProvider):
                logger.debug("is_provider_already_registered: 发现 VisionBridgeProvider 实例，已注册")
                return True
            cfg = getattr(prov, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                logger.debug("is_provider_already_registered: provider_insts[%d] id=PROVIDER_ID, 已注册", i)
                return True
        logger.debug("is_provider_already_registered: 未找到已注册实例")
        return False
    except Exception as e:
        logger.debug("is_provider_already_registered: 检查过程中异常: %s", e)
        return False


def _build_api_base(plugin) -> str:
    """计算本插件提供的 OpenAI 兼容端点地址。

    现走独立 server (main_server.py) port = DEFAULT_OPENAI_COMPAT_PORT — bypass
    framework legacy_router JWT middleware. framework /api/plug/<plugin>/* 对
    /v1/chat/completions path 需要 JWT token, openai SDK 发 'Bearer placeholder'
    导致 401 'Token 无效'.
    """
    try:
        openai_compat_port = int(plugin.config.get("openai_compat_port") or DEFAULT_OPENAI_COMPAT_PORT)
    except Exception:
        openai_compat_port = DEFAULT_OPENAI_COMPAT_PORT
    url = f"http://127.0.0.1:{openai_compat_port}/v1/chat/completions"
    logger.debug("_build_api_base (solo server): port=%d, url=%s", openai_compat_port, url)
    return url


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
            # : P1 fix — 不再清 framework ProviderOpenAIOfficial instance
            #   之前 cfg_id == PROVIDER_ID 会标记 framework instance 为 None
            #   framework instance 是合法的 (openai_chat_completion type, meta() work)
            #   仅清旧 vision_bridge_compat type instance (老版本残留 - meta() 抛错)
            from .constants import PROVIDER_TYPE  # 'vision_bridge_compat' 旧 type
            if cfg_type == PROVIDER_TYPE and cfg_type != "openai_chat_completion":
                logger.debug("_cleanup_broken_instances: 标记实例[%d]为 None (旧 vision_bridge_compat 类型)", i)
                inst_list[i] = None  # 标记为可替换位置
        else:
            logger.debug("_cleanup_broken_instances: 实例[%d] 无有效 provider_config", i)

    # 清理 providers 字典中的旧值
    if isinstance(prov_dict, dict) and PROVIDER_ID in prov_dict:
        existing = prov_dict.get(PROVIDER_ID)
        # : P1 fix — 不再清 framework ProviderOpenAIOfficial instance
        #   framework instance 是合法的 (meta() 走 provider_cls_map["openai_chat_completion"] work)
        #   我方 plugin 后启动 — 已有 instance — keep
        if isinstance(existing, VisionBridgeProvider):
            pass  # 我方自己的 — keep
        # 其它 (ProviderOpenAIOfficial instance from framework) — keep
        else:
            logger.debug("_cleanup_broken_instances: providers[%s] 是 framework instance, keep", PROVIDER_ID)

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


async def auto_register_provider(plugin, log_details: bool = False) -> bool:
    """调度注册流程：先持久化改写 user config → 计算参数 → 注册类型 → 清理 → 实例化并加入管理器。

    若已注册则直接返回 True。
    """
    logger.debug("auto_register_provider: 开始自动注册流程")
    # : 关键 — 先调用 framework create_provider 接口 — dashboard 「模型提供商」页面实时可见
    # 持久化改写 user config — 保留 type=openai_chat_completion (framework 原生支持), 但补全 key/api_key/api_base
    _patch_user_config_type(PROVIDER_ID, "openai_chat_completion")
    # : 清理 cmd_config.json 中可能的损坏 entry (str entry  → framework AttributeError)
    _sanitize_cmd_config_file()
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
        model_name = (
            plugin.config.get("model_name")
            or plugin.config.get("openai_compat_model_name")
            or plugin.config.get("smart_imagechat_hub_model_name")
            or PROVIDER_DEFAULT_MODEL
        )
        full_model_name = model_name  # 重命名 — 在后面 provider_config 改写里用
        logger.debug("auto_register_provider: api_base=%s, api_key_is_set=%s, model=%s",
                     api_base, bool(api_key), model_name)

        # : P0 fix — 不再调 _register_custom_provider_type!
        #   之前该函数最后 "pm.provider_class_map = {PROVIDER_TYPE: VisionBridgeProvider}"
        #   **重置** framework 内部 map (如果 framework 用这个 attr) — 杀掉其它 25 个 provider entry 实例化
        #   我方 meta() **已** override 返 openai_chat_completion type — 框架已注册
        #   **不** 需要注册 custom type — 跳这一步
        logger.debug("auto_register_provider: 步骤1 跳过 _register_custom_provider_type (P0 fix)")

        # 步骤2：清理可能残留的错误实例
        logger.debug("auto_register_provider: 步骤2 - 清理残留实例")
        prov_dict, inst_list = _cleanup_broken_instances(pm)

        # 步骤3：构造配置（已包含 api_key 占位和正确的 provider_type）
        logger.debug("auto_register_provider: 步骤3 - 构造配置")
        config = {
            "type": "openai_chat_completion",  # 关键 — framework 加载 ProviderOpenAIOfficial (provider_cls_map 里有)
            "id": PROVIDER_ID,
            "enable": True,
            "api_base": api_base,
            "key": [api_key] if api_key else ["placeholder"],
            "api_key": api_key if api_key else "placeholder",
            "model": model_name,
            "provider_type": "chat_completion",
        }
        logger.debug("auto_register_provider: 配置内容=%s", config)

        # : P0 fix — ALWAYS instantiate 我方 VisionBridgeProvider + add pm.provider_insts
        #   之前 fallback 只在 framework 无 instance 时 instantiate
        #   真实 bug: framework instance meta() 调用 provider_cls_map['openai_chat_completion']
        #   **理论上** work 但之前 meta() 返 dict（dict 没 .id attr）→ sih getattr(meta, 'id') AttributeError → 跳过
        #   现在我方 meta() 返 ProviderMeta dataclass → 一定 work
        #   instantiate 我方 instance + add pm.provider_insts 即使 framework 已有
        #   我方 instance 是 primary, framework instance 是 secondary (做 framework 调用)

        my_inst = VisionBridgeProvider(
            provider_config=config,
            provider_settings={},
        )
        # : 把我方 instance 加到 pm.provider_insts (用 add_or_replace 替换 None)
        #   不重复 add (重复 add 会导致 sih dropdown 重复显示我方)
        already_in_list = any(
            isinstance(p, VisionBridgeProvider)
            for p in getattr(pm, "provider_insts", [])
        )
        if not already_in_list:
            _add_or_replace_inst(inst_list, prov_dict, my_inst)
            logger.debug("auto_register_provider: 已 add 我方 VisionBridgeProvider instance 到 provider_insts")
        else:
            logger.debug("auto_register_provider: 我方 VisionBridgeProvider instance 已在 list 中, skip add")
            # 替换 pm.providers[id] 指向我方 instance (framework meta 也 work)
            if isinstance(prov_dict, dict):
                prov_dict[PROVIDER_ID] = my_inst
            framework_inst = my_inst

        # : 修 framework instance (if 已存在 && 不是 我方 instance) — 补全 model_config
        framework_inst = prov_dict.get(PROVIDER_ID) if isinstance(prov_dict, dict) else None
        if framework_inst is not None and not isinstance(framework_inst, VisionBridgeProvider):
            if hasattr(framework_inst, "provider_config") and isinstance(framework_inst.provider_config, dict):
                if "model_config" not in framework_inst.provider_config or not isinstance(
                    framework_inst.provider_config.get("model_config"), dict
                ):
                    framework_inst.provider_config["model_config"] = {
                        "model": full_model_name,
                    }
                    logger.debug("auto_register_provider: 补全 framework instance.model_config")
            if hasattr(framework_inst, "set_model"):
                try:
                    framework_inst.set_model(full_model_name)
                except Exception as e:
                    logger.debug("auto_register_provider: set_model 失败: %s", e)
            if hasattr(framework_inst, "model_name"):
                try:
                    framework_inst.model_name = full_model_name
                except Exception:
                    pass

        # : 同步写 pm.providers_config[id] = config — framework 查模型时读这个 dict
        if isinstance(prov_dict, dict):
            providers_config = getattr(pm, "providers_config", None)
            if not isinstance(providers_config, dict):
                try:
                    providers_config = {}
                    pm.providers_config = providers_config
                except Exception:
                    providers_config = None
            if isinstance(providers_config, dict):
                # : 构造完整 config — 跟 ProviderOpenAIOfficial 期望的格式一致
                #   必须含 type='openai_chat_completion' 才能让 meta() 找到 provider_cls_map
                full_config = {
                    "id": PROVIDER_ID,
                    "type": "openai_chat_completion",   # 关键 — framework 走 meta() 找此 type
                    "provider_type": "chat_completion",
                    "enable": True,
                    "key": config["key"],
                    "api_key": config["api_key"],
                    "api_base": api_base,
                    "model": full_model_name,
                    "model_config": {"model": full_model_name},
                }
                providers_config[PROVIDER_ID] = full_config
                logger.debug("auto_register_provider: 同步 pm.providers_config[id] type=%s", full_config["type"])

        # : P1 fix — 调 framework create_provider 让 dashboard「模型提供商」页面实时显示我方
        #   这是 dashboard "新增模型提供商" 走的标准后端 API, 我方模拟同一调用
        #   也避免 cmd_config.json 仍有 str 损环 entry 导致框架启动 AttributeError
        #   create_provider 内部: append to config['provider'] + load_provider() + save
        try:
            if hasattr(pm, "create_provider"):
                # 检查 entry 是否已存在 (framework 用 id 判定 — 重复会抛 ValueError)
                existing = None
                if isinstance(getattr(pm, "providers_config", None), list):
                    for ec in pm.providers_config:
                        if isinstance(ec, dict) and ec.get("id") == PROVIDER_ID:
                            existing = ec
                            break
                if existing is None:
                    logger.info(
                        "auto_register_provider: 调 framework pm.create_provider(%s) — 让 dashboard 模型提供商页面实时看到",
                        PROVIDER_ID,
                    )
                    create_cfg = {
                        "id": PROVIDER_ID,
                        "type": "openai_chat_completion",
                        "provider_type": "chat_completion",
                        "enable": True,
                        "key": ["placeholder"],
                        "api_key": "placeholder",
                        "api_base": api_base,
                        "model": full_model_name,
                        "model_config": {"model": full_model_name},
                    }
                    try:
                        await pm.create_provider(create_cfg)
                        logger.info("auto_register_provider: pm.create_provider 成功 — instance 已 instantiate")
                    except ValueError as ve:
                        # : Provider ID exists — 更新 (update_provider reload)
                        if "already exists" in str(ve):
                            logger.debug("auto_register_provider: pm.create_provider 报重复 id, 走 update_provider")
                            try:
                                await pm.update_provider(PROVIDER_ID, create_cfg)
                                logger.info("auto_register_provider: pm.update_provider 成功")
                            except Exception as ue:
                                logger.debug("auto_register_provider: update_provider 失败: %s", ue)
                        else:
                            logger.debug("auto_register_provider: pm.create_provider ValueError: %s", ve)
                else:
                    logger.debug("auto_register_provider: pm.providers_config 已有 %s entry, skip create", PROVIDER_ID)
            else:
                logger.debug("auto_register_provider: pm 没有 create_provider 方法, skip")
        except Exception as pe:
            logger.debug("auto_register_provider: 调 framework create_provider 异常 (不影响其它): %s", pe)

        logger.info(
            "[vision_text_bridge] 已自动注册 OpenAI compatible provider: id=%s, type=%s, api_base=%s, model=%s",
            PROVIDER_ID, "openai_chat_completion", api_base, model_name,
        )
        if log_details:
            _log_registered_instance(plugin)
        else:
            logger.debug("auto_register_provider: 注册完成, log_details=False, 跳过集中 log")
        return True
    except Exception as e:
        logger.warning("[vision_text_bridge] auto_register_provider 失败: %s", e)
        logger.debug("auto_register_provider: 异常详情", exc_info=True)
        return False


def _log_registered_instance(plugin) -> None:
    """: 集中 log 输出 — 用户要求 #3 排查用.

    输出:
      - provider_id        (AstrBot dashboard 显示的 id)
      - provider_instance_id (内存地址 0x... — framework 给 instance 分配的唯一 ID)
      - api_base           (POST endpoint URL)
      - api_key            (脱敏 — 前 4 + *** + 后 4)
      - model              (模型昵称 id)
    """
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            return
        prov_dict = getattr(pm, "providers", {})
        inst = prov_dict.get(PROVIDER_ID) if isinstance(prov_dict, dict) else None
        if inst is None:
            # 兼容: 也可能在 provider_insts 列表里
            for p_inst in getattr(pm, "provider_insts", []):
                if isinstance(p_inst, VisionBridgeProvider):
                    inst = p_inst
                    break
        if inst is None:
            logger.warning("[vision_text_bridge] _log_registered_instance: 找不到已注册 instance")
            return
        # : instance 唯一 ID — Python id() 内存地址 (框架级 instance 唯一)
        instance_id = f"0x{id(inst):08x}"
        api_base = getattr(inst, "api_base", "") or ""
        api_key = getattr(inst, "api_key", "") or ""
        model = getattr(inst, "_current_model", None) or getattr(inst, "model", "") or ""
        # 脱敏
        if len(api_key) > 8:
            key_masked = api_key[:4] + "***" + api_key[-4:]
        else:
            key_masked = "***"
        logger.info(
            "[vision_text_bridge] provider 已就绪 — 完整配置:\n"
            "  provider_id        (AstrBot dashboard 显示名) = %s\n"
            "  provider_instance_id (内存唯一 ID)           = %s\n"
            "  api_base           (POST endpoint URL)        = %s\n"
            "  api_key            (脱敏)                    = %s\n"
            "  model              (模型昵称 id)             = %s",
            PROVIDER_ID, instance_id, api_base, key_masked, model,
        )
    except Exception as e:
        logger.debug("[vision_text_bridge] _log_registered_instance 异常: %s", e)


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
