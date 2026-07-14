"""通过 AstrBot webui HTTP API 注册 provider — 不修改、不注入。

设计 (用户 10:08/10:28 要求):
  - 不改 cmd_config.json
  - 不注入 framework 内部状态 (pm.providers / pm.provider_insts)
  - 通过 webui 接口 (POST /api/v1/providers) 注册
  - endpoint 用独立 server (127.0.0.1:2023) — bypass framework legacy_router JWT
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
from typing import Optional

import httpx as _httpx

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


def _emit(level: str, msg: str) -> None:
    """emit 一行 — 同时走 logger 和 print，绕开任何 logger 过滤/路由问题.

    print 直接到 stdout，docker/终端/tmux 必能看到。
    logger 走 AstrBot 的 loguru 桥接，WebUI 控制台也能看到。
    """
    try:
        getattr(logger, level)(msg)
    except Exception:
        pass
    try:
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] [vision_text_bridge] [{level.upper()}] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


from constants import (
    PROVIDER_ID,
    DEFAULT_OPENAI_COMPAT_PORT,
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_MODEL,
)


_emit("info", "provider_registration module loaded")


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
        _emit("debug", f"_read_webui_credentials 异常: {e}")
    return username, password, port


async def auto_register_provider(plugin, log_details: bool = False) -> bool:
    """通过 webui HTTP API 注册 OpenAI compatible provider (OpenAI-compat mode).

    支持两种认证方式 (优先级从高到低):
      1. OpenAPI Key (Bearer token) — 在 Dashboard「设置→OpenAPI」创建
      2. username + password — 在 plugin.config 配 webui_password
    """
    _emit("info", "========== provider 注册开始 ==========")
    try:
        # 入口探针 — 即使后面的代码异常，也至少能看到这条
        _emit("info", f"[1/6] 进入 auto_register_provider, plugin={type(plugin).__name__}")
        _emit("info", f"[2/6] plugin.config type={type(plugin.config).__name__}")

        if plugin is None or not hasattr(plugin, "config"):
            _emit("error", f"plugin 或 plugin.config 缺失: plugin={plugin}")
            return False

        try:
            openapi_key = (plugin.config.get("openapi_key") or "").strip()
            username, password, dash_port = _read_webui_credentials(plugin)
        except Exception as e:
            _emit("error", f"[3/6] 读 config 异常: {e!r}")
            raise

        use_bearer = bool(openapi_key)
        # 入口 INFO log — 一定能看见
        _emit(
            "info",
            f"[3/6] provider 注册尝试: bearer={use_bearer}, "
            f"username={username!r}, password_len={len(password)}, "
            f"dash_port={dash_port}, "
            f"openapi_key_prefix={openapi_key[:8] + '***' if openapi_key else '(empty)'}",
        )
        if not use_bearer and not password:
            _emit(
                "warning",
                "OpenAPI Key 和 webui password 均未配置 — "
                "无法通过 webui API 注册 provider. "
                "请在 webui「设置 → OpenAPI」创建 Key 填入 openapi_key, "
                "或在「系统配置 → dashboard」配置 password.",
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

        # AstrBot v4.x ProviderConfigRequest schema:
        #   to_dashboard_config() uses self.config (if present) or model_dump()
        #   with explicit excludes. provider_config & config fields are both excluded
        #   from model_dump(), so we put all fields flat at root level.
        config = {
            "provider_id": PROVIDER_ID,
            "provider_source_id": "openai_source",
            "id": PROVIDER_ID,
            "enable": True,
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "key": [api_key] if api_key else ["placeholder"],
            "api_key": api_key if api_key else "placeholder",
            "api_base": api_base,
            "model": model_name,
        }

        base_url = f"http://127.0.0.1:{dash_port}"
        _emit(
            "info",
            f"[4/6] 准备调用 webui API: base_url={base_url}, "
            f"provider_id={PROVIDER_ID}, api_base={api_base}, model={model_name}",
        )

        async with _httpx.AsyncClient(timeout=15.0) as client:
            headers = {}
            if use_bearer:
                headers["X-API-Key"] = openapi_key
                _emit(
                    "info",
                    "[5/6] 认证方式: OpenAPI Key (X-API-Key header)",
                )
            else:
                _emit(
                    "info",
                    f"[5/6] 认证方式: username/password (user={username})",
                )
                # 传统 username/password 登录
                _emit("info", f"  → POST {base_url}/api/auth/login")
                login_resp = await client.post(
                    f"{base_url}/api/auth/login",
                    json={"username": username, "password": password},
                )
                _emit(
                    "info",
                    f"  → 登录响应 status={login_resp.status_code}",
                )
                if login_resp.status_code not in (200, 204):
                    _emit(
                        "warning",
                        f"webui 登录失败 (status={login_resp.status_code}, username={username}) — "
                        f"请检查 password 配置. resp={(login_resp.text or '')[:300]}",
                    )
                    return False

            # 2. POST provider (id 重复 → 400/409 with "already exists")
            try:
                _emit(
                    "info",
                    f"[6/6] POST {base_url}/api/v1/providers",
                )
                _emit("info", f"  → payload={config}")
                create_resp = await client.post(
                    f"{base_url}/api/v1/providers",
                    json=config,
                    headers=headers,
                )
                _emit(
                    "info",
                    f"  → POST 响应 status={create_resp.status_code}",
                )
                if create_resp.status_code in (200, 201):
                    _emit(
                        "info",
                        f"✓ 通过 webui API 注册 provider 成功: id={PROVIDER_ID}",
                    )
                    if log_details:
                        _log_registered_instance(plugin)
                    return True
                # 完整 resp body 给 INFO 级别, 方便诊断
                _emit(
                    "warning",
                    f"POST /api/v1/providers 返回 {create_resp.status_code} — body={(create_resp.text or '')[:500]}",
                )
                if create_resp.status_code == 403:
                    _emit(
                        "warning",
                        "提示: 403 通常表示 OpenAPI Key 缺少 'provider' scope. "
                        "请到 Dashboard「设置 → OpenAPI」编辑 Key, 勾选 'provider' scope.",
                    )
                elif create_resp.status_code == 401:
                    _emit(
                        "warning",
                        "提示: 401 表示 OpenAPI Key 无效. "
                        "请检查 openapi_key 是否正确 (格式 abk_xxx).",
                    )
                elif create_resp.status_code == 422:
                    _emit(
                        "warning",
                        "提示: 422 表示 payload 校验失败. "
                        "请看上面 resp body 的 detail 字段.",
                    )
                elif create_resp.status_code == 400 and "already exists" in (create_resp.text or "").lower():
                    _emit(
                        "warning",
                        "提示: 'already exists' — 该 provider_id 已注册, 但本次返回 400. "
                        "请尝试重启 AstrBot 让 framework 加载现有 provider.",
                    )
            except Exception as e:
                _emit("warning", f"POST /api/v1/providers 异常: {e!r}")

            # 3. Fallback: PUT update by-id
            try:
                _emit(
                    "info",
                    f"[6/6-fallback] PUT {base_url}/api/v1/providers/by-id?provider_id={PROVIDER_ID}",
                )
                update_resp = await client.put(
                    f"{base_url}/api/v1/providers/by-id",
                    params={"provider_id": PROVIDER_ID},
                    json=config,
                    headers=headers,
                )
                _emit(
                    "info",
                    f"  → PUT 响应 status={update_resp.status_code}",
                )
                if update_resp.status_code in (200, 204):
                    _emit(
                        "info",
                        f"✓ 通过 webui API 更新 provider 成功: id={PROVIDER_ID}",
                    )
                    if log_details:
                        _log_registered_instance(plugin)
                    return True
                _emit(
                    "warning",
                    f"PUT /api/v1/providers/by-id 返回 {update_resp.status_code} — body={(update_resp.text or '')[:500]}",
                )
            except Exception as e:
                _emit("warning", f"PUT /api/v1/providers/by-id 异常: {e!r}")

            _emit(
                "warning",
                "webui API 注册失败 (POST + PUT 都失败) — "
                "请看上面 HTTP 响应 body 排查.",
            )
            return False
    except Exception as e:
        _emit("error", f"auto_register_provider 顶层异常: {e!r}")
        try:
            logger.exception("auto_register_provider 异常: %s", e)
        except Exception:
            pass
        return False
    finally:
        _emit("info", "========== provider 注册结束 ==========")


def _log_registered_instance(plugin) -> None:
    """注册后只读查 pm 输出 5 字段集中 log。"""
    try:
        pm = getattr(plugin.context, "provider_manager", None)
        if pm is None:
            _emit("warning", "plugin.context.provider_manager 为 None, 无法验证注册")
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
            _emit(
                "info",
                "provider 已就绪 — 但 pm.providers[id] 仍 None "
                "(framework 还未完成 load, 下次 plugin 重启后可用)",
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
        _emit(
            "info",
            f"provider 已就绪 — 完整配置:\n"
            f"  provider_id        (AstrBot dashboard 显示名) = {PROVIDER_ID}\n"
            f"  provider_instance_id (内存唯一 ID)           = 0x{id(inst):08x}\n"
            f"  api_base           (POST endpoint URL)        = {api_base}\n"
            f"  api_key            (脱敏)                    = {key_masked}\n"
            f"  model              (模型昵称 id)             = {model}",
        )
    except Exception as e:
        _emit("debug", f"_log_registered_instance 异常: {e!r}")


async def remove_provider(plugin) -> bool:
    """通过 webui DELETE 卸载 provider。支持 OpenAPI Key (X-API-Key) 或 username/password。"""
    try:
        openapi_key = (plugin.config.get("openapi_key") or "").strip()
        username, password, dash_port = _read_webui_credentials(plugin)
        if not openapi_key and not password:
            return False
        base_url = f"http://127.0.0.1:{dash_port}"
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
            _emit("info", f"DELETE /providers/by-id status={r.status_code}")
            return r.status_code in (200, 204)
    except Exception as e:
        _emit("warning", f"remove_provider 异常: {e!r}")
        return False