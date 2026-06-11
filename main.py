"""
astrbot_plugin_vision_text_bridge
==================================

拦截 LLM 请求中的图片，调用 MiniMax CLI 图像理解后把描述以 user content
block 形式注入到 ``req.extra_user_content_parts``，再交给对话模型。

参考实现：
- 拦截钩子: ``astrbot_plugin_uni_nickname`` 的 ``@filter.on_llm_request``
- 图像理解: ``astrbot_plugin_MiniMax_CLI`` 的 ``mmx vision describe`` 子进程
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import os
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

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
try:
    # : 把图说作为 Pydantic ContentPart 注入 req.extra_user_content_parts
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # 测试沙箱/未来重命名兼容

# : 同级模块直接 import (sys.path 已加, AstrBot + 测试沙箱都能解析)
from config_helpers import cfg_int as _cfg_int, cfg_str as _cfg_str
from image_utils import is_image_url_part as _is_image_url_part, extract_url_from_item as _extract_url_from_item, extract_urls_from_parts as _extract_urls_from_parts, extract_urls_from_context_list as _extract_urls_from_context_list, is_data_url as _is_data_url, strip_image_urls as _strip_image_urls
from image_meta import to_text_part as _to_text_part, sniff_image_meta as _sniff_image_meta, is_cacheable_url as _is_cacheable_url
from image_fetch import read_image_bytes as _read_image_bytes, _read_file_bytes_sync
try:
    from image_utils import collect_image_urls_from_components as _collect_image_urls_from_components
except ImportError:
    # 向后兼容: 旧版 image_utils.py 没有这个函数 (v0.8.37 之前) — 本地 fallback 复制
    # 让插件不 import 失败, 走老逻辑 (不递归扫嵌套 comp)
    async def _collect_image_urls_from_components(components, dedupe=None):
        added = 0
        for comp in components:
            ctype = getattr(comp, "type", None)
            if ctype in ("image", "Image") and callable(getattr(comp, "convert_to_file_path", None)):
                try:
                    fp = await comp.convert_to_file_path()
                except Exception:
                    fp = None
                if fp and (dedupe is None or fp not in dedupe):
                    if dedupe is not None:
                        dedupe.append(fp)
                    added += 1
        return added
    logger.warning("[vision_text_bridge] 旧版 image_utils.py 无 collect_image_urls_from_components — 已用本地 fallback, 嵌套扫描将失效。git pull 后重启 AstrBot 解决。")
from tool_filter import match_tool_name as _match_tool_name, filter_disabled_tools as _filter_disabled_tools
from mmx_runner import (
    MmxResult, build_vision_command as _build_vision_command,
    run_mmx as _run_mmx_fn, install_mmx_cli as _install_mmx_cli_fn,
    diagnose_mmx_error as _diagnose_mmx_error_fn,
    truncate as _truncate_text, strip_mmx_content as _strip_mmx_content_fn,
    preview as _preview_text, redact_text as _redact_text, redact_args as _redact_args_fn,
)
import web_api  # web_api.register_all_routes 在 _register_web_apis 里调


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

PLUGIN_NAME = "astrbot_plugin_vision_text_bridge"
# AstrBot on_llm_request priority 越大越先跑；100 高于多数常见插件。
# priority 在 import 时锁定，调配置后需重启 AstrBot。
DEFAULT_PRIORITY = 100

# Module-level: 预编译 bot 头像 URL 过滤 regex
# 避免每个 hook 入口都 re.compile() (虽然 Python 内部有 cache, 但显式更快更清晰)
_BOT_AVATAR_PAT = re.compile(r"q\.qlogo\.cn/headimg_dl\?", re.IGNORECASE)

# : 预编译的 markdown 清理 regex 抽到 mmx_runner.py ()


class _MemoryCache:
    """: 内存热缓存——带 TTL + LRU size 上限。

    - 防御资源泄露：之前是裸 dict，永远不会过期也不会被淘汰
    - 过期处理：get() 检查 ``expire_at``（同 _sibling_cache.CaptionCache 5 分钟去重窗口一致）
    - LRU 淘汰：set 越上限删最久未访问项
    - 本类是单线程使用（asyncio 事件循环），不需要锁
    """

    __slots__ = ("_m", "_max_size", "_ttl")

    def __init__(self, ttl_seconds: int, max_size: int):
        # dict 是有序的（Python 3.7+），插入/更新顺序即为 LRU 顺序
        self._m: dict[str, tuple[str, float]] = {}
        self._max_size = max(1, int(max_size))
        self._ttl = max(0, int(ttl_seconds))

    def get(self, key: str) -> str | None:
        """取并刷新 LRU 顺序；过期或不存在返 None。"""
        v = self._m.get(key)
        if v is None:
            return None
        text, expire_at = v
        if self._ttl > 0 and time.time() >= expire_at:
            # 过期，懒删除
            self._m.pop(key, None)
            return None
        # 刷新 LRU：pop + set（Python dict 重赋值同 key **不**会动插入顺序）
        if self._max_size > 0:
            self._m.pop(key, None)
            self._m[key] = v
        return text

    def put(self, key: str, value: str) -> None:
        if self._max_size <= 0:
            return
        if key in self._m:
            self._m.pop(key, None)
        expire = time.time() + self._ttl if self._ttl > 0 else float("inf")
        self._m[key] = (value, expire)
        # 越上限——从头开始删（最久未访问）
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

    # 字典语法糖——允许 ``cache[key] = v`` / ``cache[key]`` 老用法
    def __setitem__(self, key: str, value: str) -> None:
        self.put(key, value)

    def __getitem__(self, key: str) -> str:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号（避免 @register 装饰器硬编码跟 metadata.yaml 脱节）。"""
    try:
        import yaml  # AstrBot 依赖 PyYAML
        meta_path = Path(__file__).resolve().parent / "metadata.yaml"
        with open(meta_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return str(data.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


# : 配置读取 helper——原模式 ``int(self.config.get(k, d) or d)`` 重复 15+ 次
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


def _read_file_bytes_sync(path: str) -> bytes:
    """供 asyncio.to_thread 调用的同步读文件。"""
    with open(path, "rb") as f:
        return f.read()


# ===========================================================================
# 数据结构 (MmxResult 移到 mmx_runner.py — 在 import 区域透传过来)
# ===========================================================================


# ===========================================================================
# 插件主体
# ===========================================================================
@register(
    "astrbot_plugin_vision_text_bridge",
    "Mavis",
    "把图片转成 MiniMax CLI 图像理解后的文本，再喂给对话 LLM",
    PLUGIN_VERSION,
)
class VisionTextBridgePlugin(Star):
    """Vision -> Text 桥接。

    链路：on_llm_request 钩子 → 扫描三类图片来源 → 并发调 mmx → 描述以
    ``req.extra_user_content_parts`` (content block 形式) 注入 user message →
    从原字段移除 image_url → 链末 (priority=-10000) 兜底清残留。
    """

    # mmx 同一错误只诊断一次，避免刷屏
    _DIAGNOSED: set[str] = set()

    # ------------------------------------------------------------------ init

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.mmx_path = (self.config.get("mmx_path") or "").strip() or shutil.which("mmx") or shutil.which("mmx.cmd")
        self.npm_path = shutil.which("npm") or shutil.which("npm.cmd")
        self._caption_cache: CaptionCache | None = None
        # : 内存热缓存改为 _MemoryCache——带 TTL + LRU size 上限
        # ttl_seconds=0 表示不过期，max_size=0 表示不限制；默认值在配置里调
        self._description_cache: _MemoryCache = _MemoryCache(
            ttl_seconds=_cfg_int(self.config, "memory_cache_ttl_seconds", 300),
            max_size=_cfg_int(self.config, "memory_cache_max_size", 500),
        )
        self._vision_semaphore: asyncio.Semaphore | None = None
        self._configured_priority: int = self._resolve_priority()
        # 跨 _describe_one -> _persist 复用刚读过的图片 bytes, 避免 mmx 后再读一次 (P0 优化)
        self._last_image_bytes: dict[str, bytes] = {}
        self._priority_locked_warning_emitted = False
        # 钩子快照（主钩子入口设置，_process_request 消费；_process_request 也可独立调用）
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
        """任一 verbose_* 开关为 true 即开。verbose_logging 是总开关。

        调试时不必 4 个开关都开，先开 ``verbose_logging`` 看全量；定位到具体
        阶段后只开对应细粒度开关，避免日志爆炸。
        """
        if self.config.get("verbose_logging", False):
            return True
        return any(bool(self.config.get(f"verbose_{f}", False)) for f in flags)

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        max_concurrent = max(1, _cfg_int(self.config, "max_concurrent_vision", 3))
        self._vision_semaphore = asyncio.Semaphore(max_concurrent)

        # 1. SQLite 缓存
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

        # 1.5 : 启动 SQLite 过期清理后台 task
        if self._caption_cache is not None:
            try:
                ttl_days = _cfg_int(self.config, "sqlite_cache_ttl_days", 7)
                interval_h = _cfg_int(self.config, "sqlite_clean_interval_hours", 1)
                # 启动时先清一次（不管 interval）
                if ttl_days > 0:
                    deleted = self._caption_cache.clean_expired(ttl_days)
                    self._last_clean_at = time.time()  # : 供 webui 算下次清理
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

        # 3. mmx 安装 + 预登录
        if not self.mmx_path and self.config.get("auto_install_cli", False):
            await self._install_mmx_cli()
            self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd")
        if not self.mmx_path:
            logger.warning(
                "[vision_text_bridge] 未找到 mmx CLI，请先 npm install -g mmx-cli"
                " 或在插件配置中指定 mmx_path。"
            )
            return
        if self.config.get("auto_login", True):
            api_key = (self.config.get("minimax_api_key") or "").strip()
            if not api_key:
                logger.info("[vision_text_bridge] 未配置 minimax_api_key，跳过自动登录")
            else:
                await self._login_mmx(api_key)

        # 4. 伪装 provider modality（防 AstrBot 切 fallback）
        self._mark_providers_support_image()

        # 5. 联动检测
        self._check_compatibility()

    def _get_plugin_data_dir(self) -> Path:
        try:
            from astrbot.api.star import StarTools
            p = Path(StarTools.get_data_dir())
        except Exception:
            p = Path(__file__).resolve().parent / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _strip_image_fields_from_req(self, req) -> None:
        """从 req 的 extra_user_content_parts + contexts 里剩除所有 image_url 字段。

        调用场景: 主钩子入口 (_bridge_vision_to_text) 调。
        """
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

    def _mark_providers_support_image(self) -> None:
        """给所有 provider 补 'image' modality 标签，骗 AstrBot 不切 fallback。

        **为什么需要**：on_llm_request 钩子入口已清空 image_urls，但 AstrBot
        在钩子**之前**就检查了 provider modality。本钩子只修改 provider 内存
        里的 ``provider_config["modalities"]``，不会真发图给不支持图的 provider
        （因为 image_urls 始终为空）。
        """
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
        """收集所有 provider 对象 (兼容多版本 AstrBot API)。

        优先从 ``ctx.provider_manager.providers`` 取；老版本没有 manager 时
        从 ``_using_provider_id`` / ``default_provider_id`` + fallback_chat_models 反查。
        """
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
        """检查已装插件并给优先级/兼容性提示。"""
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
        """: web API 全部移到 :mod:`web_api` 模块,
        handler 深度从 4 层 (闭包嵌闭包) 减到 1 层 (top-level handler(plugin))."""
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
        if self._vision_semaphore is None:
            self._vision_semaphore = asyncio.Semaphore(
                max(1, _cfg_int(self.config, "max_concurrent_vision", 3))
            )

        # === 0) : 预先过滤待注入的工具集（chat_plus 之后才 merge）===
        # chat_plus 会在 priority=-1 从 event.get_extra() 取待注入的 tool set 合并到 req.func_tool
        # 我们 priority=100 先跑，把待合并的工具集里不该要的提前删掉
        # 这样 chat_plus merge 进去的就是干净版
        try:
            self._filter_tools_in_event(event, req)
        except Exception as e:
            if self._should_log("hook_trace"):
                logger.debug("[vision_text_bridge] 工具过滤跳过：%s", e)

        # === 1) 快照三类图片来源，**先清空** 防 AstrBot 切 fallback provider ===
        saved_urls = list(req.image_urls or [])
        saved_parts = list(req.extra_user_content_parts or []) if req.extra_user_content_parts else []
        saved_contexts = [c for c in (req.contexts or []) if isinstance(c, dict)]

        # 1a-pre) : 诊断日志——打印 saved_urls 原始来源，
        # 查出是否有 bot 自己的头像 / at 段被误注入
        if self._should_log("hook_trace"):
            logger.info(
                "[vision_text_bridge] hook 入口 saved_urls (size=%d): %s",
                len(saved_urls),
                [u[:80] + "..." if isinstance(u, str) and len(u) > 80 else u for u in saved_urls],
            )

        # 1a) 从 event.message_obj 补提（ 防御 chat_plus 抽走图）
        # : 递归扫描嵌套 (引用消息里包 image)
        #
        # 【重要】  同一个用户原图在 AstrBot 内部被存成 2 份：
        #   - req.image_urls 里: compressed_xxx.jpg （压缩版、AstrBot 用于发送给 provider）
        #   - event.message_obj 里: io_temp_img_xxx.jpg （原图、未压缩、供其他插件读）
        # 两份内容不同 (压缩 vs 未压缩) → md5 不同 → 调 2 次 mmx 浪费 13s
        #
        # 正确做法: 只在 ``req.image_urls`` **空**时 (chat_plus 已抽走图) 才递归补
        # event.message_obj——AstrBot 主动给到 image_urls 时, 不应重复补。
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

        # : 过滤 AstrBot 框架 user @ bot 时注入的 bot avatar
        # AstrBot 框架会主动把 bot 自己的头像 URL 塞到 req.image_urls
        # (q.qlogo.cn/headimg_dl?dst_uin=... 的固定模式)
        # 视觉理解 bot 头像没意义——跳过
        if saved_urls:
            filtered = [u for u in saved_urls if not (isinstance(u, str) and _BOT_AVATAR_PAT.search(u))]
            if len(filtered) != len(saved_urls):
                removed = set(saved_urls) - set(filtered)
                logger.info(
                    "[vision_text_bridge] 过滤 bot 头像 %d 张: %s",
                    len(removed), list(removed),
                )
                saved_urls = filtered
        # : 诊断——打 saved_urls 头部看看 AstrBot 到底注入了什么
        if saved_urls:
            logger.info(
                "[vision_text_bridge] hook 入口 saved_urls (size=%d): %s",
                len(saved_urls),
                [u[:120] + ("..." if isinstance(u, str) and len(u) > 120 else "") for u in saved_urls],
            )

        # 1b) 清空
        req.image_urls = []
        self._strip_image_fields_from_req(req)

        # 1c) 存快照给 _process_request 读
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

        # === : 链末兜底删 func_tool（chat_plus priority=-1 跑过之后）===
        # 即使主钩子 priority=100 没清干净，链末 priority=-10000 还能在最后扫一次
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
        """串行调 mmx（受 semaphore 限流）。返回 [(idx, url, desc), ...]。"""
        results = []
        for i, url in enumerate(urls, 1):
            results.append((i, url, await self._describe_one(url)))
        return results

    async def _describe_one(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        cacheable = self.config.get("cache_descriptions", True) and _is_cacheable_url(url, self.config)
        cache_key, image_bytes = await self._compute_image_cache_key(url) if cacheable else (None, b"")
        # cache_key 同步存 instance 临时字段, 给 _persist 复用 image_bytes (避免重读)
        if cacheable and cache_key:
            # lazy init (测试可能跳过 initialize 直接 new_plugin)
            cache = getattr(self, "_last_image_bytes", None)
            if cache is None:
                cache = self._last_image_bytes = {}
            cache[url] = image_bytes

        # 1) 内存缓存
        if cacheable and cache_key and cache_key in self._description_cache:
            self._last_image_bytes.pop(url, None)
            if self._should_log("cache_trace"):
                logger.info("[vision_text_bridge] 命中内存缓存: key=%s, url=%s",
                            cache_key[:16], self._preview(url))
            return self._description_cache[cache_key]

        # 2) SQLite 缓存
        if cacheable and cache_key and self._caption_cache is not None:
            entry = self._caption_cache.get(cache_key)
            if entry is not None:
                self._last_image_bytes.pop(url, None)
                if self._should_log("cache_trace"):
                    logger.info("[vision_text_bridge] 命中 SQLite 缓存: key=%s, hits=%d",
                                cache_key[:16], entry.hit_count)
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
        """写 SQLite 缓存（带 base64/mime/dim 元信息）。

        优化: 调用方传 ``image_bytes`` (cache_key 算的时候已读) 时复用, 避免再读一次。
        ``image_bytes=b""`` 时退到 _fetch_image_meta() 重读 (老路径, 兼容)。

        .1: 改成 async 以便在 event loop 里正常 await ``_read_image_bytes``。
        老版本用 ``asyncio.get_event_loop().run_until_complete`` 在 async 上下文
        会抛 ``RuntimeError("This event loop is already running")``，再 fallback
        到同步读 file:// — 但临时文件可能已被清理、/AstrBot 路径可能没读权限，
        异常被 except 静默吞掉，导致 SQLite 写入了 description 但 base64 是空。
        webui 看上去“缓存存在但没有缩略图”。
        """
        if self._caption_cache is None:
            return
        b64, mime, w, h, size = await self._fetch_image_meta(url, image_bytes)
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
        """读图片字节 + 算 base64/mime/dim/size。

        优化: ``preloaded`` 非空时跳过读字节, 直接从预读 bytes 算 meta。
        失败返 5 个空值 (b64='', mime='', w=0, h=0, size=0)。
        读字节失败 **仅** 影响缩略图 (base64/mime/dim), description 仍正常写入 SQLite。
        """
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
        # : 大图跳过 b64 存储 (避免 6.5MB base64 吞磁盘)
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
        """: 向 system_prompt 注入'严格引用图说'提示。

        可选配置 ``inject_caption_text_to_system_prompt`` 把图说本身也复制
        一份（默认 False 节省 token）。
        """
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

    async def _install_mmx_cli(self) -> None:
        await _install_mmx_cli_fn(self.npm_path)

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

    # : 在主钩子入口过滤工具
    def _filter_tools_in_event(self, event, req) -> None:
        """提前清 ``event.get_extra(extra_key)`` 里的待合并 tool set。

        场景：chat_plus priority=-1 会从这个 key 拿 tool set 合并到 ``req.func_tool``。
        我们 priority=100 先跑，把 set 里不想保留的工具删掉，chat_plus merge 进去的
        就是干净版。
        """
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

    _SENSITIVE = (
        re.compile(r"(sk-[A-Za-z0-9_-]{8,})"),
        re.compile(r"(?i)(token|signature|x-sign)=[^&\s]+"),
    )

    @staticmethod
    def _redact_text(text: str) -> str:
        """脱敏 —  调 mmx_runner.redact_text (regex 同源), 保留这个
        staticmethod 是因为 :class:`_old__test` 还会调它。
        """
        return _redact_text(text)

    async def _compute_image_cache_key(self, url) -> tuple[str, bytes]:
        """算图片的 cache_key + 同步返读到的 bytes (给 _persist 复用, 避免重读)。

        返回: (image_id, image_bytes)。读失败 / 空 bytes 时 image_id 退到 md5(url), bytes=b""。
        """
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
        """.4: 支持裸本地路径——薄包装, 实际逻辑抽到 image_fetch.py。"""
        return await _read_image_bytes(url)


# ===========================================================================
# : image url 工具已抽到 image_utils, 工具过滤到 tool_filter
# 这里是 shim 留旧名, 方便  时期测试还能 import (向后兼容过渡)
# ===========================================================================
import fnmatch as _fnmatch
def _glob_match(name: str, pattern: str) -> bool:
    return _fnmatch.fnmatchcase(name, pattern)

