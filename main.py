"""main.py - 插件入口、业务核心与 AstrBot 钩子实现。"""

from __future__ import annotations

# : import 报错清缓存

import asyncio
import base64
import importlib.util
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# 插件目录加 sys.path (AstrBot 加载器不自动加, 8 个同级模块都需要)
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

try:
    from astrbot.api import AstrBotConfig
    from astrbot.api import logger as _astr_logger
except Exception:  # logger 可能不存在 — 兜底
    _astr_logger = None
import logging
logger = _astr_logger if _astr_logger is not None else logging.getLogger(__name__)
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
try:
    # : ContentPart 注入
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # 测试沙箱/未来重命名兼容

# : 同级模块直接 import

from image_utils import (
    is_image_url_part as _is_image_url_part,
    extract_urls_from_parts as _extract_urls_from_parts,
    extract_urls_from_context_list as _extract_urls_from_context_list,
    strip_image_urls as _strip_image_urls,
    to_text_part as _to_text_part,
    sniff_image_meta as _sniff_image_meta,
    is_cacheable_url as _is_cacheable_url,
    read_image_bytes as _read_image_bytes,
    _read_file_bytes_sync,
    collect_image_urls_from_components as _collect_image_urls_from_components,
)
from tool_filter import filter_disabled_tools as _filter_disabled_tools
import chat_archive_integration
import provider_registration  # webui HTTP API 注册 provider (顶部 import)
from mmx_runner import (
    MmxResult, build_vision_command as _build_vision_command,
    run_mmx as _run_mmx_fn, install_mmx_cli as _install_mmx_cli_fn,
    install_mmx_local as _install_mmx_local_fn,
    find_local_mmx as _find_local_mmx_fn,
    diagnose_mmx_error as _diagnose_mmx_error_fn,
    truncate as _truncate_text, strip_mmx_content as _strip_mmx_content_fn,
    preview as _preview_text, redact_text as _redact_text, redact_args as _redact_args_fn,
)
import web_api
try:
    import main_server  # 独立 OpenAI 兼容 server (bypass framework JWT)
except ImportError:
    main_server = None


