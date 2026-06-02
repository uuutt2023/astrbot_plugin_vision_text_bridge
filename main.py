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
import os
import re
import shutil
import time
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


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

        # URL -> 描述的内存缓存。仅对 http(s) URL 生效，base64/file:// 跳过缓存。
        self._description_cache: dict[str, str] = {}
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
        """AstrBot 启动插件后调用：处理 mmx-cli 安装、预登录、初始化信号量。"""
        max_concurrent = max(1, int(self.config.get("max_concurrent_vision", 3) or 1))
        self._vision_semaphore = asyncio.Semaphore(max_concurrent)

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

    async def terminate(self) -> None:
        """AstrBot 关闭插件时调用：清理缓存。"""
        self._description_cache.clear()
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

        try:
            await self._process_request(event, req)
        except Exception as exc:  # 防御性兜底，绝不让插件崩溃整个请求
            logger.exception("[vision_text_bridge] 处理请求时发生未捕获异常: %s", exc)

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
        """对单张图片执行图像理解，含缓存与超时控制。"""
        url = (url or "").strip()
        if not url:
            return ""

        # 1) 缓存命中
        if self.config.get("cache_descriptions", True) and self._is_cacheable_url(url):
            cached = self._description_cache.get(url)
            if cached is not None:
                logger.debug(
                    "[vision_text_bridge] 命中缓存: %s -> %s",
                    self._safe_preview(url),
                    self._safe_preview(cached),
                )
                return cached

        # 2) 启动子进程调用 mmx
        timeout = max(5, int(self.config.get("command_timeout", 60) or 60))
        vision_prompt = (
            self.config.get("vision_prompt", "")
            or "请用中文详细描述这张图片的内容，重点关注主体、场景、文字（如有）和关键细节。"
        )
        command = self._build_vision_command(url, vision_prompt)

        assert self._vision_semaphore is not None
        async with self._vision_semaphore:
            t0 = time.monotonic()
            try:
                stdout, stderr = await self._run_mmx(*command, timeout=timeout)
                elapsed = time.monotonic() - t0
                description = (stdout or "").strip()
                if not description:
                    description = (
                        (stderr or "").strip()
                        or "MiniMax CLI 未返回描述文本"
                    )
                description = self._truncate(description)
                logger.info(
                    "[vision_text_bridge] 图像理解完成: %s, 耗时=%.2fs, 长度=%d",
                    self._safe_preview(url),
                    elapsed,
                    len(description),
                )
                # 写缓存
                if self.config.get("cache_descriptions", True) and self._is_cacheable_url(url):
                    self._description_cache[url] = description
                return description
            except asyncio.TimeoutError:
                logger.warning(
                    "[vision_text_bridge] 图像理解超时(%ss): %s",
                    timeout,
                    self._safe_preview(url),
                )
                return ""
            except Exception as exc:
                logger.warning(
                    "[vision_text_bridge] 图像理解失败: %s, error=%s",
                    self._safe_preview(url),
                    exc,
                )
                return ""

    def _attach_descriptions_to_prompt(
        self,
        req: ProviderRequest,
        descriptions: list[tuple[int, str, str]],
        start_index: int,
        field: str,
        context_target: dict | None = None,
    ) -> None:
        """把 ``(index, url, description)`` 列表拼成【图片：...】文本，注入到对应位置。"""
        if not descriptions:
            return

        # 找到所有 description 为空的项（=调用失败），统一用 failure_message 兜底
        placeholder_template = (
            self.config.get("image_placeholder_template", "")
            or "【图片{index}：{description}】"
        )
        failure_template = (
            self.config.get("failure_message", "") or "【图片{index}：理解失败：{error}】"
        )

        rendered_parts: list[str] = []
        successful_urls: list[str] = []
        for offset, (orig_index, url, description) in enumerate(descriptions):
            global_index = start_index + offset
            if description:
                rendered_parts.append(
                    placeholder_template.format(
                        index=global_index, description=description
                    )
                )
                successful_urls.append(url)
            else:
                # 用失败占位保留图片位置（让 LLM 知道“用户确实发了图但我们没读出来”）
                rendered_parts.append(
                    failure_template.format(
                        index=global_index, error="mmx 调用失败或超时"
                    )
                )

        text_block = "\n".join(rendered_parts)

        # 1) 始终把图说塞进主 prompt：这是用户最关心的输入
        if req.prompt:
            req.prompt = f"{req.prompt}\n\n{text_block}"
        else:
            req.prompt = text_block

        # 2) 把成功描述的图片从原字段里移除，避免 LLM 仍然按多模态处理
        # image_urls 字段：最简单，直接清空成功的 URL
        if field == "image_urls" and req.image_urls:
            remaining = [u for u in req.image_urls if u not in successful_urls]
            req.image_urls = remaining
        elif field == "extra_user_content_parts" and req.extra_user_content_parts:
            self._remove_image_parts(req.extra_user_content_parts, successful_urls)
        elif field == "contexts" and context_target is not None:
            content = context_target.get("content")
            if isinstance(content, list):
                self._remove_image_urls_in_context_list(content, successful_urls)

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
    ) -> tuple[str, str]:
        """异步执行 mmx CLI，返回 ``(stdout, stderr)``。失败抛 RuntimeError。"""
        if not self.mmx_path:
            raise RuntimeError("mmx CLI 未配置或未安装")

        # 隐藏敏感凭据
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
            raise

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            message = (
                stderr_text.strip()
                or stdout_text.strip()
                or f"退出码 {process.returncode}"
            )
            raise RuntimeError(self._redact_text(message))

        return stdout_text, stderr_text

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
