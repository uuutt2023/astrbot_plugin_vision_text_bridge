"""通过 AstrBot webui HTTP API 注册 provider — 不修改、不注入。

设计 (用户 10:08/10:28 要求):
  - 不改 cmd_config.json
  - 不注入 framework 内部状态 (pm.providers / pm.provider_insts)
  - 通过 webui 接口 (POST /api/v1/providers) 注册
  - endpoint 用独立 server (127.0.0.1:2023) — bypass framework legacy_router JWT
"""
from __future__ import annotations

from pathlib import Path as _Path
from typing import Optional

import httpx as _httpx

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_vision_text_bridge")

from constants import (
    PROVIDER_ID,
    DEFAULT_OPENAI_COMPAT_PORT,
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_MODEL,
)


def _get_plugin_root() -> Optional[_Path]:
    """探测 AstrBot 根目录 (向上 5 层)。"""
    try:
        cur = _Path.cwd()
        for _ in range(5):
            if (cur / "astrbot" / "core").is_dir() or (cur / "data").is_dir():
                return cur
            cur = cur.parent
    except Exception:
        pass
    return _Path("/AstrBot")


def is_smart_imagechat_hub_installed() -> bool:
    """跨进程 cache 检测外部图片理解插件是否安装。"""
    root = _get_plugin_root()
    if root is None:
        return False
    candidates = [
        root / "data" / "plugins" / "astrbot_plugin_smart_imagechat_hub" / "main.py",
    ]
    for c in candidates:
        if c.is_file():
            return True
    return False


def _read_webui_credentials(plugin) -> tuple[str, str, int]:
    """读 dashboard 用户名/密码，Dashboard 端口固定 6185。"""
    username = ""
    password = ""
    port = DEFAULT_DASHBOARD_PORT
    try:
        pc = plugin.config if plugin and hasattr(plugin, "config") else {}
        if isinstance(pc, dict):
            cu = pc.get("webui_username") or pc.get("dashboard_username")
            cp = pc.get("webui_password") or pc.get("dashboard_password")
            if cu:
                username = cu.strip()
            if cp:
                password = cp.strip()
    except Exception as e:
        logger.debug("_read_webui_credentials 异常: %s", e)
    return username, password, port


async def auto_register_provider(plugin, log_details: bool = False) -> bool:
    """通过 webui HTTP API 注册 OpenAI compatible provider (OpenAI-compat mode).

    支持两种认证方式 (优先级从高到低):
      1. OpenAPI Key (Bearer token) — 在 Dashboard「设置→OpenAPI」创建
      2. username + password — 在 plugin.config 配 webui_password
    """
    try:
        openapi_key = (plugin.config.get("openapi_key") or "").strip()
        username, password, dash_port = _read_webui_credentials(plugin)

        use_bearer = bool(openapi_key)
        logger.warning(
            "[vision_text_bridge] provider 注册尝试: bearer=%s, username=%r, password_len=%d, "
            "dash_port=%d, openapi_key_prefix=%s",
            use_bearer, username, len(password), dash_port,
            openapi_key[:8] + "***" if openapi_key else "(empty)",
        )
        if not use_bearer and not password:
            logger.warning(
                "[vision_text_bridge] OpenAPI Key 和 webui password 均未配置 — "
                "无法通过 webui API 注册 provider. "
                "请在 webui「设置 → OpenAPI」创建 Key 填入 openapi_key, "
                "或在「系统配置 → dashboard」配置 password."
            )
            return False

        actual_port = getattr(plugin, "_openai_compat_port", None) or DEFAULT_OPENAI_COMPAT_PORT
        api_base = f"http://127.0.0.1:{actual_port}/v1/chat/completions"

        api_key = (
            plugin.config.get("api_key", "")
            or plugin.config.get("openai_compat_api_key", "")
        )
        model_name = (
            plugin.config.get("model_name")
            or DEFAULT_MODEL
        )

        config = {
            "id": PROVIDER_ID,
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": [api_key] if api_key else ["placeholder"],
            "api_key": api_key if api_key else "placeholder",
            "api_base": api_base,
            "model": model_name,
            "model_config": {"model": model_name},
        }

        base_url = f"http://localhost:{dash_port}"
        async with _httpx.AsyncClient(timeout=15.0) as client:
            headers = {}
            if use_bearer:
                headers["X-API-Key"] = openapi_key
                logger.info(
                    "[vision_text_bridge] 使用 OpenAPI Key (X-API-Key) 认证注册 provider"
                )
            else:
                # 传统 username/password 登录
                login_resp = await client.post(
                    f"{base_url}/api/auth/login",
                    json={"username": username, "password": password},
                )
                if login_resp.status_code not in (200, 204):
                    logger.warning(
                        "[vision_text_bridge] webui 登录失败 (status=%d, username=%s) — "
                        "请检查 password 配置", login_resp.status_code, username,
                    )
                    return False

            # 2. POST provider (id 重复 → 400/409 with "already exists")
            try:
                create_resp = await client.post(
                    f"{base_url}/api/v1/providers",
                    json=config,
                    headers=headers,
                )
                if create_resp.status_code in (200, 201):
                    logger.info(
                        "[vision_text_bridge] 通过 webui API 注册 provider 成功: id=%s, "
                        "api_base=%s, model=%s", PROVIDER_ID, api_base, model_name,
                    )
                    if log_details:
                        _log_registered_instance(plugin)
                    return True
                logger.warning(
                    "[vision_text_bridge] POST /api/v1/providers 返回 %d: %s",
                    create_resp.status_code, (create_resp.text or "")[:300],
                )
            except Exception as e:
                logger.debug("create exception: %s", e)

            # 3. Fallback: PUT update by-id
            try:
                update_resp = await client.put(
                    f"{base_url}/api/v1/providers/by-id",
                    params={"provider_id": PROVIDER_ID},
                    json=config,
                    headers=headers,
                )
                if update_resp.status_code in (200, 204):
                    logger.info(
                        "[vision_text_bridge] 通过 webui API 更新 provider 成功: id=%s",
                        PROVIDER_ID,
                    )
                    if log_details:
                        _log_registered_instance(plugin)
                    return True
                logger.warning(
                    "[vision_text_bridge] PUT /api/v1/providers/by-id 返回 %d: %s",
                    update_resp.status_code, (update_resp.text or "")[:300],
                )
            except Exception as e:
                logger.debug("update exception: %s", e)

            logger.warning(
                "[vision_text_bridge] webui API 注册失败 (create+update 都失败) — "
                "请看上面日志"
            )
            return False
    except Exception as e:
        logger.exception("auto_register_provider 异常: %s", e)
        return False


