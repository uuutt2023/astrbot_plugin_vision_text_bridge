"""
astrbot_plugin_vision_text_bridge
==================================

拦截发送给 LLM 的请求，把里面的图片转成 MiniMax CLI 图像理解的文本描述，
再用 ``【图片：理解内容】`` 的形式回填到请求中，再交给对话模型。

参考实现：
- 消息拦截 -> ``astrbot_plugin_uni_nickname`` 的 ``@filter.on_llm_request`` 用法
- 图像理解 -> ``astrbot_plugin_MiniMax_CLI`` 的 ``mmx vision describe`` 子进程调用
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


# ---------------------------------------------------------------------------
# 动态加载同级模块
# ---------------------------------------------------------------------------
# AstrBot 加载插件时不会自动把插件目录加到 sys.path，因此不能直接用
# ``from caption_cache import CaptionCache``。采用 importlib 动态加载
# 保证 main.py 与同级 .py 文件能在任何加载环境下被一起 import。
# ---------------------------------------------------------------------------
def _load_sibling_module(name: str):
    """加载插件目录下与 main.py 同级的指定 .py 文件。"""
    here = Path(__file__).resolve().parent
    target = here / f"{name}.py"
    if not target.exists():
        raise ImportError(
            f"插件目录中找不到依赖文件: {target}。"
            f"请确认 {name}.py 与 main.py 在同一目录下。"
        )
    spec = importlib.util.spec_from_file_location(
        f"astrbot_plugin_vision_text_bridge.{name}", target
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建 spec: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sibling_cache = _load_sibling_module("caption_cache")
_sibling_link = _load_sibling_module("chat_archive_link")

CaptionCache = _sibling_cache.CaptionCache
CaptionEntry = _sibling_cache.CaptionEntry
CacheStats = _sibling_cache.CacheStats
ChatArchiveLink = _sibling_link.ChatArchiveLink


# 插件名（用于 web API 路径前缀）
PLUGIN_NAME = "astrbot_plugin_vision_text_bridge"


# ---------------------------------------------------------------------------
# 拦截优先级
# ---------------------------------------------------------------------------
# AstrBot 的 on_llm_request 钩子按 priority 降序执行（值越大越先运行）。
# 默认 100，高于多数常见插件（如 uni_nickname 的 0），能保证本插件在它们的
# 图片处理之前先把图片转成文本。
#
# 如果还有插件抢在本插件前面（比如 conversation_ledger 的“图片转述”、
# AngelHeart 等），请按以下两种方式调整：
#   1. 临时：在 AstrBot 管理面板修改本插件的 priority 配置项，然后重启 AstrBot。
#   2. 永久：直接修改下面的 DEFAULT_PRIORITY 常量，重启 AstrBot。
#
# 注意：AstrBot 的 on_llm_request priority 在 import 时锁定，不能热更新。
DEFAULT_PRIORITY = 100


@register(
    "astrbot_plugin_vision_text_bridge",
    "Mavis",
    "把图片转成 MiniMax CLI 图像理解后的文本，再喂给对话 LLM",
    "1.0.0",
)
class VisionTextBridgePlugin(Star):
    """Vision -> Text 桥接插件。

    典型链路：

    1. 监听 AstrBot 抛出的 ``on_llm_request`` 事件；
    2. 扫描 ``req.image_urls``、``req.extra_user_content_parts``、``req.contexts`` 里的图片；
    3. 对每张图片调用 ``mmx vision describe --image <url> --prompt <...>``；
    4. 拿到描述后用 ``【图片{n}：<描述>】`` 模板回填到 ``req.prompt``；
    5. 把图片从对应字段里移除，让 LLM 收到纯文本请求。
    """

    # mmx vision describe 命令的子命令 + action
    _MMX_VISION_ACTION = "vision"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 解析 mmx 路径：优先用配置项，其次从 PATH 查找
        configured = (self.config.get("mmx_path") or "").strip()
        if configured and os.path.isfile(configured):
            self.mmx_path = configured
        else:
            self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd")
        self.npm_path = shutil.which("npm") or shutil.which("npm.cmd")

        # SQLite 描述缓存（持久化，跨重启保留）
        self._caption_cache: CaptionCache | None = None
        # 内存热缓存（仅当前进程内，避免频繁 SQLite 查询）
        self._description_cache: dict[str, str] = {}
        # Chat Archive 联动
        self._chat_archive_link: ChatArchiveLink | None = None
        # 并发控制信号量，初始为 0，在 initialize() 中按配置创建
        self._vision_semaphore: asyncio.Semaphore | None = None
        # 当前插件实例期望的 priority
        self._configured_priority: int = self._resolve_priority()

        # 兼容历史配置：早期版本可能没有 enabled
        if not self.config.get("enabled", True):
            logger.info(
                "[vision_text_bridge] 插件默认启用，但当前配置为关闭，将不会拦截任何请求"
            )
        logger.info(
            "[vision_text_bridge] 已加载，mmx_path=%s，启用状态=%s，priority=%d",
            self.mmx_path or "<未找到>",
            self.config.get("enabled", True),
            self._configured_priority,
        )
        self._warn_if_priority_mismatch()

    def _resolve_priority(self) -> int:
        """读取配置中的 priority，未配置则用默认 DEFAULT_PRIORITY。"""
        raw = self.config.get("priority", None)
        if raw is None or raw == "":
            return DEFAULT_PRIORITY
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[vision_text_bridge] priority 配置值非法 (%r)，回退到默认 %d",
                raw,
                DEFAULT_PRIORITY,
            )
            return DEFAULT_PRIORITY
        return value

    def _warn_if_priority_mismatch(self) -> None:
        """检查 priority 配置值与 import 时锁定的 DEFAULT_PRIORITY 是否一致。

        AstrBot 的 on_llm_request priority 在 import 时锁定，不能热更新。
        如果配置值与当前注册值不一致，提示用户重启 AstrBot；
        同时把模块全局变量 DEFAULT_PRIORITY 更新为配置值，使下一次 import（重启后）生效。
        """
        global DEFAULT_PRIORITY
        if self._configured_priority == DEFAULT_PRIORITY:
            return
        if self._configured_priority < -1000 or self._configured_priority > 10000:
            logger.warning(
                "[vision_text_bridge] priority=%d 超出建议范围 [-1000, 10000]，"
                "请确认是否填错。",
                self._configured_priority,
            )
        logger.warning(
            "[vision_text_bridge] priority 配置=%d，但当前注册的 priority=%d。"
            "AstrBot 的 on_llm_request priority 在 import 时锁定，"
            "需要重启 AstrBot / 重新加载本插件后新值才会生效。"
            "如要永久调整，也可直接编辑 main.py 顶部的 DEFAULT_PRIORITY 常量。",
            self._configured_priority,
            DEFAULT_PRIORITY,
        )
        DEFAULT_PRIORITY = self._configured_priority

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        """AstrBot 启动插件后调用：处理 mmx-cli 安装、预登录、初始化缓存、注册页面 API。"""
        max_concurrent = max(1, int(self.config.get("max_concurrent_vision", 3) or 1))
        self._vision_semaphore = asyncio.Semaphore(max_concurrent)

        # 1. SQLite 描述缓存
        try:
            data_dir = self._get_plugin_data_dir()
            db_path = data_dir / "caption_cache.sqlite3"
            self._caption_cache = CaptionCache(db_path)
            logger.info(
                "[vision_text_bridge] 描述缓存已初始化: %s (条目数=%d)",
                db_path,
                self._caption_cache.count(),
            )
        except Exception as exc:
            logger.exception(
                "[vision_text_bridge] 初始化描述缓存失败，降级为内存缓存: %s", exc
            )
            self._caption_cache = None

        # 2. Chat Archive 联动
        try:
            data_dir = self._get_plugin_data_dir()
            self._chat_archive_link = ChatArchiveLink(plugin_data_dir=data_dir)
            if self._chat_archive_link.available:
                logger.info(
                    "[vision_text_bridge] Chat Archive 联动已启用，web_cache=%s",
                    self._chat_archive_link.web_cache_dir,
                )
        except Exception as exc:
            logger.exception("[vision_text_bridge] Chat Archive 联动检测失败: %s", exc)
            self._chat_archive_link = None

        # 3. 注册 web API (缓存管理页面用)
        try:
            self._register_web_apis()
        except Exception as exc:
            logger.exception("[vision_text_bridge] 注册 web API 失败: %s", exc)

        # 4. mmx-cli 安装与预登录
        if not self.mmx_path and self.config.get("auto_install_cli", False):
            await self._install_mmx_cli()
            self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd")

        if not self.mmx_path:
            logger.warning(
                "[vision_text_bridge] 未找到 mmx 命令。请先安装 mmx-cli：npm install -g mmx-cli。"
                "如已安装也可在插件配置中手动指定 mmx_path。"
            )
            return

        # mmx 已安装则尝试预登录。空 key 或关闭 auto_login 时跳过。
        if self.config.get("auto_login", True):
            api_key = (self.config.get("minimax_api_key") or "").strip()
            if not api_key:
                logger.info(
                    "[vision_text_bridge] 未配置 minimax_api_key，跳过自动登录。"
                    "若 mmx 尚未登录，vision describe 会失败。"
                )
            else:
                await self._login_mmx(api_key)

    def _get_plugin_data_dir(self) -> "Path":
        """拿到本插件的 data 目录。优先用 StarTools。"""
        try:
            from astrbot.api.star import StarTools
            p = Path(StarTools.get_data_dir())
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            # fallback: 插件目录下的 data 子目录
            p = Path(__file__).resolve().parent / "data"
            p.mkdir(parents=True, exist_ok=True)
            return p

    # ------------------------------------------------------------------ 页面 API

    def _register_web_apis(self) -> None:
        """注册 AstrBot 内置页面使用的后端 API。

        路由路径约定: /{PLUGIN_NAME}/<endpoint>
        页面中的 bridge.apiGet/apiPost("endpoint") 会转发到这里。
        """
        # quart 在 AstrBot 运行时由依赖提供；测试环境下没有，stub 一个
        try:
            from quart import jsonify
        except ImportError:
            def jsonify(obj):
                return obj

        def ok(data: Any):
            return jsonify({"ok": True, "data": data})

        def err(message: str, status: int = 400):
            return jsonify({"ok": False, "error": message}), status

        # --- GET /cache/stats ---
        async def api_cache_stats():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            stats = self._caption_cache.stats()
            chat_archive = {
                "available": bool(
                    self._chat_archive_link and self._chat_archive_link.available
                ),
                "web_cache_dir": (
                    str(self._chat_archive_link.web_cache_dir)
                    if self._chat_archive_link and self._chat_archive_link.web_cache_dir
                    else None
                ),
            }
            data = stats.to_dict()
            data["chat_archive"] = chat_archive
            data["in_memory_cache_size"] = len(self._description_cache)
            return ok(data)

        # --- GET /cache/list ---
        async def api_cache_list():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            try:
                request = self.context.request
                args = request.args if hasattr(request, "args") else {}
            except Exception:
                args = {}
            limit = int(args.get("limit", 50) or 50)
            offset = int(args.get("offset", 0) or 0)
            search = (args.get("search", "") or "").strip()
            order_by = (args.get("order_by", "created_at_desc") or "created_at_desc").strip()
            entries = self._caption_cache.list(
                limit=limit, offset=offset, search=search, order_by=order_by
            )
            total = self._caption_cache.count(search=search)
            return ok({
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": [e.to_dict() for e in entries],
            })

        # --- POST /cache/delete ---
        async def api_cache_delete():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            try:
                body = await self.context.request.json
            except Exception:
                body = {}
            key = (body.get("key") or "").strip()
            if not key:
                return err("缺少参数 key")
            # 同时从内存缓存和 SQLite 删
            self._description_cache.pop(key, None)
            ok_deleted = self._caption_cache.delete(key)
            return ok({"deleted": ok_deleted, "key": key})

        # --- POST /cache/clear ---
        async def api_cache_clear():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            n = self._caption_cache.clear()
            self._description_cache.clear()
            # VACUUM 释放空间
            try:
                self._caption_cache.vacuum()
            except Exception as exc:
                logger.warning("[vision_text_bridge] VACUUM 失败: %s", exc)
            return ok({"cleared": n})

        # --- POST /cache/regenerate ---
        async def api_cache_regenerate():
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            try:
                body = await self.context.request.json
            except Exception:
                body = {}
            key = (body.get("key") or "").strip()
            if not key:
                return err("缺少参数 key")
            # 从两个缓存删掉
            self._description_cache.pop(key, None)
            self._caption_cache.delete(key)
            # 重新调 mmx 生成
            new_desc = await self._describe_one(key)
            return ok({
                "key": key,
                "description": new_desc,
                "ok": bool(new_desc),
            })

        # --- GET /cache/export ---
        async def api_cache_export():
            """导出全部缓存为 JSON（页面上可触发下载）。"""
            if self._caption_cache is None:
                return err("SQLite 缓存未初始化", 500)
            entries = self._caption_cache.list(limit=10000, offset=0)
            return ok({
                "exported_at": time.time(),
                "count": len(entries),
                "items": [e.to_dict() for e in entries],
            })

        # --- POST /chat-archive/refresh ---
        async def api_chat_archive_refresh():
            """重新检测 Chat Archive 联动状态。"""
            if self._chat_archive_link is None:
                return err("Chat Archive 联动未启用")
            self._chat_archive_link.refresh()
            return ok({
                "available": self._chat_archive_link.available,
                "web_cache_dir": (
                    str(self._chat_archive_link.web_cache_dir)
                    if self._chat_archive_link.web_cache_dir
                    else None
                ),
            })

        # 路由: /{PLUGIN_NAME}/<endpoint>
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/stats", api_cache_stats, ["GET"], "Cache stats"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/list", api_cache_list, ["GET"], "Cache list"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/delete", api_cache_delete, ["POST"], "Delete cache entry"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/clear", api_cache_clear, ["POST"], "Clear all cache"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/regenerate", api_cache_regenerate, ["POST"], "Regenerate entry"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/export", api_cache_export, ["GET"], "Export cache as JSON"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/chat-archive/refresh",
            api_chat_archive_refresh,
            ["POST"],
            "Refresh chat archive link",
        )
        logger.info(
            "[vision_text_bridge] 已注册 7 个 web API 用于缓存管理页面"
        )

    async def terminate(self) -> None:
        """AstrBot 关闭插件时调用：清理缓存。"""
        self._description_cache.clear()
        # SQLite 连接随 CaptionCache 析构自动关闭
        self._caption_cache = None
        logger.info("[vision_text_bridge] 插件已卸载，缓存已清理")

    # ------------------------------------------------------------------ 拦截主入口

    @filter.on_llm_request(priority=DEFAULT_PRIORITY)
    async def bridge_vision_to_text(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """拦截 LLM 请求，把所有图片转成文本描述。"""
        if not self.config.get("enabled", True):
            return

        if not self.mmx_path:
            logger.warning(
                "[vision_text_bridge] 跳过本次拦截：未配置 mmx CLI。"
                "安装方式：npm install -g mmx-cli。"
            )
            return

        if self._vision_semaphore is None:
            # initialize 还没跑完（极少见），退化为无限并发
            self._vision_semaphore = asyncio.Semaphore(
                max(1, int(self.config.get("max_concurrent_vision", 3) or 1))
            )

        # 可选的冗余日志：让用户/调试者能确认钩子被触发
        if self.config.get("verbose_logging", False):
            n_image = len(req.image_urls or [])
            n_extra = sum(
                1
                for p in (req.extra_user_content_parts or [])
                if (isinstance(p, dict) and p.get("type") == "image_url")
                or (getattr(p, "type", None) == "image_url")
            )
            n_ctx = sum(
                1
                for c in (req.contexts or [])
                if isinstance(c, dict)
                and isinstance(c.get("content"), list)
                and any(
                    isinstance(x, dict) and x.get("type") == "image_url"
                    for x in c.get("content", [])
                )
            )
            logger.info(
                "[vision_text_bridge] on_llm_request 触发: image_urls=%d, "
                "extra_parts_images=%d, contexts_with_images=%d, priority=%d",
                n_image,
                n_extra,
                n_ctx,
                self._configured_priority,
            )

        try:
            await self._process_request(event, req)
            # 可选：向 system_prompt 注入“严格引用”提示，避免 LLM 改写/扩充图说
            self._maybe_inject_system_prompt_guidance(req)
        except Exception as exc:  # 防御性兜底，绝不让插件崩溃整个请求
            logger.exception("[vision_text_bridge] 处理请求时发生未捕获异常: %s", exc)

    @filter.on_llm_request(priority=-10000)
    async def strip_residual_base64(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """链末兜底：清理残留的 image_url 组件。

        背景：某些插件（如 AngelHeart）的 on_llm_request 钩子会在中途重写
        req.contexts，把图片以 base64 data URL 形式塞回去。仅仅在主钩子
        (priority=DEFAULT_PRIORITY) 中清理会被覆盖。

        本钩子使用极低 priority (-10000)，保证在所有插件跑完后才执行。

        默认行为：仅删除 ``data:image/...;base64,...`` 形式（防 LLM 报
        "File name too long"）。

        可选行为：配置 ``strip_all_image_urls_in_fallback: true`` 后，
        **删除所有形式的 image_url 组件**（不仅是 base64）。适用于
        LLM provider 是 deepseek/anthropic 之类不识别 image_url 的
        纯文本 provider，以避免上报 400 错误。代价是 LLM 看不到任何
        图片信息（如果主钩子没成功转述）。

        仅删除不调 mmx——主钩子已经处理过这些图片了，重复调反而浪费。
        """
        if not self.config.get("enabled", True):
            return

        try:
            if self.config.get("strip_all_image_urls_in_fallback", False):
                removed = self._strip_all_image_urls(req)
                tag = "image_url"
            else:
                removed = self._strip_all_data_url_images(req)
                tag = "data:base64"
            if removed and self.config.get("verbose_logging", False):
                logger.info(
                    "[vision_text_bridge] 链末兜底: 删除了 %d 个 %s 残留",
                    removed, tag,
                )
        except Exception as exc:
            logger.exception("[vision_text_bridge] 链末兜底异常: %s", exc)

    async def _process_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """按顺序处理三类图片来源。"""
        image_index_start = 1

        # 1. 处理 req.image_urls（最常见的位置）
        if req.image_urls:
            descriptions = await self._describe_urls(req.image_urls)
            self._attach_descriptions_to_prompt(
                req,
                descriptions,
                start_index=image_index_start,
                field="image_urls",
            )
            image_index_start += len(descriptions)

        # 2. 处理 req.extra_user_content_parts（多模态 parts）
        if self.config.get("include_extra_parts", True) and req.extra_user_content_parts:
            urls = self._extract_image_urls_from_parts(req.extra_user_content_parts)
            if urls:
                descriptions = await self._describe_urls(urls)
                self._attach_descriptions_to_prompt(
                    req,
                    descriptions,
                    start_index=image_index_start,
                    field="extra_user_content_parts",
                )
                image_index_start += len(descriptions)

        # 3. 处理 req.contexts 历史（默认关闭，避免历史图片成本过高）
        if self.config.get("include_history", False) and req.contexts:
            for ctx in req.contexts:
                if not isinstance(ctx, dict):
                    continue
                content = ctx.get("content")
                if isinstance(content, list):
                    urls = self._extract_image_urls_from_context_list(content)
                    if urls:
                        descriptions = await self._describe_urls(urls)
                        self._attach_descriptions_to_prompt(
                            req,
                            descriptions,
                            start_index=image_index_start,
                            field="contexts",
                            context_target=ctx,
                        )
                        image_index_start += len(descriptions)

    # ------------------------------------------------------------------ 描述 & 替换

    async def _describe_urls(self, urls: list[str]) -> list[tuple[int, str, str]]:
        """批量调用 mmx vision describe。

        Returns:
            一个 list，每项是 ``(index, url, description_or_error)``。
            ``index`` 是 1-based 序号。
            描述失败时该项 ``description_or_error`` 是 ``""``，由调用方决定如何呈现。
        """
        results: list[tuple[int, str, str]] = []
        for idx, url in enumerate(urls, start=1):
            description = await self._describe_one(url)
            results.append((idx, url, description))
        return results

    async def _describe_one(self, url: str) -> str:
        """对单张图片执行图像理解，含多级缓存与超时控制。"""
        url = (url or "").strip()
        if not url:
            return ""

        cache_enabled = self.config.get("cache_descriptions", True)
        cacheable = self._is_cacheable_url(url) if cache_enabled else False

        # 1) 内存热缓存
        if cacheable and url in self._description_cache:
            logger.debug(
                "[vision_text_bridge] 命中内存缓存: %s", self._safe_preview(url),
            )
            return self._description_cache[url]

        # 2) SQLite 持久化缓存
        if cacheable and self._caption_cache is not None:
            entry = self._caption_cache.get(url)
            if entry is not None:
                logger.info(
                    "[vision_text_bridge] 命中 SQLite 缓存: %s (hit_count=%d)",
                    self._safe_preview(url),
                    entry.hit_count,
                )
                # 同步到内存缓存
                self._description_cache[url] = entry.description
                return entry.description

        # 2) 启动子进程调用 mmx
        timeout = max(5, int(self.config.get("command_timeout", 60) or 60))
        vision_prompt = (
            self.config.get("vision_prompt", "")
            or "请客观描述图中可见的元素，列出主体人物/物品、场景背景、出现的文字（原文）、色调、风格。\n"
            "严禁猜测未明确显示的游戏/番剧/品牌/角色名称——如果不能从图中明确看出，请说'无法确定'。\n"
            "描述中只能包含你看到的事实，不要补充背景知识或推断。"
        )
        command = self._build_vision_command(url, vision_prompt)

        assert self._vision_semaphore is not None
        async with self._vision_semaphore:
            t0 = time.monotonic()
            try:
                result = await self._run_mmx(*command, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[vision_text_bridge] 图像理解超时(%ss): %s",
                    timeout,
                    self._safe_preview(url),
                )
                return ""
            except Exception as exc:
                err_text = str(exc) or ""
                self._diagnose_mmx_error(err_text, url)
                logger.warning(
                    "[vision_text_bridge] 图像理解异常: %s, error=%s",
                    self._safe_preview(url),
                    exc,
                )
                return ""

            elapsed = time.monotonic() - t0
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode

            # 成功路径
            if result.ok and stdout.strip():
                description = self._truncate(stdout.strip())
                logger.info(
                    "[vision_text_bridge] 图像理解完成: %s, 耗时=%.2fs, 长度=%d",
                    self._safe_preview(url),
                    elapsed,
                    len(description),
                )
                # 默认输出描述前 100 字符到日志，方便诊断 mmx 质量
                # （不依赖 verbose_logging，让用户随时能看 mmx 实际返回了什么）
                logger.info(
                    "[vision_text_bridge] 描述预览: %s",
                    self._safe_preview(description, limit=120),
                )
                if cacheable:
                    # 同时写内存 + SQLite
                    self._description_cache[url] = description
                    if self._caption_cache is not None:
                        try:
                            self._caption_cache.put(
                                key=url,
                                url=url,
                                description=description,
                            )
                        except Exception as exc:
                            logger.warning(
                                "[vision_text_bridge] 写 SQLite 缓存失败: %s", exc
                            )
                return description

            # 失败路径：收集 stderr + stdout + returncode
            err_text = (
                stderr.strip()
                or stdout.strip()
                or f"mmx 退出码 {returncode}"
            )
            self._diagnose_mmx_error(err_text, url)
            logger.warning(
                "[vision_text_bridge] 图像理解失败: %s, exit_code=%d, error=%s",
                self._safe_preview(url),
                returncode,
                self._redact_text(err_text[:300]),
            )
            # verbose 模式：打印完整 stdout/stderr（但脱敏），便于诊断
            if self.config.get("verbose_logging", False):
                logger.info(
                    "[vision_text_bridge] mmx 完整输出:\n--- stdout ---\n%s\n--- stderr ---\n%s",
                    self._redact_text(stdout[:2000]),
                    self._redact_text(stderr[:2000]),
                )
            return ""

    # 常见 mmx 错误模式 → 诊断信息。仅在“该错误首次出现”时告警一次，避免刷屏
    _DIAGNOSED_MMX_ERRORS: set[str] = set()

    def _diagnose_mmx_error(self, err_text: str, url: str) -> None:
        """识别常见 mmx 错误并提供诊断提示，避免只看到裸错误信息。

        设计上仅对“首次出现”的错误类型告警一次（类变量缓存），
        避免多张图连续失败时刷屏。
        """
        if not err_text:
            return
        lowered = err_text.lower()

        # === 1. 余额不足 (优先于 HTTP 200 检查) ===
        if "insufficient balance" in lowered or "余额" in err_text or ("quota" in lowered and ("exceed" in lowered or "limit" in lowered or "不足" in err_text)):
            self._warn_once(
                "balance",
                "[vision_text_bridge] mmx 报 'insufficient balance'。\n"
                "注意：MiniMax Token Plan 通常应包含 mmx vision describe，\n"
                "如果你确认是 Token Plan 主账户、但仍报余额不足，可能是：\n"
                "  (1) mmx 路由到了一个不识别该 API key 的 endpoint；\n"
                "  (2) 该 key 实际属于另一个环境（staging/test），未在生产 Token Plan 中；\n"
                "  (3) mmx CLI 版本过旧、调用了已废弃的 endpoint；\n"
                "  (4) 这个 key 本身是专用的（比如只开 text，vision 未开通）。\n"
                "排查步骤：\n"
                "  1. `mmx --version`\n"
                "  2. `mmx auth status` 看当前绑定的环境\n"
                "  3. `mmx quota` 看面板是否正常\n"
                "  4. 手动跑 `mmx vision describe --image <本地图片>` 验证\n"
                "如果 1~3 都正常但 4 报错，几乎可以确认是 mmx 版本/endpoint 问题，\n"
                "请加 `verbose_logging: true` 后重试，本插件会输出 mmx 完整 stdout/stderr。",
            )
            return

        # === 2. 诡计: HTTP 200 + error body ===
        if "http 200" in lowered or ("http" in lowered and "error" in lowered and "code" in lowered):
            self._warn_once(
                "http200_error_body",
                "[vision_text_bridge] mmx 返回 HTTP 200 但 body 是 error JSON。\n"
                "这是一个 **诡计型错误模式**，说明 mmx 进程与 MiniMax API 后端可能协议不匹配，\n"
                "通常原因有三种：\n"
                "  (1) mmx CLI 版本过旧，调用的是已废弃的 endpoint；\n"
                "  (2) API key 在 mmx 路由到的 endpoint 上没有访问权限；\n"
                "  (3) API key 是另一个环境的（如 test/staging），与生产 Token Plan 不匹配。\n"
                "调试方法：\n"
                "  1. `mmx --version` 查看 mmx 版本；\n"
                "  2. `mmx auth status` 查看当前 key 绑定的环境；\n"
                "  3. `mmx quota` 查看面板是否能正常查询；\n"
                "  4. 手动 `mmx vision describe --image <本地图片路径>` 看是报同样错误。\n"
                "如果 1~3 都能跑、只有第 4 步报错，【几乎肯定】是 mmx 版本/endpoint 问题。",
            )
            return

        # === 3. 认证 / 登录问题 ===
        if (
            "unauthenticated" in lowered
            or "unauthorized" in lowered
            or ("auth" in lowered and ("expired" in lowered or "invalid" in lowered))
            or "认证失败" in err_text
            or "未登录" in err_text
        ):
            self._warn_once(
                "auth",
                "[vision_text_bridge] mmx 认证失败。请检查：\n"
                "  (1) minimax_api_key 是否有效；\n"
                "  (2) 环境内是否还残留之前 `mmx auth login` 登录的其他 key\n"
                "      (检查 `mmx auth status` 看是否覆盖成功)；\n"
                "  (3) 手动 `mmx auth login --api-key <key>` 重新登录试试。",
            )
            return

        # === 4. 参数 / 路径错误 ===
        if (
            "invalid argument" in lowered
            or "invalid_parameter" in lowered
            or "no such file" in lowered
            or "file not found" in lowered
            or "model not found" in lowered
            or "unknown model" in lowered
        ):
            self._warn_once(
                "argument",
                f"[vision_text_bridge] mmx 报参数/模型错误。可能原因：\n"
                f"  (1) 图片路径不可访问：{self._safe_preview(url)}\n"
                f"      本地路径 /AstrBot/data/temp/... 可能在 AstrBot 清理后失效；\n"
                f"  (2) mmx 不识别该模型名（需更新 mmx-cli）。\n"
                f"调试：手动 `mmx vision describe --image <任意本地图>` 验证。",
            )
            return

        # === 5. 网络问题 ===
        if (
            "timeout" in lowered
            or "connection" in lowered
            or "network" in lowered
            or "eof" in lowered
        ):
            self._warn_once(
                "network",
                "[vision_text_bridge] mmx 调用网络异常。检查 mmx 进程能否连上 MiniMax 后端。"
                "可手动 `mmx quota` 验证网络。",
            )
            return

    def _warn_once(self, key: str, message: str) -> None:
        """同一个错误 key 只警告一次（跨多次插件调用）。"""
        if key in VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS:
            return
        VisionTextBridgePlugin._DIAGNOSED_MMX_ERRORS.add(key)
        logger.warning(message)

    def _attach_descriptions_to_prompt(
        self,
        req: ProviderRequest,
        descriptions: list[tuple[int, str, str]],
        start_index: int,
        field: str,
        context_target: dict | None = None,
    ) -> None:
        """把每张图的描述作为 ``extra_user_content_parts`` 的 text block 注入到 user message。

        **设计原因**：
        其他 on_llm_request 插件（例如 ``astrbot_plugin_angel_heart``）会在自己
        的优先级中 **完全重写** ``req.prompt`` / ``req.contexts``，这会丢掉我们
        之前作为 prompt 字符串追加的【图片N：xxx】占位文本。

        AstrBot 的 ``req.extra_user_content_parts`` 是 **user message 的 content
        block 列表**（多模态 OpenAI 格式），是 **唯一** 不被那些重写插件修改
        的字段。LLM 看到的会是::

            {"role": "user", "content": [
                {"type": "text", "text": "@ai酱这是什么图片 妹妹"},
                {"type": "text", "text": "[Image 1 描述] 这是一张抖音评论区截图..."},
            ]}

        这样图说作为 user message 的自然组成部分传入，LLM 会把它当作“用户描述
        给他听”的信息，而不是“prompt 中的人工占位符”。

        **重要**：所有被处理过的 image_url 都会从原字段中清除，避免 LLM 同时
        看到原图 + 图说（会浪费 token 且可能让 LLM 直接看图，不读图说）。
        """
        if not descriptions:
            return

        placeholder_template = (
            self.config.get("image_placeholder_template", "")
            or "[Image {index} 描述] {description}"
        )
        failure_template = (
            self.config.get("failure_message", "")
            or "[Image {index} 描述] 理解失败：{error}"
        )

        parts_to_attach: list[dict] = []
        success_count = 0
        failure_count = 0
        for offset, (orig_index, url, description) in enumerate(descriptions):
            global_index = start_index + offset
            if description:
                text = placeholder_template.format(
                    index=global_index, description=description
                )
                success_count += 1
            else:
                text = failure_template.format(
                    index=global_index, error="mmx 调用失败或超时"
                )
                failure_count += 1
            parts_to_attach.append({"type": "text", "text": text})

        # 1) **以 content block 形式** 附加到 req.extra_user_content_parts
        #    这是 AstrBot 在 _encode_message 中**直接作为 user content block**
        #    发给 LLM 的字段，且**不被 AngelHeart 等重写插件修改**。
        if req.extra_user_content_parts is None:
            req.extra_user_content_parts = []
        for part in parts_to_attach:
            # 兼容 AstrBot 的 ContentPart Pydantic 模型：允许 dict 或对象
            req.extra_user_content_parts.append(part)

        # 2) **全部** 清除被处理过的 image_url，不留残留
        # 这样即使 mmx 调用失败，也不会让 raw image_url 走到 LLM 那里
        if field == "image_urls":
            req.image_urls = []
        elif field == "extra_user_content_parts" and req.extra_user_content_parts:
            self._remove_all_image_url_parts(req.extra_user_content_parts)
        elif field == "contexts" and context_target is not None:
            content = context_target.get("content")
            if isinstance(content, list):
                self._remove_all_image_url_components_in_context(content)

        if self.config.get("verbose_logging", False):
            logger.info(
                "[vision_text_bridge] field=%s 处理完成: 成功=%d, 失败=%d, "
                "注入位置=extra_user_content_parts",
                field, success_count, failure_count,
            )

    def _maybe_inject_system_prompt_guidance(
        self, req: ProviderRequest
    ) -> None:
        """向 system_prompt 注入“严格引用图说”提示。

        **设计变化**：从 v0.7 开始，图说本身不再注入到 system_prompt，**而是
        作为 user message 的 content block 注入到 ``req.extra_user_content_parts``**
        （看 :func:`_attach_descriptions_to_prompt`）。这是因为：
          1. ``req.extra_user_content_parts`` 是 AstrBot 在 LLM 请求中
             **直接当 user content block 使用** 的字段，不被任何重写插件修改。
          2. LLM 看到图说是在 user message 里（更自然），更容易遵守。
          3. system_prompt 里只留“严格引用”指导，文本量小、token 节省。

        本函数：
          - 检查 ``req.extra_user_content_parts`` 中是否有图说标记（[Image N 描述]）。
          - 如果有，向 ``req.system_prompt`` 追加“严格引用”指导。
          - 如果用户额外开启 ``inject_caption_text_to_system_prompt``，同时把
            图说复制一份到 system_prompt（冗余防覆盖，老用户兼容）。

        仅在配置 ``inject_system_prompt_guidance: true``（默认）时生效。
        """
        if not self.config.get("inject_system_prompt_guidance", True):
            return

        # 从 extra_user_content_parts 中检查图说标记
        import re
        captions: list[str] = []
        if req.extra_user_content_parts:
            for part in req.extra_user_content_parts:
                if isinstance(part, dict):
                    text = part.get("text", "")
                else:
                    text = getattr(part, "text", "") or ""
                if text and re.search(r"\[Image\s+\d+\s+描述\]", text):
                    captions.append(text)

        n = len(captions)
        if n <= 0:
            return

        if n == 1:
            tags_hint = "[Image 1 描述]"
        else:
            tags_hint = (
                ", ".join(f"[Image {i+1} 描述]" for i in range(n))
            )

        guidance_lines = [
            f"\n\n[视觉模型描述] 用户消息中包含 {n} 张图片，描述标记为 {tags_hint}。\n"
            f"请在回复时严格基于这些描述来回答用户，不要：\n"
            f"  - 猜测未在描述中明确出现的游戏/番剧/品牌/角色名；\n"
            f"  - 凭印象补充描述之外的背景知识；\n"
            f"  - 改写/扩充已描述的内容；\n"
            f"  - 装作“看到”描述中未出现的信息。\n"
            f"如果描述不足以回答用户问题，请明确说“无法从图中看出”。"
        ]
        guidance = "".join(guidance_lines)

        # 可选：把图说也复制一份到 system_prompt（冗余防覆盖）
        # 默认 False——因为已经在 user message 里了
        if self.config.get("inject_caption_text_to_system_prompt", False):
            captions_text = "\n\n".join(captions)
            guidance = (
                f"\n\n[视觉模型描述] 用户消息中包含 {n} 张图片，描述如下：\n\n"
                f"{captions_text}\n\n"
                f"以上描述标记为 {tags_hint}。\n"
                f"请在回复时严格基于这些描述来回答用户，不要：\n"
                f"  - 猜测未在描述中明确出现的游戏/番剧/品牌/角色名；\n"
                f"  - 凭印象补充描述之外的背景知识；\n"
                f"  - 改写/扩充已描述的内容；\n"
                f"  - 装作“看到”描述中未出现的信息。\n"
                f"如果描述不足以回答用户问题，请明确说“无法从图中看出”。"
            )

        if req.system_prompt:
            req.system_prompt = req.system_prompt + guidance
        else:
            req.system_prompt = guidance
        if self.config.get("verbose_logging", False):
            logger.info(
                "[vision_text_bridge] 已向 system_prompt 注入严格引用提示，"
                "图片数=%d, system_prompt 增量长度=%d",
                n, len(guidance),
            )

    # ------------------------------------------------------------------ 字段提取

    @staticmethod
    def _extract_image_urls_from_parts(parts: list[Any]) -> list[str]:
        """从 ``extra_user_content_parts`` 里抽取图片 URL。

        兼容 dict（已经被 model_dump 过的）和 ContentPart 对象两种形态。
        """
        urls: list[str] = []
        for part in parts:
            url = VisionTextBridgePlugin._extract_image_url_from_part(part)
            if url:
                urls.append(url)
        return urls

    @staticmethod
    def _extract_image_url_from_part(part: Any) -> str:
        """单条 part 抽取 URL，dict / pydantic 对象都支持。"""
        # 1) 优先看 type 字段
        ptype: str | None = None
        if isinstance(part, dict):
            ptype = part.get("type")
        else:
            ptype = getattr(part, "type", None)

        if ptype != "image_url":
            return ""

        # 2) 再去拿 image_url.url
        image_url_field: Any = None
        if isinstance(part, dict):
            image_url_field = part.get("image_url")
        else:
            image_url_field = getattr(part, "image_url", None)

        if image_url_field is None:
            return ""
        if isinstance(image_url_field, str):
            return image_url_field
        if isinstance(image_url_field, dict):
            return image_url_field.get("url", "") or ""
        # pydantic model
        return getattr(image_url_field, "url", "") or ""

    @staticmethod
    def _extract_image_urls_from_context_list(content_list: list[Any]) -> list[str]:
        """从 ``contexts[i].content``（多模态 list）里抽取图片 URL。"""
        urls: list[str] = []
        for item in content_list:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_url":
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, str):
                urls.append(image_url)
            elif isinstance(image_url, dict):
                url = image_url.get("url", "")
                if url:
                    urls.append(url)
        return urls

    @staticmethod
    def _remove_image_parts(parts: list[Any], urls_to_remove: list[str]) -> None:
        """就地删除 extra_user_content_parts 中匹配的 image_url 项。"""
        if not urls_to_remove:
            return
        url_set = set(urls_to_remove)
        keep: list[Any] = []
        for part in parts:
            url = VisionTextBridgePlugin._extract_image_url_from_part(part)
            if url and url in url_set:
                continue
            keep.append(part)
        parts[:] = keep

    @staticmethod
    def _remove_image_urls_in_context_list(
        content_list: list[dict], urls_to_remove: list[str]
    ) -> None:
        """就地删除 contexts.content list 中匹配的 image_url 项。"""
        if not urls_to_remove:
            return
        url_set = set(urls_to_remove)
        keep: list[dict] = []
        for item in content_list:
            if (
                isinstance(item, dict)
                and item.get("type") == "image_url"
                and VisionTextBridgePlugin._context_image_url(item) in url_set
            ):
                continue
            keep.append(item)
        content_list[:] = keep

    @staticmethod
    def _context_image_url(item: dict) -> str:
        image_url = item.get("image_url")
        if isinstance(image_url, str):
            return image_url
        if isinstance(image_url, dict):
            return image_url.get("url", "") or ""
        return ""

    @staticmethod
    def _is_image_url_part(part: Any) -> bool:
        """判断 part 是否是 image_url 类型（不管 URL 形式）。"""
        if isinstance(part, dict):
            return part.get("type") == "image_url"
        return getattr(part, "type", None) == "image_url"

    @staticmethod
    def _remove_all_image_url_parts(parts: list[Any]) -> None:
        """就地删除 extra_user_content_parts 中所有 image_url 项，不分 URL。"""
        keep: list[Any] = []
        for part in parts:
            if VisionTextBridgePlugin._is_image_url_part(part):
                continue
            keep.append(part)
        parts[:] = keep

    @staticmethod
    def _remove_all_image_url_components_in_context(content_list: list[dict]) -> None:
        """就地删除 contexts.content list 中所有 type=='image_url' 项。"""
        keep: list[dict] = []
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "image_url":
                continue
            keep.append(item)
        content_list[:] = keep

    @staticmethod
    def _is_data_url(url: str) -> bool:
        """判断是否是 ``data:image/...;base64,...`` 形式的 data URL。"""
        return bool(url) and url.startswith("data:image/") and ";base64," in url[:64]

    def _strip_all_data_url_images(self, req: ProviderRequest) -> int:
        """从 req 的三个位置扫描并删除 data:base64 image_url 组件。

        Returns:
            被删除的组件数量（image_urls 中按 URL 算，parts/contexts 中按项算）。
        """
        removed = 0

        # 1. req.image_urls：直接过滤 data URL
        if req.image_urls:
            kept = [u for u in req.image_urls if not self._is_data_url(u)]
            removed += len(req.image_urls) - len(kept)
            req.image_urls = kept

        # 2. req.extra_user_content_parts
        if req.extra_user_content_parts:
            kept: list[Any] = []
            for part in req.extra_user_content_parts:
                url = VisionTextBridgePlugin._extract_image_url_from_part(part)
                if url and self._is_data_url(url):
                    removed += 1
                    continue
                kept.append(part)
            req.extra_user_content_parts[:] = kept

        # 3. req.contexts[].content
        if req.contexts:
            for ctx in req.contexts:
                if not isinstance(ctx, dict):
                    continue
                content = ctx.get("content")
                if not isinstance(content, list):
                    continue
                kept_ctx: list[dict] = []
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "image_url"
                        and self._is_data_url(
                            VisionTextBridgePlugin._context_image_url(item)
                        )
                    ):
                        removed += 1
                        continue
                    kept_ctx.append(item)
                content[:] = kept_ctx

        return removed

    def _strip_all_image_urls(self, req: ProviderRequest) -> int:
        """从 req 的三个位置删除**所有** image_url，不区分 URL 形式。

        适用于 LLM provider 不支持 image_url 字段的场景（如 deepseek）。
        代价是 LLM 完全看不到任何图片信息（如果主钩子未成功转述）。
        """
        removed = 0

        # 1. req.image_urls
        if req.image_urls:
            removed += len(req.image_urls)
            req.image_urls = []

        # 2. req.extra_user_content_parts
        if req.extra_user_content_parts:
            kept: list[Any] = []
            for part in req.extra_user_content_parts:
                if VisionTextBridgePlugin._is_image_url_part(part):
                    removed += 1
                    continue
                kept.append(part)
            req.extra_user_content_parts[:] = kept

        # 3. req.contexts[].content
        if req.contexts:
            for ctx in req.contexts:
                if not isinstance(ctx, dict):
                    continue
                content = ctx.get("content")
                if not isinstance(content, list):
                    continue
                kept_ctx: list[dict] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        removed += 1
                        continue
                    kept_ctx.append(item)
                content[:] = kept_ctx

        return removed

    # ------------------------------------------------------------------ mmx CLI 封装

    def _build_vision_command(self, image: str, prompt: str) -> tuple[str, ...]:
        """构造 ``mmx vision describe`` 命令。

        模仿 ``astrbot_plugin_MiniMax_CLI`` 的 ``_build_vision_command``：
        - ``file-`` 开头用 ``--file-id``
        - 否则用 ``--image``
        - 后置 ``--prompt <prompt>`` 作为理解提示词
        """
        if image.startswith("file-"):
            command = ["vision", "describe", "--file-id", image]
        else:
            command = ["vision", "describe", "--image", image]
        if prompt:
            command.extend(["--prompt", prompt])
        return tuple(command)

    async def _run_mmx(
        self, *args: str, timeout: int
    ) -> "MmxResult":
        """异步执行 mmx CLI，返回 :class:`MmxResult`。

        永不抛异常（除非超时或 mmx 不可执行）；调用者根据 ``returncode`` / ``ok``
        决定后续处理。这样能把 mmx 的完整 stdout/stderr 交给诊断逻辑。

        Returns:
            MmxResult 包含 ``stdout`` / ``stderr`` / ``returncode`` / ``ok``。
        """
        from dataclasses import dataclass

        @dataclass
        class _Result:
            stdout: str
            stderr: str
            returncode: int
            ok: bool

        if not self.mmx_path:
            return _Result("", "mmx CLI 未配置或未安装", -1, False)

        redacted = self._redact(args)

        process = await asyncio.create_subprocess_exec(
            self.mmx_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            logger.warning(
                "[vision_text_bridge] mmx 子进程超时(%ss)，命令=%s",
                timeout,
                " ".join(redacted),
            )
            return _Result("", f"mmx timeout after {timeout}s", -1, False)

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # 诊断：HTTP 200 + error body 的诡异情况
        ok = process.returncode == 0
        return _Result(stdout_text, stderr_text, process.returncode, ok)

    async def _login_mmx(self, api_key: str) -> None:
        """调用 ``mmx auth login --api-key`` 做预登录。

        出错只告警，不抹环境。可能的原因：mmx 本身未初始化、key 无效、网络问题。
        """
        if not self.mmx_path:
            return
        # 脱敏后仅打印前 4 位 + 总长，方便排查
        masked = (
            f"{api_key[:4]}***REDACTED***(len={len(api_key)})"
            if self.config.get("redact_sensitive", True)
            else api_key
        )
        logger.info("[vision_text_bridge] 正在预登录 MiniMax CLI: %s", masked)
        try:
            stdout, stderr = await self._run_mmx(
                "auth", "login", "--api-key", api_key, timeout=30
            )
            logger.info(
                "[vision_text_bridge] MiniMax CLI 预登录成功: %s",
                (stdout or "").strip() or "(无输出)",
            )
        except Exception as exc:
            logger.warning(
                "[vision_text_bridge] MiniMax CLI 预登录失败: %s。"
                "请检查 minimax_api_key 是否正确，或在环境中手动执行 mmx auth login。",
                exc,
            )

    async def _install_mmx_cli(self) -> None:
        if not self.npm_path:
            logger.warning("[vision_text_bridge] 未找到 npm 命令，无法自动安装 mmx-cli")
            return
        logger.info("[vision_text_bridge] 开始自动安装 mmx-cli")
        try:
            process = await asyncio.create_subprocess_exec(
                self.npm_path,
                "install",
                "-g",
                "mmx-cli",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=600
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning("[vision_text_bridge] 自动安装 mmx-cli 超时")
                return
            if process.returncode != 0:
                logger.warning(
                    "[vision_text_bridge] 自动安装 mmx-cli 失败: %s",
                    (stderr or b"").decode("utf-8", errors="replace"),
                )
            else:
                logger.info("[vision_text_bridge] mmx-cli 安装完成")
        except Exception:
            logger.exception("[vision_text_bridge] 自动安装 mmx-cli 时发生异常")

    # ------------------------------------------------------------------ 工具方法

    def _truncate(self, text: str) -> str:
        max_len = int(self.config.get("max_description_length", 800) or 0)
        if max_len <= 0 or len(text) <= max_len:
            return text
        # 截断到 max_len 字符再加省略号，避免单字符被吞
        return text[:max_len] + "…"

    @staticmethod
    def _is_cacheable_url(url: str) -> bool:
        """只对 ``http(s)://`` URL 做缓存。base64 / file:// / 本地路径都跳过。"""
        if not url:
            return False
        lowered = url.lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    def _safe_preview(self, text: str, limit: int = 80) -> str:
        """日志预览：超过长度的字符串截断 + 敏感脱敏。"""
        if text is None:
            return ""
        s = str(text)
        if self.config.get("redact_sensitive", True):
            s = self._redact_text(s)
        if len(s) > limit:
            return s[:limit] + "…"
        return s

    def _redact(self, args: tuple[str, ...]) -> tuple[str, ...]:
        if not self.config.get("redact_sensitive", True):
            return args
        return tuple(self._redact_text(a) for a in args)

    _SENSITIVE_PATTERNS = (
        re.compile(r"(sk-[A-Za-z0-9_-]{8,})"),  # MiniMax API Key
        re.compile(r"(?i)(token|signature|x-sign)=[^&\s]+"),
    )

    @classmethod
    def _redact_text(cls, text: str) -> str:
        if not text:
            return text
        for pat in cls._SENSITIVE_PATTERNS:
            text = pat.sub(lambda m: m.group(0)[:4] + "***REDACTED***", text)
        return text
