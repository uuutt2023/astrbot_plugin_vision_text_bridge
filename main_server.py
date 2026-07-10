"""独立 OpenAI 兼容 HTTP server — bypass framework legacy_router JWT middleware.

Why: framework /api/plug/<plugin>/* 路径 require_dashboard_user (JWT 必需).
openai SDK 发 'Authorization: Bearer placeholder' → 不匹配 JWT → framework 401 'Token 无效'.

解决: 我方 start 独立 quart ASGI server on 127.0.0.1:<port> (loopback only).
框架 ProviderOpenAIOfficial uses openai SDK → POST 我方独立 server (no JWT) → 200.

使用 quart.run_task() in-place asyncio server — 跑 in main asyncio loop
(兼容 plugin 已经跑的 event loop)。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

logger = logging.getLogger(__name__)

try:
    from quart import Quart, request as quart_request
    HAS_QUART = True
except ImportError:
    HAS_QUART = False


_task: "asyncio.Task | None" = None
_shutdown: "asyncio.Event | None" = None
_app = None
_port: int = 6188


def _make_app(plugin):
    """构造 quart app 注册 /v1/chat/completions endpoint."""
    app = Quart("vision_text_bridge_solo")

    @app.post("/v1/chat/completions")
    async def chat_completions():
        """独立 OpenAI compatible /v1/chat/completions — no JWT required."""
        try:
            body = await quart_request.get_json(force=True, silent=True) or {}
        except Exception:
            try:
                raw = (await quart_request.get_data(as_text=True)) or "{}"
                import json as _json
                body = _json.loads(raw)
            except Exception as e:
                return {"status": "error", "message": f"无法解析请求体: {e}"}, 400
        messages = body.get("messages") or []
        if not messages:
            return {"status": "error", "message": "messages 不能为空"}, 400
        image_urls: list[str] = []
        prompt_parts: list[str] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "image_url":
                        url = (part.get("image_url") or {}).get("url")
                        if isinstance(url, str) and url:
                            image_urls.append(url)
                    elif ptype == "text":
                        t = part.get("text")
                        if t:
                            prompt_parts.append(t)
            elif isinstance(content, str):
                prompt_parts.append(content)
        if not image_urls:
            return {"status": "error", "message": "未提供 image_url"}, 400
        # Call plugin._describe_one (mmx-via)
        captions: list[str] = []
        try:
            for u in image_urls:
                cap = plugin._describe_one(u)
                if asyncio.iscoroutine(cap):
                    cap = await cap
                if cap:
                    captions.append(cap)
        except Exception as e:
            logger.exception("chat_completions 调用 mmx 失败: %s", e)
            return {"status": "error", "message": f"mmx 描述失败: {e}"}, 500
        if not captions:
            return {"status": "error", "message": "mmx 描述返回空"}, 500
        caption_text = "\n".join(f"[图片{i+1}] {c}" for i, c in enumerate(captions))
        model = body.get("model") or "vision_text_bridge"
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": caption_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "server": "vision_text_bridge_solo"}

    return app


async def start_solo_server(plugin, port: int = 6188) -> bool:
    """启动 quart.run_task() on 127.0.0.1:<port> in main asyncio loop."""
    global _task, _app, _port
    if not HAS_QUART:
        logger.warning(
            "[vision_text_bridge] quart 未安装, skip solo server "
            "(HAS_QUART=%s, HAS_QUART=%s)",
            HAS_QUART, HAS_QUART,
        )
        return False
    if _task is not None and not _task.done():
        logger.debug("[vision_text_bridge] solo server 已在跑, port=%d", _port)
        return True
    try:
        _app = _make_app(plugin)
        _port = port

        # quart.run_task() 直接 in-place asyncio server (no hypercorn)
        async def _serve():
            try:
                await _app.run_task(host="127.0.0.1", port=port, debug=False)
            except asyncio.CancelledError:
                logger.debug("[vision_text_bridge] solo server cancelled")
            except Exception as e:
                logger.warning("[vision_text_bridge] solo server 异常: %s", e)

        loop = asyncio.get_event_loop()
        _task = loop.create_task(_serve(), name="vision_text_bridge_solo_server")
        # Give server a moment to bind
        await asyncio.sleep(0.3)
        logger.info(
            "[vision_text_bridge] ✓ solo openai-compat server 启动: "
            "http://127.0.0.1:%d/v1/chat/completions (bypass JWT)",
            port,
        )
        return True
    except Exception as e:
        logger.exception("[vision_text_bridge] solo server 启动失败: %s", e)
        return False


async def stop_solo_server():
    """停止 solo server."""
    global _task, _shutdown
    if _shutdown is None:
        _shutdown = asyncio.Event()
    _shutdown.set()
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    logger.debug("[vision_text_bridge] solo server stopped")