def _log_registered_instance(plugin) -> None:
    """注册后只读查 pm 输出 5 字段集中 log。"""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            return
        prov_dict = getattr(pm, "providers", {})
        inst = prov_dict.get(PROVIDER_ID) if isinstance(prov_dict, dict) else None
        if inst is None:
            for p in getattr(pm, "provider_insts", []):
                cfg = getattr(p, "provider_config", None)
                if isinstance(cfg, dict) and cfg.get("id") == PROVIDER_ID:
                    inst = p
                    break
        if inst is None:
            logger.info(
                "[vision_text_bridge] provider 已就绪 — 但 pm.providers[id] 仍 None "
                "(framework 还未完成 load, 下次 plugin 重启后可用)"
            )
            return
        api_base = getattr(inst, "api_base", "") or ""
        api_key = getattr(inst, "api_key", "") or ""
        model = (
            getattr(inst, "model_name", None)
            or getattr(inst, "_current_model", None)
            or getattr(inst, "model", "") or ""
        )
        if len(api_key) > 8:
            key_masked = api_key[:4] + "***" + api_key[-4:]
        else:
            key_masked = "***"
        logger.info(
            "[vision_text_bridge] provider 已就绪 — 完整配置:\n"
            "  provider_id        (AstrBot dashboard 显示名) = %s\n"
            "  provider_instance_id (内存唯一 ID)           = 0x%08x\n"
            "  api_base           (POST endpoint URL)        = %s\n"
            "  api_key            (脱敏)                    = %s\n"
            "  model              (模型昵称 id)             = %s",
            PROVIDER_ID, id(inst), api_base, key_masked, model,
        )
    except Exception as e:
        logger.debug("_log_registered_instance 异常: %s", e)


async def remove_provider(plugin) -> bool:
    """通过 webui DELETE 卸载 provider。支持 OpenAPI Key (X-API-Key) 或 username/password。"""
    try:
        openapi_key = (plugin.config.get("openapi_key") or "").strip()
        username, password, dash_port = _read_webui_credentials(plugin)
        if not openapi_key and not password:
            return False
        base_url = f"http://localhost:{dash_port}"
        headers = {}
        async with _httpx.AsyncClient(timeout=10.0) as client:
            if openapi_key:
                headers["X-API-Key"] = openapi_key
            else:
                await client.post(
                    f"{base_url}/api/auth/login",
                    json={"username": username, "password": password},
                )
            r = await client.delete(
                f"{base_url}/api/v1/providers/by-id",
                params={"provider_id": PROVIDER_ID},
                headers=headers,
            )
            return r.status_code in (200, 204)
    except Exception:
        return False