# ---------------------------------------------------------------------------
# 动态加载同级 caption_cache.py（AstrBot 不会把插件目录加到 sys.path）
# ---------------------------------------------------------------------------
def _load_sibling_module(name: str):
    here = Path(__file__).resolve().parent
    target = here / f"{name}.py"
    if not target.exists():
        raise ImportError(f"插件目录中找不到依赖文件: {target}")
    spec = importlib.util.spec_from_file_location(
        f"astrbot_plugin_vision_text_bridge.{name}", target
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sibling_cache = _load_sibling_module("caption_cache")
CaptionCache = _sibling_cache.CaptionCache
CaptionEntry = _sibling_cache.CaptionEntry

# : 共享常量跨模块来源
from constants import (
    PLUGIN_NAME, DEFAULT_DASHBOARD_PORT, PLUGIN_ROUTE_PREFIX,
    OPENAI_COMPAT_PATH, IMAGE_CAPTION_PATH, PROVIDER_ID,
    DEFAULT_MODEL, PLACEHOLDER_API_KEY,
)

# AstrBot on_llm_request priority 越大越先跑；100 高于多数常见插件。
# priority 在 import 时锁定，调配置后需重启 AstrBot。
DEFAULT_PRIORITY = 100

# 预编译 bot 头像 URL 过滤 regex
_BOT_AVATAR_PAT = re.compile(r"q\.qlogo\.cn/headimg_dl\?", re.IGNORECASE)

# : 正则见 mmx_runner


class _MemoryCache:
    """: 内存热缓存 — TTL + LRU size 上限。"""
    __slots__ = ("_m", "_max_size", "_ttl")

    def __init__(self, ttl_seconds: int, max_size: int):
        self._m: dict[str, tuple[str, float]] = {}
        self._max_size = max(1, int(max_size))
        self._ttl = max(0, int(ttl_seconds))

    def get(self, key: str) -> str | None:
        v = self._m.get(key)
        if v is None:
            return None
        text, expire_at = v
        if self._ttl > 0 and time.time() >= expire_at:
            self._m.pop(key, None)  # 过期懒删除
            return None
        return text

    def put(self, key: str, value: str) -> None:
        if self._max_size <= 0:
            return
        if key in self._m:
            self._m.pop(key, None)
        expire = time.time() + self._ttl if self._ttl > 0 else float("inf")
        self._m[key] = (value, expire)
        while len(self._m) > self._max_size:
            oldest = next(iter(self._m))
            self._m.pop(oldest, None)

    def pop(self, key: str) -> str | None:
        v = self._m.pop(key, None)
        return v[0] if v else None

    def clear(self) -> None:
        self._m.clear()

    def __len__(self) -> int:
        return len(self._m)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    # 字典语法糖
    def __setitem__(self, key: str, value: str) -> None:
        self.put(key, value)

    def __getitem__(self, key: str) -> str:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号。"""
    try:
        import yaml  # AstrBot 依赖 PyYAML
        meta_path = Path(__file__).resolve().parent / "metadata.yaml"
        with open(meta_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return str(data.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


# : 配置读取 helper
def _cfg_int(config, key: str, default: int) -> int:
    v = config.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _cfg_str(config, key: str, default: str) -> str:
    v = config.get(key, default)
    if v is None:
        return default
    return str(v)


PLUGIN_VERSION = _read_plugin_version()


def _flatten_group_config(config: dict) -> dict:
    """: 展平嵌套 group config，兼容 3 种 schema 格式。"""
    if not isinstance(config, dict):
        return config
    flat = dict(config)  # 浅拷贝, 保留 group 引用
    SCHEMA_META_KEYS = {"description", "type", "hint", "default", "obvious_hint", "items"}
    for _key, value in list(config.items()):
        if not isinstance(value, dict):
            continue
        if "items" in value and isinstance(value["items"], dict):
            # 格式 A: schema definition with items wrapper
            for ik, iv in value["items"].items():
                flat[ik] = iv
        else:
            # 格式 B: group 容器只有字段, 无 items 包装
            is_schema_def = any(mk in value for mk in ("description", "type", "hint", "default", "obvious_hint"))
            if not is_schema_def:
                # 普通 user-data group, 展平
                for ik, iv in value.items():
                    if ik not in flat:  # 不覆盖已有顶层 key
                        flat[ik] = iv
    return flat


# ===========================================================================
# 数据结构 (MmxResult 移到 mmx_runner.py — 在 import 区域透传过来)
# ===========================================================================


# ===========================================================================
# 插件主体
# ===========================================================================
@register(
    "astrbot_plugin_vision_text_bridge",
    "uuutt",
    "把图片转成 MiniMax CLI 图像理解后的文本，再喂给对话 LLM",
    PLUGIN_VERSION,
)
class VisionTextBridgePlugin(Star):
    """Vision -> Text 桥接，拦截图片消息并调用 mmx 生成文字描述。"""

    # mmx 同一错误只诊断一次，避免刷屏
    _DIAGNOSED: set[str] = set()

    # ------------------------------------------------------------------ init

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = _flatten_group_config(config) if isinstance(config, dict) else config
        self.mmx_path = (self.config.get("mmx_path") or "").strip()
        if not self.mmx_path:
            local = _find_local_mmx_fn(str(_PLUGIN_DIR))
            if local:
                self.mmx_path = local
                logger.info("[vision_text_bridge] 找到 plugin 本地装 mmx: %s", local)
        if not self.mmx_path:
            self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd") or ""
        self.npm_path = shutil.which("npm") or shutil.which("npm.cmd")
        self._caption_cache: CaptionCache | None = None
        # : 内存热缓存 — TTL + LRU size 上限
        self._description_cache: _MemoryCache = _MemoryCache(
            ttl_seconds=_cfg_int(self.config, "memory_cache_ttl_seconds", 300),
            max_size=_cfg_int(self.config, "memory_cache_max_size", 500),
        )
        self._vision_semaphore: asyncio.Semaphore | None = None
        self._configured_priority: int = self._resolve_priority()
        self._last_image_bytes: dict[str, bytes] = {}
        self._priority_locked_warning_emitted = False
        self._pending_urls: list[str] | None = None
        self._pending_parts: list[Any] | None = None
        self._pending_contexts: list[Any] | None = None

        if not self.config.get("enabled", True):
            logger.info("[vision_text_bridge] 插件已配置为关闭，不会拦截任何请求")
        logger.info(
            "[vision_text_bridge] 已加载，mmx_path=%s, enabled=%s, priority=%d",
            self.mmx_path or "<未找到>",
            self.config.get("enabled", True),
            self._configured_priority,
        )
        self._warn_if_priority_mismatch()

    def _resolve_priority(self) -> int:
        raw = self.config.get("priority", None)
        if raw is None or raw == "":
            return DEFAULT_PRIORITY
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[vision_text_bridge] priority 配置值非法 (%r)，回退到默认 %d",
                raw, DEFAULT_PRIORITY,
            )
            return DEFAULT_PRIORITY

    def _warn_if_priority_mismatch(self) -> None:
        if self._configured_priority == DEFAULT_PRIORITY:
            return
        if not (-1000 <= self._configured_priority <= 10000):
            logger.warning(
                "[vision_text_bridge] priority=%d 超出建议范围 [-1000, 10000]",
                self._configured_priority,
            )
        if not self._priority_locked_warning_emitted:
            logger.warning(
                "[vision_text_bridge] priority 配置=%d 与注册值=%d 不一致。"
                "AstrBot 的 on_llm_request priority 在 import 时锁定，"
                "需重启 AstrBot 生效。",
                self._configured_priority, DEFAULT_PRIORITY,
            )
            self._priority_locked_warning_emitted = True

    # ------------------------------------------------------------------ 详细日志开关

    def _should_log(self, *flags: str) -> bool:
        """verbose_logging 总开关或任一细粒度开关为 true 即开。"""
        if self.config.get("verbose_logging", False):
            return True
        return any(bool(self.config.get(f"verbose_{f}", False)) for f in flags)

    def _vdebug(self, flag: str, msg: str, *args) -> None:
        """: 细粒度调试日志。"""
        if self._should_log(flag):
            logger.debug(msg, *args)

    def _vinfo(self, flag: str, msg: str, *args) -> None:
        """: 细粒度信息日志。"""
        if self._should_log(flag):
            logger.info(msg, *args)

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        max_concurrent = max(1, _cfg_int(self.config, "max_concurrent_vision", 3))
        self._vision_semaphore = asyncio.Semaphore(max_concurrent)

        # 同步 webui_password 到 framework dashboard
        try:
            cfg_pwd = (
                self.config.get("webui_password")
                or self.config.get("dashboard_password")
                or ""
            )
            cfg_user = (
                self.config.get("webui_username")
                or self.config.get("dashboard_username")
                or ""
            )
            if cfg_pwd:
                ac = getattr(self.context, "astr_context", None)
                if ac is not None:
                    # 写 in-memory + 尝试持久化
                    target_dict = getattr(ac, "config", None)
                    if isinstance(target_dict, dict):
                        dashboard = target_dict.get("dashboard") or {}
                        dashboard["password"] = cfg_pwd
                        if cfg_user:
                            dashboard["username"] = cfg_user
                        target_dict["dashboard"] = dashboard
                    # 尝试 save to disk via AstrBotConfigManager
                    saved = False
                    try:
                        cfgmgr = getattr(ac, "config_manager", None)
                        if cfgmgr is not None:
                            fn = getattr(cfgmgr, "update_profile", None) or getattr(cfgmgr, "save_config", None)
                            if callable(fn):
                                try:
                                    fn("default", ac.config)
                                    saved = True
                                except Exception:
                                    pass
                        if not saved:
                            confs = getattr(cfgmgr, "confs", None) if cfgmgr else None
                            if not confs:
                                confs = getattr(ac, "confs", None) or getattr(ac, "_confs", None)
                            if isinstance(confs, dict):
                                conf = confs.get("default")
                                save_fn = getattr(conf, "save_config", None)
                                if callable(save_fn):
                                    try:
                                        save_fn()
                                        saved = True
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.debug("[vision_text_bridge] 同步 dashboard 密码到 framework 异常: %s", e)
                    logger.info(
                        "[vision_text_bridge] 已设置 webui 密码到 framework (来源: plugin.config.webui_password)"
                        + (" (持久化)" if saved else " (仅本次进程)"),
                    )
        except Exception as e:
            logger.debug("[vision_text_bridge] webui_password 处理异常: %s", e)

        # SQLite 缓存初始化
        try:
            data_dir = self._get_plugin_data_dir()
            db_path = data_dir / "caption_cache.sqlite3"
            self._caption_cache = CaptionCache(db_path)
            logger.info(
                "[vision_text_bridge] 描述缓存已初始化: %s (条目=%d)",
                db_path, self._caption_cache.count(),
            )
        except Exception as exc:
            logger.exception("[vision_text_bridge] 初始化描述缓存失败，降级为内存缓存: %s", exc)
            self._caption_cache = None

        # 启动 SQLite 过期清理后台 task
        if self._caption_cache is not None:
            try:
                ttl_days = _cfg_int(self.config, "sqlite_cache_ttl_days", 7)
                interval_h = _cfg_int(self.config, "sqlite_clean_interval_hours", 1)
                if ttl_days > 0:
                    deleted = self._caption_cache.clean_expired(ttl_days)
                    self._last_clean_at = time.time()
                    if deleted > 0:
                        logger.info("[vision_text_bridge] 启动时清理过期缓存: 删除 %d 条 (TTL=%d天)", deleted, ttl_days)
                if interval_h > 0 and ttl_days > 0:
                    self._clean_task = asyncio.create_task(self._clean_loop(ttl_days, interval_h))
                    logger.info("[vision_text_bridge] 已启动过期清理后台任务: TTL=%d天, 间隔=%d小时", ttl_days, interval_h)
            except Exception as exc:
                logger.exception("[vision_text_bridge] 启动过期清理 task 失败: %s", exc)

        # 2. web API
        try:
            self._register_web_apis()
        except Exception as exc:
            logger.exception("[vision_text_bridge] 注册 web API 失败: %s", exc)

        # 启动独立 OpenAI 兼容 server (bypass framework JWT)
        if main_server is None:
            logger.warning("[vision_text_bridge] main_server 模块未 import, 跳过独立 server 启动")
        else:
            self._openai_compat_port: int | None = None
            try:
                actual_port = await main_server.start_solo_server(self, port=2023)
                if actual_port is None:
                    logger.warning("[vision_text_bridge] main_server.start_solo_server 失败")
                else:
                    self._openai_compat_port = actual_port
                    logger.info(
                        "[vision_text_bridge] 独立 OpenAI 兼容 server 启动: 127.0.0.1:%d",
                        actual_port,
                    )
            except Exception as exc:
                logger.exception("[vision_text_bridge] 启动独立 OpenAI endpoint server 失败: %s", exc)

        # 3. mmx 安装 + 预登录
        if not self.mmx_path and self.config.get("auto_install_cli", True):
            # 优先持久化装到 plugin 本地 (不需 root, 不改 system PATH)
            local_target = str(_PLUGIN_DIR / ".mmx")
            logger.info("[vision_text_bridge] 未找到 mmx CLI, 尝试装到 plugin 本地: %s", local_target)
            install_ok = await _install_mmx_local_fn(self.npm_path, local_target)
            if install_ok:
                self.mmx_path = _find_local_mmx_fn(str(_PLUGIN_DIR)) or ""
                if self.mmx_path:
                    logger.info("[vision_text_bridge] mmx-cli 本地装成功: %s", self.mmx_path)
                else:
                    logger.warning("[vision_text_bridge] 本地装成功但 .bin/mmx 仍找不到, 请检查 node_modules")
            if not self.mmx_path:
                # 本地装失败, 退到全局装
                logger.info("[vision_text_bridge] 本地装失败, 尝试 npm install -g ...")
                install_ok = await self._install_mmx_cli()
                if install_ok:
                    self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd") or ""
                    if self.mmx_path:
                        logger.info("[vision_text_bridge] mmx-cli 全局装成功: %s", self.mmx_path)
                else:
                    logger.warning(
                        "[vision_text_bridge] mmx-cli 装失败。请手动执行:\n"
                        "  1. 装 Node.js/npm (https://nodejs.org/)\n"
                        "  2.  npm install -g mmx-cli\n"
                        "  3. 重启 AstrBot 或在插件配置中指定 mmx_path 绝对路径"
                    )
        if not self.mmx_path:
            logger.warning(
                "[vision_text_bridge] 未找到 mmx CLI, 插件不处理图片转换。"
                "请手动 npm install -g mmx-cli 或在插件配置中指定 mmx_path。"
            )
            return
        if self.config.get("auto_login", True):
            api_key = (self.config.get("minimax_api_key") or "").strip()
            if not api_key:
                logger.info("[vision_text_bridge] 未配置 minimax_api_key，跳过自动登录")
            else:
                await self._login_mmx(api_key)

        # 伪装 provider modality
        self._mark_providers_support_image()

        # 联动检测
        self._check_compatibility()
        # smart_imagechat_hub 兼容
        self._detect_smart_imagechat_hub()
        # 自动注册 OpenAI compatible provider
        await self._auto_register_sih_provider()

    def _get_plugin_data_dir(self) -> Path:
        try:
            from astrbot.api.star import StarTools
            p = Path(StarTools.get_data_dir())
        except Exception:
            p = Path(__file__).resolve().parent / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _strip_image_fields_from_req(self, req) -> None:
        """从 req 清除所有 image_url 字段。"""
        if req.extra_user_content_parts:
            req.extra_user_content_parts[:] = [p for p in req.extra_user_content_parts
                                               if not _is_image_url_part(p)]
        for c in (req.contexts or []):
            if not isinstance(c, dict):
                continue
            content = c.get("content")
            if not isinstance(content, list):
                continue
            content[:] = [x for x in content
                         if not (isinstance(x, dict) and x.get("type") == "image_url")]

    # ------------------------------------------------------------------ provider 伪装

    def _detect_smart_imagechat_hub(self) -> None:
        """: 启动期检测 smart_imagechat_hub 是否安装。"""
        try:
            installed = provider_registration.is_smart_imagechat_hub_installed()
        except Exception as e:
            logger.debug("[vision_text_bridge] 检测外部图片理解插件失败: %s", e)
            return
        if installed:
            compat_enabled = bool(self.config.get("enable_openai_compat_endpoint")
                          or self.config.get("enable_smart_imagechat_hub_compat", True))
            if compat_enabled:
                logger.info(
                    "[vision_text_bridge] 检测到外部图片理解插件已安装, "
                    "兼容 endpoint 已启用 (POST /v1/chat/completions) — "
                    "可在其配置中指 provider id 接管 image caption"
                )
            else:
                logger.info(
                    "[vision_text_bridge] 检测到外部图片理解插件已安装, "
                    "但 enable_openai_compat_endpoint=False (合并老配置 key), 兼容 endpoint 未启用"
                )



    async def _auto_register_sih_provider(self) -> None:
        """: 启动期自动注册 OpenAI compatible provider。"""
        if not (self.config.get("auto_register")
                or self.config.get("auto_register_openai_compat_provider")
                or self.config.get("smart_imagechat_hub_auto_register_provider", True)):
            logger.info(
                "[vision_text_bridge] auto_register=False, 跳过自动注册 OpenAI compatible provider"
            )
            return
        logger.info("[vision_text_bridge] 调度 smart_imagechat_hub OpenAI compatible provider 注册")
        # : 提前把 openapi_key / webui_password 状态印出，方便诊断
        _openapi_key_set = bool((self.config.get("openapi_key") or "").strip())
        _webui_pw_set = bool((self.config.get("webui_password") or "").strip())
        logger.info(
            "[vision_text_bridge] 注册前置状态: openapi_key=%s, webui_password=%s",
            "已设置" if _openapi_key_set else "(空)",
            "已设置" if _webui_pw_set else "(空)",
        )
        try:
            ok = await provider_registration.auto_register_provider(self)
        except Exception as e:
            logger.exception("[vision_text_bridge] _auto_register_sih_provider 异常: %s", e)
            return
        if ok:
            logger.info("[vision_text_bridge] webui API 注册成功")
        else:
            logger.warning("[vision_text_bridge] webui API 注册返回 False — 请检查 openapi_key 或 webui_password 配置")

        # : 集中 log 由注册成功路径处理
        if ok:
            logger.debug("[vision_text_bridge] _auto_register_sih_provider 成功 — 5 字段集中 log 见 _log_registered_instance")

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """: 检查群白名单 / 用户白名单 / 仅私聊权限。"""
        try:
            # 拿 event 的群/用户信息
            msg = getattr(event, "message_obj", None) or getattr(event, "message", None)
            group_id = str(getattr(msg, "group_id", "") or "") if msg else ""
            user_id = str(getattr(msg, "sender", None) and (msg.sender.user_id or getattr(msg.sender, "user_id", "")) or "")
            if not user_id and hasattr(event, "get_sender_id"):
                user_id = str(event.get_sender_id() or "")
            is_private = not group_id  # 没 group_id = 私聊

            # 1. 仅私聊 + 当前是群 → 跳过
            if not is_private and self.config.get("private_chat_only", False):
                return False, "private_chat_only"

            # 2. 群白名单
            if not is_private and self.config.get("enable_group_whitelist", False):
                whitelist = self.config.get("group_whitelist", []) or []
                whitelist_str = {str(g) for g in whitelist}
                if group_id not in whitelist_str:
                    return False, "group_not_in_whitelist"

            # 3. 用户白名单 (私聊/群聊都生效)
            if self.config.get("enable_user_whitelist", False):
                whitelist = self.config.get("user_whitelist", []) or []
                whitelist_str = {str(u) for u in whitelist}
                if user_id not in whitelist_str:
                    return False, "user_not_in_whitelist"

            return True, ""
        except Exception as e:
            # 提取失败 → 保守放行 (避免因权限检查 bug 漏掉所有拦截)
            if self._should_log("hook_trace"):
                logger.debug("[vision_text_bridge] _check_permission 异常, 默认放行: %s", e)
            return True, ""

    def _mark_providers_support_image(self) -> None:
        """给所有 provider 补 'image' modality 标签，防 AstrBot 切 fallback。"""
        if self.config.get("keep_provider_modality_as_is", False):
            return
        try:
            ctx = self.context.astr_context  # type: ignore[attr-defined]
        except Exception:
            return
        providers = self._collect_all_providers(ctx)
        # 改 modalities
        modified = 0
        for prov in providers:
            cfg = getattr(prov, "provider_config", None)
            if not isinstance(cfg, dict):
                continue
            mods = cfg.get("modalities")
            if mods is None:
                cfg["modalities"] = ["text", "image"]
                modified += 1
            elif isinstance(mods, list) and "image" not in mods:
                cfg["modalities"] = list(mods) + ["image"]
                modified += 1
        if modified:
            logger.info("[vision_text_bridge] 已给 %d 个 provider 补 'image' modality", modified)

    def _collect_all_providers(self, ctx) -> list[Any]:
        """收集所有 provider 对象，兼容多版本 AstrBot API。"""
        providers: list[Any] = []
        manager = getattr(ctx, "provider_manager", None) or getattr(ctx, "providers", None)
        if manager is not None:
            provs = getattr(manager, "providers", None)
            if isinstance(provs, dict):
                providers.extend(provs.values())
            elif isinstance(provs, list):
                providers.extend(provs)
            getter = getattr(manager, "get_all_providers", None)
            if callable(getter):
                try:
                    providers.extend(getter())
                except Exception:
                    pass
            return providers
        # 无 manager: 从 id 反查
        return self._resolve_providers_by_id(ctx)

    def _resolve_providers_by_id(self, ctx) -> list[Any]:
        """从 provider id (当前/fallback) 反查 provider 对象。"""
        providers: list[Any] = []
        seen: set[str] = set()
        for attr in ("_using_provider_id", "default_provider_id"):
            pid = getattr(ctx, attr, None)
            if pid and pid not in seen and hasattr(ctx, "get_provider_by_id"):
                seen.add(pid)
                p = ctx.get_provider_by_id(pid)
                if p is not None:
                    providers.append(p)
        try:
            cfg = ctx.get_config() if hasattr(ctx, "get_config") else None
        except Exception:
            cfg = None
        if cfg and isinstance(cfg.get("provider_settings"), dict):
            for pid in cfg["provider_settings"].get("fallback_chat_models", []) or []:
                if pid not in seen and hasattr(ctx, "get_provider_by_id"):
                    prov = ctx.get_provider_by_id(pid)
                    if prov is not None:
                        providers.append(prov)
        return providers

    # ------------------------------------------------------------------ 兼容性

    def _check_compatibility(self) -> None:
        """检查已装插件并给出兼容性提示。"""
        names = self._get_installed_plugin_names()
        if not names:
            return
        if "astrbot_plugin_chat_archive" in names:
            logger.info(
                "[vision_text_bridge] ℹ️ 检测到 astrbot_plugin_chat_archive。"
                "本插件不与之联动，图片存到本插件自己的 SQLite（含 base64）。"
                "两个插件可同装互不干扰。"
            )
        if "astrbot_plugin_angel_heart" in names:
            cmp = ">" if self._configured_priority > 50 else "<="
            logger.info(
                "[vision_text_bridge] ✓ AngelHeart 联动：本插件 priority=%d %s 50。"
                "如果出现 '[Image Attachment: data:image/...]'，"
                "请禁用 AngelHeart 的 image_caption_provider_id。",
                self._configured_priority, cmp,
            )
        if "astrbot_plugin_uni_nickname" in names:
            if self._configured_priority > 0:
                logger.info(
                    "[vision_text_bridge] ✓ uni_nickname：本插件 priority=%d > 0，会先跑。",
                    self._configured_priority,
                )
            else:
                logger.warning(
                    "[vision_text_bridge] 检测到 uni_nickname 但本插件 priority=%d <= 0，"
                    "uni_nickname 可能会先改 prompt。建议 priority 调到 >= 50。",
                    self._configured_priority,
                )
        for name in (
            "astrbot_plugin_sylanne",
            "astrbot_plugin_conversation_ledger",
            "astrbot_plugin_minimax_image_caption",
        ):
            if name in names:
                logger.info(
                    "[vision_text_bridge] ℹ️ 检测到 %s；本插件 priority=%d 应先于它跑。"
                    "如有冲突可把 priority 调到 500~1000。",
                    name, self._configured_priority,
                )
        if "astrbot_plugin_group_chat_plus" in names:
            logger.info(
                "[vision_text_bridge] ✓ 检测到 chat_plus (priority=-1 会重填 image_urls)。"
                "本插件链末钩子 (priority=-10000) 总清 image_urls 防住干扰。"
            )

    def _get_installed_plugin_names(self) -> set[str]:
        """从 context 拿已装插件名（兼容多版本 API）。"""
        names: set[str] = set()
        manager = getattr(self.context, "plugin_manager", None)
        if manager is not None:
            provs = getattr(manager, "plugins", None) or getattr(manager, "_plugins", None)
            if isinstance(provs, dict):
                names.update(provs.keys())
            elif isinstance(provs, list):
                for p in provs:
                    n = getattr(p, "name", None) or getattr(p, "__name__", None)
                    if isinstance(n, str):
                        names.add(n)
        for meth in ("get_registered_plugin_names", "list_plugin_names", "list_plugins"):
            fn = getattr(self.context, meth, None)
            if not callable(fn):
                continue
            try:
                r = fn()
            except Exception:
                continue
            if isinstance(r, (list, tuple, set)):
                names.update(str(x) for x in r if x)
        return names

    # =========================================================================
    # 页面 API
    # =========================================================================
    def _register_web_apis(self) -> None:
        """: web API 注册委托给 web_api 模块。"""
        import web_api
        web_api.register_all_routes(self.context, self)

    async def terminate(self) -> None:
        if getattr(self, "_clean_task", None) is not None:
            self._clean_task.cancel()
            try:
                await self._clean_task
            except (asyncio.CancelledError, Exception):
                pass
            self._clean_task = None
        self._description_cache.clear()
        self._caption_cache = None
        logger.info("[vision_text_bridge] 插件已卸载，缓存已清理")

    async def _clean_loop(self, ttl_days: int, interval_h: int) -> None:
        """: 定期清理过期 SQLite 缓存的后台 task。"""
        interval_s = interval_h * 3600
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self._caption_cache is None:
                    break
                try:
                    deleted = self._caption_cache.clean_expired(ttl_days)
                    self._last_clean_at = time.time()  # : 供 webui 算下次清理
                    if deleted > 0 and self._should_log("cache_trace"):
                        logger.info("[vision_text_bridge] 后台清理过期缓存: 删除 %d 条 (TTL=%d天)", deleted, ttl_days)
                except Exception as e:
                    logger.warning("[vision_text_bridge] 后台清理失败: %s", e)
        except asyncio.CancelledError:
            pass  # terminate() 取消时正常退出

    # =========================================================================
    # 主钩子: bridge_vision_to_text
    # =========================================================================
    @filter.on_llm_request(priority=DEFAULT_PRIORITY)
    async def bridge_vision_to_text(
        self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs
    ) -> None:
        if not self.config.get("enabled", True):
            return
        if not self.mmx_path:
            logger.warning("[vision_text_bridge] 跳过本次拦截：未配置 mmx CLI")
            return
        # : 权限检查 (群白名单 / 用户白名单 / 仅私聊) — 不通过直接 return
        allowed, skip_reason = self._check_permission(event)
        if not allowed:
            if self._should_log("hook_trace"):
                logger.info("[vision_text_bridge] 跳过拦截（%s）", skip_reason)
            return
        if self._vision_semaphore is None:
            self._vision_semaphore = asyncio.Semaphore(
                max(1, _cfg_int(self.config, "max_concurrent_vision", 3))
            )

        # === 0) 预先过滤待注入的工具集 ===
        # chat_plus 在 priority=-1 才会 merge, 我们提前清理
        try:
            self._filter_tools_in_event(event, req)
        except Exception as e:
            if self._should_log("hook_trace"):
                logger.debug("[vision_text_bridge] 工具过滤跳过：%s", e)

        # === 1) 快照三类图片来源，**先清空** 防 AstrBot 切 fallback provider ===
        saved_urls = list(req.image_urls or [])
        saved_parts = list(req.extra_user_content_parts or []) if req.extra_user_content_parts else []
        saved_contexts = [c for c in (req.contexts or []) if isinstance(c, dict)]

        # : 诊断日志 — 打印 saved_urls 原始来源
        if self._should_log("hook_trace"):
            logger.info(
                "[vision_text_bridge] hook 入口 saved_urls (size=%d): %s",
                len(saved_urls),
                [u[:80] + "..." if isinstance(u, str) and len(u) > 80 else u for u in saved_urls],
            )

        # 从 event.message_obj 补提（防御 chat_plus 抽走图）
        # req.image_urls 空时才递归补，避免同一张图重复 mmx
        if event is not None and not saved_urls:
            try:
                chain = getattr(getattr(event, "message_obj", None), "message", None)
                if chain:
                    added_count = await _collect_image_urls_from_components(chain, saved_urls)
                    type_summary = [str(getattr(c, "type", "?")) for c in chain]
                    logger.info(
                        "[vision_text_bridge] chain 顶层 types=%s, 递归补提了 %d 张图",
                        type_summary, added_count,
                    )
            except Exception as e:
                if self._should_log("hook_trace"):
                    logger.debug("[vision_text_bridge] 补提 event.message_obj 图失败: %s", e)

        # : 过滤 bot 头像 URL (q.qlogo.cn 固定模式)
        if saved_urls:
            filtered = [u for u in saved_urls if not (isinstance(u, str) and _BOT_AVATAR_PAT.search(u))]
            if len(filtered) != len(saved_urls):
                removed = set(saved_urls) - set(filtered)
                logger.info(
                    "[vision_text_bridge] 过滤 bot 头像 %d 张: %s",
                    len(removed), list(removed),
                )
                saved_urls = filtered

        # 清空
        req.image_urls = []
        self._strip_image_fields_from_req(req)

        # 存快照
        self._pending_urls = saved_urls
        self._pending_parts = saved_parts
        self._pending_contexts = saved_contexts

        if self._should_log("hook_trace"):
            logger.info(
                "[vision_text_bridge] on_llm_request: image_urls=%d, parts=%d, contexts=%d, priority=%d",
                len(saved_urls), len(saved_parts), len(saved_contexts), self._configured_priority,
            )

        # === 2) 处理 ===
        try:
            await self._process_request(req)
            self._inject_guidance(req)
        except Exception as e:
            logger.exception("[vision_text_bridge] 处理请求时未捕获异常: %s", e)

    @filter.on_llm_request(priority=-10000)
    async def strip_residual_base64(
        self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs
    ) -> None:
        """链末兜底：总清 image_urls（防 chat_plus 等中间插件重填）+ 可选删 data:base64。"""
        if not self.config.get("enabled", True):
            return
        try:
            n = len(req.image_urls or [])
            if n:
                req.image_urls = []
                if self._should_log("hook_trace"):
                    logger.info("[vision_text_bridge] 链末兜底: 清空 %d 个 image_urls", n)
            if req.extra_user_content_parts:
                req.extra_user_content_parts[:] = [p for p in req.extra_user_content_parts
                                                   if not _is_image_url_part(p)]
            if req.contexts:
                for c in req.contexts:
                    if isinstance(c, dict) and isinstance(c.get("content"), list):
                        c["content"][:] = [x for x in c["content"]
                                           if not (isinstance(x, dict) and x.get("type") == "image_url")]
            # 可选：清 data:base64 残留
            tag = "image_url" if self.config.get("strip_all_image_urls_in_fallback", False) else "data:base64"
            removed = _strip_image_urls(req, only_data_url=tag == "data:base64")
            if removed and self._should_log("hook_trace"):
                logger.info("[vision_text_bridge] 链末兜底: 删 %d 个 %s 残留", removed, tag)
        except Exception as e:
            logger.exception("[vision_text_bridge] 链末兜底异常: %s", e)

        # === : 链末兜底删 func_tool ===
        try:
            mode = _cfg_str(self.config, "tool_filter_mode", "off").lower()
            if mode != "off":
                names_raw = _cfg_str(self.config, "tool_filter_names", "")
                names = [n.strip() for n in names_raw.split(",") if n.strip()]
                if names:
                    ft = getattr(req, "func_tool", None)
                    if ft is not None:
                        n2 = _filter_disabled_tools(ft, mode, names)
                        if n2 and self._should_log("hook_trace"):
                            logger.info("[vision_text_bridge] 链末兜底: 从 req.func_tool 移除 %d 个工具", n2)
        except Exception:
            if self._should_log("hook_trace"):
                logger.debug("[vision_text_bridge] 链末兜底跳过 func_tool", exc_info=True)

    # =========================================================================
    # 内部: 处理请求
    # =========================================================================
    async def _process_request(self, req: ProviderRequest) -> None:
        """按 image_urls → extra_parts → contexts 顺序处理。"""
        idx = 1
        # image_urls
        urls = self._pending_urls or list(req.image_urls or [])
        if urls:
            results = await self._describe_urls(urls)
            self._attach(req, results, idx, "image_urls")
            idx += len(results)
        self._pending_urls = None

        # extra_user_content_parts
        if self.config.get("include_extra_parts", True):
            parts = self._pending_parts or list(req.extra_user_content_parts or [])
            urls = _extract_urls_from_parts(parts)
            if urls:
                results = await self._describe_urls(urls)
                self._attach(req, results, idx, "extra_user_content_parts")
                idx += len(results)
        self._pending_parts = None

        # contexts
        if self.config.get("include_history", False):
            ctxs = self._pending_contexts or [
                c for c in (req.contexts or [])
                if isinstance(c, dict) and isinstance(c.get("content"), list)
                and any(isinstance(x, dict) and x.get("type") == "image_url"
                        for x in c.get("content", []))
            ]
            for c in ctxs:
                if not isinstance(c, dict):
                    continue
                content = c.get("content")
                if isinstance(content, list):
                    urls = _extract_urls_from_context_list(content)
                    if urls:
                        results = await self._describe_urls(urls)
                        self._attach(req, results, idx, "contexts", context_target=c)
                        idx += len(results)
        self._pending_contexts = None

    async def _describe_urls(self, urls):
        """: 并行调 mmx，并发度由 _vision_semaphore 控制。"""
        if not urls:
            return []
        # : gather 返 list[str] desc — 包装回 caller 期望的 [(idx, url, desc)] 格式
        descs = await asyncio.gather(*[self._describe_one(u) for u in urls])
        return [(i + 1, u, d) for i, (u, d) in enumerate(zip(urls, descs))]

    async def _describe_one(self, url: str) -> str:
        """: 内存缓存 → SQLite 缓存 → mmx 三级查找。"""
        url = (url or "").strip()
        if not url:
            return ""
        cacheable = self.config.get("cache_descriptions", True) and _is_cacheable_url(url, self.config)

        # : 快路径 — URL md5 当 id 查
        if cacheable:
            quick_key = CaptionCache.make_id_from_url(url)
            # 1) 内存缓存
            if quick_key in self._description_cache:
                if self._should_log("cache_trace"):
                    logger.info("[vision_text_bridge] 命中内存缓存 (快路径, 跳过下载): key=%s, url=%s",
                                quick_key[:16], self._preview(url))
                return self._description_cache[quick_key]
            # 2) SQLite 缓存
            if self._caption_cache is not None:
                entry = self._caption_cache.get(quick_key)
                if entry is not None:
                    self._description_cache[quick_key] = entry.description
                    if self._should_log("cache_trace"):
                        logger.info("[vision_text_bridge] 命中 SQLite 缓存 (快路径): key=%s, hits=%d",
                                    quick_key[:16], entry.hit_count)
                    return entry.description

        # : 慢路径 — md5(image_bytes) 防内容重复
        cache_key, image_bytes = await self._compute_image_cache_key(url) if cacheable else (None, b"")
        if cacheable and cache_key:
            cache = getattr(self, "_last_image_bytes", None)
            if cache is None:
                cache = self._last_image_bytes = {}
            cache[url] = image_bytes

        # 内容 hash 命中检查
        if cacheable and cache_key and cache_key != quick_key:
            if cache_key in self._description_cache:
                self._last_image_bytes.pop(url, None)
                return self._description_cache[cache_key]
            if self._caption_cache is not None:
                entry = self._caption_cache.get(cache_key)
                if entry is not None:
                    self._description_cache[cache_key] = entry.description
                    return entry.description

        # 3) 调 mmx
        return await self._describe_via_mmx(url, cache_key, cacheable)

    async def _describe_via_mmx(self, url: str, cache_key: str | None, cacheable: bool) -> str:
        """实际调 mmx 子进程拿描述。失败返 "" + 记 log。"""
        timeout = max(5, _cfg_int(self.config, "command_timeout", 60))
        vision_prompt = (
            self.config.get("vision_prompt", "")
            or "请客观描述图中可见的元素（主体/场景/文字原文/色调/风格），"
               "严禁猜测游戏/番剧/品牌/角色名，看不出就说'无法确定'。"
        )
        command = self._build_vision_command(url, vision_prompt)
        assert self._vision_semaphore is not None
        async with self._vision_semaphore:
            t0 = time.monotonic()
            result, err = await self._exec_mmx_safely(command, timeout, url)
            if err is not None:
                self._last_image_bytes.pop(url, None)
                return ""
            elapsed = time.monotonic() - t0
            if not (result.ok and result.stdout.strip()):
                self._log_mmx_failure(result, url)
                self._last_image_bytes.pop(url, None)
                return ""
            description = self._truncate(self._strip_mmx_content(result.stdout))
            self._log_mmx_success(url, description, elapsed)
            if cacheable and cache_key:
                self._description_cache[cache_key] = description
                # 复用 _compute_image_cache_key 读过的 bytes, 避免 _persist 内部重读
                preloaded = self._last_image_bytes.pop(url, b"")
                await self._persist(cache_key, url, description, image_bytes=preloaded)
            return description

    async def _exec_mmx_safely(self, command, timeout, url):
        """调 mmx 子进程, 把各种异常收拢为 (result, err) 二元返 (要么有 result, 要么 err 是 str)。"""
        try:
            result = await self._run_mmx(*command, timeout=timeout)
            return result, None
        except asyncio.TimeoutError:
            logger.warning("[vision_text_bridge] mmx 超时(%ss): %s", timeout, self._preview(url))
            return None, "timeout"
        except Exception as e:
            self._diagnose_mmx_error(str(e), url)
            logger.warning("[vision_text_bridge] mmx 异常: %s, err=%s", self._preview(url), e)
            return None, str(e)

    def _log_mmx_failure(self, result, url: str) -> None:
        err_text = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
        self._diagnose_mmx_error(err_text, url)
        logger.warning(
            "[vision_text_bridge] mmx 失败: %s, exit=%d, err=%s",
            self._preview(url), result.returncode, self._redact_text(err_text[:300]),
        )
        if self._should_log("mmx_subprocess"):
            # 优化: redact + slice 只在 verbose 开启时执行 (避开 2000B 字符串构造)
            logger.info("[vision_text_bridge] mmx 完整输出:\n--- stdout ---\n%s\n--- stderr ---\n%s",
                        self._redact_text(result.stdout[:2000]),
                        self._redact_text(result.stderr[:2000]))

    def _log_mmx_success(self, url: str, description: str, elapsed: float) -> None:
        logger.info(
            "[vision_text_bridge] mmx 完成: %s, 耗时=%.2fs, 长度=%d",
            self._preview(url), elapsed, len(description),
        )
        logger.info("[vision_text_bridge] 描述预览: %s", self._preview(description, 120))

    async def _persist(
        self, image_id: str, url: str, description: str, image_bytes: bytes = b"",
    ) -> None:
        """写 SQLite 缓存，含 base64/mime/dim 元信息。"""
        if self._caption_cache is None:
            return
        b64, mime, w, h, size = await self._fetch_image_meta(url, image_bytes)
        # chat_archive 安装时统一由它管理图片缓存
        if chat_archive_integration.is_chat_archive_installed():
            b64 = ""
        try:
            self._caption_cache.put(
                image_id=image_id, url=url, description=description,
                image_b64=b64, mime_type=mime, file_size=size, width=w, height=h,
            )
            # **重要：始终 log 持久化结果** （不依赖 verbose 配置） 。
            # 用户反馈 "webui 看不到缓存" 场景：都是这里没日志。
            logger.info(
                "[vision_text_bridge] 写 SQLite 缓存成功: id=%s, url=%s, "
                "desc_len=%d, b64=%dB, mime=%s, size=%d",
                image_id[:16], self._preview(url, 60), len(description), len(b64), mime, size,
            )
        except Exception as e:
            logger.warning("[vision_text_bridge] 写 SQLite 缓存失败: %s", e)

    async def _fetch_image_meta(
        self, url: str, preloaded: bytes = b"",
    ) -> tuple[str, str, int, int, int]:
        """读图片字节并算 base64/mime/dim/size。失败只影响缩略图。"""
        if preloaded:
            return self._build_meta_from_bytes(preloaded)
        try:
            data = await self._read_image_bytes(url)
        except Exception as e:
            logger.warning(
                "[vision_text_bridge] 读图字节失败（仅缩略图受影响，description 仍会写）: %s",
                self._preview(url), exc_info=False,
            )
            if self._should_log("id_computation"):
                logger.debug("[vision_text_bridge] 读字节异常详情: %s", e)
            return "", "", 0, 0, 0
        if not data:
            return "", "", 0, 0, 0
        return self._build_meta_from_bytes(data)

    def _build_meta_from_bytes(self, data: bytes) -> tuple[str, str, int, int, int]:
        """从图片字节算 (b64, mime, w, h, size)。同步、不读 I/O。"""
        size = len(data)
        mime, w, h = _sniff_image_meta(data)
        # : 大图跳过 b64 存储
        max_b64_kb = _cfg_int(self.config, "max_b64_size_kb", 2048)
        if max_b64_kb > 0 and size <= max_b64_kb * 1024:
            b64 = base64.b64encode(data).decode("ascii")
        else:
            b64 = ""
            if self._should_log("cache_trace"):
                logger.info(
                    "[vision_text_bridge] 跳过 b64 存储: size=%dB > %dKB",
                    size, max_b64_kb,
                )
        return b64, mime, w, h, size

    def _attach(self, req, descriptions, start_index, field, context_target=None):
        """把描述作为 TextPart 注入 req.extra_user_content_parts。"""
        if not descriptions:
            return
        ph = self.config.get("image_placeholder_template", "") or "[Image {index} 描述] {description}"
        fail = self.config.get("failure_message", "") or "[Image {index} 描述] 理解失败：{error}"
        if req.extra_user_content_parts is None:
            req.extra_user_content_parts = []
        ok_n = fail_n = 0
        for off, (_, _url, desc) in enumerate(descriptions):
            gi = start_index + off
            if desc:
                text = ph.format(index=gi, description=desc)
                ok_n += 1
            else:
                text = fail.format(index=gi, error="mmx 调用失败或超时")
                fail_n += 1
            req.extra_user_content_parts.append(_to_text_part({"type": "text", "text": text}))
        # 同步清掉被处理过的 image_url（仅对应字段）
        if field == "image_urls":
            req.image_urls = []
        elif field == "extra_user_content_parts" and req.extra_user_content_parts:
            req.extra_user_content_parts[:] = [p for p in req.extra_user_content_parts
                                               if not _is_image_url_part(p)]
        elif field == "contexts" and isinstance(context_target, dict):
            content = context_target.get("content")
            if isinstance(content, list):
                content[:] = [x for x in content
                              if not (isinstance(x, dict) and x.get("type") == "image_url")]
        if self._should_log("hook_trace"):
            logger.info("[vision_text_bridge] field=%s 处理: 成功=%d, 失败=%d", field, ok_n, fail_n)

    def _inject_guidance(self, req):
        """: 向 system_prompt 注入图说引导提示。"""
        if not self.config.get("inject_system_prompt_guidance", True):
            return
        captions = []
        for p in (req.extra_user_content_parts or []):
            text = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") or ""
            if text and re.search(r"\[Image\s+\d+\s+描述\]", text):
                captions.append(text)
        if not captions:
            return
        n = len(captions)
        tags = "[Image 1 描述]" if n == 1 else ", ".join(f"[Image {i+1} 描述]" for i in range(n))
        if self.config.get("inject_caption_text_to_system_prompt", False):
            guidance = (
                f"\n\n[视觉模型描述] 用户消息中包含 {n} 张图片，描述如下：\n\n"
                + "\n\n".join(captions)
                + f"\n\n以上描述标记为 {tags}。请严格基于这些描述回答，"
                  "不要猜测未出现的游戏/番剧/品牌/角色名，不要补充背景知识，"
                  "不要改写/扩充，不要装作'看到'描述外的信息。"
                  "如描述不足请明确说'无法从图中看出'。"
            )
        else:
            guidance = (
                f"\n\n[视觉模型描述] 用户消息中包含 {n} 张图片，描述标记为 {tags}。"
                "请严格基于这些描述回答用户，不要：\n"
                "  - 猜测未在描述中明确出现的游戏/番剧/品牌/角色名；\n"
                "  - 凭印象补充描述之外的背景知识；\n"
                "  - 改写/扩充已描述的内容；\n"
                "  - 装作'看到'描述中未出现的信息。\n"
                "如果描述不足以回答用户问题，请明确说'无法从图中看出'。"
            )
        req.system_prompt = (req.system_prompt or "") + guidance
        if self._should_log("hook_trace"):
            logger.info("[vision_text_bridge] system_prompt 注入提示，图片数=%d, 增量=%d",
                        n, len(guidance))

    # =========================================================================
    # mmx CLI 封装 (: 逻辑全抽到 mmx_runner, main.py 只留薄包装)
    # =========================================================================

    def _build_vision_command(self, image, prompt):
        return _build_vision_command(image, prompt)

    async def _run_mmx(self, *args, timeout) -> MmxResult:
        return await _run_mmx_fn(
            self.mmx_path, args, timeout,
            log_subprocess=self._should_log("mmx_subprocess"),
        )

    async def _login_mmx(self, api_key: str) -> None:
        if not self.mmx_path:
            return
        masked = (f"{api_key[:4]}***REDACTED***(len={len(api_key)})"
                  if self.config.get("redact_sensitive", True) else api_key)
        logger.info("[vision_text_bridge] 预登录 MiniMax CLI: %s", masked)
        try:
            # 走 self._run_mmx 让 patch.object 仍能拦截 (老测试依赖这个 path)
            r = await self._run_mmx("auth", "login", "--api-key", api_key, timeout=30)
            if r.ok:
                logger.info("[vision_text_bridge] 预登录成功: %s", (r.stdout or "").strip() or "(无输出)")
            else:
                logger.warning(
                    "[vision_text_bridge] 预登录失败: rc=%d, stderr=%s",
                    r.returncode, (r.stderr or "").strip()[:200],
                )
        except Exception as e:
            logger.warning("[vision_text_bridge] 预登录异常: %s", e)

    async def _install_mmx_cli(self) -> bool:
        """: 装 mmx-cli, 返 bool. 装成功 True, 失败 False (不抛)."""
        return await _install_mmx_cli_fn(self.npm_path)

    def _diagnose_mmx_error(self, err_text: str, url: str) -> None:
        _diagnose_mmx_error_fn(err_text, url, self._preview, VisionTextBridgePlugin._DIAGNOSED)

    def _warn_once(self, key: str, message: str) -> None:
        if key in VisionTextBridgePlugin._DIAGNOSED:
            return
        VisionTextBridgePlugin._DIAGNOSED.add(key)
        logger.warning(message)

    # =========================================================================
    # 工具
    # =========================================================================
    def _truncate(self, text: str) -> str:
        return _truncate_text(text, self.config)

    def _strip_mmx_content(self, stdout: str) -> str:
        return _strip_mmx_content_fn(stdout, self.config)

    def _preview(self, text: str, limit: int = 80) -> str:
        return _preview_text(text, limit, self.config)

    # : 过滤工具
    def _filter_tools_in_event(self, event, req) -> None:
        """提前清除 event extra 和 req.func_tool 中禁用的工具。"""
        mode = _cfg_str(self.config, "tool_filter_mode", "off").lower()
        if mode == "off":
            return
        names_raw = _cfg_str(self.config, "tool_filter_names", "")
        names = [n.strip() for n in names_raw.split(",") if n.strip()]
        if not names:
            return
        extra_key = _cfg_str(self.config, "tool_filter_extra_key", "_group_chat_plus_func_tool").strip()
        # 1) 清 event.get_extra(extra_key) 里的待合并 tool set
        if extra_key:
            try:
                plugin_tool_set = event.get_extra(extra_key, None)
            except Exception:
                plugin_tool_set = None
            if plugin_tool_set is not None:
                n = _filter_disabled_tools(plugin_tool_set, mode, names)
                if n and self._should_log("hook_trace"):
                    logger.info("[vision_text_bridge] 从 %s 移除了 %d 个工具（mode=%s）", extra_key, n, mode)
        # 2) 同步清 req.func_tool 里已注册的工具（防御性：其它插件可能直接 push）
        try:
            ft = getattr(req, "func_tool", None)
        except Exception:
            ft = None
        if ft is not None:
            n = _filter_disabled_tools(ft, mode, names)
            if n and self._should_log("hook_trace"):
                logger.info("[vision_text_bridge] 从 req.func_tool 移除了 %d 个工具（mode=%s）", n, mode)

    def _redact(self, args):
        return _redact_args_fn(args, self.config)

    @staticmethod
    def _redact_text(text: str) -> str:
        """脱敏，调用 mmx_runner.redact_text。"""
        return _redact_text(text)

    async def _compute_image_cache_key(self, url) -> tuple[str, bytes]:
        """算 image_id + 返读到的 bytes（供 _persist 复用）。"""
        try:
            data = await self._read_image_bytes(url)
        except Exception as e:
            if self._should_log("id_computation"):
                logger.debug("[vision_text_bridge] 读图字节失败，image_id 退到 md5(url): %s, err=%s",
                             self._preview(url), e)
            return CaptionCache.make_id_from_url(url), b""
        if not data:
            return CaptionCache.make_id_from_url(url), b""
        if self._should_log("id_computation"):
            logger.info("[vision_text_bridge] image_id=md5(%dB)=%s", len(data),
                        CaptionCache.make_id_from_bytes(data)[:16] + "…")
        return CaptionCache.make_id_from_bytes(data), data

    async def _read_image_bytes(self, url):
        """薄包装，实际逻辑在 image_fetch.py。"""
        return await _read_image_bytes(url)


# ===========================================================================
# : 工具已抽到 image_utils/tool_filter, 此处为向后兼容 shim
# ===========================================================================
import fnmatch as _fnmatch
def _glob_match(name: str, pattern: str) -> bool:
    return _fnmatch.fnmatchcase(name, pattern)

