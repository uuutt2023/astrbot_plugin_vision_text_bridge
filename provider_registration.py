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


_quiet = False


def _emit(level: str, msg: str) -> None:
    """emit 一行 — 同时走 logger 和 print，绕开任何 logger 过滤/路由问题.

    print 直接到 stdout，docker/终端/tmux 必能看到。
    logger 走 AstrBot 的 loguru 桥接，WebUI 控制台也能看到。
    _quiet 开启时只保留 warning/error，避免注册重试刷屏。
    """
    if _quiet and level not in ("warning", "error"):
        return
    try:
        getattr(logger, level)(msg)
    except Exception:
        pass
    try:
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(
            f"[{ts}] [vision_text_bridge] [{level.upper()}] {msg}",
            file=sys.stderr,
            flush=True,
        )
    except Exception:
        pass


from constants import (  # noqa: E402
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


def _read_dashboard_port(plugin) -> int:
    """读 dashboard 端口，默认 6185，可被 dashboard_port 覆盖。"""
    port = DEFAULT_DASHBOARD_PORT
    try:
        pc = plugin.config if plugin and hasattr(plugin, "config") else {}
        if isinstance(pc, dict):
            cport = pc.get("dashboard_port")
            if cport:
                port = int(cport)
    except (TypeError, ValueError):
        pass
    except Exception as e:
        _emit("debug", f"_read_dashboard_port 异常: {e}")
    return port


async def auto_register_provider(
    plugin, log_details: bool = False, quiet: bool = False
) -> bool:
    """通过 webui HTTP API 注册 OpenAI compatible provider (OpenAI-compat mode).

    认证方式: OpenAPI Key (X-API-Key header) — 在 Dashboard「设置→OpenAPI」创建。

    quiet=True 时抑制 [1/5] 等步骤日志 (重试用)，只保留 warning/error。
    """
    global _quiet
    _quiet = quiet
    _emit("info", "========== provider 注册开始 ==========")
    try:
        creds = _prepare_credentials(plugin)
        if creds is None:
            return False
        openapi_key, dash_port = creds

        config = _build_provider_payload(plugin)
        base_url = f"http://127.0.0.1:{dash_port}"

        async with _httpx.AsyncClient(timeout=15.0) as client:
            headers = {"X-API-Key": openapi_key}

            if await _post_create_provider(
                client, base_url, headers, config, plugin, log_details
            ):
                return True

            if await _put_update_provider(
                client, base_url, headers, config, plugin, log_details
            ):
                return True

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
        _quiet = False


def _prepare_credentials(plugin) -> Optional[tuple[str, int]]:
    """读取并校验注册凭证 (openapi_key, dash_port)。失败返 None（已记 warning）。"""
    _emit("info", f"[1/5] 进入 auto_register_provider, plugin={type(plugin).__name__}")

    if plugin is None or not hasattr(plugin, "config"):
        _emit("error", f"plugin 或 plugin.config 缺失: plugin={plugin}")
        return None

    _emit("info", f"[2/5] plugin.config type={type(plugin.config).__name__}")

    try:
        openapi_key = (plugin.config.get("openapi_key") or "").strip()
        dash_port = _read_dashboard_port(plugin)
    except Exception as e:
        _emit("error", f"[3/5] 读 config 异常: {e!r}")
        raise

    _emit(
        "info",
        f"[3/5] 注册凭证: dash_port={dash_port}, "
        f"openapi_key_prefix={openapi_key[:8] + '***' if openapi_key else '(empty)'}",
    )
    if not openapi_key:
        _emit(
            "warning",
            "OpenAPI Key 未配置 — 无法通过 webui API 注册 provider. "
            "请在 webui「设置 → OpenAPI」创建 Key 填入 openapi_key.",
        )
        return None
    return (openapi_key, dash_port)


def _build_provider_payload(plugin) -> dict:
    """构造 webui POST /api/v1/providers 的请求体。"""
    actual_port = (
        getattr(plugin, "_openai_compat_port", None) or DEFAULT_OPENAI_COMPAT_PORT
    )
    api_base = f"http://127.0.0.1:{actual_port}/v1"
    api_key = plugin.config.get("api_key", "") or plugin.config.get(
        "openai_compat_api_key", ""
    )
    model_name = plugin.config.get("model_name") or DEFAULT_MODEL

    _emit(
        "info",
        f"[4/5] 准备调用 webui API: api_base={api_base}, model={model_name}",
    )

    # AstrBot v4.x ProviderConfigRequest schema:
    #   to_dashboard_config() uses self.config (if present) or model_dump()
    #   with explicit excludes. provider_config & config fields are both excluded
    #   from model_dump(), so we put all fields flat at root level.
    return {
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
        # 兼容其他插件 (如 astrbot_plugin_private_companion) 检查 provider_config.modalities
        # private_companion 在 _provider_supports_image 里:
        #   if modalities == []: return True  (AstrBot 旧空 list 当作"全部")
        #   return isinstance(modalities, list) and "image" in modalities
        # 没设 → 返 None → isinstance(..., list) False → 当作不支持图片 → attempts=0
        "modalities": ["text", "image"],
    }


async def _post_create_provider(
    client: "_httpx.AsyncClient",
    base_url: str,
    headers: dict,
    config: dict,
    plugin,
    log_details: bool,
) -> bool:
    """POST 注册 provider。成功返 True。"""
    _emit("info", f"[5/5] POST {base_url}/api/v1/providers")
    _emit("info", f"  → payload={config}")
    try:
        resp = await client.post(
            f"{base_url}/api/v1/providers",
            json=config,
            headers=headers,
        )
    except Exception as e:
        _emit("warning", f"POST /api/v1/providers 异常: {e!r}")
        return False

    _emit("info", f"  → POST 响应 status={resp.status_code}")
    if resp.status_code in (200, 201):
        _emit("info", f"✓ 通过 webui API 注册 provider 成功: id={PROVIDER_ID}")
        if log_details:
            _log_registered_instance(plugin)
        return True

    _emit(
        "warning",
        f"POST /api/v1/providers 返回 {resp.status_code} — body={(resp.text or '')[:500]}",
    )
    for hint in _hint_for_post_status(resp.status_code, resp.text or ""):
        _emit("warning", hint)
    return False


async def _put_update_provider(
    client: "_httpx.AsyncClient",
    base_url: str,
    headers: dict,
    config: dict,
    plugin,
    log_details: bool,
) -> bool:
    """PUT 兜底更新 provider。成功返 True。"""
    _emit(
        "info",
        f"[5/5-fallback] PUT {base_url}/api/v1/providers/by-id?provider_id={PROVIDER_ID}",
    )
    try:
        resp = await client.put(
            f"{base_url}/api/v1/providers/by-id",
            params={"provider_id": PROVIDER_ID},
            json=config,
            headers=headers,
        )
    except Exception as e:
        _emit("warning", f"PUT /api/v1/providers/by-id 异常: {e!r}")
        return False

    _emit("info", f"  → PUT 响应 status={resp.status_code}")
    if resp.status_code in (200, 204):
        _emit("info", f"✓ 通过 webui API 更新 provider 成功: id={PROVIDER_ID}")
        if log_details:
            _log_registered_instance(plugin)
        return True

    _emit(
        "warning",
        f"PUT /api/v1/providers/by-id 返回 {resp.status_code} — body={(resp.text or '')[:500]}",
    )
    return False


def _hint_for_post_status(status: int, body: str) -> list[str]:
    """根据 POST 失败状态返回 1+ 条提示。"""
    if status == 403:
        return [
            "提示: 403 通常表示 OpenAPI Key 缺少 'provider' scope. "
            "请到 Dashboard「设置 → OpenAPI」编辑 Key, 勾选 'provider' scope.",
        ]
    if status == 401:
        return [
            "提示: 401 表示 OpenAPI Key 无效. "
            "请检查 openapi_key 是否正确 (格式 abk_xxx).",
        ]
    if status == 422:
        return [
            "提示: 422 表示 payload 校验失败. 请看上面 resp body 的 detail 字段.",
        ]
    if status == 400 and "already exists" in body.lower():
        return [
            "提示: 'already exists' — 该 provider_id 已注册, 但本次返回 400. "
            "请尝试重启 AstrBot 让 framework 加载现有 provider.",
        ]
    return []


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
            or getattr(inst, "model", "")
            or ""
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
    """通过 webui DELETE 卸载 provider。使用 OpenAPI Key (X-API-Key) 认证。"""
    try:
        openapi_key = (plugin.config.get("openapi_key") or "").strip()
        if not openapi_key:
            return False
        dash_port = _read_dashboard_port(plugin)
        base_url = f"http://127.0.0.1:{dash_port}"
        headers = {"X-API-Key": openapi_key}
        async with _httpx.AsyncClient(timeout=10.0) as client:
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
