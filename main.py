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
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
try:
    # v0.7+: 把图说作为 Pydantic ContentPart 注入 req.extra_user_content_parts
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # 测试沙箱/未来重命名兼容


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
CacheStats = _sibling_cache.CacheStats

PLUGIN_NAME = "astrbot_plugin_vision_text_bridge"
# AstrBot on_llm_request priority 越大越先跑；100 高于多数常见插件。
# priority 在 import 时锁定，调配置后需重启 AstrBot。
DEFAULT_PRIORITY = 100


def _read_file_bytes_sync(path: str) -> bytes:
    """供 asyncio.to_thread 调用的同步读文件。"""
    with open(path, "rb") as f:
        return f.read()


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class MmxResult:
    """mmx 子进程返回结果封装。"""
    stdout: str
    stderr: str
    returncode: int
    ok: bool


# ===========================================================================
# 插件主体
# ===========================================================================
@register(
    "astrbot_plugin_vision_text_bridge",
    "Mavis",
    "把图片转成 MiniMax CLI 图像理解后的文本，再喂给对话 LLM",
    "0.8.7.5",
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
        self._description_cache: dict[str, str] = {}
        self._vision_semaphore: asyncio.Semaphore | None = None
        self._configured_priority: int = self._resolve_priority()
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
        max_concurrent = max(1, int(self.config.get("max_concurrent_vision", 3) or 1))
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
        # 收集所有 provider 对象
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
        else:
            # 无 manager 的版本：从当前/fallback provider id 反查
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

    # ------------------------------------------------------------------ 兼容性

    def _check_compatibility(self) -> None:
        """检查已装插件并给优先级/兼容性提示。"""
        names = self._get_installed_plugin_names()
        if not names:
            return
        if "astrbot_plugin_chat_archive" in names:
            logger.info(
                "[vision_text_bridge] ℹ️ 检测到 astrbot_plugin_chat_archive。"
                "v0.8.6 起本插件不与之联动，图片存到本插件自己的 SQLite（含 base64）。"
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
        try:
            from quart import jsonify
        except ImportError:  # 测试沙箱
            def jsonify(o):  # type: ignore
                return o

        def ok(data):
            return jsonify({"ok": True, "data": data})

        def err(message, status=400):
            return jsonify({"ok": False, "error": message}), status

        def _args():
            try:
                return self.context.request.args if hasattr(self.context.request, "args") else {}
            except Exception:
                return {}

        async def api_stats():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            s = self._caption_cache.stats().to_dict()
            s["in_memory_cache_size"] = len(self._description_cache)
            return ok(s)

        async def api_list():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            a = _args()
            try:
                body = await self.context.request.json
            except Exception:
                body = {}
            limit = int(a.get("limit", 50) or 50)
            offset = int(a.get("offset", 0) or 0)
            search = (a.get("search", "") or "").strip()
            order_by = a.get("order_by", "created_at_desc") or "created_at_desc"
            items = self._caption_cache.list(limit=limit, offset=offset, search=search, order_by=order_by)
            return ok({"total": self._caption_cache.count(search=search),
                       "limit": limit, "offset": offset,
                       "items": [e.to_dict() for e in items]})

        async def api_delete():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            try:
                body = await self.context.request.json
            except Exception:
                body = {}
            key = (body.get("key") or "").strip()
            if not key:
                return err("缺少参数 key")
            self._description_cache.pop(key, None)
            return ok({"deleted": self._caption_cache.delete(key), "key": key})

        async def api_clear():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            n = self._caption_cache.clear()
            self._description_cache.clear()
            try:
                self._caption_cache.vacuum()
            except Exception as e:
                logger.warning("[vision_text_bridge] VACUUM 失败: %s", e)
            return ok({"cleared": n})

        async def api_regenerate():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            try:
                body = await self.context.request.json
            except Exception:
                body = {}
            key = (body.get("key") or "").strip()
            if not key:
                return err("缺少参数 key")
            self._description_cache.pop(key, None)
            self._caption_cache.delete(key)
            new_desc = await self._describe_one(key)
            return ok({"key": key, "description": new_desc, "ok": bool(new_desc)})

        async def api_export():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            entries = self._caption_cache.list(limit=10000, offset=0)
            return ok({"exported_at": time.time(), "count": len(entries),
                       "items": [e.to_dict() for e in entries]})

        async def api_thumbnail():
            """v0.8.7.5: 改 POST。绕开 AstrBot bridge SDK 在带 query string 时
            将 /api/plugin/ 拼成 /api/plug/ 的路径 bug（返 400 Bad Request）。"""
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            # 优先 JSON body（POST），fallback 到 query string（GET）
            image_id = ""
            try:
                body = await self.context.request.json
                if isinstance(body, dict):
                    image_id = (body.get("image_id", "") or "").strip()
            except Exception:
                pass
            if not image_id:
                a = _args()
                image_id = (a.get("image_id", "") or "").strip()
            if not image_id:
                return err("缺少参数 image_id")
            e = self._caption_cache.get(image_id, with_b64=True)
            if e is None:
                return err("未找到该 image_id", 404)
            mime = e.mime_type
            if not e.image_b64:
                return ok({"image_id": image_id, "mime_type": mime, "data_url": "",
                           "width": e.width, "height": e.height,
                           "file_size": e.file_size, "has_image": False})
            data_mime = mime or "image/jpeg"
            return ok({"image_id": image_id, "mime_type": data_mime,
                       "data_url": f"data:{data_mime};base64,{e.image_b64}",
                       "width": e.width, "height": e.height,
                       "file_size": e.file_size, "has_image": True})

        async def api_diag():
            """v0.8.7.1 新增: 诊断 endpoint。

            返 SQLite 路径、条目数、最近 3 条记录摘要、schema。
            在 webui 看不到数据时调用，验证 SQLite 里到底有没有东西。
            """
            if self._caption_cache is None:
                return ok({"cache_initialized": False,
                           "hint": "SQLite 缓存未初始化——请看 AstrBot 启动日志里 [vision_text_bridge] 初始化描述缓存"})
            # 直接查 SQLite 拿原始 schema 和最近 3 条
            import sqlite3
            try:
                conn = sqlite3.connect(self._caption_cache._db_path)
                conn.row_factory = sqlite3.Row
                cols = [r[1] for r in conn.execute("PRAGMA table_info(image_captions)").fetchall()]
                total = conn.execute("SELECT COUNT(*) FROM image_captions").fetchone()[0]
                # 最近 3 条
                recent = []
                for row in conn.execute(
                    "SELECT image_id, length(description) AS desc_len, image_b64, "
                    "mime_type, file_size, width, height, created_at "
                    "FROM image_captions ORDER BY created_at DESC LIMIT 3"
                ).fetchall():
                    recent.append({
                        "image_id": row["image_id"],
                        "desc_len": row["desc_len"],
                        "has_b64": bool(row["image_b64"]),
                        "b64_len": len(row["image_b64"]) if row["image_b64"] else 0,
                        "mime_type": row["mime_type"],
                        "file_size": row["file_size"],
                        "width": row["width"],
                        "height": row["height"],
                        "created_at": row["created_at"],
                    })
                conn.close()
            except Exception as e:
                return ok({"cache_initialized": True, "error": str(e)})
            return ok({
                "cache_initialized": True,
                "db_path": self._caption_cache._db_path,
                "schema_columns": cols,
                "total_entries": total,
                "in_memory_cache_size": len(self._description_cache),
                "recent_3": recent,
            })

        for path, fn, methods, desc in [
            ("/cache/stats", api_stats, ["GET"], "Cache stats"),
            ("/cache/list", api_list, ["GET"], "Cache list"),
            ("/cache/delete", api_delete, ["POST"], "Delete entry"),
            ("/cache/clear", api_clear, ["POST"], "Clear all"),
            ("/cache/regenerate", api_regenerate, ["POST"], "Regenerate"),
            ("/cache/export", api_export, ["GET"], "Export JSON"),
            ("/cache/thumbnail", api_thumbnail, ["POST", "GET"], "Thumbnail data URL (POST 绕开 AstrBot bridge SDK 路径 bug)"),
            ("/cache/diag", api_diag, ["GET"], "v0.8.7.1 诊断：DB 路径/schema/最近 3 条"),
        ]:
            self.context.register_web_api(f"/{PLUGIN_NAME}{path}", fn, methods, desc)

    async def terminate(self) -> None:
        self._description_cache.clear()
        self._caption_cache = None
        logger.info("[vision_text_bridge] 插件已卸载，缓存已清理")

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
                max(1, int(self.config.get("max_concurrent_vision", 3) or 1))
            )

        # === 1) 快照三类图片来源，**先清空** 防 AstrBot 切 fallback provider ===
        saved_urls = list(req.image_urls or [])
        saved_parts = list(req.extra_user_content_parts or []) if req.extra_user_content_parts else []
        saved_contexts = [c for c in (req.contexts or []) if isinstance(c, dict)]

        # 1a) 从 event.message_obj 补提（v0.8.5 防御 chat_plus 抽走图）
        if event is not None:
            try:
                chain = getattr(getattr(event, "message_obj", None), "message", None)
                if chain:
                    for comp in chain:
                        ctype = getattr(comp, "type", None)
                        if ctype not in ("image", "Image"):
                            continue
                        if not callable(getattr(comp, "convert_to_file_path", None)):
                            continue
                        try:
                            fp = await comp.convert_to_file_path()
                        except Exception:
                            fp = None
                        if fp and fp not in saved_urls:
                            saved_urls.append(fp)
            except Exception as e:
                if self._should_log("hook_trace"):
                    logger.debug("[vision_text_bridge] 补提 event.message_obj 图失败: %s", e)

        # 1b) 清空
        req.image_urls = []
        if req.extra_user_content_parts:
            req.extra_user_content_parts[:] = [p for p in req.extra_user_content_parts
                                               if not _is_image_url_part(p)]
        if req.contexts:
            for c in req.contexts:
                if isinstance(c, dict) and isinstance(c.get("content"), list):
                    c["content"][:] = [x for x in c["content"]
                                       if not (isinstance(x, dict) and x.get("type") == "image_url")]

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
        cache_key = await self._compute_image_cache_key(url) if cacheable else None

        # 1) 内存缓存
        if cacheable and cache_key and cache_key in self._description_cache:
            if self._should_log("cache_trace"):
                logger.info("[vision_text_bridge] 命中内存缓存: key=%s, url=%s",
                            cache_key[:16], self._preview(url))
            return self._description_cache[cache_key]

        # 2) SQLite 缓存
        if cacheable and cache_key and self._caption_cache is not None:
            entry = self._caption_cache.get(cache_key)
            if entry is not None:
                if self._should_log("cache_trace"):
                    logger.info("[vision_text_bridge] 命中 SQLite 缓存: key=%s, hits=%d",
                                cache_key[:16], entry.hit_count)
                self._description_cache[cache_key] = entry.description
                return entry.description

        # 3) 调 mmx
        timeout = max(5, int(self.config.get("command_timeout", 60) or 60))
        vision_prompt = (
            self.config.get("vision_prompt", "")
            or "请客观描述图中可见的元素（主体/场景/文字原文/色调/风格），"
               "严禁猜测游戏/番剧/品牌/角色名，看不出就说'无法确定'。"
        )
        command = self._build_vision_command(url, vision_prompt)
        assert self._vision_semaphore is not None
        async with self._vision_semaphore:
            t0 = time.monotonic()
            try:
                result = await self._run_mmx(*command, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("[vision_text_bridge] mmx 超时(%ss): %s", timeout, self._preview(url))
                return ""
            except Exception as e:
                self._diagnose_mmx_error(str(e), url)
                logger.warning("[vision_text_bridge] mmx 异常: %s, err=%s", self._preview(url), e)
                return ""

            elapsed = time.monotonic() - t0
            if not (result.ok and result.stdout.strip()):
                err_text = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
                self._diagnose_mmx_error(err_text, url)
                logger.warning(
                    "[vision_text_bridge] mmx 失败: %s, exit=%d, err=%s",
                    self._preview(url), result.returncode, self._redact_text(err_text[:300]),
                )
                if self._should_log("mmx_subprocess"):
                    logger.info("[vision_text_bridge] mmx 完整输出:\n--- stdout ---\n%s\n--- stderr ---\n%s",
                                self._redact_text(result.stdout[:2000]),
                                self._redact_text(result.stderr[:2000]))
                return ""

            # 成功
            description = self._truncate(result.stdout.strip())
            logger.info(
                "[vision_text_bridge] mmx 完成: %s, 耗时=%.2fs, 长度=%d",
                self._preview(url), elapsed, len(description),
            )
            logger.info("[vision_text_bridge] 描述预览: %s", self._preview(description, 120))
            if cacheable and cache_key:
                self._description_cache[cache_key] = description
                # v0.8.7.1: _persist 变 async，直接 await。之前的同步版本
                # 在 async 上下文里用 `asyncio.get_event_loop().run_until_complete`
                # 必抛 RuntimeError，fallback 到同步读 file:// 经常被静默吞掉。
                await self._persist(cache_key, url, description)
            return description

    async def _persist(self, image_id: str, url: str, description: str) -> None:
        """写 SQLite 缓存（带 base64/mime/dim 元信息）。

        v0.8.7.1: 改成 async 以便在 event loop 里正常 await ``_read_image_bytes``。
        老版本用 ``asyncio.get_event_loop().run_until_complete`` 在 async 上下文
        会抛 ``RuntimeError("This event loop is already running")``，再 fallback
        到同步读 file:// — 但临时文件可能已被清理、/AstrBot 路径可能没读权限，
        异常被 except 静默吞掉，导致 SQLite 写入了 description 但 base64 是空。
        webui 看上去“缓存存在但没有缩略图”。
        """
        if self._caption_cache is None:
            return
        b64, mime, w, h, size = "", "", 0, 0, 0
        try:
            data = await self._read_image_bytes(url)
            if data:
                b64 = base64.b64encode(data).decode("ascii")
                size = len(data)
                mime, w, h = _sniff_image_meta(data)
        except Exception as e:
            # 读字节失败 **仅** 影响缩略图（base64/mime/dim），
            # description 仍正常写入 SQLite。记 warning 不报错。
            logger.warning(
                "[vision_text_bridge] 读图字节失败（仅缩略图受影响，description 仍会写）: %s",
                self._preview(url), exc_info=False,
            )
            if self._should_log("id_computation"):
                logger.debug("[vision_text_bridge] 读字节异常详情: %s", e)
        try:
            # v0.8.7.3: 无论如何都记录一次，put 是否真写了。
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
        """v0.7+: 向 system_prompt 注入'严格引用图说'提示。

        可选配置 ``inject_caption_text_to_system_prompt`` 把图说本身也复制
        一份（默认 False 节省 token）。
        """
        if not self.config.get("inject_system_prompt_guidance", True):
            return
        import re as _re
        captions = []
        for p in (req.extra_user_content_parts or []):
            text = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") or ""
            if text and _re.search(r"\[Image\s+\d+\s+描述\]", text):
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
    # mmx CLI 封装
    # =========================================================================
    def _build_vision_command(self, image, prompt):
        if image.startswith("file-"):
            cmd = ["vision", "describe", "--file-id", image]
        else:
            cmd = ["vision", "describe", "--image", image]
        if prompt:
            cmd.extend(["--prompt", prompt])
        return tuple(cmd)

    async def _run_mmx(self, *args, timeout) -> MmxResult:
        if not self.mmx_path:
            return MmxResult("", "mmx CLI 未配置或未安装", -1, False)
        if self._should_log("mmx_subprocess"):
            logger.info("[vision_text_bridge] mmx cmd: %s", " ".join(self._redact(args)))
        proc = await asyncio.create_subprocess_exec(
            self.mmx_path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            logger.warning("[vision_text_bridge] mmx 子进程超时(%ss): %s", timeout, " ".join(self._redact(args)))
            return MmxResult("", f"mmx timeout after {timeout}s", -1, False)
        stdout_s = stdout.decode("utf-8", errors="replace")
        stderr_s = stderr.decode("utf-8", errors="replace")
        if self._should_log("mmx_subprocess"):
            logger.info(
                "[vision_text_bridge] mmx rc=%d, stdout=%dB, stderr=%dB\n%s\n%s",
                proc.returncode, len(stdout_s), len(stderr_s),
                self._redact_text(stdout_s[:2000]), self._redact_text(stderr_s[:2000]),
            )
        return MmxResult(stdout_s, stderr_s, proc.returncode, proc.returncode == 0)

    async def _login_mmx(self, api_key: str) -> None:
        if not self.mmx_path:
            return
        masked = (f"{api_key[:4]}***REDACTED***(len={len(api_key)})"
                  if self.config.get("redact_sensitive", True) else api_key)
        logger.info("[vision_text_bridge] 预登录 MiniMax CLI: %s", masked)
        try:
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
        if not self.npm_path:
            logger.warning("[vision_text_bridge] 未找到 npm，无法自动安装 mmx-cli")
            return
        logger.info("[vision_text_bridge] 开始自动安装 mmx-cli...")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.npm_path, "install", "-g", "mmx-cli",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("[vision_text_bridge] 自动安装 mmx-cli 超时")
                return
            if proc.returncode != 0:
                logger.warning("[vision_text_bridge] 自动安装失败: %s", stderr.decode("utf-8", errors="replace"))
            else:
                logger.info("[vision_text_bridge] mmx-cli 安装完成")
        except Exception:
            logger.exception("[vision_text_bridge] 自动安装 mmx-cli 异常")

    # =========================================================================
    # mmx 错误诊断（首次出现警告一次）
    # =========================================================================
    def _diagnose_mmx_error(self, err_text: str, url: str) -> None:
        if not err_text:
            return
        lo = err_text.lower()
        if "insufficient balance" in lo or "余额" in err_text or ("quota" in lo and ("exceed" in lo or "limit" in lo or "不足" in err_text)):
            self._warn_once("balance", "[vision_text_bridge] mmx 报 'insufficient balance'。可能：\n"
                "  (1) mmx 路由到不识别该 key 的 endpoint\n"
                "  (2) 该 key 实际属另一环境（staging/test），未在生产 Token Plan 中\n"
                "  (3) mmx CLI 版本过旧、调用已废弃 endpoint\n"
                "  (4) 这个 key 仅开通 text、未开通 vision\n"
                "排查：`mmx --version` / `mmx auth status` / `mmx quota` / "
                "手动 `mmx vision describe --image <本地图>`\n"
                "若 1~3 正常但 4 报错，几乎确认是 mmx 版本/endpoint 问题，"
                "请加 `verbose_mmx_subprocess: true` 后重试。")
            return
        if "http 200" in lo or ("http" in lo and "error" in lo and "code" in lo):
            self._warn_once("http200", "[vision_text_bridge] mmx 返回 HTTP 200 但 body 是 error JSON。\n"
                "通常：mmx CLI 过旧 / key 在该 endpoint 无权限 / key 属另一环境。\n"
                "调试：`mmx --version` / `mmx auth status` / `mmx quota` / "
                "手动 `mmx vision describe --image <本地图>`。")
            return
        if "unauthenticated" in lo or "unauthorized" in lo \
                or ("auth" in lo and ("expired" in lo or "invalid" in lo)) \
                or "认证失败" in err_text or "未登录" in err_text:
            self._warn_once("auth", "[vision_text_bridge] mmx 认证失败。检查 minimax_api_key / "
                "`mmx auth status` / 手动 `mmx auth login --api-key <key>`。")
            return
        if "invalid argument" in lo or "no such file" in lo or "file not found" in lo or "model not found" in lo or "unknown model" in lo:
            self._warn_once("argument", f"[vision_text_bridge] mmx 参数/模型错误。可能：\n"
                f"  (1) 图片路径不可访问：{self._preview(url)}\n"
                f"  (2) mmx 不识别该模型名。手动 `mmx vision describe --image <本地图>` 验证。")
            return
        if "timeout" in lo or "connection" in lo or "network" in lo or "eof" in lo:
            self._warn_once("network", "[vision_text_bridge] mmx 网络异常。手动 `mmx quota` 验证。")
            return

    def _warn_once(self, key: str, message: str) -> None:
        if key in VisionTextBridgePlugin._DIAGNOSED:
            return
        VisionTextBridgePlugin._DIAGNOSED.add(key)
        logger.warning(message)

    # =========================================================================
    # 工具
    # =========================================================================
    def _truncate(self, text: str) -> str:
        max_len = int(self.config.get("max_description_length", 800) or 0)
        if max_len <= 0 or len(text) <= max_len:
            return text
        return text[:max_len] + "…"

    def _preview(self, text: str, limit: int = 80) -> str:
        if not text:
            return ""
        s = str(text)
        if self.config.get("redact_sensitive", True):
            s = self._redact_text(s)
        return s if len(s) <= limit else s[:limit] + "…"

    def _redact(self, args):
        if not self.config.get("redact_sensitive", True):
            return args
        return tuple(self._redact_text(a) for a in args)

    _SENSITIVE = (
        re.compile(r"(sk-[A-Za-z0-9_-]{8,})"),
        re.compile(r"(?i)(token|signature|x-sign)=[^&\s]+"),
    )

    @staticmethod
    def _redact_text(text: str) -> str:
        if not text:
            return text
        for p in VisionTextBridgePlugin._SENSITIVE:
            text = p.sub(lambda m: m.group(0)[:4] + "***REDACTED***", text)
        return text

    async def _compute_image_cache_key(self, url):
        try:
            data = await self._read_image_bytes(url)
        except Exception as e:
            if self._should_log("id_computation"):
                logger.debug("[vision_text_bridge] 读图字节失败，image_id 退到 md5(url): %s, err=%s",
                             self._preview(url), e)
            return CaptionCache.make_id_from_url(url)
        if not data:
            return CaptionCache.make_id_from_url(url)
        if self._should_log("id_computation"):
            logger.info("[vision_text_bridge] image_id=md5(%dB)=%s", len(data),
                        CaptionCache.make_id_from_bytes(data)[:16] + "…")
        return CaptionCache.make_id_from_bytes(data)

    async def _read_image_bytes(self, url):
        """v0.8.7.4: 支持裸本地路径（除 http(s)/file:// 之外，以 / 开头的绝对路径）。"""
        lo = url.lower()
        if lo.startswith("file://"):
            path = unquote(url[len("file://"):])
            if path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            return await asyncio.to_thread(_read_file_bytes_sync, path)
        if lo.startswith("http://") or lo.startswith("https://"):
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    r.raise_for_status()
                    return await r.read()
        # v0.8.7.4: 裸本地路径
        if lo.startswith("/") or (len(lo) >= 2 and lo[1] == ":"):
            return await asyncio.to_thread(_read_file_bytes_sync, url)
        raise ValueError(f"unsupported scheme: {url[:50]}")


# ===========================================================================
# 模块级 helper（不依赖 self，方便 inlining）
# ===========================================================================
def _is_image_url_part(part) -> bool:
    if isinstance(part, dict):
        return part.get("type") == "image_url"
    return getattr(part, "type", None) == "image_url"


def _extract_url_from_item(item) -> str:
    """从 image_url 类型的 part/dict 取 URL。"""
    if isinstance(item, dict):
        iu = item.get("image_url")
        if isinstance(iu, str):
            return iu
        if isinstance(iu, dict):
            return iu.get("url", "") or ""
    else:
        iu = getattr(item, "image_url", None)
        if iu is None:
            return ""
        if isinstance(iu, str):
            return iu
        return getattr(iu, "url", "") or ""
    return ""


def _extract_urls_from_parts(parts):
    return [u for p in parts if (u := _extract_url_from_item(p))]


def _extract_urls_from_context_list(content_list):
    urls = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "image_url":
            u = _extract_url_from_item(item)
            if u:
                urls.append(u)
    return urls


def _is_data_url(url: str) -> bool:
    return bool(url) and url.startswith("data:image/") and ";base64," in url[:64]


def _strip_image_urls(req, only_data_url: bool) -> int:
    """从 req 三处删 image_url 组件。only_data_url=True 只删 data:base64。"""
    removed = 0
    if req.image_urls:
        kept = [u for u in req.image_urls if not (only_data_url and _is_data_url(u))]
        if only_data_url:
            removed += len(req.image_urls) - len(kept)
        else:
            removed += len(req.image_urls)
            kept = []
        req.image_urls = kept
    if req.extra_user_content_parts:
        kept = []
        for p in req.extra_user_content_parts:
            if _is_image_url_part(p):
                u = _extract_url_from_item(p)
                if only_data_url and not _is_data_url(u):
                    kept.append(p)
                    continue
                removed += 1
                continue
            kept.append(p)
        req.extra_user_content_parts[:] = kept
    if req.contexts:
        for c in req.contexts:
            if not isinstance(c, dict):
                continue
            content = c.get("content")
            if not isinstance(content, list):
                continue
            kept = []
            for x in content:
                if isinstance(x, dict) and x.get("type") == "image_url":
                    u = _extract_url_from_item(x)
                    if only_data_url and not _is_data_url(u):
                        kept.append(x)
                        continue
                    removed += 1
                    continue
                kept.append(x)
            content[:] = kept
    return removed


def _to_text_part(part_dict):
    if TextPart is not None and isinstance(part_dict, dict):
        return TextPart(text=part_dict.get("text", ""))
    return part_dict


def _sniff_image_meta(data: bytes):
    """嗅探 (mime, w, h)。优先 PIL，降级 magic-bytes。失败返 ('', 0, 0)。"""
    if not data or len(data) < 16:
        return "", 0, 0
    try:
        from PIL import Image as _PIL
        with _PIL.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "JPG": "image/jpeg",
                    "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp",
                    "ICO": "image/x-icon"}.get(fmt, "image/jpeg")
            return mime, int(im.width or 0), int(im.height or 0)
    except Exception:
        pass
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                break
            m = data[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return "image/jpeg", w, h
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
        return "image/jpeg", 0, 0
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30:
            return "image/webp", int.from_bytes(data[26:28], "little") & 0x3FFF, \
                              int.from_bytes(data[28:30], "little") & 0x3FFF
        if data[12:16] == b"VP8L" and len(data) >= 25:
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            w = ((b1 & 0x3F) << 8 | b0) + 1
            h = (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)) + 1
            return "image/webp", w, h
        return "image/webp", 0, 0
    return "", 0, 0


def _is_cacheable_url(url: str, config) -> bool:
    """v0.8.7.4: 接受裸本地路径 (如 /AstrBot/data/temp/io_temp_img_*.jpg)。

    v0.8.7.3 及之前只认 http:// / https:// / file://，但实际场景中
    AstrBot 直接传裸路径 (无 scheme)。那种情况下 cacheable=False →
    ``_describe_one`` 里 ``if cacheable and cache_key`` 跳过所有缓存
    逻辑，包括最后的 ``_persist`` 写 SQLite。webui 看上去"总是空"。

    现在识别的 scheme：
      - http://, https://
      - file://
      - 以 / 开头（Unix 绝对路径）
      - Windows 盘符开头 C:\\ 或 C:/
      - data:image/... （不入缓存，base64 重复太多）
    """
    if not url:
        return False
    lo = url.lower()
    if lo.startswith("http://") or lo.startswith("https://"):
        return True
    if lo.startswith("file://"):
        return bool(config.get("cache_file_paths", True))
    if lo.startswith("data:image/"):
        return False
    # 裸本地路径: Unix 绝对路径 或 Windows 盘符
    if lo.startswith("/") or (len(lo) >= 2 and lo[1] == ":"):
        return bool(config.get("cache_file_paths", True))
    return False
